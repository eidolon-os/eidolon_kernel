"""Clock, persistence and hot-projection ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from eidolon_kernel.domain.model import AuditEvent, DeviceMount


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class StoredRequest:
    request_id: str
    operation: str
    fingerprint: str
    mount: DeviceMount
    audit_position: int


@dataclass(frozen=True, slots=True)
class CommitResult:
    mount: DeviceMount
    audit_position: int
    replayed: bool


class MountStore(Protocol):
    def get(self, device_id: str) -> DeviceMount | None: ...

    def get_request(self, request_id: str) -> StoredRequest | None: ...

    def commit(
        self,
        *,
        mount: DeviceMount,
        expected_revision: int,
        operation: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> CommitResult: ...

    def list_all(self) -> tuple[DeviceMount, ...]: ...

    def list_audit(
        self, *, after_position: int, limit: int, owner_id: str | None = None
    ) -> tuple[AuditEvent, ...]: ...


class MountProjection(Protocol):
    def rebuild(self, mounts: tuple[DeviceMount, ...]) -> None: ...

    def put(self, mount: DeviceMount) -> None: ...

    def get(self, device_id: str) -> DeviceMount | None: ...

    def list(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        active_only: bool,
        after_device_id: str | None,
        limit: int,
    ) -> tuple[DeviceMount, ...]: ...
