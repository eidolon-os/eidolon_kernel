"""Client side of the privileged applier, used by the systemd adapter.

The manager runs as `eidolon` and cannot start a unit. It states the intent on a
socket; root decides and acts. This module is the unprivileged half and knows
nothing about the decision — only how to ask and how to report the answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.unitapplier.protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    Request,
    decode_response,
)


class ApplierUnitMutator:
    """Apply a unit verb by asking the root applier to do it."""

    mechanism = "unit-applier"

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def apply(self, verb: str, unit: str) -> None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                answer = await self._exchange(Request(verb=verb, unit=unit))
        except TimeoutError as exc:
            raise HostOperationFailed(
                f"unit applier did not answer {verb} {unit} within "
                f"{self.timeout_seconds:.0f}s"
            ) from exc
        except (OSError, ProtocolError) as exc:
            raise HostOperationFailed(
                f"unit applier unreachable at {self.socket_path} for {verb} {unit}: {exc}"
            ) from exc
        if not answer.ok:
            raise HostOperationFailed(
                f"unit applier refused {verb} {unit}: {answer.error or 'no reason given'}"
            )

    async def _exchange(self, request: Request):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(request.encode())
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                # The applier answers and closes; a reset on our own close is
                # not an outcome, and must not mask the answer we already read.
                pass
        if not line:
            raise ProtocolError("applier closed without answering")
        if len(line) > MAX_FRAME_BYTES:
            raise ProtocolError("applier answer exceeds the frame limit")
        return decode_response(line.rstrip(b"\n"))
