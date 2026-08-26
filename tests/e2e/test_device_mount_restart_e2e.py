from __future__ import annotations

import httpx
import pytest

from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalOwnerAuthorizer
from eidolon_kernel.composition.app import build_services, create_http_app
from eidolon_kernel.domain.body import body_endpoint_id
from tests.support import (
    DEVICE_1,
    FakeCompanionAuthority,
    FakeDeviceAuthority,
    MutableClock,
    headers,
    mount_body,
)


def e2e_app(store):
    services = build_services(
        store=store,
        projection=InMemoryMountProjection(),
        devices=FakeDeviceAuthority(),
        companions=FakeCompanionAuthority(),
        authorizer=TrustedLocalOwnerAuthorizer(),
        clock=MutableClock(),
    )
    return create_http_app(services=services)


@pytest.mark.asyncio
async def test_device_mount_survives_restart_then_is_assigned_and_unmounted(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first_store = SqliteMountStore(path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=e2e_app(first_store)),
        base_url="http://kernel.test",
    ) as client:
        response = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        assert response.status_code == 200
    first_store.close()

    restarted_store = SqliteMountStore(path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=e2e_app(restarted_store)),
        base_url="http://kernel.test",
    ) as client:
        restored = await client.get(
            f"/api/kernel/v1/device-mounts/resolve/{DEVICE_1}",
            headers=headers(),
        )
        assigned = await client.put(
            f"/api/kernel/v1/body-endpoints/{body_endpoint_id(DEVICE_1, 'body')}/assignment",
            headers=headers(),
            json={
                "operation": "body.replace-assignment",
                "request_id": "assign-1",
                "expected_assignment_revision": 0,
                "companion_id": "companion-2",
                "origin": "owner",
            },
        )
        unmounted = await client.post(
            f"/api/kernel/v1/device-mounts/devices/{DEVICE_1}/unmount",
            headers=headers(),
            json={
                "operation": "device.unmount",
                "request_id": "unmount-1",
                "expected_revision": 1,
            },
        )
        audit = await client.get(
            "/api/kernel/v1/audit/events",
            headers=headers(),
        )
        assert restored.json()["revision"] == 1
        assert assigned.json()["assignment"]["companion_id"] == "companion-2"
        assert assigned.json()["assignment"]["revision"] == 1
        assert unmounted.json()["mount"]["revision"] == 2
        assert [event["event_type"] for event in audit.json()["events"]] == [
            "eidolon.kernel.device-mounted.v1",
            "eidolon.kernel.body-assignment-created.v1",
            "eidolon.kernel.device-unmounted.v1",
        ]
    restarted_store.close()
