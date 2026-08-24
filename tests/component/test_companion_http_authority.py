from __future__ import annotations

import httpx
import pytest

from eidolon_kernel.adapters.companion.eidolon_data_http import (
    EidolonDataHttpCompanionAuthority,
)
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityRejected, AuthorityUnavailable

TOKEN = "companion-authority-token-0001"


def document(**overrides):
    value = {
        "operation": "companion.identity",
        "companion_id": "companion one",
        "owner_id": "owner-1",
        "lifecycle_state": "active",
        "kind": "conversational",
        "revision": 1,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_companion_adapter_consumes_only_exact_identity_get() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == (b"/api/companion-authority/v1/companions/companion%20one")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json=document())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = EidolonDataHttpCompanionAuthority(
        base_url="http://data.test/",
        bearer_token=TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        identity = await adapter.get_companion(companion_id="companion one")
    finally:
        await client.aclose()

    assert identity.companion_id == "companion one"
    assert identity.owner_id == "owner-1"
    assert identity.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {}, AuthorityRejected),
        (401, {}, AuthorityUnavailable),
        (403, {}, AuthorityUnavailable),
        (503, {}, AuthorityUnavailable),
        (202, {}, AuthorityUnavailable),
        (200, {"unexpected": True}, AuthorityUnavailable),
    ],
)
async def test_companion_adapter_maps_policy_and_contract_failures(status, body, error) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    )
    adapter = EidolonDataHttpCompanionAuthority(
        base_url="http://data.test",
        bearer_token=TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        with pytest.raises(error):
            await adapter.get_companion(companion_id="companion")
    finally:
        await client.aclose()


def test_companion_adapter_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        EidolonDataHttpCompanionAuthority(
            base_url="file:///data",
            bearer_token=TOKEN,
            contracts=ContractRegistry(),
        )
    with pytest.raises(ValueError):
        EidolonDataHttpCompanionAuthority(
            base_url="http://",
            bearer_token=TOKEN,
            contracts=ContractRegistry(),
        )
    with pytest.raises(ValueError):
        EidolonDataHttpCompanionAuthority(
            base_url="http://data.test",
            bearer_token="short",
            contracts=ContractRegistry(),
        )
