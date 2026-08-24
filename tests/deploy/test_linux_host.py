from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from eidolon_deploy.activation import ActivationReceipt, ActivationStatus
from eidolon_deploy.fingerprints import environment_sha256, source_tree_sha256
from eidolon_deploy.linux import (
    CommandResult,
    LinuxDeploymentError,
    LinuxDeploymentHost,
    SubprocessRunner,
)
from eidolon_deploy.manifest import release_descriptor_from_document
from tests.deploy.support import release_document


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.verified_unit_texts: dict[str, str] = {}
        self.fail_command: tuple[str, ...] | None = None
        self.missing_units: set[str] = set()
        self.current_bootstrap_schema_version = 5
        self.release_bootstrap_schema_version = 5
        self.bootstrap_database_schema_version = 5

    def run(self, *command: str) -> CommandResult:
        self.calls.append(command)
        if command[:2] == ("/usr/bin/systemd-analyze", "verify"):
            self.verified_unit_texts = {
                Path(path).name: Path(path).read_text(encoding="utf-8")
                for path in command[2:]
            }
        if self.fail_command and command[: len(self.fail_command)] == self.fail_command:
            return CommandResult(1, "", "injected command failure")
        if command[1:4] == ("show", "--property=LoadState", "--value"):
            state = "not-found" if command[-1] in self.missing_units else "loaded"
            return CommandResult(0, f"{state}\n", "")
        if len(command) >= 3 and command[-2] == "-c" and "metadata.distributions" in command[-1]:
            return CommandResult(0, "pip==25.1\neidolon-test==1.0\n", "")
        if command[-2:] == (
            "import platform; print(f'{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}')",
            "",
        ):
            raise AssertionError("unexpected Python probe shape")
        if len(command) >= 3 and command[-2] == "-c" and "python_version_tuple" in command[-1]:
            return CommandResult(0, "3.13\n", "")
        if len(command) >= 3 and command[1] == "-c" and "BOOTSTRAP_SCHEMA_VERSION" in command[2]:
            version = (
                self.current_bootstrap_schema_version
                if "/old/eidolon_admin/" in command[0]
                else self.release_bootstrap_schema_version
            )
            return CommandResult(0, f"{version}\n", "")
        if len(command) == 4 and command[1] == "-c" and "PRAGMA user_version" in command[2]:
            return CommandResult(0, f"{self.bootstrap_database_schema_version}\n", "")
        return CommandResult(0, "", "")


def _host_path(root: Path, path: str | Path) -> Path:
    value = Path(path)
    return root / value.relative_to("/")


def prepared_release(tmp_path: Path):
    root = tmp_path / "root"
    document = release_document()
    runner = FakeRunner()

    for component in document["components"]:
        release_path = _host_path(root, component["release_path"])
        (release_path / ".venv/bin").mkdir(parents=True)
        python = release_path / ".venv/bin/python"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        (release_path / "uv.lock").write_text(f"lock:{component['component_id']}\n")
        (release_path / "pyproject.toml").write_text(
            f"[project]\nname='{component['component_id']}'\n"
        )
        for entrypoint in component["required_entrypoints"]:
            path = release_path / entrypoint
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        component["lock_sha256"] = hashlib.sha256(
            (release_path / "uv.lock").read_bytes()
        ).hexdigest()
        component["environment_sha256"] = environment_sha256("pip==25.1\neidolon-test==1.0\n")

        old_target = _host_path(
            root,
            f"/opt/eidolon/releases/old/{component['component_id']}",
        )
        old_target.mkdir(parents=True)
        if component["component_id"] == "eidolon_admin":
            old_python = old_target / ".venv/bin/python"
            old_python.parent.mkdir(parents=True)
            old_python.write_text("#!/bin/sh\n")
            old_python.chmod(0o755)
        current_link = _host_path(root, component["current_link"])
        current_link.parent.mkdir(parents=True, exist_ok=True)
        current_link.symlink_to(old_target)

    for support_source in document["support_sources"]:
        support_path = _host_path(root, support_source["release_path"])
        support_path.mkdir(parents=True)
        (support_path / "pyproject.toml").write_text("[project]\nname='eidolon_sdk'\n")
        support_source["source_tree_sha256"] = source_tree_sha256(support_path)

    component_roots = {
        component["component_id"]: _host_path(root, component["release_path"])
        for component in document["components"]
    }
    for asset in document["system_assets"]:
        source = component_roots[asset["source_component_id"]] / asset["source"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"asset:{asset['destination']}\n")
        asset["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = _host_path(root, asset["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"old:{asset['destination']}\n")

    for secret in document["required_secrets"]:
        path = _host_path(root, secret["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret\n")
        path.chmod(0o600)

    bootstrap_database = _host_path(root, "/var/lib/eidolon-bootstrap/bootstrap.sqlite3")
    bootstrap_database.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_database.write_bytes(b"test database placeholder")

    for component in document["components"]:
        component["source_tree_sha256"] = source_tree_sha256(
            _host_path(root, component["release_path"])
        )

    release = release_descriptor_from_document(document)
    host = LinuxDeploymentHost(
        root=root,
        runner=runner,
        system="linux",
        machine="aarch64",
        readiness_probe=lambda check: True,
        readiness_timeout_seconds=0.1,
        readiness_interval_seconds=0.001,
        require_root=False,
    )
    return root, release, host, runner


def test_source_tree_fingerprint_excludes_runtime_and_cache_state(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "tracked.py").write_text("value = 1\n")
    initial = source_tree_sha256(root)
    (root / ".venv/lib").mkdir(parents=True)
    (root / ".venv/lib/runtime.py").write_text("ignored\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/tracked.pyc").write_bytes(b"ignored")

    assert source_tree_sha256(root) == initial
    (root / "tracked.py").write_text("value = 2\n")
    assert source_tree_sha256(root) != initial


def test_source_tree_fingerprint_rejects_external_symlink_inputs(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    external = tmp_path / "external.py"
    external.write_text("mutable = True\n")
    (root / "linked.py").symlink_to(external)

    with pytest.raises(ValueError, match="symlink"):
        source_tree_sha256(root)


def test_preflight_verifies_target_release_and_returns_current_targets(tmp_path: Path) -> None:
    root, release, host, runner = prepared_release(tmp_path)

    previous = host.preflight(release)

    assert set(previous) == {
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
    }
    assert previous["eidolon_kernel"].endswith("/old/eidolon_kernel")
    verify_call = next(
        call for call in runner.calls if call[:2] == ("/usr/bin/systemd-analyze", "verify")
    )
    assert len(verify_call[2:]) == 16
    assert not any("/etc/avahi/" in item for item in verify_call)
    assert not any(call[:2] == ("/usr/bin/systemctl", "stop") for call in runner.calls)


def test_preflight_verifies_new_entrypoints_against_the_candidate_release(tmp_path: Path) -> None:
    root, release, host, runner = prepared_release(tmp_path)
    lifecycle = next(
        asset
        for asset in release.system_assets
        if asset.destination.name == "eidolon-lifecycle-workflow.service"
    )
    source = _host_path(root, release.components_by_id[lifecycle.source_component_id].release_path)
    source = source / lifecycle.source
    source.write_text(
        "[Service]\n"
        "ExecStart=/opt/eidolon/current/eidolon_admin/.venv/bin/"
        "eidolon-lifecycle-workflow\n",
        encoding="utf-8",
    )
    object.__setattr__(lifecycle, "sha256", hashlib.sha256(source.read_bytes()).hexdigest())
    admin_component = release.components_by_id["eidolon_admin"]
    object.__setattr__(
        admin_component,
        "source_tree_sha256",
        source_tree_sha256(_host_path(root, admin_component.release_path)),
    )

    host.preflight(release)

    rendered = runner.verified_unit_texts["eidolon-lifecycle-workflow.service"]
    assert "/opt/eidolon/current/eidolon_admin" not in rendered
    assert str(release.components_by_id["eidolon_admin"].release_path) in rendered


def test_preflight_rejects_bootstrap_schema_transition_before_host_mutation(
    tmp_path: Path,
) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    runner.current_bootstrap_schema_version = 4

    with pytest.raises(LinuxDeploymentError, match="outside release rollback semantics"):
        host.preflight(release)

    assert not any(call[:2] == ("/usr/bin/systemctl", "stop") for call in runner.calls)


def test_preflight_rejects_bootstrap_database_schema_drift(tmp_path: Path) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    runner.bootstrap_database_schema_version = 4

    with pytest.raises(LinuxDeploymentError, match="authority schema"):
        host.preflight(release)


def test_quiesce_and_start_order_prevents_competing_restart_authorities(
    tmp_path: Path,
) -> None:
    _, release, host, runner = prepared_release(tmp_path)

    host.quiesce(release)
    host.start_release(release)

    systemctl_calls = [
        call[1:] for call in runner.calls if call[0] == "/usr/bin/systemctl" and call[1] != "show"
    ]
    assert systemctl_calls == [
        ("stop", "eidolon-local-api.service"),
        ("stop", "eidolon-lifecycle-workflow.service"),
        ("stop", "eidolon-admin.service"),
        ("stop", "eidolon-bootstrapd.service"),
        ("stop", "eidolond.service"),
        ("stop", "eidolon-channel.service"),
        ("stop", "eidolon-channel-provider.service"),
        ("stop", "eidolon-agent.service"),
        ("stop", "eidolon-memory-discovery.service"),
        ("stop", "eidolon-memory-supervisor.service"),
        ("stop", "eidolon-livekit.service"),
        ("stop", "eidolon-nats.service"),
        ("stop", "eidolon-kernel.service"),
        ("stop", "eidolon-hub.service"),
        ("stop", "eidolon-data-workspace.service"),
        ("stop", "eidolon-data.service"),
        ("start", "eidolon-bootstrapd.service"),
        ("start", "eidolond.service"),
        ("start", "eidolon-admin.service"),
        ("start", "eidolon-lifecycle-workflow.service"),
        ("start", "eidolon-local-api.service"),
    ]


def test_quiesce_retries_only_transient_canceled_systemd_stop(tmp_path: Path) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    original_run = runner.run
    canceled = {"remaining": 1}

    def transient(*command: str) -> CommandResult:
        if (
            command == ("/usr/bin/systemctl", "stop", "eidolon-livekit.service")
            and canceled["remaining"]
        ):
            runner.calls.append(command)
            canceled["remaining"] -= 1
            return CommandResult(1, "", "Job for eidolon-livekit.service canceled.")
        return original_run(*command)

    runner.run = transient  # type: ignore[method-assign]

    host.quiesce(release)

    assert canceled["remaining"] == 0
    assert runner.calls.count(
        ("/usr/bin/systemctl", "stop", "eidolon-livekit.service")
    ) == 2


def test_quiesce_skips_units_not_installed_before_first_activation(
    tmp_path: Path,
) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    runner.missing_units = {
        "eidolon-admin.service",
        "eidolon-data-workspace.service",
    }

    host.quiesce(release)

    stopped = {call[-1] for call in runner.calls if call[:2] == ("/usr/bin/systemctl", "stop")}
    assert "eidolon-admin.service" not in stopped
    assert "eidolon-data-workspace.service" not in stopped
    assert "eidolon-bootstrapd.service" in stopped
    assert "eidolond.service" in stopped


def test_doctor_requires_the_sealed_release_to_be_active(tmp_path: Path) -> None:
    _, release, host, runner = prepared_release(tmp_path)

    with pytest.raises(LinuxDeploymentError, match="not active release"):
        host.doctor(release)

    host.switch_components(release)
    report = host.doctor(release)

    assert report["release_id"] == release.release_id
    assert set(report["active_targets"]) == {
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
    }
    active_checks = [
        call for call in runner.calls if call[:3] == ("/usr/bin/systemctl", "is-active", "--quiet")
    ]
    assert len(active_checks) == 16


def test_preflight_fails_closed_on_source_or_secret_drift(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    kernel_path = _host_path(root, release.components[0].release_path)
    (kernel_path / "pyproject.toml").write_text("tampered\n")

    with pytest.raises(LinuxDeploymentError, match="source tree"):
        host.preflight(release)

    (kernel_path / "pyproject.toml").write_text("[project]\nname='eidolon_kernel'\n")
    corrected = replace(release.components[0], source_tree_sha256=source_tree_sha256(kernel_path))
    release = replace(release, components=(corrected, *release.components[1:]))
    _host_path(root, "/etc/eidolon/data.env").chmod(0o644)
    with pytest.raises(LinuxDeploymentError, match="secret mode"):
        host.preflight(release)


def test_preflight_rejects_current_link_outside_release_namespace(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    link = _host_path(root, release.components[0].current_link)
    link.unlink()
    link.symlink_to(_host_path(root, "/tmp/untrusted/eidolon_kernel"))

    with pytest.raises(LinuxDeploymentError, match="outside release namespace"):
        host.preflight(release)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda root, release: (
                _host_path(root, release.components[0].release_path)
                .joinpath("uv.lock")
                .write_text("drift\n")
            ),
            "source tree",
        ),
        (
            lambda root, release: (
                _host_path(root, release.components[0].release_path)
                .joinpath(".venv/bin/python")
                .chmod(0o644)
            ),
            "virtual environment",
        ),
        (
            lambda root, release: (
                _host_path(root, release.components[0].release_path)
                .joinpath(".venv/bin/eidolond")
                .chmod(0o644)
            ),
            "entrypoint",
        ),
        (
            lambda root, release: _host_path(root, release.components[0].current_link).unlink(),
            "current link",
        ),
        (
            lambda root, release: (
                _host_path(root, release.components[0].release_path)
                / release.system_assets[0].source
            ).write_text("drift\n"),
            "source tree",
        ),
        (
            lambda root, release: _host_path(root, release.required_secrets[0].path).unlink(),
            "secret is missing",
        ),
    ],
)
def test_preflight_rejects_incomplete_prepared_state(
    tmp_path: Path, mutation, message: str
) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    mutation(root, release)

    with pytest.raises(LinuxDeploymentError, match=message):
        host.preflight(release)


def test_preflight_rejects_target_python_and_environment_mismatch(tmp_path: Path) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    release = replace(release, target=replace(release.target, python="3.12"))
    with pytest.raises(LinuxDeploymentError, match="Python version"):
        host.preflight(release)

    root, release, host, runner = prepared_release(tmp_path / "environment")
    component = replace(release.components[0], environment_sha256="f" * 64)
    release = replace(release, components=(component, *release.components[1:]))
    with pytest.raises(LinuxDeploymentError, match="environment fingerprint"):
        host.preflight(release)


def test_preflight_rejects_wrong_host_profile(tmp_path: Path) -> None:
    _, release, _, runner = prepared_release(tmp_path)
    host = LinuxDeploymentHost(
        root=tmp_path,
        runner=runner,
        system="darwin",
        machine="arm64",
        readiness_probe=lambda check: True,
        require_root=False,
    )

    with pytest.raises(LinuxDeploymentError, match="target profile"):
        host.preflight(release)


def test_snapshot_switch_and_restore_are_recoverable(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    previous = host.preflight(release)
    old_assets = {
        asset.destination: _host_path(root, asset.destination).read_text()
        for asset in release.system_assets
    }

    snapshot = host.create_snapshot(release, previous)
    host.install_assets(release)
    host.switch_components(release)

    for component in release.components:
        assert _host_path(root, component.current_link).resolve() == _host_path(
            root, component.release_path
        )
    for asset in release.system_assets:
        source = (
            _host_path(root, release.components_by_id[asset.source_component_id].release_path)
            / asset.source
        )
        assert _host_path(root, asset.destination).read_bytes() == source.read_bytes()

    host.restore(release, snapshot)

    for component in release.components:
        assert str(_host_path(root, component.current_link).resolve()).endswith(
            f"/old/{component.component_id}"
        )
    for destination, content in old_assets.items():
        assert _host_path(root, destination).read_text() == content


def test_core_topology_expands_and_rollback_removes_new_components(tmp_path: Path) -> None:
    root, release, host, runner = prepared_release(tmp_path)
    expansion_components = {"eidolon_agent", "eidolon_channel", "eidolon_memory"}
    new_asset_destinations = {
        "/etc/systemd/system/eidolon-nats.service",
        "/etc/systemd/system/eidolon-livekit.service",
        "/etc/systemd/system/eidolon-memory-supervisor.service",
        "/etc/systemd/system/eidolon-memory-discovery.service",
        "/etc/systemd/system/eidolon-agent.service",
        "/etc/systemd/system/eidolon-channel.service",
        "/usr/local/libexec/eidolon-livekit-launch",
    }
    for component in release.components:
        if component.component_id in expansion_components:
            _host_path(root, component.current_link).unlink()
    for destination in new_asset_destinations:
        _host_path(root, destination).unlink()

    previous = host.preflight(release)

    assert set(previous) == {
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
    }
    snapshot = host.create_snapshot(release, previous)
    host.install_assets(release)
    host.switch_components(release)
    fresh_host = LinuxDeploymentHost(
        root=root,
        runner=runner,
        system="linux",
        machine="aarch64",
        readiness_probe=lambda check: True,
        require_root=False,
    )
    assert fresh_host.load_snapshot(release, Path(snapshot.backup_path)) == snapshot
    observed_readiness: list[str] = []
    host._readiness_probe = lambda check: observed_readiness.append(check.check_id) or True
    runner.calls.clear()

    host.restore(release, snapshot)

    for component in release.components:
        current = _host_path(root, component.current_link)
        if component.component_id in expansion_components:
            assert not current.exists()
            assert not current.is_symlink()
        else:
            assert str(current.resolve()).endswith(f"/old/{component.component_id}")
    for destination in new_asset_destinations:
        assert not _host_path(root, destination).exists()
    started = {call[-1] for call in runner.calls if call[:2] == ("/usr/bin/systemctl", "start")}
    assert not started & {
        "eidolon-nats.service",
        "eidolon-livekit.service",
        "eidolon-memory-supervisor.service",
        "eidolon-memory-discovery.service",
        "eidolon-agent.service",
        "eidolon-channel.service",
    }
    assert not set(observed_readiness) & {"nats", "livekit", "memory", "agent", "channel"}


def test_topology_expansion_never_allows_a_missing_core_component(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    kernel = release.components_by_id["eidolon_kernel"]
    _host_path(root, kernel.current_link).unlink()

    with pytest.raises(LinuxDeploymentError, match="current link is missing"):
        host.preflight(release)


def test_topology_expansion_rejects_a_partial_new_component_set(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    expansion_components = {"eidolon_agent", "eidolon_channel"}
    for component in release.components:
        if component.component_id in expansion_components:
            _host_path(root, component.current_link).unlink()

    with pytest.raises(LinuxDeploymentError, match="partial topology expansion"):
        host.preflight(release)


def test_first_install_rollback_restores_only_previous_units_and_readiness(
    tmp_path: Path,
) -> None:
    root, release, host, runner = prepared_release(tmp_path)
    for destination in (
        "/etc/systemd/system/eidolon-admin.service",
        "/etc/systemd/system/eidolon-data-workspace.service",
    ):
        _host_path(root, destination).unlink()
    previous = host.preflight(release)
    snapshot = host.create_snapshot(release, previous)
    host.install_assets(release)
    host.switch_components(release)
    observed_readiness: list[str] = []
    host._readiness_probe = lambda check: observed_readiness.append(check.check_id) or True
    runner.calls.clear()

    host.restore(release, snapshot)

    started = {call[-1] for call in runner.calls if call[:2] == ("/usr/bin/systemctl", "start")}
    assert "eidolon-admin.service" not in started
    assert "eidolon-data-workspace.service" not in started
    assert "eidolon-bootstrapd.service" in started
    assert "eidolond.service" in started
    assert "eidolon-lifecycle-workflow.service" in started
    assert "eidolon-local-api.service" in started
    assert "admin" not in observed_readiness
    assert "data-workspace" not in observed_readiness
    assert set(observed_readiness) == {
        "eidolond",
        "data",
        "hub",
        "kernel",
        "lifecycle-workflow",
        "local-api",
        "nats",
        "livekit",
        "memory",
        "agent",
        "channel-provider",
        "channel",
    }


def test_snapshot_v2_records_and_restores_asset_ownership(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    destination = _host_path(root, release.system_assets[0].destination)
    expected = destination.stat()
    previous = host.preflight(release)

    snapshot = host.create_snapshot(release, previous)
    document = json.loads(
        (Path(snapshot.backup_path) / "snapshot.json").read_text(encoding="utf-8")
    )
    state = next(
        item
        for item in document["system_assets"]
        if item["destination"] == str(release.system_assets[0].destination)
    )
    assert document["schema_version"] == 2
    assert state["uid"] == expected.st_uid
    assert state["gid"] == expected.st_gid

    restored: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        host,
        "_restore_file_ownership",
        lambda path, uid, gid: restored.append((path, uid, gid)),
    )
    host.install_assets(release)
    host.restore(release, snapshot)

    restored_state = next(
        item
        for item in restored
        if item[0].parent == destination.parent and item[0].name.startswith(f".{destination.name}.")
    )
    assert restored_state[1:] == (expected.st_uid, expected.st_gid)


def test_command_failure_never_becomes_success(tmp_path: Path) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    runner.fail_command = ("/usr/bin/systemd-analyze", "verify")

    with pytest.raises(LinuxDeploymentError, match="systemd unit verification"):
        host.preflight(release)


def test_readiness_timeout_is_fail_closed(tmp_path: Path) -> None:
    root, release, _, runner = prepared_release(tmp_path)
    host = LinuxDeploymentHost(
        root=root,
        runner=runner,
        system="linux",
        machine="aarch64",
        readiness_probe=lambda check: False,
        readiness_timeout_seconds=0,
        readiness_interval_seconds=0,
        require_root=False,
    )

    with pytest.raises(LinuxDeploymentError, match="readiness timeout"):
        host.wait_ready(release)


def test_readiness_probe_tolerates_transient_os_error(tmp_path: Path) -> None:
    root, release, _, runner = prepared_release(tmp_path)
    attempts = 0

    def probe(check):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient")
        return True

    host = LinuxDeploymentHost(
        root=root,
        runner=runner,
        system="linux",
        machine="aarch64",
        readiness_probe=probe,
        readiness_timeout_seconds=0.1,
        readiness_interval_seconds=0,
        require_root=False,
    )

    host.wait_ready(release)
    assert attempts > len(release.readiness_checks)


def test_fixed_subprocess_runner_is_shell_free() -> None:
    result = SubprocessRunner().run("/usr/bin/true")

    assert result.returncode == 0


def test_activation_lock_rejects_concurrent_operator(tmp_path: Path) -> None:
    root, _, first, runner = prepared_release(tmp_path)
    second = LinuxDeploymentHost(
        root=root,
        runner=runner,
        system="linux",
        machine="aarch64",
        readiness_probe=lambda check: True,
        require_root=False,
    )

    with first.exclusive_activation():
        with pytest.raises(LinuxDeploymentError, match="already in progress"):
            with second.exclusive_activation():
                raise AssertionError("concurrent activation entered critical section")


def test_receipt_is_atomic_machine_readable_evidence(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    previous = host.preflight(release)
    snapshot = host.create_snapshot(release, previous)
    host.write_receipt(
        ActivationReceipt(
            release_id=release.release_id,
            status=ActivationStatus.ACTIVATED,
            transaction_id=snapshot.transaction_id,
            previous_targets=MappingProxyType(dict(previous)),
        )
    )

    receipt = json.loads((Path(snapshot.backup_path) / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "activated"
    assert receipt["release_id"] == release.release_id
    assert not list(Path(snapshot.backup_path).glob("*.tmp"))


def test_snapshot_can_be_loaded_by_a_new_operator_process(tmp_path: Path) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    previous = host.preflight(release)
    snapshot = host.create_snapshot(release, previous)
    fresh_host = LinuxDeploymentHost(
        root=root,
        runner=FakeRunner(),
        system="linux",
        machine="aarch64",
        readiness_probe=lambda check: True,
        require_root=False,
    )

    loaded = fresh_host.load_snapshot(release, Path(snapshot.backup_path))

    assert loaded == snapshot
    fresh_host.write_receipt(
        ActivationReceipt(
            release_id=release.release_id,
            status=ActivationStatus.RESTORED,
            transaction_id=loaded.transaction_id,
            previous_targets=loaded.previous_targets,
        )
    )
    assert (Path(snapshot.backup_path) / "receipt.json").is_file()


def test_restore_validates_backups_before_stopping_services(tmp_path: Path) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    previous = host.preflight(release)
    snapshot = host.create_snapshot(release, previous)
    first_asset = release.system_assets[0].destination.relative_to("/")
    (Path(snapshot.backup_path) / "assets" / first_asset).unlink()
    runner.calls.clear()

    with pytest.raises(LinuxDeploymentError, match="backup is missing"):
        host.restore(release, snapshot)

    assert not any(call[:2] == ("/usr/bin/systemctl", "stop") for call in runner.calls)


def test_restore_rejects_snapshot_without_ownership_before_stopping_services(
    tmp_path: Path,
) -> None:
    _, release, host, runner = prepared_release(tmp_path)
    previous = host.preflight(release)
    snapshot = host.create_snapshot(release, previous)
    metadata_path = Path(snapshot.backup_path) / "snapshot.json"
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    document["system_assets"][0].pop("uid")
    metadata_path.write_text(json.dumps(document), encoding="utf-8")
    runner.calls.clear()

    with pytest.raises(LinuxDeploymentError, match="asset state is invalid"):
        host.restore(release, snapshot)

    assert not any(call[:2] == ("/usr/bin/systemctl", "stop") for call in runner.calls)


def test_receipt_and_snapshot_lookup_reject_unknown_or_unsafe_transactions(
    tmp_path: Path,
) -> None:
    root, release, host, _ = prepared_release(tmp_path)
    with pytest.raises(LinuxDeploymentError, match="no transaction id"):
        host.write_receipt(
            ActivationReceipt(
                release_id=release.release_id,
                status=ActivationStatus.DRY_RUN,
                transaction_id=None,
                previous_targets={},
            )
        )
    with pytest.raises(LinuxDeploymentError, match="unknown"):
        host.write_receipt(
            ActivationReceipt(
                release_id=release.release_id,
                status=ActivationStatus.ACTIVATED,
                transaction_id="unknown",
                previous_targets={},
            )
        )
    with pytest.raises(LinuxDeploymentError, match="outside"):
        host.load_snapshot(release, tmp_path / "outside")


def test_default_http_readiness_probe_accepts_only_ready_json(monkeypatch) -> None:
    class Response:
        def __init__(self, status: int, payload: bytes) -> None:
            self.status = status
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class Connection:
        response = Response(200, b'{"status":"ready"}')

        def __init__(self, *args, **kwargs) -> None:
            pass

        def request(self, method: str, path: str) -> None:
            assert method == "GET"

        def getresponse(self):
            return self.response

        def close(self) -> None:
            pass

    from eidolon_deploy import linux

    monkeypatch.setattr(linux.http.client, "HTTPConnection", Connection)
    check = release_descriptor_from_document(release_document()).readiness_checks[1]
    assert LinuxDeploymentHost._probe_readiness(check)

    Connection.response = Response(503, b"{}")
    assert not LinuxDeploymentHost._probe_readiness(check)
    Connection.response = Response(200, b"not-json")
    assert not LinuxDeploymentHost._probe_readiness(check)


def test_https_readiness_uses_descriptor_status_over_loopback(monkeypatch) -> None:
    class Response:
        status = 200

        @staticmethod
        def read() -> bytes:
            return b'{"status":"ok"}'

    class Connection:
        def __init__(self, host, port, *, timeout, context) -> None:
            assert host == "127.0.0.1"
            assert port == 9002
            assert timeout == 2
            assert context.check_hostname is False

        @staticmethod
        def request(method: str, path: str) -> None:
            assert (method, path) == ("GET", "/healthz")

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close() -> None:
            pass

    from eidolon_deploy import linux

    monkeypatch.setattr(linux.http.client, "HTTPSConnection", Connection)
    check = next(
        item
        for item in release_descriptor_from_document(release_document()).readiness_checks
        if item.check_id == "local-api"
    )
    assert LinuxDeploymentHost._probe_readiness(check)


def test_tcp_and_systemd_readiness_are_bounded(monkeypatch) -> None:
    from eidolon_deploy import linux

    checks = release_descriptor_from_document(release_document()).readiness_checks
    tcp = next(item for item in checks if item.check_id == "livekit")
    systemd = next(item for item in checks if item.check_id == "channel")
    calls: list[tuple[str, int]] = []

    def connected(address, *, timeout):
        calls.append(address)
        assert timeout == 2
        return contextlib.nullcontext()

    monkeypatch.setattr(linux.socket, "create_connection", connected)
    assert LinuxDeploymentHost._probe_readiness(tcp)
    assert calls == [("127.0.0.1", 7880)]

    monkeypatch.setattr(
        linux.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("closed")),
    )
    assert not LinuxDeploymentHost._probe_readiness(tcp)

    monkeypatch.setattr(
        linux.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0 if command[-1] == "eidolon-channel.service" else 1
        ),
    )
    assert LinuxDeploymentHost._probe_readiness(systemd)
    monkeypatch.setattr(
        linux.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing systemctl")),
    )
    assert not LinuxDeploymentHost._probe_readiness(systemd)


def test_http_2xx_readiness_does_not_require_json(monkeypatch) -> None:
    class Response:
        status = 200

        @staticmethod
        def read() -> bytes:
            return b"not-json"

    class Connection:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @staticmethod
        def request(_method: str, _path: str) -> None:
            pass

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close() -> None:
            pass

    from eidolon_deploy import linux

    monkeypatch.setattr(linux.http.client, "HTTPConnection", Connection)
    check = next(
        item
        for item in release_descriptor_from_document(release_document()).readiness_checks
        if item.check_id == "memory"
    )
    assert LinuxDeploymentHost._probe_readiness(check)


def test_unix_http_readiness_preserves_query(monkeypatch) -> None:
    class Response:
        status = 200

        @staticmethod
        def read() -> bytes:
            return b'{"status":"ready"}'

    class Connection:
        def __init__(self, socket_path) -> None:
            assert socket_path == Path("/run/eidolon/system.sock")

        @staticmethod
        def request(method: str, path: str) -> None:
            assert (method, path) == ("GET", "/health?detail=1")

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close() -> None:
            pass

    from eidolon_deploy import linux

    monkeypatch.setattr(linux, "_UnixHTTPConnection", Connection)
    original = next(
        item
        for item in release_descriptor_from_document(release_document()).readiness_checks
        if item.check_id == "eidolond"
    )
    check = replace(original, url="http://eidolond/health?detail=1")
    assert LinuxDeploymentHost._probe_readiness(check)
