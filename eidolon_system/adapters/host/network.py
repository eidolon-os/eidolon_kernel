"""Current OS network inputs, with settling and disposable applied observations.

psutil already supplies Host telemetry. Use its cross-platform enumeration rather
than another platform-specific address picker. No DNS, Internet probe or watcher
process: the existing service reconciliation loop samples this adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import time
import uuid
from pathlib import Path

import psutil


def network_fingerprint() -> str | None:
    interfaces = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    addresses = []
    for name, entries in interfaces.items():
        if name not in stats or not stats[name].isup:
            continue
        for entry in entries:
            if entry.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            address = ipaddress.ip_address(entry.address.split("%", 1)[0])
            if address.is_loopback or address.is_link_local or address.is_unspecified:
                continue
            addresses.append((name, str(address), entry.netmask))
    if not addresses:
        return None
    # UDP connect performs a local route lookup; it sends no packet and does
    # not require Internet access. Include route selection as well as NICs.
    routes = []
    for family, destination in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as connection:
                connection.connect((destination, 9))
                routes.append(connection.getsockname()[0])
        except OSError:
            routes.append(None)
    payload = json.dumps([sorted(addresses), routes], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class LocalNetworkEnvironment:
    def __init__(
        self,
        path: Path,
        *,
        read=network_fingerprint,
        monotonic=time.monotonic,
        settle_seconds: float = 10.0,
    ):
        self.path = path
        self.read = read
        self.monotonic = monotonic
        self.settle_seconds = settle_seconds
        self._candidate = None
        self._since = 0.0
        try:
            document = json.loads(path.read_text())
            self._applied = (
                document
                if isinstance(document, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in document.items())
                else {}
            )
        except (OSError, ValueError):
            self._applied = {}

    async def snapshot(self) -> str | None:
        try:
            current = await asyncio.to_thread(self.read)
        except (OSError, ValueError, psutil.Error):
            current = None
        now = self.monotonic()
        if current != self._candidate:
            self._candidate, self._since = current, now
        if current is None or now - self._since < self.settle_seconds:
            return None
        return current

    def applied(self, service_id: str) -> str | None:
        return self._applied.get(service_id)

    def record(self, service_id: str, fingerprint: str) -> None:
        updated = {**self._applied, service_id: fingerprint}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(updated, stream, sort_keys=True)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._applied = updated
