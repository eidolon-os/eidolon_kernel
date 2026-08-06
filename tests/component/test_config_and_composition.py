from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.adapters.runtime import SystemClock
from eidolon_kernel.composition.app import create_production_app
from eidolon_kernel.config import (
    CompanionAuthoritySettings,
    DeploymentSettings,
    HubSettings,
    KernelSettings,
    PersistenceSettings,
    ReconciliationSettings,
    SystemDirectorySettings,
    load_companion_authority_token,
    load_hub_token,
    load_settings,
)


def test_settings_load_relative_paths_and_secrets_explicitly(tmp_path, monkeypatch) -> None:
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        """persistence:\n  path: var/test.sqlite3\nsystem_directory:\n  base_url: http://eidolond\n  uds_path: var/system.sock\n  timeout_seconds: 2\nhub:\n  timeout_seconds: 2\ndeployment:\n  mode: trusted-local\n  trusted_local_ingress: true\n""",
        encoding="utf-8",
    )
    settings = load_settings(settings_file)
    assert settings.persistence.path.is_absolute()
    assert settings.system_directory.uds_path.is_absolute()
    hub_token = "hub-device-registry-reader-token-0001"
    monkeypatch.setenv("EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN", f" {hub_token} ")
    assert load_hub_token() == hub_token
    monkeypatch.delenv("EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        load_hub_token()
    monkeypatch.setenv("EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        load_hub_token()
    monkeypatch.setenv(
        "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN",
        "companion-authority-token-0001",
    )
    assert load_companion_authority_token() == "companion-authority-token-0001"


def test_settings_reject_unsupported_network_and_identity_modes() -> None:
    with pytest.raises(ValidationError):
        HubSettings(base_url="http://127.0.0.1:8082")
    with pytest.raises(ValidationError):
        SystemDirectorySettings(base_url="http://remote.example", uds_path=None)
    with pytest.raises(ValidationError):
        SystemDirectorySettings(base_url="unix:///tmp/system.sock")
    with pytest.raises(ValidationError):
        SystemDirectorySettings(base_url="http://")
    with pytest.raises(ValidationError):
        DeploymentSettings(mode="jwt")
    with pytest.raises(ValidationError):
        CompanionAuthoritySettings(base_url="unix:///tmp/data.sock")
    with pytest.raises(ValidationError):
        ReconciliationSettings(interval_seconds=0)


def test_production_composition_rejects_unconfigured_identity_root(tmp_path) -> None:
    settings = KernelSettings(
        persistence=PersistenceSettings(path=tmp_path / "kernel.sqlite3"),
        deployment=DeploymentSettings(trusted_local_ingress=False),
    )
    with pytest.raises(RuntimeError, match="no configured Kernel identity"):
        create_production_app(settings)


@pytest.mark.asyncio
async def test_production_composition_builds_exclusive_store_and_closes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN",
        "hub-device-registry-reader-token-0001",
    )
    monkeypatch.setenv(
        "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN",
        "companion-authority-token-0001",
    )
    settings = KernelSettings(
        persistence=PersistenceSettings(path=tmp_path / "kernel.sqlite3"),
        system_directory=SystemDirectorySettings(
            base_url="http://eidolond",
            uds_path=tmp_path / "system.sock",
        ),
    )
    runtime = create_production_app(settings)
    assert runtime.app.title == "Eidolon Sovereign Kernel"
    assert runtime.projection.get("missing") is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://kernel.test"
    ) as client:
        health = await client.get("/health")
    assert health.json() == {
        "status": "degraded",
        "authoritative_store": "ready",
        "device_mount_write_available": False,
        "companion_attachment_write_available": False,
        "blockers": [
            "Hub device authority endpoint is not ready in eidolond",
            "Data companion authority endpoint is not ready in eidolond",
        ],
    }
    # Drive lifespan explicitly and prove shutdown releases the database lock.
    context = runtime.app.router.lifespan_context(runtime.app)
    await context.__aenter__()
    await context.__aexit__(None, None, None)
    reopened = SqliteMountStore(tmp_path / "kernel.sqlite3")
    reopened.close()


def test_main_factory_returns_composed_app(monkeypatch) -> None:
    import eidolon_kernel.main as main

    sentinel = SimpleNamespace(app="sentinel-app")
    monkeypatch.setattr(main, "create_production_app", lambda: sentinel)
    assert main.create_app() == "sentinel-app"


def test_system_clock_returns_timezone_aware_time() -> None:
    assert SystemClock().now().utcoffset() is not None
