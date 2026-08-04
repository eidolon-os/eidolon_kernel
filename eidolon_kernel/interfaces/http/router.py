"""Kernel V1 Device Mount and audit HTTP API."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Header, HTTPException, Query
from jsonschema import ValidationError

from eidolon_kernel.application.device_mounts import MountDevice, UnmountDevice
from eidolon_kernel.application.queries import AuditQueries, DeviceMountQueries
from eidolon_kernel.contracts.bindings import (
    AuditPageWire,
    DeviceMountPageWire,
    DeviceMountWire,
    MountDeviceRequestWire,
    MutationResultWire,
    UnmountDeviceRequestWire,
)
from eidolon_kernel.contracts.mappers import (
    audit_to_wire,
    commit_to_wire,
    mount_request_to_domain,
    mount_to_wire,
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
from eidolon_kernel.ports.authorities import ActorAuthorizer


@dataclass(frozen=True, slots=True)
class KernelHttpServices:
    mount_device: MountDevice
    unmount_device: UnmountDevice
    mounts: DeviceMountQueries
    audit: AuditQueries
    authorizer: ActorAuthorizer
    contracts: ContractRegistry


def _document(model) -> dict:
    return model.model_dump(mode="json")


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

    async def actor_for(
        *,
        action: str,
        owner_id: str,
        authorization: str | None,
        actor_id: str | None,
        actor_owner: str | None,
    ):
        return await services.authorizer.authorize(
            action=action,
            owner_id=owner_id,
            credential=authorization,
            actor_id_hint=actor_id,
            actor_owner_hint=actor_owner,
        )

    @router.post("/device-mounts", response_model=MutationResultWire)
    async def mount_device(
        payload: MountDeviceRequestWire,
        authorization: str | None = Header(default=None, alias="Authorization"),
        actor_id: str | None = Header(default=None, alias="X-Eidolon-Actor"),
        actor_owner: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> MutationResultWire:
        try:
            services.contracts.validate(
                "device-mount/mount-request.schema.json", _document(payload)
            )
            actor = await actor_for(
                action="device-mount:write",
                owner_id=payload.owner_id,
                authorization=authorization,
                actor_id=actor_id,
                actor_owner=actor_owner,
            )
            result = await services.mount_device.execute(
                mount_request_to_domain(payload), actor=actor
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
        owner_id: str,
        resolve: bool,
        authorization: str | None,
        actor_id: str | None,
        actor_owner: str | None,
    ) -> DeviceMountWire:
        await actor_for(
            action="device-mount:read",
            owner_id=owner_id,
            authorization=authorization,
            actor_id=actor_id,
            actor_owner=actor_owner,
        )
        mount = services.mounts.resolve(device_id) if resolve else services.mounts.get(device_id)
        if mount.owner_id != owner_id:
            raise NotFound("device mount not found in owner scope")
        wire = mount_to_wire(mount)
        services.contracts.validate("device-mount/mount.schema.json", _document(wire))
        return wire

    @router.get("/device-mounts/devices/{device_id}", response_model=DeviceMountWire)
    async def get_mount(
        device_id: str,
        owner_id: str = Query(min_length=1, max_length=64),
        authorization: str | None = Header(default=None, alias="Authorization"),
        actor_id: str | None = Header(default=None, alias="X-Eidolon-Actor"),
        actor_owner: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> DeviceMountWire:
        try:
            return await read_mount(
                device_id=device_id,
                owner_id=owner_id,
                resolve=False,
                authorization=authorization,
                actor_id=actor_id,
                actor_owner=actor_owner,
            )
        except Exception as exc:
            _raise_http(exc)

    @router.get("/device-mounts/resolve/{device_id}", response_model=DeviceMountWire)
    async def resolve_mount(
        device_id: str,
        owner_id: str = Query(min_length=1, max_length=64),
        authorization: str | None = Header(default=None, alias="Authorization"),
        actor_id: str | None = Header(default=None, alias="X-Eidolon-Actor"),
        actor_owner: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> DeviceMountWire:
        try:
            return await read_mount(
                device_id=device_id,
                owner_id=owner_id,
                resolve=True,
                authorization=authorization,
                actor_id=actor_id,
                actor_owner=actor_owner,
            )
        except Exception as exc:
            _raise_http(exc)

    @router.get("/device-mounts", response_model=DeviceMountPageWire)
    async def list_mounts(
        owner_id: str = Query(min_length=1, max_length=64),
        companion_id: str | None = Query(default=None, min_length=1, max_length=64),
        active_only: bool = True,
        after_device_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=50, ge=1, le=100),
        authorization: str | None = Header(default=None, alias="Authorization"),
        actor_id: str | None = Header(default=None, alias="X-Eidolon-Actor"),
        actor_owner: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> DeviceMountPageWire:
        try:
            await actor_for(
                action="device-mount:read",
                owner_id=owner_id,
                authorization=authorization,
                actor_id=actor_id,
                actor_owner=actor_owner,
            )
            mounts = services.mounts.list(
                owner_id=owner_id,
                companion_id=companion_id,
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

    @router.post(
        "/device-mounts/devices/{device_id}/unmount", response_model=MutationResultWire
    )
    async def unmount_device(
        device_id: str,
        payload: UnmountDeviceRequestWire,
        authorization: str | None = Header(default=None, alias="Authorization"),
        actor_id: str | None = Header(default=None, alias="X-Eidolon-Actor"),
        actor_owner: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> MutationResultWire:
        try:
            services.contracts.validate(
                "device-mount/unmount-request.schema.json", _document(payload)
            )
            actor = await actor_for(
                action="device-mount:write",
                owner_id=payload.owner_id,
                authorization=authorization,
                actor_id=actor_id,
                actor_owner=actor_owner,
            )
            result = services.unmount_device.execute(
                unmount_request_to_domain(payload, device_id=device_id), actor=actor
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
        owner_id: str = Query(min_length=1, max_length=64),
        after_position: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        authorization: str | None = Header(default=None, alias="Authorization"),
        actor_id: str | None = Header(default=None, alias="X-Eidolon-Actor"),
        actor_owner: str | None = Header(default=None, alias="X-Eidolon-Owner"),
    ) -> AuditPageWire:
        try:
            await actor_for(
                action="kernel-audit:read",
                owner_id=owner_id,
                authorization=authorization,
                actor_id=actor_id,
                actor_owner=actor_owner,
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
