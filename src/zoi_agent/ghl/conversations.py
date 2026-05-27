from __future__ import annotations

from zoi_agent.config import settings
from zoi_agent.ghl.client import GHLClient, get_client


async def search_conversations(
    contact_id: str, *, client: GHLClient | None = None
) -> dict:
    c = client or get_client()
    return await c.get(
        "/conversations/search",
        params={"locationId": settings.ghl_location_id, "contactId": contact_id},
        operation="conversations.search",
    )


async def get_messages(
    conversation_id: str,
    *,
    limit: int | None = None,
    client: GHLClient | None = None,
) -> dict:
    c = client or get_client()
    params: dict = {"limit": limit or settings.conversation_history_limit}
    return await c.get(
        f"/conversations/{conversation_id}/messages",
        params=params,
        operation="conversations.get_messages",
    )


async def send_message(
    *,
    contact_id: str,
    message: str | None = None,
    conversation_id: str | None = None,
    attachments: list[str] | None = None,
    message_type: str = "SMS",
    client: GHLClient | None = None,
) -> dict:
    c = client or get_client()
    payload: dict = {
        "type": message_type,
        "contactId": contact_id,
    }
    if message is not None:
        payload["message"] = message
    if conversation_id:
        payload["conversationId"] = conversation_id
    if attachments:
        payload["attachments"] = attachments
    return await c.post(
        "/conversations/messages",
        json=payload,
        operation="conversations.send_message",
    )
