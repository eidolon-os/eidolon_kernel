"""Machine-scoped System Service resolution port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ServiceDirectoryUnavailable(Exception):
    """The machine-scoped directory cannot resolve a required service endpoint."""


@dataclass(frozen=True, slots=True)
class ResolvedServiceEndpoint:
    service_id: str
    endpoint_id: str
    protocol: str
    address: str
    contract: str


class SystemServiceDirectory(Protocol):
    async def resolve(
        self,
        *,
        service_id: str,
        endpoint_id: str,
        required_protocol: str,
        required_contract: str,
    ) -> ResolvedServiceEndpoint: ...
