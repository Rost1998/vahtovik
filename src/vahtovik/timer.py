"""The systemd user timer that starts `vahtovik run` at a fixed period."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from .config import Config, ConfigError, parse_interval

UNIT = "vahtovik"
HEADER = "# Written by `vahtovik install-timer`; remove it with `vahtovik uninstall-timer`.\n"


class TimerError(Exception):
    pass


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def on_calendar(every: timedelta) -> str:
    """systemd calendar expression for a period that divides an hour or a day."""
    seconds = int(every.total_seconds())
    minutes, hours = seconds // 60, seconds // 3600
    if seconds % 60 == 0 and 0 < minutes < 60 and 60 % minutes == 0:
        return f"*:0/{minutes}"
    if seconds % 3600 == 0 and 0 < hours <= 24 and 24 % hours == 0:
        return "daily" if hours == 24 else f"0/{hours}:00"
    raise TimerError("the timer period must divide an hour or a day, for example 5m, 15m, 30m, 1h, 6h or 1d")


def _escape(value: str) -> str:
    return value.replace("%", "%%")  # % starts a specifier in unit files


def service_text(cfg: Config, uv: str, path_env: str) -> str:
    return HEADER + f"""[Unit]
Description=vahtovik: one monitoring cycle

[Service]
Type=oneshot
WorkingDirectory={_escape(str(cfg.root))}
Environment="PATH={_escape(path_env)}"
ExecStart="{_escape(uv)}" run --quiet vahtovik run
"""


def timer_text(every: str, calendar: str) -> str:
    return HEADER + f"""[Unit]
Description=vahtovik: start a monitoring cycle every {every}

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _systemctl(*args: str) -> None:
    if not shutil.which("systemctl"):
        raise TimerError("systemctl not found; use cron with bin/vahtovik-cron instead (see README)")
    proc = _command("systemctl", "--user", *args)
    if proc.returncode != 0:
        raise TimerError(f"systemctl --user {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")


def _lingers() -> bool:
    if not shutil.which("loginctl"):
        return True  # nothing to advise
    proc = _command("loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value")
    return proc.stdout.strip() == "yes"


def install(cfg: Config, every: str) -> list[str]:
    """Write the units and start the timer. Returns lines to show the person."""
    try:
        calendar = on_calendar(parse_interval(every))
    except ConfigError as exc:
        raise TimerError(str(exc)) from None
    uv = shutil.which("uv")
    if not uv:
        raise TimerError("uv not found on PATH")
    folder = unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    existed = (folder / f"{UNIT}.timer").exists()
    (folder / f"{UNIT}.service").write_text(service_text(cfg, uv, os.environ.get("PATH", "")))
    (folder / f"{UNIT}.timer").write_text(timer_text(every, calendar))
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{UNIT}.timer")
    lines = [
        f"{'Updated' if existed else 'Installed'} {UNIT}.timer: a cycle every {every}, for {cfg.root}.",
        f"Next run: systemctl --user list-timers {UNIT}.timer",
        f"Crash output: journalctl --user -u {UNIT}",
    ]
    if not _lingers():
        lines.append("The timer runs only while you are logged in. "
                     "To run it while you are logged out too: loginctl enable-linger $USER")
    return lines


def uninstall() -> list[str]:
    folder = unit_dir()
    files = [folder / f"{UNIT}.timer", folder / f"{UNIT}.service"]
    if not any(f.exists() for f in files):
        return ["No vahtovik timer is installed."]
    _systemctl("disable", "--now", f"{UNIT}.timer")
    for f in files:
        f.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return [f"Removed {UNIT}.timer."]
