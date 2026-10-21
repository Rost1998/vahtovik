"""The watchdog looks after the group sessions: at the start of every run, and while a run waits for a group
turn. Every `watchdog.every` it closes the windows of failed groups, fails the turns whose window is gone, and
sends notices. At the start of every run and every `watchdog.check_every` it asks an AI session to check the
group sessions that need it, and carries out the action the AI picks from a fixed list
(skills/watchdog/SKILL.md). vahtovik checks every pick before it acts, and closes a done window only after
such a check."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import artifacts, delivery, group_session, groups, skills, windows
from .claude_session import Claude, SessionSpec
from .config import Config, ConfigError, Watch
from .stages import group_turn
from .state import State
from .tmux import Tmux, Window
from .util import LockBusy, duration, human, load_schema, plural, render

log = logging.getLogger(__name__)

GRACE = timedelta(seconds=10)  # a window of `vahtovik open` is left alone this long, so the person can attach to it
CLOSABLE = ("finished", "waiting", "idle")  # statuses of an agent that is done, once it is idle
WORKING = ("running", "background")  # statuses the AI checks every time, changed or not
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
DONE_WHAT = {"press": "Pressed keys", "nudge": "Nudged", "resume": "Resumed", "retry": "Retrying",
             "maintenance": "Needs a person", "failed": "Failed"}


def tick(cfg: Config, state: State, tmux: Tmux, claude: Claude | None = None, run_id: str | None = None,
         check_sessions: bool = True) -> None:
    tidy(cfg, state, tmux)
    if check_sessions:
        check(cfg, state, tmux, claude or Claude(cfg), run_id)
    notify(cfg, state, tmux)
    warn_long_turns(cfg, state, tmux)
    warn_given_up_reports(cfg, state)


def tidy(cfg: Config, state: State, tmux: Tmux) -> None:
    """Close the windows of failed groups that nobody looks at; fail the turns whose window is gone."""
    open_refs = set()
    for window in tmux.windows():
        open_refs.add(window.ref)
        watch, key = groups.parse_ref(window.ref)
        group = state.group(watch, key)
        if group is None or window.clients or group["status"] != "failed" or _just_opened(state, watch, key):
            continue
        _close(cfg, state, tmux, window, "status failed")
        open_refs.discard(window.ref)
    for turn in state.unfinished_turns():
        if f"{turn['watch']}/{turn['group_key']}" not in open_refs:
            started = datetime.fromisoformat(turn["started_at"])
            group_turn.fail(state, turn, windows.exit_error(state, turn["watch"], turn["group_key"]),
                            (datetime.now(timezone.utc) - started).total_seconds())


def _close(cfg: Config, state: State, tmux: Tmux, window: Window, why: str) -> None:
    tmux.close(window.id)
    log.info("%s: Closed the session window: %s", window.ref, why)
    watch, key = groups.parse_ref(window.ref)
    deadline = time.monotonic() + 5  # the window's program lets go of the group lock as it ends
    while not _lock_free(cfg, watch, key) and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        artifacts.register_agent_files(cfg, state, cfg.watch(watch), key)
    except ConfigError:  # the watch was removed from the config
        pass


def _just_opened(state: State, watch: str, key: str) -> bool:
    """`vahtovik open` started the window a moment ago: the person may not have attached to it yet."""
    record = state.window_record(watch, key)
    return bool(record and record["purpose"] == "open") and windows.age(state, watch, key) < GRACE


def _time(value: str | None) -> datetime:
    return datetime.fromisoformat(value) if value else EPOCH


def _last_activity(state: State, claude: Claude, watch: str, key: str, group, window: Window | None) -> datetime:
    """The latest change: a prompt or a status, and for an open window also its start and the session's
    conversation file. The end of a window is no change: the watchdog itself ends the windows it checked, and
    Claude Code writes to the conversation file as it ends."""
    times = [_time(group["prompt_at"]), _time(group["status_at"])]
    if window:
        record = state.window_record(watch, key)
        times.append(_time(record and record["started_at"]))
        path = claude.session_file(group["session_id"])
        if path:
            times.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    return max(times)


@dataclass
class Session:
    """A group session the AI checks."""
    watch: Watch
    group: object  # the groups row
    window: Window | None

    @property
    def ref(self) -> str:
        return f"{self.watch.name}/{self.group['key']}"


def sessions_to_check(cfg: Config, state: State, tmux: Tmux, claude: Claude) -> list[Session]:
    """The sessions the AI should check: every working one (`running` or `background`, with a window or
    without one, which means it was interrupted), and every other one that changed since the last check:
    with an open window, or `finished` or `waiting` with a window that ended before a check. Not the ones a
    person looks at, nor the ones that wait for a person (`maintenance`) or are failed."""
    open_windows = {w.ref: w for w in tmux.windows()}
    found = []
    for watch in cfg.watches.values():
        for group in state.groups(watch.name):
            key, status = group["key"], group["status"]
            window = open_windows.get(f"{watch.name}/{key}")
            if window and (window.clients or _just_opened(state, watch.name, key)):
                continue
            if status in ("maintenance", "failed") or (window is None and status == "idle"):
                continue
            # Times are kept to the second: a change in the second of the last check counts as a change.
            changed = _last_activity(state, claude, watch.name, key, group, window) >= _time(group["checked_at"])
            if status in WORKING or changed:
                found.append(Session(watch, group, window))
    return found


def check(cfg: Config, state: State, tmux: Tmux, claude: Claude, run_id: str | None) -> None:
    """Ask the watchdog AI to check the sessions that need it, then carry out its picks."""
    sessions = sessions_to_check(cfg, state, tmux, claude)
    if not sessions:
        return
    skill = skills.watchdog_skill(cfg.skills_dir)
    screens = {str(s["id"]): s for s in skill.meta.get("screens") or []}
    workdir = cfg.data_dir / "watchdog"
    workdir.mkdir(parents=True, exist_ok=True)
    spec = SessionSpec(
        stage="watchdog", workdir=workdir, settings=cfg.stage(None, "watchdog"), schema=load_schema("watchdog"),
        system_prompt=skill.body, mode="throwaway", scope="watchdog",
        prompt=render("watchdog.md", now=human(), example=sessions[0].ref,
                      sessions="\n\n".join(_facts(cfg, state, tmux, claude, s) for s in sessions),
                      screens="\n".join(f"- `{i}`: {s['looks_like']}" for i, s in screens.items()) or "- none"),
    )
    refs = ", ".join(s.ref for s in sessions)
    log.info("Watchdog: Started a check of %s: %s", plural(len(sessions), "session"), refs)
    result = claude.run(spec, cfg.runs_dir(run_id or "watchdog"))
    state.log_session(run_id, "watchdog", "", "watchdog", result)
    if not result.ok:
        log.error("Watchdog: Failed a check after %s: %s", duration(result.duration_s), result.error)
        return
    by_ref = {s.ref: s for s in sessions}
    for answer in result.output["sessions"]:
        if answer["group"] in by_ref:
            session = by_ref.pop(answer["group"])
            state.group_checked(session.watch.name, session.group["key"])  # only an answer counts as a check
            _act(cfg, state, tmux, claude, session, answer, skill.meta, screens)
    log.info("Watchdog: Finished a check in %s, $%.2f", duration(result.duration_s), result.cost_usd)


def _ago(value: str | None) -> str:
    if not value:
        return "never"
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    return f"{human(datetime.fromisoformat(value))} ({duration(abs(seconds))} {'ago' if seconds >= 0 else 'from now'})"


def _lock_free(cfg: Config, watch: str, key: str) -> bool:
    try:
        with groups.lock(cfg.group_dir(watch, key)):
            return True
    except LockBusy:
        return False


def _facts(cfg: Config, state: State, tmux: Tmux, claude: Claude, session: Session) -> str:
    group, watch, key = session.group, session.watch.name, session.group["key"]
    lines = [f"- Status: `{group['status']}` since {_ago(group['status_at'])}"
             + (f": {group['status_reason']}" if group["status_reason"] else "")]
    if group["check_after"]:
        lines.append(f"- Check after: {_ago(group['check_after'])}")
    questions = json.loads(group["questions"] or "[]") if group["status"] == "waiting" else []
    if questions:
        lines.append("- Open questions: " + " / ".join(questions))
    lines.append("- Agent idle (it stopped and waits at its input prompt): " + ("yes" if group["idle"] else "no"))
    lines.append(f"- Last prompt to the agent: {_ago(group['prompt_at'])}")
    turn = state.latest_turn(watch, key)
    if turn:
        how = "not finished" if not turn["finished_at"] else ("finished" if turn["ok"] else f"failed: {turn['error']}")
        lines.append(f"- Latest turn: started {_ago(turn['started_at'])}, {how}")
    record = state.window_record(watch, key)
    if session.window:
        lines.append(f"- Window: open since {_ago(record and record['started_at'])}"
                     + (f", started for: {record['purpose']}" if record else ""))
    else:
        ended = f", ended {_ago(record['ended_at'])} with exit code {record['exit_code']}" if record and record["ended_at"] else ""
        lines.append(f"- Window: none{ended}. The session does not run now: nothing in it can still be working")
        lines.append(f"- Group lock: {'free' if _lock_free(cfg, watch, key) else 'held by another process'}")
    path = claude.session_file(group["session_id"])
    changed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if path else None
    lines.append(f"- Conversation last changed: {_ago(changed)}")
    lines.append(f"- Your last check: {_ago(group['checked_at'])}")
    text = f"### `{session.ref}`: {group['title']}\n\n" + "\n".join(lines)
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
            kind, when = item.get("type"), str(item.get("timestamp", ""))[11:19]
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
    ref, watch, key = session.ref, session.watch.name, session.group["key"]
    action, reason = answer["action"], " ".join(answer["reason"].split())
    group = state.group(watch, key)
    current = tmux.window(ref)
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
            log.info("%s: Watchdog pressed %s on the screen %s: %s", ref, " ".join(keys), screen["id"], reason)
            return _debug(cfg, state, session, action, f"vahtovik pressed {' '.join(keys)} on the screen {screen['id']}", reason)
    if action == "none":
        if same_window and group["idle"] and group["status"] in CLOSABLE:
            _close(cfg, state, tmux, current, f"status {group['status']}, checked by the watchdog")
        else:
            log.info("%s: Watchdog found it right: %s", ref, reason)
        return
    if action == "nudge":
        if not (same_window and group["idle"] and group["status"] != "maintenance"):
            log.warning("%s: Watchdog did not nudge: the agent is not idle in its window now (%s)", ref, reason)
            return
        tmux.send_text(current.id, " ".join(str(meta["nudge"]).split()))
        log.warning("%s: Watchdog nudged the agent: %s", ref, reason)
        return _debug(cfg, state, session, action, "vahtovik sent the agent the watchdog's fixed message to check its "
                      "work and report its status", reason)
    if action in ("resume", "retry") and (current or not _lock_free(cfg, watch, key)):
        log.warning("%s: Watchdog did not %s: the session window is open or the group is busy (%s)", ref, action, reason)
        return
    if action == "resume":
        if not claude.session_exists(group["session_id"]):
            action, reason = "maintenance", f"the session to resume is gone: {reason}"
        else:
            groups.write_claude_md(cfg, state, session.watch, key)
            spec, _ = group_session.spec_for(cfg, claude, state, session.watch, key)
            spec.prompt = " ".join(str(meta["resume"]).split())
            window_id = windows.launch(cfg, state, tmux, claude, spec, "resume")
            log.warning("%s: Watchdog resumed the session in tmux window %s: %s", ref, window_id, reason)
            return _debug(cfg, state, session, action, "vahtovik opened the session again with the watchdog's fixed "
                          "message to finish where it stopped", reason)
    if action == "retry":
        turn = state.latest_turn(watch, key)
        if turn is None:
            log.warning("%s: Watchdog did not retry: the group has no turn (%s)", ref, reason)
            return
        error = f"the watchdog asked to repeat the turn: {reason}"
        if turn["finished_at"] is None:
            group_turn.fail(state, turn, error, 0)
        else:
            state.retry_turn(turn["id"], error)
            state.mark_attempt_failed(watch, json.loads(turn["artifact_ids"] or "[]"), group_turn.MAX_ATTEMPTS)
            state.set_status(watch, key, "failed", error)
        log.warning("%s: Watchdog asked to repeat the latest turn: %s", ref, reason)
        return _debug(cfg, state, session, action, "vahtovik counted the latest turn as failed, so a later run "
                      "repeats it", reason)
    if action == "maintenance":
        state.set_status(watch, key, "maintenance", reason)
        log.warning("%s: Needs a person: %s", ref, reason)
        return _debug(cfg, state, session, action, "vahtovik set the status to maintenance", reason)
    if action == "failed":
        state.set_status(watch, key, "failed", reason)
        log.error("%s: Watchdog found the session broken: %s", ref, reason)
        return _debug(cfg, state, session, action, "vahtovik set the status to failed", reason)


def _debug(cfg: Config, state: State, session: Session, action: str, what: str, reason: str) -> None:
    """Tell the watch's channels what the watchdog did, for now while it is new."""
    _send(cfg, state, session.watch, session.group["key"], session.watch.channels,
          f"{DONE_WHAT[action]}: {session.group['title']}",
          f"{session.ref}: {what}. The watchdog saw: {reason}\n\nOpen its session: vahtovik open {session.ref}",
          prefix="WATCHDOG-DEBUG")


def notify(cfg: Config, state: State, tmux: Tmux) -> None:
    """Send a notice to the watch's channels about each group that waits for a person, needs attention,
    or failed for good, unless a person looks at its window. Repeat it every `notify_every`."""
    now = datetime.now(timezone.utc)
    watched = {w.ref for w in tmux.windows() if w.clients}
    for watch in cfg.watches.values():
        for group in state.groups(watch.name):
            key, status = group["key"], group["status"]
            if status not in ("waiting", "maintenance", "failed") or f"{watch.name}/{key}" in watched:
                continue
            if status == "failed" and state.pending_artifacts(watch.name, key, group_turn.MAX_ATTEMPTS):
                continue  # a later run retries the turn
            last = group["notified_at"]
            if last is None and status == "waiting" and state.reported_since(watch.name, key, group["prompt_at"]):
                last = group["status_at"]  # the agent's own report asked the questions
            every = cfg.watchdog.notify_every
            if last and (not every or now - datetime.fromisoformat(last) < every):
                continue
            _send_notice(cfg, state, watch, group)


def _send_notice(cfg: Config, state: State, watch: Watch, group) -> None:
    key, ref = group["key"], f"{watch.name}/{group['key']}"
    state.set_notified(watch.name, key)
    if group["status"] == "waiting":
        questions = "\n".join(f"- {q}" for q in json.loads(group["questions"] or "[]"))
        subject, body = f"Waiting for your answer: {group['title']}", f"{ref} waits for your answer:\n{questions}"
    elif group["status"] == "maintenance":
        subject, body = f"Needs attention: {group['title']}", f"{ref} is stuck: {group['status_reason']}"
    else:
        subject = f"Failed: {group['title']}"
        body = f"{ref} failed, and vahtovik stopped retrying: {group['status_reason']}"
    _send(cfg, state, watch, key, watch.channels, subject, body + f"\n\nOpen its session: vahtovik open {ref}")


def _send(cfg: Config, state: State, watch: Watch, key: str, channels: list[str], subject: str, body: str,
          prefix: str = "WATCHDOG") -> None:
    subject = f"{prefix}: {subject}"  # tells notices apart from the agents' own reports
    if not channels:
        log.warning("%s/%s: Skipped notice \"%s\": no channel to send it to", watch.name, key, subject)
    for channel in channels:
        delivery.report(cfg, state, watch.name, key, channel, subject, body, "notice",
                        dry_run=os.environ.get("VAHTOVIK_DRY_RUN") == "1")


def warn_long_turns(cfg: Config, state: State, tmux: Tmux) -> None:
    """Warn once about each turn that runs longer than `long_turn_after`: sessions have no time limit."""
    limit = cfg.watchdog.long_turn_after
    if not limit:
        return
    now = datetime.now(timezone.utc)
    for turn in state.unfinished_turns():
        ref = f"{turn['watch']}/{turn['group_key']}"
        running = now - datetime.fromisoformat(turn["started_at"])
        if turn["long_warned_at"] or running < limit or turn["watch"] not in cfg.watches:
            continue
        state.execute("UPDATE group_turns SET long_warned_at = ? WHERE id = ?", now.isoformat(timespec="seconds"),
                      turn["id"])
        log.warning("%s: Long group turn: running for %s", ref, duration(running.total_seconds()))
        window = tmux.window(ref)
        if window and window.clients:
            continue  # a person is looking at it
        title = (state.group(turn["watch"], turn["group_key"]) or {"title": ref})["title"]
        _send(cfg, state, cfg.watches[turn["watch"]], turn["group_key"], cfg.watches[turn["watch"]].channels,
              f"Long turn: {title}", f"{ref} has been working on one turn for {duration(running.total_seconds())}. "
              f"It may be fine. See it: vahtovik open {ref}")


def warn_given_up_reports(cfg: Config, state: State) -> None:
    """Tell the watch's other channels about each report that vahtovik stopped retrying."""
    rows = state.rows("SELECT * FROM deliveries WHERE status = 'failed' AND attempts >= ? AND origin != 'notice' "
                      "AND given_up_warned_at IS NULL", delivery.MAX_ATTEMPTS)
    for row in rows:
        state.execute("UPDATE deliveries SET given_up_warned_at = ? WHERE id = ?",
                      datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"])
        ref = f"{row['watch']}/{row['group_key']}"
        log.error("%s: Gave up report %d to %s after %d attempts: %s", ref, row["id"], row["channel"],
                  row["attempts"], row["last_error"])
        watch = cfg.watches.get(row["watch"])
        if watch is None:
            continue
        body = (f"vahtovik could not send report {row['id']} of {ref} to {row['channel']} and stopped retrying "
                f"after {row['attempts']} attempts. Last error: {row['last_error']}\n"
                f"Check the channel: vahtovik test-channel {row['channel']}\n\n"
                f"The report, \"{row['subject']}\":\n{row['body'][:2000]}")
        _send(cfg, state, watch, row["group_key"], [c for c in watch.channels if c != row["channel"]],
              f"Report not delivered: {row['subject'] or ref}", body)


class Ticker:
    """`tick` at most every `watchdog.every`, and with the AI check at most every `watchdog.check_every`, for
    the loop that waits for a turn. The run's start has just ticked with a check."""

    def __init__(self, cfg: Config, state: State, tmux: Tmux, claude: Claude, run_id: str):
        self.cfg, self.state, self.tmux, self.claude, self.run_id = cfg, state, tmux, claude, run_id
        self.last = self.last_check = time.monotonic()

    def __call__(self) -> None:
        now = time.monotonic()
        if now - self.last < self.cfg.watchdog.every.total_seconds():
            return
        check_sessions = now - self.last_check >= self.cfg.watchdog.check_every.total_seconds()
        self.last = now
        if check_sessions:
            self.last_check = now
        tick(self.cfg, self.state, self.tmux, self.claude, self.run_id, check_sessions)
