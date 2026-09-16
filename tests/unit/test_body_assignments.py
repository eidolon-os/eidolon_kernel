import dataclasses

import pytest
from eidolon_sdk.device_foundation.v1 import AssignmentCondition, SelectionProvenance
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.application.body_assignments import (
    BodyEndpoints,
    ReconcileAssignments,
    ReplaceAssignment,
)
from eidolon_kernel.application.device_mounts import MountDevice, UnmountDevice
from eidolon_kernel.domain.body import body_endpoint_id
from eidolon_kernel.domain.commands import (
    ASSIGNMENT_ORIGIN_COMPANION_LIFECYCLE,
    ASSIGNMENT_ORIGIN_OWNER,
    MountDeviceCommand,
    ReplaceAssignmentCommand,
    UnmountDeviceCommand,
)
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    AuthorityUnavailable,
    InvalidRequest,
    NotFound,
    RevisionConflict,
)
from tests.support import (
    FakeCompanionAuthority,
    FakeDeviceAuthority,
    MemoryStore,
    MutableClock,
)

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")
_BODY_1 = body_endpoint_id(_DEVICE_1, "body")


def services(*, devices=None, companions=None):
    store = MemoryStore()
    projection = InMemoryMountProjection()
    clock = MutableClock()
    devices = devices or FakeDeviceAuthority()
    companions = companions or FakeCompanionAuthority()
    return (
        MountDevice(store, projection, devices, clock),
        ReplaceAssignment(store, projection, companions, clock),
        ReconcileAssignments(
            store=store, projection=projection, companions=companions, clock=clock
        ),
        BodyEndpoints(projection=projection, store=store),
        store,
        projection,
    )


def _owner_selects(companion_id, *, expected, request_id="assign-1"):
    return ReplaceAssignmentCommand(
        request_id=request_id,
        owner_id="owner-1",
        body_endpoint_id=_BODY_1,
        expected_assignment_revision=expected,
        companion_id=companion_id,
        origin=ASSIGNMENT_ORIGIN_OWNER,
    )


@pytest.mark.asyncio
async def test_mounting_a_device_neither_requires_nor_creates_an_assignment() -> None:
    """A device joining is not a decision about who answers through it.

    Kept from when the Companion was a field on the mount: mounting must not
    consult the Companion authority, and a freshly mounted Body must be
    unassigned rather than assigned to nobody-in-particular.
    """

    companions = FakeCompanionAuthority(status="inactive")
    mount, _, _, endpoints, store, _ = services(companions=companions)

    result = await mount.execute(
        MountDeviceCommand(
            request_id="mount-1",
            device_id=_DEVICE_1,
            owner_id="owner-1",
            expected_revision=0,
        )
    )

    assert result.mount.active is True
    assert companions.calls == 0
    endpoint = endpoints.resolve(owner_id="owner-1", body_endpoint_id=_BODY_1)
    assert endpoint.present is True
    assert endpoint.source == "derived"
    assert endpoints.assignment(endpoint) is None
    assert store.list_assignments() == ()


@pytest.mark.asyncio
async def test_replacing_is_cas_guarded_and_carries_why_from_who_asked() -> None:
    mount, replace, _, endpoints, _, _ = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))

    assigned = await replace.execute(_owner_selects("companion-1", expected=0))
    cleared = await replace.execute(
        _owner_selects(None, expected=1, request_id="clear-1")
    )

    assert assigned.assignment.companion_id == "companion-1"
    assert assigned.assignment.selection_provenance == (
        SelectionProvenance.USER_SELECTED.value
    )
    assert cleared.assignment.companion_id is None
    assert cleared.assignment.selection_provenance == (
        SelectionProvenance.USER_CLEARED.value
    )
    assert (assigned.assignment.revision, cleared.assignment.revision) == (1, 2)
    assert (assigned.assignment.generation, cleared.assignment.generation) == (1, 2)


@pytest.mark.asyncio
async def test_putting_an_eidolon_away_releases_its_bodies_and_says_so() -> None:
    """The same committed state, "nobody answers here", with a different reason.

    A person who cleared a speaker themselves and a person whose Eidolon was put
    away are owed different sentences, and the difference has to survive past the
    screen that showed it — which means it has to be on the record, not in the
    response body.
    """

    mount, replace, _, _, _, _ = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))

    released = await replace.execute(
        ReplaceAssignmentCommand(
            request_id="archive-1",
            owner_id="owner-1",
            body_endpoint_id=_BODY_1,
            expected_assignment_revision=1,
            companion_id=None,
            origin=ASSIGNMENT_ORIGIN_COMPANION_LIFECYCLE,
            change_reason="companion-archived",
        )
    )

    assert released.assignment.companion_id is None
    assert released.assignment.selection_provenance == (
        SelectionProvenance.COMPANION_DELETED.value
    )
    assert released.assignment.change_reason == "companion-archived"


@pytest.mark.asyncio
async def test_a_stale_revision_whose_end_state_already_holds_is_success() -> None:
    """What a lost response looks like from the caller's side.

    Refusing this would make re-reading the only safe move after any timeout,
    which is the failure this product has already decided against once, for the
    Owner's default Companion.
    """

    mount, replace, _, _, _, _ = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))

    again = await replace.execute(
        _owner_selects("companion-1", expected=0, request_id="assign-retry")
    )
    assert again.replayed is True
    assert again.assignment.revision == 1

    with pytest.raises(RevisionConflict):
        await replace.execute(
            _owner_selects("companion-2", expected=0, request_id="assign-other")
        )


@pytest.mark.asyncio
async def test_an_assignment_outlives_the_mount_it_was_made_through() -> None:
    """Re-running setup on a speaker must not change who answers through it.

    This is the defect the old shape had by construction: the Companion was a
    field on the mount, so a new Claim generation built a new mount and the
    Eidolon was silently forgotten.
    """

    mount, replace, _, endpoints, store, projection = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))

    UnmountDevice(store, projection, MutableClock()).execute(
        UnmountDeviceCommand("unmount-1", _DEVICE_1, "owner-1", 1)
    )
    await mount.execute(
        MountDeviceCommand("mount-2", _DEVICE_1, "owner-1", 2, replace_existing=True)
    )

    endpoint = endpoints.resolve(owner_id="owner-1", body_endpoint_id=_BODY_1)
    assignment = endpoints.assignment(endpoint)
    assert assignment is not None
    assert assignment.companion_id == "companion-1"
    assert assignment.revision == 1
    assert assignment.status(endpoint=endpoint).conditions == (AssignmentCondition.REALIZED,)


@pytest.mark.asyncio
async def test_an_unmounted_body_reports_capability_missing_and_refuses_a_new_eidolon() -> None:
    """Kept rather than deleted, and clearable rather than frozen.

    The design calls a Body whose device is gone ``CapabilityMissing``: the
    assignment stays so the device can come back to it, an Owner can still tidy
    it away, and nothing new can be pointed at hardware that is not there.
    """

    mount, replace, _, endpoints, store, projection = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))
    UnmountDevice(store, projection, MutableClock()).execute(
        UnmountDeviceCommand("unmount-1", _DEVICE_1, "owner-1", 1)
    )

    endpoint = endpoints.resolve(owner_id="owner-1", body_endpoint_id=_BODY_1)
    assignment = endpoints.assignment(endpoint)
    assert endpoint.present is False
    assert assignment.status(endpoint=endpoint).conditions == (
        AssignmentCondition.CAPABILITY_MISSING,
    )
    assert assignment.status(endpoint=endpoint).effective_companion_id is None

    with pytest.raises(NotFound):
        await replace.execute(
            _owner_selects("companion-2", expected=1, request_id="assign-2")
        )
    cleared = await replace.execute(
        _owner_selects(None, expected=1, request_id="clear-1")
    )
    assert cleared.assignment.companion_id is None


@pytest.mark.asyncio
async def test_only_an_active_companion_of_this_owner_can_be_named() -> None:
    companions = FakeCompanionAuthority(status="archived")
    mount, replace, _, _, _, _ = services(companions=companions)
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))

    with pytest.raises(AuthorityRejected):
        await replace.execute(_owner_selects("companion-1", expected=0))


@pytest.mark.asyncio
async def test_another_owners_body_is_not_found_rather_than_forbidden() -> None:
    mount, replace, _, endpoints, _, _ = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))

    with pytest.raises(NotFound):
        endpoints.resolve(owner_id="owner-2", body_endpoint_id=_BODY_1)
    with pytest.raises(NotFound):
        await replace.execute(
            ReplaceAssignmentCommand(
                request_id="assign-1",
                owner_id="owner-2",
                body_endpoint_id=_BODY_1,
                expected_assignment_revision=0,
                companion_id="companion-1",
                origin=ASSIGNMENT_ORIGIN_OWNER,
            )
        )


@pytest.mark.asyncio
async def test_reconciliation_releases_a_body_whose_eidolon_is_no_longer_active() -> None:
    """This is what makes "no orphan active assignment" checkable rather than hoped.

    And it releases with a reason: the row afterwards says the Eidolon went
    away, not that somebody cleared it.
    """

    companions = FakeCompanionAuthority()
    mount, replace, reconcile, endpoints, store, _ = services(companions=companions)
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))
    companions.status = "archived"

    result = await reconcile.execute()

    current = store.get_assignment(_BODY_1)
    assert (result.checked, result.released, result.deferred) == (1, 1, 0)
    assert current.companion_id is None
    assert current.selection_provenance == SelectionProvenance.COMPANION_DELETED.value
    assert store.events[-1].event_type == (
        "eidolon.kernel.body-assignment-released-by-authority.v1"
    )
    assert store.events[-1].subject == "body-assignment"
    assert store.events[-1].subject_id == _BODY_1
    # The device itself is untouched: an Eidolon being put away is not a reason
    # to take somebody's speaker off their Host.
    assert store.get(_DEVICE_1).active is True


@pytest.mark.asyncio
async def test_reconciliation_asks_nothing_about_a_body_nobody_answers_through() -> None:
    companions = FakeCompanionAuthority()
    mount, replace, reconcile, _, _, _ = services(companions=companions)
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects(None, expected=0))
    companions.calls = 0

    result = await reconcile.execute()

    assert (result.checked, result.released, result.deferred) == (0, 0, 0)
    assert companions.calls == 0


@pytest.mark.asyncio
async def test_reconciliation_defers_an_outage_instead_of_releasing() -> None:
    """An authority that did not answer has not revoked anything.

    The safe direction is leaving the Body answering as the Eidolon its Owner
    chose: a credential outage must not go quiet on somebody's speaker.
    """

    class OfflineCompanionAuthority(FakeCompanionAuthority):
        async def get_companion(self, **kwargs):
            raise AuthorityUnavailable("Companion authority offline")

    mount, replace, _, _, store, projection = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))

    result = await ReconcileAssignments(
        store=store,
        projection=projection,
        companions=OfflineCompanionAuthority(),
        clock=MutableClock(),
    ).execute()

    assert (result.checked, result.released, result.deferred) == (1, 0, 1)
    assert store.get_assignment(_BODY_1).companion_id == "companion-1"


@pytest.mark.asyncio
async def test_nothing_but_a_person_may_name_the_eidolon_that_answers() -> None:
    """The one decision this product insists a person makes.

    A reconciler that could assign would be a second production path for "who
    answers here" that nobody would ever see happen, so the domain refuses the
    combination rather than the reconciler merely declining to use it.
    """

    ports = {field.name for field in dataclasses.fields(ReconcileAssignments)}
    assert ports == {"store", "projection", "companions", "clock"}

    with pytest.raises(InvalidRequest):
        ReplaceAssignmentCommand(
            request_id="reconcile-1",
            owner_id="owner-1",
            body_endpoint_id=_BODY_1,
            expected_assignment_revision=0,
            companion_id="companion-1",
            origin="reconciler",
        ).fingerprint  # noqa: B018 - construction is what must fail


@pytest.mark.asyncio
async def test_generation_moves_only_when_the_spec_does() -> None:
    """A fence a runtime can rely on, rather than a counter of every write.

    ``revision`` is what the next writer compares against and moves on every
    commit. ``generation`` is what a session pins, and a write that changed
    nothing it depends on must not invalidate it.
    """

    mount, replace, _, _, _, _ = services()
    await mount.execute(MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0))
    await replace.execute(_owner_selects("companion-1", expected=0))

    same = await replace.execute(
        ReplaceAssignmentCommand(
            request_id="reason-1",
            owner_id="owner-1",
            body_endpoint_id=_BODY_1,
            expected_assignment_revision=1,
            companion_id="companion-1",
            origin=ASSIGNMENT_ORIGIN_OWNER,
            change_reason="said-again",
        )
    )

    assert same.assignment.revision == 2
    assert same.assignment.generation == 1
    assert same.assignment.change_reason == "said-again"


@pytest.mark.asyncio
async def test_no_active_assignment_is_left_naming_an_eidolon_that_is_gone() -> None:
    """The Phase 5 exit condition, stated where it can actually be checked.

    "No orphan active BodyAssignment" was not a checkable claim while the
    concept did not exist. It is one now, and it is held up by two things
    together: the archive workflow releases a Companion's Bodies before its
    state moves, and this scan converges anything that survived a crash between
    those two steps. This asserts the second half — the half that has to be true
    even when nobody was there to run the first.
    """

    companions = FakeCompanionAuthority()
    mount, replace, reconcile, endpoints, store, _ = services(companions=companions)
    for name in ("device-a", "device-b"):
        device = named_device_instance_id(name)
        await mount.execute(MountDeviceCommand(f"mount-{name}", device, "owner-1", 0))
        await replace.execute(
            ReplaceAssignmentCommand(
                request_id=f"assign-{name}",
                owner_id="owner-1",
                body_endpoint_id=body_endpoint_id(device, "body"),
                expected_assignment_revision=0,
                companion_id="companion-1",
                origin=ASSIGNMENT_ORIGIN_OWNER,
            )
        )
    # The archive crashed after the authority moved and before either Body was
    # let go: the Companion is gone and two assignments still name it.
    companions.status = "archived"
    assert [a.companion_id for a in store.list_assignments()] == [
        "companion-1",
        "companion-1",
    ]

    await reconcile.execute()

    orphans = [
        assignment
        for assignment in store.list_assignments()
        if assignment.companion_id is not None
    ]
    assert orphans == []
    # And it converged rather than forgot: each Body still exists and still says
    # why it is quiet, so the device can be pointed somewhere again.
    assert len(store.list_assignments()) == 2
    assert {a.selection_provenance for a in store.list_assignments()} == {
        "companion_deleted"
    }
