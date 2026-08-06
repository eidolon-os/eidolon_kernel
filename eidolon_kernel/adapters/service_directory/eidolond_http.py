"""Strict consumer of eidolond's machine-scoped endpoint resolution API."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from jsonschema import ValidationError
from pydantic import ValidationError as PydanticValidationError

from eidolon_kernel.contracts.bindings import SystemServiceEndpointWire
from eidolon_kernel.contracts.mappers import system_service_endpoint_to_port
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.ports.system_services import (
    ResolvedServiceEndpoint,
    ServiceDirectoryUnavailable,
)


class EidolondHttpServiceDirectory:
    def __init__(
        self,
        *,
        base_url: str,
        contracts: ContractRegistry,
        uds_path: Path | None = None,
        timeout_seconds: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("System Service Directory base_url must be HTTP(S)")
        self._base_url = base_url.rstrip("/")
        self._contracts = contracts
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            transport = (
                httpx.AsyncHTTPTransport(uds=str(uds_path.resolve()))
                if uds_path is not None
                else None
            )
            self._client = httpx.AsyncClient(
                timeout=timeout_seconds,
                transport=transport,
                trust_env=False,
            )

    async def resolve(
        self,
        *,
        service_id: str,
        endpoint_id: str,
        required_protocol: str,
        required_contract: str,
    ) -> ResolvedServiceEndpoint:
        url = (
            f"{self._base_url}/api/system/v1/services/"
            f"{quote(service_id, safe='')}/endpoints/{quote(endpoint_id, safe='')}"
        )
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise ServiceDirectoryUnavailable(
                "System Service Directory is unreachable"
            ) from exc
        if response.status_code == 404:
            raise ServiceDirectoryUnavailable(
                f"system service endpoint is not available: {service_id}/{endpoint_id}"
            )
        if response.status_code == 503:
            raise ServiceDirectoryUnavailable(
                f"system service endpoint is not ready: {service_id}/{endpoint_id}"
            )
        if response.status_code != 200:
            raise ServiceDirectoryUnavailable(
                f"unexpected System Service Directory status {response.status_code}"
            )
        try:
            document = response.json()
            if not isinstance(document, dict):
                raise TypeError("Service Directory response must be an object")
            self._contracts.validate(
                "external/system-service-endpoint.schema.json", document
            )
            wire = SystemServiceEndpointWire.model_validate(document)
        except (TypeError, ValueError, ValidationError, PydanticValidationError) as exc:
            raise ServiceDirectoryUnavailable(
                "System Service Directory response violated consumed endpoint contract"
            ) from exc
        if wire.service_id != service_id or wire.endpoint_id != endpoint_id:
            raise ServiceDirectoryUnavailable(
                "System Service Directory returned a different endpoint identity"
            )
        if wire.protocol != required_protocol:
            raise ServiceDirectoryUnavailable(
                f"system service endpoint protocol mismatch: expected {required_protocol}"
            )
        address = urlparse(wire.address)
        if wire.protocol == "http" and (
            address.scheme not in {"http", "https"} or not address.netloc
        ):
            raise ServiceDirectoryUnavailable(
                "system service endpoint address does not match HTTP protocol"
            )
        if wire.contract != required_contract:
            raise ServiceDirectoryUnavailable(
                f"system service endpoint contract mismatch: expected {required_contract}"
            )
        return system_service_endpoint_to_port(wire)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
