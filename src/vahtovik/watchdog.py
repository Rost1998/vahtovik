"""The watchdog looks after the worker sessions at the start of a run: it closes the windows of failed workers,
fails the turns whose window is gone, and sends notices. It asks an AI session to check the worker sessions that
need it, and carries out the action the AI picks from a fixed list (skills/watchdog/SKILL.md). vahtovik checks
every pick before it acts, and closes a done window only after such a check. The run timer sets its pace.

Notices go to vahtovik's `notices` channels: by default every input channel that is not limited."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import delivery, knowledge, windows, worker_status, workers
from .claude_session import Claude, SessionSpec
from .config import Config
from .stages import worker_turn
from .state import State
from .tmux import Tmux, Window
from .util import duration, human, load_schema, plural, render

log = logging.getLogger(__name__)

GRACE = timedelta(seconds=10)  # a window of `vahtovik open` is left alone this long, so the person can attach to it
CLOSABLE = ("finished", "scheduled", "waiting", "idle")  # statuses of an agent that is done, once it is idle
WORKING = ("running", "background")  # statuses the AI checks every time, changed or not
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
DONE_WHAT = {"press": "Pressed keys", "nudge": "Nudged", "resume": "Resumed", "retry": "Retrying", "failed": "Failed"}


def tick(cfg: Config, state: State, tmux: Tmux, claude: Claude | None = None, run_id: str | None = None) -> None:
    tidy(cfg, state, tmux)
    check(cfg, state, tmux, claude or Claude(cfg), run_id)
    notify(cfg, state, tmux)
    warn_long_turns(cfg, state, tmux)
    warn_given_up(cfg, state)


def tidy(cfg: Config, state: State, tmux: Tmux) -> None:
    """Close the windows of failed workers that nobody looks at; fail the turns whose window is gone."""
    open_ids = set()
    for window in tmux.windows():
        open_ids.add(window.worker)
        row = state.worker(window.worker)
        if row is None or window.clients or row["status"] != "failed" or _just_opened(state, window.worker):
            continue
        _close(cfg, state, tmux, window, "status failed")
        open_ids.discard(window.worker)
    for turn in state.unfinished_turns():
        if turn["worker"] not in open_ids:
            started = datetime.fromisoformat(turn["started_at"])
            worker_turn.fail(cfg, state, turn, windows.exit_error(state, turn["worker"]),
                             (datetime.now(timezone.utc) - started).total_seconds())


def _close(cfg: Config, state: State, tmux: Tmux, window: Window, why: str) -> None:
    tmux.close(window.id)
    row = state.worker(window.worker)
    log.info("%s: Closed the session window: %s", workers.label(row) if row else window.worker, why)
    deadline = time.monotonic() + 5  # the window's program lets go of the worker's lock as it ends
    while row is not None and workers.is_busy(cfg, row) and time.monotonic() < deadline:
        time.sleep(0.1)


def _just_opened(state: State, worker_id: str) -> bool:
    """`vahtovik open` started the window a moment ago: the person may not have attached to it yet."""
    record = state.window_record(worker_id)
    return bool(record and record["purpose"] == "open") and windows.age(state, worker_id) < GRACE


def _time(value: str | None) -> datetime:
    return datetime.fromisoformat(value) if value else EPOCH


def _last_activity(state: State, claude: Claude, row, window: Window | None) -> datetime:
    """The latest change: a prompt or a status, and for an open window also its start and the session's
    conversation file. The end of a window is no change: the watchdog itself ends the windows it checked, and
    Claude Code writes to the conversation file as it ends."""
    times = [_time(row["prompt_at"]), _time(row["status_at"])]
    if window:
        record = state.window_record(row["id"])
        times.append(_time(record and record["started_at"]))
        path = claude.session_file(row["session_id"])
        if path:
            times.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    return max(times)


@dataclass
class Session:
    """A worker session the AI checks."""
    worker: object  # the workers row
    window: Window | None

    @property
    def name(self) -> str:
        return workers.name(self.worker)

    @property
    def label(self) -> str:
        return workers.label(self.worker)


def sessions_to_check(cfg: Config, state: State, tmux: Tmux, claude: Claude) -> list[Session]:
    """The sessions the AI should check: every working one (`running` or `background`, with a window or
    without one, which means it was interrupted), and every other one that changed since the last check:
    with an open window, or `finished`, `scheduled` or `waiting` with a window that ended before a check. Not the
    ones a person looks at, nor the ones that wait for a person (`maintenance`) or are failed."""
    open_windows = {w.worker: w for w in tmux.windows()}
    found = []
    for row in state.workers():
        if row["role"] not in cfg.roles:
            continue
        status, window = row["status"], open_windows.get(row["id"])
        if window and (window.clients or _just_opened(state, row["id"])):
            continue
        if status in ("maintenance", "failed") or (window is None and status == "idle"):
            continue
        # Times are kept to the second: a change in the second of the last check counts as a change.
        changed = _last_activity(state, claude, row, window) >= _time(row["checked_at"])
        if status in WORKING or changed:
            found.append(Session(row, window))
    return found


def check(cfg: Config, state: State, tmux: Tmux, claude: Claude, run_id: str | None) -> None:
    """Ask the watchdog AI to check the sessions that need it, then carry out its picks."""
    sessions = sessions_to_check(cfg, state, tmux, claude)
    if not sessions:
        return
    skill = knowledge.watchdog_skill(cfg)
    screens = {str(s["id"]): s for s in skill.meta.get("screens") or []}
    workdir = cfg.data_dir / "watchdog"
    workdir.mkdir(parents=True, exist_ok=True)
    spec = SessionSpec(
        stage="watchdog", workdir=workdir, settings=cfg.stage("watchdog"), schema=load_schema("watchdog"),
        system_prompt=skill.body, mode="throwaway", scope="watchdog",
        prompt=render("watchdog.md", now=human(), example=sessions[0].name,
                      sessions="\n\n".join(_facts(cfg, state, tmux, claude, s) for s in sessions),
                      screens="\n".join(f"- `{i}`: {s['looks_like']}" for i, s in screens.items()) or "- none"),
    )
    names = ", ".join(s.name for s in sessions)
    log.info("Watchdog: Started a check of %s: %s", plural(len(sessions), "session"), names)
    result = claude.run(spec, cfg.runs_dir(run_id or "watchdog"))
    state.log_session(run_id, "watchdog", "", "watchdog", result)
    if not result.ok:
        log.error("Watchdog: Failed a check after %s: %s", duration(result.duration_s), result.error)
        return
    by_name = {s.name: s for s in sessions}
    for answer in result.output["sessions"]:
        if answer["worker"] in by_name:
            session = by_name.pop(answer["worker"])
            state.worker_checked(session.worker["id"])  # only an answer counts as a check
            _act(cfg, state, tmux, claude, session, answer, skill.meta, screens)
    log.info("Watchdog: Finished a check in %s, $%.2f", duration(result.duration_s), result.cost_usd)


def _ago(value: str | None) -> str:
    if not value:
        return "never"
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    return f"{human(datetime.fromisoformat(value))} ({duration(abs(seconds))} {'ago' if seconds >= 0 else 'from now'})"


def _facts(cfg: Config, state: State, tmux: Tmux, claude: Claude, session: Session) -> str:
    row = session.worker
    lines = [f"- Status: `{row['status']}` since {_ago(row['status_at'])}"
             + (f": {row['status_reason']}" if row["status_reason"] else "")]
    if row["check_after"]:
        lines.append(f"- Check after: {_ago(row['check_after'])}")
    questions = json.loads(row["questions"] or "[]") if row["status"] == "waiting" else []
    if questions:
        lines.append("- Open questions: " + " / ".join(questions))
    lines.append("- Agent idle (it stopped and waits at its input prompt): " + ("yes" if row["idle"] else "no"))
    lines.append(f"- Last prompt to the agent: {_ago(row['prompt_at'])}")
    turn = state.latest_turn(row["id"])
    if turn:
        how = "not finished" if not turn["finished_at"] else ("finished" if turn["ok"] else f"failed: {turn['error']}")
        lines.append(f"- Latest turn: started {_ago(turn['started_at'])}, {how}")
    record = state.window_record(row["id"])
    if session.window:
        lines.append(f"- Window: open since {_ago(record and record['started_at'])}"
                     + (f", started for: {record['purpose']}" if record else ""))
    else:
        ended = f", ended {_ago(record['ended_at'])} with exit code {record['exit_code']}" if record and record["ended_at"] else ""
        lines.append(f"- Window: none{ended}. The session does not run now: nothing in it can still be working")
        lines.append(f"- Worker lock: {'held by another process' if workers.is_busy(cfg, row) else 'free'}")
    path = claude.session_file(row["session_id"])
    changed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if path else None
    lines.append(f"- Conversation last changed: {_ago(changed)}")
    lines.append(f"- Your last check: {_ago(row['checked_at'])}")
    text = f"### `{session.name}`: {row['title']} (role `{row['role']}`)\n\n" + "\n".join(lines)
    if session.window:
        screen = "\n".join(tmux.capture(session.window.id).rstrip().splitlines()[-60:])
        text += f"\n\nThe screen:\n\n```text\n{screen or '(blank)'}\n```"
    return text + f"\n\nThe end of the conversation:\n\n```text\n{conversation_tail(path)}\n```"


def conversation_tail(path: Path | None, entries: int = 30, width: int = 400) -> str:
    """The last messages of a Claude Code conversation file, one short line each."""
    if path is None:
        return "(no conversation file)"
    lines = []
    with open(path, errors="replace") as handle:
        for raw in deque(handle, maxlen=500):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind, when = item.get("type"), _clock(item.get("timestamp"))
            if kind == "system" and item.get("subtype") == "turn_duration":
                lines.append(f"{when} (the agent stopped)")
                continue
            if kind not in ("user", "assistant"):
                continue
            content = (item.get("message") or {}).get("content")
            parts = [content] if isinstance(content, str) else [_block(b) for b in content or [] if isinstance(b, dict)]
            text = " ".join(" ".join(p for p in parts if p).split())
            if text:
                lines.append(f"{when} {kind}: {text[:width]}")
    return "\n".join(lines[-entries:]) or "(empty)"


def _clock(stamp) -> str:
    """A conversation file's time, which is UTC, as local time like the other times in the prompt."""
    try:
        return datetime.fromisoformat(str(stamp)).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


def _block(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        return f"[runs {block.get('name')}] {json.dumps(block.get('input'), ensure_ascii=False)}"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return f"[result] {content}"
    return ""


def _act(cfg: Config, state: State, tmux: Tmux, claude: Claude, session: Session, answer: dict, meta: dict,
         screens: dict) -> None:
    """Carry out the AI's pick, after vahtovik checks that it is safe now."""
    label, worker_id = session.label, session.worker["id"]
    action, reason = answer["action"], " ".join(answer["reason"].split())
    row = state.worker(worker_id)
    current = tmux.window(worker_id)
    same_window = bool(session.window and current and current.id == session.window.id)
    if current and current.clients:
        return  # a person came to look
    if action == "press":
        screen = screens.get(answer.get("screen_id") or "")
        if screen is None:
            action, reason = "maintenance", f"a screen that is not on the known list: {reason}"
        elif not same_window:
            return
        else:
            keys = [str(k) for k in screen["keys"]]
            tmux.send_keys(current.id, keys)
            log.info("%s: Watchdog pressed %s on the screen %s: %s", label, " ".join(keys), screen["id"], reason)
            return _debug(cfg, state, row, action, f"vahtovik pressed {' '.join(keys)} on the screen {screen['id']}",
                          reason)
    if action == "none":
        if same_window and row["idle"] and row["status"] in CLOSABLE:
            _close(cfg, state, tmux, current, f"status {row['status']}, checked by the watchdog")
        else:
            log.info("%s: Watchdog found it right: %s", label, reason)
        return
    if action == "nudge":
        if not (same_window and row["idle"] and row["status"] != "maintenance"):
            log.warning("%s: Watchdog did not nudge: the agent is not idle in its window now (%s)", label, reason)
            return
        tmux.send_text(current.id, " ".join(str(meta["nudge"]).split()))
        log.warning("%s: Watchdog nudged the agent: %s", label, reason)
        return _debug(cfg, state, row, action, "vahtovik sent the agent the watchdog's fixed message to check its "
                      "work and report its status", reason)
    if action in ("resume", "retry") and (current or workers.is_busy(cfg, row)):
        log.warning("%s: Watchdog did not %s: the session window is open or the worker is busy (%s)", label, action,
                    reason)
        return
    if action == "resume":
        account = workers.role_of(cfg, row).account
        if not claude.session_exists(row["session_id"], account):
            action, reason = "maintenance", f"the session to resume is gone: {reason}"
        else:
            spec, _ = workers.spec_for(cfg, claude, state, row)
            spec.prompt = " ".join(str(meta["resume"]).split())
            window_id = windows.launch(cfg, state, tmux, claude, spec, "resume")
            log.warning("%s: Watchdog resumed the session in tmux window %s: %s", label, window_id, reason)
            return _debug(cfg, state, row, action, "vahtovik opened the session again with the watchdog's fixed "
                          "message to finish where it stopped", reason)
    if action == "retry":
        turn = state.latest_turn(worker_id)
        if turn is None:
            log.warning("%s: Watchdog did not retry: the worker has no turn (%s)", label, reason)
            return
        error = f"the watchdog asked to repeat the turn: {reason}"
        if turn["finished_at"] is None:
            worker_turn.fail(cfg, state, turn, error, 0)
        else:
            state.retry_turn(turn["id"], error, worker_turn.MAX_ATTEMPTS)
            state.set_status(worker_id, "failed", error)
        log.warning("%s: Watchdog asked to repeat the latest turn: %s", label, reason)
        return _debug(cfg, state, row, action, "vahtovik counted the latest turn as failed, so a later run "
                      "repeats it", reason)
    if action == "maintenance":
        state.set_status(worker_id, "maintenance", reason)
        log.warning("%s: Needs a person: %s", label, reason)
        worker_status.notify_watchers(state, state.worker(worker_id), "maintenance", reason=reason)
        return _send_notice(cfg, state, state.worker(worker_id))  # now, not at the next tick
    if action == "failed":
        state.set_status(worker_id, "failed", reason)
        log.error("%s: Watchdog found the session broken: %s", label, reason)
        return _debug(cfg, state, row, action, "vahtovik set the status to failed", reason)


def _debug(cfg: Config, state: State, row, action: str, what: str, reason: str) -> None:
    """Tell the person what the watchdog did, for now while it is new."""
    name = workers.name(row)
    _send(cfg, state, row, f"{DONE_WHAT[action]}: {row['title']}",
          f"{name}: {what}. The watchdog saw: {reason}\n\nOpen its session: vahtovik open {name}")


def notify(cfg: Config, state: State, tmux: Tmux) -> None:
    """Send a notice about each worker that waits for a person, needs attention, or failed for good, unless a
    person looks at its window. Repeat it every `notify_every`."""
    now = datetime.now(timezone.utc)
    watched = {w.worker for w in tmux.windows() if w.clients}
    for row in state.workers():
        if row["status"] not in ("waiting", "maintenance", "failed") or row["id"] in watched:
            continue
        if row["role"] not in cfg.roles:
            continue
        if row["status"] == "failed" and worker_turn.retries_left(state, row):
            continue  # a later run retries the turn
        last, every = row["notified_at"], cfg.watchdog.notify_every
        if last and (not every or now - datetime.fromisoformat(last) < every):
            continue
        _send_notice(cfg, state, row)


def _send_notice(cfg: Config, state: State, row) -> None:
    name = workers.name(row)
    if row["status"] == "waiting":
        questions = "\n".join(f"- {q}" for q in json.loads(row["questions"] or "[]"))
        subject, body = f"Waiting for your answer: {row['title']}", f"{name} waits for your answer:\n{questions}"
    elif row["status"] == "maintenance":
        subject, body = f"Needs attention: {row['title']}", f"{name} is stuck: {row['status_reason']}"
    else:
        subject = f"Failed: {row['title']}"
        body = f"{name} failed, and vahtovik stopped retrying: {row['status_reason']}"
    _send(cfg, state, row, subject, body + f"\n\nOpen its session: vahtovik open {name}")
    state.set_notified(row["id"])  # only after the send: if it breaks, notify() sends the notice again


def _send(cfg: Config, state: State, row, subject: str, body: str, channels: list[str] | None = None) -> None:
    subject = f"WATCHDOG: {subject}"  # tells notices apart from the agents' own reports
    channels = cfg.notice_channels() if channels is None else channels
    about = workers.label(row) if row is not None else "vahtovik"
    if not channels:
        log.warning("%s: Skipped notice \"%s\": no channel to send it to; see notices in vahtovik.yaml", about,
                    subject)
    for channel in channels:
        delivery.notice(cfg, state, channel, subject, body, row["id"] if row is not None else "",
                        dry_run=os.environ.get("VAHTOVIK_DRY_RUN") == "1")


def warn_long_turns(cfg: Config, state: State, tmux: Tmux) -> None:
    """Warn once about each turn that runs longer than `long_turn_after`: sessions have no time limit."""
    limit = cfg.watchdog.long_turn_after
    if not limit:
        return
    now = datetime.now(timezone.utc)
    for turn in state.unfinished_turns():
        row = state.worker(turn["worker"])
        running = now - datetime.fromisoformat(turn["started_at"])
        if turn["long_warned_at"] or running < limit or row is None or row["role"] not in cfg.roles:
            continue
        state.execute("UPDATE turns SET long_warned_at = ? WHERE id = ?", now.isoformat(timespec="seconds"),
                      turn["id"])
        log.warning("%s: Long turn: running for %s", workers.label(row), duration(running.total_seconds()))
        window = tmux.window(row["id"])
        if window and window.clients:
            continue  # a person is looking at it
        name = workers.name(row)
        _send(cfg, state, row, f"Long turn: {row['title']}",
              f"{name} has been working on one turn for {duration(running.total_seconds())}. It may be fine. "
              f"See it: vahtovik open {name}")


def warn_given_up(cfg: Config, state: State) -> None:
    """Tell the other notice channels about each reply to the person that vahtovik stopped retrying."""
    rows = state.rows("SELECT * FROM deliveries WHERE status = 'failed' AND attempts >= ? AND origin != 'notice' "
                      "AND origin != ? AND given_up_warned_at IS NULL", delivery.MAX_ATTEMPTS, delivery.ACKNOWLEDGMENT)
    for row in rows:
        state.execute("UPDATE deliveries SET given_up_warned_at = ? WHERE id = ?",
                      datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"])
        log.error("%s: Gave up %s %d to %s after %d attempts: %s", delivery.scope(state, row), delivery.kind(row),
                  row["id"], row["channel"], row["attempts"], row["last_error"])
        body = (f"vahtovik could not send its reply {row['id']} to {row['channel']} and stopped retrying after "
                f"{row['attempts']} attempts. Last error: {row['last_error']}\n"
                f"Check the channel: vahtovik test-channel {row['channel']}\n\n"
                f"The reply:\n{row['body'][:2000]}")
        _send(cfg, state, None, f"Reply not delivered: {row['subject'] or row['channel']}", body,
              [c for c in cfg.notice_channels() if c != row["channel"]])
