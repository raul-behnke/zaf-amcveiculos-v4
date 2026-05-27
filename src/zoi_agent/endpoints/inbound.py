"""POST /webhook/inbound — gatilho do GHL.

Payload do GHL traz apenas `contact_id` útil. Tudo o mais vem por API:
  1) search_conversations(contact_id) -> conv_id + lastMessageDirection
     - se lastMessageDirection != inbound: 200 noop (foi a gente enviando)
  2) get_messages(conv_id) -> história completa
  3) pega último inbound, strip "Received on 📱[...]"
  4) áudio: transcreve via Whisper (concat múltiplos)
  5) imagem/doc sem texto: 200 noop
  6) dispatch orchestrator.process_turn(contact_id, last_message)
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request

from zoi_agent.audio.whisper import TranscriptionError, transcribe_url
from zoi_agent.config import settings
from zoi_agent.ghl import conversations as ghl_conv
from zoi_agent.logging import get_logger
from zoi_agent.orchestrator import process_turn
from zoi_agent.security import require_secret

router = APIRouter()
log = get_logger(__name__)


# --- Helpers --------------------------------------------------------------

_RECEIVED_ON_RE = re.compile(r"\s*Received on\s*📱?\s*\[.*?\]\s*$", re.IGNORECASE | re.DOTALL)
# Placeholders que o GHL/WhatsApp Plugin insere no body quando a mensagem é não-textual.
# Ex: "> Voice Note <", "> Image <", "> Video <", "> Document <"
_GHL_TYPE_MARKER_RE = re.compile(r"^\s*>\s*(Voice Note|Image|Video|Document|Sticker|Location|Audio|GIF)\s*<\s*$", re.IGNORECASE)
# Quote/reply do WhatsApp (linhas iniciadas com ↪︎ até a primeira linha em branco).
_QUOTE_PREFIX_RE = re.compile(r"^↪︎.*?\n\s*\n", re.DOTALL)


def strip_received_on(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _RECEIVED_ON_RE.sub("", text)
    cleaned = _QUOTE_PREFIX_RE.sub("", cleaned)
    # Linhas que são só marcadores GHL viram vazio
    out_lines: list[str] = []
    for line in cleaned.splitlines():
        if _GHL_TYPE_MARKER_RE.match(line):
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip()


_AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".m4a", ".mp4", ".wav", ".opus", ".aac", ".flac", ".webm"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}


def _ext_of(url: str) -> str:
    try:
        path = urlparse(url).path.lower()
        if "." in path:
            return "." + path.rsplit(".", 1)[-1]
    except Exception:
        pass
    return ""


def classify_attachments(urls: list[str]) -> dict[str, list[str]]:
    audio, image, other = [], [], []
    for u in urls or []:
        ext = _ext_of(u)
        if ext in _AUDIO_EXTS:
            audio.append(u)
        elif ext in _IMAGE_EXTS:
            image.append(u)
        else:
            other.append(u)
    return {"audio": audio, "image": image, "other": other}


def parse_tags_csv(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        return {str(t).strip() for t in value if str(t).strip()}
    if isinstance(value, str):
        return {p.strip() for p in value.split(",") if p.strip()}
    return set()


def extract_latest_inbound(messages: list[dict]) -> dict | None:
    """Retorna o último inbound. Lista pode vir ordenada DESC ou ASC; varremos."""
    inbound = [m for m in (messages or []) if m.get("direction") == "inbound"]
    if not inbound:
        return None
    # Ordena por dateAdded ascendente; pega o último
    inbound.sort(key=lambda m: m.get("dateAdded") or "")
    return inbound[-1]


def _unwrap_messages(payload: dict) -> list[dict]:
    block = payload.get("messages")
    if isinstance(block, dict):
        return block.get("messages") or []
    if isinstance(block, list):
        return block
    return []


# --- Endpoint -------------------------------------------------------------


@router.post("/webhook/inbound", dependencies=[Depends(require_secret)])
async def inbound(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    contact_id = (
        payload.get("contact_id")
        or payload.get("contactId")
        or (payload.get("contact") or {}).get("id")
    )
    if not contact_id:
        log.warning("webhook_no_contact_id", keys=list(payload.keys())[:20])
        return {"status": "ignored", "reason": "no contact_id"}

    # Gate por tag (CSV ou array no payload, ou via API se ausente)
    tags = parse_tags_csv(payload.get("tags"))
    if not tags:
        # fallback: busca via API
        try:
            from zoi_agent.ghl import contacts as gc

            contact = await gc.get_contact(contact_id)
            ctags = (contact.get("contact", contact) or {}).get("tags") or []
            tags = parse_tags_csv(ctags)
        except Exception as e:
            log.warning("webhook_tag_fetch_failed", err=str(e))

    if settings.ghl_tag_agent_gate not in tags:
        log.info("webhook_tag_missing", contact_id=contact_id, tags=sorted(tags))
        return {"status": "ignored", "reason": "no agent tag"}

    # Busca conversa + última mensagem
    try:
        search = await ghl_conv.search_conversations(contact_id)
    except Exception as e:
        log.error("webhook_conv_search_failed", err=str(e))
        return {"status": "error", "reason": "ghl search failed"}

    convs = search.get("conversations") or []
    if not convs:
        log.info("webhook_no_conversation", contact_id=contact_id)
        return {"status": "ignored", "reason": "no conversation"}

    conv = convs[0]
    conv_id = conv.get("id")
    last_dir = conv.get("lastMessageDirection")
    if last_dir != "inbound":
        log.info("webhook_last_outbound", contact_id=contact_id, dir=last_dir)
        return {"status": "ignored", "reason": "last message outbound"}

    try:
        msgs_resp = await ghl_conv.get_messages(conv_id)
    except Exception as e:
        log.error("webhook_messages_fetch_failed", err=str(e))
        return {"status": "error", "reason": "ghl messages failed"}

    messages = _unwrap_messages(msgs_resp)
    latest = extract_latest_inbound(messages)
    if not latest:
        log.info("webhook_no_inbound", contact_id=contact_id)
        return {"status": "ignored", "reason": "no inbound message"}

    body = strip_received_on(latest.get("body"))
    attachments = latest.get("attachments") or []
    classes = classify_attachments(attachments)

    # Áudio: transcreve e concatena. PLAN: texto + áudio = áudio se texto vazio.
    last_message_text: str | None = None
    if classes["audio"]:
        try:
            parts = []
            for url in classes["audio"]:
                t = await transcribe_url(url)
                if t:
                    parts.append(t)
            transcribed = "\n".join(parts).strip()
            last_message_text = (body + "\n" + transcribed).strip() if body else transcribed
        except TranscriptionError as e:
            log.error("webhook_whisper_failed", err=str(e), contact_id=contact_id)
            return {"status": "error", "reason": "whisper failed"}
    elif body:
        last_message_text = body
    elif classes["image"] or classes["other"]:
        # Imagem/doc sem texto → ignora (PLAN C12)
        log.info(
            "webhook_only_attachment_ignored",
            contact_id=contact_id,
            n_image=len(classes["image"]),
            n_other=len(classes["other"]),
        )
        return {"status": "ignored", "reason": "attachment only"}

    if not last_message_text:
        # Vazio total (mensagem só com emoji é processada — emoji vai em body normal)
        log.info("webhook_empty_message", contact_id=contact_id)
        return {"status": "ignored", "reason": "empty"}

    log.info(
        "webhook_dispatch",
        contact_id=contact_id,
        conv_id=conv_id,
        text_preview=last_message_text[:80],
        has_audio=bool(classes["audio"]),
    )
    # Dispatch (preempção dentro do orchestrator). Não esperamos o pipeline acabar.
    await process_turn(contact_id, last_message_text)
    return {"status": "accepted", "contact_id": contact_id, "conv_id": conv_id}
