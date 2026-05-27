from __future__ import annotations

from zoi_agent.ghl.client import GHLClient, get_client


async def add_to_workflow(
    contact_id: str, workflow_id: str, *, client: GHLClient | None = None
) -> dict:
    c = client or get_client()
    return await c.post(
        f"/contacts/{contact_id}/workflow/{workflow_id}",
        operation="workflows.add",
    )
