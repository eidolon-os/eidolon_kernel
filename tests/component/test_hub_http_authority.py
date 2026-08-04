from __future__ import annotations

import httpx
import pytest

from eidolon_kernel.adapters.device_registry.hub_http import HubHttpDeviceAuthority
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityRejected, AuthorityUnavailable


def document(**overrides):
    value = {
        "operation": "device.directory-entry",
        "device_id": "device one",
        "owner_scope": "owner one",
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
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "arguments",
    [
        {"base_url": "file:///hub", "bearer_token": "token"},
        {"base_url": "https://hub.example", "bearer_token": "  "},
    ],
)
def test_hub_adapter_rejects_invalid_configuration(arguments) -> None:
    with pytest.raises(ValueError):
        HubHttpDeviceAuthority(contracts=ContractRegistry(), **arguments)


@pytest.mark.asyncio
async def test_hub_adapter_consumes_only_owner_scoped_device_get() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == (
            b"/api/device-management/v1/owners/owner%20one/devices/device%20one"
        )
        assert request.headers["Authorization"] == "Bearer hub-issued-token"
        return httpx.Response(200, json=document())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HubHttpDeviceAuthority(
        base_url="https://hub.example/",
        bearer_token="hub-issued-token",
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        admission = await adapter.get_device(owner_id="owner one", device_id="device one")
        assert admission.owner_id == "owner one"
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
        bearer_token="token",
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
        bearer_token="token",
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
        bearer_token="token",
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(AuthorityUnavailable, match="violated"):
        await adapter.get_device(owner_id="owner", device_id="device")
    await client.aclose()
