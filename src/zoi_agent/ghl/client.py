from __future__ import annotations

import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from zoi_agent.config import settings
from zoi_agent.logging import get_logger
from zoi_agent.metrics import GHL_REQUEST_LATENCY

log = get_logger(__name__)


class GHLError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, GHLError):
        if exc.status_code in _RETRYABLE_STATUS:
            return True
        # GHL às vezes devolve 401 com body {"message":"Command timed out"} —
        # é timeout interno deles disfarçado de auth error. Token segue válido.
        # Retentar nesse caso específico evita silêncio pro lead.
        if exc.status_code == 401 and isinstance(exc.body, dict):
            msg = str(exc.body.get("message", "")).lower()
            if "timed out" in msg or "timeout" in msg:
                return True
    return False


class GHLClient:
    def __init__(
        self,
        token: str | None = None,
        location_id: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.token = token or settings.ghl_pit_token
        self.location_id = location_id or settings.ghl_location_id
        self.base_url = (base_url or settings.ghl_base_url).rstrip("/")
        self.api_version = api_version or settings.ghl_api_version
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Version": self.api_version,
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GHLClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        operation: str = "unknown",
    ) -> dict:
        start = time.perf_counter()
        try:
            resp = await self._client.request(method, path, params=params, json=json)
        except httpx.TransportError as e:
            log.warning("ghl_transport_error", op=operation, path=path, err=str(e))
            raise
        finally:
            GHL_REQUEST_LATENCY.labels(operation=operation).observe(time.perf_counter() - start)

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            err = GHLError(
                f"GHL {method} {path} failed: {resp.status_code}",
                status_code=resp.status_code,
                body=body,
            )
            if resp.status_code in _RETRYABLE_STATUS:
                log.warning(
                    "ghl_retryable_error",
                    op=operation,
                    status=resp.status_code,
                    body=body,
                )
                raise err
            log.error("ghl_error", op=operation, status=resp.status_code, body=body)
            raise err

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def get(self, path: str, *, params: dict | None = None, operation: str = "get") -> dict:
        return await self._request("GET", path, params=params, operation=operation)

    async def post(
        self, path: str, *, json: Any = None, params: dict | None = None, operation: str = "post"
    ) -> dict:
        return await self._request("POST", path, params=params, json=json, operation=operation)

    async def put(self, path: str, *, json: Any = None, operation: str = "put") -> dict:
        return await self._request("PUT", path, json=json, operation=operation)

    async def delete(self, path: str, *, operation: str = "delete") -> dict:
        return await self._request("DELETE", path, operation=operation)


_client_singleton: GHLClient | None = None


def get_client() -> GHLClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = GHLClient()
    return _client_singleton


async def close_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None
