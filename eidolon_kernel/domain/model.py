"""Device Mount aggregate and supporting immutable facts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from eidolon_kernel.domain.errors import InvalidRequest


def require_identifier(name: str, value: str, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise InvalidRequest(f"{name} must contain 1..{maximum} non-blank characters")
    return normalized


def require_utc(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRequest(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DeviceAdmission:
    device_id: str
    owner_id: str
    status: str
    manifest_revision: str


@dataclass(frozen=True, slots=True)
class CompanionIdentity:
    companion_id: str
    owner_id: str
    status: str


@dataclass(frozen=True, slots=True)
class DeviceMount:
    device_id: str
    owner_id: str
    companion_id: str
    revision: int
    created_at: datetime
    updated_at: datetime
    request_id: str
    fingerprint: str
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        object.__setattr__(
            self, "companion_id", require_identifier("companion_id", self.companion_id, 64)
        )
        object.__setattr__(self, "request_id", require_identifier("request_id", self.request_id, 96))
        if self.revision < 1:
            raise InvalidRequest("revision must be positive")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.fingerprint) is None:
            raise InvalidRequest("fingerprint must be a sha256 digest")
        object.__setattr__(self, "created_at", require_utc("created_at", self.created_at))
        object.__setattr__(self, "updated_at", require_utc("updated_at", self.updated_at))
        if self.updated_at < self.created_at:
            raise InvalidRequest("updated_at cannot precede created_at")

    @classmethod
    def first(
        cls,
        *,
        device_id: str,
        owner_id: str,
        companion_id: str,
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> DeviceMount:
        return cls(
            device_id=device_id,
            owner_id=owner_id,
            companion_id=companion_id,
            revision=1,
            created_at=at,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
        )

    def mounted_as(
        self,
        *,
        owner_id: str,
        companion_id: str,
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> DeviceMount:
        at = require_utc("mounted_at", at)
        if at < self.updated_at:
            raise InvalidRequest("mount transition time cannot move backwards")
        if owner_id != self.owner_id:
            raise InvalidRequest("device mount owner namespace cannot change")
        return DeviceMount(
            device_id=self.device_id,
            owner_id=owner_id,
            companion_id=companion_id,
            revision=self.revision + 1,
            created_at=at,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
            active=True,
        )

    def unmounted(
        self, *, at: datetime, request_id: str, fingerprint: str
    ) -> DeviceMount:
        at = require_utc("unmounted_at", at)
        if at < self.updated_at:
            raise InvalidRequest("mount transition time cannot move backwards")
        return replace(
            self,
            revision=self.revision + 1,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
            active=False,
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    position: int
    event_id: str
    event_type: str
    mount: DeviceMount
    occurred_at: datetime
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if self.position < 1:
            raise InvalidRequest("audit position must be positive")
        object.__setattr__(self, "event_id", require_identifier("event_id", self.event_id, 255))
        object.__setattr__(
            self, "event_type", require_identifier("event_type", self.event_type, 255)
        )
        object.__setattr__(self, "occurred_at", require_utc("occurred_at", self.occurred_at))
        if self.occurred_at != self.mount.updated_at:
            raise InvalidRequest("audit occurrence time must match mount update time")


def request_fingerprint(operation: str, values: dict[str, Any]) -> str:
    body = {"operation": operation, **values}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
