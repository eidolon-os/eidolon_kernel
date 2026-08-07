from __future__ import annotations

import configparser
from pathlib import Path

import yaml

from eidolon_kernel.config import load_settings as load_kernel_settings

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy/systemd"
POLKIT = ROOT / "deploy/polkit/60-eidolon-system-manager.rules"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with (SYSTEMD / name).open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return parser


def test_only_eidolond_is_enabled_by_host_init() -> None:
    manager = _unit("eidolond.service")
    data = _unit("eidolon-data.service")
    hub = _unit("eidolon-hub.service")
    kernel = _unit("eidolon-kernel.service")

    assert manager["Install"]["WantedBy"] == "multi-user.target"
    assert "Install" not in data
    assert "Install" not in hub
    assert "Install" not in kernel
    assert data["Service"]["Restart"] == "on-failure"
    assert hub["Service"]["Restart"] == "on-failure"
    assert kernel["Service"]["Restart"] == "on-failure"


def test_system_services_run_unprivileged_with_fixed_release_commands() -> None:
    expected_commands = {
        "eidolon-data.service": (
            "/srv/eidolon/current/eidolon_data/.venv/bin/uvicorn "
            "eidolon_data.api.companion_authority:create_app "
            "--factory --host 127.0.0.1 --port 8084"
        ),
        "eidolond.service": "/srv/eidolon/current/eidolon_kernel/.venv/bin/eidolond",
        "eidolon-hub.service": (
            "/srv/eidolon/current/eidolon_hub/.venv/bin/uvicorn "
            "hub.main:app --host 127.0.0.1 --port 8082"
        ),
        "eidolon-kernel.service": (
            "/srv/eidolon/current/eidolon_kernel/.venv/bin/uvicorn "
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


def test_hub_hardening_allows_linux_interface_discovery() -> None:
    service = _unit("eidolon-hub.service")["Service"]

    assert "AF_NETLINK" in service["RestrictAddressFamilies"].split()


def test_data_unit_uses_dedicated_authority_store_and_secret_file() -> None:
    service = _unit("eidolon-data.service")["Service"]

    assert service["EnvironmentFile"] == "-/etc/eidolon/data.env"
    assert (
        "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3" in service["Environment"]
    )
    assert "EIDOLON_DATA_OBJECT_STORE_PATH=/var/lib/eidolon/objects" in service["Environment"]


def test_polkit_rule_is_bound_to_manager_unit_targets_and_verbs() -> None:
    policy = POLKIT.read_text(encoding="utf-8")

    assert 'action.id !== "org.freedesktop.systemd1.manage-units"' in policy
    assert 'subject.user !== "eidolon"' in policy
    assert 'subject.system_unit !== "eidolond.service"' in policy
    assert "!subject.no_new_privileges" in policy
    assert '"eidolon-hub.service"' in policy
    assert '"eidolon-data.service"' in policy
    assert '"eidolon-kernel.service"' in policy
    assert 'var allowedVerbs = ["start", "stop", "restart"]' in policy
    assert "manage-unit-files" not in policy
    assert "daemon-reload" not in policy
    assert "polkit.Result.NO" in policy


def test_systemd_manifest_targets_units_without_false_hard_dependency() -> None:
    document = yaml.safe_load(
        (ROOT / "config/system-services.systemd.example.yaml").read_text(encoding="utf-8")
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


def test_dev_manifest_publishes_all_control_plane_authorities() -> None:
    document = yaml.safe_load((ROOT / "config/system-services.yaml").read_text(encoding="utf-8"))
    services = {item["service_id"]: item for item in document["services"]}

    assert services["data"]["host_targets"]["supervisord"] == "data:data-api"
    assert services["data"]["endpoints"][0]["contract"] == (
        "https://eidolon.dev/data/contracts/v1/companion/identity.schema.json"
    )
    assert services["hub"]["host_targets"]["supervisord"] == "hub:hub-api"
    assert services["hub"]["endpoints"][0]["health_url"] == "http://127.0.0.1:8082/health"
    assert services["kernel"]["host_targets"]["supervisord"] == "kernel:kernel-api"
    assert services["kernel"]["endpoints"][0]["contract"] == (
        "eidolon.kernel.device-mount.v1"
    )
