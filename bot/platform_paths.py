"""Platform-specific paths and process helpers for session wrappers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def default_market_session_script() -> Path:
    if is_windows():
        return Path("scripts/run_market_session.ps1")
    return Path("scripts/run_market_session.sh")


def session_start_command(script: Path) -> list[str]:
    """Build argv to launch the market session wrapper script."""
    if script.suffix.lower() == ".ps1":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    return [str(script)]


def _create_new_process_group_flag() -> int:
    # subprocess.CREATE_NEW_PROCESS_GROUP exists only on Windows builds.
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))


def popen_session_kwargs() -> dict[str, object]:
    """Extra subprocess.Popen kwargs for detached session wrappers."""
    if is_windows():
        return {"creationflags": _create_new_process_group_flag()}
    return {"start_new_session": True}


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_process(pid: int) -> None:
    """Stop a session wrapper and its child processes."""
    if is_windows():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    os.kill(pid, signal.SIGTERM)
