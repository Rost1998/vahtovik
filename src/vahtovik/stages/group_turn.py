"""Group turns: start each group's session in a tmux window to process its new artifacts and report.

The run does not wait for a turn. The agent ends it with `vahtovik turn`, and the watchdog looks after the
windows: it ends the turns whose window ended first, and closes the windows of done turns.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from .. import context_file, group_session, groups, skills, windows
from ..claude_session import Claude
from ..config import Config, Watch
from ..state import State
from ..tmux import Tmux, TmuxError
from ..util import LockBusy, duration, human, plural, render

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def run(cfg: Config, state: State, claude: Claude, tmux: Tmux, watch: Watch, run_id: str) -> list[str]:
    """Start a turn for each group with new artifacts. Returns the ids of the turns started."""
    if not Tmux.installed():
        log.error("%s: Skipped group turns: tmux is not installed", watch.name)
        return []
    started = []
    pending = state.pending_groups(watch.name, MAX_ATTEMPTS)
    for index, key in enumerate(pending):
        working = state.working_turns()
        if working >= cfg.claude.parallel_turns:
            log.info("%s: Held back %s: %s work already, the most at once (claude.parallel_turns); "
                     "a later run starts them", watch.name, plural(len(pending) - index, "group turn"),
                     plural(working, "group turn"))
            break
        ref = f"{watch.name}/{key}"
        if tmux.window(ref):
            log.warning("%s: Skipped group turn: its session window is open; a later run will process it", ref)
            continue
        try:
            with groups.lock(cfg.group_dir(watch.name, key)):
                pass
        except LockBusy:
            log.warning("%s: Skipped group turn: the group is busy; a later run will process it", ref)
            continue
        turn_id = turn(cfg, state, claude, tmux, watch, key, run_id)
        if turn_id:
            started.append(turn_id)
    return started


def _read(path: Path) -> str:
    return path.read_text().strip() if path.is_file() else f"({path.name} is missing)"


def _artifacts_text(rows) -> str:
    return "\n".join(f"- `artifacts/{r['file']}`: {r['title']} ({r['url'] or 'no link'}, source `{r['source_id']}`)"
                     for r in rows)


def _process_text(watch: Watch) -> str:
    files = watch.process_files()
    return "\n\n".join(f"### {p.name}\n\n{p.read_text().strip()}" for p in files) or "(no process/*.md files)"


def _channels_text(cfg: Config, watch: Watch) -> str:
    parts = []
    for name in watch.channels:
        try:
            body = skills.load(cfg.skills_dir, "channels", cfg.channels[name].skill).body.strip()
        except skills.SkillError as exc:
            body = f"(skill missing: {exc})"
        parts.append(f"### `{name}`\n\n{body}")
    return "\n\n".join(parts) or "This watch has no channels; do not report."


def _deliveries_text(rows) -> str:
    lines = []
    for r in rows:
        status = f"sent ({r['external_id']})" if r["status"] == "sent" else f"{r['status']}: {r['last_error']}"
        lines.append(f"- delivery {r['id']} to `{r['channel']}`, \"{r['subject']}\": {status}, "
                     f"attempts {r['attempts']}")
    return "\n".join(lines) or "- none"


def turn(cfg: Config, state: State, claude: Claude, tmux: Tmux, watch: Watch, key: str, run_id: str) -> str | None:
    """Start the group's turn in a session window. Returns the turn's id, or None if the window did not start."""
    pending = state.pending_artifacts(watch.name, key, MAX_ATTEMPTS)
    ids = [r["id"] for r in pending]
    context = cfg.group_dir(watch.name, key) / "context.md"
    groups.write_claude_md(cfg, state, watch, key)
    spec, note = group_session.spec_for(cfg, claude, state, watch, key)
    last = state.last_turn(watch.name, key)
    turn_id = uuid.uuid4().hex[:12]
    now = human()
    spec.prompt = render(
        "group_turn.md",
        run_id=run_id, turn_id=turn_id, now=now, session_note=note or "",
        artifacts=_artifacts_text(pending), process_rules=_process_text(watch),
        report_rules=_read(watch.report_md), channels=_channels_text(cfg, watch),
        deliveries=_deliveries_text(state.deliveries_since(watch.name, key, last["started_at"] if last else None)),
    )
    spec.env.update(VAHTOVIK_TURN=turn_id, VAHTOVIK_RUN=run_id)
    state.start_turn(turn_id, run_id, watch.name, key, ids, context_file.fingerprint(context).work_log_entries)
    state.prompt_submitted(watch.name, key)  # running until the agent reports its status
    try:
        window_id = windows.launch(cfg, state, tmux, claude, spec, "turn", turn_id)
    except TmuxError as exc:
        fail(state, state.turn(turn_id), str(exc), 0)
        return None
    log.info("%s: Started group turn: %s, %s, tmux window %s", spec.scope, plural(len(ids), "artifact"),
             group_session.describe(spec, note), window_id)
    return turn_id


def fail(state: State, turn, error: str | None, seconds: float) -> None:
    """Close an unfinished turn as failed. Its artifacts are retried, up to MAX_ATTEMPTS turns."""
    if turn["finished_at"]:
        return
    watch, key = turn["watch"], turn["group_key"]
    group = state.group(watch, key)
    if group["status"] == "failed" and group["status_reason"]:
        error = group["status_reason"]
    error = error or "the session ended before the agent ran `vahtovik turn`"
    ids = json.loads(turn["artifact_ids"] or "[]")
    attempt = 1 + max((r["attempts"] for r in state.rows(
        "SELECT attempts FROM artifacts WHERE watch = ? AND id IN (SELECT value FROM json_each(?))",
        watch, turn["artifact_ids"] or "[]")), default=0)
    state.mark_attempt_failed(watch, ids, MAX_ATTEMPTS)
    state.finish_turn(turn["id"], False, None, error)
    state.set_status(watch, key, "failed", error)
    log.error("%s/%s: Failed group turn after %s (attempt %d of %d%s): %s", watch, key, duration(seconds),
              attempt, MAX_ATTEMPTS, ", giving up" if attempt >= MAX_ATTEMPTS else "", error)
