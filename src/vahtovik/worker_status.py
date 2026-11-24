"""Worker status. Agents set it with `vahtovik turn`; Claude Code hooks make sure they do.

    idle         no turn yet
    running      the agent is working: a turn started, or a person or a message reached it
    finished     the agent's work is really done: it gets a turn again only for new artifacts or messages
    scheduled    the agent is done for now: it gets a turn at a set time, even without new artifacts
    waiting      the agent needs a person's answer; the questions are in context.md, Open questions
    background   the agent waits for its own background work, which wakes it when it ends
    maintenance  the session is stuck and needs a person
    failed       the session stopped without a status
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from datetime import datetime, timedelta, timezone

from . import context_file, inbox, workers
from .config import Config
from .state import State
from .util import duration, human, plural, utc_iso

log = logging.getLogger(__name__)

IMPORTANCE = ("low", "medium", "high", "critical")
MAX_STOP_BLOCKS = 3
COMMAND = ('`vahtovik turn finished` if your work is really done, or `vahtovik turn scheduled --after 6h` (or '
           '`--at 08:00`) if you need another turn later, or `vahtovik turn waiting --question "..."` if you need a '
           "person's answer (add `--importance` and `--summary` at the end of an automated turn), or "
           '`vahtovik turn background --reason "..."` if you wait for background work you started')
DONE = ("finished", "scheduled", "waiting")  # statuses of an agent that says it is done
DONE_TASKS = ("completed", "failed", "killed", "stopped", "cancelled", "canceled", "done", "error")


class StatusError(Exception):
    pass


COMPACTED = ("Claude Code compacted your context, so what you read from files earlier may be gone from it. Your "
             "CLAUDE.md is loaded again. Read your role's memory again now: `{memory}`. Before you use a skill, read "
             "its `SKILL.md` again. Then go on with what you were doing.")


def hooks() -> dict:
    """Claude Code hooks for worker sessions, for the `--settings` of every worker session. `SessionStart` with
    the matcher `compact` runs right after a compaction, and what its command prints goes into the context."""
    def command(event: str, matcher: str | None = None) -> list[dict]:
        hook = {"type": "command", "command": shlex.join([sys.executable, "-m", "vahtovik", "hook", event])}
        return [{**({"matcher": matcher} if matcher else {}), "hooks": [hook]}]
    return {"UserPromptSubmit": command("prompt"), "Stop": command("stop"),
            "SessionStart": command("compact", "compact")}


def on_compact(cfg: Config, row) -> str:
    """The context of the worker's session was compacted. Returns what to tell the agent: Claude Code puts
    CLAUDE.md back by itself, but not what the agent read from files."""
    log.info("%s: Claude Code compacted the session's context", workers.label(row))
    return COMPACTED.format(memory=workers.role_of(cfg, row).memory_md)


def background_work(hook_data: dict) -> list[str] | None:
    """What Claude Code still runs for the agent, from the Stop hook's data: background commands and agents,
    and scheduled prompts. None if this Claude Code version does not tell."""
    if "background_tasks" not in hook_data:
        return None
    work = [f"{t.get('type') or 'task'} \"{t.get('description') or t.get('command') or t.get('id')}\""
            for t in hook_data.get("background_tasks") or [] if str(t.get("status", "")).lower() not in DONE_TASKS]
    crons = hook_data.get("session_crons") or []
    if crons:
        work.append(plural(len(crons), "scheduled prompt"))
    return work


def declare(cfg: Config, state: State, row, status: str, importance: str | None = None,
            summary: str | None = None, questions: list[str] | None = None, turn_id: str | None = None,
            reason: str | None = None, check_after: timedelta | None = None, after: timedelta | None = None,
            at: datetime | None = None) -> str:
    """Record the status the agent gives with `vahtovik turn`. Raises StatusError with what to fix."""
    questions = [q for q in (questions or []) if q.strip()]
    if status != "scheduled" and (after is not None or at is not None):
        raise StatusError("--after and --at are for `vahtovik turn scheduled`")
    if status == "background":
        return _declare_background(state, row, reason, check_after, questions)
    if status == "waiting" and not questions:
        raise StatusError('waiting needs at least one --question "..."')
    if status in ("finished", "scheduled") and questions:
        raise StatusError(f"{status} takes no questions; use `vahtovik turn waiting` when you need an answer")
    schedule = _schedule(row, after, at, reason) if status == "scheduled" else None
    context = workers.context_md(cfg, row)
    turn = state.turn(turn_id) if turn_id else None
    ends_turn = turn is not None and turn["finished_at"] is None and turn["worker"] == row["id"]
    if ends_turn:
        missing = [flag for flag, value in (("--importance", importance), ("--summary", summary)) if not value]
        if missing:
            raise StatusError(f"this ends an automated turn, so give {' and '.join(missing)} too")
        if context_file.fingerprint(context).work_log_entries <= (turn["work_log_before"] or 0):
            raise StatusError("add your Work log entry for this turn to context.md first, then run this again")
    context_file.replace_section(context, "Open questions", context_file.questions_text(questions), after="Summary")
    # A run starts turns by the status and the new artifacts: it must not see the one without the other.
    with state.transaction():
        state.set_status(row["id"], status, schedule[1] if schedule else None, questions=questions)
        if status == "finished":  # the work is really done
            state.set_schedule(row["id"], None)
        elif status == "scheduled":
            state.set_schedule(row["id"], *schedule)
        # a waiting worker keeps its scheduled turn for after the answer
        if summary:
            state.set_summary(row["id"], summary.strip())
        if ends_turn:
            output = {"status": status, "importance": importance, "summary": summary, "questions": questions}
            state.finish_turn(turn_id, True, output, None)
            state.set_seen(row["id"], turn["snapshot"])  # the agent got this turn's notes (workers.status_note)
            state.mark_processed(row["id"], json.loads(turn["artifact_ids"] or "[]"))
            state.touch_worker(row["id"])
    what = status + (f" ({plural(len(questions), 'question')})" if questions else "")
    what += f" for {human(datetime.fromisoformat(schedule[0]))}" if schedule else ""
    what += f", importance {importance}" if importance else ""
    log.info("%s: Status %s%s", workers.label(row), what, f": {summary[:150]}" if summary else "")
    if status in ("finished", "waiting"):
        report_back(state, state.worker(row["id"]), status, summary, questions)
    return f"recorded: status {what}"


def _schedule(row, after: timedelta | None, at: datetime | None, reason: str | None) -> tuple[str, str]:
    """When and why the worker gets its next scheduled turn: `--after`, `--at`, or, with neither, the scheduled
    turn it has already."""
    reason = (reason or "").strip()
    if after is not None and at is not None:
        raise StatusError("give --after or --at, not both")
    if after is not None:
        return utc_iso(datetime.now(timezone.utc) + after), reason or "a check you scheduled"
    if at is not None:
        return utc_iso(at), reason or "a check you scheduled"
    if not row["scheduled_at"]:
        raise StatusError("you have no scheduled turn to keep: give --after (such as 6h) or --at (such as 08:00, "
                          "or 2026-11-25 08:00): when you need your next turn")
    return row["scheduled_at"], reason or row["scheduled_reason"] or "a scheduled check"


def _declare_background(state: State, row, reason: str | None, check_after: timedelta | None,
                        questions: list[str]) -> str:
    if not (reason or "").strip():
        raise StatusError('background needs --reason "<what you wait for>"')
    if questions:
        raise StatusError("background takes no questions; use `vahtovik turn waiting` when you need an answer")
    deadline = utc_iso(datetime.now(timezone.utc) + check_after) if check_after else None
    state.set_status(row["id"], "background", reason.strip(), check_after=deadline)
    after = f", check after {duration(check_after.total_seconds())}" if check_after else ""
    log.info("%s: Status background%s: %s", workers.label(row), after, reason.strip()[:150])
    return (f"recorded: status background{after}. Stop now; Claude Code wakes you when your background work ends. "
            f"Then finish the work and end with {COMMAND}.")


def report_back(state: State, row, status: str, summary: str | None = None, questions: list[str] = (),
                reason: str | None = None) -> None:
    """Tell the parent of a worker spawned with --report-back how the worker ended its turn: finished, waiting,
    or failed for good. The message goes into the parent's inbox, so the parent gets a turn for it."""
    if not row["report_back"] or not row["parent"]:
        return
    parent = state.worker(row["parent"])
    if parent is not None and parent["archived_at"]:
        parent = state.merge_target(parent)
    if parent is None:
        log.warning("%s: Skipped reporting back: its parent %s is archived", workers.label(row), row["parent"])
        return
    lines = [f"Your child worker {workers.name(row)} (role {row['role']}, \"{row['title']}\") is {status} now."]
    lines += [f"Summary: {summary.strip()}"] if summary else []
    lines += [f"Its questions: {' / '.join(questions)}"] if questions else []
    lines += [f"Why: {reason.strip()}"] if reason else []
    lines.append(f"See it: vahtovik worker show {workers.name(row)}")
    inbox.send(state, parent, f"worker:{row['id']}", "\n".join(lines))


def on_prompt(state: State, worker_id: str, session_id: str | None) -> None:
    """A turn prompt, a message or a person's words were submitted: the agent is working."""
    state.prompt_submitted(worker_id)
    row = state.worker(worker_id)
    if row and session_id and row["session_id"] != session_id:
        state.set_session(worker_id, session_id)


def on_stop(state: State, worker_id: str, hook_data: dict | None = None, turn_id: str | None = None) -> dict | None:
    """The agent wants to stop. Returns the hook answer that blocks it, or None to let it stop.

    Claude Code says in `hook_data` what background work it still runs for the agent. A `finished`, `scheduled` or
    `waiting` agent with such work becomes `background`, and the turn it just ended is open again; a `background`
    agent without any is told that nothing will wake it."""
    row = state.worker(worker_id)
    if row is None:
        return None
    status, work = row["status"], background_work(hook_data or {})
    name = workers.label(row)
    if status == "running":
        if state.add_stop_block(worker_id) <= MAX_STOP_BLOCKS:
            return {"decision": "block", "reason": f"Before you stop, report your status: {COMMAND}."}
        reason = f"the agent stopped {MAX_STOP_BLOCKS + 1} times without running `vahtovik turn`"
        state.set_status(worker_id, "failed", reason)
        log.error("%s: Failed: %s", name, reason)
    elif status in DONE and work:
        reason = "Claude Code still runs background work for the agent: " + "; ".join(work)
        turn = state.turn(turn_id) if turn_id else None
        with state.transaction():
            if turn and turn["ok"] and turn["finished_at"] >= (row["prompt_at"] or ""):
                state.reopen_turn(turn_id)
            state.set_status(worker_id, "background", reason)
        log.warning("%s: Status background, not %s: %s", name, status, reason)
    elif status == "background" and work == []:
        if state.add_stop_block(worker_id) <= MAX_STOP_BLOCKS:
            return {"decision": "block", "reason": "You reported `background`, but Claude Code runs no background "
                    f"work for you, so nothing will wake you. Finish the work now, then report: {COMMAND}."}
        reason = "the agent reported `background`, but no background work runs that would wake it"
        state.set_status(worker_id, "maintenance", reason)
        log.warning("%s: Needs a person: %s", name, reason)
    state.agent_stopped(worker_id)
    return None
