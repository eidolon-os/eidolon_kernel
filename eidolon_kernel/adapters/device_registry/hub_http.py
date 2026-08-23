"""Narrow consumer of Hub's stable owner-scoped device GET."""

from __future__ import annotations

from urllib.parse import quote

import httpx
from jsonschema import ValidationError
from pydantic import ValidationError as PydanticValidationError

from eidolon_kernel.contracts.bindings import (
    HubClaimEventPageWire,
    HubDeviceDirectoryEntryWire,
)
from eidolon_kernel.contracts.mappers import (
    hub_claim_event_to_domain,
    hub_device_to_domain,
)
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityRejected, AuthorityUnavailable
from eidolon_kernel.domain.model import ClaimEvent, DeviceAdmission


class HubHttpDeviceAuthority:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        contracts: ContractRegistry,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Hub base_url must be HTTP(S)")
        if len(bearer_token.strip().encode()) < 32:
            raise ValueError("Hub device registry reader token is invalid")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token.strip()
        self._contracts = contracts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def get_device(self, *, owner_id: str, device_id: str) -> DeviceAdmission:
        url = (
            f"{self._base_url}/api/device-management/v1/owners/"
            f"{quote(owner_id, safe='')}/devices/{quote(device_id, safe='')}"
        )
        try:
            response = await self._client.get(
                url, headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as exc:
            raise AuthorityUnavailable("Hub device authority is unreachable") from exc
        if response.status_code == 404:
            raise AuthorityRejected("Hub device does not exist in owner scope")
        if response.status_code in {401, 403}:
            raise AuthorityUnavailable("Hub rejected Kernel's management credential")
        if response.status_code >= 500:
            raise AuthorityUnavailable("Hub device authority failed")
        if response.status_code != 200:
            raise AuthorityUnavailable(
                f"unexpected Hub device authority status {response.status_code}"
            )
        try:
            document = response.json()
            if not isinstance(document, dict):
                raise TypeError("Hub response must be an object")
            self._contracts.validate(
                "external/hub-device-directory-entry.schema.json", document
            )
            wire = HubDeviceDirectoryEntryWire.model_validate(document)
        except (TypeError, ValueError, ValidationError, PydanticValidationError) as exc:
            raise AuthorityUnavailable("Hub response violated consumed device contract") from exc
        return hub_device_to_domain(wire)

    async def list_claim_events(
        self, *, after_stream_position: int, limit: int
    ) -> tuple[ClaimEvent, ...]:
        try:
            response = await self._client.get(
                f"{self._base_url}/api/device-management/v1/claim-events",
                headers={"Authorization": f"Bearer {self._token}"},
                params={
                    "after_stream_position": str(after_stream_position),
                    "limit": str(limit),
                },
            )
        except httpx.HTTPError as exc:
            raise AuthorityUnavailable("Hub Claim event stream is unreachable") from exc
        if response.status_code in {401, 403}:
            raise AuthorityUnavailable("Hub rejected Kernel's Claim event credential")
        if response.status_code != 200:
            raise AuthorityUnavailable(
                f"unexpected Hub Claim event status {response.status_code}"
            )
        try:
            document = response.json()
            if not isinstance(document, dict):
                raise TypeError("Hub Claim event response must be an object")
            self._contracts.validate(
                "external/hub-claim-event-page.schema.json", document
            )
            page = HubClaimEventPageWire.model_validate(document)
        except (TypeError, ValueError, ValidationError, PydanticValidationError) as exc:
            raise AuthorityUnavailable("Hub Claim event response violated contract") from exc
        return tuple(hub_claim_event_to_domain(item) for item in page.events)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
