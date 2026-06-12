"""Tests for platform-specific session script helpers."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from bot import platform_paths


def test_default_market_session_script_by_platform(monkeypatch):
    monkeypatch.setattr(platform_paths, "is_windows", lambda: True)
    assert platform_paths.default_market_session_script() == Path(
        "scripts/run_market_session.ps1"
    )

    monkeypatch.setattr(platform_paths, "is_windows", lambda: False)
    assert platform_paths.default_market_session_script() == Path(
        "scripts/run_market_session.sh"
    )


def test_session_start_command():
    ps1 = Path("C:/proj/scripts/run_market_session.ps1")
    assert platform_paths.session_start_command(ps1) == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
    ]
    sh = Path("/proj/scripts/run_market_session.sh")
    assert platform_paths.session_start_command(sh) == [str(sh)]


def test_process_exists(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    assert platform_paths.process_exists(os.getpid())

    def _missing(_pid: int, _sig: int) -> None:
        raise OSError("gone")

    monkeypatch.setattr(os, "kill", _missing)
    assert not platform_paths.process_exists(4242)


def test_terminate_process_windows(monkeypatch):
    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(platform_paths, "is_windows", lambda: True)
    monkeypatch.setattr(platform_paths.subprocess, "run", _run)
    platform_paths.terminate_process(1234)
    assert calls == [["taskkill", "/PID", "1234", "/T", "/F"]]


def test_terminate_process_unix(monkeypatch):
    killed: list[tuple[int, int]] = []

    monkeypatch.setattr(platform_paths, "is_windows", lambda: False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    platform_paths.terminate_process(5678)
    assert killed == [(5678, signal.SIGTERM)]


def test_popen_session_kwargs_windows(monkeypatch):
    monkeypatch.setattr(platform_paths, "is_windows", lambda: True)
    kwargs = platform_paths.popen_session_kwargs()
    assert kwargs == {"creationflags": platform_paths._create_new_process_group_flag()}


def test_popen_session_kwargs_unix(monkeypatch):
    monkeypatch.setattr(platform_paths, "is_windows", lambda: False)
    assert platform_paths.popen_session_kwargs() == {"start_new_session": True}
