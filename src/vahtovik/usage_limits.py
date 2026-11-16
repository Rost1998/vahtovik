"""Warn the person when the Claude plan's usage limits fill up.

At the start of a run, once `usage.every` has passed, vahtovik asks Claude Code how full the plan's limits are
(Claude.plan_usage): the 5-hour limit, the weekly limit for all models, and the weekly limits of single models.
Each limit gets one warning at each percent of `usage.warn_at` that it passes, until it resets. A limit that passes
several percents between two checks gets one warning, at the highest. The warnings go to `usage.channels`, by
default to every input channel that is not limited.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from . import delivery
from .claude_session import Claude, UsageError
from .config import Config
from .state import State
from .util import utc_iso

log = logging.getLogger(__name__)

SUBJECT = "Claude usage"
NAMES = {"session": "the 5-hour limit", "weekly_all": "the weekly limit for all models"}
# Older answers have no `limits` rows: these fields instead, as (kind, model) of the row each stands for.
OLD_FIELDS = {"five_hour": ("session", None), "seven_day": ("weekly_all", None),
              "seven_day_opus": ("weekly_scoped", "Opus"), "seven_day_sonnet": ("weekly_scoped", "Sonnet")}
RESET_MARGIN = timedelta(minutes=1)  # a limit's reset time moves by a second or so from one answer to the next


@dataclass
class Meter:
    key: str  # the same for a limit from check to check: its kind, and the model of a single-model limit
    name: str  # for people, such as "the 5-hour limit"
    percent: float
    resets_at: str | None  # ISO time


def _meter(kind: str, label: str | None, percent: object, resets_at: object) -> Meter | None:
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    if label:
        name = f"the {'weekly ' if kind.startswith('weekly') else ''}{label} limit"
    else:
        name = NAMES.get(kind, f"the {kind.replace('_', ' ')} limit")
    return Meter(f"{kind}:{label}" if label else kind, name, float(percent),
                 resets_at if isinstance(resets_at, str) else None)


def meters(rate_limits: dict) -> list[Meter]:
    """The limits in Claude Code's answer: its `limits` rows, or its older fields when it has no rows."""
    found = []
    rows = rate_limits.get("limits")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
            label = next((part["display_name"] for part in (scope.get("model"), scope.get("surface"))
                          if isinstance(part, dict) and part.get("display_name")), None)
            found.append(_meter(str(row.get("kind") or "usage"), label, row.get("percent"), row.get("resets_at")))
    else:
        for field, (kind, label) in OLD_FIELDS.items():
            window = rate_limits.get(field)
            if isinstance(window, dict):
                found.append(_meter(kind, label, window.get("utilization"), window.get("resets_at")))
    return [m for m in found if m]


def channels(cfg: Config) -> list[str]:
    """Where the warnings go: `usage.channels`, or every input channel that is not limited."""
    if cfg.usage.channels is not None:
        return cfg.usage.channels
    return [c.name for c in cfg.channels.values() if c.input and not c.input.limited]


def check(cfg: Config, state: State, claude: Claude, dry_run: bool = False) -> None:
    """Ask Claude Code for the usage once `usage.every` has passed, and warn about the limits that passed a level."""
    if not cfg.usage.every or cfg.claude.auth != "subscription":
        return
    now = datetime.now(timezone.utc)
    last = state.get_meta("usage_checked_at")
    if last and datetime.fromisoformat(last) + cfg.usage.every > now:
        return
    state.set_meta("usage_checked_at", utc_iso(now))
    try:
        rate_limits = claude.plan_usage()
    except UsageError as exc:
        if not state.get_meta("usage_error"):  # one line until a check works again
            log.error("Failed checking Claude usage: %s", exc)
        state.set_meta("usage_error", str(exc) or "failed")
        return
    if state.get_meta("usage_error"):
        log.info("Checked Claude usage again after a failure")
        state.set_meta("usage_error", "")
    found = meters(rate_limits) if rate_limits else []
    state.set_meta("usage_last", json.dumps({"at": utc_iso(now), "meters": [asdict(m) for m in found]}))
    warnings = _warnings(cfg, state, found, now)
    if not warnings:
        return
    targets = channels(cfg)
    if not targets:
        log.warning("Skipped Claude usage warning: no channel to send it to; see usage.channels")
    for channel in targets:
        delivery.notice(cfg, state, channel, SUBJECT, "\n".join(warnings), dry_run)


def _warnings(cfg: Config, state: State, found: list[Meter], now: datetime) -> list[str]:
    """A line for each limit that passed a level of `usage.warn_at` since its last warning. Keeps the levels."""
    before = json.loads(state.get_meta("usage_warned") or "{}")
    warned, lines = {}, []
    for meter in found:
        level = int((before.get(meter.key) or {}).get("level") or 0)
        reset = _time((before.get(meter.key) or {}).get("resets_at"))
        if level and (meter.percent < level or (reset and reset + RESET_MARGIN < now)):
            level = 0  # the limit has reset since its last warning
        passed = [p for p in cfg.usage.warn_at if level < p <= meter.percent]
        if passed:
            level = passed[-1]
            lines.append(text(meter, level, now))
            log.warning("Claude usage: %s is at %d%%, past %d%%; %s", meter.name, round(meter.percent), level,
                        resets(meter, now))
        warned[meter.key] = {"level": level, "resets_at": meter.resets_at}
    state.set_meta("usage_warned", json.dumps(warned))
    return lines


def text(meter: Meter, level: int, now: datetime) -> str:
    """A warning's line, such as "The 5-hour limit is at 91% (past 90%). It resets today at 18:20, in 3 hours 11
    minutes." """
    name = meter.name[0].upper() + meter.name[1:]
    return f"{name} is at {round(meter.percent)}% (past {level}%). It {resets(meter, now)}."


def resets(meter: Meter, now: datetime) -> str:
    """When the limit resets, in local time: "resets today at 18:20, in 3 hours 11 minutes"."""
    at = _time(meter.resets_at)
    if at is None:
        return "has no reset time"
    local, today = at.astimezone(), now.astimezone().date()
    days = (local.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or local.strftime("on %a %d %b")
    if at <= now:
        return f"reset {day} at {local:%H:%M}"  # for `vahtovik status` after an old check
    return f"resets {day} at {local:%H:%M}, {_wait((at - now).total_seconds())}"


def _wait(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    days, hours, minutes = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if days:
        parts = [(days, "day"), (hours, "hour")]
    elif hours:
        parts = [(hours, "hour"), (minutes, "minute")]
    else:
        return f"in {minutes} minute{'' if minutes == 1 else 's'}"
    return "in " + " ".join(f"{n} {word}{'' if n == 1 else 's'}" for n, word in parts if n)


def _time(value: object) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return at if at is None or at.tzinfo else at.replace(tzinfo=timezone.utc)


def last(state: State) -> tuple[datetime, list[Meter]] | None:
    """The limits at the last check that worked, for `vahtovik status`."""
    raw = state.get_meta("usage_last")
    if not raw:
        return None
    data = json.loads(raw)
    return datetime.fromisoformat(data["at"]), [Meter(**m) for m in data["meters"]]
