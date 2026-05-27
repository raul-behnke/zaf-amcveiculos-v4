from __future__ import annotations

from typing import Any

from zoi_agent.ghl.client import GHLClient, get_client


async def get_contact(contact_id: str, *, client: GHLClient | None = None) -> dict:
    c = client or get_client()
    return await c.get(f"/contacts/{contact_id}", operation="contacts.get")


async def update_contact(
    contact_id: str, payload: dict, *, client: GHLClient | None = None
) -> dict:
    c = client or get_client()
    return await c.put(f"/contacts/{contact_id}", json=payload, operation="contacts.update")


async def update_custom_field(
    contact_id: str,
    field_id: str,
    value: Any,
    *,
    client: GHLClient | None = None,
) -> dict:
    payload = {"customFields": [{"id": field_id, "value": value}]}
    return await update_contact(contact_id, payload, client=client)


async def add_note(contact_id: str, body: str, *, client: GHLClient | None = None) -> dict:
    c = client or get_client()
    return await c.post(
        f"/contacts/{contact_id}/notes",
        json={"body": body},
        operation="contacts.add_note",
    )


async def add_tag(
    contact_id: str, tags: list[str], *, client: GHLClient | None = None
) -> dict:
    c = client or get_client()
    return await c.post(
        f"/contacts/{contact_id}/tags",
        json={"tags": tags},
        operation="contacts.add_tag",
    )


async def remove_tag(
    contact_id: str, tags: list[str], *, client: GHLClient | None = None
) -> dict:
    c = client or get_client()
    return await c._request(
        "DELETE",
        f"/contacts/{contact_id}/tags",
        json={"tags": tags},
        operation="contacts.remove_tag",
    )


def read_custom_field_value(contact: dict, field_id: str) -> Any:
    cf = contact.get("contact", contact).get("customFields", [])
    for entry in cf:
        if entry.get("id") == field_id:
            return entry.get("value")
    return None


def has_tag(contact: dict, tag: str) -> bool:
    tags = contact.get("contact", contact).get("tags", [])
    return tag in tags
