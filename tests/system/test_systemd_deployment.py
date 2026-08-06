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
    hub = _unit("eidolon-hub.service")
    kernel = _unit("eidolon-kernel.service")

    assert manager["Install"]["WantedBy"] == "multi-user.target"
    assert "Install" not in hub
    assert "Install" not in kernel
    assert hub["Service"]["Restart"] == "on-failure"
    assert kernel["Service"]["Restart"] == "on-failure"


def test_system_services_run_unprivileged_with_fixed_release_commands() -> None:
    expected_commands = {
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


def test_polkit_rule_is_bound_to_manager_unit_targets_and_verbs() -> None:
    policy = POLKIT.read_text(encoding="utf-8")

    assert 'action.id !== "org.freedesktop.systemd1.manage-units"' in policy
    assert 'subject.user !== "eidolon"' in policy
    assert 'subject.system_unit !== "eidolond.service"' in policy
    assert "!subject.no_new_privileges" in policy
    assert '"eidolon-hub.service"' in policy
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

    assert services["hub"]["host_targets"]["systemd"] == "eidolon-hub.service"
    assert services["hub"]["endpoints"][0]["health_url"].endswith("/health")
    assert services["kernel"]["host_targets"]["systemd"] == "eidolon-kernel.service"
    assert services["kernel"]["dependencies"] == []
    assert services["kernel"]["endpoints"][0]["health_url"].endswith("/health")


def test_product_kernel_profile_uses_local_authorities_and_dedicated_store() -> None:
    settings = load_kernel_settings(ROOT / "config/kernel.systemd.example.yaml")

    assert settings.persistence.path == Path("/var/lib/eidolon/eidolon-kernel.sqlite3")
    assert settings.system_directory.uds_path == Path("/run/eidolon/system.sock")
    assert settings.companion_authority.base_url == "http://127.0.0.1:8084"


def test_dev_hub_readiness_uses_the_producer_health_route() -> None:
    document = yaml.safe_load((ROOT / "config/system-services.yaml").read_text(encoding="utf-8"))

    assert document["services"][0]["endpoints"][0]["health_url"] == ("http://127.0.0.1:8082/health")
