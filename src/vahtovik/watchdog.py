"""The watchdog looks after the group session windows: at the start of every run, and every minute
while a run waits for a turn."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from . import artifacts, groups, windows
from .config import Config, ConfigError
from .stages import group_turn
from .state import State
from .tmux import Tmux

log = logging.getLogger(__name__)

EVERY_SECONDS = 60
GRACE = timedelta(minutes=1)  # a new window is left alone this long, so a person can attach to it
DONE = ("finished", "waiting", "failed")


def tick(cfg: Config, state: State, tmux: Tmux) -> None:
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


class Ticker:
    """`tick` at most every EVERY_SECONDS, for the loop that waits for a turn."""

    def __init__(self, cfg: Config, state: State, tmux: Tmux):
        self.cfg, self.state, self.tmux = cfg, state, tmux
        self.last = time.monotonic()

    def __call__(self) -> None:
        if time.monotonic() - self.last >= EVERY_SECONDS:
            self.last = time.monotonic()
            tick(self.cfg, self.state, self.tmux)
