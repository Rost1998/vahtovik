"""The persistent Claude Code session of each group: new, resumed, or opened by a person."""

from __future__ import annotations

import logging
import uuid

from . import groups, skills, windows
from .claude_session import Claude, SessionSpec
from .config import Config, ConfigError, Watch
from .state import State
from .tmux import Tmux
from .util import LockBusy

log = logging.getLogger(__name__)

NEW_FOR_EXISTING = (
    "Note: this is a new session for an existing group; the earlier session was lost or reset. "
    "Read CLAUDE.md, context.md and the files in artifacts/ to rebuild your picture before you start."
)
OPEN_NEW_PROMPT = (
    "A person opened this group's session. Read CLAUDE.md and context.md, then tell them in a few lines "
    "what this group is about and what has been done so far."
)


def spec_for(cfg: Config, claude: Claude, state: State, watch: Watch, key: str,
             force_new: bool = False) -> tuple[SessionSpec, str | None]:
    """Build the session spec for this group. Returns (spec, note for the prompt or None)."""
    group = state.group(watch.name, key)
    stored = group["session_id"] if group else None
    spec = SessionSpec(
        stage="group",
        workdir=cfg.group_dir(watch.name, key),
        prompt="",
        settings=cfg.stage(watch, "group"),
        env={**watch.secrets(), "VAHTOVIK_WATCH": watch.name, "VAHTOVIK_GROUP": key},
        scope=f"{watch.name}/{key}",
    )
    if stored and not force_new and claude.session_exists(stored):
        spec.mode, spec.session_id = "resume", stored
        return spec, None
    spec.mode, spec.session_id = "new", str(uuid.uuid4())
    spec.name = f"vahtovik: {watch.name}/{key}"
    spec.system_prompt = skills.main_skill(cfg.skills_dir).body
    return spec, NEW_FOR_EXISTING if stored else None


def describe(spec: SessionSpec, note: str | None) -> str:
    """How the session starts, for the journal: "resumed session d097ae50" or "new session ..."."""
    if spec.mode == "resume":
        return f"resumed session {spec.session_id[:8]}"
    return f"new session {spec.session_id[:8]}" + (" (the earlier one was lost or reset)" if note else "")


def remember(claude: Claude, state: State, watch: Watch, key: str, spec: SessionSpec) -> None:
    """Store a new session's identifier once Claude Code has saved the session."""
    if spec.mode == "new" and claude.session_exists(spec.session_id):
        state.set_session(watch.name, key, spec.session_id)


def open_session(cfg: Config, state: State, ref: str, new_session: bool = False) -> int:
    """Show the group's session window to the person; start one if the session is not running."""
    watch_name, key = groups.parse_ref(ref)
    watch = cfg.watch(watch_name)
    if state.group(watch_name, key) is None:
        raise ConfigError(f"unknown group {ref}; see `vahtovik status --watch {watch_name}`")
    tmux = Tmux(cfg)
    window = tmux.window(ref)
    if window and new_session:
        raise ConfigError(f"{ref} is running in tmux window {window.id}; close that window first "
                          f"(tmux kill-window -t {window.id}) to start a new session")
    if window is None:
        try:
            with groups.lock(cfg.group_dir(watch_name, key)):
                pass
        except LockBusy:
            raise ConfigError(f"{ref} is busy; try again in a moment") from None
        groups.write_claude_md(cfg, state, watch, key)
        claude = Claude(cfg)
        spec, note = spec_for(cfg, claude, state, watch, key, force_new=new_session)
        if spec.mode == "new":
            spec.prompt = f"{note}\n\n{OPEN_NEW_PROMPT}" if note else OPEN_NEW_PROMPT
        window_id = windows.launch(cfg, state, tmux, claude, spec, "open")
        log.info("%s: Started interactive session, %s, tmux window %s", ref, describe(spec, note), window_id)
    else:
        window_id = window.id
    if tmux.attach(window_id):
        print(f"Switched to {ref} (tmux session {tmux.session}, window {window_id}). Switch back to your own "
              "window whenever you like: the agent keeps working, and vahtovik closes the window when it is done.")
    else:
        print(f"Detached. The session keeps running in tmux window {window_id}; vahtovik closes the window when "
              f"the agent is done. `vahtovik open {ref}` brings you back.")
    return 0
