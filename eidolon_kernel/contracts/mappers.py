"""Explicit mappings between transport DTOs and pure domain facts."""

from __future__ import annotations

from eidolon_kernel.contracts.bindings import (
    AuditEventWire,
    DeviceMountWire,
    HubDeviceDirectoryEntryWire,
    MountDeviceRequestWire,
    MutationResultWire,
    UnmountDeviceRequestWire,
)
from eidolon_kernel.domain.commands import MountDeviceCommand, UnmountDeviceCommand
from eidolon_kernel.domain.model import AuditEvent, DeviceAdmission, DeviceMount
from eidolon_kernel.ports.runtime import CommitResult


def mount_request_to_domain(
    wire: MountDeviceRequestWire, *, owner_id: str
) -> MountDeviceCommand:
    return MountDeviceCommand(
        request_id=wire.request_id,
        device_id=wire.device_id,
        owner_id=owner_id,
        companion_id=wire.companion_id,
        expected_revision=wire.expected_revision,
        replace_existing=wire.replace_existing,
    )


def unmount_request_to_domain(
    wire: UnmountDeviceRequestWire, *, device_id: str, owner_id: str
) -> UnmountDeviceCommand:
    return UnmountDeviceCommand(
        request_id=wire.request_id,
        device_id=device_id,
        owner_id=owner_id,
        expected_revision=wire.expected_revision,
    )


def mount_to_wire(mount: DeviceMount) -> DeviceMountWire:
    return DeviceMountWire(
        device_id=mount.device_id,
        owner_id=mount.owner_id,
        companion_id=mount.companion_id,
        revision=mount.revision,
        created_at=mount.created_at,
        updated_at=mount.updated_at,
        request_id=mount.request_id,
        fingerprint=mount.fingerprint,
        active=mount.active,
    )


def commit_to_wire(result: CommitResult) -> MutationResultWire:
    return MutationResultWire(
        mount=mount_to_wire(result.mount),
        audit_position=result.audit_position,
        replayed=result.replayed,
    )


def audit_to_wire(event: AuditEvent) -> AuditEventWire:
    mount = event.mount
    return AuditEventWire(
        position=event.position,
        event_id=event.event_id,
        event_type=event.event_type,
        device_id=mount.device_id,
        owner_id=mount.owner_id,
        companion_id=mount.companion_id,
        mount_revision=mount.revision,
        active=mount.active,
        request_id=mount.request_id,
        fingerprint=mount.fingerprint,
        occurred_at=event.occurred_at,
        data=event.data,
    )


def hub_device_to_domain(wire: HubDeviceDirectoryEntryWire) -> DeviceAdmission:
    return DeviceAdmission(
        device_id=wire.device_id,
        owner_id=wire.owner_scope,
        status=wire.lifecycle_state,
        manifest_revision=wire.manifest_revision,
    )
