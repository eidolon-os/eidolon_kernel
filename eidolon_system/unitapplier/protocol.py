"""The applier wire: one JSON request line in, one JSON response line out.

Shared by the unprivileged client adapter and the root server, so the two can
never disagree about the frame. Deliberately smaller than the verbs the
`HostServiceSupervisor` port exposes: `inspect` needs no privilege and never
crosses this socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: The only mutations that cross the privilege boundary. Anything else — enable,
#: disable, daemon-reload, mask — is an install-time concern that Ops performs
#: as root, not a runtime one the manager may reach for.
VERBS = frozenset({"start", "stop", "restart"})
# Machine power is a separate fixed-target capability, never a unit verb.
# power-status only probes availability; poweroff never takes caller arguments.
POWER_VERBS = frozenset({"power-status", "poweroff"})
HOST_POWER_TARGET = "@host"

#: A request or response line, cap included. The frame carries two short
#: identifiers; anything larger is a caller that has lost the protocol, and
#: reading it to the end would be reading whatever it chose to send.
MAX_FRAME_BYTES = 4096


class ProtocolError(ValueError):
    """A frame that is not a well-formed request or response."""


@dataclass(frozen=True, slots=True)
class Request:
    verb: str
    unit: str

    def encode(self) -> bytes:
        return _encode({"verb": self.verb, "unit": self.unit})


@dataclass(frozen=True, slots=True)
class Response:
    ok: bool
    error: str = ""

    def encode(self) -> bytes:
        payload: dict[str, object] = {"ok": self.ok}
        if self.error:
            payload["error"] = self.error
        return _encode(payload)


def _encode(payload: dict[str, object]) -> bytes:
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds the applier frame limit")
    return frame


def _document(line: bytes) -> dict[str, object]:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds the applier frame limit")
    try:
        document = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("frame is not UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ProtocolError("frame must be a JSON object")
    return document


def decode_request(line: bytes) -> Request:
    document = _document(line)
    verb = document.get("verb")
    unit = document.get("unit")
    if not isinstance(verb, str) or not isinstance(unit, str):
        raise ProtocolError("request requires string verb and unit")
    return Request(verb=verb, unit=unit)


def decode_response(line: bytes) -> Response:
    document = _document(line)
    ok = document.get("ok")
    error = document.get("error", "")
    if not isinstance(ok, bool) or not isinstance(error, str):
        raise ProtocolError("response requires boolean ok and string error")
    return Response(ok=ok, error=error)
