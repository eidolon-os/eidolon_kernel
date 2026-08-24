"""Strict runtime normalization bindings; JSON Schema remains normative."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from eidolon_sdk.device_foundation.v1 import DeviceRef
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DeviceMountWire(ContractModel):
    operation: Literal["kernel.device-mount"] = "kernel.device-mount"
    device_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=64)
    device_ref: DeviceRef
    attached_companion_id: str | None = Field(default=None, min_length=1, max_length=64)
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    request_id: str = Field(min_length=1, max_length=96)
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    active: bool


class MountDeviceRequestWire(ContractModel):
    operation: Literal["device.mount"]
    request_id: str = Field(min_length=1, max_length=96)
    device_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0, strict=True)
    replace_existing: bool = Field(strict=True)


class AttachCompanionRequestWire(ContractModel):
    operation: Literal["companion.attach"]
    request_id: str = Field(min_length=1, max_length=96)
    companion_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1, strict=True)


class DetachCompanionRequestWire(ContractModel):
    operation: Literal["companion.detach"]
    request_id: str = Field(min_length=1, max_length=96)
    expected_revision: int = Field(ge=1, strict=True)


class UnmountDeviceRequestWire(ContractModel):
    operation: Literal["device.unmount"]
    request_id: str = Field(min_length=1, max_length=96)
    expected_revision: int = Field(ge=1, strict=True)


class MutationResultWire(ContractModel):
    operation: Literal["kernel.device-mount-mutation-result"] = (
        "kernel.device-mount-mutation-result"
    )
    mount: DeviceMountWire
    audit_position: int = Field(ge=1)
    replayed: bool


class DeviceMountPageWire(ContractModel):
    operation: Literal["kernel.device-mount-page"] = "kernel.device-mount-page"
    next_cursor: str | None = Field(default=None, max_length=128)
    mounts: tuple[DeviceMountWire, ...] = Field(default=(), max_length=100)

    @field_validator("mounts", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class AuditEventWire(ContractModel):
    operation: Literal["kernel.audit-event"] = "kernel.audit-event"
    position: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=64)
    attached_companion_id: str | None = Field(default=None, min_length=1, max_length=64)
    mount_revision: int = Field(ge=1)
    active: bool
    request_id: str = Field(min_length=1, max_length=96)
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    occurred_at: datetime
    data: dict[str, Any]


class AuditPageWire(ContractModel):
    operation: Literal["kernel.audit-page"] = "kernel.audit-page"
    next_position: int = Field(ge=0)
    events: tuple[AuditEventWire, ...] = Field(default=(), max_length=500)

    @field_validator("events", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class LegacyHubDirectoryDeviceRefWire(ContractModel):
    """Pre-PH2 Hub directory shape retained outside the Claim-event capability."""

    device_instance_id: str = Field(min_length=1, max_length=128)
    owner_domain_id: str = Field(min_length=1, max_length=64)
    owner_domain_generation: int = Field(ge=1)
    claim_generation: int = Field(ge=1)
    trust_epoch: int = Field(ge=1)
    accepted_manifest_digest: str = Field(min_length=1, max_length=128)


class HubDeviceDirectoryEntryWire(ContractModel):
    operation: Literal["device.directory-entry"]
    device_id: str = Field(min_length=1, max_length=128)
    owner_scope: str = Field(min_length=1, max_length=64)
    display_name: str = Field(max_length=128)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest: dict[str, Any]
    manifest_revision: str = Field(min_length=1, max_length=128)
    lifecycle_state: Literal["pending-approval", "approved", "revoked"]
    enrolled_at: datetime
    updated_at: datetime
    device_ref: LegacyHubDirectoryDeviceRefWire


class CompanionIdentityWire(ContractModel):
    """The Companion identity Kernel consumes, closed to anything unnamed.

    Closed on purpose, like the schema beside it: a field arriving here that
    nobody admitted is Data handing Kernel something no one decided it should
    see. Each addition is let in deliberately — that review is the point, not
    an obstacle to it.
    """

    operation: Literal["companion.identity"]
    companion_id: str = Field(min_length=1, max_length=64)
    owner_id: str = Field(min_length=1, max_length=64)
    #: What the Owner calls this Companion. Kernel does not use it; it is
    #: admitted so Data may answer one document to every consumer.
    display_name: str = Field(default="", max_length=128)
    #: Kernel only ever asks whether this Companion may be assigned, which is
    #: ``active`` and nothing else. The other three are admitted so the answer
    #: parses — a Companion being retired or archived is a real state Data can
    #: report, and refusing to read it would turn "you may not assign this" into
    #: "the authority is broken".
    #: Kept as a literal here on purpose. Every other consumer imports this
    #: vocabulary from ``eidolon_sdk.biz.contracts.companion``; this package
    #: deliberately does not depend on the SDK and mirrors external schemas
    #: instead (``schemas/external/``), with a test comparing the mirror to the
    #: producer. That is a trust boundary, not an oversight — so the copy stays
    #: and the mirror test is what keeps it honest.
    lifecycle_state: Literal["active", "retiring", "archived", "deleting"]
    #: Neither is used here, and both are admitted for the same reason as
    #: ``display_name``: Data answers one document to every consumer. ``kind``
    #: is a product type, ``revision`` is the version a writer compares against
    #: — assignment eligibility depends on neither.
    kind: str = Field(default="", max_length=32)
    revision: int = Field(default=1, ge=1)


class SystemServiceEndpointWire(ContractModel):
    operation: Literal["system.service-endpoint"]
    service_id: str = Field(min_length=1, max_length=128)
    endpoint_id: str = Field(min_length=1, max_length=128)
    protocol: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=1, max_length=2048)
    contract: str = Field(min_length=1, max_length=256)
