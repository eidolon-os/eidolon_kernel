"""Fail-closed activation state machine with explicit rollback."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from eidolon_deploy.manifest import ReleaseDescriptor
from eidolon_deploy.ports import DeploymentHost, DeploymentSnapshot


class ActivationStatus(str, Enum):
    DRY_RUN = "dry_run"
    ACTIVATED = "activated"
    ROLLED_BACK = "rolled_back"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    release_id: str
    status: ActivationStatus
    transaction_id: str | None
    previous_targets: Mapping[str, str]
    error: str | None = None


def receipt_to_document(receipt: ActivationReceipt) -> dict[str, object]:
    """Map immutable application evidence to a JSON-ready wire document."""

    return {
        "release_id": receipt.release_id,
        "status": receipt.status.value,
        "transaction_id": receipt.transaction_id,
        "previous_targets": dict(receipt.previous_targets),
        "error": receipt.error,
    }


class ActivationFailed(RuntimeError):
    def __init__(self, receipt: ActivationReceipt) -> None:
        super().__init__(f"release activation failed and rolled back: {receipt.error}")
        self.receipt = receipt


class RollbackFailed(RuntimeError):
    def __init__(self, *, activation_error: str, rollback_error: str) -> None:
        super().__init__(
            f"release activation failed ({activation_error}); rollback also failed ({rollback_error})"
        )
        self.activation_error = activation_error
        self.rollback_error = rollback_error


class ReleaseActivator:
    def __init__(self, host: DeploymentHost) -> None:
        self._host = host

    def activate(
        self,
        release: ReleaseDescriptor,
        *,
        dry_run: bool = False,
    ) -> ActivationReceipt:
        with self._host.exclusive_activation():
            return self._activate_locked(release, dry_run=dry_run)

    def _activate_locked(
        self,
        release: ReleaseDescriptor,
        *,
        dry_run: bool,
    ) -> ActivationReceipt:
        previous_targets = dict(self._host.preflight(release))
        if dry_run:
            return ActivationReceipt(
                release_id=release.release_id,
                status=ActivationStatus.DRY_RUN,
                transaction_id=None,
                previous_targets=MappingProxyType(previous_targets),
            )

        snapshot = self._host.create_snapshot(release, previous_targets)
        try:
            self._host.quiesce(release)
            self._host.install_assets(release)
            self._host.switch_components(release)
            self._host.reload_systemd()
            self._host.start_manager()
            self._host.wait_ready(release)
        except Exception as activation_exc:
            try:
                self._host.restore(release, snapshot)
            except Exception as rollback_exc:
                raise RollbackFailed(
                    activation_error=str(activation_exc),
                    rollback_error=str(rollback_exc),
                ) from rollback_exc
            receipt = ActivationReceipt(
                release_id=release.release_id,
                status=ActivationStatus.ROLLED_BACK,
                transaction_id=snapshot.transaction_id,
                previous_targets=MappingProxyType(previous_targets),
                error=str(activation_exc),
            )
            self._host.write_receipt(receipt)
            raise ActivationFailed(receipt) from activation_exc

        receipt = ActivationReceipt(
            release_id=release.release_id,
            status=ActivationStatus.ACTIVATED,
            transaction_id=snapshot.transaction_id,
            previous_targets=MappingProxyType(previous_targets),
        )
        self._host.write_receipt(receipt)
        return receipt

    def rollback(
        self,
        release: ReleaseDescriptor,
        snapshot: DeploymentSnapshot,
    ) -> ActivationReceipt:
        """Explicitly restore a previously captured transaction snapshot."""

        with self._host.exclusive_activation():
            self._host.restore(release, snapshot)
            receipt = ActivationReceipt(
                release_id=release.release_id,
                status=ActivationStatus.RESTORED,
                transaction_id=snapshot.transaction_id,
                previous_targets=MappingProxyType(dict(snapshot.previous_targets)),
            )
            self._host.write_receipt(receipt)
            return receipt
