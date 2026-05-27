from __future__ import annotations

import httpx
import pytest

from zoi_agent.ghl.client import GHLClient, GHLError


@pytest.mark.asyncio
async def test_client_sends_auth_headers() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["version"] = request.headers.get("version")
        captured["path"] = request.url.path
        return httpx.Response(200, json={"contact": {"id": "abc"}})

    transport = httpx.MockTransport(handler)
    client = GHLClient(token="pit-fake", location_id="loc1")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=transport,
        headers={
            "Authorization": f"Bearer {client.token}",
            "Version": client.api_version,
            "Accept": "application/json",
        },
    )

    body = await client.get("/contacts/abc", operation="contacts.get")
    assert body == {"contact": {"id": "abc"}}
    assert captured["auth"] == "Bearer pit-fake"
    assert captured["version"]
    assert captured["path"] == "/contacts/abc"
    await client.aclose()


@pytest.mark.asyncio
async def test_client_retries_on_500_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(500, json={"err": "boom"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = GHLClient(token="pit-fake")
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=transport)

    body = await client.get("/ping", operation="test")
    assert body == {"ok": True}
    assert calls["n"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_client_raises_on_4xx_no_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"err": "not found"})

    transport = httpx.MockTransport(handler)
    client = GHLClient(token="pit-fake")
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=transport)

    with pytest.raises(GHLError) as exc:
        await client.get("/contacts/missing", operation="test")
    assert exc.value.status_code == 404
    assert calls["n"] == 1
    await client.aclose()
