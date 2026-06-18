"""Whisper transcription: multi-audio concat with tenacity retry."""
from __future__ import annotations

import asyncio
import io
import time
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from zoi_agent import usage as usage_sink
from zoi_agent.config import settings
from zoi_agent.llm import get_openai
from zoi_agent.logging import get_logger
from zoi_agent.metrics import LLM_LATENCY

log = get_logger(__name__)


class TranscriptionError(Exception):
    pass


def _filename_from_url(url: str, default: str = "audio.ogg") -> str:
    try:
        name = PurePosixPath(urlparse(url).path).name
        return name or default
    except Exception:
        return default


async def _download(url: str, *, timeout: float = 30.0) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content, _filename_from_url(url)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4), reraise=True)
async def _transcribe_bytes(audio: bytes, filename: str, *, language: str = "pt") -> str:
    client = get_openai()
    buf = io.BytesIO(audio)
    buf.name = filename  # API usa o name pra inferir formato
    start = time.perf_counter()
    try:
        # verbose_json expõe `duration` (segundos) → custo Whisper por minuto.
        resp = await client.audio.transcriptions.create(
            model=settings.openai_model_whisper,
            file=buf,
            language=language,
            response_format="verbose_json",
        )
    finally:
        elapsed = time.perf_counter() - start
        LLM_LATENCY.labels(component="whisper").observe(elapsed)

    if isinstance(resp, str):
        return resp.strip()
    text = (getattr(resp, "text", "") or "").strip()
    duration_s = getattr(resp, "duration", None)
    if duration_s is not None:
        try:
            usage_sink.record(
                component="whisper",
                model=settings.openai_model_whisper,
                audio_seconds=float(duration_s),
                latency_ms=int(elapsed * 1000),
            )
        except (TypeError, ValueError):
            pass
    return text


async def transcribe_url(url: str, *, language: str = "pt") -> str:
    audio, filename = await _download(url)
    log.info("audio_downloaded", url=url, bytes=len(audio), filename=filename)
    text = await _transcribe_bytes(audio, filename, language=language)
    log.info("audio_transcribed", url=url, chars=len(text))
    return text


async def transcribe_bytes(audio: bytes, filename: str = "audio.ogg", *, language: str = "pt") -> str:
    return await _transcribe_bytes(audio, filename, language=language)


async def transcribe_many(urls: list[str], *, language: str = "pt") -> str:
    """Transcreve N áudios em paralelo e concatena com newline. Falha total se algum falhar
    após 3 retries — handoff_erro fica a cargo do orquestrador."""
    if not urls:
        return ""
    if len(urls) == 1:
        return await transcribe_url(urls[0], language=language)
    results = await asyncio.gather(
        *(transcribe_url(u, language=language) for u in urls), return_exceptions=True
    )
    texts: list[str] = []
    for u, r in zip(urls, results):
        if isinstance(r, Exception):
            log.error("audio_transcribe_failed", url=u, err=str(r))
            raise TranscriptionError(f"falha em {u}: {r}") from r
        if r:
            texts.append(r)
    return "\n".join(texts)
