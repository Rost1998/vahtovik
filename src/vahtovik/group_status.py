"""Group status. Agents set it with `vahtovik turn`; Claude Code hooks make sure they do.

    idle         no turn yet
    running      the agent is working: a turn started, or a person sent a message
    finished     the agent is done and waits for nothing
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

from . import context_file
from .config import Config, Watch
from .state import State
from .util import duration, plural, utc_iso

log = logging.getLogger(__name__)

IMPORTANCE = ("low", "medium", "high", "critical")
MAX_STOP_BLOCKS = 3
COMMAND = ('`vahtovik turn finished`, or `vahtovik turn waiting --question "..."` if you need a person\'s answer '
           "(add `--importance` and `--summary` at the end of an automated turn), or "
           '`vahtovik turn background --reason "..."` if you wait for background work you started')
DONE_TASKS = ("completed", "failed", "killed", "stopped", "cancelled", "canceled", "done", "error")


class StatusError(Exception):
    pass


def hooks() -> dict:
    """Claude Code hooks for group sessions, for the `--settings` of every group session."""
    def command(event: str) -> list[dict]:
        return [{"hooks": [{"type": "command", "command": shlex.join([sys.executable, "-m", "vahtovik", "hook", event])}]}]
    return {"UserPromptSubmit": command("prompt"), "Stop": command("stop")}


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


def declare(cfg: Config, state: State, watch: Watch, key: str, status: str, importance: str | None = None,
            summary: str | None = None, questions: list[str] | None = None, turn_id: str | None = None,
            reason: str | None = None, check_after: timedelta | None = None) -> str:
    """Record the status the agent gives with `vahtovik turn`. Raises StatusError with what to fix."""
    questions = [q for q in (questions or []) if q.strip()]
    if status == "background":
        return _declare_background(state, watch.name, key, reason, check_after, questions)
    if status == "waiting" and not questions:
        raise StatusError('waiting needs at least one --question "..."')
    if status == "finished" and questions:
        raise StatusError("finished takes no questions; use `vahtovik turn waiting` when you need an answer")
    if state.group(watch.name, key) is None:
        raise StatusError(f"unknown group {watch.name}/{key}")
    context = cfg.group_dir(watch.name, key) / "context.md"
    turn = state.turn(turn_id) if turn_id else None
    ends_turn = turn is not None and turn["finished_at"] is None
    if ends_turn:
        missing = [flag for flag, value in (("--importance", importance), ("--summary", summary)) if not value]
        if missing:
            raise StatusError(f"this ends an automated turn, so give {' and '.join(missing)} too")
        if context_file.fingerprint(context).work_log_entries <= (turn["work_log_before"] or 0):
            raise StatusError("add your Work log entry for this turn to context.md first, then run this again")
    context_file.replace_section(context, "Open questions", context_file.questions_text(questions), after="Summary")
    # A run starts turns by the status and the new artifacts: it must not see the one without the other.
    with state.transaction():
        state.set_status(watch.name, key, status, questions=questions)
        if ends_turn:
            output = {"status": status, "importance": importance, "summary": summary, "questions": questions}
            state.finish_turn(turn_id, True, output, None)
            state.mark_processed(watch.name, json.loads(turn["artifact_ids"] or "[]"))
            state.touch_group(watch.name, key)
    what = status + (f" ({plural(len(questions), 'question')})" if questions else "")
    what += f", importance {importance}" if importance else ""
    log.info("%s/%s: Status %s%s", watch.name, key, what, f": {summary[:150]}" if summary else "")
    return f"recorded: status {what}"


def _declare_background(state: State, watch: str, key: str, reason: str | None, check_after: timedelta | None,
                        questions: list[str]) -> str:
    if not (reason or "").strip():
        raise StatusError('background needs --reason "<what you wait for>"')
    if questions:
        raise StatusError("background takes no questions; use `vahtovik turn waiting` when you need an answer")
    if state.group(watch, key) is None:
        raise StatusError(f"unknown group {watch}/{key}")
    deadline = utc_iso(datetime.now(timezone.utc) + check_after) if check_after else None
    state.set_status(watch, key, "background", reason.strip(), check_after=deadline)
    after = f", check after {duration(check_after.total_seconds())}" if check_after else ""
    log.info("%s/%s: Status background%s: %s", watch, key, after, reason.strip()[:150])
    return (f"recorded: status background{after}. Stop now; Claude Code wakes you when your background work ends. "
            f"Then finish the work and end with {COMMAND}.")


def on_prompt(state: State, watch: str, key: str, session_id: str | None) -> None:
    """A turn prompt or a person's message was submitted: the agent is working."""
    state.prompt_submitted(watch, key)
    group = state.group(watch, key)
    if group and session_id and group["session_id"] != session_id:
        state.set_session(watch, key, session_id)


def on_stop(state: State, watch: str, key: str, hook_data: dict | None = None,
            turn_id: str | None = None) -> dict | None:
    """The agent wants to stop. Returns the hook answer that blocks it, or None to let it stop.

    Claude Code says in `hook_data` what background work it still runs for the agent. A `finished` or
    `waiting` agent with such work becomes `background`, and the turn it just ended is open again; a
    `background` agent without any is told that nothing will wake it."""
    group = state.group(watch, key)
    if group is None:
        return None
    status, work = group["status"], background_work(hook_data or {})
    if status == "running":
        if state.add_stop_block(watch, key) <= MAX_STOP_BLOCKS:
            return {"decision": "block", "reason": f"Before you stop, report your status: {COMMAND}."}
        reason = f"the agent stopped {MAX_STOP_BLOCKS + 1} times without running `vahtovik turn`"
        state.set_status(watch, key, "failed", reason)
        log.error("%s/%s: Failed: %s", watch, key, reason)
    elif status in ("finished", "waiting") and work:
        reason = "Claude Code still runs background work for the agent: " + "; ".join(work)
        turn = state.turn(turn_id) if turn_id else None
        with state.transaction():
            if turn and turn["ok"] and turn["finished_at"] >= (group["prompt_at"] or ""):
                state.reopen_turn(turn_id)
            state.set_status(watch, key, "background", reason)
        log.warning("%s/%s: Status background, not %s: %s", watch, key, status, reason)
    elif status == "background" and work == []:
        if state.add_stop_block(watch, key) <= MAX_STOP_BLOCKS:
            return {"decision": "block", "reason": "You reported `background`, but Claude Code runs no background "
                    f"work for you, so nothing will wake you. Finish the work now, then report: {COMMAND}."}
        reason = "the agent reported `background`, but no background work runs that would wake it"
        state.set_status(watch, key, "maintenance", reason)
        log.warning("%s/%s: Needs a person: %s", watch, key, reason)
    state.agent_stopped(watch, key)
    return None
