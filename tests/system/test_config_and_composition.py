from __future__ import annotations

import stat
import tempfile
from pathlib import Path
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
    assert InterfaceSettings(uds_mode="0600").uds_mode_bits == 0o600
    with pytest.raises(ValidationError):
        InterfaceSettings(uds_mode="666")
    with pytest.raises(ValidationError):
        InterfaceSettings(uds_mode="0680")


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


def test_main_runner_prebinds_uds_with_configured_mode(monkeypatch, tmp_path) -> None:
    import uvicorn

    import eidolon_system.main as main

    class FakeListener:
        closed = False

        def fileno(self) -> int:
            return 42

        def close(self) -> None:
            self.closed = True

    listener = FakeListener()
    socket_path = tmp_path / "system.sock"
    settings = SystemSettings(
        interface=InterfaceSettings(uds=socket_path, uds_mode="0660", uds_group="staff")
    )
    bind_calls = []
    run_calls = []
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(
        main,
        "_bind_unix_socket",
        lambda path, mode, group: bind_calls.append((path, mode, group)) or listener,
    )
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: run_calls.append(kwargs))

    main.run()

    assert bind_calls == [(socket_path, 0o660, "staff")]
    assert run_calls == [{"factory": True, "fd": 42}]
    assert listener.closed is True


def test_uds_binder_refuses_to_replace_a_non_socket_path(tmp_path) -> None:
    from eidolon_system.main import _bind_unix_socket, _remove_unix_socket

    protected = tmp_path / "system.sock"
    protected.write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="non-socket"):
        _bind_unix_socket(protected, 0o600)
    _remove_unix_socket(protected)
    assert protected.read_text(encoding="utf-8") == "keep"


def test_uds_binder_applies_restrictive_permissions() -> None:
    from eidolon_system.main import _bind_unix_socket, _remove_unix_socket

    with tempfile.TemporaryDirectory(prefix="es-", dir="/tmp") as temp_dir:
        socket_path = Path(temp_dir) / "system.sock"
        try:
            listener = _bind_unix_socket(socket_path, 0o600)
        except PermissionError:
            pytest.skip("test sandbox forbids binding Unix domain sockets")
        try:
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            listener.listen()
            with pytest.raises(RuntimeError, match="already active"):
                _bind_unix_socket(socket_path, 0o600)
        finally:
            listener.close()
        replacement = _bind_unix_socket(socket_path, 0o660)
        try:
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
        finally:
            replacement.close()
            _remove_unix_socket(socket_path)
        assert not socket_path.exists()
        _remove_unix_socket(socket_path)


def test_uds_group_applies_only_to_the_bound_socket():
    import grp
    import os

    from eidolon_system.main import _bind_unix_socket, _remove_unix_socket

    group = grp.getgrgid(os.getgid()).gr_name
    with tempfile.TemporaryDirectory(prefix="es-", dir="/tmp") as temp_dir:
        root = Path(temp_dir)
        untouched = root / "private"
        untouched.write_text("private")
        before = untouched.stat()
        path = root / "system.sock"
        try:
            listener = _bind_unix_socket(path, 0o660, group)
        except PermissionError:
            pytest.skip("sandbox forbids Unix sockets")
        try:
            assert path.stat().st_gid == grp.getgrnam(group).gr_gid
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
            assert (untouched.stat().st_gid, untouched.stat().st_mode) == (before.st_gid, before.st_mode)
        finally:
            listener.close()
            _remove_unix_socket(path)
