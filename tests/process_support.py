"""Process-only dependency authority used by the real Kernel/Data V2 E2E."""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException


def create_dependency_app() -> FastAPI:
    hub_token = os.environ["EIDOLON_TEST_HUB_TOKEN"]
    base_url = os.environ["EIDOLON_TEST_DEPENDENCY_BASE_URL"].rstrip("/")
    data_base_url = os.environ["EIDOLON_TEST_DATA_BASE_URL"].rstrip("/")
    app = FastAPI(title="Kernel E2E dependency authority")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/api/system/v1/services/hub/endpoints/device-authority.http")
    async def resolve_hub() -> dict[str, str]:
        return {
            "operation": "system.service-endpoint",
            "service_id": "hub",
            "endpoint_id": "device-authority.http",
            "protocol": "http",
            "address": base_url,
            "contract": "eidolon.hub.device-directory.v1",
        }

    @app.get("/api/system/v1/services/data/endpoints/companion-authority.http")
    async def resolve_data() -> dict[str, str]:
        return {
            "operation": "system.service-endpoint",
            "service_id": "data",
            "endpoint_id": "companion-authority.http",
            "protocol": "http",
            "address": data_base_url,
            "contract": ("https://eidolon.dev/data/contracts/v1/companion/identity.schema.json"),
        }

    @app.get("/api/device-management/v1/owners/{owner_id}/devices/{device_id}")
    async def get_device(
        owner_id: str,
        device_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        if authorization != f"Bearer {hub_token}":
            raise HTTPException(status_code=403, detail="invalid Hub credential")
        timestamp = "2026-08-06T00:00:00Z"
        return {
            "operation": "device.directory-entry",
            "device_id": device_id,
            "owner_scope": owner_id,
            "display_name": device_id,
            "device_kind": "e2e-fixture",
            "manifest": {
                "schema_version": 1,
                "title": "E2E fixture",
                "properties": [],
                "actions": [],
                "events": [],
                "media": [],
            },
            "manifest_revision": "fixture-v1",
            "lifecycle_state": "approved",
            "enrolled_at": timestamp,
            "updated_at": timestamp,
            "device_ref": {
                "device_instance_id": device_id,
                "owner_domain_id": owner_id,
                "owner_domain_generation": 1,
                "claim_generation": 1,
                "trust_epoch": 1,
            },
        }

    @app.get("/api/admission/v1/claim-events")
    async def claim_events(
        after_stream_position: int = 0,
        limit: int = 100,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        if authorization != f"Bearer {hub_token}":
            raise HTTPException(status_code=403, detail="invalid Hub credential")
        return {
            "stream_id": "admission-claims-v1",
            "requested_after": {
                "stream_id": "admission-claims-v1",
                "stream_position": after_stream_position,
            },
            "events": [],
            "next_cursor": {
                "stream_id": "admission-claims-v1",
                "stream_position": after_stream_position,
            },
            "high_watermark": after_stream_position,
            "observed_at": "2026-08-06T00:00:00Z",
        }

    return app
