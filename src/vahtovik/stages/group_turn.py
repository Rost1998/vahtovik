"""Group turns: resume each group's session in a tmux window to process its new artifacts and report."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .. import artifacts, context_file, group_session, groups, skills, windows
from ..claude_session import Claude, SessionResult
from ..config import Config, Watch
from ..state import State
from ..tmux import Tmux, TmuxError
from ..util import LockBusy, duration, human, plural, render

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def run(cfg: Config, state: State, claude: Claude, tmux: Tmux, watch: Watch, run_id: str,
        tick: Callable[[], None] | None = None) -> None:
    if not Tmux.installed():
        log.error("%s: Skipped group turns: tmux is not installed", watch.name)
        return
    for key in state.pending_groups(watch.name, MAX_ATTEMPTS):
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
        turn(cfg, state, claude, tmux, watch, key, run_id, tick)


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


def turn(cfg: Config, state: State, claude: Claude, tmux: Tmux, watch: Watch, key: str, run_id: str,
         tick: Callable[[], None] | None = None) -> bool:
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
    started = time.monotonic()
    try:
        window_id = windows.launch(cfg, state, tmux, claude, spec, "turn", turn_id)
    except TmuxError as exc:
        fail(state, state.turn(turn_id), str(exc), 0)
        return False
    log.info("%s: Started group turn: %s, %s, tmux window %s", spec.scope, plural(len(ids), "artifact"),
             group_session.describe(spec, note), window_id)
    outcome = windows.wait_for_turn(state, tmux, watch.name, key, turn_id, tick)
    seconds = time.monotonic() - started
    group_session.remember(claude, state, watch, key, spec)
    state.log_session(run_id, "group", watch.name, key,
                      SessionResult(ok=outcome == "done", duration_s=seconds, session_id=spec.session_id))
    if outcome == "maintenance":
        log.warning("%s: Paused group turn after %s: %s; its window stays open for a person", spec.scope,
                    duration(seconds), state.group(watch.name, key)["status_reason"] or "it needs a person")
        return False
    if outcome == "background":
        log.info("%s: Paused group turn after %s: the agent waits for its background work (%s); its window stays "
                 "open, and the agent ends the turn when the work wakes it", spec.scope, duration(seconds),
                 state.group(watch.name, key)["status_reason"])
        return False
    if outcome == "done":
        output = json.loads(state.turn(turn_id)["output"])
        status = output["status"] + (f" ({plural(len(output['questions']), 'question')})" if output["questions"] else "")
        log.info("%s: Finished group turn in %s: status %s, %s", spec.scope, duration(seconds), status,
                 plural(len(state.turn_deliveries(turn_id)), "report"))
    else:
        fail(state, state.turn(turn_id), windows.exit_error(state, watch.name, key) if outcome == "ended" else None,
             seconds)
    window = tmux.window(spec.scope)
    if window and not window.clients and outcome != "done":
        tmux.close(window.id)  # a done window stays until the watchdog's check confirms it (watchdog.py)
    artifacts.register_agent_files(cfg, state, watch, key)
    return outcome == "done"


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
