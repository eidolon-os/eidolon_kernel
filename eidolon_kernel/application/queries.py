"""Typed in-process projection queries and authoritative audit reads."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_kernel.domain.errors import InvalidRequest, NotFound
from eidolon_kernel.domain.model import AuditEvent, DeviceMount, require_identifier
from eidolon_kernel.ports.runtime import MountProjection, MountStore


@dataclass(slots=True)
class DeviceMountQueries:
    projection: MountProjection

    def get(self, *, owner_id: str, device_id: str) -> DeviceMount:
        owner_id = require_identifier("owner_id", owner_id, 64)
        mount = self.projection.get(require_identifier("device_id", device_id))
        if mount is None or mount.owner_id != owner_id:
            raise NotFound("device mount not found")
        return mount

    def resolve(self, *, owner_id: str, device_id: str) -> DeviceMount:
        mount = self.get(owner_id=owner_id, device_id=device_id)
        if not mount.active:
            raise NotFound("active device mount not found")
        return mount

    def list(
        self,
        *,
        owner_id: str,
        companion_id: str | None,
        active_only: bool,
        after_device_id: str | None,
        limit: int,
    ) -> tuple[DeviceMount, ...]:
        if owner_id is None:
            raise InvalidRequest("owner_id scope is required")
        owner_id = require_identifier("owner_id", owner_id, 64)
        if companion_id is not None:
            companion_id = require_identifier("companion_id", companion_id, 64)
        if after_device_id is not None:
            after_device_id = require_identifier("after_device_id", after_device_id)
        if not 1 <= limit <= 100:
            raise InvalidRequest("list limit must be between 1 and 100")
        return self.projection.list(
            owner_id=owner_id,
            companion_id=companion_id,
            active_only=active_only,
            after_device_id=after_device_id,
            limit=limit,
        )


@dataclass(slots=True)
class AuditQueries:
    store: MountStore

    def list(
        self, *, after_position: int, limit: int, owner_id: str
    ) -> tuple[AuditEvent, ...]:
        if after_position < 0:
            raise InvalidRequest("after_position cannot be negative")
        if not 1 <= limit <= 500:
            raise InvalidRequest("audit limit must be between 1 and 500")
        if owner_id is None:
            raise InvalidRequest("owner_id scope is required")
        owner_id = require_identifier("owner_id", owner_id, 64)
        return self.store.list_audit(
            after_position=after_position, limit=limit, owner_id=owner_id
        )
