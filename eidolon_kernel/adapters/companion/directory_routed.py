"""Eidolon Data CompanionAuthority routed through the local service directory."""

from __future__ import annotations

import httpx

from eidolon_kernel.adapters.companion.eidolon_data_http import (
    EidolonDataHttpCompanionAuthority,
)
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityUnavailable
from eidolon_kernel.domain.model import CompanionIdentity
from eidolon_kernel.ports.system_services import (
    ResolvedServiceEndpoint,
    ServiceDirectoryUnavailable,
    SystemServiceDirectory,
)

DATA_SERVICE_ID = "data"
DATA_COMPANION_ENDPOINT_ID = "companion-authority.http"
DATA_COMPANION_CONTRACT = "https://eidolon.dev/data/contracts/v1/companion/identity.schema.json"


class DirectoryRoutedEidolonDataCompanionAuthority:
    def __init__(
        self,
        *,
        directory: SystemServiceDirectory,
        bearer_token: str,
        contracts: ContractRegistry,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if len(bearer_token.strip()) < 24:
            raise ValueError("Companion authority bearer token is invalid")
        self._directory = directory
        self._token = bearer_token.strip()
        self._contracts = contracts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def _resolve_data_endpoint(self) -> ResolvedServiceEndpoint:
        return await self._directory.resolve(
            service_id=DATA_SERVICE_ID,
            endpoint_id=DATA_COMPANION_ENDPOINT_ID,
            required_protocol="http",
            required_contract=DATA_COMPANION_CONTRACT,
        )

    async def is_available(self) -> bool:
        """Report whether eidolond currently publishes the Data endpoint."""
        try:
            await self._resolve_data_endpoint()
        except ServiceDirectoryUnavailable:
            return False
        return True

    async def get_companion(self, *, companion_id: str) -> CompanionIdentity:
        try:
            endpoint = await self._resolve_data_endpoint()
        except ServiceDirectoryUnavailable as exc:
            raise AuthorityUnavailable(
                "Companion endpoint is unavailable from the System Service Directory"
            ) from exc
        delegate = EidolonDataHttpCompanionAuthority(
            base_url=endpoint.address,
            bearer_token=self._token,
            contracts=self._contracts,
            client=self._client,
        )
        return await delegate.get_companion(companion_id=companion_id)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
