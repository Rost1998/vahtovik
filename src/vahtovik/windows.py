"""Group sessions run as interactive Claude Code in tmux windows, so a person can watch and join them.

Each window runs `vahtovik window <spec file>`, which holds the group lock while `claude` runs in it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import groups, journal
from .claude_session import Claude, SessionSpec
from .config import Config
from .state import State
from .tmux import Tmux
from .util import LockBusy

log = logging.getLogger(__name__)

BUSY_EXIT = 75
LAUNCH_EXIT = 127  # claude could not start, as a shell says for a command it cannot run
# The prompt goes to claude as one argument, and Linux takes at most 128 KiB in one (MAX_ARG_STRLEN): exec fails
# with "Argument list too long" above that. A longer prompt goes through a file.
INLINE_PROMPT_LIMIT = 100 * 1024  # bytes
PROMPT_FILE_KEEP = timedelta(days=2)  # a window that was killed leaves its prompt file
PROMPT_FILE_NOTE = (
    "Your prompt is {size} KiB, too long to pass on the command line, so vahtovik wrote it to the file {path} "
    "({lines} lines). Read the whole file now, in parts with the Read tool's offset and limit, then follow it as "
    "your prompt."
)
TERMINAL_VARS = ("TERM", "COLORTERM", "TMUX", "TMUX_PANE")  # from the window, not from whoever started it


def launch(cfg: Config, state: State, tmux: Tmux, claude: Claude, spec: SessionSpec, purpose: str,
           turn_id: str | None = None) -> str:
    """Start the group session in a new window. Returns the window id."""
    watch, key = groups.parse_ref(spec.scope)
    folder = cfg.data_dir / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex
    path = folder / f"window-{name}.json"
    prompt_file = _prompt_file(folder, name, spec.prompt)
    if prompt_file:
        lines = spec.prompt.count("\n") + 1
        spec = dataclasses.replace(spec, prompt=PROMPT_FILE_NOTE.format(
            size=len(spec.prompt.encode()) // 1024, path=prompt_file, lines=lines))
    data = {"ref": spec.scope, "who": journal.who_from_env(spec.env), "cwd": str(spec.workdir),
            "args": claude.interactive_args(spec), "env": claude.env(spec),
            "prompt_file": str(prompt_file) if prompt_file else None}
    # The environment holds the watch's secrets: only this user may read the file, and the window deletes it.
    _write_private(path, json.dumps(data))
    state.window_started(watch, key, purpose, turn_id)
    return tmux.new_window(spec.scope, [sys.executable, "-m", "vahtovik", "window", str(path)], spec.workdir)


def _write_private(path: Path, text: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        handle.write(text)


def _prompt_file(folder: Path, name: str, prompt: str) -> Path | None:
    """Write a prompt too long for one argument to a file that the window deletes when claude ends.
    Returns its path, or None when the prompt fits in the argument."""
    if len(prompt.encode()) <= INLINE_PROMPT_LIMIT:
        return None
    cutoff = time.time() - PROMPT_FILE_KEEP.total_seconds()
    for old in folder.glob("prompt-*.md"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    path = folder / f"prompt-{name}.md"
    _write_private(path, prompt)  # it may hold the watch's rules and memory: only this user may read it
    return path


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
            try:
                code = subprocess.run(data["args"], cwd=data["cwd"], env=env).returncode
                screen = tmux.capture(os.environ["TMUX_PANE"]) if os.environ.get("TMUX_PANE") else ""
            except OSError as exc:  # such as a missing binary, or arguments too long
                code, screen = LAUNCH_EXIT, f"claude could not start: {exc}"
                log.error("%s: %s", data["ref"], screen)
    except LockBusy:
        code, screen = BUSY_EXIT, "another process is using the group"
    finally:
        if data.get("prompt_file"):
            Path(data["prompt_file"]).unlink(missing_ok=True)
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
    if record["exit_code"] == LAUNCH_EXIT and (record["exit_screen"] or "").startswith("claude could not start"):
        return record["exit_screen"]
    if record["exit_code"] == 0:
        return "claude exited before the agent ran `vahtovik turn`"
    return f"claude exited with code {record['exit_code']}" + (f": {' / '.join(lines[-3:])}" if lines else "")


def age(state: State, watch: str, key: str) -> timedelta:
    record = state.window_record(watch, key)
    if not record or not record["started_at"]:
        return timedelta.max
    return datetime.now(timezone.utc) - datetime.fromisoformat(record["started_at"])
