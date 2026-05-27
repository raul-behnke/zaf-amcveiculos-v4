from __future__ import annotations

from zoi_agent.config import settings
from zoi_agent.ghl.client import GHLClient, get_client


async def get_custom_value(
    custom_value_id: str, *, client: GHLClient | None = None
) -> dict:
    c = client or get_client()
    return await c.get(
        f"/locations/{settings.ghl_location_id}/customValues/{custom_value_id}",
        operation="custom_values.get",
    )


def extract_value(payload: dict) -> str | None:
    cv = payload.get("customValue", payload)
    return cv.get("value")
