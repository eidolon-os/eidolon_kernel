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
) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=service_id,
        description=f"{service_id} service",
        required=required,
        enabled_by_default=True,
        dependencies=dependencies,
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
