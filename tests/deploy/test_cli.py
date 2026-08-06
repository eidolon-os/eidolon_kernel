from __future__ import annotations

import json
from pathlib import Path

from eidolon_deploy import cli
from eidolon_deploy.activation import (
    ActivationFailed,
    ActivationReceipt,
    ActivationStatus,
    RollbackFailed,
)
from eidolon_deploy.ports import DeploymentSnapshot
from tests.deploy.support import release_document, write_release_document


class FakeHost:
    def load_snapshot(self, release, path: Path) -> DeploymentSnapshot:
        return DeploymentSnapshot(
            transaction_id="tx-cli",
            previous_targets={
                "eidolon_kernel": "/srv/eidolon/releases/old/eidolon_kernel",
                "eidolon_data": "/srv/eidolon/releases/old/eidolon_data",
            },
            backup_path=str(path),
        )


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
        previous_targets={
            "eidolon_kernel": "/srv/eidolon/releases/old/eidolon_kernel",
            "eidolon_data": "/srv/eidolon/releases/old/eidolon_data",
        },
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
            "--sdk-revision",
            "c" * 40,
        ]
    )

    assert result == 0
    assert captured["release_id"] == "20260806-m2d-test"
    assert json.loads(capsys.readouterr().out)["status"] == "sealed"


def test_cli_dry_run_and_explicit_rollback(monkeypatch, capsys, tmp_path: Path) -> None:
    descriptor = tmp_path / "release.json"
    write_release_document(descriptor, release_document())
    monkeypatch.setattr(cli, "LinuxDeploymentHost", FakeHost)
    monkeypatch.setattr(cli, "ReleaseActivator", FakeActivator)

    assert cli.main(["activate", str(descriptor), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"

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
        assert cli.main(
            ["rollback", str(descriptor), "/var/lib/eidolon/deployments/tx"]
        ) == 3
        assert json.loads(capsys.readouterr().err)["status"] == "rollback_failed"
    finally:
        FakeActivator.failure = None


def test_cli_reports_contract_failure_without_traceback(capsys, tmp_path: Path) -> None:
    result = cli.main(["activate", str(tmp_path / "missing.json")])

    assert result == 1
    document = json.loads(capsys.readouterr().err)
    assert document["status"] == "failed"
    assert "missing.json" in document["error"]
