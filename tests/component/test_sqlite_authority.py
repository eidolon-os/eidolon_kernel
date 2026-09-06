from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from eidolon_sdk.device_foundation.v1 import ClaimEventCursor, ClaimEventStreamItem
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.persistence.sqlite import (
    SqliteMountStore,
    schema_report,
    selection_census,
)
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.domain.body import BodyAssignment, derived_endpoint
from eidolon_kernel.domain.errors import IdempotencyConflict, RevisionConflict
from eidolon_kernel.domain.model import DeviceMount, request_fingerprint
from tests.support import claim_event_item, sample_mount

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")


def test_sqlite_atomically_commits_mount_request_and_ordered_audit(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    try:
        first = store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={"source": "hub"},
        )
        replay = store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={"source": "ignored-on-replay"},
        )
        inactive = sample_mount(2, request_id="request-2", active=False)
        second = store.commit(
            mount=inactive,
            expected_revision=1,
            operation="device.unmount",
            event_type="unmounted",
            event_data={"previous_revision": 1},
        )
        events = store.list_audit(after_position=0, limit=100, owner_id="owner-1")
        assert first.audit_position == replay.audit_position == 1
        assert replay.replayed is True
        assert second.audit_position == 2
        assert [event.position for event in events] == [1, 2]
        assert events[0].subject_revision == 1 and events[0].data["active"] is True
        assert events[1].subject_revision == 2 and events[1].data["active"] is False
        assert {event.subject for event in events} == {"device-mount"}
        assert store.get_request("request-2").mount == inactive
        assert store.list_audit(after_position=2, limit=1, owner_id="owner-1") == ()
        assert store.list_audit(after_position=0, limit=1, owner_id="other") == ()
    finally:
        store.close()


def test_sqlite_enforces_cas_idempotency_and_single_process_ownership(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    store = SqliteMountStore(path)
    try:
        store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={},
        )
        with pytest.raises(RevisionConflict):
            store.commit(
                mount=sample_mount(2, request_id="other"),
                expected_revision=0,
                operation="device.mount",
                event_type="mounted",
                event_data={},
            )
        with pytest.raises(IdempotencyConflict):
            store.commit(
                mount=sample_mount(),
                expected_revision=1,
                operation="device.unmount",
                event_type="unmounted",
                event_data={},
            )
        with pytest.raises(RevisionConflict, match="owner namespace"):
            store.commit(
                mount=replace(
                    sample_mount(2, request_id="owner-transfer"), owner_id="owner-2"
                ),
                expected_revision=1,
                operation="device.mount",
                event_type="remounted",
                event_data={},
            )
        with pytest.raises(RuntimeError, match="already owned"):
            SqliteMountStore(path)
    finally:
        store.close()


def test_restart_rebuilds_projection_from_only_authoritative_table(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first = SqliteMountStore(path)
    first.commit(
        mount=sample_mount(),
        expected_revision=0,
        operation="device.mount",
        event_type="mounted",
        event_data={},
    )
    first.close()

    restarted = SqliteMountStore(path)
    try:
        projection = InMemoryMountProjection()
        projection.rebuild(restarted.list_all())
        assert projection.get(_DEVICE_1) == restarted.get(_DEVICE_1)
        assert projection.list(
            owner_id="owner-1",
            active_only=True,
            after_device_id=None,
            limit=10,
        )[0].device_id == _DEVICE_1
    finally:
        restarted.close()


def test_partial_or_old_database_is_rejected_without_migration(tmp_path) -> None:
    path = tmp_path / "partial.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE kernel_device_mounts(device_id TEXT PRIMARY KEY)")
    connection.close()
    with pytest.raises(RuntimeError, match="migrations are unsupported"):
        SqliteMountStore(path)


def test_a_database_predating_body_assignments_is_rejected_instead_of_inferred(
    tmp_path,
) -> None:
    """Migrations are unsupported here on purpose, so the refusal has to be loud.

    Named for the newest thing a stale file is missing rather than for a version
    number, because that is what the next person will actually be holding: a
    Host whose Kernel database was created before Bodies could be assigned has
    no table to put them in, and a Kernel that started anyway would answer "no
    Eidolon answers through this" to every device.
    """

    path = tmp_path / "kernel-old.sqlite3"
    current = SqliteMountStore(path)
    current.commit(
        mount=sample_mount(),
        expected_revision=0,
        operation="device.mount",
        event_type="mounted",
        event_data={"source": "v4-characterization"},
    )
    current.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE kernel_body_assignments")
    connection.execute("UPDATE kernel_schema_meta SET schema_version = 7")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="partial or unknown"):
        SqliteMountStore(path)

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE kernel_body_assignments(body_endpoint_id TEXT PRIMARY KEY)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="does not match schema v8"):
        SqliteMountStore(path)


def _mount_from_claim(item: ClaimEventStreamItem, *, current=None) -> DeviceMount:
    ref = item.event.data.device_ref
    request_id = f"claim-event-{item.stream_position}"
    fingerprint = request_fingerprint(
        "device.mount-by-claim-event",
        {"event_id": item.event.id, "device_ref": ref.model_dump(mode="json")},
    )
    if current is None:
        return DeviceMount.first(
            device_id=ref.device_instance_id,
            owner_id=str(item.event.data.business_owner_id),
            device_ref=ref,
            at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
            request_id=request_id,
            fingerprint=fingerprint,
        )
    return current.mounted_as(
        owner_id=str(item.event.data.business_owner_id),
        device_ref=ref,
        at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        request_id=request_id,
        fingerprint=fingerprint,
    )


def test_claim_inbox_mount_and_cursor_checkpoint_are_one_transaction(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    item = claim_event_item()
    mount = _mount_from_claim(item)
    try:
        result = store.commit_claim_event(
            item=item,
            requested_after=ClaimEventCursor(stream_position=0),
            next_cursor=ClaimEventCursor(stream_position=1),
            high_watermark=4,
            outcome="mounted",
            mount=mount,
            expected_mount_revision=0,
            processed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        )
        assert result.mount == mount
        assert store.get(_DEVICE_1) == mount
        assert store.claim_event_cursor().stream_position == 1
        assert store.claim_event_high_watermark() == 4
        stored = store.claim_events_for_device(_DEVICE_1)
        assert stored[0].device_ref == item.event.data.device_ref
        row = store._connection.execute(
            "SELECT manifest_id, manifest_revision, manifest_digest "
            "FROM kernel_claim_event_inbox"
        ).fetchone()
        assert tuple(row) == ("manifest-one", 1, "sha256:" + "a" * 64)
    finally:
        store.close()


def test_claim_checkpoint_failure_rolls_back_mount_inbox_and_audit(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    item = claim_event_item()
    mount = _mount_from_claim(item)
    store._connection.execute(
        "CREATE TRIGGER fail_claim_cursor BEFORE UPDATE ON kernel_claim_event_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected cursor failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected cursor failure"):
            store.commit_claim_event(
                item=item,
                requested_after=ClaimEventCursor(stream_position=0),
                next_cursor=ClaimEventCursor(stream_position=1),
                high_watermark=1,
                outcome="mounted",
                mount=mount,
                expected_mount_revision=0,
                processed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
            )
        assert store.get(_DEVICE_1) is None
        assert store.claim_events_for_device(_DEVICE_1) == ()
        assert store.claim_event_cursor().stream_position == 0
        assert store.list_audit(after_position=0, limit=10, owner_id="owner-1") == ()
    finally:
        store.close()


def test_claim_same_event_replays_and_same_source_id_different_bytes_conflict(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    item = claim_event_item()
    mount = _mount_from_claim(item)
    arguments = {
        "item": item,
        "requested_after": ClaimEventCursor(stream_position=0),
        "next_cursor": ClaimEventCursor(stream_position=1),
        "high_watermark": 1,
        "outcome": "mounted",
        "mount": mount,
        "expected_mount_revision": 0,
        "processed_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    }
    try:
        first = store.commit_claim_event(**arguments)
        replay = store.commit_claim_event(**arguments)
        assert first.replayed is False
        assert replay.replayed is True
        conflicting = ClaimEventStreamItem(
            stream_position=1,
            event=item.event.model_copy(update={"correlationid": "intent-other"}),
        )
        with pytest.raises(IdempotencyConflict, match=r"source\+id"):
            store.commit_claim_event(**{**arguments, "item": conflicting})
        assert store.claim_event_cursor().stream_position == 1
    finally:
        store.close()


def test_claim_cursor_and_high_watermark_survive_restart(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first = SqliteMountStore(path)
    item = claim_event_item()
    first.commit_claim_event(
        item=item,
        requested_after=ClaimEventCursor(stream_position=0),
        next_cursor=ClaimEventCursor(stream_position=1),
        high_watermark=7,
        outcome="mounted",
        mount=_mount_from_claim(item),
        expected_mount_revision=0,
        processed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    )
    first.close()

    restarted = SqliteMountStore(path)
    try:
        assert restarted.claim_event_cursor().stream_position == 1
        assert restarted.claim_event_high_watermark() == 7
        restarted.checkpoint_claim_cursor(
            requested_after=ClaimEventCursor(stream_position=1),
            next_cursor=ClaimEventCursor(stream_position=1),
            high_watermark=9,
            processed_at=datetime(2026, 8, 4, 9, 1, tzinfo=UTC),
        )
        assert restarted.claim_event_high_watermark() == 9
        with pytest.raises(RevisionConflict, match="inconsistent"):
            restarted.checkpoint_claim_cursor(
                requested_after=ClaimEventCursor(stream_position=1),
                next_cursor=ClaimEventCursor(stream_position=1),
                high_watermark=8,
                processed_at=datetime(2026, 8, 4, 9, 2, tzinfo=UTC),
            )
        assert restarted.claim_event_high_watermark() == 9
    finally:
        restarted.close()


def _assignment(revision: int = 1, *, companion_id: str | None, request_id: str):
    mount = sample_mount()
    endpoint = derived_endpoint(mount)
    at = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
    first = BodyAssignment.first(
        endpoint=endpoint,
        companion_id=companion_id,
        selection_provenance=("user_selected" if companion_id else "user_cleared"),
        change_reason=None,
        policy_refs=(),
        at=at,
        request_id=request_id,
        fingerprint=request_fingerprint("body.replace-assignment", {"r": request_id}),
    )
    return endpoint, replace(first, revision=revision, generation=revision)


def test_sqlite_commits_an_assignment_under_its_own_cas_and_audits_it(tmp_path) -> None:
    """The Body's revision, not the device's.

    Two facts with two compare-and-swap tokens is the whole reason this is a
    resource: mounting a device and choosing who answers through it used to
    contend over one row, so a remount silently discarded a choice.
    """

    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    try:
        store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={},
        )
        endpoint, assignment = _assignment(companion_id="companion-1", request_id="assign-1")
        committed = store.commit_assignment(
            assignment=assignment,
            expected_revision=0,
            mount_revision=1,
            event_type="eidolon.kernel.body-assignment-created.v1",
            event_data={"previous_revision": 0},
        )

        assert committed.replayed is False
        assert store.get_assignment(endpoint.body_endpoint_id) == assignment
        assert store.get(_DEVICE_1).revision == 1

        events = store.list_audit(after_position=0, limit=100, owner_id="owner-1")
        assert [event.subject for event in events] == ["device-mount", "body-assignment"]
        assert events[1].subject_id == endpoint.body_endpoint_id
        assert events[1].device_id == _DEVICE_1
        assert events[1].data["mount_revision"] == 1
    finally:
        store.close()


def test_sqlite_replays_the_same_request_and_refuses_a_stale_one(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    try:
        _, assignment = _assignment(companion_id="companion-1", request_id="assign-1")
        store.commit_assignment(
            assignment=assignment,
            expected_revision=0,
            mount_revision=1,
            event_type="eidolon.kernel.body-assignment-created.v1",
            event_data={},
        )
        replay = store.commit_assignment(
            assignment=assignment,
            expected_revision=0,
            mount_revision=1,
            event_type="eidolon.kernel.body-assignment-created.v1",
            event_data={},
        )
        assert replay.replayed is True
        assert replay.audit_position == 1

        _, other = _assignment(companion_id="companion-2", request_id="assign-2")
        with pytest.raises(RevisionConflict):
            store.commit_assignment(
                assignment=other,
                expected_revision=0,
                mount_revision=1,
                event_type="eidolon.kernel.body-assignment-replaced.v1",
                event_data={},
            )

        reused = replace(other, request_id="assign-1")
        with pytest.raises(IdempotencyConflict):
            store.commit_assignment(
                assignment=reused,
                expected_revision=1,
                mount_revision=1,
                event_type="eidolon.kernel.body-assignment-replaced.v1",
                event_data={},
            )
    finally:
        store.close()


def test_an_assignment_survives_restart_because_it_is_keyed_to_the_body(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first = SqliteMountStore(path)
    endpoint, assignment = _assignment(companion_id="companion-1", request_id="assign-1")
    first.commit_assignment(
        assignment=assignment,
        expected_revision=0,
        mount_revision=1,
        event_type="eidolon.kernel.body-assignment-created.v1",
        event_data={},
    )
    first.close()

    restarted = SqliteMountStore(path)
    try:
        assert restarted.get_assignment(endpoint.body_endpoint_id) == assignment
        assert restarted.list_assignments() == (assignment,)
    finally:
        restarted.close()


def _stale(path) -> str:
    """Refuse a v8 database that has been moved off v8, and return the message."""

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE kernel_requests ADD COLUMN arrived_late TEXT")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError) as raised:
        SqliteMountStore(path)
    return str(raised.value)


def test_a_refused_database_says_how_many_owner_selections_it_would_cost(tmp_path) -> None:
    """The refusal is right; what it left out was the price of obeying it.

    A Kernel that will not open this file cannot be started until the file is
    set aside, and setting it aside destroys the one fact in here that nothing
    else on this Host can supply back: which Companion the Owner chose to answer
    through each Body. Mounts return from the Hub Claim stream, and a backup of
    this file is a backup at the schema just refused, so neither is a way back.

    Before this, an operator met ``schema is partial or unknown`` and reached for
    ``rm`` — the workspace still holds one file somebody renamed by hand, with no
    record of what went with it.
    """

    path = tmp_path / "kernel.sqlite3"
    store = SqliteMountStore(path)
    try:
        for index, companion in enumerate(("companion-1", "companion-2", None), start=1):
            _, assignment = _assignment(companion_id=companion, request_id=f"assign-{index}")
            store.commit_assignment(
                assignment=replace(
                    assignment, body_endpoint_id=f"body-{index}", device_id=f"device-{index}"
                ),
                expected_revision=0,
                mount_revision=1,
                event_type="eidolon.kernel.body-assignment-created.v1",
                event_data={"previous_revision": 0},
            )
    finally:
        store.close()

    message = _stale(path)

    assert "does not match schema v8" in message
    assert "destroys 2 of 3 Body assignments" in message
    assert "kernel-schema-reset" in message


def test_a_refusal_with_nothing_to_lose_says_that_rather_than_the_same_warning(
    tmp_path,
) -> None:
    """A count nobody can act on is the same silence in more words.

    Most Hosts that fall behind the schema are development Hosts holding no
    Owner selection at all, and a refusal that warned them identically would
    train the operator to skip the sentence on the Host where it is true.
    """

    path = tmp_path / "kernel.sqlite3"
    SqliteMountStore(path).close()

    message = _stale(path)

    assert "destroys no Owner Companion selection" in message


def test_a_database_with_no_assignment_table_is_told_the_count_is_unknown(tmp_path) -> None:
    """Never zero for a shape this Kernel does not recognise.

    The one real file this has happened to is schema v3, which kept the Owner's
    choice as ``kernel_device_mounts.attached_companion_id`` — a column this
    Kernel has never heard of. Counting the current table and reporting ``0``
    would have told its operator the truest-sounding lie available: that setting
    the file aside was free, on the exact file where it was not.

    So the census counts only where *this* Kernel keeps the selection, and says
    it cannot count anywhere else. That also means it stays honest through the
    next move of that fact rather than needing to be taught each old shape.
    """

    path = tmp_path / "kernel-v3-shaped.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE kernel_device_mounts(device_id TEXT PRIMARY KEY, attached_companion_id TEXT)"
    )
    connection.execute("INSERT INTO kernel_device_mounts VALUES ('device-1', 'companion-1')")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError) as raised:
        SqliteMountStore(path)

    message = str(raised.value)
    assert "migrations are unsupported" in message
    assert "cannot say how many that is" in message
    assert "1 device mount" in message
    assert "destroys no Owner" not in message


def test_counting_the_cost_can_never_become_a_second_way_to_fail(tmp_path) -> None:
    """The census runs inside an error path, so it is not allowed to have one.

    The census asks this file questions the refusal itself never asked. On the
    "partial or unknown" path the Kernel stops at the table *names*, so a
    ``kernel_schema_meta`` shaped like somebody else's is a column error the
    census meets alone. A census that let ``sqlite3.Error`` out would replace a
    refusal naming its cost with a traceback naming nothing — strictly worse
    than the message this change exists to improve.
    """

    path = tmp_path / "not-this-kernels.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE kernel_schema_meta(version INTEGER)")
    connection.execute("INSERT INTO kernel_schema_meta VALUES (8)")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError) as raised:
        SqliteMountStore(path)

    message = str(raised.value)
    assert "migrations are unsupported" in message
    assert "kernel-schema-reset" in message

    census = selection_census(path)
    assert census.schema_version is None

    not_a_database = tmp_path / "notes.txt"
    not_a_database.write_text("this is not a Kernel authority", encoding="utf-8")
    assert selection_census(not_a_database).as_document()["unreadable"] is not None


def test_the_census_answers_off_a_file_no_kernel_is_holding(tmp_path) -> None:
    """Ops asks this before repairing a Host, with nothing running to ask.

    Read-only and openable by whoever is deciding, so that the number in the
    refusal and the number in the repair plan have one definition rather than
    two implementations that can disagree.
    """

    path = tmp_path / "kernel.sqlite3"
    store = SqliteMountStore(path)
    try:
        _, assignment = _assignment(companion_id="companion-1", request_id="assign-1")
        store.commit_assignment(
            assignment=assignment,
            expected_revision=0,
            mount_revision=1,
            event_type="eidolon.kernel.body-assignment-created.v1",
            event_data={"previous_revision": 0},
        )
    finally:
        store.close()

    document = selection_census(path).as_document()

    assert document["schema_version"] == 8
    assert document["selections"] == 1
    assert document["assignments"] == 1
    assert document["countable"] is True
    assert document["unreadable"] is None
    assert selection_census(tmp_path / "absent.sqlite3").as_document()["countable"] is False


def test_the_repair_asks_this_kernel_the_same_question_startup_asked(tmp_path) -> None:
    """One predicate, two askers, so a repair can never act on a different answer.

    The decision to set an authority aside must not rest on an inference — a
    version number that looks wrong, or a unit observed crash-looping. It rests
    on this Kernel saying it will not open this file, which is the same sentence
    it puts in front of the operator when it refuses to start.
    """

    path = tmp_path / "kernel.sqlite3"
    SqliteMountStore(path).close()

    healthy = schema_report(path)
    assert healthy["accepted"] is True
    assert healthy["refusal"] is None
    assert healthy["code_schema_version"] == 8

    message = _stale(path)
    refused = schema_report(path)

    assert refused["accepted"] is False
    assert message.startswith(refused["refusal"])
    assert refused["notice"] in message


def test_a_file_this_kernel_cannot_read_at_all_is_not_reported_as_acceptable(
    tmp_path,
) -> None:
    """``accepted`` is three-valued because "could not ask" is not "yes".

    A repair that read a missing or unreadable file as acceptable would refuse
    the one operation that could fix the Host; one that read it as refused would
    set aside a file it never managed to look inside.
    """

    absent = schema_report(tmp_path / "absent.sqlite3")

    assert absent["accepted"] is None
    assert absent["unreadable"] is not None
