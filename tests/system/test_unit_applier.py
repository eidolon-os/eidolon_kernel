"""The privilege boundary: who may apply which verb, and what a refusal says.

eidolond runs unprivileged and every start it attempted was refused. The refusal
carried no reason because polkit decided it. These tests hold the replacement to
the property that was missing: each condition is separable, and each refusal
names the condition it failed on.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import platform
import shutil
import socket
import struct
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from eidolon_system.adapters.host.systemd import SystemdHostSupervisor
from eidolon_system.adapters.host.unit_applier import ApplierUnitMutator
from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.unitapplier.authorize import (
    MANAGER_UNIT,
    Denied,
    Peer,
    authorize,
    peer_of,
    unit_of_process,
)
from eidolon_system.unitapplier.protocol import (
    ProtocolError,
    Request,
    Response,
    decode_request,
    decode_response,
)
from eidolon_system.unitapplier.server import UnitApplier, managed_units

ALLOWED = frozenset({"eidolon-nats.service", "eidolon-kernel.service"})
MANAGER = Peer(pid=4242, uid=997, unit=MANAGER_UNIT, pinned=True)


def _denial(request: Request, peer: Peer) -> Denied:
    with pytest.raises(Denied) as raised:
        authorize(request, peer, allowed_units=ALLOWED)
    return raised.value


def test_the_manager_may_apply_the_three_verbs_to_a_managed_unit() -> None:
    for verb in ("start", "stop", "restart"):
        authorize(
            Request(verb=verb, unit="eidolon-nats.service"),
            MANAGER,
            allowed_units=ALLOWED,
        )


def test_each_condition_refuses_separately_and_names_itself() -> None:
    # Install-time verbs are not runtime verbs. Ops performs those as root.
    assert _denial(
        Request(verb="enable", unit="eidolon-nats.service"), MANAGER
    ).condition == "verb"
    assert _denial(
        Request(verb="daemon-reload", unit="eidolon-nats.service"), MANAGER
    ).condition == "verb"
    # A unit outside the manifest, including one root would love to be asked for.
    assert _denial(Request(verb="start", unit="sshd.service"), MANAGER).condition == "unit"
    # Right uid, wrong unit: a shell running as `eidolon` gets nothing.
    shell = Peer(pid=99, uid=997, unit="session-3.scope", pinned=False)
    assert _denial(
        Request(verb="start", unit="eidolon-nats.service"), shell
    ).condition == "caller"
    # A caller whose unit could not be resolved is refused, not trusted.
    unknown = Peer(pid=99, uid=997, unit=None, pinned=False)
    refusal = _denial(Request(verb="start", unit="eidolon-nats.service"), unknown)
    assert refusal.condition == "caller"
    assert "no known unit" in str(refusal)


def test_root_is_not_special_and_uid_alone_grants_nothing() -> None:
    # Authority is the caller's unit, never its uid. A root process that is not
    # eidolond has no business driving the product's units through this socket,
    # and eidolond keeps its authority even though its uid is unprivileged.
    root_shell = Peer(pid=7, uid=0, unit="sshd.service", pinned=True)
    assert _denial(
        Request(verb="start", unit="eidolon-nats.service"), root_shell
    ).condition == "caller"
    authorize(
        Request(verb="start", unit="eidolon-nats.service"),
        Peer(pid=8, uid=997, unit=MANAGER_UNIT, pinned=True),
        allowed_units=ALLOWED,
    )


@pytest.mark.parametrize(
    ("cgroup", "expected"),
    [
        # cgroup v2, the case that matters on the board.
        ("0::/system.slice/eidolond.service\n", "eidolond.service"),
        # A unit that delegated sub-cgroups still resolves to the unit itself.
        ("0::/system.slice/eidolond.service/payload\n", "eidolond.service"),
        # Nested slices are skipped the way systemd skips them.
        ("0::/user.slice/user-1000.slice/session-3.scope\n", "session-3.scope"),
        # cgroup v1: only the systemd hierarchy is authoritative about units.
        (
            "12:pids:/irrelevant\n1:name=systemd:/system.slice/eidolond.service\n",
            "eidolond.service",
        ),
        # The root cgroup names no unit, and neither does a path we cannot read
        # as one. Both must be None so the caller refuses instead of guessing.
        ("0::/\n", None),
        ("0::/system.slice/something-odd\n", None),
        ("12:pids:/system.slice/eidolond.service\n", None),
        ("", None),
    ],
)
def test_a_caller_unit_is_read_from_its_cgroup_the_way_systemd_reads_it(
    tmp_path: Path, cgroup: str, expected: str | None
) -> None:
    (tmp_path / "31").mkdir()
    (tmp_path / "31" / "cgroup").write_text(cgroup, encoding="utf-8")

    assert unit_of_process(31, proc_root=tmp_path) == expected


def test_a_pid_that_is_gone_resolves_to_no_unit(tmp_path: Path) -> None:
    assert unit_of_process(31, proc_root=tmp_path) is None


def test_a_malformed_frame_is_refused_without_reaching_proc() -> None:
    for line in (b"not json", b"[]", b'{"verb":"start"}', b'{"verb":1,"unit":"a"}'):
        with pytest.raises(ProtocolError):
            decode_request(line)


def test_a_frame_over_the_limit_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(ProtocolError):
        decode_request(b'{"verb":"start","unit":"' + b"a" * 5000 + b'"}')


def test_the_wire_round_trips() -> None:
    assert decode_request(Request("start", "x.service").encode().rstrip(b"\n")) == Request(
        "start", "x.service"
    )
    assert decode_response(Response(ok=True).encode().rstrip(b"\n")) == Response(ok=True)
    assert decode_response(
        Response(ok=False, error="no").encode().rstrip(b"\n")
    ) == Response(ok=False, error="no")


def test_the_allowlist_is_derived_from_the_manifest_the_manager_loads() -> None:
    root = Path(__file__).resolve().parents[2]

    derived = managed_units(root / "config/system-services.yaml")

    # Not a copy of a list: the same file, through the same loader. `external`
    # targets are catalogued but unactuatable, and must not be granted.
    assert "eidolon-nats.service" in derived
    assert "external" not in derived
    assert all(unit.endswith(".service") for unit in derived)


# ---------------------------------------------------------------------------
# The applier over a real socket. `handle` is exercised everywhere by injecting
# the peer; `peer_of` itself needs Linux SO_PEERCRED and is checked there only.


def _pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def _served(applier: UnitApplier, request: bytes) -> Response:
    server, client = _pair()
    with server, client:
        client.sendall(request)
        response = applier.handle(server)
    return response


def test_a_refused_request_never_runs_systemctl(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    fake = tmp_path / "systemctl"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    applier = UnitApplier(
        allowed_units=ALLOWED,
        systemctl=str(fake),
        identify=lambda _: Peer(pid=1, uid=997, unit="session-3.scope", pinned=False),
    )

    answer = _served(applier, Request("start", "eidolon-nats.service").encode())

    assert answer.ok is False
    assert "not eidolond.service" in answer.error
    assert not marker.exists()


def test_an_authorised_request_applies_the_verb_and_says_so(tmp_path: Path) -> None:
    argv = tmp_path / "argv"
    fake = tmp_path / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" > {argv}\n', encoding="utf-8")
    fake.chmod(0o755)
    applier = UnitApplier(
        allowed_units=ALLOWED, systemctl=str(fake), identify=lambda _: MANAGER
    )

    answer = _served(applier, Request("restart", "eidolon-kernel.service").encode())

    assert answer == Response(ok=True)
    # `--` because a unit name is data: it arrives over a socket and must never
    # be able to present itself to systemctl as an option.
    assert argv.read_text(encoding="utf-8").split() == [
        "--no-block",        "restart",
        "--",
        "eidolon-kernel.service",
    ]


def test_systemctl_failure_is_reported_with_its_own_message(tmp_path: Path) -> None:
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\necho 'Unit not found.' >&2\nexit 5\n", encoding="utf-8")
    fake.chmod(0o755)
    applier = UnitApplier(
        allowed_units=ALLOWED, systemctl=str(fake), identify=lambda _: MANAGER
    )

    answer = _served(applier, Request("start", "eidolon-nats.service").encode())

    assert answer.ok is False
    assert "Unit not found." in answer.error


def test_every_outcome_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    allowed = UnitApplier(
        allowed_units=ALLOWED, systemctl=str(fake), identify=lambda _: MANAGER
    )
    refused = UnitApplier(
        allowed_units=ALLOWED,
        systemctl=str(fake),
        identify=lambda _: Peer(pid=9, uid=997, unit=None, pinned=False),
    )

    with caplog.at_level(logging.INFO, logger="eidolon.unitapplier"):
        _served(allowed, Request("start", "eidolon-nats.service").encode())
        _served(refused, Request("start", "eidolon-nats.service").encode())

    messages = [record.getMessage() for record in caplog.records]
    # The whole reason this replaced a polkit rule: an operator reading the
    # journal can see the decision and its cause.
    assert any("accepted start eidolon-nats.service" in line for line in messages)
    assert any(
        "refused start eidolon-nats.service" in line and "caller" in line
        for line in messages
    )


def test_a_caller_that_says_nothing_does_not_hold_the_queue(tmp_path: Path) -> None:
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    applier = UnitApplier(
        allowed_units=ALLOWED, systemctl=str(fake), identify=lambda _: MANAGER
    )
    server, client = _pair()

    with server, client:
        client.close()
        answer = applier.handle(server)

    assert answer == Response(ok=False, error="malformed request")


# ---------------------------------------------------------------------------
# Client adapter: the manager's mutating verbs cross the socket, and its read
# verb does not.


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A directory short enough to hold an AF_UNIX path.

    macOS puts pytest's tmp_path under /private/var/folders/..., which alone
    overruns the 104-byte sun_path limit. /tmp is the conventional escape.
    """

    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _serve_once(applier: UnitApplier, path: Path) -> threading.Thread:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def once() -> None:
        with listener:
            connection, _ = listener.accept()
            with connection:
                connection.sendall(applier.handle(connection).encode())

    thread = threading.Thread(target=once, daemon=True)
    thread.start()
    return thread


def test_the_manager_start_verb_crosses_the_socket_and_root_applies_it(
    tmp_path: Path, socket_dir: Path
) -> None:
    argv = tmp_path / "argv"
    fake = tmp_path / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" > {argv}\n', encoding="utf-8")
    fake.chmod(0o755)
    path = socket_dir / "applier.sock"
    thread = _serve_once(
        UnitApplier(
            allowed_units=ALLOWED, systemctl=str(fake), identify=lambda _: MANAGER
        ),
        path,
    )
    host = SystemdHostSupervisor(mutator=ApplierUnitMutator(path))

    asyncio.run(host.start("eidolon-nats.service"))
    thread.join(timeout=5)

    assert argv.read_text(encoding="utf-8").split() == [
        "--no-block",        "start",
        "--",
        "eidolon-nats.service",
    ]


def test_a_refusal_reaches_the_manager_as_a_host_failure_with_the_reason(
    tmp_path: Path, socket_dir: Path
) -> None:
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    path = socket_dir / "applier.sock"
    thread = _serve_once(
        UnitApplier(
            allowed_units=ALLOWED,
            systemctl=str(fake),
            identify=lambda _: Peer(pid=9, uid=997, unit="session-3.scope", pinned=False),
        ),
        path,
    )
    host = SystemdHostSupervisor(mutator=ApplierUnitMutator(path))

    with pytest.raises(HostOperationFailed) as raised:
        asyncio.run(host.start("eidolon-nats.service"))
    thread.join(timeout=5)

    # The reason has to survive the hop, or the manager publishes a failure as
    # opaque as the one this whole change exists to remove.
    assert "not eidolond.service" in str(raised.value)


def test_an_absent_applier_is_a_named_failure_not_a_hang(socket_dir: Path) -> None:
    host = SystemdHostSupervisor(
        mutator=ApplierUnitMutator(socket_dir / "missing.sock", timeout_seconds=2.0)
    )

    with pytest.raises(HostOperationFailed) as raised:
        asyncio.run(host.start("eidolon-nats.service"))

    assert "unit applier unreachable" in str(raised.value)


def test_inspect_does_not_cross_the_privilege_boundary(tmp_path: Path) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        mechanism = "recorder"

        async def apply(self, verb: str, unit: str) -> None:
            self.calls.append((verb, unit))

    class Runner:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        async def run(self, *command: str):
            self.commands.append(command)

            class Result:
                returncode = 0
                stdout = "ActiveState=active\nSubState=running\n"
                stderr = ""

            return Result()

    recorder, runner = Recorder(), Runner()
    host = SystemdHostSupervisor(runner=runner, mutator=recorder)

    state = asyncio.run(host.inspect("eidolon-nats.service"))

    # `systemctl show` is world-readable. Routing it through root would put a
    # privileged process on the reconciliation hot path for no authority gained.
    assert state.active is True
    assert recorder.calls == []
    assert runner.commands and runner.commands[0][1] == "show"


def test_a_kernel_without_peerpidfd_still_identifies_the_caller(
    tmp_path: Path,
) -> None:
    """The RK3588 board runs 6.1 and answers ENOPROTOOPT to SO_PEERPIDFD.

    Checked on the board rather than assumed: `getsockopt(SOL_SOCKET, 77)`
    raises OSError(92) there, so the fallback is the path that actually runs in
    the product. It must still identify the caller, not fail the connection.
    """

    (tmp_path / "77").mkdir()
    (tmp_path / "77" / "cgroup").write_text(
        "0::/system.slice/eidolond.service\n", encoding="utf-8"
    )

    class OldKernelSocket:
        def getsockopt(self, level, option, *size):
            if option == 77:
                raise OSError(errno.ENOPROTOOPT, "Protocol not available")
            return struct.pack("3i", 77, 997, 997)

    peer = peer_of(OldKernelSocket(), proc_root=tmp_path)

    assert peer == Peer(pid=77, uid=997, unit="eidolond.service", pinned=False)
    # And it is still authorised: the pin is a property of how the caller was
    # identified, never a condition on whether it may act.
    authorize(
        Request("start", "eidolon-nats.service"), peer, allowed_units=ALLOWED
    )


@pytest.mark.skipif(
    platform.system() != "Linux", reason="SO_PEERCRED is a Linux socket option"
)
def test_peer_of_reports_this_process_on_linux() -> None:
    server, client = _pair()
    with server, client:
        peer = peer_of(server)

    # Both ends are this process, so its own pid and uid are the answer, and its
    # unit is whatever ran the test suite — asserted only as "resolved or not",
    # since a pytest run is not under a systemd unit.
    assert peer.pid == os.getpid()
    assert peer.uid == os.getuid()
    assert peer.unit is None or peer.unit.endswith((".service", ".scope"))


def test_the_applier_allowlist_follows_the_hosts_capabilities(tmp_path) -> None:
    """One filter over one file gives both halves, which is the point.

    The allowlist is what the root applier will act on at all. A Host without
    the capability has no such unit installed, so allowing its name would allow
    something that cannot exist; a Host with it needs the name here or the
    manager's own start would be refused by its own applier.

    Derived from the same manifest, through the same loader, filtered by the
    same declaration eidolond filters by — so there is no second place to keep
    in step.
    """

    manifest = Path(__file__).resolve().parents[2] / "config/system-services.yaml"

    plain = managed_units(manifest)
    npu = managed_units(manifest, frozenset({"local_asr"}))

    assert "eidolon-asr.service" not in plain
    assert "eidolon-asr.service" in npu
    assert plain < npu
