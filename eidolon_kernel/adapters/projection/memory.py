"""Rebuildable, typed Device Mount projection."""

from __future__ import annotations

import threading

from eidolon_kernel.domain.model import DeviceMount


class InMemoryMountProjection:
    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._mounts: dict[str, DeviceMount] = {}

    def rebuild(self, mounts: tuple[DeviceMount, ...]) -> None:
        with self._mutex:
            self._mounts = {mount.device_id: mount for mount in mounts}

    def put(self, mount: DeviceMount) -> None:
        with self._mutex:
            current = self._mounts.get(mount.device_id)
            if current is None or mount.revision >= current.revision:
                self._mounts[mount.device_id] = mount

    def get(self, device_id: str) -> DeviceMount | None:
        with self._mutex:
            return self._mounts.get(device_id)

    def list(
        self,
        *,
        owner_id: str,
        companion_id: str | None,
        active_only: bool,
        after_device_id: str | None,
        limit: int,
    ) -> tuple[DeviceMount, ...]:
        with self._mutex:
            values = tuple(self._mounts.values())
        return tuple(
            mount
            for mount in sorted(values, key=lambda item: item.device_id)
            if mount.owner_id == owner_id
            and (
                companion_id is None
                or mount.attached_companion_id == companion_id
            )
            and (not active_only or mount.active)
            and (after_device_id is None or mount.device_id > after_device_id)
        )[:limit]
