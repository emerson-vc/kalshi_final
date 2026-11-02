"""macOS launchd agents (user LaunchAgents, no sudo).

- ``recorder``  KeepAlive: the 24/7 read-only RFQ-feed recorder.
- ``requote``   every 10 min: re-RFQ panel combos that are due (never creates combos, never bets).
- ``fullscan``  every 30 min: optional scheduler tick, OFF by default.

The Mac must be awake for any of them to run. See the README for ``caffeinate`` / ``pmset`` notes.
Agents are installed only when you flip a toggle (dashboard) or run ``combo-edge launchd install …``.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LABEL_PREFIX = "com.combo-edge"
AGENTS: dict[str, dict[str, Any]] = {
    "recorder": {"args": ["recorder"], "keepalive": True},
    "requote": {"args": ["requote-due"], "interval": 600},
    "fullscan": {"args": ["fullscan-check"], "interval": 1800},
}


def label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def plist_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label(name)}.plist"


def _executable() -> str:
    exe = Path(sys.executable).with_name("combo-edge")
    return str(exe) if exe.exists() else shutil.which("combo-edge") or str(exe)


def plist_dict(name: str, project_dir: Path | None = None) -> dict[str, Any]:
    if name not in AGENTS:
        raise ValueError(f"unknown agent {name}")
    spec = AGENTS[name]
    project_dir = project_dir or Path.cwd()
    logs = project_dir / "data" / "logs"
    d: dict[str, Any] = {
        "Label": label(name),
        "ProgramArguments": [_executable(), *spec["args"]],
        "WorkingDirectory": str(project_dir),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + str(Path.home() / ".local" / "bin"), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(logs / f"{name}.out.log"),
        "StandardErrorPath": str(logs / f"{name}.err.log"),
        "ProcessType": "Background",
    }
    if spec.get("keepalive"):
        d["RunAtLoad"] = True
        d["KeepAlive"] = True
        d["ThrottleInterval"] = 30
    else:
        d["StartInterval"] = spec["interval"]
        d["RunAtLoad"] = False
    return d


def render(name: str, project_dir: Path | None = None) -> bytes:
    return plistlib.dumps(plist_dict(name, project_dir))


def _guard() -> None:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("COMBO_EDGE_LAUNCHD_DRYRUN") == "1":
        raise RuntimeError("launchd changes are disabled in tests / dry-run mode")
    if sys.platform != "darwin":
        raise RuntimeError("launchd agents are macOS-only")


def _uid() -> int:
    return os.getuid()


def install(name: str, project_dir: Path | None = None) -> None:
    _guard()
    p = plist_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    (Path(project_dir or Path.cwd()) / "data" / "logs").mkdir(parents=True, exist_ok=True)
    if is_installed(name):
        uninstall(name)
    p.write_bytes(render(name, project_dir))
    subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(p)], check=True, capture_output=True)


def uninstall(name: str) -> None:
    _guard()
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label(name)}"], capture_output=True)
    p = plist_path(name)
    if p.exists():
        p.unlink()


def is_installed(name: str) -> bool:
    if sys.platform != "darwin":
        return False
    r = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{label(name)}"], capture_output=True)
    return r.returncode == 0


def set_agent(name: str, enable: bool) -> None:
    install(name) if enable else uninstall(name)


def status() -> dict[str, bool]:
    return {name: is_installed(name) for name in AGENTS}
