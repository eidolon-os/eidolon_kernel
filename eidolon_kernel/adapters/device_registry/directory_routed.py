"""Hub DeviceAuthority routed through the local System Service Directory."""

from __future__ import annotations

import httpx
from eidolon_sdk.device_foundation.v1 import ClaimEventCursor, ClaimEventPage

from eidolon_kernel.adapters.device_registry.hub_http import HubHttpDeviceAuthority
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityUnavailable
from eidolon_kernel.domain.model import DeviceAdmission
from eidolon_kernel.ports.system_services import (
    ResolvedServiceEndpoint,
    ServiceDirectoryUnavailable,
    SystemServiceDirectory,
)

HUB_SERVICE_ID = "hub"
HUB_DEVICE_ENDPOINT_ID = "device-authority.http"
HUB_DEVICE_CONTRACT = "eidolon.hub.device-directory.v1"


class DirectoryRoutedHubDeviceAuthority:
    def __init__(
        self,
        *,
        directory: SystemServiceDirectory,
        bearer_token: str,
        contracts: ContractRegistry,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if len(bearer_token.strip().encode()) < 32:
            raise ValueError("Hub device registry reader token is invalid")
        self._directory = directory
        self._token = bearer_token.strip()
        self._contracts = contracts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def _resolve_hub_endpoint(self) -> ResolvedServiceEndpoint:
        return await self._directory.resolve(
            service_id=HUB_SERVICE_ID,
            endpoint_id=HUB_DEVICE_ENDPOINT_ID,
            required_protocol="http",
            required_contract=HUB_DEVICE_CONTRACT,
        )

    async def is_available(self) -> bool:
        """Report whether eidolond currently publishes the required Hub endpoint."""
        try:
            await self._resolve_hub_endpoint()
        except ServiceDirectoryUnavailable:
            return False
        return True

    async def get_device(self, *, owner_id: str, device_id: str) -> DeviceAdmission:
        try:
            endpoint = await self._resolve_hub_endpoint()
        except ServiceDirectoryUnavailable as exc:
            raise AuthorityUnavailable(
                "Hub endpoint is unavailable from the System Service Directory"
            ) from exc
        delegate = HubHttpDeviceAuthority(
            base_url=endpoint.address,
            bearer_token=self._token,
            contracts=self._contracts,
            client=self._client,
        )
        return await delegate.get_device(owner_id=owner_id, device_id=device_id)

    async def list_claim_events(
        self, *, cursor: ClaimEventCursor, limit: int
    ) -> ClaimEventPage:
        try:
            endpoint = await self._resolve_hub_endpoint()
        except ServiceDirectoryUnavailable as exc:
            raise AuthorityUnavailable(
                "Hub endpoint is unavailable from the System Service Directory"
            ) from exc
        delegate = HubHttpDeviceAuthority(
            base_url=endpoint.address,
            bearer_token=self._token,
            contracts=self._contracts,
            client=self._client,
        )
        return await delegate.list_claim_events(cursor=cursor, limit=limit)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
