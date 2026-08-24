"""External authority and local authorization ports."""

from __future__ import annotations

from typing import Protocol

from eidolon_sdk.device_foundation.v1 import ClaimEventCursor, ClaimEventPage

from eidolon_kernel.domain.model import CompanionIdentity, DeviceAdmission


class DeviceAuthority(Protocol):
    async def get_device(self, *, owner_id: str, device_id: str) -> DeviceAdmission: ...

    async def list_claim_events(
        self, *, cursor: ClaimEventCursor, limit: int
    ) -> ClaimEventPage: ...


class CompanionAuthority(Protocol):
    async def get_companion(self, *, companion_id: str) -> CompanionIdentity: ...


class OwnerAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        action: str,
        credential: str | None,
        owner_id_hint: str | None,
    ) -> str: ...
