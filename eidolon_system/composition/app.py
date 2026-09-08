"""Independent eidolond process composition."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.host.runner import SubprocessCommandRunner
from eidolon_system.adapters.host.supervisord import SupervisordHostSupervisor
from eidolon_system.adapters.host.systemd import SystemdHostSupervisor
from eidolon_system.adapters.host.unit_applier import ApplierUnitMutator
from eidolon_system.adapters.host_profile import declared_host_capabilities
from eidolon_system.adapters.manifest.yaml_file import YamlServiceManifest
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.adapters.readiness.http import HttpReadinessProbe
from eidolon_system.adapters.reconciliation.periodic import PeriodicServiceReconciler
from eidolon_system.adapters.runtime import SystemClock
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.config import SystemSettings, load_settings, selected_host_driver
from eidolon_system.contracts.registry import SystemContractRegistry
from eidolon_system.adapters.host.vitals import LinuxHostVitals
from eidolon_system.ports.runtime import HostVitalsReader
from eidolon_system.interfaces.http.router import create_system_router
from eidolon_system.ports.runtime import SystemStateStore


@dataclass(slots=True)
class SystemRuntime:
    app: FastAPI
    manager: ServiceManager
    store: SystemStateStore


def create_http_app(
    *,
    manager: ServiceManager,
    contracts: SystemContractRegistry | None = None,
    # Injected so a test of the HTTP surface needs a fake, not a Linux box.
    vitals: HostVitalsReader | None = None,
    startup=None,
    shutdown=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if startup is not None:
            await startup()
        try:
            yield
        finally:
            if shutdown is not None:
                await shutdown()

    app = FastAPI(title="Eidolon System Manager", version="1.0.0", lifespan=lifespan)
    app.include_router(
        create_system_router(
            manager=manager,
            contracts=contracts or SystemContractRegistry(),
            vitals=vitals or LinuxHostVitals(),
        )
    )

    @app.get("/health", tags=["operations"])
    async def health() -> dict[str, object]:
        return {
            "status": "ready",
            "scope": "machine",
            "owner_scoped": False,
            "host_driver": manager.host.driver_name,
        }

    return app


def create_production_app(settings: SystemSettings | None = None) -> SystemRuntime:
    settings = settings or load_settings()
    contracts = SystemContractRegistry()
    catalog = YamlServiceManifest(
        settings.manifest.path,
        contracts=contracts,
        capabilities=declared_host_capabilities(),
    ).load()
    store = SqliteSystemStateStore(settings.persistence.path)
    runner = SubprocessCommandRunner(
        timeout_seconds=settings.host.command_timeout_seconds
    )
    driver = selected_host_driver(settings.host)
    if driver == "systemd":
        applier_socket = settings.host.unit_applier_socket
        host = SystemdHostSupervisor(
            runner=runner,
            systemctl=settings.host.systemctl,
            mutator=(
                None
                if applier_socket is None
                else ApplierUnitMutator(
                    applier_socket,
                    timeout_seconds=settings.host.command_timeout_seconds,
                )
            ),
        )
    else:
        if settings.host.supervisor_config is None:
            store.close()
            raise RuntimeError("supervisord adapter requires host.supervisor_config")
        host = SupervisordHostSupervisor(
            runner=runner,
            supervisorctl=settings.host.supervisorctl,
            config_path=settings.host.supervisor_config,
        )
    readiness = HttpReadinessProbe(
        timeout_seconds=settings.reconciliation.readiness_timeout_seconds
    )
    manager = ServiceManager(
        catalog=catalog,
        store=store,
        directory=InMemoryServiceDirectory(),
        host=host,
        readiness=readiness,
        clock=SystemClock(),
    )
    reconciler = PeriodicServiceReconciler(
        manager, interval_seconds=settings.reconciliation.interval_seconds
    )

    async def startup() -> None:
        await manager.initialize()
        await manager.reconcile()
        reconciler.start()

    async def shutdown() -> None:
        await reconciler.close()
        await readiness.close()
        store.close()

    return SystemRuntime(
        app=create_http_app(
            manager=manager,
            contracts=contracts,
            startup=startup,
            shutdown=shutdown,
        ),
        manager=manager,
        store=store,
    )
