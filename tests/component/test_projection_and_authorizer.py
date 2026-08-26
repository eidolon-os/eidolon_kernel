import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.adapters.security.trusted_local import TrustedLocalOwnerAuthorizer
from eidolon_kernel.domain.errors import AuthorityUnavailable, AuthorizationDenied
from tests.support import OfflineCompanionAuthority, sample_mount

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")


@pytest.mark.asyncio
async def test_offline_companion_authority_fake_fails_closed() -> None:
    with pytest.raises(AuthorityUnavailable, match="offline"):
        await OfflineCompanionAuthority().get_companion(companion_id="companion")


@pytest.mark.asyncio
async def test_trusted_local_authorizer_requires_matching_explicit_hints() -> None:
    authorizer = TrustedLocalOwnerAuthorizer()
    owner_id = await authorizer.authorize(
        action="read",
        credential=None,
        owner_id_hint="owner-1",
    )
    assert owner_id == "owner-1"
    with pytest.raises(AuthorizationDenied):
        await authorizer.authorize(
            action="write",
            credential="Bearer not-supported",
            owner_id_hint="owner-1",
        )
    with pytest.raises(AuthorizationDenied):
        await authorizer.authorize(
            action="write",
            credential=None,
            owner_id_hint=None,
        )


def test_projection_ignores_stale_updates_and_filters_scopes() -> None:
    projection = InMemoryMountProjection()
    current = sample_mount(2, request_id="r2", active=False)
    projection.rebuild((current,))
    projection.put(sample_mount(1))
    assert projection.get(_DEVICE_1) == current
    assert projection.list(
        owner_id="owner-1",
        companion_id=None,
        active_only=True,
        after_device_id=None,
        limit=10,
    ) == ()
