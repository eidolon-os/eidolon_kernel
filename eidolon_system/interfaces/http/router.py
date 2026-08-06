"""Local, machine-scoped eidolond API; it has no Owner namespace."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from jsonschema import ValidationError

from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.contracts.bindings import (
    AuditPageWire,
    EndpointWire,
    MutationRequestWire,
    MutationResultWire,
    ServicePageWire,
    ServiceStatusWire,
)
from eidolon_system.contracts.mappers import (
    audit_to_wire,
    endpoint_to_wire,
    mutation_to_wire,
    status_to_wire,
)
from eidolon_system.contracts.registry import SystemContractRegistry
from eidolon_system.domain.errors import (
    Conflict,
    HostOperationFailed,
    InvalidManifest,
    NotFound,
    NotReady,
)


def _document(model) -> dict:
    return model.model_dump(mode="json")


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, NotReady):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, Conflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, HostOperationFailed):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, (InvalidManifest, ValidationError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def create_system_router(
    *, manager: ServiceManager, contracts: SystemContractRegistry
) -> APIRouter:
    router = APIRouter(prefix="/api/system/v1", tags=["system-services"])

    @router.get("/services", response_model=ServicePageWire)
    async def list_services() -> ServicePageWire:
        wire = ServicePageWire(
            services=tuple(status_to_wire(status) for status in manager.list_services())
        )
        contracts.validate("system/service-page.schema.json", _document(wire))
        return wire

    @router.get("/services/{service_id}", response_model=ServiceStatusWire)
    async def get_service(service_id: str) -> ServiceStatusWire:
        try:
            wire = status_to_wire(manager.get_service(service_id))
            contracts.validate("system/service-status.schema.json", _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    @router.get(
        "/services/{service_id}/endpoints/{endpoint_id}", response_model=EndpointWire
    )
    async def resolve_endpoint(service_id: str, endpoint_id: str) -> EndpointWire:
        try:
            wire = endpoint_to_wire(
                manager.resolve(service_id, endpoint_id), service_id=service_id
            )
            contracts.validate("system/endpoint.schema.json", _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    async def mutate(
        service_id: str, payload: MutationRequestWire, expected_operation: str
    ) -> MutationResultWire:
        contracts.validate("system/mutation-request.schema.json", _document(payload))
        if payload.operation != expected_operation:
            raise ValueError(f"operation must be {expected_operation}")
        if expected_operation == "system.service.restart":
            result = await manager.restart(
                service_id=service_id,
                expected_revision=payload.expected_revision,
                request_id=payload.request_id,
            )
        else:
            result = await manager.set_enabled(
                service_id=service_id,
                enabled=expected_operation == "system.service.enable",
                expected_revision=payload.expected_revision,
                request_id=payload.request_id,
            )
        wire = mutation_to_wire(result)
        contracts.validate("system/mutation-result.schema.json", _document(wire))
        return wire

    @router.post("/services/{service_id}/enable", response_model=MutationResultWire)
    async def enable_service(
        service_id: str, payload: MutationRequestWire
    ) -> MutationResultWire:
        try:
            return await mutate(service_id, payload, "system.service.enable")
        except Exception as exc:
            _raise_http(exc)

    @router.post("/services/{service_id}/disable", response_model=MutationResultWire)
    async def disable_service(
        service_id: str, payload: MutationRequestWire
    ) -> MutationResultWire:
        try:
            return await mutate(service_id, payload, "system.service.disable")
        except Exception as exc:
            _raise_http(exc)

    @router.post("/services/{service_id}/restart", response_model=MutationResultWire)
    async def restart_service(
        service_id: str, payload: MutationRequestWire
    ) -> MutationResultWire:
        try:
            return await mutate(service_id, payload, "system.service.restart")
        except Exception as exc:
            _raise_http(exc)

    @router.get("/audit/events", response_model=AuditPageWire)
    async def list_audit(
        after_position: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> AuditPageWire:
        events = manager.list_audit(after_position=after_position, limit=limit)
        wire = AuditPageWire(
            next_position=events[-1].position if events else after_position,
            events=tuple(audit_to_wire(event) for event in events),
        )
        contracts.validate("system/audit-page.schema.json", _document(wire))
        return wire

    return router
