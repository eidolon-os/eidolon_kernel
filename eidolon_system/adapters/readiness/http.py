"""Narrow HTTP readiness probe; it does not own service health facts."""

from __future__ import annotations

import httpx
from eidolon_sdk.core.http import create_async_client


class HttpReadinessProbe:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self._client = create_async_client(timeout=timeout_seconds, trust_env=False)

    async def check(self, url: str) -> bool:
        try:
            response = await self._client.get(url)
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 400

    async def close(self) -> None:
        await self._client.aclose()
