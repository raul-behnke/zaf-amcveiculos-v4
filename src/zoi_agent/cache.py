from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int, loader: Callable[[], Awaitable[T]]) -> None:
        self.ttl = ttl_seconds
        self._loader = loader
        self._value: T | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> T:
        now = time.monotonic()
        if self._value is not None and (now - self._fetched_at) < self.ttl:
            return self._value
        async with self._lock:
            now = time.monotonic()
            if self._value is not None and (now - self._fetched_at) < self.ttl:
                return self._value
            self._value = await self._loader()
            self._fetched_at = time.monotonic()
            return self._value

    def invalidate(self) -> None:
        self._value = None
        self._fetched_at = 0.0
