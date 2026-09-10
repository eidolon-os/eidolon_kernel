import os
import runpy
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("override", ["", "203.0.113.4"])
def test_linux_launcher_uses_native_ice_unless_operator_overrides(monkeypatch, tmp_path, override):
    output = tmp_path / "livekit.yaml"
    opened = os.open
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret")
    monkeypatch.setenv("EIDOLON_LIVEKIT_NODE_IP", override)
    monkeypatch.setattr(os, "open", lambda path, flags, mode: opened(output, flags, mode))
    calls = []
    monkeypatch.setattr(os, "execv", lambda *args: calls.append(args))
    runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "deploy/systemd/eidolon-livekit-launch")
    )
    config = yaml.safe_load(output.read_text())
    assert config["rtc"].get("node_ip") == (override or None)
    assert config["bind_addresses"] == ["0.0.0.0"]
    assert calls[0][0] == "/usr/local/bin/livekit-server"
