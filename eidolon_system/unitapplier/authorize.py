"""Who may apply which verb to which unit, and how the caller is identified.

Two halves, deliberately split at the socket edge:

* `peer_of` reads the connection and answers *which systemd unit the caller runs
  under* — the one privileged fact, and the only part that touches the kernel.
* `authorize` decides. It is pure, takes the already-resolved caller, and names
  the condition it refused on. The whole point of owning this instead of a
  polkit rule is that a refusal has to say which condition failed.
"""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

from eidolon_system.unitapplier.protocol import VERBS, Request

#: The systemd unit a caller must itself be running under. eidolond is the only
#: process with a mandate to actuate the product units; a shell that happens to
#: run as `eidolon`, or any other service under that uid, is not.
MANAGER_UNIT = "eidolond.service"

#: SO_PEERPIDFD (Linux 6.5+). The kernel hands back a pidfd for the process that
#: called connect(), which pins that pid: the caller cannot exit and have its pid
#: recycled underneath the /proc read that follows. Python does not name the
#: constant yet. The RK3588 board runs 6.1 and answers ENOPROTOOPT, so the
#: fallback below is the path that runs in the product today.
_SO_PEERPIDFD = 77

#: Linux's own value, used when the platform does not name it. This module only
#: ever runs for real on Linux; the fallback keeps it importable — and its
#: decisions testable against a fake socket — on the workstation.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)

_SLICE_SUFFIX = ".slice"

#: What a resolved cgroup component must end with to be a unit we recognise.
#: Anything else means we did not understand the path, and must refuse.
_UNIT_SUFFIXES = (".service", ".scope")


class Denied(Exception):
    """The request was understood and refused. The message is the reason."""

    def __init__(self, reason: str, condition: str) -> None:
        super().__init__(reason)
        self.condition = condition


@dataclass(frozen=True, slots=True)
class Peer:
    pid: int
    uid: int
    #: The caller's own systemd unit, or None when the pid is gone or its cgroup
    #: names nothing we recognise. None is a refusal, never a benefit of doubt.
    unit: str | None
    #: Whether the pid was pinned by a pidfd while its unit was read. False means
    #: the kernel predates SO_PEERPIDFD and a pid-recycling race exists in
    #: principle; see `_peer_by_credentials`.
    pinned: bool


def peer_of(connection: socket.socket, *, proc_root: Path = Path("/proc")) -> Peer:
    """Identify the caller: pid, uid, and the systemd unit it runs under."""

    raw = connection.getsockopt(
        socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i")
    )
    pid, uid, _gid = struct.unpack("3i", raw)
    try:
        pidfd = connection.getsockopt(socket.SOL_SOCKET, _SO_PEERPIDFD)
    except OSError:
        return _peer_by_credentials(pid, uid, proc_root=proc_root)
    try:
        # The pidfd holds the pid, so this read cannot land on a different
        # process than the one that connected.
        return Peer(
            pid=pid, uid=uid, unit=unit_of_process(pid, proc_root=proc_root), pinned=True
        )
    finally:
        os.close(pidfd)


def _peer_by_credentials(pid: int, uid: int, *, proc_root: Path) -> Peer:
    """Pre-6.5 fallback: SO_PEERCRED, stamped by the kernel at connect() time.

    The pid is not pinned, so in principle the caller could exit and its pid be
    recycled before the cgroup read. What a winner of that race gains is bounded
    by the socket's own permissions: it is group-`eidolon`, the `eidolon` uid
    belongs to Eidolon's own units, and the prize is start/stop/restart of a unit
    already on the managed list. That is not an escalation, which is why the
    fallback is allowed to exist rather than refusing outright on old kernels.
    """

    return Peer(pid=pid, uid=uid, unit=unit_of_process(pid, proc_root=proc_root), pinned=False)


def unit_of_process(pid: int, *, proc_root: Path = Path("/proc")) -> str | None:
    """Resolve a pid's systemd unit from its cgroup, the way systemd does."""

    try:
        text = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    path = _cgroup_path(text)
    if path is None:
        return None
    for component in path.strip("/").split("/"):
        if component.endswith(_SLICE_SUFFIX):
            continue
        return component if component.endswith(_UNIT_SUFFIXES) else None
    return None


def _cgroup_path(text: str) -> str | None:
    """The unified path, or the v1 systemd hierarchy. No other line is authoritative."""

    fallback: str | None = None
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hierarchy, controllers, path = fields
        if hierarchy == "0" and not controllers:
            return path
        if controllers == "name=systemd":
            fallback = path
    return fallback


def authorize(
    request: Request,
    peer: Peer,
    *,
    allowed_units: frozenset[str],
    caller_unit: str = MANAGER_UNIT,
) -> None:
    """Raise `Denied` unless every condition holds. Silence means allowed."""

    if request.verb not in VERBS:
        raise Denied(f"verb {request.verb!r} is not appliable", "verb")
    if request.unit not in allowed_units:
        raise Denied(f"unit {request.unit!r} is not a managed unit", "unit")
    if peer.unit != caller_unit:
        raise Denied(
            f"caller pid {peer.pid} runs under {peer.unit or 'no known unit'}, "
            f"not {caller_unit}",
            "caller",
        )
