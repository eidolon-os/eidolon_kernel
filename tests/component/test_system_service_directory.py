from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from eidolon_kernel.adapters.companion.directory_routed import (
    DirectoryRoutedEidolonDataCompanionAuthority,
)
from eidolon_kernel.adapters.device_registry.directory_routed import (
    DirectoryRoutedHubDeviceAuthority,
)
from eidolon_kernel.adapters.service_directory.eidolond_http import (
    EidolondHttpServiceDirectory,
)
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityUnavailable
from eidolon_kernel.ports.system_services import (
    ResolvedServiceEndpoint,
    ServiceDirectoryUnavailable,
)
from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.composition.app import create_http_app as create_system_http_app
from eidolon_system.domain.model import ServiceCatalog, ServiceDefinition, ServiceEndpoint
from tests.component.test_companion_http_authority import (
    TOKEN as COMPANION_TOKEN,
)
from tests.component.test_companion_http_authority import (
    document as companion_document,
)
from tests.component.test_hub_http_authority import HUB_READER_TOKEN, document
from tests.system.support import FakeHostSupervisor, FakeReadinessProbe, FixedClock

HUB_CONTRACT = "eidolon.hub.device-directory.v1"
DATA_COMPANION_CONTRACT = "https://eidolon.dev/data/contracts/v1/companion/identity.schema.json"


def endpoint_document(**overrides) -> dict:
    value = {
        "operation": "system.service-endpoint",
        "service_id": "hub",
        "endpoint_id": "device-authority.http",
        "protocol": "http",
        "address": "http://127.0.0.1:8082",
        "contract": HUB_CONTRACT,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_directory_adapter_consumes_exact_resolve_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == (
            b"/api/system/v1/services/hub/endpoints/device-authority.http"
        )
        return httpx.Response(200, json=endpoint_document())

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://eidolond.test"
    )
    directory = EidolondHttpServiceDirectory(
        base_url="http://eidolond.test",
        contracts=ContractRegistry(),
        client=client,
    )
    endpoint = await directory.resolve(
        service_id="hub",
        endpoint_id="device-authority.http",
        required_protocol="http",
        required_contract=HUB_CONTRACT,
    )
    assert endpoint == ResolvedServiceEndpoint(
        service_id="hub",
        endpoint_id="device-authority.http",
        protocol="http",
        address="http://127.0.0.1:8082",
        contract=HUB_CONTRACT,
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_directory_adapter_resolves_over_a_real_unix_socket() -> None:
    requests: list[bytes] = []
    response_body = json.dumps(endpoint_document(), separators=(",", ":")).encode()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(response_body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + response_body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    # macOS limits AF_UNIX paths to 104 bytes; pytest's nested tmp_path is longer.
    with tempfile.TemporaryDirectory(prefix="ek-", dir="/tmp") as temp_dir:
        socket_path = Path(temp_dir) / "eidolond.sock"
        try:
            server = await asyncio.start_unix_server(handle, path=socket_path)
        except PermissionError:
            pytest.skip("test sandbox forbids binding Unix domain sockets")
        directory = EidolondHttpServiceDirectory(
            base_url="http://eidolond",
            uds_path=socket_path,
            contracts=ContractRegistry(),
        )
        try:
            endpoint = await directory.resolve(
                service_id="hub",
                endpoint_id="device-authority.http",
                required_protocol="http",
                required_contract=HUB_CONTRACT,
            )
        finally:
            await directory.close()
            server.close()
            await server.wait_closed()

    assert endpoint.address == "http://127.0.0.1:8082"
    assert requests[0].startswith(
        b"GET /api/system/v1/services/hub/endpoints/device-authority.http HTTP/1.1"
    )


@pytest.mark.asyncio
async def test_directory_adapter_consumes_real_eidolond_http_response(tmp_path) -> None:
    """Exercise the producer and consumer contracts without sharing their DTOs."""
    store = SqliteSystemStateStore(tmp_path / "eidolond.sqlite3")
    manager = ServiceManager(
        catalog=ServiceCatalog(
            (
                ServiceDefinition(
                    service_id="hub",
                    description="Device authority",
                    required=True,
                    enabled_by_default=True,
                    dependencies=(),
                    host_targets={"fake": "hub"},
                    endpoints=(
                        ServiceEndpoint(
                            endpoint_id="device-authority.http",
                            protocol="http",
                            address="http://127.0.0.1:8082",
                            contract=HUB_CONTRACT,
                            health_url="http://127.0.0.1:8082/health",
                        ),
                    ),
                ),
            )
        ),
        store=store,
        directory=InMemoryServiceDirectory(),
        host=FakeHostSupervisor(),
        readiness=FakeReadinessProbe(),
        clock=FixedClock(),
    )
    await manager.initialize()
    await manager.reconcile()
    app = create_system_http_app(manager=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://eidolond.test",
    ) as client:
        directory = EidolondHttpServiceDirectory(
            base_url="http://eidolond.test",
            contracts=ContractRegistry(),
            client=client,
        )
        endpoint = await directory.resolve(
            service_id="hub",
            endpoint_id="device-authority.http",
            required_protocol="http",
            required_contract=HUB_CONTRACT,
        )

    assert endpoint.address == "http://127.0.0.1:8082"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (404, {}, "not available"),
        (503, {}, "not ready"),
        (418, {}, "unexpected"),
        (200, [], "violated"),
        (200, {"unexpected": True}, "violated"),
        (200, endpoint_document(service_id="other"), "identity"),
        (200, endpoint_document(protocol="grpc"), "protocol"),
        (200, endpoint_document(address="grpc://127.0.0.1:8082"), "address"),
        (200, endpoint_document(contract="eidolon.hub.v2"), "contract"),
    ],
)
async def test_directory_adapter_fails_closed_on_status_shape_and_contract_drift(
    status, body, message
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
        base_url="http://eidolond.test",
    )
    directory = EidolondHttpServiceDirectory(
        base_url="http://eidolond.test",
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(ServiceDirectoryUnavailable, match=message):
        await directory.resolve(
            service_id="hub",
            endpoint_id="device-authority.http",
            required_protocol="http",
            required_contract=HUB_CONTRACT,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_directory_adapter_maps_transport_failure_and_rejects_bad_bootstrap_url() -> None:
    with pytest.raises(ValueError, match="HTTP"):
        EidolondHttpServiceDirectory(
            base_url="unix:///tmp/eidolond.sock",
            contracts=ContractRegistry(),
        )
    with pytest.raises(ValueError, match="HTTP"):
        EidolondHttpServiceDirectory(
            base_url="http://",
            contracts=ContractRegistry(),
        )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    directory = EidolondHttpServiceDirectory(
        base_url="http://eidolond.test",
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(ServiceDirectoryUnavailable, match="unreachable"):
        await directory.resolve(
            service_id="hub",
            endpoint_id="device-authority.http",
            required_protocol="http",
            required_contract=HUB_CONTRACT,
        )
    await client.aclose()


class FakeDirectory:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls = []

    async def resolve(self, **arguments) -> ResolvedServiceEndpoint:
        self.calls.append(arguments)
        if self.unavailable:
            raise ServiceDirectoryUnavailable("offline")
        return ResolvedServiceEndpoint(
            service_id="hub",
            endpoint_id="device-authority.http",
            protocol="http",
            address="https://hub.test",
            contract=HUB_CONTRACT,
        )


class FakeCompanionDirectory:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls = []

    async def resolve(self, **arguments) -> ResolvedServiceEndpoint:
        self.calls.append(arguments)
        if self.unavailable:
            raise ServiceDirectoryUnavailable("offline")
        return ResolvedServiceEndpoint(
            service_id="data",
            endpoint_id="companion-authority.http",
            protocol="http",
            address="https://data.test",
            contract=DATA_COMPANION_CONTRACT,
        )


@pytest.mark.asyncio
async def test_directory_routed_hub_authority_resolves_then_calls_existing_contract() -> None:
    directory = FakeDirectory()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document()))
    )
    authority = DirectoryRoutedHubDeviceAuthority(
        directory=directory,
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    admission = await authority.get_device(owner_id="owner one", device_id="device one")
    assert admission.status == "approved"
    assert await authority.is_available() is True
    expected_resolve = {
        "service_id": "hub",
        "endpoint_id": "device-authority.http",
        "required_protocol": "http",
        "required_contract": HUB_CONTRACT,
    }
    assert directory.calls == [expected_resolve, expected_resolve]
    await client.aclose()


@pytest.mark.asyncio
async def test_directory_routed_hub_authority_maps_directory_failure() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("Hub must not be called"))
    )
    authority = DirectoryRoutedHubDeviceAuthority(
        directory=FakeDirectory(unavailable=True),
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(AuthorityUnavailable, match="Service Directory"):
        await authority.get_device(owner_id="owner", device_id="device")
    assert await authority.is_available() is False
    await authority.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_directory_routed_companion_authority_resolves_then_calls_existing_contract() -> None:
    directory = FakeCompanionDirectory()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=companion_document())
        )
    )
    authority = DirectoryRoutedEidolonDataCompanionAuthority(
        directory=directory,
        bearer_token=COMPANION_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    identity = await authority.get_companion(companion_id="companion one")
    assert identity.status == "active"
    assert await authority.is_available() is True
    expected_resolve = {
        "service_id": "data",
        "endpoint_id": "companion-authority.http",
        "required_protocol": "http",
        "required_contract": DATA_COMPANION_CONTRACT,
    }
    assert directory.calls == [expected_resolve, expected_resolve]
    await client.aclose()


@pytest.mark.asyncio
async def test_directory_routed_companion_authority_maps_directory_failure() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("Data must not be called"))
    )
    authority = DirectoryRoutedEidolonDataCompanionAuthority(
        directory=FakeCompanionDirectory(unavailable=True),
        bearer_token=COMPANION_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    with pytest.raises(AuthorityUnavailable, match="Service Directory"):
        await authority.get_companion(companion_id="companion")
    assert await authority.is_available() is False
    await authority.close()
    await client.aclose()
