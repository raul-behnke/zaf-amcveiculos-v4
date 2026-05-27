"""Smoke test ao vivo do cliente GHL.

Uso:
    .venv/bin/python scripts/smoke_ghl.py [CONTACT_ID]

Default contact_id é o do PLAN.md (d9ILOnEyNkYhkIALa3wq).
"""

from __future__ import annotations

import asyncio
import json
import sys

from zoi_agent.config import settings
from zoi_agent.ghl import GHLError, get_client
from zoi_agent.ghl.client import close_client
from zoi_agent.ghl.contacts import get_contact, has_tag, read_custom_field_value
from zoi_agent.ghl.custom_values import extract_value, get_custom_value
from zoi_agent.logging import configure_logging, get_logger

DEFAULT_CONTACT_ID = "d9ILOnEyNkYhkIALa3wq"


async def main(contact_id: str) -> int:
    configure_logging()
    log = get_logger("smoke_ghl")
    failures = 0

    log.info("smoke_start", contact_id=contact_id, location=settings.ghl_location_id)

    try:
        contact = await get_contact(contact_id)
        body = contact.get("contact", contact)
        log.info(
            "contact_ok",
            id=body.get("id"),
            name=body.get("contactName") or body.get("firstName"),
            tags=body.get("tags"),
        )
        print("\n--- contact (resumo) ---")
        print(json.dumps({k: body.get(k) for k in ("id", "firstName", "lastName", "phone", "email", "tags")}, indent=2, ensure_ascii=False))

        gate = settings.ghl_tag_agent_gate
        print(f"\ntag '{gate}' presente? {has_tag(contact, gate)}")

        veic = read_custom_field_value(contact, settings.ghl_field_veiculo_interesse)
        print(f"Veículo de Interesse: {veic!r}")
    except GHLError as e:
        log.error("contact_fail", status=e.status_code, body=e.body)
        failures += 1

    try:
        cv = await get_custom_value(settings.ghl_stock_custom_value_id)
        val = extract_value(cv)
        size = len(val) if val else 0
        log.info("stock_cv_ok", size=size)
        print(f"\nstock custom value bytes: {size}")
    except GHLError as e:
        log.error("stock_cv_fail", status=e.status_code, body=e.body)
        failures += 1

    try:
        cv = await get_custom_value(settings.ghl_faq_custom_value_id)
        val = extract_value(cv)
        size = len(val) if val else 0
        log.info("faq_cv_ok", size=size)
        print(f"FAQ custom value bytes: {size}")
    except GHLError as e:
        log.error("faq_cv_fail", status=e.status_code, body=e.body)
        failures += 1

    await close_client()
    print(f"\nfailures: {failures}")
    return failures


if __name__ == "__main__":
    cid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONTACT_ID
    sys.exit(asyncio.run(main(cid)))
