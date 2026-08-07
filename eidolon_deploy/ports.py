"""Host operation boundary for offline target release activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ContextManager, Mapping, Protocol

if TYPE_CHECKING:
    from eidolon_deploy.activation import ActivationReceipt
    from eidolon_deploy.manifest import ReleaseDescriptor


@dataclass(frozen=True, slots=True)
class DeploymentSnapshot:
    transaction_id: str
    previous_targets: Mapping[str, str]
    backup_path: str


class DeploymentHost(Protocol):
    def exclusive_activation(self) -> ContextManager[None]: ...

    def preflight(self, release: ReleaseDescriptor) -> Mapping[str, str]: ...

    def create_snapshot(
        self,
        release: ReleaseDescriptor,
        previous_targets: Mapping[str, str],
    ) -> DeploymentSnapshot: ...

    def quiesce(self, release: ReleaseDescriptor) -> None: ...

    def install_assets(self, release: ReleaseDescriptor) -> None: ...

    def switch_components(self, release: ReleaseDescriptor) -> None: ...

    def reload_systemd(self) -> None: ...

    def start_release(self, release: ReleaseDescriptor) -> None: ...

    def wait_ready(self, release: ReleaseDescriptor) -> None: ...

    def restore(self, release: ReleaseDescriptor, snapshot: DeploymentSnapshot) -> None: ...

    def write_receipt(self, receipt: ActivationReceipt) -> None: ...
