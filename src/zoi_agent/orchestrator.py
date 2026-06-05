"""Per-contact orchestrator: preempção via Task table + pipeline updater->dispatch->responder->send.

Pipeline por turno:
  1) load session_state (DB)
  2) se terminal_reason setado: ignora (não responde)
  3) fetch histórico GHL (limit 100)
  4) run_updater -> StateUpdate
  5) merge_into_state
  6) tool dispatch baseado em update.intent_secundario
  7) run_responder com tool_outputs -> bubbles[]
  8) shield(send_bubbles)  -- não é cancelado por preempção
  9) save state (e marca terminal se update.terminal_reason)
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from zoi_agent.agent.question_planner import plan_next_question, push_asked_field
from zoi_agent.agent.responder import run_responder
from zoi_agent.agent.schemas import SessionState
from zoi_agent.agent.templates import build_vehicle_blocks, build_vehicle_blocks_with_ids
from zoi_agent.agent.updater import merge_into_state, run_updater
from zoi_agent.config import settings
from zoi_agent.db import sessions as session_repo
from zoi_agent.ghl import conversations as ghl_conv
from zoi_agent.logging import get_logger
from zoi_agent.metrics import TURNS_TOTAL
from zoi_agent.tools.calendar import book_appointment, find_exact_slot, propose_slots
from zoi_agent.tools.faq import get_faq_raw
from zoi_agent.tools.handoff import encaminhar_para_vendedor
from zoi_agent.tools.inventory import get_vehicle_details, search_inventory
from zoi_agent.tools.origem import buscar_veiculo_interesse_origem
from zoi_agent.tools.photos import build_photo_payload, build_photo_payload_by_id
from zoi_agent.tools.terminal import TERMINAL_REASONS

log = get_logger(__name__)


# Match anos 4 dígitos isolados ou notação "2023/2024", "2023/24", "/2024"
_YEAR_TOKEN_PAT = re.compile(r"\b(?:19|20)\d{2}(?:\s*/\s*(?:19|20)?\d{2})?\b|/\s*(?:19|20)?\d{2}\b")


def _strip_year_tokens(text: str) -> str:
    """Remove tokens de ano (2024, 2023/2024, /24). Usado pra alargar âncora
    quando o lead pede 'outras opções' do mesmo modelo."""
    return re.sub(r"\s+", " ", _YEAR_TOKEN_PAT.sub("", text or "")).strip()


# --- Task table (preempção por contactId) ---------------------------------

_TASKS: dict[str, asyncio.Task] = {}


_PLURAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("algum desses veículos chamou a sua atenção", "esse veículo te chamou atenção"),
    ("algum desses veículos chamou sua atenção", "esse veículo te chamou atenção"),
    ("algum desses chamou a sua atenção", "esse te chamou atenção"),
    ("algum desses chamou sua atenção", "esse te chamou atenção"),
    ("algum desses chamou atenção", "esse te chamou atenção"),
    ("qual desses chamou mais sua atenção", "esse te chamou atenção"),
    ("qual desses chamou sua atenção", "esse te chamou atenção"),
    ("qual desses chamou atenção", "esse te chamou atenção"),
    ("qual desses", "esse"),
    ("algum desses", "esse"),
    ("quais desses", "esse"),
    ("alguns desses", "esse"),
    ("algum deles chamou", "esse te chamou"),
    ("algum deles", "esse"),
    ("desses veículos", "esse veículo"),
    ("desses carros", "esse carro"),
    ("desses modelos", "esse modelo"),
)


def _enforce_singular_question(bubble: str) -> str:
    """Reescreve frases plurais quando só 1 veículo foi apresentado."""
    if not bubble:
        return bubble
    out = bubble
    low = out.lower()
    for needle, repl in _PLURAL_PATTERNS:
        if needle in low:
            # case-insensitive replace preservando capitalização inicial
            idx = low.find(needle)
            replaced = out[:idx] + repl + out[idx + len(needle):]
            if out[idx].isupper():
                replaced = out[:idx] + repl[0].upper() + repl[1:] + out[idx + len(needle):]
            out = replaced
            low = out.lower()
    return out


def _set_task(contact_id: str, task: asyncio.Task) -> None:
    _TASKS[contact_id] = task

    def _cleanup(t: asyncio.Task) -> None:
        # remove só se ainda for o mesmo
        cur = _TASKS.get(contact_id)
        if cur is t:
            _TASKS.pop(contact_id, None)

    task.add_done_callback(_cleanup)


def cancel_existing(contact_id: str) -> None:
    prev = _TASKS.get(contact_id)
    if prev and not prev.done():
        log.info("turn_preempted", contact_id=contact_id)
        prev.cancel()


# --- Tool dispatch --------------------------------------------------------


async def _dispatch_tools(
    *,
    update_intent_sec: str | None,
    last_message: str,
    state,
    update_intent: str | None = None,
    update_topics: list[str] | None = None,
    update_preferencia_dia: str | None = None,
    update_preferencia_periodo: str | None = None,
    update_preferencia_hora: str | None = None,
    update_photo_target_external_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    # Union de tópicos: novo campo `topics` + legacy `intent_secundario` +
    # promotion de intent=agendamento. Garante multi-intenção: lead pode
    # tocar em vários assuntos no mesmo turno e o orchestrator dispara TODAS
    # as ferramentas necessárias (FAQ + slots, FAQ + search, foto + FAQ, etc.)
    topics: set[str] = set(update_topics or [])
    if update_intent_sec:
        topics.add(update_intent_sec)
    if update_intent == "agendamento":
        topics.add("agendamento")
    out["topics_dispatched"] = sorted(topics)

    # PRIORITÁRIO: ainda não apresentamos a origem do CRM
    # -> traz matches do estoque ANTES de qualificar (PLAN §5 + §16 C4).
    # Gate usa flag origem_apresentada (set abaixo independente de ter rendido).
    # Evita refazer a mesma busca toda rodada quando origem deu 0/0.
    #
    # EXCEÇÃO: se o lead nomeou modelo específico neste turno
    # (topic=ver_outros_carros), o desejo ATUAL tem precedência sobre o anúncio
    # de origem. Pular origem evita despejar "parecidos da Sentra" quando lead
    # acabou de pedir FOX.
    origem_empty = False
    lead_pediu_modelo = "ver_outros_carros" in topics
    if state.veiculo_origem and not state.origem_apresentada and not lead_pediu_modelo:
        try:
            origem = await buscar_veiculo_interesse_origem(state)
            # Mesmo com 0/0, marca apresentada -> não refaz a mesma busca nas
            # próximas rodadas (bug histórico: loop infinito de "Sentra=0").
            state.origem_apresentada = True
            if origem:
                out["origem_matches"] = origem
                m = (origem or {}).get("matches") or {}
                if not (m.get("exatos") or m.get("parecidos")):
                    origem_empty = True
            else:
                origem_empty = True
        except Exception as e:
            log.error("origem_dispatch_failed", err=str(e))

    if "duvida_operacional" in topics:
        try:
            out["faq_yaml"] = await get_faq_raw()
        except Exception as e:
            log.error("faq_fetch_failed", err=str(e))
            out["faq_yaml"] = ""

    # search_inventory dispara em 2 cenários:
    #   a) topic=ver_outros_carros (lead pediu alternativas)
    #   b) origem retornou 0/0 nesta rodada (fallback automático pra não
    #      deixar o lead sem opções; PLAN §5 promete VALOR na 1ª resposta)
    want_search = "ver_outros_carros" in topics or origem_empty
    if want_search:
        try:
            # Quando lead pediu modelo específico, NUNCA usar texto do anúncio
            # como anchor — evita contaminar query com modelo errado (ex:
            # "Nissan Sentra Tem algum FOX?" virava filtro Sentra ao invés de Fox).
            if lead_pediu_modelo:
                anchor = state.collected.veiculo_interesse or ""
            else:
                anchor = state.collected.veiculo_interesse or (
                    state.veiculo_origem.texto if state.veiculo_origem else ""
                )
            if origem_empty:
                anchor = ""
            if "ver_outros_carros" in topics:
                anchor = _strip_year_tokens(anchor)
            query = f"{anchor} {last_message}".strip() if anchor else last_message
            if len(query.strip()) < 3:
                query = "veículos disponíveis"
            res = await search_inventory(
                query,
                exclude_ids=list(state.vehicles_shown or []),
            )
            out["search_results"] = res.model_dump()
            # Sinaliza ao responder quando lead pediu modelo específico e NÃO
            # temos exatos no estoque. Responder vai reconhecer "não temos X"
            # antes de listar parecidos (sem fingir disponibilidade).
            modelo_pedido = (res.filters_used or {}).get("modelo")
            if (
                "ver_outros_carros" in topics
                and modelo_pedido
                and not res.exatos
            ):
                out["modelo_solicitado_indisponivel"] = {
                    "modelo": modelo_pedido,
                    "tem_alternativas": bool(res.parecidos),
                }
        except Exception as e:
            log.error("search_inventory_failed", err=str(e))
            out["search_results"] = {"error": str(e)}

    # Pre-render templates determinísticos: prepende ao envio antes das bolhas
    # do responder. Reduz token, mantém visual consistente.
    pre_bubbles: list[str] = []
    rendered_ids: list[str] = []
    # Origem só renderiza se DE FATO trouxe veículos. Se veio vazia, o bloco
    # de search_results (fallback acima) é quem assume o pre-render.
    om = (out.get("origem_matches") or {}).get("matches") or {}
    origem_has_content = bool(om.get("exatos") or om.get("parecidos"))
    sr = out.get("search_results") or {}
    search_has_content = (
        bool(sr) and not sr.get("error") and bool(sr.get("exatos") or sr.get("parecidos"))
    )
    # Precedência: desejo ATUAL (search_results disparado por ver_outros_carros)
    # vence o anúncio de origem. Sem isso, "lead pediu FOX mas origem=Sentra"
    # renderiza parecidos da Sentra e ignora o Fox encontrado.
    prefer_search = lead_pediu_modelo and search_has_content
    if prefer_search:
        exatos = sr.get("exatos") or []
        parecidos = [p.get("vehicle") for p in (sr.get("parecidos") or []) if p.get("vehicle")]
        bs, ids = build_vehicle_blocks_with_ids(exatos=exatos, parecidos=parecidos)
        pre_bubbles.extend(bs)
        rendered_ids.extend(ids)
    elif origem_has_content:
        exatos = om.get("exatos") or []
        parecidos = [p.get("vehicle") for p in (om.get("parecidos") or []) if p.get("vehicle")]
        bs, ids = build_vehicle_blocks_with_ids(exatos=exatos, parecidos=parecidos)
        pre_bubbles.extend(bs)
        rendered_ids.extend(ids)
    elif search_has_content:
        exatos = sr.get("exatos") or []
        parecidos = [p.get("vehicle") for p in (sr.get("parecidos") or []) if p.get("vehicle")]
        bs, ids = build_vehicle_blocks_with_ids(exatos=exatos, parecidos=parecidos)
        pre_bubbles.extend(bs)
        rendered_ids.extend(ids)
    if pre_bubbles:
        out["pre_bubbles"] = pre_bubbles
        out["rendered_vehicle_ids"] = rendered_ids
        out["vehicles_presented_count"] = len(rendered_ids)
    # Gate de agendamento (PLAN §11) — flexibilizado + multi-topic:
    # quer_agendar: collected ou intent=agendamento ou tópico=agendamento.
    # focus_ok: confirmado=true OU foco implícito (último card único OU
    #   exatamente 1 veículo na sessão).
    quer_agendar = (
        bool(state.collected.interesse_agendamento)
        or update_intent == "agendamento"
        or "agendamento" in topics
    )
    has_single_focus = (
        bool(state.last_card_external_id)
        or len(state.vehicles_shown or []) == 1
    )
    focus_ok = bool(state.collected.veiculo_interesse_confirmado) or has_single_focus
    if quer_agendar and focus_ok:
        # CAMINHO RÁPIDO: lead deu horário explícito (ex: "passo aí umas 10:00").
        # Antes de propor uma lista, verifica se o calendário tem slot real
        # naquele horário (±15min). Se sim, marca como slot escolhido pro
        # bloco de booking lá embaixo agendar direto — sem fazer o lead
        # escolher de uma lista. Isso atende leads que pedem horário
        # específico fora dos que o agente já propôs em texto.
        if update_preferencia_hora:
            try:
                exact = await find_exact_slot(
                    dia=update_preferencia_dia,
                    hora=update_preferencia_hora,
                )
                if exact:
                    out["auto_book_slot_iso"] = exact.iso
                    out["auto_book_requested_hora"] = update_preferencia_hora
            except Exception as e:
                log.error("find_exact_slot_failed", err=str(e))

        # Sempre propõe slots também (fallback caso a hora pedida não exista
        # OU caso o lead não tenha dado hora). Responder usa auto_book pra
        # confirmar; se não houve auto_book, usa slots pra negociar.
        try:
            slots, fallback = await propose_slots(
                dia=update_preferencia_dia,
                periodo=update_preferencia_periodo,
                hora=update_preferencia_hora,
                limit=3,
            )
            out["slots"] = [{"iso": s.iso, "label": s.label_pt()} for s in slots]
            if fallback:
                out["slots_fallback"] = {
                    "pref_dia": update_preferencia_dia,
                    "pref_periodo": update_preferencia_periodo,
                    "motivo": "sem disponibilidade na preferência do lead",
                }
        except Exception as e:
            log.error("propose_slots_failed", err=str(e))
            out["slots"] = []
    elif quer_agendar and not focus_ok:
        # C19: lead quer agendar mas sem foco -> agente puxa foco antes
        out["agendamento_gate"] = {"motivo": "veiculo_interesse_confirmado=false"}

    if "pedido_foto" in topics:
        try:
            # Updater LLM já escolheu o alvo a partir de candidates_for_photo
            # validados contra inventário. Se veio ID → usa direto, sem
            # heurística textual (evita substring tipo "fitos"→Fit).
            # Se updater devolveu null/invalid → cai pro picker determinístico
            # (com fuzzy match anti-typo + fallbacks).
            if update_photo_target_external_id:
                out["photos"] = await build_photo_payload_by_id(
                    external_id=update_photo_target_external_id, state=state
                )
            else:
                out["photos"] = await build_photo_payload(
                    last_message=last_message, state=state
                )
        except Exception as e:
            log.error("photos_payload_failed", err=str(e))
            out["photos"] = {
                "available": False, "vehicle": None, "images": [],
                "single_image_only": False, "will_send_count": 0,
            }

    # Ficha completa do veículo em foco — evita alucinação em perguntas
    # "esse tem X?". Prioridade pra determinar o foco:
    #   1. Veículo da foto recém-enviada (lead engajou nele AGORA).
    #   2. last_card_external_id (card único renderizado).
    #   3. Último de vehicles_shown (último que o lead viu).
    # NUNCA cai pro veiculo_origem.texto — pode ser modelo fora do estoque.
    focus_eid: str | None = None
    photos_vehicle = (out.get("photos") or {}).get("vehicle") or {}
    if photos_vehicle.get("external_id"):
        focus_eid = str(photos_vehicle["external_id"])
    elif state.last_card_external_id:
        focus_eid = state.last_card_external_id
    elif state.vehicles_shown:
        focus_eid = state.vehicles_shown[-1]

    if focus_eid:
        try:
            details = await get_vehicle_details(focus_eid)
            if details:
                out["vehicle_in_focus"] = details
        except Exception as e:
            log.error("vehicle_details_failed", err=str(e))

    return out


# --- Sender ---------------------------------------------------------------


async def _send_bubble(
    *, contact_id: str, conversation_id: str | None, text: str
) -> None:
    """Envia 1 bolha. Falhas tratadas em ordem superior."""
    await ghl_conv.send_message(
        contact_id=contact_id,
        conversation_id=conversation_id,
        message=text,
    )


async def _send_photo(
    *, contact_id: str, conversation_id: str | None, url: str
) -> None:
    """Envia 1 foto (sem texto). PLAN §5: type SMS, attachments=[url]."""
    await ghl_conv.send_message(
        contact_id=contact_id,
        conversation_id=conversation_id,
        attachments=[url],
    )


async def _send_photos_parallel(
    *, contact_id: str, conversation_id: str | None, urls: list[str]
) -> int:
    """Envia N fotos via asyncio.gather (paralelo). PLAN: return_exceptions=True.
    Retorna quantas foram enviadas com sucesso."""
    if not urls:
        return 0
    results = await asyncio.gather(
        *(
            _send_photo(contact_id=contact_id, conversation_id=conversation_id, url=u)
            for u in urls
        ),
        return_exceptions=True,
    )
    ok = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log.error("photo_send_failed", idx=i, url=urls[i], err=str(r))
        else:
            ok += 1
    log.info("photos_sent", total=len(urls), ok=ok)
    return ok


async def _send_bubbles(
    *,
    contact_id: str,
    conversation_id: str | None,
    bubbles: list[str],
    photos: list[str] | None = None,
) -> None:
    """1) Fotos paralelo (se houver). 2) Wait 1s. 3) Bolhas sequencial com sleeps 0.6-1.2s.
    Pula bolhas que falham persistentemente (tenacity do client já tenta 3x)."""
    if photos:
        await _send_photos_parallel(
            contact_id=contact_id, conversation_id=conversation_id, urls=photos
        )
        await asyncio.sleep(1.0)
    for i, b in enumerate(bubbles):
        try:
            await _send_bubble(contact_id=contact_id, conversation_id=conversation_id, text=b)
        except Exception as e:
            log.error("bubble_send_failed", idx=i, err=str(e))
            continue
        if i < len(bubbles) - 1:
            sleep_s = random.uniform(settings.responder_sleep_min, settings.responder_sleep_max)
            await asyncio.sleep(sleep_s)


# --- Pipeline -------------------------------------------------------------


async def _fetch_history(contact_id: str) -> tuple[list[dict], str | None]:
    """Busca histórico via GHL. Retorna (messages, conversation_id)."""
    try:
        search = await ghl_conv.search_conversations(contact_id)
        convs = search.get("conversations") or []
        if not convs:
            return [], None
        conv_id = convs[0].get("id")
        if not conv_id:
            return [], None
        msgs_resp = await ghl_conv.get_messages(conv_id)
        msgs_block = msgs_resp.get("messages") or {}
        # GHL costuma retornar {"messages": [...]} dentro do bloco
        msgs = msgs_block.get("messages") if isinstance(msgs_block, dict) else msgs_block
        return (msgs or []), conv_id
    except Exception as e:
        log.warning("history_fetch_failed", err=str(e))
        return [], None


async def _run_turn(contact_id: str, last_message: str) -> None:
    state = await session_repo.load_or_new(contact_id)
    if state.terminal_reason:
        log.info("turn_skipped_terminal", contact_id=contact_id, reason=state.terminal_reason)
        return

    history, conversation_id = await _fetch_history(contact_id)

    try:
        update = await run_updater(history=history, state=state, last_message=last_message)
    except Exception as e:
        # C23/PLAN §13: 3-retry da tenacity já se exauriu -> handoff_erro
        log.error("updater_failed_terminal", contact_id=contact_id, err=str(e))
        state.stage = "fechado"
        state.terminal_reason = "handoff_erro"
        try:
            await encaminhar_para_vendedor(
                contact_id=contact_id,
                state=state,
                terminal_reason="handoff_erro",
                handoff_reason=f"updater LLM falhou: {type(e).__name__}: {e}",
            )
        except Exception as e2:
            log.error("terminal_dispatch_failed", err=str(e2))
        try:
            await session_repo.save(contact_id, state)
        except Exception as e3:
            log.error("state_save_failed", err=str(e3))
        return

    new_state = merge_into_state(state, update)

    # Planner determinístico: decide próxima pergunta APÓS o merge. Substitui
    # a decisão dispersa entre update.missing / update.next_action / responder.
    next_q = plan_next_question(state=new_state, update=update, history=history)
    log.info(
        "next_question_planned",
        field=next_q.field,
        intent=next_q.intent,
        skip=next_q.skip_funnel_reason,
    )

    tools = await _dispatch_tools(
        update_intent=update.intent,
        update_intent_sec=update.intent_secundario,
        update_topics=list(update.topics or []),
        update_preferencia_dia=(update.preferencia_horario.dia if update.preferencia_horario else None),
        update_preferencia_periodo=(update.preferencia_horario.periodo if update.preferencia_horario else None),
        update_preferencia_hora=(update.preferencia_horario.hora if update.preferencia_horario else None),
        update_photo_target_external_id=update.photo_target_external_id,
        last_message=last_message,
        state=new_state,
    )
    tools["next_question"] = {
        "field": next_q.field,
        "intent": next_q.intent,
        "canonical_text": next_q.canonical_text,
        "skip_funnel_reason": next_q.skip_funnel_reason,
    }

    # GUARD RAIL: terminal=qualificado_agendado SÓ pode existir se houve
    # booking real. Updater não pode setar essa terminal sem o orchestrator
    # ter rodado book_appointment com sucesso. Bug visto em prod: updater
    # setou terminal por conta própria quando lead disse "passo aí umas 10:00",
    # sem chosen_slot_iso válido → CRM marcou qualificado_agendado mas
    # calendário ficou vazio. Aqui esmagamos terminal prematuro.
    if (
        update.terminal_reason == "qualificado_agendado"
        and not update.chosen_slot_iso
        and not new_state.appointment
    ):
        log.warning(
            "stripped_premature_terminal",
            contact_id=contact_id,
            reason="qualificado_agendado sem booking real",
        )
        update.terminal_reason = None

    # Booking: 2 caminhos de slot escolhido
    #   1) `update.chosen_slot_iso` — lead aceitou slot que JÁ tinha sido proposto
    #      pela Patricia em texto (caminho clássico).
    #   2) `tools.auto_book_slot_iso` — lead deu horário explícito (ex: "10:00")
    #      e o orquestrador encontrou slot real no calendário sem precisar
    #      propor lista. Auto-agendamento direto do desejo do lead.
    slot_to_book = update.chosen_slot_iso or tools.get("auto_book_slot_iso")
    booking_source = "lead_pick" if update.chosen_slot_iso else "auto_match"
    if slot_to_book:
        try:
            modelo = new_state.collected.veiculo_interesse
            booked = await book_appointment(
                contact_id=contact_id,
                slot_iso=slot_to_book,
                lead_name=new_state.collected.nome,
                modelo=modelo,
            )
            new_state.appointment = {
                "slot_iso": slot_to_book,
                "id": (booked.get("appointment") or {}).get("id") or booked.get("id"),
                "modelo": modelo,
            }
            tools["booking"] = {
                "ok": True,
                "slot": slot_to_book,
                "source": booking_source,
            }
            log.info(
                "auto_book_success" if booking_source == "auto_match" else "lead_pick_book_success",
                contact_id=contact_id, slot=slot_to_book,
            )
            # Promove terminal_reason se updater não tiver setado
            if not update.terminal_reason:
                update.terminal_reason = "qualificado_agendado"
        except Exception as e:
            log.error("book_appointment_failed", err=str(e), source=booking_source)
            tools["booking"] = {"ok": False, "error": str(e), "source": booking_source}
            # PLAN §11 conflito de slot -> handoff_erro
            if not update.terminal_reason:
                update.terminal_reason = "handoff_erro"
                update.handoff_reason = f"conflito ao bookar slot: {e}"

    try:
        bubbles = await run_responder(
            state=new_state,
            update=update,
            history=history,
            last_message=last_message,
            tool_outputs=tools,
        )
    except Exception as e:
        # PLAN §13: responder esgotou retries -> handoff_erro
        log.error("responder_failed_terminal", contact_id=contact_id, err=str(e))
        new_state.stage = "fechado"
        new_state.terminal_reason = "handoff_erro"
        try:
            await encaminhar_para_vendedor(
                contact_id=contact_id,
                state=new_state,
                terminal_reason="handoff_erro",
                handoff_reason=f"responder LLM falhou: {type(e).__name__}: {e}",
            )
        except Exception as e2:
            log.error("terminal_dispatch_failed", err=str(e2))
        try:
            await session_repo.save(contact_id, new_state)
        except Exception as e3:
            log.error("state_save_failed", err=str(e3))
        return

    # Prepende bolhas pré-renderizadas (templates Python) antes das do responder.
    # Mantém ordem: [pre_bubbles..., bolha de pergunta do responder].
    pre_bubbles = tools.get("pre_bubbles") or []
    if pre_bubbles:
        bubbles = list(pre_bubbles) + list(bubbles)
        bubbles = bubbles[: settings.responder_max_bubbles + len(pre_bubbles)]

    # Guard determinístico: quando vehicles_presented_count == 1, reescreve a
    # última bolha se o LLM teimou em usar pergunta no plural ("algum desses",
    # "qual desses", etc). Garante contrato independentemente do LLM.
    presented = int(tools.get("vehicles_presented_count") or 0)
    if presented == 1 and bubbles:
        bubbles[-1] = _enforce_singular_question(bubbles[-1])

    # Fotos a enviar (paralelo) + bolhas
    photo_urls: list[str] = []
    photos_payload = tools.get("photos") or {}
    if photos_payload.get("images"):
        photo_urls = list(photos_payload["images"])
        vid = (photos_payload.get("vehicle") or {}).get("external_id")
        if vid:
            if vid not in new_state.vehicles_shown:
                new_state.vehicles_shown.append(vid)
            # Foco corrente: lead engajou nesse veículo pedindo foto.
            # Próximas perguntas "esse tem X?" referem-se a ele.
            new_state.last_card_external_id = str(vid)

    # vehicles_shown só recebe IDs efetivamente RENDERIZADOS em bolhas (não
    # candidatos da busca). last_card_external_id setado só quando 1 card único.
    rendered_ids = tools.get("rendered_vehicle_ids") or []
    if rendered_ids:
        for eid in rendered_ids:
            if eid not in new_state.vehicles_shown:
                new_state.vehicles_shown.append(eid)
        # 1 card único -> foco; lista -> mantém foco anterior (não há
        # convergência ainda; o lead vai escolher um).
        if len(rendered_ids) == 1:
            new_state.last_card_external_id = rendered_ids[0]
    # NÃO limpa last_card_external_id quando turno é vazio de cards: foco
    # implícito de pedido_foto / engajamento anterior é preservado.

    # NOTA: removido flag origem_apresentada. Semântica derivada de
    # state.vehicles_shown não-vazio (a inserção acima já cuida disso).

    # Send phase sob shield: não pode ser cancelado por nova preempção no meio.
    await asyncio.shield(
        _send_bubbles(
            contact_id=contact_id,
            conversation_id=conversation_id,
            bubbles=bubbles,
            photos=photo_urls,
        )
    )

    if update.terminal_reason:
        new_state.stage = "fechado"
        new_state.terminal_reason = update.terminal_reason
        log.info("turn_terminal", contact_id=contact_id, reason=update.terminal_reason)
        if update.terminal_reason in TERMINAL_REASONS:
            try:
                await encaminhar_para_vendedor(
                    contact_id=contact_id,
                    state=new_state,
                    terminal_reason=update.terminal_reason,
                    handoff_reason=update.handoff_reason,
                )
            except Exception as e:
                log.error("terminal_dispatch_failed", err=str(e))

    # Registra a pergunta enviada (rolling window pra anti-repetição).
    if next_q.field and next_q.intent == "funil":
        push_asked_field(new_state, next_q.field)

    try:
        await session_repo.save(contact_id, new_state)
    except Exception as e:
        log.error("state_save_failed", err=str(e))

    TURNS_TOTAL.labels(stage=update.stage, intent=update.intent).inc()


async def process_turn(contact_id: str, last_message: str) -> asyncio.Task:
    """Entrypoint: cancela turno anterior do mesmo contato e dispara um novo.
    Retorna a Task (útil em testes; em prod o webhook não precisa esperar)."""
    cancel_existing(contact_id)
    task = asyncio.create_task(_run_turn(contact_id, last_message), name=f"turn:{contact_id}")
    _set_task(contact_id, task)
    return task


def get_active_task(contact_id: str) -> asyncio.Task | None:
    return _TASKS.get(contact_id)
