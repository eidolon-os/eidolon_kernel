from __future__ import annotations

import httpx
import pytest
from eidolon_sdk.device_foundation.v1 import ClaimEventCursor

from eidolon_kernel.adapters.device_registry.hub_http import HubHttpDeviceAuthority
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    AuthorityUnavailable,
    ClaimCursorGap,
)

HUB_READER_TOKEN = "hub-device-registry-reader-token-0001"


def claim_event_page(**event_overrides):
    event = {
        "specversion": "1.0",
        "id": "claim-event-1",
        "source": "urn:eidolon:authority:admission",
        "type": "live.eidolon.device.claim-activated.v1",
        "subject": "device-instances/device-one",
        "time": "2026-08-04T08:00:00Z",
        "datacontenttype": "application/json",
        "dataschema": "https://contracts.eidolon.live/device-foundation/v1/events/claim-activated-data.schema.json",
        "audience": "eidolon-claim-consumers",
        "ownerdomainid": "owner-one",
        "aggregaterev": 5,
        "correlationid": "intent-one",
        "causationid": "grant-ack-one",
        "data": {
            "device_ref": {
                "device_instance_id": "device-one",
                "owner_domain_id": "owner-one",
                "owner_domain_generation": 1,
                "claim_generation": 1,
                "trust_epoch": 1,
            },
            "manifest_ref": {
                "manifest_id": "manifest-one",
                "revision": 1,
                "digest": "sha256:" + "a" * 64,
            },
            "approval_decision_id": "decision-one",
            "activated_at": "2026-08-04T08:00:00Z",
        },
    }
    event.update(event_overrides)
    return {
        "stream_id": "admission-claims-v1",
        "requested_after": {
            "stream_id": "admission-claims-v1",
            "stream_position": 0,
        },
        "events": [{"stream_position": 1, "event": event}],
        "next_cursor": {
            "stream_id": "admission-claims-v1",
            "stream_position": 1,
        },
        "high_watermark": 1,
        "observed_at": "2026-08-04T08:00:01Z",
    }


def document(**overrides):
    value = {
        "operation": "device.directory-entry",
        "device_id": "device-one",
        "owner_scope": "owner-one",
        "display_name": "Desk",
        "device_kind": "desktop",
        "manifest": {
            "schema_version": 1,
            "title": "Desk",
            "properties": [],
            "actions": [],
            "events": [],
            "media": [],
        },
        "manifest_revision": "sha256:manifest",
        "lifecycle_state": "approved",
        "enrolled_at": "2026-08-04T08:00:00Z",
        "updated_at": "2026-08-04T08:00:00Z",
        "device_ref": {
            "device_instance_id": "device-one",
            "owner_domain_id": "owner-one",
            "owner_domain_generation": 1,
            "claim_generation": 1,
            "trust_epoch": 1,
            "accepted_manifest_digest": "sha256:manifest",
        },
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "arguments",
    [
        {"base_url": "file:///hub", "bearer_token": HUB_READER_TOKEN},
        {"base_url": "https://hub.example", "bearer_token": "  "},
        {"base_url": "https://hub.example", "bearer_token": "short"},
    ],
)
def test_hub_adapter_rejects_invalid_configuration(arguments) -> None:
    with pytest.raises(ValueError):
        HubHttpDeviceAuthority(contracts=ContractRegistry(), **arguments)


@pytest.mark.asyncio
async def test_hub_adapter_consumes_only_owner_scoped_device_get() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == (
            b"/api/device-management/v1/owners/owner-one/devices/device-one"
        )
        assert request.headers["Authorization"] == f"Bearer {HUB_READER_TOKEN}"
        return httpx.Response(200, json=document())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example/",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        admission = await adapter.get_device(owner_id="owner-one", device_id="device-one")
        assert admission.owner_id == "owner-one"
        assert admission.status == "approved"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {}, AuthorityRejected),
        (403, {}, AuthorityUnavailable),
        (503, {}, AuthorityUnavailable),
        (202, {}, AuthorityUnavailable),
        (200, {"unexpected": True}, AuthorityUnavailable),
    ],
)
async def test_hub_adapter_maps_policy_and_contract_failures(status, body, error) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    )
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        with pytest.raises(error):
            await adapter.get_device(owner_id="owner", device_id="device")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hub_adapter_maps_transport_and_non_object_json_failures() -> None:
    async def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(disconnected))
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(AuthorityUnavailable, match="unreachable"):
        await adapter.get_device(owner_id="owner", device_id="device")
    await client.aclose()

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    )
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(AuthorityUnavailable, match="violated"):
        await adapter.get_device(owner_id="owner", device_id="device")
    await client.aclose()


@pytest.mark.asyncio
async def test_claim_consumer_calls_real_canonical_hub_target_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/admission/v1/claim-events"
        assert dict(request.url.params) == {
            "after_stream_position": "0",
            "limit": "100",
        }
        assert request.headers["Authorization"] == f"Bearer {HUB_READER_TOKEN}"
        return httpx.Response(200, json=claim_event_page())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        page = await adapter.list_claim_events(
            cursor=ClaimEventCursor(stream_position=0), limit=100
        )
        assert page.next_cursor.stream_position == 1
        assert page.events[0].event.data.device_ref.claim_generation == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"source": "urn:eidolon:authority:other"}, "canonical SDK"),
        ({"audience": "other-consumer"}, "canonical SDK"),
        ({"subject": "device-instances/device-other"}, "canonical SDK"),
        ({"dataschema": "https://contracts.eidolon.live/wrong.json"}, "canonical SDK"),
        ({"type": "live.eidolon.device.claim-state-changed.v1"}, "canonical SDK"),
        ({"ownerdomainid": "owner-other"}, "canonical SDK"),
    ],
)
async def test_claim_consumer_fails_closed_on_event_metadata(overrides, match) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=claim_event_page(**overrides))
        )
    )
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        with pytest.raises(AuthorityUnavailable, match=match):
            await adapter.list_claim_events(
                cursor=ClaimEventCursor(stream_position=0), limit=100
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_claim_cursor_gap_is_distinct_fail_closed_and_recoverable() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                409,
                json={
                    "code": "CURSOR_GAP",
                    "category": "conflict",
                    "retryable": False,
                    "authority": "admission",
                    "command_id": None,
                    "resource_ref": None,
                    "current_revision": None,
                    "current_generation": None,
                    "retry_after_ms": None,
                    "detail": "cursor is ahead of the durable stream",
                    "incident_id": "incident-one",
                },
            )
        return httpx.Response(200, json=claim_event_page())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        cursor = ClaimEventCursor(stream_position=0)
        with pytest.raises(ClaimCursorGap, match="ahead"):
            await adapter.list_claim_events(cursor=cursor, limit=100)
        recovered = await adapter.list_claim_events(cursor=cursor, limit=100)
        assert recovered.next_cursor.stream_position == 1
    finally:
        await client.aclose()
