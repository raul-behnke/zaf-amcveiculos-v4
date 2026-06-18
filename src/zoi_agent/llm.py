from __future__ import annotations

import time
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from zoi_agent import usage as usage_sink
from zoi_agent.config import settings
from zoi_agent.metrics import LLM_LATENCY

T = TypeVar("T", bound=BaseModel)


def _record_usage(resp, *, component: str, model: str, latency_ms: int | None = None) -> None:
    """Captura response.usage da OpenAI no sink do turno. Tolerante a ausência."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    details = getattr(u, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details else None
    usage_sink.record(
        component=component,
        model=model,
        tokens_input=getattr(u, "prompt_tokens", 0) or 0,
        tokens_output=getattr(u, "completion_tokens", 0) or 0,
        tokens_total=getattr(u, "total_tokens", 0) or 0,
        reasoning_tokens=reasoning,
        latency_ms=latency_ms,
    )

_client: AsyncOpenAI | None = None


def get_openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def parse_structured(
    *,
    model: str,
    schema: type[T],
    system: str,
    user: str,
    component: str = "llm",
    temperature: float = 0.0,
) -> T:
    client = get_openai()
    start = time.perf_counter()
    try:
        resp = await client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=schema,
            temperature=temperature,
        )
    finally:
        elapsed = time.perf_counter() - start
        LLM_LATENCY.labels(component=component).observe(elapsed)
    _record_usage(resp, component=component, model=model, latency_ms=int(elapsed * 1000))
    parsed = resp.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError(f"LLM {model} retornou parsed=None (refusal? {resp.choices[0].message.refusal!r})")
    return parsed


async def chat_text(
    *,
    model: str,
    system: str,
    user: str,
    component: str = "llm",
    temperature: float = 0.4,
) -> str:
    client = get_openai()
    start = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
    finally:
        elapsed = time.perf_counter() - start
        LLM_LATENCY.labels(component=component).observe(elapsed)
    _record_usage(resp, component=component, model=model, latency_ms=int(elapsed * 1000))
    return resp.choices[0].message.content or ""
