"""Fail-closed adapter until a stable Companion authority contract exists."""

from eidolon_kernel.domain.errors import AuthorityUnavailable
from eidolon_kernel.domain.model import CompanionIdentity


class UnavailableCompanionAuthority:
    async def get_companion(self, *, companion_id: str) -> CompanionIdentity:
        raise AuthorityUnavailable(
            "Companion authority contract is unavailable; see ADR-0002"
        )
