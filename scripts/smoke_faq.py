"""Smoke C9: carrega FAQ ao vivo + faz parse."""
from __future__ import annotations

import asyncio
import sys

from zoi_agent.ghl.client import close_client
from zoi_agent.tools.faq import get_faq_parsed, get_faq_raw


async def main() -> int:
    raw = await get_faq_raw()
    print(f"=== bytes ===\n{len(raw)}\n")
    print(f"=== primeiros 800 chars ===\n{raw[:800]}\n")

    parsed = await get_faq_parsed()
    print(f"=== parsed type ===\n{type(parsed).__name__}\n")
    if isinstance(parsed, dict):
        print(f"=== top-level keys ===\n{list(parsed.keys())}")
    elif isinstance(parsed, list):
        print(f"=== entries: {len(parsed)} ===")
        if parsed:
            print(f"first: {parsed[0]}")

    await close_client()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
