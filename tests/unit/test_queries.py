from __future__ import annotations

import pytest

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.application.queries import AuditQueries, DeviceMountQueries
from eidolon_kernel.domain.errors import InvalidRequest, NotFound
from tests.support import MemoryStore, sample_mount


def test_get_resolve_and_scoped_list_query_projection() -> None:
    projection = InMemoryMountProjection()
    projection.rebuild((sample_mount(),))
    queries = DeviceMountQueries(projection)
    assert queries.get("device-1").revision == 1
    assert queries.resolve("device-1").active
    assert queries.list(
        owner_id="owner-1",
        companion_id=None,
        active_only=True,
        after_device_id=None,
        limit=10,
    )[0].device_id == "device-1"

    projection.put(sample_mount(2, request_id="inactive", active=False))
    with pytest.raises(NotFound):
        queries.resolve("device-1")
    with pytest.raises(NotFound):
        queries.get("missing")


@pytest.mark.parametrize(
    "arguments",
    [
        {"owner_id": None, "companion_id": None, "limit": 1},
        {"owner_id": "owner", "companion_id": None, "limit": 0},
        {"owner_id": "owner", "companion_id": None, "limit": 101},
        {"owner_id": "", "companion_id": None, "limit": 1},
        {"owner_id": "owner", "companion_id": "", "limit": 1},
        {
            "owner_id": "owner",
            "companion_id": None,
            "after_device_id": "",
            "limit": 1,
        },
    ],
)
def test_list_query_rejects_unscoped_or_unbounded_requests(arguments) -> None:
    arguments.setdefault("active_only", True)
    arguments.setdefault("after_device_id", None)
    with pytest.raises(InvalidRequest):
        DeviceMountQueries(InMemoryMountProjection()).list(**arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"after_position": -1, "limit": 1, "owner_id": None},
        {"after_position": 0, "limit": 0, "owner_id": None},
        {"after_position": 0, "limit": 501, "owner_id": None},
        {"after_position": 0, "limit": 1, "owner_id": ""},
    ],
)
def test_audit_query_rejects_invalid_bounds(arguments) -> None:
    with pytest.raises(InvalidRequest):
        AuditQueries(MemoryStore()).list(**arguments)
