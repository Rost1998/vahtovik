"""The watchdog looks after the group sessions: at the start of every run, and every `watchdog.every`
while a run waits for a turn. It closes finished windows, fails turns whose window is gone, and notifies
the watch's channels about groups that wait for a person or need attention."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from . import artifacts, delivery, groups, windows
from .config import Config, ConfigError, Watch
from .stages import group_turn
from .state import State
from .tmux import Tmux

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=1)  # a new window is left alone this long, so a person can attach to it
DONE = ("finished", "waiting", "failed")


def tick(cfg: Config, state: State, tmux: Tmux) -> None:
    tidy(cfg, state, tmux)
    notify(cfg, state, tmux)


def tidy(cfg: Config, state: State, tmux: Tmux) -> None:
    """Close the windows whose agent is done and that nobody looks at; fail the turns whose window is gone."""
    open_refs = set()
    for window in tmux.windows():
        open_refs.add(window.ref)
        watch, key = groups.parse_ref(window.ref)
        group = state.group(watch, key)
        if group is None or window.clients or windows.age(state, watch, key) < GRACE:
            continue
        if group["idle"] and group["status"] in DONE:
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
    body += f"\n\nOpen its session: vahtovik open {ref}"
    if not watch.channels:
        log.warning("%s: Skipped notice \"%s\": the watch has no channels", ref, subject)
    for channel in watch.channels:
        delivery.report(cfg, state, watch.name, key, channel, subject, body, "notice",
                        dry_run=os.environ.get("VAHTOVIK_DRY_RUN") == "1")


class Ticker:
    """`tick` at most every `watchdog.every`, for the loop that waits for a turn."""

    def __init__(self, cfg: Config, state: State, tmux: Tmux):
        self.cfg, self.state, self.tmux = cfg, state, tmux
        self.last = time.monotonic()

    def __call__(self) -> None:
        if time.monotonic() - self.last >= self.cfg.watchdog.every.total_seconds():
            self.last = time.monotonic()
            tick(self.cfg, self.state, self.tmux)
