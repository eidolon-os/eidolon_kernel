from __future__ import annotations

import httpx
import pytest

from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.composition.app import create_http_app
from eidolon_system.domain.model import ServiceCatalog
from tests.system.support import FakeHostSupervisor, FakeReadinessProbe, FixedClock
from tests.system.test_domain import service


@pytest.mark.asyncio
async def test_versioned_http_api_resolves_and_mutates_without_owner_scope(tmp_path) -> None:
    host = FakeHostSupervisor()
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(
        catalog=ServiceCatalog((service("kernel", required=True), service("agent"))),
        store=store,
        directory=InMemoryServiceDirectory(),
        host=host,
        readiness=FakeReadinessProbe(),
        clock=FixedClock(),
    )
    await manager.initialize()
    await manager.reconcile()
    app = create_http_app(manager=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://system.test"
    ) as client:
        health = await client.get("/health")
        listing = await client.get("/api/system/v1/services")
        resolved = await client.get(
            "/api/system/v1/services/agent/endpoints/control.http"
        )
        disabled = await client.post(
            "/api/system/v1/services/agent/disable",
            json={
                "operation": "system.service.disable",
                "request_id": "req-disable-agent",
                "expected_revision": 1,
            },
        )
        missing = await client.get("/api/system/v1/services/missing")
        audit = await client.get("/api/system/v1/audit/events")
        wrong_operation = await client.post(
            "/api/system/v1/services/kernel/restart",
            json={
                "operation": "system.service.enable",
                "request_id": "req-wrong-operation",
                "expected_revision": 1,
            },
        )
        forbidden = await client.post(
            "/api/system/v1/services/kernel/disable",
            json={
                "operation": "system.service.disable",
                "request_id": "req-disable-kernel",
                "expected_revision": 1,
            },
        )

    assert health.json()["status"] == "ready"
    assert listing.json()["operation"] == "system.service-page"
    assert resolved.json()["endpoint_id"] == "control.http"
    assert disabled.status_code == 200
    assert disabled.json()["state"]["enabled"] is False
    assert missing.status_code == 404
    assert audit.json()["next_position"] == 1
    assert wrong_operation.status_code == 422
    assert forbidden.status_code == 409
    store.close()
