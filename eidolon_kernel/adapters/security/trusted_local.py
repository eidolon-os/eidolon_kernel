"""Explicit trust adapter for a loopback-only first deployment."""

from eidolon_kernel.domain.errors import AuthorizationDenied
from eidolon_kernel.domain.model import require_identifier


class TrustedLocalOwnerAuthorizer:
    """Trust an Owner identity hint supplied by the same-host ingress.

    This is deliberately not presented as authentication. Composition only enables it
    under the ADR-0003 loopback/trusted-process deployment assumption.
    """

    async def authorize(
        self,
        *,
        action: str,
        credential: str | None,
        owner_id_hint: str | None,
    ) -> str:
        if credential:
            raise AuthorizationDenied("Kernel bearer identity is not configured in V1")
        if not owner_id_hint:
            raise AuthorizationDenied("trusted local owner hint is required")
        return require_identifier("owner_id", owner_id_hint, 64)
