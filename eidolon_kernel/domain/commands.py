"""Validated mutation commands independent of any wire representation."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_kernel.domain.errors import InvalidRequest
from eidolon_kernel.domain.model import request_fingerprint, require_identifier


@dataclass(frozen=True, slots=True)
class MountDeviceCommand:
    request_id: str
    device_id: str
    owner_id: str
    companion_id: str
    expected_revision: int
    replace_existing: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_identifier("request_id", self.request_id, 96))
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        object.__setattr__(
            self, "companion_id", require_identifier("companion_id", self.companion_id, 64)
        )
        if self.expected_revision < 0:
            raise InvalidRequest("expected_revision cannot be negative")

    @property
    def fingerprint(self) -> str:
        return request_fingerprint(
            "device.mount",
            {
                "device_id": self.device_id,
                "owner_id": self.owner_id,
                "companion_id": self.companion_id,
                "expected_revision": self.expected_revision,
                "replace_existing": self.replace_existing,
            },
        )


@dataclass(frozen=True, slots=True)
class UnmountDeviceCommand:
    request_id: str
    device_id: str
    owner_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_identifier("request_id", self.request_id, 96))
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        if self.expected_revision < 1:
            raise InvalidRequest("unmount expected_revision must be positive")

    @property
    def fingerprint(self) -> str:
        return request_fingerprint(
            "device.unmount",
            {
                "device_id": self.device_id,
                "owner_id": self.owner_id,
                "expected_revision": self.expected_revision,
            },
        )
