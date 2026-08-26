"""Validated mutation commands independent of any wire representation."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_kernel.domain.errors import InvalidRequest
from eidolon_kernel.domain.model import request_fingerprint, require_identifier

#: Which act this replacement is part of. A caller names the *act* — never the
#: provenance — and this authority derives the provenance from it in one place.
#: The difference matters because the same committed state, "nobody answers
#: here", reaches an Owner as three different sentences: I cleared it, the
#: Eidolon it answered as was put away, or the Host let it go.
#:
#: ``owner`` is a person acting on this Body. ``companion-lifecycle`` is a
#: consequence of a Companion being put away, which only the coordinator running
#: that workflow knows it is doing. ``reconciler`` is this Kernel converging on
#: its own.
ASSIGNMENT_ORIGIN_OWNER = "owner"
ASSIGNMENT_ORIGIN_COMPANION_LIFECYCLE = "companion-lifecycle"
ASSIGNMENT_ORIGIN_RECONCILER = "reconciler"
ASSIGNMENT_ORIGINS = (
    ASSIGNMENT_ORIGIN_OWNER,
    ASSIGNMENT_ORIGIN_COMPANION_LIFECYCLE,
    ASSIGNMENT_ORIGIN_RECONCILER,
)


@dataclass(frozen=True, slots=True)
class MountDeviceCommand:
    request_id: str
    device_id: str
    owner_id: str
    expected_revision: int
    replace_existing: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_identifier("request_id", self.request_id, 96))
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        if self.expected_revision < 0:
            raise InvalidRequest("expected_revision cannot be negative")

    @property
    def fingerprint(self) -> str:
        return request_fingerprint(
            "device.mount",
            {
                "device_id": self.device_id,
                "owner_id": self.owner_id,
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


@dataclass(frozen=True, slots=True)
class ReplaceAssignmentCommand:
    """Point one Body at one Companion, or at nobody.

    ``origin`` says which act this is part of, and ``selection_provenance`` is
    derived from it rather than sent. A caller that could write the provenance
    directly would be asserting *why* something happened; naming the act it is
    performing is reporting *what* it is doing, which it is the only one that
    knows.
    """

    request_id: str
    owner_id: str
    body_endpoint_id: str
    expected_assignment_revision: int
    companion_id: str | None
    origin: str
    change_reason: str | None = None
    policy_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", require_identifier("request_id", self.request_id, 96)
        )
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        object.__setattr__(
            self,
            "body_endpoint_id",
            require_identifier("body_endpoint_id", self.body_endpoint_id),
        )
        if self.companion_id is not None:
            object.__setattr__(
                self,
                "companion_id",
                require_identifier("companion_id", self.companion_id, 64),
            )
        if self.expected_assignment_revision < 0:
            raise InvalidRequest("expected_assignment_revision cannot be negative")
        if self.origin not in ASSIGNMENT_ORIGINS:
            raise InvalidRequest("assignment origin is not one this Kernel defines")
        if self.companion_id is not None and self.origin != ASSIGNMENT_ORIGIN_OWNER:
            # Refused by construction rather than declined by convention. Only a
            # person adds an Eidolon to a Body; a Host that could would be a
            # second production path for "who answers here" that nobody sees
            # happen. Every other origin can only release.
            raise InvalidRequest("only an explicit Owner selection can name a Companion")
        if self.change_reason is not None and not self.change_reason.strip():
            raise InvalidRequest("change_reason cannot be blank when given")
        if len(set(self.policy_refs)) != len(self.policy_refs):
            raise InvalidRequest("policy_refs must be unique")

    @property
    def fingerprint(self) -> str:
        return request_fingerprint(
            "body.replace-assignment",
            {
                "body_endpoint_id": self.body_endpoint_id,
                "owner_id": self.owner_id,
                "companion_id": self.companion_id,
                "expected_assignment_revision": self.expected_assignment_revision,
                "origin": self.origin,
                "policy_refs": list(self.policy_refs),
            },
        )
