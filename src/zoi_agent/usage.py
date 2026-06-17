"""Coletor de uso de LLM por turno (tokens).

Pipeline multi-LLM: Updater + EstoqueExpert + Patricia (+ Whisper). Cada chamada
registra seu `usage` aqui; o orchestrator drena no fim do turno e emite eventos
LLM_CALL / WHISPER_TRANSCRIPTION na tabela `agent_events`.

ContextVar é por-task asyncio → seguro sob preempção e turnos concorrentes
(cada `_run_turn` roda em sua própria Task com seu próprio sink).
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from zoi_agent.logging import get_logger

log = get_logger(__name__)


@dataclass
class UsageRecord:
    component: str  # updater | estoque_expert | patricia | whisper
    model: str
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int = 0
    reasoning_tokens: int | None = None  # só modelos reasoning (gpt-5-mini etc.)
    audio_seconds: float | None = None  # só Whisper
    latency_ms: int | None = None


@dataclass
class UsageSink:
    records: list[UsageRecord] = field(default_factory=list)


_sink: ContextVar[UsageSink | None] = ContextVar("usage_sink", default=None)


def start_turn() -> UsageSink:
    """Inicia (ou reinicia) o sink do turno atual. Chamar no topo de _run_turn."""
    sink = UsageSink()
    _sink.set(sink)
    return sink


def record(
    *,
    component: str,
    model: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_total: int = 0,
    reasoning_tokens: int | None = None,
    audio_seconds: float | None = None,
    latency_ms: int | None = None,
) -> None:
    """Registra uso de uma chamada LLM. No-op se não houver sink (ex.: scripts)."""
    sink = _sink.get()
    if sink is None:
        return
    if not tokens_total:
        tokens_total = tokens_input + tokens_output
    sink.records.append(
        UsageRecord(
            component=component,
            model=model,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total=tokens_total,
            reasoning_tokens=reasoning_tokens,
            audio_seconds=audio_seconds,
            latency_ms=latency_ms,
        )
    )


def drain() -> list[UsageRecord]:
    """Retorna e limpa os registros do turno."""
    sink = _sink.get()
    if sink is None:
        return []
    recs = sink.records
    _sink.set(None)
    return recs
