"""Kernel's operations contract against Kernel's own configuration.

This repository is in an unusual position: it holds the systemd unit files for
five components that are not Kernel's, so a change here can break a component
whose tests never run. The last test walks all of them and checks each unit
runs out of the venv of the component that owns it — which is the failure mode
those files actually have.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

_REPOSITORY = Path(__file__).resolve().parents[1]
_CONTRACT = _REPOSITORY / "ops/component.toml"
_UNITS = _REPOSITORY / "deploy/systemd"


@pytest.fixture(scope="module")
def contract() -> dict:
    return tomllib.loads(_CONTRACT.read_text(encoding="utf-8"))


def _settings(name: str) -> dict:
    return yaml.safe_load((_REPOSITORY / "config" / name).read_text(encoding="utf-8"))


def _authority(contract: dict) -> set[str]:
    return {entry["path"] for entry in contract["state"]["authority"]}


def test_the_declared_databases_are_the_ones_the_product_profile_opens(
    contract: dict,
) -> None:
    declared = _authority(contract)

    # These two YAML files are what a Host is started with, so they are the
    # statement of record about where each database lives.
    assert _settings("eidolond.systemd.example.yaml")["persistence"]["path"] in declared
    assert _settings("kernel.systemd.example.yaml")["persistence"]["path"] in declared


def test_the_declared_eidolond_port_is_the_one_it_binds(contract: dict) -> None:
    interface = _settings("eidolond.systemd.example.yaml")["interface"]

    assert contract["ports"]["eidolond"]["default"] == interface["port"]
    # It also answers on a socket. Loopback is the right bind either way — what
    # reaches eidolond from a network reaches it through something else.
    assert contract["ports"]["eidolond"]["bind"] == "loopback"
    assert interface["host"] == "127.0.0.1"


def test_the_socket_eidolond_answers_on_is_under_declared_runtime(
    contract: dict,
) -> None:
    socket = Path(_settings("eidolond.systemd.example.yaml")["interface"]["uds"])
    runtime = [Path(item) for item in contract["state"]["runtime"]]

    # A reset that missed it would leave a stale socket that the next start
    # either fails on or, worse, connects to.
    assert any(socket.is_relative_to(root) for root in runtime)


def test_a_factory_reset_removes_everything_kernel_holds(contract: dict) -> None:
    removed = [Path(item) for item in contract["reset"]["factory"]]

    for path in _authority(contract):
        assert any(Path(path).is_relative_to(root) for root in removed), (
            f"{path} would survive a factory reset"
        )


def test_kernels_own_units_run_out_of_kernels_own_venv(contract: dict) -> None:
    for unit in contract["units"]:
        source = _UNITS / f"{unit['id']}.service"
        assert source.is_file(), f"{source.name} is declared but not shipped"

        exec_start = _exec_start(source)
        assert f"/eidolon_kernel/{unit['exec']} " in f"{exec_start} "
        assert f"User={unit['user']}" in source.read_text(encoding="utf-8")


def test_the_units_this_repository_hosts_for_others_point_at_their_own_venvs() -> None:
    """The check that only this repository is in a position to make.

    Hub, Data, Memory, Agent and Channel do not hold their own .service files.
    A unit that runs the right entrypoint out of the wrong component's venv
    starts, fails to import, and restarts forever — and nothing in the owning
    repository would have noticed.
    """

    others = {
        "eidolon-hub": "eidolon_hub",
        "eidolon-data": "eidolon_data",
        "eidolon-data-workspace": "eidolon_data",
        "eidolon-memory-supervisor": "eidolon_memory",
        "eidolon-memory-discovery": "eidolon_memory",
        "eidolon-agent": "eidolon_agent",
        "eidolon-channel": "eidolon_channel",
        "eidolon-channel-provider": "eidolon_channel",
    }

    for unit_id, component in others.items():
        source = _UNITS / f"{unit_id}.service"
        assert source.is_file(), f"{source.name} is missing from this repository"
        assert f"/opt/eidolon/current/{component}/.venv/bin/" in _exec_start(source), (
            f"{unit_id} does not run out of {component}'s venv"
        )


def _exec_start(source: Path) -> str:
    return next(
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    )
