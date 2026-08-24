"""Explicit mappings between transport DTOs and pure domain facts."""

from __future__ import annotations

from eidolon_sdk.device_foundation.v1 import DeviceRef

from eidolon_kernel.contracts.bindings import (
    AttachCompanionRequestWire,
    AuditEventWire,
    CompanionIdentityWire,
    DetachCompanionRequestWire,
    DeviceMountWire,
    HubDeviceDirectoryEntryWire,
    MountDeviceRequestWire,
    MutationResultWire,
    SystemServiceEndpointWire,
    UnmountDeviceRequestWire,
)
from eidolon_kernel.domain.commands import (
    AttachCompanionCommand,
    DetachCompanionCommand,
    MountDeviceCommand,
    UnmountDeviceCommand,
)
from eidolon_kernel.domain.model import (
    AuditEvent,
    CompanionIdentity,
    DeviceAdmission,
    DeviceMount,
)
from eidolon_kernel.ports.runtime import CommitResult
from eidolon_kernel.ports.system_services import ResolvedServiceEndpoint


def mount_request_to_domain(
    wire: MountDeviceRequestWire, *, owner_id: str
) -> MountDeviceCommand:
    return MountDeviceCommand(
        request_id=wire.request_id,
        device_id=wire.device_id,
        owner_id=owner_id,
        expected_revision=wire.expected_revision,
        replace_existing=wire.replace_existing,
    )


def attach_request_to_domain(
    wire: AttachCompanionRequestWire, *, device_id: str, owner_id: str
) -> AttachCompanionCommand:
    return AttachCompanionCommand(
        request_id=wire.request_id,
        device_id=device_id,
        owner_id=owner_id,
        companion_id=wire.companion_id,
        expected_revision=wire.expected_revision,
    )


def detach_request_to_domain(
    wire: DetachCompanionRequestWire, *, device_id: str, owner_id: str
) -> DetachCompanionCommand:
    return DetachCompanionCommand(
        request_id=wire.request_id,
        device_id=device_id,
        owner_id=owner_id,
        expected_revision=wire.expected_revision,
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
        device_ref=mount.device_ref,
        attached_companion_id=mount.attached_companion_id,
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
        attached_companion_id=mount.attached_companion_id,
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
        device_ref=DeviceRef(
            device_instance_id=wire.device_ref.device_instance_id,
            owner_domain_id=wire.device_ref.owner_domain_id,
            owner_domain_generation=wire.device_ref.owner_domain_generation,
            claim_generation=wire.device_ref.claim_generation,
            trust_epoch=wire.device_ref.trust_epoch,
        ),
    )


def companion_identity_to_domain(wire: CompanionIdentityWire) -> CompanionIdentity:
    return CompanionIdentity(
        companion_id=wire.companion_id,
        owner_id=wire.owner_id,
        status=wire.lifecycle_state,
    )


def system_service_endpoint_to_port(
    wire: SystemServiceEndpointWire,
) -> ResolvedServiceEndpoint:
    return ResolvedServiceEndpoint(
        service_id=wire.service_id,
        endpoint_id=wire.endpoint_id,
        protocol=wire.protocol,
        address=wire.address,
        contract=wire.contract,
    )
