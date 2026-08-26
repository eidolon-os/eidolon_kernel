"""Device Mount aggregate and supporting immutable facts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from eidolon_sdk.device_foundation.v1 import DeviceRef, OwnerDomainId

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
    device_ref: DeviceRef

    def __post_init__(self) -> None:
        require_identifier("owner_id", self.owner_id, 64)
        if self.device_ref.device_instance_id != self.device_id:
            raise InvalidRequest("DeviceAdmission and DeviceRef do not match")


@dataclass(frozen=True, slots=True)
class CompanionIdentity:
    companion_id: str
    owner_id: str
    status: str


@dataclass(frozen=True, slots=True)
class DeviceMount:
    device_id: str
    owner_id: str
    owner_domain_id: str
    claim_generation: int
    trust_epoch: int
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
        try:
            owner_domain_id = str(OwnerDomainId(self.owner_domain_id))
        except ValueError as exc:
            raise InvalidRequest("owner_domain_id is invalid") from exc
        object.__setattr__(self, "owner_domain_id", owner_domain_id)
        if (
            min(
                self.owner_domain_generation,
                self.claim_generation,
                self.trust_epoch,
            )
            < 1
        ):
            raise InvalidRequest("mount Owner, Claim and trust generations must be positive")
        object.__setattr__(
            self, "request_id", require_identifier("request_id", self.request_id, 96)
        )
        if self.revision < 1:
            raise InvalidRequest("revision must be positive")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.fingerprint) is None:
            raise InvalidRequest("fingerprint must be a sha256 digest")
        object.__setattr__(self, "created_at", require_utc("created_at", self.created_at))
        object.__setattr__(self, "updated_at", require_utc("updated_at", self.updated_at))
        if self.updated_at < self.created_at:
            raise InvalidRequest("updated_at cannot precede created_at")

    @property
    def device_ref(self) -> DeviceRef:
        """Return the exact canonical generation fenced identity persisted by Mount."""

        return DeviceRef(
            device_instance_id=self.device_id,
            owner_domain_id=self.owner_domain_id,
            owner_domain_generation=self.owner_domain_generation,
            claim_generation=self.claim_generation,
            trust_epoch=self.trust_epoch,
        )

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
        if device_ref.device_instance_id != device_id:
            raise InvalidRequest("mount target and DeviceRef do not match")
        return cls(
            device_id=device_id,
            owner_id=owner_id,
            owner_domain_id=str(device_ref.owner_domain_id),
            claim_generation=device_ref.claim_generation,
            trust_epoch=device_ref.trust_epoch,
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
            or str(device_ref.owner_domain_id) != self.owner_domain_id
        ):
            raise InvalidRequest("remount target and DeviceRef do not match")
        return DeviceMount(
            device_id=self.device_id,
            owner_id=owner_id,
            owner_domain_id=self.owner_domain_id,
            claim_generation=device_ref.claim_generation,
            trust_epoch=device_ref.trust_epoch,
            revision=self.revision + 1,
            created_at=at,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
            active=True,
            owner_domain_generation=device_ref.owner_domain_generation,
        )

    def unmounted(self, *, at: datetime, request_id: str, fingerprint: str) -> DeviceMount:
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


#: What an audit row is about. Two kinds of fact live in this Owner's namespace
#: now — whether a device is mounted, and which Companion answers through one of
#: its Bodies — and they move independently. A stream that could only describe
#: the first would have to leave the second unrecorded or pretend an assignment
#: change was a mount change.
AUDIT_SUBJECT_DEVICE_MOUNT = "device-mount"
AUDIT_SUBJECT_BODY_ASSIGNMENT = "body-assignment"

AUDIT_SUBJECTS = (AUDIT_SUBJECT_DEVICE_MOUNT, AUDIT_SUBJECT_BODY_ASSIGNMENT)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One committed change in this Owner's namespace.

    ``device_id`` is present on every row including assignment ones: a Body
    belongs to a device, and "what happened to this speaker" is the question
    anyone reading this stream is actually asking.
    """

    position: int
    event_id: str
    event_type: str
    owner_id: str
    device_id: str
    subject: str
    #: The device for a mount, the Body endpoint for an assignment.
    subject_id: str
    subject_revision: int
    request_id: str
    fingerprint: str
    occurred_at: datetime
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if self.position < 1:
            raise InvalidRequest("audit position must be positive")
        object.__setattr__(self, "event_id", require_identifier("event_id", self.event_id, 255))
        object.__setattr__(
            self, "event_type", require_identifier("event_type", self.event_type, 255)
        )
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        if self.subject not in AUDIT_SUBJECTS:
            raise InvalidRequest("audit subject is not one this Kernel writes")
        object.__setattr__(
            self, "subject_id", require_identifier("subject_id", self.subject_id)
        )
        if self.subject_revision < 1:
            raise InvalidRequest("audit subject revision must be positive")
        object.__setattr__(
            self, "request_id", require_identifier("request_id", self.request_id, 96)
        )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.fingerprint) is None:
            raise InvalidRequest("fingerprint must be a sha256 digest")
        object.__setattr__(self, "occurred_at", require_utc("occurred_at", self.occurred_at))


def request_fingerprint(operation: str, values: dict[str, Any]) -> str:
    body = {"operation": operation, **values}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
