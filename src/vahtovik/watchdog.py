"""The watchdog looks after the group sessions: at the start of every run, and every `watchdog.every`
while a run waits for a turn. It closes finished windows, fails turns whose window is gone, asks an AI
session about working sessions that show no activity, and notifies the watch's channels about groups that
wait for a person or need attention."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from . import artifacts, delivery, groups, skills, windows
from .claude_session import Claude, SessionSpec
from .config import Config, ConfigError, Watch
from .stages import group_turn
from .state import State
from .tmux import Tmux, Window
from .util import duration, human, load_schema, plural, render

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=1)  # a new window is left alone this long, so a person can attach to it
DONE = ("finished", "waiting", "failed")


def tick(cfg: Config, state: State, tmux: Tmux, claude: Claude | None = None, run_id: str | None = None) -> None:
    tidy(cfg, state, tmux)
    look(cfg, state, tmux, claude or Claude(cfg), run_id)
    notify(cfg, state, tmux)
    warn_long_turns(cfg, state, tmux)
    warn_given_up_reports(cfg, state)


def tidy(cfg: Config, state: State, tmux: Tmux) -> None:
    """Close the windows whose agent is done and that nobody looks at; fail the turns whose window is gone."""
    open_refs = set()
    for window in tmux.windows():
        open_refs.add(window.ref)
        watch, key = groups.parse_ref(window.ref)
        group = state.group(watch, key)
        if group is None or window.clients or windows.age(state, watch, key) < GRACE:
            continue
        if group["status"] == "failed" or (group["idle"] and group["status"] in DONE):
            tmux.close(window.id)
            open_refs.discard(window.ref)
            log.info("%s: Closed the session window: status %s", window.ref, group["status"])
            try:
                artifacts.register_agent_files(cfg, state, cfg.watch(watch), key)
            except ConfigError:  # the watch was removed from the config
                pass
    for turn in state.unfinished_turns():
        if f"{turn['watch']}/{turn['group_key']}" not in open_refs:
            started = datetime.fromisoformat(turn["started_at"])
            group_turn.fail(state, turn, windows.exit_error(state, turn["watch"], turn["group_key"]),
                            (datetime.now(timezone.utc) - started).total_seconds())


def _last_activity(state: State, claude: Claude, watch: str, key: str, group) -> datetime:
    """The latest sign of life: the window's start, a prompt or status, the previous look, or a change
    of the session's conversation file. (The screen is no sign: a status line may change by itself.)"""
    record = state.window_record(watch, key)
    times = [datetime.fromisoformat(t) for t in (group["prompt_at"], group["status_at"],
                                                 record and record["started_at"], record and record["checked_at"]) if t]
    path = claude.session_file(group["session_id"])
    if path:
        times.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    return max(times, default=datetime.now(timezone.utc))


def look(cfg: Config, state: State, tmux: Tmux, claude: Claude, run_id: str | None) -> None:
    """Ask the watchdog AI about the working sessions that showed no activity for `stale_after`."""
    now = datetime.now(timezone.utc)
    quiet: dict[str, tuple[Window, timedelta]] = {}
    for window in tmux.windows():
        watch, key = groups.parse_ref(window.ref)
        group = state.group(watch, key)
        if window.clients or group is None or group["status"] != "running":
            continue
        idle_for = now - _last_activity(state, claude, watch, key, group)
        if idle_for >= cfg.watchdog.stale_after:
            quiet[window.ref] = (window, idle_for)
    if not quiet:
        return
    skill = skills.watchdog_skill(cfg.skills_dir)
    screens = {str(s["id"]): s for s in skill.meta.get("screens") or []}
    sessions = []
    for ref, (window, idle_for) in quiet.items():
        screen = "\n".join(tmux.capture(window.id).rstrip().splitlines()[-80:])
        sessions.append(f"### `{ref}`\n\nNo activity for {duration(idle_for.total_seconds())}. "
                        f"The screen:\n\n```text\n{screen}\n```")
        state.window_checked(*groups.parse_ref(ref))
    workdir = cfg.data_dir / "watchdog"
    workdir.mkdir(parents=True, exist_ok=True)
    spec = SessionSpec(
        stage="watchdog", workdir=workdir, settings=cfg.stage(None, "watchdog"), schema=load_schema("watchdog"),
        system_prompt=skill.body, mode="throwaway", scope="watchdog",
        prompt=render("watchdog.md", now=human(), example=next(iter(quiet)), sessions="\n\n".join(sessions),
                      screens="\n".join(f"- `{i}`: {s['looks_like']}" for i, s in screens.items()) or "- none"),
    )
    log.info("Watchdog: Started a look at %s: %s", plural(len(quiet), "quiet session"), ", ".join(quiet))
    result = claude.run(spec, cfg.runs_dir(run_id or "watchdog"))
    state.log_session(run_id, "watchdog", "", "watchdog", result)
    if not result.ok:
        log.error("Watchdog: Failed a look after %s: %s", duration(result.duration_s), result.error)
        return
    for answer in result.output["sessions"]:
        if answer["group"] in quiet:
            _act(state, tmux, quiet[answer["group"]][0], answer, screens)
    log.info("Watchdog: Finished a look in %s, $%.2f", duration(result.duration_s), result.cost_usd)


def _act(state: State, tmux: Tmux, window: Window, answer: dict, screens: dict) -> None:
    watch, key = groups.parse_ref(window.ref)
    action, reason = answer["action"], " ".join(answer["reason"].split())
    if action == "press":
        screen = screens.get(answer.get("screen_id") or "")
        current = tmux.window(window.ref)
        if screen is None:
            action, reason = "maintenance", f"a screen that is not on the known list: {reason}"
        elif current is None or current.id != window.id or current.clients:
            return  # it closed, or a person came to look
        else:
            keys = [str(k) for k in screen["keys"]]
            tmux.send_keys(window.id, keys)
            log.info("%s: Watchdog pressed %s on the screen %s: %s", window.ref, " ".join(keys), screen["id"], reason)
            return
    if action == "maintenance":
        state.set_status(watch, key, "maintenance", reason)
        log.warning("%s: Needs a person: %s", window.ref, reason)
    elif action == "failed":
        state.set_status(watch, key, "failed", reason)
        log.error("%s: Watchdog found the session broken: %s", window.ref, reason)
    else:
        log.info("%s: Watchdog found it still working: %s", window.ref, reason)


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


def _send(cfg: Config, state: State, watch: Watch, key: str, channels: list[str], subject: str, body: str) -> None:
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
    """`tick` at most every `watchdog.every`, for the loop that waits for a turn."""

    def __init__(self, cfg: Config, state: State, tmux: Tmux, claude: Claude, run_id: str):
        self.cfg, self.state, self.tmux, self.claude, self.run_id = cfg, state, tmux, claude, run_id
        self.last = time.monotonic()

    def __call__(self) -> None:
        if time.monotonic() - self.last >= self.cfg.watchdog.every.total_seconds():
            self.last = time.monotonic()
            tick(self.cfg, self.state, self.tmux, self.claude, self.run_id)
