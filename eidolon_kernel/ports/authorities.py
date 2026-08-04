"""External authority and local authorization ports."""

from __future__ import annotations

from typing import Protocol

from eidolon_kernel.domain.model import Actor, CompanionIdentity, DeviceAdmission


class DeviceAuthority(Protocol):
    async def get_device(self, *, owner_id: str, device_id: str) -> DeviceAdmission: ...


class CompanionAuthority(Protocol):
    async def get_companion(self, *, companion_id: str) -> CompanionIdentity: ...


class ActorAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        action: str,
        owner_id: str,
        credential: str | None,
        actor_id_hint: str | None,
        actor_owner_hint: str | None,
    ) -> Actor: ...
