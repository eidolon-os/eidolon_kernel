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
    RESTORED_NOT_READY = "restored_not_ready"
    FORWARD_FIX_REQUIRED = "forward_fix_required"


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    release_id: str
    status: ActivationStatus
    transaction_id: str | None
    previous_targets: Mapping[str, str]
    error: str | None = None
    cutover_mode: str = "reversible"
    persistent_state_mutated: bool = False
    database_migrations: tuple[str, ...] = ()


def receipt_to_document(receipt: ActivationReceipt) -> dict[str, object]:
    """Map immutable application evidence to a JSON-ready wire document."""

    return {
        "release_id": receipt.release_id,
        "status": receipt.status.value,
        "transaction_id": receipt.transaction_id,
        "previous_targets": dict(receipt.previous_targets),
        "error": receipt.error,
        "cutover_mode": receipt.cutover_mode,
        "persistent_state_mutated": receipt.persistent_state_mutated,
        "database_migrations": list(receipt.database_migrations),
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


class RestoredButNotReady(RuntimeError):
    """Raised by the host adapter: links restored, readiness did not converge.

    Deliberately not a ``LinuxDeploymentError``: a caller that cannot tell the
    difference must not silently treat this as a failed restore, and one that
    can needs to catch it by name.
    """


class RestoredNotReady(RuntimeError):
    """The Host is on the restored release, and that release will not start.

    Distinct from a failed rollback, which leaves the Host in a state nobody
    has established. Collapsing the two told an operator to go looking for a
    broken Host when what was broken was the release they had just gone back
    to — on hardware, an ops-installed config input the rollback could not
    move back with the code.
    """

    def __init__(self, receipt: ActivationReceipt) -> None:
        super().__init__(
            "release snapshot was restored, but the restored release did not "
            f"become ready: {receipt.error}"
        )
        self.receipt = receipt


class ForwardFixRequired(RuntimeError):
    def __init__(self, receipt: ActivationReceipt) -> None:
        super().__init__(
            "release crossed its forward-only persistent-state barrier; "
            f"old targets were not restored: {receipt.error}"
        )
        self.receipt = receipt


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
                cutover_mode=release.cutover_mode,
                database_migrations=release.database_migrations,
            )

        snapshot = self._host.create_snapshot(release, previous_targets)
        persistent_state_mutated = False
        try:
            self._host.quiesce(release)
            self._host.install_assets(release)
            self._host.switch_components(release)
            self._host.reload_systemd()
            # Starting a forward-only candidate is the last point at which an
            # old interpreter is provably safe. Startup, ExecStartPre, import
            # hooks, or the process itself may commit persistent schema/state
            # before readiness can observe it. Without a verified full state
            # snapshot, any failure from here must keep candidate targets and
            # be repaired on the same target schema.
            persistent_state_mutated = release.cutover_mode == "forward-only"
            self._host.start_release(release)
            self._host.wait_ready(release)
        except Exception as activation_exc:
            if persistent_state_mutated:
                receipt = ActivationReceipt(
                    release_id=release.release_id,
                    status=ActivationStatus.FORWARD_FIX_REQUIRED,
                    transaction_id=snapshot.transaction_id,
                    previous_targets=MappingProxyType(previous_targets),
                    error=str(activation_exc),
                    cutover_mode=release.cutover_mode,
                    persistent_state_mutated=True,
                    database_migrations=release.database_migrations,
                )
                self._host.write_receipt(receipt)
                raise ForwardFixRequired(receipt) from activation_exc
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
                cutover_mode=release.cutover_mode,
                database_migrations=release.database_migrations,
            )
            self._host.write_receipt(receipt)
            raise ActivationFailed(receipt) from activation_exc

        receipt = ActivationReceipt(
            release_id=release.release_id,
            status=ActivationStatus.ACTIVATED,
            transaction_id=snapshot.transaction_id,
            previous_targets=MappingProxyType(previous_targets),
            cutover_mode=release.cutover_mode,
            persistent_state_mutated=persistent_state_mutated,
            database_migrations=release.database_migrations,
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
            if release.cutover_mode == "forward-only":
                raise ForwardFixRequired(
                    ActivationReceipt(
                        release_id=release.release_id,
                        status=ActivationStatus.FORWARD_FIX_REQUIRED,
                        transaction_id=snapshot.transaction_id,
                        previous_targets=MappingProxyType(dict(snapshot.previous_targets)),
                        error="explicit rollback is forbidden for a forward-only release",
                        cutover_mode=release.cutover_mode,
                        persistent_state_mutated=True,
                        database_migrations=release.database_migrations,
                    )
                )
            try:
                self._host.restore(release, snapshot)
            except RestoredButNotReady as exc:
                receipt = ActivationReceipt(
                    release_id=release.release_id,
                    status=ActivationStatus.RESTORED_NOT_READY,
                    transaction_id=snapshot.transaction_id,
                    previous_targets=MappingProxyType(dict(snapshot.previous_targets)),
                    error=str(exc),
                    cutover_mode=release.cutover_mode,
                    database_migrations=release.database_migrations,
                )
                # Written, not discarded: what actually happened is the only
                # thing that tells the next operator where the Host stands.
                self._host.write_receipt(receipt)
                raise RestoredNotReady(receipt) from exc
            receipt = ActivationReceipt(
                release_id=release.release_id,
                status=ActivationStatus.RESTORED,
                transaction_id=snapshot.transaction_id,
                previous_targets=MappingProxyType(dict(snapshot.previous_targets)),
                cutover_mode=release.cutover_mode,
                database_migrations=release.database_migrations,
            )
            self._host.write_receipt(receipt)
            return receipt
