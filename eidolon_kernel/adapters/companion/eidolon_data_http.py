"""Narrow consumer of Eidolon Data V2's Companion Identity V1 authority."""

from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx
from jsonschema import ValidationError
from pydantic import ValidationError as PydanticValidationError

from eidolon_kernel.contracts.bindings import CompanionIdentityWire
from eidolon_kernel.contracts.mappers import companion_identity_to_domain
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.errors import AuthorityRejected, AuthorityUnavailable
from eidolon_kernel.domain.model import CompanionIdentity


class EidolonDataHttpCompanionAuthority:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        contracts: ContractRegistry,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Companion authority base_url must be HTTP(S)")
        if len(bearer_token.strip()) < 24:
            raise ValueError("Companion authority bearer token is invalid")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token.strip()
        self._contracts = contracts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def get_companion(self, *, companion_id: str) -> CompanionIdentity:
        url = (
            f"{self._base_url}/api/companion-authority/v1/companions/{quote(companion_id, safe='')}"
        )
        try:
            response = await self._client.get(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise AuthorityUnavailable("Companion authority is unreachable") from exc
        if response.status_code == 404:
            raise AuthorityRejected("Companion does not exist")
        if response.status_code in {401, 403}:
            raise AuthorityUnavailable("Companion authority rejected Kernel credential")
        if response.status_code >= 500:
            raise AuthorityUnavailable("Companion authority failed")
        if response.status_code != 200:
            raise AuthorityUnavailable(
                f"unexpected Companion authority status {response.status_code}"
            )
        try:
            document = response.json()
            if not isinstance(document, dict):
                raise TypeError("Companion authority response must be an object")
            self._contracts.validate(
                "external/companion-identity.schema.json",
                document,
            )
            wire = CompanionIdentityWire.model_validate(document)
        except (TypeError, ValueError, ValidationError, PydanticValidationError) as exc:
            raise AuthorityUnavailable(
                "Companion authority response violated consumed contract"
            ) from exc
        return companion_identity_to_domain(wire)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
