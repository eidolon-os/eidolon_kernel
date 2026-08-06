"""Explicit mappings between system wire DTOs and domain entities."""

from __future__ import annotations

from eidolon_system.contracts.bindings import (
    AuditEventWire,
    DesiredStateWire,
    EndpointWire,
    ManifestEndpointWire,
    ManifestServiceWire,
    MutationResultWire,
    ServiceStatusWire,
)
from eidolon_system.domain.model import (
    DesiredServiceState,
    ServiceDefinition,
    ServiceEndpoint,
    ServiceStatus,
    SystemAuditEvent,
)
from eidolon_system.domain.model import StoredMutation as DomainStoredMutation


def manifest_endpoint_to_domain(wire: ManifestEndpointWire) -> ServiceEndpoint:
    return ServiceEndpoint(**wire.model_dump())


def manifest_service_to_domain(wire: ManifestServiceWire) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=wire.service_id,
        description=wire.description,
        required=wire.required,
        enabled_by_default=wire.enabled_by_default,
        dependencies=wire.dependencies,
        host_targets=wire.host_targets,
        endpoints=tuple(manifest_endpoint_to_domain(endpoint) for endpoint in wire.endpoints),
    )


def desired_to_wire(state: DesiredServiceState) -> DesiredStateWire:
    return DesiredStateWire(
        service_id=state.service_id,
        enabled=state.enabled,
        revision=state.revision,
        updated_at=state.updated_at,
    )


def endpoint_to_wire(endpoint: ServiceEndpoint, *, service_id: str) -> EndpointWire:
    return EndpointWire(
        service_id=service_id,
        endpoint_id=endpoint.endpoint_id,
        protocol=endpoint.protocol,
        address=endpoint.address,
        contract=endpoint.contract,
    )


def status_to_wire(status: ServiceStatus) -> ServiceStatusWire:
    return ServiceStatusWire(
        service_id=status.service_id,
        required=status.required,
        desired=desired_to_wire(status.desired),
        runtime_state=status.runtime_state,
        detail=status.detail,
        observed_at=status.observed_at,
        endpoints=tuple(
            endpoint_to_wire(endpoint, service_id=status.service_id)
            for endpoint in status.endpoints
        ),
    )


def mutation_to_wire(result: DomainStoredMutation) -> MutationResultWire:
    return MutationResultWire(
        state=desired_to_wire(result.state),
        audit_position=result.audit_position,
        replayed=result.replayed,
    )


def audit_to_wire(event: SystemAuditEvent) -> AuditEventWire:
    return AuditEventWire(
        position=event.position,
        service_id=event.service_id,
        operation=event.operation,
        desired_revision=event.desired_revision,
        enabled=event.enabled,
        request_id=event.request_id,
        fingerprint=event.fingerprint,
        occurred_at=event.occurred_at,
    )
