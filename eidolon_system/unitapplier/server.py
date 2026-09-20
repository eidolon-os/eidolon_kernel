"""The root-side applier: the only place a unit verb runs with privilege.

Socket-activated, serial, and loud. Every request is logged with its outcome —
accepted for asynchronous execution, refused with the condition that failed, or failed with systemctl's own
message — because the failure this replaces was a refusal nothing recorded.

The allowlist is not a list. It is derived, on every start, from the same
`system-services.yaml` the manager loads through the same loader, so "which
units may be actuated" has exactly one definition on the Host and a second copy
cannot drift away from it.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from eidolon_system.adapters.host_profile import declared_host_capabilities
from eidolon_system.adapters.manifest.yaml_file import YamlServiceManifest
from eidolon_system.unitapplier.authorize import (
    MANAGER_UNIT,
    Denied,
    Peer,
    authorize,
    peer_of,
)
from eidolon_system.unitapplier.protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    Request,
    Response,
    decode_request,
)

#: systemd hands inherited listeners starting here.
_LISTEN_FDS_START = 3

#: How long a connected caller has to send its request line before we drop it.
#: The loop is serial on purpose — two concurrent `systemctl start` runs against
#: the same unit is not a thing worth supporting — so a caller that connects and
#: says nothing must not be able to hold the queue.
_REQUEST_TIMEOUT_SECONDS = 5.0

_DEFAULT_MANIFEST = Path("/etc/eidolon/system-services.yaml")
_DEFAULT_SOCKET = Path("/run/eidolon-unit-applier.sock")

_log = logging.getLogger("eidolon.unitapplier")


def managed_units(
    manifest_path: Path, capabilities: frozenset[str] = frozenset()
) -> frozenset[str]:
    """The systemd units the manifest says this driver actuates, on this Host.

    Filtered by the same capabilities eidolond filters by, and for the same
    reason: this allowlist is what the applier will act on at all. A Host
    without the capability has no such unit installed, so allowing its name
    would be allowing something that cannot exist — and a Host with it needs
    the name here or the manager's own start would be refused by its applier.

    Both facts come from one filter over one file, which is the property worth
    having: there is no second place to keep in step.
    """

    catalog = YamlServiceManifest(manifest_path, capabilities=capabilities).load()
    return frozenset(
        definition.target_for("systemd")
        for definition in catalog.definitions
        if definition.manages("systemd")
    )


class UnitApplier:
    def __init__(
        self,
        *,
        allowed_units: frozenset[str],
        systemctl: str = "/usr/bin/systemctl",
        caller_unit: str = MANAGER_UNIT,
        # Injected because SO_PEERCRED is Linux-only, and the decision this
        # class makes has to be testable on a machine that has no such thing.
        identify: Callable[[socket.socket], Peer] = peer_of,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        self._power_off_started = False
        self.allowed_units = allowed_units
        self.systemctl = systemctl
        self.caller_unit = caller_unit
        self.identify = identify
        self.command_timeout_seconds = command_timeout_seconds

    def handle(self, connection: socket.socket) -> Response:
        try:
            line = _read_frame(connection)
        except (OSError, ProtocolError) as exc:
            _log.warning("refused: unreadable request (%s)", exc)
            return Response(ok=False, error="malformed request")
        try:
            request = decode_request(line)
        except ProtocolError as exc:
            _log.warning("refused: %s", exc)
            return Response(ok=False, error="malformed request")
        peer = self.identify(connection)
        try:
            authorize(
                request,
                peer,
                allowed_units=self.allowed_units,
                caller_unit=self.caller_unit,
            )
        except Denied as exc:
            _log.warning(
                "refused %s %s for pid %d uid %d on %s: %s",
                request.verb,
                request.unit,
                peer.pid,
                peer.uid,
                exc.condition,
                exc,
            )
            return Response(ok=False, error=str(exc))
        if request.verb in {"power-status", "poweroff"}:
            return self._power(request, peer.pid)
        return self._apply(request, peer.pid)

    def _power(self, request: Request, pid: int) -> Response:
        # This process already runs as root. No caller controls the executable,
        # arguments, or target; shutdown -f / arbitrary unit names are impossible.
        executable = next((p for p in ("/usr/sbin/poweroff", "/sbin/poweroff")
                           if Path(p).is_file() and os.access(p, os.X_OK)), None)
        if executable is None:
            return Response(ok=False, error="poweroff command unavailable")
        if self._power_off_started:
            return Response(ok=False, error="poweroff already requested; check Host state")
        if request.verb == "power-status":
            return Response(ok=True)
        self._power_off_started = True
        try:
            result = subprocess.run([executable], capture_output=True, text=True,
                                    timeout=min(self.command_timeout_seconds, 8), check=False)
        except subprocess.TimeoutExpired:
            _log.error("poweroff outcome unknown for pid %d: timed out", pid)
            return Response(ok=False, error="poweroff outcome unknown")
        except OSError:
            self._power_off_started = False
            _log.exception("poweroff unavailable for pid %d", pid)
            return Response(ok=False, error="poweroff unavailable")
        if result.returncode != 0:
            self._power_off_started = False
            _log.error("poweroff rejected for pid %d, exit %d", pid, result.returncode)
            return Response(ok=False, error="poweroff rejected")
        _log.info("poweroff accepted for pid %d", pid)
        return Response(ok=True)

    def _apply(self, request: Request, pid: int) -> Response:
        try:
            result = subprocess.run(
                [self.systemctl, "--no-block", request.verb, "--", request.unit],
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _log.error(
                "failed %s %s for pid %d: systemctl timed out after %.0fs",
                request.verb, request.unit, pid, self.command_timeout_seconds,
            )
            return Response(ok=False, error="systemctl timed out")
        except OSError as exc:
            _log.error(
                "failed %s %s for pid %d: %s", request.verb, request.unit, pid, exc
            )
            return Response(ok=False, error=f"systemctl unavailable: {exc}")
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            _log.error(
                "failed %s %s for pid %d: systemctl exit %d: %s",
                request.verb, request.unit, pid, result.returncode, detail,
            )
            return Response(ok=False, error=detail)
        _log.info("accepted %s %s for pid %d", request.verb, request.unit, pid)
        return Response(ok=True)

    def serve(self, listener: socket.socket) -> None:
        _log.info(
            "applier listening; %d managed units, caller must be %s",
            len(self.allowed_units),
            self.caller_unit,
        )
        while True:
            try:
                connection, _ = listener.accept()
            except OSError as exc:
                _log.error("accept failed, stopping: %s", exc)
                return
            with connection:
                connection.settimeout(_REQUEST_TIMEOUT_SECONDS)
                response = self.handle(connection)
                try:
                    connection.sendall(response.encode())
                except OSError as exc:
                    # The verb already ran. Say so, or an operator reading the
                    # journal would see a client-side timeout and no cause.
                    _log.warning("could not answer caller: %s", exc)


def _read_frame(connection: socket.socket) -> bytes:
    buffer = bytearray()
    while b"\n" not in buffer:
        if len(buffer) > MAX_FRAME_BYTES:
            raise ProtocolError("request exceeds the applier frame limit")
        chunk = connection.recv(MAX_FRAME_BYTES)
        if not chunk:
            raise ProtocolError("caller closed before sending a request")
        buffer.extend(chunk)
    return bytes(buffer.split(b"\n", 1)[0])


def inherited_listener() -> socket.socket | None:
    """The socket systemd activated us with, if that is how we were started."""

    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    count = int(os.environ.get("LISTEN_FDS", "0"))
    if count != 1:
        raise RuntimeError(f"expected exactly one activated socket, got {count}")
    return socket.socket(fileno=_LISTEN_FDS_START)


def _bound_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path.unlink(missing_ok=True)
    listener.bind(str(path))
    listener.listen(16)
    return listener


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eidolon-unit-applier")
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--systemctl", default="/usr/bin/systemctl")
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="bind this path instead of using socket activation",
    )
    parser.add_argument(
        "--caller-unit",
        default=MANAGER_UNIT,
        help="the systemd unit a caller must run under",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        allowed = managed_units(arguments.manifest, declared_host_capabilities())
    except Exception as exc:  # a manifest we cannot read is not a partial start
        _log.error("cannot derive managed units from %s: %s", arguments.manifest, exc)
        return 1
    applier = UnitApplier(
        allowed_units=allowed,
        systemctl=arguments.systemctl,
        caller_unit=arguments.caller_unit,
    )
    listener = inherited_listener()
    if listener is None:
        if arguments.socket is None:
            _log.error("no activated socket and no --socket given")
            return 1
        listener = _bound_listener(arguments.socket)
    with listener:
        applier.serve(listener)
    return 0


def main() -> None:
    sys.exit(run())
