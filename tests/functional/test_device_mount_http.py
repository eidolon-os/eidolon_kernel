from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalOwnerAuthorizer
from eidolon_kernel.composition.app import (
    KernelReadinessChecks,
    build_services,
    create_http_app,
)
from eidolon_kernel.domain.body import body_endpoint_id
from tests.support import (
    FakeCompanionAuthority,
    FakeDeviceAuthority,
    MemoryStore,
    MutableClock,
    OfflineCompanionAuthority,
    headers,
    mount_body,
)

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")
_BODY_1 = body_endpoint_id(_DEVICE_1, "body")


def app(*, companions=None, devices=None, readiness_checks=None):
    services = build_services(
        store=MemoryStore(),
        projection=InMemoryMountProjection(),
        devices=devices or FakeDeviceAuthority(),
        companions=companions or FakeCompanionAuthority(),
        authorizer=TrustedLocalOwnerAuthorizer(),
        clock=MutableClock(),
    )
    return create_http_app(services=services, readiness_checks=readiness_checks)


@pytest.mark.asyncio
async def test_health_reports_mount_and_attachment_capabilities_independently() -> None:
    checks = KernelReadinessChecks(
        device_mount_write=AsyncMock(return_value=True),
        companion_attachment_write=AsyncMock(return_value=False),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(readiness_checks=checks)),
        base_url="http://kernel.test",
    ) as client:
        health = await client.get("/health")

    assert health.json() == {
        "status": "degraded",
        "authoritative_store": "ready",
        "device_mount_write_available": True,
        "companion_attachment_write_available": False,
        "blockers": ["Data companion authority endpoint is not ready in eidolond"],
    }


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
            f"/api/kernel/v1/device-mounts/resolve/{_DEVICE_1}",
            headers=headers(),
        )
        unassigned_page = await client.get(
            "/api/kernel/v1/body-endpoints",
            params={"companion_id": "companion-1"},
            headers=headers(),
        )
        assigned = await client.put(
            f"/api/kernel/v1/body-endpoints/{_BODY_1}/assignment",
            headers=headers(),
            json={
                "operation": "body.replace-assignment",
                "request_id": "assign-1",
                "expected_assignment_revision": 0,
                "companion_id": "companion-1",
                "origin": "owner",
            },
        )
        current = await client.get(
            f"/api/kernel/v1/device-mounts/devices/{_DEVICE_1}", headers=headers()
        )
        assigned_page = await client.get(
            "/api/kernel/v1/body-endpoints",
            params={"companion_id": "companion-1"},
            headers=headers(),
        )
        assert resolved.status_code == unassigned_page.status_code == 200
        assert unassigned_page.json()["endpoints"] == []
        assert assigned.status_code == current.status_code == assigned_page.status_code == 200
        # Pointing a Body at an Eidolon does not move the mount's revision: two
        # facts, two compare-and-swap tokens.
        assert current.json()["revision"] == 1
        assert assigned.json()["assignment"]["revision"] == 1
        assert assigned.json()["assignment"]["status"]["conditions"] == ["Realized"]
        assert assigned_page.json()["endpoints"][0]["device_id"] == _DEVICE_1

        cleared = await client.put(
            f"/api/kernel/v1/body-endpoints/{_BODY_1}/assignment",
            headers=headers(),
            json={
                "operation": "body.replace-assignment",
                "request_id": "clear-1",
                "expected_assignment_revision": 1,
                "companion_id": None,
                "origin": "owner",
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["assignment"]["companion_id"] is None
        assert cleared.json()["assignment"]["selection_provenance"] == "user_cleared"
        assert cleared.json()["assignment"]["revision"] == 2

        unmounted = await client.post(
            f"/api/kernel/v1/device-mounts/devices/{_DEVICE_1}/unmount",
            headers=headers(),
            json={
                "operation": "device.unmount",
                "request_id": "unmount-1",
                "expected_revision": 1,
            },
        )
        assert unmounted.status_code == 200
        assert unmounted.json()["mount"]["active"] is False
        assert unmounted.json()["mount"]["revision"] == 2

        no_resolution = await client.get(
            f"/api/kernel/v1/device-mounts/resolve/{_DEVICE_1}",
            headers=headers(),
        )
        inactive = await client.get(
            f"/api/kernel/v1/device-mounts/devices/{_DEVICE_1}",
            headers=headers(),
        )
        audit = await client.get(
            "/api/kernel/v1/audit/events",
            params={"after_position": 0},
            headers=headers(),
        )
        assert no_resolution.status_code == 404
        assert inactive.status_code == 200 and inactive.json()["active"] is False
        assert [event["position"] for event in audit.json()["events"]] == [1, 2, 3, 4]
        assert [event["subject"] for event in audit.json()["events"]] == [
            "device-mount",
            "body-assignment",
            "body-assignment",
            "device-mount",
        ]


@pytest.mark.asyncio
async def test_http_boundary_fails_closed_for_identity_authorities_and_cas() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(companions=OfflineCompanionAuthority())),
        base_url="http://kernel.test",
    ) as client:
        missing_identity = await client.post("/api/kernel/v1/device-mounts", json=mount_body())
        mounted = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        unavailable = await client.put(
            f"/api/kernel/v1/body-endpoints/{_BODY_1}/assignment",
            headers=headers(),
            json={
                "operation": "body.replace-assignment",
                "request_id": "assign",
                "expected_assignment_revision": 0,
                "companion_id": "companion-1",
                "origin": "owner",
            },
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
        assert mounted.status_code == 200
        assert unavailable.status_code == 503
        assert invalid.status_code == 422
        assert coerced.status_code == 422
        assert missing.status_code == 422


@pytest.mark.asyncio
async def test_http_hides_other_owner_mounts_and_rejects_revoked_hub_device() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app()), base_url="http://kernel.test"
    ) as client:
        await client.post("/api/kernel/v1/device-mounts", headers=headers(), json=mount_body())
        hidden = await client.get(
            f"/api/kernel/v1/device-mounts/devices/{_DEVICE_1}",
            headers=headers("owner-2"),
        )
        unresolved = await client.get(
            f"/api/kernel/v1/device-mounts/resolve/{_DEVICE_1}",
            headers=headers("owner-2"),
        )
        empty_list = await client.get("/api/kernel/v1/device-mounts", headers=headers("owner-2"))
        empty_audit = await client.get("/api/kernel/v1/audit/events", headers=headers("owner-2"))
        denied_unmount = await client.post(
            f"/api/kernel/v1/device-mounts/devices/{_DEVICE_1}/unmount",
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
        transport=httpx.ASGITransport(app=app(devices=FakeDeviceAuthority(status="revoked"))),
        base_url="http://kernel.test",
    ) as client:
        rejected = await client.post(
            "/api/kernel/v1/device-mounts", headers=headers(), json=mount_body()
        )
        assert rejected.status_code == 409
