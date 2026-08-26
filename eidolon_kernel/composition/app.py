"""Production and testable Kernel composition."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import FastAPI

from eidolon_kernel.adapters.companion.directory_routed import (
    DirectoryRoutedEidolonDataCompanionAuthority,
)
from eidolon_kernel.adapters.device_registry.directory_routed import (
    DirectoryRoutedHubDeviceAuthority,
)
from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.reconciliation.periodic import (
    PeriodicReconciliationWorker,
)
from eidolon_kernel.adapters.runtime import SystemClock
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalOwnerAuthorizer
from eidolon_kernel.adapters.service_directory.eidolond_http import (
    EidolondHttpServiceDirectory,
)
from eidolon_kernel.application.body_assignments import (
    BodyEndpoints,
    ReconcileAssignments,
    ReplaceAssignment,
)
from eidolon_kernel.application.device_mounts import (
    MountDevice,
    ReconcileClaimEvents,
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


@dataclass(frozen=True, slots=True)
class KernelReadinessChecks:
    device_mount_write: Callable[[], Awaitable[bool]]
    companion_attachment_write: Callable[[], Awaitable[bool]]


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
        replace_assignment=ReplaceAssignment(store, projection, companions, clock),
        unmount_device=UnmountDevice(store, projection, clock),
        mounts=DeviceMountQueries(projection),
        body_endpoints=BodyEndpoints(projection=projection, store=store),
        audit=AuditQueries(store),
        authorizer=authorizer,
        contracts=contracts,
    )


def create_http_app(
    *,
    services: KernelHttpServices,
    readiness_checks: KernelReadinessChecks | None = None,
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
        if readiness_checks is None:
            device_mount_write_available = True
            companion_attachment_write_available = True
        else:
            (
                device_mount_write_available,
                companion_attachment_write_available,
            ) = await asyncio.gather(
                readiness_checks.device_mount_write(),
                readiness_checks.companion_attachment_write(),
            )
        blockers = []
        if not device_mount_write_available:
            blockers.append("Hub device authority endpoint is not ready in eidolond")
        if not companion_attachment_write_available:
            blockers.append("Data companion authority endpoint is not ready in eidolond")
        return {
            "status": "ready" if not blockers else "degraded",
            "authoritative_store": "ready",
            "device_mount_write_available": device_mount_write_available,
            "companion_attachment_write_available": companion_attachment_write_available,
            "blockers": blockers,
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
    directory = EidolondHttpServiceDirectory(
        base_url=settings.system_directory.base_url,
        uds_path=settings.system_directory.uds_path,
        timeout_seconds=settings.system_directory.timeout_seconds,
        contracts=contracts,
    )
    devices = DirectoryRoutedHubDeviceAuthority(
        directory=directory,
        bearer_token=hub_token,
        timeout_seconds=settings.hub.timeout_seconds,
        contracts=contracts,
    )
    companions = DirectoryRoutedEidolonDataCompanionAuthority(
        directory=directory,
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
    reconciliation = ReconcileAssignments(
        store=store,
        projection=projection,
        companions=companions,
        clock=SystemClock(),
    )
    reconciliation_worker = PeriodicReconciliationWorker(
        reconciliation,
        interval_seconds=settings.reconciliation.interval_seconds,
    )
    claim_event_worker = PeriodicReconciliationWorker(
        ReconcileClaimEvents(
            store=store,
            projection=projection,
            devices=devices,
            clock=SystemClock(),
        ),
        interval_seconds=settings.hub.claim_event_poll_seconds,
        task_name="eidolon-kernel-claim-event-reconciliation",
    )

    def startup() -> None:
        claim_event_worker.start()
        reconciliation_worker.start()

    async def shutdown() -> None:
        await claim_event_worker.close()
        await reconciliation_worker.close()
        await devices.close()
        await companions.close()
        await directory.close()
        store.close()

    return KernelRuntime(
        app=create_http_app(
            services=services,
            readiness_checks=KernelReadinessChecks(
                device_mount_write=devices.is_available,
                companion_attachment_write=companions.is_available,
            ),
            startup=startup,
            shutdown=shutdown,
        ),
        store=store,
        projection=projection,
    )
