"""Use LiveKit's native second-signal shutdown for occupied rooms."""
import os
import runpy
import select
import signal
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / 'deploy/systemd/eidolon-livekit-launch'


@pytest.mark.parametrize('ready,expected_signals,code', [
    ([True], 1, 0), ([False, True], 2, 0),
    ([False, False], 2, 'LiveKit did not finish its bounded native shutdown'),
])
def test_bounded_native_stop(monkeypatch, ready, expected_signals, code):
    waits = iter(ready)
    signals, closed, deadlines = [], [], []
    monkeypatch.setattr(sys, 'argv', [str(LAUNCHER), '--stop', '42'])
    monkeypatch.delenv('LIVEKIT_API_KEY', raising=False)
    monkeypatch.delenv('LIVEKIT_API_SECRET', raising=False)
    monkeypatch.setattr(os, 'pidfd_open', lambda pid: 17 if pid == 42 else None, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda fd, sig: signals.append((fd, sig)), raising=False)
    monkeypatch.setattr(os, 'close', closed.append)
    def poll(read, write, error, timeout):
        deadlines.append(timeout)
        return (read if next(waits) else [], [], [])
    monkeypatch.setattr(select, 'select', poll)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(LAUNCHER))
    assert exc.value.code == code
    assert signals == [(17, signal.SIGTERM)] * expected_signals
    assert closed == [17]
    assert deadlines == [3.0, 6.0][:expected_signals]


def test_already_exited_stop(monkeypatch):
    monkeypatch.setattr(sys, 'argv', [str(LAUNCHER), '--stop', '42'])
    def gone(pid):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'pidfd_open', gone, raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(LAUNCHER))
    assert exc.value.code == 0


def test_unit_uses_bounded_stop():
    assert 'ExecStop=/usr/bin/python3 /usr/local/libexec/eidolon-livekit-launch --stop $MAINPID' in LAUNCHER.with_name('eidolon-livekit.service').read_text()
