import pytest

from eidolon_kernel.adapters.companion.unavailable import UnavailableCompanionAuthority
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalActorAuthorizer
from eidolon_kernel.domain.errors import AuthorityUnavailable, AuthorizationDenied
from tests.support import sample_mount


@pytest.mark.asyncio
async def test_companion_adapter_fails_closed_until_contract_exists() -> None:
    with pytest.raises(AuthorityUnavailable, match="ADR-0002"):
        await UnavailableCompanionAuthority().get_companion(companion_id="companion")


@pytest.mark.asyncio
async def test_trusted_local_authorizer_requires_matching_explicit_hints() -> None:
    authorizer = TrustedLocalActorAuthorizer()
    actor = await authorizer.authorize(
        action="read",
        owner_id="owner-1",
        credential=None,
        actor_id_hint="actor-1",
        actor_owner_hint="owner-1",
    )
    assert actor.source == "trusted-local-ingress"
    with pytest.raises(AuthorizationDenied):
        await authorizer.authorize(
            action="write",
            owner_id="owner-1",
            credential="Bearer not-supported",
            actor_id_hint="actor-1",
            actor_owner_hint="owner-1",
        )
    with pytest.raises(AuthorizationDenied):
        await authorizer.authorize(
            action="write",
            owner_id="owner-1",
            credential=None,
            actor_id_hint=None,
            actor_owner_hint="owner-1",
        )


def test_projection_ignores_stale_updates_and_filters_scopes() -> None:
    projection = InMemoryMountProjection()
    current = sample_mount(2, request_id="r2", active=False)
    projection.rebuild((current,))
    projection.put(sample_mount(1))
    assert projection.get("device-1") == current
    assert projection.list(
        owner_id="owner-1",
        companion_id=None,
        active_only=True,
        after_device_id=None,
        limit=10,
    ) == ()
