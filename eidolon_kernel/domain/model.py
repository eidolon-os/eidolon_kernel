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
class DeviceRef:
    device_instance_id: str
    owner_domain_id: str
    claim_generation: int
    trust_epoch: int
    accepted_manifest_digest: str
    owner_domain_generation: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "device_instance_id",
            require_identifier("device_instance_id", self.device_instance_id),
        )
        object.__setattr__(
            self,
            "owner_domain_id",
            require_identifier("owner_domain_id", self.owner_domain_id, 64),
        )
        if min(
            self.owner_domain_generation,
            self.claim_generation,
            self.trust_epoch,
        ) < 1:
            raise InvalidRequest("Owner, Claim and trust generations must be positive")
        object.__setattr__(
            self,
            "accepted_manifest_digest",
            require_identifier(
                "accepted_manifest_digest", self.accepted_manifest_digest
            ),
        )


@dataclass(frozen=True, slots=True)
class DeviceAdmission:
    device_id: str
    owner_id: str
    status: str
    manifest_revision: str
    device_ref: DeviceRef

    def __post_init__(self) -> None:
        if (
            self.device_ref.device_instance_id != self.device_id
            or self.device_ref.owner_domain_id != self.owner_id
            or self.device_ref.accepted_manifest_digest != self.manifest_revision
        ):
            raise InvalidRequest("DeviceAdmission and DeviceRef do not match")


@dataclass(frozen=True, slots=True)
class ClaimEvent:
    stream_position: int
    event_id: str
    event_type: str
    device_ref: DeviceRef
    aggregate_revision: int
    correlation_id: str
    causation_id: str
    occurred_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if self.stream_position < 1 or self.aggregate_revision < 1:
            raise InvalidRequest("Claim event positions and revisions must be positive")
        object.__setattr__(self, "occurred_at", require_utc("occurred_at", self.occurred_at))


@dataclass(frozen=True, slots=True)
class CompanionIdentity:
    companion_id: str
    owner_id: str
    status: str


@dataclass(frozen=True, slots=True)
class DeviceMount:
    device_id: str
    owner_id: str
    claim_generation: int
    trust_epoch: int
    accepted_manifest_digest: str
    attached_companion_id: str | None
    revision: int
    created_at: datetime
    updated_at: datetime
    request_id: str
    fingerprint: str
    active: bool = True
    owner_domain_generation: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        if min(
            self.owner_domain_generation,
            self.claim_generation,
            self.trust_epoch,
        ) < 1:
            raise InvalidRequest("mount Owner, Claim and trust generations must be positive")
        object.__setattr__(
            self,
            "accepted_manifest_digest",
            require_identifier(
                "accepted_manifest_digest", self.accepted_manifest_digest
            ),
        )
        if self.attached_companion_id is not None:
            object.__setattr__(
                self,
                "attached_companion_id",
                require_identifier("attached_companion_id", self.attached_companion_id, 64),
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
        device_ref: DeviceRef,
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> DeviceMount:
        if (
            device_ref.device_instance_id != device_id
            or device_ref.owner_domain_id != owner_id
        ):
            raise InvalidRequest("mount target and DeviceRef do not match")
        return cls(
            device_id=device_id,
            owner_id=owner_id,
            claim_generation=device_ref.claim_generation,
            trust_epoch=device_ref.trust_epoch,
            accepted_manifest_digest=device_ref.accepted_manifest_digest,
            attached_companion_id=None,
            revision=1,
            created_at=at,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
            owner_domain_generation=device_ref.owner_domain_generation,
        )

    def mounted_as(
        self,
        *,
        owner_id: str,
        device_ref: DeviceRef,
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> DeviceMount:
        at = require_utc("mounted_at", at)
        if at < self.updated_at:
            raise InvalidRequest("mount transition time cannot move backwards")
        if owner_id != self.owner_id:
            raise InvalidRequest("device mount owner namespace cannot change")
        if (
            device_ref.device_instance_id != self.device_id
            or device_ref.owner_domain_id != owner_id
        ):
            raise InvalidRequest("remount target and DeviceRef do not match")
        return DeviceMount(
            device_id=self.device_id,
            owner_id=owner_id,
            claim_generation=device_ref.claim_generation,
            trust_epoch=device_ref.trust_epoch,
            accepted_manifest_digest=device_ref.accepted_manifest_digest,
            attached_companion_id=None,
            revision=self.revision + 1,
            created_at=at,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
            active=True,
            owner_domain_generation=device_ref.owner_domain_generation,
        )

    def attached(
        self,
        *,
        companion_id: str,
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> DeviceMount:
        at = require_utc("attached_at", at)
        if at < self.updated_at:
            raise InvalidRequest("attachment transition time cannot move backwards")
        if not self.active:
            raise InvalidRequest("cannot attach a companion to an inactive device mount")
        return replace(
            self,
            attached_companion_id=require_identifier("companion_id", companion_id, 64),
            revision=self.revision + 1,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
        )

    def detached(
        self, *, at: datetime, request_id: str, fingerprint: str
    ) -> DeviceMount:
        at = require_utc("detached_at", at)
        if at < self.updated_at:
            raise InvalidRequest("attachment transition time cannot move backwards")
        if not self.active:
            raise InvalidRequest("cannot detach a companion from an inactive device mount")
        return replace(
            self,
            attached_companion_id=None,
            revision=self.revision + 1,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
        )

    def unmounted(
        self, *, at: datetime, request_id: str, fingerprint: str
    ) -> DeviceMount:
        at = require_utc("unmounted_at", at)
        if at < self.updated_at:
            raise InvalidRequest("mount transition time cannot move backwards")
        return replace(
            self,
            attached_companion_id=None,
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
