from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = _ROOT / "deploy/raspberry-pi/eidolon-pi-release.sh"


def test_pi_release_driver_is_dry_run_by_default_and_requires_explicit_commits() -> None:
    source = _DRIVER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "activate=false" in source
    assert "resume=false" in source
    assert 'if [[ "${activate}" == true ]]' in source
    assert 'if [[ "${resume}" == false ]]' in source
    assert "--kernel-revision" in source
    assert "--data-revision" in source
    assert "--hub-revision" in source
    assert "--admin-revision" in source
    assert "--sdk-revision" in source
    assert "BatchMode=yes" in source
    assert "eval " not in source
    assert "eidolon-system.sqlite3" not in source


def test_pi_release_driver_help_is_local_and_non_mutating() -> None:
    result = subprocess.run(
        ["bash", str(_DRIVER), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert "Without --activate" in result.stdout
    assert "Host identity" in result.stdout
