from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

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
        self.fail_command: tuple[str, ...] | None = None

    def run(self, *command: str) -> CommandResult:
        self.calls.append(command)
        if self.fail_command and command[: len(self.fail_command)] == self.fail_command:
            return CommandResult(1, "", "injected command failure")
        if len(command) >= 3 and command[-2] == "-c" and "metadata.distributions" in command[-1]:
            return CommandResult(0, "pip==25.1\neidolon-test==1.0\n", "")
        if command[-2:] == (
            "import platform; print(f'{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}')",
            "",
        ):
            raise AssertionError("unexpected Python probe shape")
        if len(command) >= 3 and command[-2] == "-c" and "python_version_tuple" in command[-1]:
            return CommandResult(0, "3.13\n", "")
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
            f"/srv/eidolon/releases/old/{component['component_id']}",
        )
        old_target.mkdir(parents=True)
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
    }
    assert previous["eidolon_kernel"].endswith("/old/eidolon_kernel")
    verify_call = next(
        call for call in runner.calls if call[:2] == ("/usr/bin/systemd-analyze", "verify")
    )
    assert len(verify_call[2:]) == 7
    assert not any("/etc/avahi/" in item for item in verify_call)
    assert not any(call[:2] == ("/usr/bin/systemctl", "stop") for call in runner.calls)


def test_quiesce_and_start_order_prevents_competing_restart_authorities(
    tmp_path: Path,
) -> None:
    _, release, host, runner = prepared_release(tmp_path)

    host.quiesce(release)
    host.start_release(release)

    systemctl_calls = [call[1:] for call in runner.calls if call[0] == "/usr/bin/systemctl"]
    assert systemctl_calls == [
        ("stop", "eidolon-admin.service"),
        ("stop", "eidolon-local-api.service"),
        ("stop", "eidolon-bootstrapd.service"),
        ("stop", "eidolond.service"),
        ("stop", "eidolon-data.service"),
        ("stop", "eidolon-hub.service"),
        ("stop", "eidolon-kernel.service"),
        ("start", "eidolon-bootstrapd.service"),
        ("start", "eidolond.service"),
        ("start", "eidolon-local-api.service"),
        ("start", "eidolon-admin.service"),
    ]


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
    }
    active_checks = [
        call for call in runner.calls if call[:3] == ("/usr/bin/systemctl", "is-active", "--quiet")
    ]
    assert len(active_checks) == 7


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
    check = release_descriptor_from_document(release_document()).readiness_checks[-1]
    assert LinuxDeploymentHost._probe_readiness(check)
