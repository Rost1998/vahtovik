"""The persistent Claude Code session of each group: new, resumed, or opened by a person."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import ExitStack

from . import artifacts, groups, skills
from .claude_session import Claude, SessionSpec
from .config import Config, ConfigError, Watch
from .state import State
from .util import LockBusy, duration

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
    watch_name, key = groups.parse_ref(ref)
    watch = cfg.watch(watch_name)
    if state.group(watch_name, key) is None:
        raise ConfigError(f"unknown group {ref}; see `vahtovik status --watch {watch_name}`")
    group_dir = cfg.group_dir(watch_name, key)
    claude = Claude(cfg)
    with ExitStack() as stack:
        try:
            stack.enter_context(groups.lock(group_dir))
        except LockBusy:
            print(f"An automated turn is using {ref}; waiting for it to finish...")
            stack.enter_context(groups.lock(group_dir, wait=True))
        groups.write_claude_md(cfg, state, watch, key)
        spec, note = spec_for(cfg, claude, state, watch, key, force_new=new_session)
        if spec.mode == "new":
            spec.prompt = f"{note}\n\n{OPEN_NEW_PROMPT}" if note else OPEN_NEW_PROMPT
        log.info("%s: Started interactive session, %s", ref, describe(spec, note))
        started = time.monotonic()
        code = claude.interactive(spec)
        remember(claude, state, watch, key, spec)
        log.info("%s: Finished interactive session after %s%s", ref, duration(time.monotonic() - started),
                 f" (exit code {code})" if code else "")
    for note in artifacts.register_agent_files(cfg, state, watch, key):
        print(note)
    return code
