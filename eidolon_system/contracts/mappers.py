"""Explicit mappings between system wire DTOs and domain entities."""

from __future__ import annotations

from eidolon_sdk.system.v1 import (
    HOST_VITALS_OPERATION,
    HostVitalsWire,
    MeasurementWire,
)

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
    HostVitals,
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
        requires_capability=wire.requires_capability,
        restart_on_network_change=wire.restart_on_network_change,
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
        network_current=status.network_current,
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


def vitals_to_wire(vitals: HostVitals) -> HostVitalsWire:
    return HostVitalsWire(
        operation=HOST_VITALS_OPERATION,
        observed_at=vitals.observed_at,
        measurements=tuple(
            MeasurementWire(
                name=measurement.name,
                value=measurement.value,
                unit=measurement.unit,
                capacity=measurement.capacity,
                unavailable_reason=measurement.unavailable_reason,
            )
            for measurement in vitals.measurements
        ),
    )
