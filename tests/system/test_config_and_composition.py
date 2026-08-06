from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eidolon_system.composition.app import create_production_app
from eidolon_system.config import (
    HostSettings,
    InterfaceSettings,
    SystemSettings,
    load_settings,
    selected_host_driver,
)
from tests.system.support import FakeHostSupervisor, FakeReadinessProbe
from tests.system.test_manifest_contract import manifest_document


def test_settings_resolve_relative_paths_and_select_platform_adapter(tmp_path) -> None:
    settings_path = tmp_path / "eidolond.yaml"
    settings_path.write_text(
        """
manifest: {path: services.yaml}
persistence: {path: var/system.sqlite3}
host:
  driver: auto
  supervisorctl: tools/supervisorctl
  supervisor_config: supervisord.conf
interface: {host: 127.0.0.1, port: 8090}
""",
        encoding="utf-8",
    )
    settings = load_settings(settings_path)
    assert settings.manifest.path == (tmp_path / "services.yaml").resolve()
    assert settings.persistence.path == (tmp_path / "var/system.sqlite3").resolve()
    assert settings.host.supervisorctl == str((tmp_path / "tools/supervisorctl").resolve())
    assert selected_host_driver(settings.host, system_name="Linux") == "systemd"
    assert selected_host_driver(settings.host, system_name="Darwin") == "supervisord"
    assert selected_host_driver(HostSettings(driver="systemd"), system_name="Darwin") == "systemd"
    with pytest.raises(RuntimeError, match="no eidolond host adapter"):
        selected_host_driver(HostSettings(), system_name="Windows")
    with pytest.raises(ValidationError):
        HostSettings(driver="supervisord")
    with pytest.raises(ValidationError):
        InterfaceSettings(host="0.0.0.0")


@pytest.mark.asyncio
async def test_production_composition_builds_independent_runtime_and_releases_store(
    tmp_path, monkeypatch
) -> None:
    import yaml

    import eidolon_system.composition.app as composition

    manifest_path = tmp_path / "services.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_document()), encoding="utf-8")
    settings = SystemSettings.model_validate(
        {
            "manifest": {"path": manifest_path},
            "persistence": {"path": tmp_path / "system.sqlite3"},
            "host": {"driver": "systemd"},
            "reconciliation": {"interval_seconds": 60},
        }
    )
    host = FakeHostSupervisor()
    host.driver_name = "systemd"

    class ClosableProbe(FakeReadinessProbe):
        async def close(self) -> None:
            self.closed = True

    probe = ClosableProbe()
    monkeypatch.setattr(composition, "SystemdHostSupervisor", lambda **kwargs: host)
    monkeypatch.setattr(composition, "HttpReadinessProbe", lambda **kwargs: probe)
    runtime = create_production_app(settings)
    context = runtime.app.router.lifespan_context(runtime.app)
    await context.__aenter__()
    assert runtime.manager.get_service("kernel").runtime_state == "ready"
    await context.__aexit__(None, None, None)
    assert probe.closed is True


def test_main_factory_and_runner_use_configured_binding(monkeypatch, tmp_path) -> None:
    import uvicorn

    import eidolon_system.main as main

    monkeypatch.setattr(
        main,
        "create_production_app",
        lambda: SimpleNamespace(app="system-app"),
    )
    assert main.create_app() == "system-app"
    settings = SystemSettings()
    settings.interface.port = 8190
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    main.run()
    assert calls[0][1]["port"] == 8190
