"""Group sessions run as interactive Claude Code in tmux windows, so a person can watch and join them.

Each window runs `vahtovik window <spec file>`, which holds the group lock while `claude` runs in it.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import groups, journal
from .claude_session import Claude, SessionSpec
from .config import Config
from .state import State
from .tmux import Tmux
from .util import LockBusy

log = logging.getLogger(__name__)

POLL_SECONDS = 0.5
BUSY_EXIT = 75
TERMINAL_VARS = ("TERM", "COLORTERM", "TMUX", "TMUX_PANE")  # from the window, not from whoever started it


def launch(cfg: Config, state: State, tmux: Tmux, claude: Claude, spec: SessionSpec, purpose: str,
           turn_id: str | None = None) -> str:
    """Start the group session in a new window. Returns the window id."""
    watch, key = groups.parse_ref(spec.scope)
    folder = cfg.data_dir / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"window-{uuid.uuid4().hex}.json"
    data = {"ref": spec.scope, "who": journal.who_from_env(spec.env), "cwd": str(spec.workdir),
            "args": claude.interactive_args(spec), "env": claude.env(spec)}
    # The environment holds the watch's secrets: only this user may read the file, and the window deletes it.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(data, handle)
    state.window_started(watch, key, purpose, turn_id)
    return tmux.new_window(spec.scope, [sys.executable, "-m", "vahtovik", "window", str(path)], spec.workdir)


def read_spec(path: Path) -> dict:
    data = json.loads(path.read_text())
    path.unlink()
    return data


def run_window(cfg: Config, state: State, tmux: Tmux, data: dict) -> int:
    """The program of a session window: hold the group lock while claude runs, then record how it ended."""
    watch, key = groups.parse_ref(data["ref"])
    env = {**data["env"], **{k: os.environ[k] for k in TERMINAL_VARS if k in os.environ}}
    # Ctrl-C belongs to claude. A handler, unlike SIG_IGN, is not passed on to claude.
    signal.signal(signal.SIGINT, lambda *_: None)
    try:
        with groups.lock(cfg.group_dir(watch, key)):
            code = subprocess.run(data["args"], cwd=data["cwd"], env=env).returncode
            screen = tmux.capture(os.environ["TMUX_PANE"]) if os.environ.get("TMUX_PANE") else ""
    except LockBusy:
        code, screen = BUSY_EXIT, "another process is using the group"
    state.window_ended(watch, key, code, screen)
    return code


def exit_error(state: State, watch: str, key: str) -> str:
    """Why a session window closed before the agent reported its status."""
    record = state.window_record(watch, key)
    if not record or record["exit_code"] is None:
        return "the session window closed before the agent ran `vahtovik turn`"
    lines = [line.strip() for line in (record["exit_screen"] or "").splitlines() if line.strip()]
    if record["exit_code"] == BUSY_EXIT:
        return "another process was using the group"
    if record["exit_code"] == 0:
        return "claude exited before the agent ran `vahtovik turn`"
    return f"claude exited with code {record['exit_code']}" + (f": {' / '.join(lines[-3:])}" if lines else "")


def wait_for_turn(state: State, tmux: Tmux, watch: str, key: str, turn_id: str,
                  tick: Callable[[], None] | None = None) -> str:
    """Wait until the turn ends: "done", "failed", "maintenance", "background" (the agent waits for its own
    background work), or "ended" (the window closed)."""
    ref = f"{watch}/{key}"
    while True:
        group, turn = state.group(watch, key), state.turn(turn_id)
        if turn["finished_at"] and (group["idle"] or not turn["ok"]):
            return "done" if turn["ok"] else "failed"
        if group["status"] in ("failed", "maintenance") or (group["status"] == "background" and group["idle"]):
            return group["status"]
        if tmux.window(ref) is None:
            return "ended"
        if tick:
            tick()
        time.sleep(POLL_SECONDS)


def age(state: State, watch: str, key: str) -> timedelta:
    record = state.window_record(watch, key)
    if not record or not record["started_at"]:
        return timedelta.max
    return datetime.now(timezone.utc) - datetime.fromisoformat(record["started_at"])
