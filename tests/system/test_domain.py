from __future__ import annotations

import pytest

from eidolon_system.domain.errors import InvalidManifest
from eidolon_system.domain.model import (
    EXTERNAL_HOST_TARGET,
    ServiceCatalog,
    ServiceDefinition,
    ServiceEndpoint,
)


def service(
    service_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    required: bool = False,
    host_targets: dict[str, str] | None = None,
    requires_capability: str | None = None,
) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=service_id,
        description=f"{service_id} service",
        required=required,
        enabled_by_default=True,
        dependencies=dependencies,
        requires_capability=requires_capability,
        host_targets=host_targets or {"fake": service_id},
        endpoints=(
            ServiceEndpoint(
                endpoint_id="control.http",
                protocol="http",
                address=f"http://127.0.0.1/{service_id}",
                contract=f"eidolon.{service_id}.v1",
                health_url=f"http://127.0.0.1/{service_id}/health",
            ),
        ),
    )


def test_catalog_orders_dependencies_and_rejects_cycles() -> None:
    catalog = ServiceCatalog((service("agent", dependencies=("kernel",)), service("kernel")))
    assert [item.service_id for item in catalog.start_order] == ["kernel", "agent"]
    assert [item.service_id for item in catalog.stop_order] == ["agent", "kernel"]
    assert catalog.dependents_of("kernel") == ("agent",)

    with pytest.raises(InvalidManifest, match="cycle"):
        ServiceCatalog((service("a", dependencies=("b",)), service("b", dependencies=("a",))))


def test_catalog_rejects_unknown_dependencies_duplicate_ids_and_bad_identifiers() -> None:
    with pytest.raises(InvalidManifest, match="unknown dependency"):
        ServiceCatalog((service("agent", dependencies=("missing",)),))
    with pytest.raises(InvalidManifest, match="duplicate service_id"):
        ServiceCatalog((service("kernel"), service("kernel")))
    with pytest.raises(InvalidManifest, match="service_id"):
        service("Not Valid")
    with pytest.raises(InvalidManifest, match="service_id"):
        service("a" * 129)
    with pytest.raises(InvalidManifest, match="protocol"):
        ServiceEndpoint(
            endpoint_id="control.http",
            protocol="p" * 33,
            address="http://127.0.0.1",
            contract="eidolon.test.v1",
        )


def test_external_target_is_a_declared_service_this_driver_cannot_act_on() -> None:
    definition = service(
        "nats",
        host_targets={"fake": EXTERNAL_HOST_TARGET, "systemd": "eidolon-nats.service"},
    )

    # It is in the catalog and it is orderable, unlike a service left out of a
    # driver's manifest entirely — which is what the two-file arrangement did.
    assert ServiceCatalog((definition,)).get("nats") is definition
    assert definition.manages("systemd") is True
    assert definition.manages("fake") is False
    assert definition.target_for("systemd") == "eidolon-nats.service"
    with pytest.raises(InvalidManifest, match="external to fake"):
        definition.target_for("fake")
    with pytest.raises(InvalidManifest, match="no host target"):
        definition.target_for("upstart")


def test_a_service_only_some_hosts_have_is_absent_rather_than_disabled() -> None:
    """Absent, because listed-but-disabled is not the same fact.

    A listed service is still health-probed, still gates its dependents, and is
    still a unit the root applier may actuate. A Host without the capability has
    none of that to do: it has no such unit installed at all.
    """

    from eidolon_system.domain.model import select_for_capabilities

    every_host = service("hub")
    npu_only = service("asr", requires_capability="local_asr")

    kept, dropped = select_for_capabilities((every_host, npu_only), frozenset())
    assert [item.service_id for item in kept] == ["hub"]
    assert dropped == (("asr", "local_asr"),)

    kept, dropped = select_for_capabilities(
        (every_host, npu_only), frozenset({"local_asr"})
    )
    assert [item.service_id for item in kept] == ["hub", "asr"]
    assert dropped == ()


def test_what_was_left_out_is_returned_rather_than_swallowed() -> None:
    """Because this is exactly how the defect hid.

    Nothing started the service, nothing reported it missing, and the readiness
    check that waited for it timed out with no explanation anywhere. The
    selector hands back what it dropped and why so the caller can say it.
    """

    from eidolon_system.domain.model import select_for_capabilities

    _kept, dropped = select_for_capabilities(
        (service("asr", requires_capability="local_asr"),), frozenset({"rknpu2"})
    )

    assert dropped == (("asr", "local_asr"),)


def test_a_manifest_nobody_could_satisfy_is_refused_on_every_host() -> None:
    """A service every Host runs must not depend on one only some Hosts have.

    Filtering the conditional one away would leave a dependency naming nothing,
    and the Host that got there would be told it has an unknown dependency
    rather than that the manifest asks for something impossible. Refused over
    the whole manifest, because it is equally wrong on the Hosts where the
    missing piece happens to be present.
    """

    from eidolon_system.domain.model import select_for_capabilities

    definitions = (
        service("channel", dependencies=("asr",)),
        service("asr", requires_capability="local_asr"),
    )

    with pytest.raises(InvalidManifest, match="only some Hosts have"):
        select_for_capabilities(definitions, frozenset({"local_asr"}))
    with pytest.raises(InvalidManifest, match="only some Hosts have"):
        select_for_capabilities(definitions, frozenset())


def test_a_conditional_service_may_depend_on_one_every_host_runs() -> None:
    """The direction that is fine, so the rule is not read as "no dependencies"."""

    from eidolon_system.domain.model import select_for_capabilities

    definitions = (
        service("hub"),
        service("asr", requires_capability="local_asr", dependencies=("hub",)),
    )

    kept, _dropped = select_for_capabilities(definitions, frozenset({"local_asr"}))
    assert [item.service_id for item in kept] == ["hub", "asr"]
