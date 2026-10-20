"""Group status. Agents set it with `vahtovik turn`; Claude Code hooks make sure they do.

    idle         no turn yet
    running      the agent is working: a turn started, or a person sent a message
    finished     the agent is done and waits for nothing
    waiting      the agent needs a person's answer; the questions are in context.md, Open questions
    maintenance  the session is stuck and needs a person
    failed       the session stopped without a status
"""

from __future__ import annotations

import json
import logging
import shlex
import sys

from . import context_file
from .config import Config, Watch
from .state import State
from .util import plural

log = logging.getLogger(__name__)

IMPORTANCE = ("low", "medium", "high", "critical")
MAX_STOP_BLOCKS = 3
COMMAND = ('`vahtovik turn finished`, or `vahtovik turn waiting --question "..."` if you need a person\'s answer '
           "(add `--importance` and `--summary` at the end of an automated turn)")


class StatusError(Exception):
    pass


def hooks() -> dict:
    """Claude Code hooks for group sessions, for the `--settings` of every group session."""
    def command(event: str) -> list[dict]:
        return [{"hooks": [{"type": "command", "command": shlex.join([sys.executable, "-m", "vahtovik", "hook", event])}]}]
    return {"UserPromptSubmit": command("prompt"), "Stop": command("stop")}


def declare(cfg: Config, state: State, watch: Watch, key: str, status: str, importance: str | None = None,
            summary: str | None = None, questions: list[str] | None = None, turn_id: str | None = None) -> str:
    """Record the status the agent gives with `vahtovik turn`. Raises StatusError with what to fix."""
    questions = [q for q in (questions or []) if q.strip()]
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


def on_prompt(state: State, watch: str, key: str, session_id: str | None) -> None:
    """A turn prompt or a person's message was submitted: the agent is working."""
    state.prompt_submitted(watch, key)
    group = state.group(watch, key)
    if group and session_id and group["session_id"] != session_id:
        state.set_session(watch, key, session_id)


def on_stop(state: State, watch: str, key: str) -> dict | None:
    """The agent wants to stop. Returns the hook answer that blocks it, or None to let it stop."""
    group = state.group(watch, key)
    if group is None:
        return None
    if group["status"] == "running":
        if state.add_stop_block(watch, key) <= MAX_STOP_BLOCKS:
            return {"decision": "block", "reason": f"Before you stop, report your status: {COMMAND}."}
        reason = f"the agent stopped {MAX_STOP_BLOCKS + 1} times without running `vahtovik turn`"
        state.set_status(watch, key, "failed", reason)
        log.error("%s/%s: Failed: %s", watch, key, reason)
    state.agent_stopped(watch, key)
    return None
