"""Body endpoints and the assignments that say which Companion answers there.

Why this is a resource and not a column
---------------------------------------

Until now "which Eidolon answers through this speaker" was a field on the
Device Mount. That made three things quietly wrong, and all three are the
reason this module exists:

* **A re-claimed device forgot its Eidolon.** Remounting builds a new mount for
  the new Claim generation, and a field on the mount goes with it. Re-running
  setup on a speaker is supposed to change nothing about who answers through it.
* **Nothing recorded why a Body went quiet.** A cleared field cannot tell the
  Owner apart from the reconciler; a speaker that stops answering with no
  sentence attached is indistinguishable from a broken one.
* **Two Controllers editing one device collided over unrelated facts**, because
  the mount's revision was the compare-and-swap token for both "this device is
  mounted" and "this Eidolon answers here".

Why there is no BodyEndpoint table
----------------------------------

The canonical model derives endpoints from the accepted Manifest through
``ReconcileEndpoints``. **No firmware in this product declares any.** A real
BOX-3 asserts ``{"actions":[],"events":[],"media":[…],"properties":[…]}`` — the
vocabulary has no ``endpoints`` array at all. So on every Host today the set of
endpoints is a pure function of the mount: one Body per mounted device.

Storing that as rows would be a second copy of "is this device mounted", kept in
step by hand. It is derived here instead, and the derivation is named so the day
a Manifest declares endpoints it is a visible switch rather than a silent one:
that day ``ReconcileEndpoints`` becomes the way in, endpoints get their own
table, and :func:`derived_endpoint` is deleted rather than extended.

The assignment *is* stored, keyed by the endpoint id — which is derived from the
device rather than from the mount's revision, which is precisely what lets it
outlive a re-claim.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from eidolon_sdk.device_foundation.v1 import (
    DERIVED_ENDPOINT_ID,
    AssignmentCondition,
    AssignmentMode,
    BodyAssignmentStatus,
    DeviceRef,
    SelectionProvenance,
)

from eidolon_kernel.domain.errors import InvalidRequest
from eidolon_kernel.domain.model import (
    DeviceMount,
    require_identifier,
    require_utc,
)

#: ``DERIVED_ENDPOINT_ID`` is re-exported, not defined here. Composing
#: ``<device_id>:body`` is what any consumer must do to address a Body, so the
#: word belongs with the contract they read rather than in this authority's
#: private domain — where the one consumer that needed it resorted to
#: substring-matching this file.

#: Where an endpoint's declaration came from. ``derived`` is the Host filling in
#: for a Manifest vocabulary that has no endpoints in it; ``manifest`` is a
#: device that declared its own. The distinction is on the record because the
#: two answer "what can this device do" with very different authority.
ENDPOINT_SOURCE_DERIVED = "derived"


def body_endpoint_id(device_id: str, endpoint_id: str) -> str:
    """The canonical ``Identifier`` addressing one Body on one device.

    Composed rather than allocated, for two reasons. It survives re-claiming —
    a new Claim generation produces the same endpoint id, so the assignment it
    keys is still there. And it is resolvable without a lookup table, which is
    what makes the endpoint set derivable rather than stored.
    """

    device_id = require_identifier("device_id", device_id)
    endpoint_id = require_identifier("endpoint_id", endpoint_id, 64)
    composed = f"{device_id}:{endpoint_id}"
    if len(composed) > 128:
        # The canonical Identifier bound. Refused rather than truncated: a
        # shortened id would collide with another device's Body.
        raise InvalidRequest("body_endpoint_id exceeds the canonical identifier length")
    return composed


def device_of(endpoint_id_value: str) -> str:
    """Read back the device a derived endpoint id addresses."""

    device_id, separator, _ = endpoint_id_value.rpartition(":")
    if not separator or not device_id:
        raise InvalidRequest("body_endpoint_id is not a derived endpoint identifier")
    return device_id


@dataclass(frozen=True, slots=True)
class BodyEndpoint:
    """One assignable Body, as this Host can currently describe it.

    ``assignment_policy``, ``risk_class`` and ``concurrency`` are the canonical
    endpoint declaration fields. On a derived endpoint they say what this Host
    actually knows, which is nothing constraining: ``optional`` means no Manifest
    told it a Companion is required or forbidden here, and that is the truth
    rather than a permissive default chosen for convenience.
    """

    body_endpoint_id: str
    device_id: str
    owner_id: str
    endpoint_id: str
    #: The device this Body is derived from, at the generation it was derived
    #: at. Carried so a runtime resolving a Body needs one read rather than two:
    #: it is not a second copy of the mount, it is which mount this endpoint
    #: currently *is*, and a fence for anything that acts on the answer later.
    device_ref: DeviceRef
    mount_revision: int
    roles: tuple[str, ...]
    assignment_policy: str
    risk_class: str
    concurrency: str
    source: str
    #: False when the device this Body belongs to is no longer mounted. The
    #: assignment is deliberately *not* deleted with it (the design calls this
    #: ``CapabilityMissing``): a device that comes back should come back to the
    #: Eidolon it answered as, and a deleted row cannot do that.
    present: bool


def derived_endpoint(mount: DeviceMount) -> BodyEndpoint:
    """The one Body a mounted device has, for as long as Manifests declare none."""

    return BodyEndpoint(
        body_endpoint_id=body_endpoint_id(mount.device_id, DERIVED_ENDPOINT_ID),
        device_id=mount.device_id,
        owner_id=mount.owner_id,
        endpoint_id=DERIVED_ENDPOINT_ID,
        device_ref=mount.device_ref,
        mount_revision=mount.revision,
        roles=("body",),
        assignment_policy="optional",
        risk_class="safe",
        concurrency="exclusive",
        source=ENDPOINT_SOURCE_DERIVED,
        present=mount.active,
    )


@dataclass(frozen=True, slots=True)
class BodyAssignment:
    """Which Companion answers through one Body, and why it says so.

    ``revision`` moves on every commit and is the compare-and-swap token.
    ``generation`` moves only when the spec changes, so a runtime that fenced a
    session on it is not disturbed by a write that changed nothing it depends on.

    There is no ``changed_by``. This authority authenticates a service credential
    and resolves an Owner from it — it never sees a Controller. A ``changed_by``
    filled from what it does know would say "the Owner" for every row including
    the reconciler's own, which is worse than absent.
    """

    body_endpoint_id: str
    device_id: str
    endpoint_id: str
    owner_id: str
    companion_id: str | None
    selection_provenance: str
    change_reason: str | None
    mode: str
    policy_refs: tuple[str, ...]
    revision: int
    generation: int
    created_at: datetime
    updated_at: datetime
    request_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "body_endpoint_id",
            require_identifier("body_endpoint_id", self.body_endpoint_id),
        )
        object.__setattr__(self, "device_id", require_identifier("device_id", self.device_id))
        object.__setattr__(
            self, "endpoint_id", require_identifier("endpoint_id", self.endpoint_id, 64)
        )
        object.__setattr__(self, "owner_id", require_identifier("owner_id", self.owner_id, 64))
        if self.companion_id is not None:
            object.__setattr__(
                self,
                "companion_id",
                require_identifier("companion_id", self.companion_id, 64),
            )
        if self.selection_provenance not in {item.value for item in SelectionProvenance}:
            raise InvalidRequest("selection_provenance is not a canonical value")
        if self.mode != AssignmentMode.DEFAULT.value:
            raise InvalidRequest("V1 defines only the default assignment mode")
        if self.companion_id is None and self.selection_provenance == (
            SelectionProvenance.USER_SELECTED.value
        ):
            raise InvalidRequest("an unassigned Body cannot have been selected by anyone")
        if self.companion_id is not None and self.selection_provenance != (
            SelectionProvenance.USER_SELECTED.value
        ):
            # The rule that a Host may never pick a Companion on its own, stated
            # where it cannot be forgotten. Every other provenance is something
            # the Host did *to* an assignment; only a person adds one. A
            # reconcile that could name a Companion would be a second,
            # invisible production path for "who answers here".
            raise InvalidRequest("only an explicit selection can name a Companion")
        if len(set(self.policy_refs)) != len(self.policy_refs):
            raise InvalidRequest("policy_refs must be unique")
        if min(self.revision, self.generation) < 1:
            raise InvalidRequest("assignment revision and generation must be positive")
        object.__setattr__(
            self, "request_id", require_identifier("request_id", self.request_id, 96)
        )
        object.__setattr__(self, "created_at", require_utc("created_at", self.created_at))
        object.__setattr__(self, "updated_at", require_utc("updated_at", self.updated_at))
        if self.updated_at < self.created_at:
            raise InvalidRequest("updated_at cannot precede created_at")

    @property
    def assignment_id(self) -> str:
        """The canonical ``assignment_id``.

        One per endpoint, because replacing is not delete-then-create: the same
        assignment keeps its identity across every Companion it has pointed at,
        and its revision is the history of that.
        """

        return f"assignment:{self.body_endpoint_id}"

    @property
    def spec(self) -> dict[str, Any]:
        return {
            "companion_ref": self.companion_id,
            "mode": self.mode,
            "policy_refs": list(self.policy_refs),
            "selection_provenance": self.selection_provenance,
            "change_reason": self.change_reason,
        }

    def status(self, *, endpoint: BodyEndpoint | None) -> BodyAssignmentStatus:
        """What is true about this assignment, from facts this authority holds.

        Returns the canonical type rather than a dictionary shaped like one, so
        that "which field says who is answering" is settled here and cannot be
        answered differently by the transport, by a consumer, or by prose.

        Two of the canonical conditions are deliberately never emitted here.
        ``CompanionMissing`` would require asking the Companion authority on
        every read; the reconciler, which can ask, converges the row instead —
        so this surface reports what was committed rather than a guess about
        whether it still stands. ``PolicyDenied`` has no evaluator to deny
        anything on this Host.
        """

        in_force = endpoint is not None and endpoint.present
        conditions: list[AssignmentCondition] = []
        if not in_force:
            conditions.append(AssignmentCondition.CAPABILITY_MISSING)
        elif self.companion_id is not None:
            conditions.append(AssignmentCondition.REALIZED)
        return BodyAssignmentStatus(
            # Equal to ``generation`` because this authority commits the spec and
            # its realization in one transaction — there is no second actor to
            # lag behind. The field is here so the day there is one, a consumer
            # already knows where to look.
            observed_generation=self.generation,
            effective_companion_id=self.companion_id if in_force else None,
            conditions=tuple(conditions),
        )

    @classmethod
    def first(
        cls,
        *,
        endpoint: BodyEndpoint,
        companion_id: str | None,
        selection_provenance: str,
        change_reason: str | None,
        policy_refs: tuple[str, ...],
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> BodyAssignment:
        return cls(
            body_endpoint_id=endpoint.body_endpoint_id,
            device_id=endpoint.device_id,
            endpoint_id=endpoint.endpoint_id,
            owner_id=endpoint.owner_id,
            companion_id=companion_id,
            selection_provenance=selection_provenance,
            change_reason=change_reason,
            mode=AssignmentMode.DEFAULT.value,
            policy_refs=policy_refs,
            revision=1,
            generation=1,
            created_at=at,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
        )

    def replaced(
        self,
        *,
        companion_id: str | None,
        selection_provenance: str,
        change_reason: str | None,
        policy_refs: tuple[str, ...],
        at: datetime,
        request_id: str,
        fingerprint: str,
    ) -> BodyAssignment:
        at = require_utc("replaced_at", at)
        if at < self.updated_at:
            raise InvalidRequest("assignment transition time cannot move backwards")
        spec_changed = (
            companion_id != self.companion_id
            or policy_refs != self.policy_refs
            or selection_provenance != self.selection_provenance
        )
        return replace(
            self,
            companion_id=companion_id,
            selection_provenance=selection_provenance,
            change_reason=change_reason,
            policy_refs=policy_refs,
            revision=self.revision + 1,
            generation=self.generation + 1 if spec_changed else self.generation,
            updated_at=at,
            request_id=request_id,
            fingerprint=fingerprint,
        )

    def states_the_same_as(
        self, *, companion_id: str | None, policy_refs: tuple[str, ...]
    ) -> bool:
        """Whether a request is asking for what is already committed.

        Only the parts a caller can ask for. ``selection_provenance`` is derived
        from *who* is asking, and comparing it here would turn a reconcile that
        agrees with the Owner's own choice into a spurious change.
        """

        return self.companion_id == companion_id and self.policy_refs == policy_refs
