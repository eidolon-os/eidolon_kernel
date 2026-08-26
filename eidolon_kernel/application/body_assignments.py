"""Replacing and converging which Companion answers through a Body."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from eidolon_sdk.device_foundation.v1 import SelectionProvenance

from eidolon_kernel.domain.body import (
    BodyAssignment,
    BodyEndpoint,
    derived_endpoint,
    device_of,
)
from eidolon_kernel.domain.commands import (
    ASSIGNMENT_ORIGIN_OWNER,
    ASSIGNMENT_ORIGIN_RECONCILER,
    ReplaceAssignmentCommand,
)
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    AuthorityUnavailable,
    InvalidRequest,
    NotFound,
    RevisionConflict,
)
from eidolon_kernel.ports.authorities import CompanionAuthority
from eidolon_kernel.ports.runtime import (
    AssignmentCommitResult,
    Clock,
    MountProjection,
    MountStore,
)


def _provenance(*, origin: str, companion_id: str | None) -> str:
    """Why this Body now points where it does, derived from the act rather than sent.

    ``POLICY_RECONCILED`` has no producer here, on purpose: nothing on this Host
    evaluates a resource policy, so nothing can release a Body for policy
    reasons. The value stays in the canonical vocabulary because the contract
    defines it; emitting it would be inventing an event that never happened.
    """

    if origin == ASSIGNMENT_ORIGIN_OWNER:
        return (
            SelectionProvenance.USER_SELECTED.value
            if companion_id is not None
            else SelectionProvenance.USER_CLEARED.value
        )
    if companion_id is not None:
        raise InvalidRequest("only an explicit selection can name a Companion")
    # Both remaining origins release a Body because the Eidolon it answered as
    # is no longer one this Host will stand behind — put away by its Owner, or
    # gone from the authority entirely. From the Body's side that is one fact.
    return SelectionProvenance.COMPANION_DELETED.value


@dataclass(slots=True)
class BodyEndpoints:
    """The Bodies in one Owner's namespace, with what each is assigned to.

    Reads the mount projection rather than the store, like every other read on
    this side, and the assignment from the authority — the assignment has no
    projection because it is small, changes rarely, and a second copy of "who
    answers here" is precisely the thing this module exists to stop being.
    """

    projection: MountProjection
    store: MountStore

    def resolve(self, *, owner_id: str, body_endpoint_id: str) -> BodyEndpoint:
        mount = self.projection.get(device_of(body_endpoint_id))
        if mount is None or mount.owner_id != owner_id:
            raise NotFound("body endpoint not found")
        endpoint = derived_endpoint(mount)
        if endpoint.body_endpoint_id != body_endpoint_id:
            raise NotFound("body endpoint not found")
        return endpoint

    def assignment(self, endpoint: BodyEndpoint) -> BodyAssignment | None:
        assignment = self.store.get_assignment(endpoint.body_endpoint_id)
        if assignment is not None and assignment.owner_id != endpoint.owner_id:
            # A Body cannot change hands. If this is ever reached the two facts
            # disagree, and answering with either would be answering about
            # somebody else's device.
            raise NotFound("body endpoint not found")
        return assignment

    def list(
        self, *, owner_id: str, companion_id: str | None = None
    ) -> tuple[tuple[BodyEndpoint, BodyAssignment | None], ...]:
        mounts = self.projection.list(
            owner_id=owner_id, active_only=False, after_device_id=None, limit=100
        )
        rows = []
        for mount in mounts:
            endpoint = derived_endpoint(mount)
            assignment = self.store.get_assignment(endpoint.body_endpoint_id)
            if companion_id is not None and (
                assignment is None or assignment.companion_id != companion_id
            ):
                continue
            rows.append((endpoint, assignment))
        return tuple(rows)


@dataclass(slots=True)
class ReplaceAssignment:
    """The canonical ``ReplaceAssignment``, compare-and-swapped on its own revision.

    On its *own* revision, which is the point: pointing a speaker at a different
    Eidolon no longer collides with a device being remounted, because those are
    two facts with two revisions instead of one row with one.
    """

    store: MountStore
    projection: MountProjection
    companions: CompanionAuthority
    clock: Clock

    async def execute(self, command: ReplaceAssignmentCommand) -> AssignmentCommitResult:
        endpoints = BodyEndpoints(projection=self.projection, store=self.store)
        endpoint = endpoints.resolve(
            owner_id=command.owner_id, body_endpoint_id=command.body_endpoint_id
        )
        if command.companion_id is not None and not endpoint.present:
            # Clearing an assignment on a device that is no longer mounted is
            # allowed and useful — it is how an Owner tidies up. Pointing one at
            # an Eidolon is not: there is nothing there to answer.
            raise NotFound("body endpoint is not mounted")

        current = endpoints.assignment(endpoint)
        if command.companion_id is not None:
            companion = await self.companions.get_companion(
                companion_id=command.companion_id
            )
            if (
                companion.companion_id != command.companion_id
                or companion.owner_id != command.owner_id
                or companion.status != "active"
            ):
                raise AuthorityRejected("Companion must exist, be active, and match owner")
            # Re-read after awaiting the authority: an identical call may have
            # committed while this one was asking.
            current = endpoints.assignment(endpoint)

        provenance = _provenance(
            origin=command.origin, companion_id=command.companion_id
        )
        actual_revision = current.revision if current is not None else 0
        if actual_revision != command.expected_assignment_revision:
            already = current is not None and current.states_the_same_as(
                companion_id=command.companion_id, policy_refs=command.policy_refs
            )
            if not already:
                raise RevisionConflict(
                    f"expected assignment revision {command.expected_assignment_revision}, "
                    f"current revision is {actual_revision}"
                )
            # A stale compare-and-swap whose end state already holds. This is
            # what a lost response looks like from the client's side, and
            # refusing it would make re-reading the only safe thing anyone could
            # do after a timeout.
            return AssignmentCommitResult(
                assignment=current, audit_position=0, replayed=True
            )

        mount = self.projection.get(endpoint.device_id)
        if mount is None:
            # The device stopped being this Owner's between resolving the
            # endpoint and here. Nothing to record against.
            raise NotFound("body endpoint not found")

        now = self.clock.now()
        if current is None:
            assignment = BodyAssignment.first(
                endpoint=endpoint,
                companion_id=command.companion_id,
                selection_provenance=provenance,
                change_reason=command.change_reason,
                policy_refs=command.policy_refs,
                at=now,
                request_id=command.request_id,
                fingerprint=command.fingerprint,
            )
            event_type = "eidolon.kernel.body-assignment-created.v1"
            previous = None
        else:
            assignment = current.replaced(
                companion_id=command.companion_id,
                selection_provenance=provenance,
                change_reason=command.change_reason,
                policy_refs=command.policy_refs,
                at=now,
                request_id=command.request_id,
                fingerprint=command.fingerprint,
            )
            event_type = "eidolon.kernel.body-assignment-replaced.v1"
            previous = current.companion_id
        return self.store.commit_assignment(
            assignment=assignment,
            expected_revision=actual_revision,
            mount_revision=mount.revision,
            event_type=event_type,
            event_data={
                "previous_revision": actual_revision,
                "previous_companion_id": previous,
                "selection_provenance": provenance,
                "origin": command.origin,
            },
        )


@dataclass(frozen=True, slots=True)
class AssignmentReconciliationResult:
    checked: int
    released: int
    deferred: int


@dataclass(slots=True)
class ReconcileAssignments:
    """Release Bodies whose Companion the authority will no longer stand behind.

    This is what makes "no orphan active assignment" a checkable claim rather
    than a hope: an assignment naming a Companion that is gone or not active is
    converged to nobody, *with the reason recorded*, so the difference between
    "you unassigned this" and "the Eidolon it answered as was put away" survives
    into what the Owner is shown.

    It never assigns. Only an Owner adds a Companion to a Body; a reconciler
    that could would be a second, invisible production path for the one decision
    this product insists a person makes.
    """

    store: MountStore
    projection: MountProjection
    companions: CompanionAuthority
    clock: Clock

    async def execute(self) -> AssignmentReconciliationResult:
        checked = released = deferred = 0
        for snapshot in self.store.list_assignments():
            if snapshot.companion_id is None:
                continue
            checked += 1
            try:
                reason = await self._rejection(snapshot)
            except AuthorityUnavailable:
                # A transport or credential outage is not a revocation. Leaving
                # the assignment standing is the safe direction: the Body keeps
                # answering as the Eidolon its Owner chose.
                deferred += 1
                continue
            if reason is None:
                continue
            current = self.store.get_assignment(snapshot.body_endpoint_id)
            if current is None or current.revision != snapshot.revision:
                # A concurrent Owner mutation wins; the next scan evaluates it.
                continue
            request_id = "reconcile-release-" + hashlib.sha256(
                f"{current.body_endpoint_id}\0{current.revision}".encode()
            ).hexdigest()
            command = ReplaceAssignmentCommand(
                request_id=request_id,
                owner_id=current.owner_id,
                body_endpoint_id=current.body_endpoint_id,
                expected_assignment_revision=current.revision,
                companion_id=None,
                origin=ASSIGNMENT_ORIGIN_RECONCILER,
                change_reason=reason,
                policy_refs=current.policy_refs,
            )
            mount = self.projection.get(current.device_id)
            assignment = current.replaced(
                companion_id=None,
                selection_provenance=_provenance(
                    origin=ASSIGNMENT_ORIGIN_RECONCILER, companion_id=None
                ),
                change_reason=reason,
                policy_refs=current.policy_refs,
                at=self.clock.now(),
                request_id=request_id,
                fingerprint=command.fingerprint,
            )
            try:
                self.store.commit_assignment(
                    assignment=assignment,
                    expected_revision=current.revision,
                    mount_revision=mount.revision if mount is not None else 0,
                    event_type="eidolon.kernel.body-assignment-released-by-authority.v1",
                    event_data={
                        "previous_revision": current.revision,
                        "previous_companion_id": current.companion_id,
                        "selection_provenance": assignment.selection_provenance,
                        "origin": ASSIGNMENT_ORIGIN_RECONCILER,
                    },
                )
            except RevisionConflict:
                continue
            released += 1
        return AssignmentReconciliationResult(checked, released, deferred)

    async def _rejection(self, assignment: BodyAssignment) -> str | None:
        try:
            companion = await self.companions.get_companion(
                companion_id=assignment.companion_id or ""
            )
        except AuthorityRejected:
            return "companion-missing"
        if (
            companion.companion_id != assignment.companion_id
            or companion.owner_id != assignment.owner_id
            or companion.status != "active"
        ):
            return "companion-not-active"
        return None
