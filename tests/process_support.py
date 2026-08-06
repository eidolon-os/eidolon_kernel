"""Process-only dependency authority used by the real Kernel/Data V2 E2E."""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException


def create_dependency_app() -> FastAPI:
    hub_token = os.environ["EIDOLON_TEST_HUB_TOKEN"]
    base_url = os.environ["EIDOLON_TEST_DEPENDENCY_BASE_URL"].rstrip("/")
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
        }

    return app
