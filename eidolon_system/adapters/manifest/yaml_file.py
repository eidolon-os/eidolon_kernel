"""Strict YAML/JSON manifest loader backed by the normative JSON Schema."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from eidolon_system.contracts.bindings import ServiceManifestWire
from eidolon_system.contracts.mappers import manifest_service_to_domain
from eidolon_system.contracts.registry import SystemContractRegistry
from eidolon_system.domain.model import ServiceCatalog, select_for_capabilities

logger = logging.getLogger("eidolon.system.manifest")


class YamlServiceManifest:
    """One manifest, read for one Host.

    The manifest is the whole product's service set and ships as committed
    bytes inside a release, which is what makes "the manifest this release was
    tested with is the manifest it runs" true. One of its services is not on
    every Host, and that cannot be expressed by shipping a different file:
    a per-Host file would either leave the release contract, or be generated
    into a release tree whose whole promise is that it holds exact commits.

    So the file stays complete and the per-Host part is an argument. The
    capabilities a Host declares are what Ops writes into the sealed Host
    profile every unit already reads; the composition root passes them here.

    Not read from the environment inside this class on purpose: a loader that
    reaches for global state is one whose result depends on something its
    caller cannot see, and both callers of this class — eidolond and the unit
    applier — have their own settings to read it from.
    """

    def __init__(
        self,
        path: Path,
        *,
        contracts: SystemContractRegistry | None = None,
        capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self.path = path.resolve()
        self.contracts = contracts or SystemContractRegistry()
        self.capabilities = capabilities

    def load(self) -> ServiceCatalog:
        document = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.contracts.validate("system/manifest.schema.json", document)
        wire = ServiceManifestWire.model_validate(document)
        definitions = tuple(manifest_service_to_domain(service) for service in wire.services)
        kept, dropped = select_for_capabilities(definitions, self.capabilities)
        for service_id, needed in dropped:
            # Said, not silent. A service missing from the catalogue is started
            # by nobody and reported missing by nothing, and the readiness check
            # that waits for it then times out with no explanation anywhere —
            # which is exactly how this was found.
            logger.info(
                "service %s is not in this Host's catalogue: it requires %s, and this "
                "Host declares %s",
                service_id,
                needed,
                ", ".join(sorted(self.capabilities)) or "no capabilities",
            )
        return ServiceCatalog(kept)
