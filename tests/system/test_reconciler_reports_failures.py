"""A reconciler that retries in silence is a reconciler nobody can debug.

Written after a first install on a new board timed out with "readiness
timeout: agent, channel, ..., nats" and nothing else. The manager had tried to
start each service every few seconds for four minutes, every attempt had been
refused by polkit, and every refusal was published to an in-memory directory
that no log and no operator ever read. The reason was one field away the whole
time.
"""

from __future__ import annotations

import logging

import pytest

from eidolon_system.domain.model import ServiceStatus



def _status(service_id: str, state: str, detail: str | None = None) -> ServiceStatus:
    from datetime import UTC, datetime

    return ServiceStatus(
        service_id=service_id,
        required=True,
        desired=None,
        runtime_state=state,
        detail=detail,
        observed_at=datetime.now(UTC),
    )


class _Directory:
    def __init__(self) -> None:
        self._statuses: dict[str, ServiceStatus] = {}

    def put(self, status: ServiceStatus) -> None:
        self._statuses[status.service_id] = status

    def get(self, service_id: str) -> ServiceStatus:
        from eidolon_system.domain.errors import NotFound

        try:
            return self._statuses[service_id]
        except KeyError as exc:
            raise NotFound(service_id) from exc


def _manager(directory):
    from eidolon_system.application.service_manager import ServiceManager

    manager = ServiceManager.__new__(ServiceManager)
    manager.directory = directory
    return manager


def test_a_failure_is_logged_with_the_reason(caplog) -> None:
    directory = _Directory()
    manager = _manager(directory)
    with caplog.at_level(logging.ERROR):
        manager._publish(_status("nats", "failed", "systemd start refused: Access denied"))

    assert "nats" in caplog.text
    assert "Access denied" in caplog.text, (
        "the reason is the whole point; a log saying only 'failed' repeats the "
        "readiness timeout that sent someone looking"
    )


def test_the_same_failure_repeated_is_logged_once(caplog) -> None:
    """Reconciliation runs every few seconds; one refusal is not forty-eight."""

    directory = _Directory()
    manager = _manager(directory)
    with caplog.at_level(logging.ERROR):
        for _ in range(48):
            manager._publish(_status("nats", "failed", "systemd start refused"))

    assert caplog.text.count("nats") == 1


def test_a_changed_reason_is_logged_again(caplog) -> None:
    """Silence after the first line would hide a failure becoming a new one."""

    directory = _Directory()
    manager = _manager(directory)
    with caplog.at_level(logging.INFO):
        manager._publish(_status("nats", "failed", "refused"))
        manager._publish(_status("nats", "failed", "no such unit"))
        manager._publish(_status("nats", "ready"))

    assert caplog.text.count("nats") == 3


def test_every_status_still_reaches_the_directory(caplog) -> None:
    """Logging is added beside publication, not in front of it."""

    directory = _Directory()
    manager = _manager(directory)
    manager._publish(_status("nats", "ready"))
    manager._publish(_status("nats", "failed", "refused"))

    assert directory.get("nats").runtime_state == "failed"
