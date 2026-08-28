from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType

from eidolon_deploy import cli
from eidolon_deploy.activation import (
    ActivationFailed,
    ActivationReceipt,
    ActivationStatus,
    RollbackFailed,
)
from eidolon_deploy.bundle import BuiltBundle
from eidolon_deploy.ports import DeploymentSnapshot
from tests.deploy.support import release_document, write_release_document


class FakeHost:
    @staticmethod
    def exclusive_activation():
        return nullcontext()

    def load_snapshot(self, release, path: Path) -> DeploymentSnapshot:
        return DeploymentSnapshot(
            transaction_id="tx-cli",
            previous_targets={
                "eidolon_kernel": "/opt/eidolon/releases/old/eidolon_kernel",
                "eidolon_data": "/opt/eidolon/releases/old/eidolon_data",
                "eidolon_hub": "/opt/eidolon/releases/old/eidolon_hub",
                "eidolon_admin": "/opt/eidolon/releases/old/eidolon_admin",
            },
            backup_path=str(path),
        )

    @staticmethod
    def doctor(release) -> dict[str, object]:
        return {
            "release_id": release.release_id,
            "active_targets": {},
            "units": (),
            "readiness_checks": (),
        }


class FakeActivator:
    failure: str | None = None

    def __init__(self, host) -> None:
        self.host = host

    def activate(self, release, *, dry_run: bool):
        if self.failure == "activation":
            raise ActivationFailed(_receipt(ActivationStatus.ROLLED_BACK))
        if self.failure == "rollback":
            raise RollbackFailed(activation_error="activate", rollback_error="restore")
        return _receipt(ActivationStatus.DRY_RUN if dry_run else ActivationStatus.ACTIVATED)

    def rollback(self, release, snapshot):
        if self.failure == "rollback":
            raise RollbackFailed(activation_error="activate", rollback_error="restore")
        return _receipt(ActivationStatus.RESTORED)


def _receipt(status: ActivationStatus) -> ActivationReceipt:
    return ActivationReceipt(
        release_id="20260806-m2d-test",
        status=status,
        transaction_id=None if status is ActivationStatus.DRY_RUN else "tx-cli",
        previous_targets=MappingProxyType(
            {
                "eidolon_kernel": "/opt/eidolon/releases/old/eidolon_kernel",
                "eidolon_data": "/opt/eidolon/releases/old/eidolon_data",
                "eidolon_hub": "/opt/eidolon/releases/old/eidolon_hub",
                "eidolon_admin": "/opt/eidolon/releases/old/eidolon_admin",
            }
        ),
    )


def test_cli_seals_prepared_release(monkeypatch, capsys, tmp_path: Path) -> None:
    descriptor = tmp_path / "release.json"
    captured = {}

    def fake_seal(**arguments):
        captured.update(arguments)
        return descriptor

    monkeypatch.setattr(cli, "seal_prepared_release", fake_seal)

    result = cli.main(
        [
            "seal",
            "20260806-m2d-test",
            "--kernel-revision",
            "a" * 40,
            "--data-revision",
            "b" * 40,
            "--hub-revision",
            "d" * 40,
            "--admin-revision",
            "e" * 40,
            "--agent-revision",
            "f" * 40,
            "--channel-revision",
            "1" * 40,
            "--memory-revision",
            "2" * 40,
            "--sdk-revision",
            "c" * 40,
        ]
    )

    assert result == 0
    assert captured["release_id"] == "20260806-m2d-test"
    assert captured["revisions"].hub == "d" * 40
    assert captured["revisions"].admin == "e" * 40
    assert json.loads(capsys.readouterr().out)["status"] == "sealed"


def _bundle_argv(tmp_path: Path) -> list[str]:
    return [
        "bundle",
        "20260807-bundle",
        str(tmp_path / "bundle"),
        "--kernel-repo",
        str(tmp_path / "kernel"),
        "--data-repo",
        str(tmp_path / "data"),
        "--hub-repo",
        str(tmp_path / "hub"),
        "--admin-repo",
        str(tmp_path / "admin"),
        "--agent-repo",
        str(tmp_path / "agent"),
        "--channel-repo",
        str(tmp_path / "channel"),
        "--memory-repo",
        str(tmp_path / "memory"),
        "--sdk-repo",
        str(tmp_path / "sdk"),
        "--kernel-revision",
        "a" * 40,
        "--data-revision",
        "b" * 40,
        "--hub-revision",
        "d" * 40,
        "--admin-revision",
        "e" * 40,
        "--agent-revision",
        "f" * 40,
        "--channel-revision",
        "1" * 40,
        "--memory-revision",
        "2" * 40,
        "--sdk-revision",
        "c" * 40,
    ]


def test_cli_builds_commit_pinned_source_bundle(monkeypatch, capsys, tmp_path: Path) -> None:
    manifest = tmp_path / "bundle/bundle.json"
    captured = {}

    def fake_build(**arguments):
        captured.update(arguments)
        return BuiltBundle(manifest=manifest, notes=())

    monkeypatch.setattr(cli, "build_source_bundle", fake_build)
    result = cli.main(_bundle_argv(tmp_path))

    assert result == 0
    assert captured["revisions"].admin == "e" * 40
    assert captured["repositories"]["eidolon_hub"] == tmp_path / "hub"
    assert json.loads(capsys.readouterr().out) == {
        "status": "bundled",
        "manifest": str(manifest),
        "notes": [],
    }


def test_cli_reports_how_the_bundle_was_built(monkeypatch, capsys, tmp_path: Path) -> None:
    """A note about this workstation must reach the operator, not just the log.

    ops appends this document verbatim into the release phases it records on the
    Host, so a rebuilt dependency cache stays visible after the fact.
    """

    manifest = tmp_path / "bundle/bundle.json"

    def fake_build(**arguments):
        return BuiltBundle(
            manifest=manifest,
            notes=("discarded the kept uv cache at /a: it was built at /b",),
        )

    monkeypatch.setattr(cli, "build_source_bundle", fake_build)
    result = cli.main(_bundle_argv(tmp_path))

    assert result == 0
    assert json.loads(capsys.readouterr().out)["notes"] == [
        "discarded the kept uv cache at /a: it was built at /b"
    ]


def test_cli_dry_run_and_explicit_rollback(monkeypatch, capsys, tmp_path: Path) -> None:
    descriptor = tmp_path / "release.json"
    write_release_document(descriptor, release_document())
    monkeypatch.setattr(cli, "LinuxDeploymentHost", FakeHost)
    monkeypatch.setattr(cli, "ReleaseActivator", FakeActivator)

    assert cli.main(["activate", str(descriptor), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"

    assert cli.main(["deploy", str(descriptor), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"

    assert cli.main(["doctor", str(descriptor)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "healthy"

    assert cli.main(["rollback", str(descriptor), "/var/lib/eidolon/deployments/tx"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "restored"


def test_cli_reports_activation_and_rollback_failures(monkeypatch, capsys, tmp_path: Path) -> None:
    descriptor = tmp_path / "release.json"
    write_release_document(descriptor, release_document())
    monkeypatch.setattr(cli, "LinuxDeploymentHost", FakeHost)
    monkeypatch.setattr(cli, "ReleaseActivator", FakeActivator)

    FakeActivator.failure = "activation"
    try:
        assert cli.main(["activate", str(descriptor)]) == 2
        assert json.loads(capsys.readouterr().err)["status"] == "rolled_back"

        FakeActivator.failure = "rollback"
        assert cli.main(["rollback", str(descriptor), "/var/lib/eidolon/deployments/tx"]) == 3
        assert json.loads(capsys.readouterr().err)["status"] == "rollback_failed"
    finally:
        FakeActivator.failure = None


def test_cli_reports_contract_failure_without_traceback(capsys, tmp_path: Path) -> None:
    result = cli.main(["activate", str(tmp_path / "missing.json")])

    assert result == 1
    document = json.loads(capsys.readouterr().err)
    assert document["status"] == "failed"
    assert "missing.json" in document["error"]


def test_contract_reports_the_formats_the_activator_speaks(capsys) -> None:
    """Operator tooling verifies interoperability without inspecting the repository."""

    assert cli.main(["contract"]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["tool"] == "eidolon-release"
    assert document["activator_relative_path"] == ".release/bin/eidolon-release"
    assert document["interpreter_relative_path"] == ".release/bin/python"
    assert len(document["package_digest"]) == 64
    assert document["bundle_schema_version"] == 3
    assert document["descriptor_schema_version"] == 2
