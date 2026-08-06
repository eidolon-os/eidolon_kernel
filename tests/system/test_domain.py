from __future__ import annotations

import pytest

from eidolon_system.domain.errors import InvalidManifest
from eidolon_system.domain.model import ServiceCatalog, ServiceDefinition, ServiceEndpoint


def service(
    service_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    required: bool = False,
) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=service_id,
        description=f"{service_id} service",
        required=required,
        enabled_by_default=True,
        dependencies=dependencies,
        host_targets={"fake": service_id},
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
