"""What the machine says about itself, read from the sealed Host profile."""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Where Ops states what this machine can do. It is written into
#: `/etc/eidolon/host.env`, the sealed Host profile that every Eidolon unit
#: already reads through `EnvironmentFile=` — which is why nothing new has to be
#: plumbed to either process that needs it.
HOST_CAPABILITIES_VARIABLE = "EIDOLON_HOST_CAPABILITIES"


def declared_host_capabilities(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """What this Host says it can do.

    An adapter, and in this layer for a reason: both processes that need it may
    import `adapters`, and the root unit applier may not import the manager's
    config — the privilege boundary runs between them, and pulling eidolond's
    settings module into the privileged process to read one string would cross
    it for nothing.

    Read at a composition boundary and passed down, never reached for inside the
    manifest loader: a loader whose result depends on ambient state is one whose
    result its caller cannot see.

    Unset means a Host that declares nothing, which is what every Host was
    before any of them could differ — so an older Host keeps running against a
    newer eidolond. The names are not checked against a closed set here on
    purpose: Ops validates that on both its own sides already, this package
    would be the sixth copy of it, and the only thing an unrecognised name can
    do is fail to match a `requires_capability` — which the manifest loader says
    out loud when it drops a service, naming both what was needed and what this
    Host declared.
    """

    source = os.environ if environ is None else environ
    raw = source.get(HOST_CAPABILITIES_VARIABLE, "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())
