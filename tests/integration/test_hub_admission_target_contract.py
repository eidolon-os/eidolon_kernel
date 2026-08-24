from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from eidolon_sdk.device_foundation.v1 import (
    ClaimEventCursor,
    ClaimEventPage,
    OwnerDomainId,
)

from eidolon_kernel.adapters.device_registry.hub_http import HubHttpDeviceAuthority
from eidolon_kernel.contracts.registry import ContractRegistry
from tests.support import claim_event_item

ROOT = Path(__file__).resolve().parents[2]
HUB_ROOT = ROOT.parent / "eidolon_hub"
HUB_READER_TOKEN = "hub-device-registry-reader-token-0001"


@pytest.mark.asyncio
async def test_kernel_consumes_the_frozen_hub_target_router(monkeypatch) -> None:
    if not (HUB_ROOT / "hub/admission/target_app.py").is_file():
        pytest.skip("frozen Hub target checkout is unavailable")
    monkeypatch.syspath_prepend(str(HUB_ROOT))
    from hub.admission.target_app import create_admission_target_app

    item = claim_event_item()

    class Authority:
        async def claim_event_page(self, *, cursor, limit, owner_domain_id):
            assert cursor == ClaimEventCursor(stream_position=0)
            assert limit == 100
            assert owner_domain_id == OwnerDomainId("owner-domain_01")
            return ClaimEventPage(
                requested_after=cursor,
                events=(item,),
                next_cursor=ClaimEventCursor(stream_position=1),
                high_watermark=1,
                observed_at=datetime(2026, 8, 4, 8, 1, tzinfo=UTC),
            )

    seen_credentials: list[str] = []

    async def actor_provider(request):
        raise AssertionError("Kernel reads the Claim event stream as a workload, not a Controller")

    async def claim_event_reader(request):
        # What a Host actually checks: an exact service capability, not a
        # Controller ActorRef. Reading the stream is not an Owner action.
        credential = request.headers.get("Authorization", "")
        seen_credentials.append(credential)
        if credential != f"Bearer {HUB_READER_TOKEN}":
            raise AssertionError("Kernel presented another credential")
        return OwnerDomainId("owner-domain_01")

    app = create_admission_target_app(
        authority=Authority(),
        actor_provider=actor_provider,
        claim_event_reader_provider=claim_event_reader,
    )
    assert "/api/admission/v1/claim-events" in app.openapi()["paths"]
    assert not any(
        "device-management" in path or "device-onboarding" in path
        for path in app.openapi()["paths"]
    )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://hub-target",
    )
    adapter = HubHttpDeviceAuthority(
        base_url="http://hub-target",
        bearer_token=HUB_READER_TOKEN,
        contracts=ContractRegistry(),
        client=client,
    )
    try:
        page = await adapter.list_claim_events(
            cursor=ClaimEventCursor(stream_position=0), limit=100
        )
        assert page.events == (item,)
        assert page.next_cursor.stream_position == 1
        assert seen_credentials == [f"Bearer {HUB_READER_TOKEN}"]
    finally:
        await client.aclose()
