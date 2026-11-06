"""Operations on groups for the person and the vahtovik agent: the `vahtovik group ...` commands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import groups
from .config import Config, ConfigError, parse_interval
from .stages.group_turn import MAX_ATTEMPTS
from .state import State
from .util import human, utc_iso

log = logging.getLogger(__name__)

OFF = "off"


def _group(cfg: Config, state: State, ref: str):
    watch, key = groups.parse_ref(ref)
    cfg.watch(watch)
    group = state.group(watch, key)
    if group is None:
        raise ConfigError(f"unknown group {ref}; see `vahtovik status --watch {watch}`")
    return watch, key, group


def _interval(value: str, flag: str):
    try:
        return parse_interval(value)
    except ConfigError as exc:
        raise ConfigError(f"{flag}: {exc}") from None


def _done_status(state: State, watch: str, key: str) -> str:
    """The status of a group that is done and has no scheduled turn: `finished`, or `idle` before its first turn."""
    return "finished" if state.latest_turn(watch, key) else "idle"


def set_schedule(cfg: Config, state: State, ref: str, recheck: str | None = None, next_turn: str | None = None,
                 reason: str | None = None) -> list[str]:
    """Change when a group gets turns without new artifacts. Returns what changed, for the person.

    `recheck`: the standing period (`1d`), or `off`. After each `finished` turn the group is `scheduled`
    this long after. `next_turn`: when the next scheduled turn comes (`6h`), or `off` to cancel it."""
    if recheck is None and next_turn is None:
        raise ConfigError("give --recheck, --next or both")
    watch, key, group = _group(cfg, state, ref)
    done = group["status"] in ("idle", "finished", "scheduled")
    lines = []
    if recheck is not None:
        every = None if recheck == OFF else recheck.strip()
        if every:
            _interval(every, "--recheck")
        state.set_recheck(watch, key, every)
        lines.append(f"standing check: every {every}" if every else "standing check: off")
        log.info("%s: Set the standing check: %s", ref, f"every {every}" if every else "off")
        waiting = state.pending_artifacts(watch, key, MAX_ATTEMPTS)
        if every and next_turn is None and not group["scheduled_at"] and done and not waiting:
            next_turn = every  # the period starts now; with new artifacts, it starts after their turn
            reason = reason or f"the group's standing check every {every}"
    if next_turn is not None:
        if next_turn == OFF:
            state.set_schedule(watch, key, None)
            if group["status"] == "scheduled":
                state.set_status(watch, key, _done_status(state, watch, key))
            lines.append("next scheduled turn: none")
            log.info("%s: Cancelled the next scheduled turn", ref)
        else:
            at = datetime.now(timezone.utc) + _interval(next_turn, "--next")
            why = (reason or "").strip() or "a check the person scheduled"
            state.set_schedule(watch, key, utc_iso(at), why)
            if done:
                state.set_status(watch, key, "scheduled", why)
            lines.append(f"next scheduled turn: {human(at)} ({why})")
            if not done:
                lines.append(f"the group is {group['status']} now: the scheduled turn starts once it is done")
            log.info("%s: Scheduled a turn for %s: %s", ref, human(at), why)
    return lines
