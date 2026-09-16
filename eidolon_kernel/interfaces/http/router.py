"""Kernel V1 Device Mount and audit HTTP API."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Header, HTTPException, Query
from jsonschema import ValidationError

from eidolon_kernel.application.body_assignments import BodyEndpoints, ReplaceAssignment
from eidolon_kernel.application.device_mounts import MountDevice, UnmountDevice
from eidolon_kernel.application.queries import AuditQueries, DeviceMountQueries
from eidolon_kernel.contracts.bindings import (
    AuditPageWire,
    BodyEndpointPageWire,
    BodyEndpointWire,
    DeviceMountPageWire,
    DeviceMountWire,
    MountDeviceRequestWire,
    MutationResultWire,
    ReplaceAssignmentRequestWire,
    UnmountDeviceRequestWire,
)
from eidolon_kernel.contracts.mappers import (
    audit_to_wire,
    commit_to_wire,
    endpoint_to_wire,
    mount_request_to_domain,
    mount_to_wire,
    replace_assignment_request_to_domain,
    unmount_request_to_domain,
)
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    AuthorityUnavailable,
    AuthorizationDenied,
    Conflict,
    InvalidRequest,
    NotFound,
)
from eidolon_kernel.ports.authorities import OwnerAuthorizer


@dataclass(frozen=True, slots=True)
class KernelHttpServices:
    mount_device: MountDevice
    replace_assignment: ReplaceAssignment
    unmount_device: UnmountDevice
    mounts: DeviceMountQueries
    body_endpoints: BodyEndpoints
    audit: AuditQueries
    authorizer: OwnerAuthorizer
    contracts: ContractRegistry


def _document(model) -> dict:
    return model.model_dump(mode="json")


def _canonical_body_document(model, document: dict) -> None:
    """Prove the document that actually leaves here re-enters its definition.

    This used to be a JSON Schema kept beside the binding. The schema said
    ``status: {"type": "object"}``, so the one field a consumer reads to decide
    who answers through a Body was outside it — a check that could not fail for
    the drift it existed to catch. The canonical type can fail for it, and is
    the same definition the consumers on the other side validate with, so this
    stays a runtime gate on every response rather than becoming a no-op.
    """

    type(model).model_validate(document)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, AuthorizationDenied):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, AuthorityUnavailable):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, (AuthorityRejected, Conflict)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (InvalidRequest, ValidationError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def create_kernel_router(*, services: KernelHttpServices) -> APIRouter:
    router = APIRouter(prefix="/api/kernel/v1", tags=["kernel-device-mount"])

    async def authorize_owner(
        *,
        action: str,
        authorization: str | None,
        owner_id_hint: str | None,
    ):
        return await services.authorizer.authorize(
            action=action,
            credential=authorization,
            owner_id_hint=owner_id_hint,
        )

    @router.post("/device-mounts", response_model=MutationResultWire)
    async def mount_device(
        payload: MountDeviceRequestWire,
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> MutationResultWire:
        try:
            services.contracts.validate(
                "device-mount/mount-request.schema.json", _document(payload)
            )
            owner_id = await authorize_owner(
                action="device-mount:write",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            result = await services.mount_device.execute(
                mount_request_to_domain(payload, owner_id=owner_id)
            )
            wire = commit_to_wire(result)
            services.contracts.validate(
                "device-mount/mutation-result.schema.json", _document(wire)
            )
            return wire
        except Exception as exc:  # mapped into stable interface errors
            _raise_http(exc)

    async def read_mount(
        *,
        device_id: str,
        resolve: bool,
        authorization: str | None,
        owner_id_hint: str | None,
    ) -> DeviceMountWire:
        owner_id = await authorize_owner(
            action="device-mount:read",
            authorization=authorization,
            owner_id_hint=owner_id_hint,
        )
        mount = (
            services.mounts.resolve(owner_id=owner_id, device_id=device_id)
            if resolve
            else services.mounts.get(owner_id=owner_id, device_id=device_id)
        )
        wire = mount_to_wire(mount)
        services.contracts.validate("device-mount/mount.schema.json", _document(wire))
        return wire

    @router.get("/device-mounts/devices/{device_id}", response_model=DeviceMountWire)
    async def get_mount(
        device_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> DeviceMountWire:
        try:
            return await read_mount(
                device_id=device_id,
                resolve=False,
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
        except Exception as exc:
            _raise_http(exc)

    @router.get("/device-mounts/resolve/{device_id}", response_model=DeviceMountWire)
    async def resolve_mount(
        device_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> DeviceMountWire:
        try:
            return await read_mount(
                device_id=device_id,
                resolve=True,
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
        except Exception as exc:
            _raise_http(exc)

    @router.get("/device-mounts", response_model=DeviceMountPageWire)
    async def list_mounts(
        active_only: bool = True,
        after_device_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=50, ge=1, le=100),
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> DeviceMountPageWire:
        try:
            owner_id = await authorize_owner(
                action="device-mount:read",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            mounts = services.mounts.list(
                owner_id=owner_id,
                active_only=active_only,
                after_device_id=after_device_id,
                limit=limit,
            )
            wire = DeviceMountPageWire(
                next_cursor=(mounts[-1].device_id if len(mounts) == limit else None),
                mounts=tuple(mount_to_wire(mount) for mount in mounts),
            )
            services.contracts.validate("device-mount/page.schema.json", _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    @router.get("/body-endpoints", response_model=BodyEndpointPageWire)
    async def list_body_endpoints(
        companion_id: str | None = Query(default=None, min_length=1, max_length=64),
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> BodyEndpointPageWire:
        """Every Body in this Owner's namespace, with what each is assigned to.

        Endpoints for inactive mounts are listed too, and say so: an assignment
        that outlived its device is exactly the thing an Owner needs to be able
        to see and clear.
        """

        try:
            owner_id = await authorize_owner(
                action="device-mount:read",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            wire = BodyEndpointPageWire(
                endpoints=tuple(
                    endpoint_to_wire(endpoint, assignment)
                    for endpoint, assignment in services.body_endpoints.list(
                        owner_id=owner_id, companion_id=companion_id
                    )
                )
            )
            _canonical_body_document(wire, _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    @router.get(
        "/body-endpoints/{body_endpoint_id}", response_model=BodyEndpointWire
    )
    async def get_body_endpoint(
        body_endpoint_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> BodyEndpointWire:
        try:
            owner_id = await authorize_owner(
                action="device-mount:read",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            endpoint = services.body_endpoints.resolve(
                owner_id=owner_id, body_endpoint_id=body_endpoint_id
            )
            wire = endpoint_to_wire(
                endpoint, services.body_endpoints.assignment(endpoint)
            )
            _canonical_body_document(wire, _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    @router.put(
        "/body-endpoints/{body_endpoint_id}/assignment",
        response_model=BodyEndpointWire,
    )
    async def replace_assignment(
        body_endpoint_id: str,
        payload: ReplaceAssignmentRequestWire,
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> BodyEndpointWire:
        """The canonical ``ReplaceAssignment``, stated as the Body's whole new state.

        A ``PUT`` because the request says what should be true, not what to do —
        which is what makes it safe to send again when the answer was lost. The
        compare-and-swap is on the assignment's own revision; a stale one whose
        end state already holds is answered as success, because the alternative
        makes re-reading the only safe move after any timeout.
        """

        try:
            services.contracts.validate(
                "body-mesh/replace-assignment-request.schema.json", _document(payload)
            )
            owner_id = await authorize_owner(
                action="device-mount:write",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            result = await services.replace_assignment.execute(
                replace_assignment_request_to_domain(
                    payload, body_endpoint_id=body_endpoint_id, owner_id=owner_id
                )
            )
            endpoint = services.body_endpoints.resolve(
                owner_id=owner_id, body_endpoint_id=body_endpoint_id
            )
            wire = endpoint_to_wire(endpoint, result.assignment)
            _canonical_body_document(wire, _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    @router.post(
        "/device-mounts/devices/{device_id}/unmount", response_model=MutationResultWire
    )
    async def unmount_device(
        device_id: str,
        payload: UnmountDeviceRequestWire,
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> MutationResultWire:
        try:
            services.contracts.validate(
                "device-mount/unmount-request.schema.json", _document(payload)
            )
            owner_id = await authorize_owner(
                action="device-mount:write",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            result = services.unmount_device.execute(
                unmount_request_to_domain(
                    payload, device_id=device_id, owner_id=owner_id
                )
            )
            wire = commit_to_wire(result)
            services.contracts.validate(
                "device-mount/mutation-result.schema.json", _document(wire)
            )
            return wire
        except Exception as exc:
            _raise_http(exc)

    @router.get("/audit/events", response_model=AuditPageWire)
    async def list_audit(
        after_position: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        authorization: str | None = Header(default=None, alias="Authorization"),
        owner_id_hint: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> AuditPageWire:
        try:
            owner_id = await authorize_owner(
                action="kernel-audit:read",
                authorization=authorization,
                owner_id_hint=owner_id_hint,
            )
            events = services.audit.list(
                after_position=after_position, limit=limit, owner_id=owner_id
            )
            wire = AuditPageWire(
                next_position=(events[-1].position if events else after_position),
                events=tuple(audit_to_wire(event) for event in events),
            )
            services.contracts.validate("audit/page.schema.json", _document(wire))
            return wire
        except Exception as exc:
            _raise_http(exc)

    return router
