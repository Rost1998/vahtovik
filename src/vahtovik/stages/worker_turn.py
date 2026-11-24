"""Worker turns: start each worker's session in a tmux window for its new artifacts, its messages, or its scheduled
turn.

The run does not wait for a turn. The agent ends it with `vahtovik turn`, and the watchdog looks after the
windows: it ends the turns whose window ended first, and closes the windows of done turns. A worker whose window is
open gets its messages typed in instead, when its agent is idle and nobody is looking.
"""

from __future__ import annotations

import json
import logging
import uuid

from .. import context_file, inbox, knowledge, windows, worker_status, workers
from ..claude_session import Claude
from ..config import Config, ConfigError
from ..state import State
from ..tmux import Tmux, TmuxError
from ..util import duration, human, plural, render, utc_iso

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SCHEDULED_NOTE = ("This is a scheduled turn: {reason}. Do that check by your rules, even if there are no new "
                  "artifacts. It was your only scheduled turn. If what you check for has not happened yet, or your "
                  "rules want this check again, end this turn with `vahtovik turn scheduled --after ...` or "
                  "`--at ...`: `finished` means that you need no more checks.")
PROMPT_LIMIT = 60_000  # bytes. The prompt is one argument of `claude`, and Linux allows 128 KiB for one argument
PROMPT_FILE = "turn-prompt.md"
LONG_PROMPT = ("The prompt of this turn is long, so vahtovik wrote it to the file `{path}`. Read the whole file now: "
               "it is your prompt for this turn, from vahtovik. Follow it as if it were written here.")


def run(cfg: Config, state: State, claude: Claude, tmux: Tmux, run_id: str,
        roles: list[str] | None = None) -> list[str]:
    """Start a turn for each worker with new artifacts, messages or a due scheduled turn, and type the messages of
    open windows. Returns the ids of the turns started."""
    if not Tmux.installed():
        log.error("Skipped worker turns: tmux is not installed")
        return []
    started, held = [], 0
    for worker_id in state.pending_workers(MAX_ATTEMPTS, roles):
        row = state.worker(worker_id)
        if row["role"] not in cfg.roles:
            continue  # its role left the config, or the command sees only some roles
        window = tmux.window(worker_id)
        if window:
            if not inbox.deliver_typed(cfg, state, tmux, row, window) and _waits(state, row):
                log.warning("%s: Skipped turn: its session window is open; a later run will start it",
                            workers.label(row))
            continue
        if state.working_turns() >= cfg.claude.parallel_turns:
            held += 1
            continue
        if workers.is_busy(cfg, row):
            log.warning("%s: Skipped turn: the worker is busy; a later run will start it", workers.label(row))
            continue
        turn_id = turn(cfg, state, claude, tmux, row, run_id)
        if turn_id:
            started.append(turn_id)
    if held:
        log.info("Held back %s for a later run: already as many work as claude.parallel_turns (%d) allows",
                 plural(held, "worker turn"), cfg.claude.parallel_turns)
    return started


def _waits(state: State, row) -> bool:
    """Whether the worker waits for a turn for more than messages: new artifacts, or a due scheduled turn."""
    return bool(state.pending_artifacts(row["id"], MAX_ATTEMPTS)) or (
        bool(row["scheduled_at"]) and row["scheduled_at"] <= utc_iso() and row["status"] in ("scheduled", "failed"))


def can_start(cfg: Config, state: State, tmux: Tmux, roles: list[str] | None = None) -> bool:
    """Whether a run would start a worker turn now, or type messages into an open window."""
    if not Tmux.installed():
        return False
    open_windows = {w.worker: w for w in tmux.windows()}
    for worker_id in state.pending_workers(MAX_ATTEMPTS, roles):
        row = state.worker(worker_id)
        if row["role"] not in cfg.roles:
            continue
        window = open_windows.get(worker_id)
        if window:
            if inbox.typed_text(state, row, window):
                return True
        elif state.working_turns() < cfg.claude.parallel_turns and not workers.is_busy(cfg, row):
            return True
    return False


def _artifacts_text(rows) -> str:
    return "\n".join(f"- `artifacts/{r['file']}`: {r['title']} ({r['url'] or 'no link'}, source `{r['source_id']}`)"
                     for r in rows)


def turn(cfg: Config, state: State, claude: Claude, tmux: Tmux, row, run_id: str) -> str | None:
    """Start the worker's turn in a session window. Returns the turn's id, or None if the window did not start."""
    pending = state.pending_artifacts(row["id"], MAX_ATTEMPTS)
    messages = state.pending_messages(row["id"], MAX_ATTEMPTS)
    ids, message_ids = [r["id"] for r in pending], [m["id"] for m in messages]
    turn_id = uuid.uuid4().hex[:12]
    now = human()
    state.start_turn(turn_id, run_id, row["id"], ids, message_ids,
                     context_file.fingerprint(workers.context_md(cfg, row)).work_log_entries)
    scheduled = state.take_schedule(row["id"], turn_id, utc_iso())
    state.deliver_messages(message_ids, "turn", turn_id)
    row = state.worker(row["id"])
    try:
        spec, note = workers.spec_for(cfg, claude, state, row)
        found, _ = workers.skills_of(cfg, row)
        changes, snapshot = workers.status_note(cfg, state, row, spec, found)
    except (ConfigError, knowledge.SkillError) as exc:
        fail(cfg, state, state.turn(turn_id), str(exc), 0)
        return None
    state.set_turn_snapshot(turn_id, snapshot)
    notes = [note, SCHEDULED_NOTE.format(reason=scheduled) if scheduled else None, changes]
    # Only what belongs to this turn: the worker's CLAUDE.md holds what stays (see workers.status_note).
    spec.prompt = render(
        "worker_turn.md",
        run_id=run_id, turn_id=turn_id, now=now,
        notes="\n\n".join(filter(None, notes)) or "No notes for this turn.",
        artifacts=_artifacts_text(pending) or "- none",
        messages=inbox.prompt_text(state, row, messages), schedule=workers.schedule_text(row),
    )
    if len(spec.prompt.encode()) > PROMPT_LIMIT:  # long messages
        (spec.workdir / PROMPT_FILE).write_text(spec.prompt)
        spec.prompt = LONG_PROMPT.format(path=spec.workdir / PROMPT_FILE)
    spec.env.update(VAHTOVIK_TURN=turn_id, VAHTOVIK_RUN=run_id)
    state.prompt_submitted(row["id"])  # running until the agent reports its status
    try:
        window_id = windows.launch(cfg, state, tmux, claude, spec, "turn", turn_id)
    except TmuxError as exc:
        fail(cfg, state, state.turn(turn_id), str(exc), 0)
        return None
    what = [plural(len(ids), "artifact")] + ([plural(len(message_ids), "message")] if message_ids else [])
    what += ["scheduled"] if scheduled else []
    log.info("%s: Started turn: %s, %s, tmux window %s", workers.label(row), ", ".join(what),
             workers.describe(spec, note), window_id)
    return turn_id


def retries_left(state: State, row) -> bool:
    """Whether a later run will try the worker's failed work again: artifacts, messages or a scheduled turn."""
    return bool(state.pending_artifacts(row["id"], MAX_ATTEMPTS) or state.pending_messages(row["id"], MAX_ATTEMPTS)
                or (row["scheduled_at"] and row["scheduled_attempts"] < MAX_ATTEMPTS))


def fail(cfg: Config, state: State, turn, error: str | None, seconds: float) -> None:
    """Close an unfinished turn as failed. Its artifacts, messages and scheduled turn are tried again, up to
    MAX_ATTEMPTS turns. When nothing is left to try, the worker's parent hears of it (report-back)."""
    if turn["finished_at"]:
        return
    row = state.worker(turn["worker"])
    if row["status"] == "failed" and row["status_reason"]:
        error = row["status_reason"]
    error = error or "the session ended before the agent ran `vahtovik turn`"
    ids = json.loads(turn["artifact_ids"] or "[]")
    message_ids = json.loads(turn["message_ids"] or "[]")
    tries = [r["attempts"] for r in state.rows(
        "SELECT attempts FROM artifacts WHERE worker = ? AND id IN (SELECT value FROM json_each(?))",
        row["id"], turn["artifact_ids"] or "[]")]
    tries += [m["attempts"] for m in state.messages(message_ids)]
    tries += [row["scheduled_attempts"]] if turn["scheduled_for"] else []
    attempt = 1 + max(tries, default=0)
    state.mark_attempt_failed(row["id"], ids, MAX_ATTEMPTS)
    state.messages_failed(message_ids, MAX_ATTEMPTS)
    state.restore_schedule(turn)
    state.finish_turn(turn["id"], False, None, error)
    state.set_status(row["id"], "failed", error)
    log.error("%s: Failed turn after %s (attempt %d of %d%s): %s", workers.label(row), duration(seconds), attempt,
              MAX_ATTEMPTS, ", giving up" if attempt >= MAX_ATTEMPTS else "", error)
    row = state.worker(row["id"])
    if not retries_left(state, row):
        worker_status.report_back(state, row, "failed", reason=error)
