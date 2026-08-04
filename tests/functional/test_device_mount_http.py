from __future__ import annotations

import httpx
import pytest

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalOwnerAuthorizer
from eidolon_kernel.composition.app import build_services, create_http_app
from tests.support import (
    FakeCompanionAuthority,
    FakeDeviceAuthority,
    MemoryStore,
    MutableClock,
    OfflineCompanionAuthority,
    headers,
    mount_body,
)


def app(*, companions=None, devices=None):
    services = build_services(
        store=MemoryStore(),
        projection=InMemoryMountProjection(),
        devices=devices or FakeDeviceAuthority(),
        companions=companions or FakeCompanionAuthority(),
        authorizer=TrustedLocalOwnerAuthorizer(),
        clock=MutableClock(),
    )
    return create_http_app(services=services)


@pytest.mark.asyncio
async def test_http_mount_resolve_list_unmount_and_audit_flow() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app()), base_url="http://kernel.test"
    ) as client:
        health = await client.get("/health")
        assert health.json()["status"] == "ready"
        mounted = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        assert mounted.status_code == 200, mounted.text
        assert mounted.json()["mount"]["revision"] == 1

        replay = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        assert replay.json()["replayed"] is True
        assert replay.json()["audit_position"] == 1

        resolved = await client.get(
            "/api/kernel/v1/device-mounts/resolve/device-1",
            headers=headers(),
        )
        current = await client.get(
            "/api/kernel/v1/device-mounts/devices/device-1",
            headers=headers(),
        )
        page = await client.get(
            "/api/kernel/v1/device-mounts",
            params={"companion_id": "companion-1"},
            headers=headers(),
        )
        assert resolved.status_code == current.status_code == page.status_code == 200
        assert page.json()["mounts"][0]["device_id"] == "device-1"

        unmounted = await client.post(
            "/api/kernel/v1/device-mounts/devices/device-1/unmount",
            headers=headers(),
            json={
                "operation": "device.unmount",
                "request_id": "unmount-1",
                "expected_revision": 1,
            },
        )
        assert unmounted.status_code == 200
        assert unmounted.json()["mount"] == {
            **current.json(),
            "revision": 2,
            "updated_at": "2026-08-04T08:00:01Z",
            "request_id": "unmount-1",
            "fingerprint": unmounted.json()["mount"]["fingerprint"],
            "active": False,
        }

        no_resolution = await client.get(
            "/api/kernel/v1/device-mounts/resolve/device-1",
            headers=headers(),
        )
        inactive = await client.get(
            "/api/kernel/v1/device-mounts/devices/device-1",
            headers=headers(),
        )
        audit = await client.get(
            "/api/kernel/v1/audit/events",
            params={"after_position": 0},
            headers=headers(),
        )
        assert no_resolution.status_code == 404
        assert inactive.status_code == 200 and inactive.json()["active"] is False
        assert [event["position"] for event in audit.json()["events"]] == [1, 2]


@pytest.mark.asyncio
async def test_http_boundary_fails_closed_for_identity_authorities_and_cas() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app(companions=OfflineCompanionAuthority())
        ),
        base_url="http://kernel.test",
    ) as client:
        missing_identity = await client.post(
            "/api/kernel/v1/device-mounts", json=mount_body()
        )
        unavailable = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        invalid = await client.post(
            "/api/kernel/v1/device-mounts",
            headers=headers(),
            json=mount_body(expected_revision=-1),
        )
        coerced = await client.post(
            "/api/kernel/v1/device-mounts",
            headers=headers(),
            json=mount_body(expected_revision="0"),
        )
        missing_required = mount_body()
        del missing_required["replace_existing"]
        missing = await client.post(
            "/api/kernel/v1/device-mounts",
            headers=headers(),
            json=missing_required,
        )
        assert missing_identity.status_code == 403
        assert unavailable.status_code == 503
        assert invalid.status_code == 422
        assert coerced.status_code == 422
        assert missing.status_code == 422


@pytest.mark.asyncio
async def test_http_hides_other_owner_mounts_and_rejects_revoked_hub_device() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app()), base_url="http://kernel.test"
    ) as client:
        await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        hidden = await client.get(
            "/api/kernel/v1/device-mounts/devices/device-1",
            headers=headers("owner-2"),
        )
        unresolved = await client.get(
            "/api/kernel/v1/device-mounts/resolve/device-1",
            headers=headers("owner-2"),
        )
        empty_list = await client.get(
            "/api/kernel/v1/device-mounts", headers=headers("owner-2")
        )
        empty_audit = await client.get(
            "/api/kernel/v1/audit/events", headers=headers("owner-2")
        )
        denied_unmount = await client.post(
            "/api/kernel/v1/device-mounts/devices/device-1/unmount",
            headers=headers("owner-2"),
            json={
                "operation": "device.unmount",
                "request_id": "owner-2-unmount",
                "expected_revision": 1,
            },
        )
        denied_remount = await client.post(
            "/api/kernel/v1/device-mounts",
            headers=headers("owner-2"),
            json=mount_body(
                request_id="owner-2-remount",
                companion_id="companion-2",
                expected_revision=1,
                replace_existing=True,
            ),
        )
        assert (
            hidden.status_code
            == unresolved.status_code
            == denied_unmount.status_code
            == denied_remount.status_code
            == 404
        )
        assert empty_list.json()["mounts"] == []
        assert empty_audit.json()["events"] == []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app(devices=FakeDeviceAuthority(status="revoked"))
        ),
        base_url="http://kernel.test",
    ) as client:
        rejected = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        assert rejected.status_code == 409
