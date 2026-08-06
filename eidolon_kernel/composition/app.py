"""Production and testable Kernel composition."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import FastAPI

from eidolon_kernel.adapters.companion.eidolon_data_http import (
    EidolonDataHttpCompanionAuthority,
)
from eidolon_kernel.adapters.device_registry.hub_http import HubHttpDeviceAuthority
from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.reconciliation.periodic import (
    PeriodicReconciliationWorker,
)
from eidolon_kernel.adapters.runtime import SystemClock
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalOwnerAuthorizer
from eidolon_kernel.application.device_mounts import (
    AttachCompanion,
    DetachCompanion,
    MountDevice,
    ReconcileMountPrerequisites,
    UnmountDevice,
)
from eidolon_kernel.application.queries import AuditQueries, DeviceMountQueries
from eidolon_kernel.config import (
    KernelSettings,
    load_companion_authority_token,
    load_hub_token,
    load_settings,
)
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.interfaces.http.router import KernelHttpServices, create_kernel_router
from eidolon_kernel.ports.authorities import (
    CompanionAuthority,
    DeviceAuthority,
    OwnerAuthorizer,
)
from eidolon_kernel.ports.runtime import Clock, MountProjection, MountStore


@dataclass(slots=True)
class KernelRuntime:
    app: FastAPI
    store: MountStore
    projection: MountProjection


def build_services(
    *,
    store: MountStore,
    projection: MountProjection,
    devices: DeviceAuthority,
    companions: CompanionAuthority,
    authorizer: OwnerAuthorizer,
    clock: Clock,
    contracts: ContractRegistry | None = None,
) -> KernelHttpServices:
    contracts = contracts or ContractRegistry()
    projection.rebuild(store.list_all())
    return KernelHttpServices(
        mount_device=MountDevice(store, projection, devices, clock),
        attach_companion=AttachCompanion(store, projection, companions, clock),
        detach_companion=DetachCompanion(store, projection, clock),
        unmount_device=UnmountDevice(store, projection, clock),
        mounts=DeviceMountQueries(projection),
        audit=AuditQueries(store),
        authorizer=authorizer,
        contracts=contracts,
    )


def create_http_app(
    *,
    services: KernelHttpServices,
    write_available: bool = True,
    blocker: str | None = None,
    startup: Callable[[], None] | None = None,
    shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if startup is not None:
            startup()
        try:
            yield
        finally:
            if shutdown is not None:
                await shutdown()

    app = FastAPI(
        title="Eidolon Sovereign Kernel",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(create_kernel_router(services=services))

    @app.get("/health", tags=["operations"])
    async def health() -> dict[str, object]:
        return {
            "status": "ready" if write_available else "degraded",
            "authoritative_store": "ready",
            "device_mount_write_available": write_available,
            "blocker": blocker,
        }

    return app


def create_production_app(settings: KernelSettings | None = None) -> KernelRuntime:
    settings = settings or load_settings()
    if not settings.deployment.trusted_local_ingress:
        raise RuntimeError("V1 has no configured Kernel identity authorizer")
    hub_token = load_hub_token()
    companion_token = load_companion_authority_token()
    store = SqliteMountStore(settings.persistence.path)
    projection = InMemoryMountProjection()
    contracts = ContractRegistry()
    devices = HubHttpDeviceAuthority(
        base_url=settings.hub.base_url,
        bearer_token=hub_token,
        timeout_seconds=settings.hub.timeout_seconds,
        contracts=contracts,
    )
    companions = EidolonDataHttpCompanionAuthority(
        base_url=settings.companion_authority.base_url,
        bearer_token=companion_token,
        timeout_seconds=settings.companion_authority.timeout_seconds,
        contracts=contracts,
    )
    services = build_services(
        store=store,
        projection=projection,
        devices=devices,
        companions=companions,
        authorizer=TrustedLocalOwnerAuthorizer(),
        clock=SystemClock(),
        contracts=contracts,
    )
    reconciliation = ReconcileMountPrerequisites(
        store=store,
        projection=projection,
        devices=devices,
        companions=companions,
        clock=SystemClock(),
    )
    reconciliation_worker = PeriodicReconciliationWorker(
        reconciliation,
        interval_seconds=settings.reconciliation.interval_seconds,
    )

    async def shutdown() -> None:
        await reconciliation_worker.close()
        await devices.close()
        await companions.close()
        store.close()

    return KernelRuntime(
        app=create_http_app(
            services=services,
            startup=reconciliation_worker.start,
            shutdown=shutdown,
        ),
        store=store,
        projection=projection,
    )
