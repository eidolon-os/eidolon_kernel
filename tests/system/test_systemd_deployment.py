from __future__ import annotations

import configparser
from pathlib import Path

import yaml

from eidolon_kernel.config import load_settings as load_kernel_settings
from eidolon_system.unitapplier.protocol import VERBS
from eidolon_system.unitapplier.server import managed_units

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy/systemd"


def _unit(name: str) -> configparser.ConfigParser:
    # systemd permits repeated directives such as EnvironmentFile; the stdlib
    # parser is used only for scalar assertions in these tests.
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with (SYSTEMD / name).open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return parser


def test_only_eidolond_is_enabled_by_host_init() -> None:
    manager = _unit("eidolond.service")
    data = _unit("eidolon-data.service")
    data_workspace = _unit("eidolon-data-workspace.service")
    hub = _unit("eidolon-hub.service")
    kernel = _unit("eidolon-kernel.service")

    assert manager["Install"]["WantedBy"] == "multi-user.target"
    assert "Install" not in data
    assert "Install" not in data_workspace
    assert "Install" not in hub
    assert "Install" not in kernel
    assert data["Service"]["Restart"] == "on-failure"
    assert data_workspace["Service"]["Restart"] == "on-failure"
    assert hub["Service"]["Restart"] == "on-failure"
    assert kernel["Service"]["Restart"] == "on-failure"


def test_system_services_run_unprivileged_with_fixed_release_commands() -> None:
    expected_commands = {
        "eidolon-data.service": (
            "/opt/eidolon/current/eidolon_data/.venv/bin/uvicorn "
            "eidolon_data.api.companion_authority:create_app "
            "--factory --host 127.0.0.1 --port 8084"
        ),
        "eidolon-data-workspace.service": (
            "/opt/eidolon/current/eidolon_data/.venv/bin/uvicorn "
            "eidolon_data.api.workspace_authority:create_app "
            "--factory --host 127.0.0.1 --port 8085"
        ),
        "eidolond.service": "/opt/eidolon/current/eidolon_kernel/.venv/bin/eidolond",
        "eidolon-hub.service": (
            "/opt/eidolon/current/eidolon_hub/.venv/bin/uvicorn "
            "hub.main:app --host 127.0.0.1 --port 8082"
        ),
        "eidolon-kernel.service": (
            "/opt/eidolon/current/eidolon_kernel/.venv/bin/uvicorn "
            "eidolon_kernel.main:create_app --factory --host 127.0.0.1 --port 8083"
        ),
    }
    for name, expected_command in expected_commands.items():
        service = _unit(name)["Service"]
        assert service["User"] == "eidolon"
        assert service["Group"] == "eidolon"
        assert service["NoNewPrivileges"] == "yes"
        assert service["CapabilityBoundingSet"] == ""
        assert service["ExecStart"] == expected_command
        assert "/bin/sh" not in service["ExecStart"]
        assert "sudo" not in service["ExecStart"]


def test_network_observers_allow_linux_interface_discovery() -> None:
    for name in ("eidolon-hub.service", "eidolond.service"):
        service = _unit(name)["Service"]
        assert "AF_NETLINK" in service["RestrictAddressFamilies"].split()


def test_data_unit_uses_dedicated_authority_store_and_secret_file() -> None:
    data = _unit("eidolon-data.service")["Service"]
    workspace = _unit("eidolon-data-workspace.service")["Unit"]
    assert data["WorkingDirectory"] == "/opt/eidolon/current/eidolon_data"
    assert data["ExecStartPre"] == "/opt/eidolon/current/eidolon_data/.venv/bin/alembic -c alembic.ini upgrade head"
    assert "eidolon-data.service" in workspace["After"]
    assert "eidolon-data.service" in workspace["Requires"]
    for unit in ("eidolon-data.service", "eidolon-data-workspace.service"):
        service = _unit(unit)["Service"]
        text = (SYSTEMD / unit).read_text(encoding="utf-8")
        assert "EnvironmentFile=-/etc/eidolon/data.env" in text
        assert "EnvironmentFile=/etc/eidolon/host.env" in text
        assert (
            "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3"
            in service["Environment"]
        )
        assert "EIDOLON_DATA_OBJECT_STORE_PATH=/var/lib/eidolon/objects" in service["Environment"]


def test_livekit_and_memory_runtime_inputs_match_systemd_directories() -> None:
    livekit = _unit("eidolon-livekit.service")["Service"]
    launcher = (SYSTEMD / "eidolon-livekit-launch").read_text(encoding="utf-8")
    assert livekit["RuntimeDirectory"] == "eidolon/livekit"
    assert 'path = "/run/eidolon/livekit/livekit.yaml"' in launcher

    for unit in ("eidolon-memory-supervisor.service", "eidolon-memory-discovery.service"):
        text = (SYSTEMD / unit).read_text(encoding="utf-8")
        assert "EnvironmentFile=/etc/eidolon/memory.env" in text
        assert "Environment=EIDOLON_MEMORY_DOTENV_MODE=environment" in text


def test_livekit_does_not_offer_devices_an_address_off_their_link() -> None:
    """A device dials the Host over the LAN, so link-local is never an answer.

    This Host grows a 169.254 address whenever a maintenance cable is plugged
    in. LiveKit gathers candidates from every interface that is up, so without
    this the address reaches devices that cannot route to it and each of them
    spends part of its connection attempt finding that out.
    """

    launcher = (SYSTEMD / "eidolon-livekit-launch").read_text(encoding="utf-8")
    rtc = launcher.split("rtc:", 1)[1].split("logging:", 1)[0]

    assert "ips:" in rtc
    assert "excludes:" in rtc
    assert "169.254.0.0/16" in rtc
    # Both filters configured means only `includes` applies, which would put
    # every other address behind an allowlist nobody is maintaining.
    assert "includes:" not in rtc


def test_applier_socket_is_enabled_and_its_service_is_activated_by_it() -> None:
    socket_unit = _unit("eidolon-unit-applier.socket")
    service = _unit("eidolon-unit-applier.service")
    manager = _unit("eidolond.service")

    # The socket is what boot brings up; the service is started by the first
    # connection. Enabling the service instead would run a root daemon whether
    # or not the manager exists to talk to it.
    assert socket_unit["Install"]["WantedBy"] == "sockets.target"
    assert "Install" not in service
    assert service["Unit"]["Requires"] == "eidolon-unit-applier.socket"

    # Root by necessity, and reachable only by the manager's group.
    assert service["Service"]["User"] == "root"
    assert socket_unit["Socket"]["SocketUser"] == "root"
    assert socket_unit["Socket"]["SocketGroup"] == "eidolon"
    assert socket_unit["Socket"]["SocketMode"] == "0660"

    # Outside /run/eidolon: seven units declare RuntimeDirectory=eidolon and
    # systemd chowns it to whichever starts first, so a root-owned socket in
    # there would have an owner decided by start order.
    listen = socket_unit["Socket"]["ListenStream"]
    assert listen == "/run/eidolon-unit-applier.sock"
    assert not listen.startswith("/run/eidolon/")

    # The manager must not reconcile before the socket exists, or every start in
    # its first pass fails on a missing path.
    assert "eidolon-unit-applier.socket" in manager["Unit"]["Requires"]
    assert "eidolon-unit-applier.socket" in manager["Unit"]["After"]


def test_applier_allowlist_is_the_manifest_and_nothing_else() -> None:
    document = yaml.safe_load(
        (ROOT / "config/system-services.yaml").read_text(encoding="utf-8")
    )
    # Stated for every Host shape, not just one. The catalogue is conditional
    # now — a board with an NPU has a service a Host without one does not — so
    # the invariant is that the boundary equals the catalogue *this* Host has,
    # for whichever capabilities it declares.
    every_capability = {
        item.get("requires_capability")
        for item in document["services"]
        if item.get("requires_capability")
    }
    for declared in (frozenset(), *(frozenset({name}) for name in sorted(every_capability))):
        managed_targets = {
            item["host_targets"]["systemd"]
            for item in document["services"]
            if item["host_targets"]["systemd"] != "external"
            and (
                item.get("requires_capability") is None
                or item["requires_capability"] in declared
            )
        }

        derived = managed_units(ROOT / "config/system-services.yaml", declared)

        # The service catalog and the privilege boundary are one contract, and
        # the applier keeps them one by deriving from the manifest at start
        # rather than carrying a list. A catalogued target missing from the
        # boundary passes every static release check and then cannot be started;
        # an extra one silently broadens root's authority.
        assert derived == managed_targets, f"declared={sorted(declared)}"
    assert VERBS == {"start", "stop", "restart"}


def test_manager_settings_route_mutations_through_the_applier() -> None:
    settings = yaml.safe_load(
        (ROOT / "config/eidolond.systemd.example.yaml").read_text(encoding="utf-8")
    )
    socket_unit = _unit("eidolon-unit-applier.socket")

    # The path the manager dials and the path root listens on are the same
    # string in two files; nothing at runtime would reconcile them.
    assert (
        settings["host"]["unit_applier_socket"]
        == socket_unit["Socket"]["ListenStream"]
    )


def test_systemd_manifest_targets_units_without_false_hard_dependency() -> None:
    document = yaml.safe_load(
        (ROOT / "config/system-services.yaml").read_text(encoding="utf-8")
    )
    services = {item["service_id"]: item for item in document["services"]}

    assert services["data"]["host_targets"]["systemd"] == "eidolon-data.service"
    assert services["data"]["dependencies"] == []
    assert services["data"]["endpoints"] == [
        {
            "endpoint_id": "companion-authority.http",
            "protocol": "http",
            "address": "http://127.0.0.1:8084",
            "contract": ("https://eidolon.dev/data/contracts/v1/companion/identity.schema.json"),
            "health_url": "http://127.0.0.1:8084/health",
        },
        {
            "endpoint_id": "companion-runtime-authority.http",
            "protocol": "http",
            "address": "http://127.0.0.1:8084",
            "contract": (
                "https://eidolon.dev/data/contracts/v1/companion/"
                "runtime-snapshot.schema.json"
            ),
            "health_url": "http://127.0.0.1:8084/health",
        },
        {
            "endpoint_id": "memory-runtime-roster.http",
            "protocol": "http",
            "address": "http://127.0.0.1:8084",
            "contract": (
                "https://eidolon.dev/data/contracts/v1/memory/"
                "runtime-roster.schema.json"
            ),
            "health_url": "http://127.0.0.1:8084/health",
        },
    ]
    assert services["data-workspace"]["host_targets"]["systemd"] == (
        "eidolon-data-workspace.service"
    )
    assert services["data-workspace"]["dependencies"] == ["data"]
    assert services["data-workspace"]["endpoints"] == [
        {
            "endpoint_id": "workspace-authority.http",
            "protocol": "http",
            "address": "http://127.0.0.1:8085",
            "contract": (
                "https://eidolon.live/contracts/system-data/workspace/"
                "onboarding-operation-v1.schema.json"
            ),
            "health_url": "http://127.0.0.1:8085/health",
        }
    ]
    assert services["hub"]["host_targets"]["systemd"] == "eidolon-hub.service"
    assert services["hub"]["endpoints"][0]["health_url"].endswith("/health")
    assert services["kernel"]["host_targets"]["systemd"] == "eidolon-kernel.service"
    assert services["kernel"]["dependencies"] == []
    assert services["kernel"]["endpoints"][0]["health_url"].endswith("/health")


def test_product_kernel_profile_uses_local_authorities_and_dedicated_store() -> None:
    settings = load_kernel_settings(ROOT / "config/kernel.systemd.example.yaml")

    assert settings.persistence.path == Path("/var/lib/eidolon/eidolon-kernel.sqlite3")
    assert settings.system_directory.uds_path == Path("/run/eidolon/system.sock")
    assert settings.companion_authority.timeout_seconds == 3


def test_supervisord_targets_name_the_macos_source_topology() -> None:
    document = yaml.safe_load((ROOT / "config/system-services.yaml").read_text(encoding="utf-8"))
    services = {item["service_id"]: item for item in document["services"]}

    # Ops owns the group:program names in
    # eidolon_ops/deploy/supervisor/product-source.conf and its own suite
    # checks these against that file; what this repository can say is that
    # every service names one, so a Host running the supervisord driver has
    # the same eleven services as one running systemd.
    assert {
        service_id: item["host_targets"]["supervisord"] for service_id, item in services.items()
    } == {
        "nats": "external",
        "livekit": "livekit:livekit-server",
            "memory-supervisor": "memory:memory-supervisor",
            "memory-embedder": "memory:memory-embedder",
            "memory-discovery": "memory:memory-discovery",
        "data": "data:data-api",
        "data-workspace": "data:data-workspace-api",
        "hub": "hub:hub-api",
        "kernel": "kernel:kernel-api",
        "agent": "agent:agent",
        "channel": "channel:channel-worker",
        "channel-provider": "channel-provider:channel-provider",
        # External to the supervisord driver: the weights are NPU artifacts and
        # a workstation has no NPU to load them with. Catalogued so a Mac source
        # run still gates on it if one is somehow listening, never started here.
        "asr": "external",
        # Same, for the same reason: the local server is pinned to a board's
        # little cores, and a source run reaches a hosted model instead.
        "llm": "external",
        # And the voice, for the same reason.
        "tts": "external",
        "laya": "external",
    }
    assert services["data"]["endpoints"][0]["contract"] == (
        "https://eidolon.dev/data/contracts/v1/companion/identity.schema.json"
    )
    assert services["data"]["endpoints"][1]["contract"] == (
        "https://eidolon.dev/data/contracts/v1/companion/runtime-snapshot.schema.json"
    )
    assert services["data-workspace"]["dependencies"] == ["data"]
    assert services["hub"]["endpoints"][0]["health_url"] == "http://127.0.0.1:8082/health"
    assert services["kernel"]["endpoints"][0]["contract"] == "eidolon.kernel.device-mount.v1"


def test_memory_supervisor_can_be_reloaded_without_stopping_realms() -> None:
    """Re-reading the roster must not cost every Realm a restart.

    The supervisor converges on the authority roster on its own schedule and on
    SIGHUP. Without ExecReload, `systemctl reload` failed outright, so the
    reachable options were `systemctl kill -s HUP` or a full restart — and a
    restart stops every Realm on the Host to pick up one.
    """
    service = _unit("eidolon-memory-supervisor.service")["Service"]
    assert service["ExecReload"] == "/bin/kill -HUP $MAINPID"
    assert service["KillSignal"] == "SIGTERM"


def test_memory_38_uses_a_fresh_storage_epoch() -> None:
    unit = (SYSTEMD / "eidolon-memory-supervisor.service").read_text(encoding="utf-8")

    assert (
        "Environment=EIDOLON_MEMORY_PALACES_ROOT="
        "/var/lib/eidolon/memory/mempalaces-v3.8" in unit
    )
    assert (
        "Environment=EIDOLON_MEMORY_PALACES_ROOT=/var/lib/eidolon/memory/mempalaces\n"
        not in unit
    )
