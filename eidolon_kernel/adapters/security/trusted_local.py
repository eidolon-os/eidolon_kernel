"""Explicit trust adapter for a loopback-only first deployment."""

from eidolon_kernel.domain.errors import AuthorizationDenied
from eidolon_kernel.domain.model import Actor


class TrustedLocalActorAuthorizer:
    """Trust identity hints supplied by the same-host ingress.

    This is deliberately not presented as authentication. Composition only enables it
    under the ADR-0003 loopback/trusted-process deployment assumption.
    """

    async def authorize(
        self,
        *,
        action: str,
        owner_id: str,
        credential: str | None,
        actor_id_hint: str | None,
        actor_owner_hint: str | None,
    ) -> Actor:
        if credential:
            raise AuthorizationDenied("Kernel bearer identity is not configured in V1")
        if not actor_id_hint or not actor_owner_hint:
            raise AuthorizationDenied("trusted local actor and owner hints are required")
        if actor_owner_hint.strip() != owner_id.strip():
            raise AuthorizationDenied("trusted local actor owner scope mismatch")
        return Actor(
            actor_id=actor_id_hint,
            owner_id=actor_owner_hint,
            source="trusted-local-ingress",
        )
