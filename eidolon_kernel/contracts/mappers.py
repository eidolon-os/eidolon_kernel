"""Explicit mappings between transport DTOs and pure domain facts."""

from __future__ import annotations

from eidolon_sdk.device_foundation.v1 import DeviceRef

from eidolon_kernel.contracts.bindings import (
    AuditEventWire,
    BodyAssignmentWire,
    BodyEndpointWire,
    CompanionIdentityWire,
    DeviceMountWire,
    HubDeviceDirectoryEntryWire,
    MountDeviceRequestWire,
    MutationResultWire,
    ReplaceAssignmentRequestWire,
    SystemServiceEndpointWire,
    UnmountDeviceRequestWire,
)
from eidolon_kernel.domain.body import BodyAssignment, BodyEndpoint
from eidolon_kernel.domain.commands import (
    MountDeviceCommand,
    ReplaceAssignmentCommand,
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


def replace_assignment_request_to_domain(
    wire: ReplaceAssignmentRequestWire, *, body_endpoint_id: str, owner_id: str
) -> ReplaceAssignmentCommand:
    return ReplaceAssignmentCommand(
        request_id=wire.request_id,
        owner_id=owner_id,
        body_endpoint_id=body_endpoint_id,
        expected_assignment_revision=wire.expected_assignment_revision,
        companion_id=wire.companion_id,
        origin=wire.origin,
        change_reason=wire.change_reason,
        # Not from the caller. Nothing on this Host defines a resource policy, so
        # the only honest value is none, and the request has no field for one.
        policy_refs=(),
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


def assignment_to_wire(
    assignment: BodyAssignment, *, endpoint: BodyEndpoint | None
) -> BodyAssignmentWire:
    return BodyAssignmentWire(
        assignment_id=assignment.assignment_id,
        body_endpoint_id=assignment.body_endpoint_id,
        device_id=assignment.device_id,
        endpoint_id=assignment.endpoint_id,
        owner_id=assignment.owner_id,
        companion_id=assignment.companion_id,
        selection_provenance=assignment.selection_provenance,
        change_reason=assignment.change_reason,
        mode=assignment.mode,
        policy_refs=assignment.policy_refs,
        revision=assignment.revision,
        generation=assignment.generation,
        updated_at=assignment.updated_at,
        status=assignment.status(endpoint=endpoint),
    )


def endpoint_to_wire(
    endpoint: BodyEndpoint, assignment: BodyAssignment | None
) -> BodyEndpointWire:
    return BodyEndpointWire(
        body_endpoint_id=endpoint.body_endpoint_id,
        device_id=endpoint.device_id,
        owner_id=endpoint.owner_id,
        endpoint_id=endpoint.endpoint_id,
        roles=endpoint.roles,
        assignment_policy=endpoint.assignment_policy,
        risk_class=endpoint.risk_class,
        concurrency=endpoint.concurrency,
        source=endpoint.source,
        present=endpoint.present,
        assignment=(
            None if assignment is None else assignment_to_wire(assignment, endpoint=endpoint)
        ),
    )


def audit_to_wire(event: AuditEvent) -> AuditEventWire:
    return AuditEventWire(
        position=event.position,
        event_id=event.event_id,
        event_type=event.event_type,
        device_id=event.device_id,
        owner_id=event.owner_id,
        subject=event.subject,
        subject_id=event.subject_id,
        subject_revision=event.subject_revision,
        request_id=event.request_id,
        fingerprint=event.fingerprint,
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
