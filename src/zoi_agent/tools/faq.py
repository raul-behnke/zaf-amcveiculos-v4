"""FAQ tool: YAML do Custom Value GHL, cache 5min."""
from __future__ import annotations

from typing import Any

import yaml

from zoi_agent.cache import TTLCache
from zoi_agent.config import settings
from zoi_agent.ghl.custom_values import extract_value, get_custom_value
from zoi_agent.logging import get_logger

log = get_logger(__name__)


async def _fetch_faq_raw() -> str:
    cv = await get_custom_value(settings.ghl_faq_custom_value_id)
    raw = extract_value(cv) or ""
    log.info("faq_loaded", bytes=len(raw))
    return raw


_faq_cache: TTLCache[str] = TTLCache(
    ttl_seconds=settings.faq_cache_ttl_seconds, loader=_fetch_faq_raw
)


async def get_faq_raw() -> str:
    """YAML cru, pra injetar diretamente no prompt do responder."""
    return await _faq_cache.get()


async def get_faq_parsed() -> Any:
    """Parse YAML. Útil pra introspecção/teste; responder usa get_faq_raw."""
    raw = await get_faq_raw()
    if not raw:
        return None
    return yaml.safe_load(raw)


def invalidate_faq_cache() -> None:
    _faq_cache.invalidate()
