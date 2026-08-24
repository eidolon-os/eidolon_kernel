from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field

import pytest

from eidolon_deploy.activation import (
    ActivationFailed,
    ActivationReceipt,
    ActivationStatus,
    ForwardFixRequired,
    ReleaseActivator,
    RollbackFailed,
)
from eidolon_deploy.manifest import release_descriptor_from_document
from eidolon_deploy.ports import DeploymentSnapshot
from tests.deploy.support import release_document


@dataclass
class FakeDeploymentHost:
    fail_at: str | None = None
    rollback_fails: bool = False
    calls: list[str] = field(default_factory=list)
    receipts: list[ActivationReceipt] = field(default_factory=list)

    def exclusive_activation(self):
        return nullcontext()

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"failed at {name}")

    def preflight(self, release) -> dict[str, str]:
        self._call("preflight")
        return {
            "eidolon_kernel": "/opt/eidolon/releases/old/eidolon_kernel",
            "eidolon_data": "/opt/eidolon/releases/old/eidolon_data",
            "eidolon_hub": "/opt/eidolon/releases/old/eidolon_hub",
            "eidolon_admin": "/opt/eidolon/releases/old/eidolon_admin",
        }

    def create_snapshot(self, release, previous_targets) -> DeploymentSnapshot:
        self._call("snapshot")
        return DeploymentSnapshot(
            transaction_id="tx-test",
            previous_targets=previous_targets,
            backup_path="/var/lib/eidolon/deployments/tx-test",
        )

    def quiesce(self, release) -> None:
        self._call("quiesce")

    def install_assets(self, release) -> None:
        self._call("assets")

    def switch_components(self, release) -> None:
        self._call("switch")

    def reload_systemd(self) -> None:
        self._call("reload")

    def start_release(self, release) -> None:
        self._call("start")

    def wait_ready(self, release) -> None:
        self._call("ready")

    def restore(self, release, snapshot) -> None:
        self.calls.append("restore")
        if self.rollback_fails:
            raise RuntimeError("restore failed")

    def write_receipt(self, receipt: ActivationReceipt) -> None:
        self.receipts.append(receipt)
        self.calls.append(f"receipt:{receipt.status.value}")


def descriptor():
    return release_descriptor_from_document(release_document())


def forward_descriptor():
    document = release_document()
    document["cutover_mode"] = "forward-only"
    return release_descriptor_from_document(document)


def test_dry_run_is_read_only() -> None:
    host = FakeDeploymentHost()

    receipt = ReleaseActivator(host).activate(descriptor(), dry_run=True)

    assert receipt.status is ActivationStatus.DRY_RUN
    assert host.calls == ["preflight"]
    assert host.receipts == []


def test_activation_orders_mutations_and_records_success() -> None:
    host = FakeDeploymentHost()

    receipt = ReleaseActivator(host).activate(descriptor())

    assert receipt.status is ActivationStatus.ACTIVATED
    assert host.calls == [
        "preflight",
        "snapshot",
        "quiesce",
        "assets",
        "switch",
        "reload",
        "start",
        "ready",
        "receipt:activated",
    ]
    assert receipt.previous_targets["eidolon_kernel"].endswith("/eidolon_kernel")


@pytest.mark.parametrize("fail_at", ["quiesce", "assets", "switch", "reload", "start", "ready"])
def test_any_activation_failure_restores_previous_release(fail_at: str) -> None:
    host = FakeDeploymentHost(fail_at=fail_at)

    with pytest.raises(ActivationFailed) as captured:
        ReleaseActivator(host).activate(descriptor())

    assert captured.value.receipt.status is ActivationStatus.ROLLED_BACK
    assert "restore" in host.calls
    assert host.calls[-1] == "receipt:rolled_back"


def test_preflight_or_snapshot_failure_never_attempts_rollback() -> None:
    for fail_at in ("preflight", "snapshot"):
        host = FakeDeploymentHost(fail_at=fail_at)
        with pytest.raises(RuntimeError, match=fail_at):
            ReleaseActivator(host).activate(descriptor())
        assert "restore" not in host.calls


def test_rollback_failure_is_reported_without_claiming_recovery() -> None:
    host = FakeDeploymentHost(fail_at="ready", rollback_fails=True)

    with pytest.raises(RollbackFailed) as captured:
        ReleaseActivator(host).activate(descriptor())

    assert captured.value.activation_error == "failed at ready"
    assert captured.value.rollback_error == "restore failed"
    assert host.receipts == []


def test_explicit_rollback_restores_snapshot_and_records_result() -> None:
    host = FakeDeploymentHost()
    snapshot = DeploymentSnapshot(
        transaction_id="tx-previous",
        previous_targets={
            "eidolon_kernel": "/opt/eidolon/releases/old/eidolon_kernel",
            "eidolon_data": "/opt/eidolon/releases/old/eidolon_data",
            "eidolon_hub": "/opt/eidolon/releases/old/eidolon_hub",
            "eidolon_admin": "/opt/eidolon/releases/old/eidolon_admin",
        },
        backup_path="/var/lib/eidolon/deployments/tx-previous",
    )

    receipt = ReleaseActivator(host).rollback(descriptor(), snapshot)

    assert receipt.status is ActivationStatus.RESTORED
    assert host.calls == ["restore", "receipt:restored"]
    assert host.receipts == [receipt]


@pytest.mark.parametrize("fail_at", ["quiesce", "assets", "switch", "reload"])
def test_forward_only_failure_before_persistent_barrier_restores_previous_targets(
    fail_at: str,
) -> None:
    host = FakeDeploymentHost(fail_at=fail_at)

    with pytest.raises(ActivationFailed):
        ReleaseActivator(host).activate(forward_descriptor())

    assert "restore" in host.calls
    assert host.receipts[-1].persistent_state_mutated is False


@pytest.mark.parametrize("fail_at", ["start", "ready"])
def test_forward_only_failure_after_persistent_barrier_requires_forward_fix(
    fail_at: str,
) -> None:
    host = FakeDeploymentHost(fail_at=fail_at)

    with pytest.raises(ForwardFixRequired) as captured:
        ReleaseActivator(host).activate(forward_descriptor())

    assert "restore" not in host.calls
    assert captured.value.receipt.status is ActivationStatus.FORWARD_FIX_REQUIRED
    assert captured.value.receipt.persistent_state_mutated is True
    assert host.receipts == [captured.value.receipt]


def test_explicit_rollback_is_forbidden_for_forward_only_release() -> None:
    host = FakeDeploymentHost()
    snapshot = DeploymentSnapshot(
        transaction_id="tx-forward",
        previous_targets=host.preflight(forward_descriptor()),
        backup_path="/var/lib/eidolon/deployments/tx-forward",
    )
    host.calls.clear()

    with pytest.raises(ForwardFixRequired):
        ReleaseActivator(host).rollback(forward_descriptor(), snapshot)

    assert host.calls == []
