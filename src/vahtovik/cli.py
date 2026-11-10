"""vahtovik command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import (channel_guard, costs, delivery, group_session, group_status, journal, operations, skills, timer,
               watchdog, windows)
from .claude_session import Claude
from .config import Config, ConfigError
from .stages import channel_input, group_turn, monitor
from .state import State
from .tmux import Tmux, TmuxError
from .util import LockBusy, duration, file_lock, human, plural, utc_iso

log = logging.getLogger("vahtovik")


def _load(who: str | None = None, echo: bool = False) -> tuple[Config, State]:
    root = config_mod.find_root()
    config_mod.load_env_file(root)
    cfg = config_mod.load(root)
    journal.setup(cfg, who, echo)
    return cfg, State(cfg.db_path)


def _next_monitoring(state: State, watch, source) -> datetime | None:
    """When the source is due: its interval after it was last monitored, or None if it never was."""
    last = state.source_last_run(watch.name, source.id)
    return datetime.fromisoformat(last) + source.interval if last else None


def _due_sources(state: State, watch, now: datetime, force: bool) -> list:
    return [s for s in watch.sources if force or (_next_monitoring(state, watch, s) or now) <= now]


def _watch_next(state: State, watch, now: datetime) -> datetime | None:
    """When the watch's next source is due, or None for a watch without sources."""
    return min(((_next_monitoring(state, watch, s) or now) for s in watch.sources), default=None)


def _nothing_due(state: State, watches, now: datetime, stage: str | None) -> str:
    if stage == "deliver":
        return "no failed reports to retry"
    if stage == "group":
        return "no group turn can start"
    if stage == "input":
        return "no messages to answer"
    if not watches:
        return "no enabled watches"
    nexts = [(w, _watch_next(state, w, now)) for w in watches]
    text = ", ".join(f"{w.name} in {duration((at - now).total_seconds())}" for w, at in nexts if at) or "no sources"
    scheduled = [g for g in (state.next_scheduled(w.name) for w in watches) if g]
    if scheduled:
        first = min(scheduled, key=lambda g: g["scheduled_at"])
        text += f"; next scheduled turn: {first['watch']}/{first['key']} {_left(first['scheduled_at'])}"
    return text


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def cmd_run(args) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg, state = _load(who=f"run {run_id}", echo=sys.stderr.isatty())
    if args.dry_run:
        os.environ["VAHTOVIK_DRY_RUN"] = "1"  # sessions inherit it, so `vahtovik report` dry-runs too
    lock = cfg.data_dir / ".run.lock"
    try:
        with file_lock(lock) as handle:
            handle.truncate(0)
            handle.write(f"{run_id} {time.time():.0f}\n")  # for a run that finds the lock busy
            handle.flush()
            return _run(cfg, state, args, run_id)
    except LockBusy:
        log.warning("Skipped run: %s", _running(lock))
        return 0


def _watchdog(cfg: Config, state: State, tmux: Tmux, claude: Claude, run_id: str) -> None:
    """The watchdog runs at a run's start once `watchdog.every` has passed since it last did."""
    last = state.get_meta("watchdog_at")
    if last and datetime.fromisoformat(last) + cfg.watchdog.every > datetime.now(timezone.utc):
        return
    state.set_meta("watchdog_at", utc_iso())
    watchdog.tick(cfg, state, tmux, claude, run_id)


def _running(lock: Path) -> str:
    """Which run holds the run lock, from what it wrote into the lock file."""
    try:
        run_id, started = lock.read_text().split()
        return f"run {run_id} has been going for {duration(time.time() - float(started))}"
    except (OSError, ValueError):
        return "another run is in progress"


def _run(cfg: Config, state: State, args, run_id: str) -> int:
    tmux, claude = Tmux(cfg), Claude(cfg)
    costs.update(state, claude)  # before Claude Code deletes old conversation files
    _watchdog(cfg, state, tmux, claude, run_id)
    now = datetime.now(timezone.utc)
    watches = [cfg.watch(args.watch)] if args.watch else [w for w in cfg.watches.values() if w.enabled]
    sources = {w.name: _due_sources(state, w, now, args.force) for w in watches} \
        if args.stage in (None, "monitor") else {}
    due = [w for w in watches if sources.get(w.name)]
    # Reading the channels comes before the run's first line: a read that finds nothing writes no line.
    reading = args.stage in (None, "input") and not args.watch
    for channel in channel_input.due(cfg, state, now, args.force) if reading else []:
        channel_input.receive(cfg, state, channel)
    inputs = channel_input.waiting(cfg, state) if reading else []
    # Group turns start at every run, not only when their watch is due: groups left over by an earlier run
    # (claude.parallel_turns, or an open window) start as soon as they can.
    turns_wait = args.stage in (None, "group") and group_turn.can_start(cfg, state, tmux, watches)
    retry = args.stage in (None, "deliver") and bool(state.retryable_deliveries(delivery.MAX_ATTEMPTS))
    if not inputs and not due and not turns_wait and not retry:
        log.info("Nothing due: %s", _nothing_due(state, watches, now, args.stage))
        return 0
    flags = [name for name, on in (("forced", args.force), ("dry run", args.dry_run)) if on]
    flags += [f"stage {args.stage}"] if args.stage else []
    what = ", ".join([*(f"messages in {c.name}" for c in inputs), *(w.name for w in due)]) or (
        "group turns" if turns_wait else "retrying failed reports")
    log.info("Started run: %s%s", what, f" ({', '.join(flags)})" if flags else "")
    started = time.monotonic()
    raw_dir = cfg.runs_dir(run_id)
    new = turns = answered = 0
    state.start_run(run_id)
    status = "ok"
    try:
        # Messages first: what the channel agent changes, such as added artifacts, counts in this run.
        for channel in inputs:
            answered += channel_input.answer(cfg, state, claude, channel, run_id, raw_dir, args.dry_run)
        for watch in watches:
            if watch in due:
                new += monitor.run(cfg, state, claude, watch, run_id, raw_dir, sources[watch.name])
                for source in sources[watch.name] if args.stage is None else []:
                    state.set_source_run(watch.name, source.id)
            if args.stage in (None, "group"):
                turns += len(group_turn.run(cfg, state, claude, tmux, watch, run_id))
        if args.stage in (None, "deliver"):
            delivery.retry_failed(cfg, state, args.dry_run)
    except BaseException as exc:
        status = "failed"
        log.error("Failed run after %s: %s", duration(time.monotonic() - started), _describe(exc))
        raise
    finally:
        state.finish_run(run_id, status)
    # The turns go on in their windows: their results come to the journal as the agents report them.
    done = [plural(new, "new artifact"), f"{plural(turns, 'group turn')} started"]
    done += [f"{plural(answered, 'message')} answered"] if inputs else []
    log.info("Finished run in %s: %s, $%.2f", duration(time.monotonic() - started), ", ".join(done),
             state.run_cost(run_id))
    return 0


def cmd_open(args) -> int:
    cfg, state = _load(who="interactive")
    return group_session.open_session(cfg, state, args.ref, new_session=args.new_session)


def cmd_window(args) -> int:
    data = windows.read_spec(Path(args.spec))
    cfg, state = _load(who=data["who"])
    return windows.run_window(cfg, state, Tmux(cfg), data)


def cmd_report(args) -> int:
    cfg, state = _load()
    watch = args.watch or os.environ.get("VAHTOVIK_WATCH")
    key = args.group or os.environ.get("VAHTOVIK_GROUP")
    if not watch or not key:
        raise ConfigError("give --watch and --group, or run this inside a group session")
    if sys.stdin.isatty():
        raise ConfigError("pipe the message body on stdin")
    turn_id = os.environ.get("VAHTOVIK_TURN")
    origin = "automated" if turn_id else ("interactive" if os.environ.get("VAHTOVIK_GROUP") else "manual")
    row = delivery.report(cfg, state, watch, key, args.channel, args.subject, sys.stdin.read(), origin,
                          turn_id, dry_run=os.environ.get("VAHTOVIK_DRY_RUN") == "1")
    if row["status"] == "sent":
        print(f"sent: delivery {row['id']} via {row['channel']}" + (f" ({row['external_id']})" if row["external_id"] else ""))
        return 0
    print(f"failed: delivery {row['id']} via {row['channel']}: {row['last_error']}. "
          "vahtovik will retry it on the next runs.")
    return 1


def _session_group() -> tuple[str, str]:
    watch, key = os.environ.get("VAHTOVIK_WATCH"), os.environ.get("VAHTOVIK_GROUP")
    if not watch or not key:
        raise ConfigError("run this inside a group session (VAHTOVIK_WATCH and VAHTOVIK_GROUP are not set)")
    return watch, key


def cmd_turn(args) -> int:
    cfg, state = _load()
    watch, key = _session_group()
    times = {}
    for flag in ("check_after", "after"):
        value = getattr(args, flag)
        try:
            times[flag] = config_mod.parse_interval(value) if value else None
        except ConfigError as exc:
            raise group_status.StatusError(f"--{flag.replace('_', '-')}: {exc}") from None
    print(group_status.declare(cfg, state, cfg.watch(watch), key, args.status, args.importance, args.summary,
                               args.question, os.environ.get("VAHTOVIK_TURN"), args.reason, **times))
    return 0


def _print(lines: list[str]) -> int:
    for line in lines:
        print(line)
    return 0


def _stdin_text(what: str) -> str:
    if sys.stdin.isatty():
        raise ConfigError(f"pipe {what} on stdin")
    return sys.stdin.read()


def cmd_group_create(args) -> int:
    cfg, state = _load()
    return _print(operations.create_group(cfg, state, args.ref, args.title))


def cmd_group_merge(args) -> int:
    cfg, state = _load()
    return _print(operations.merge(cfg, state, Tmux(cfg), args.refs, args.into))


def cmd_group_split(args) -> int:
    cfg, state = _load()
    return _print(operations.split(cfg, state, Tmux(cfg), args.ref, args.artifact, args.into, args.title))


def cmd_group_set(args) -> int:
    cfg, state = _load()
    return _print(operations.set_schedule(cfg, state, args.ref, args.recheck, args.next, args.reason))


def cmd_artifact_add(args) -> int:
    cfg, state = _load()
    text = args.json if args.json is not None else _stdin_text("the artifacts as JSON, or give --json")
    return _print(operations.add_artifacts(cfg, state, args.watch, args.group, text))


def cmd_tell(args) -> int:
    cfg, state = _load()
    message = args.message if args.message is not None else _stdin_text("the message, or give --message")
    return _print(operations.tell(cfg, state, Tmux(cfg), args.refs, message))


def cmd_hook(args) -> int:
    """Claude Code runs this for the hooks of group sessions (group_status.hooks), and for the guard of limited
    input channels (channel_guard.hooks)."""
    if args.event == "guard":
        try:
            cfg, _state = _load()
            print(json.dumps(channel_guard.run(cfg, args.channel or "", json.loads(sys.stdin.read() or "{}"))))
            return 0
        except Exception as exc:  # noqa: BLE001 - exit code 2 makes Claude Code deny the tool call
            print(f"vahtovik's guard failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    _cfg, state = _load()
    watch, key = _session_group()
    data = json.loads(sys.stdin.read() or "{}")
    if args.event == "prompt":
        group_status.on_prompt(state, watch, key, data.get("session_id"))
    else:
        answer = group_status.on_stop(state, watch, key, data, os.environ.get("VAHTOVIK_TURN"))
        if answer:
            print(json.dumps(answer))
    return 0


def cmd_status(args) -> int:
    cfg, state = _load()
    costs.update(state, Claude(cfg))
    names = [args.watch] if args.watch else sorted(cfg.watches)
    (_status_details if args.verbose else _status_table)(cfg, state, names)
    return 0


PERIODS = {"24 hours": timedelta(days=1), "30 days": timedelta(days=30)}


def _since(period: timedelta) -> str:
    return utc_iso(datetime.now(timezone.utc) - period)


def _money(costs: dict, test=lambda key: True) -> str:
    return f"${sum(c for key, c in costs.items() if test(key)):.2f}"


def _status_table(cfg: Config, state: State, names: list[str]) -> None:
    """One row per group and a total row per watch, with what the groups cost in the last 24 hours and 30 days.
    Artifacts are the processed ones. Then what the sessions of each kind cost in all watches."""
    group_costs = [state.group_costs(_since(period)) for period in PERIODS.values()]
    rows, all_processed = [], 0
    for name in names:
        watch = cfg.watch(name)
        processed = {r["group_key"]: r["n"] for r in state.rows(
            "SELECT group_key, COUNT(*) AS n FROM artifacts WHERE watch = ? AND status = 'processed' "
            "GROUP BY group_key", name)}
        watch_rows = []
        for g in state.groups(name):
            questions = json.loads(g["questions"] or "[]") if g["status"] == "waiting" else []
            status = g["status"] + (f", {plural(len(questions), 'question')}" if questions else "")
            status += f" ({_left(g['scheduled_at'])})" if g["status"] == "scheduled" and g["scheduled_at"] else ""
            watch_rows.append(["", "", g["key"], status, str(processed.get(g["key"], 0)),
                               *(_money(c, lambda k: k == (name, g["key"])) for c in group_costs)])
        all_processed += sum(processed.values())
        watch_rows.append(["", "", "total" if watch_rows else "-", "", str(sum(processed.values())),
                           *(_money(c, lambda k: k[0] == name) for c in group_costs)])
        watch_rows[0][:2] = f"{name} ({'enabled' if watch.enabled else 'disabled'})", _period(state, watch)
        rows += watch_rows
    failed = state.row("SELECT COUNT(*) AS n FROM deliveries WHERE status != 'sent'")["n"]
    footer = ["all", "", "", plural(failed, "undelivered report"), str(all_processed),
              *(_money(c, lambda k: k[0] in names) for c in group_costs)]
    for line in _table(["watch", "period", "groups", "status", "artifacts", *PERIODS], rows, footer,
                       right=(4, 5, 6)):
        print(line)
    print()
    for line in _inputs(cfg, state):
        print(line)
    for line in _cost_summary(state, dict(PERIODS)):
        print(line)


def _inputs(cfg: Config, state: State, verbose: bool = False) -> list[str]:
    """One line per input channel: how often it is read, when it was last, and its messages; `verbose` adds
    a line with its limits."""
    lines = []
    for channel in channel_input.input_channels(cfg):
        row = state.input_state(channel.name)
        counts = state.message_counts(channel.name)
        read = human(datetime.fromisoformat(row["last_read_at"])) if row and row["last_read_at"] else "never"
        error = f", last error: {row['last_error']}" if row and row["last_error"] and (
            not row["last_ok_at"] or row["last_error_at"] > row["last_ok_at"]) else ""
        lines.append(f"input channel {channel.name}: every {_every(channel.input.interval)}, last read {read}, "
                     f"{counts.get('new', 0)} waiting, {counts.get('answered', 0)} answered, "
                     f"{counts.get('failed', 0)} failed{error}")
        if verbose:
            lines.append(f"  {_limits(channel.input)}")
    return lines + [""] if lines else []


def _limits(settings) -> str:
    """What an input channel's agent may do, and who may write to it."""
    first = settings.rules.splitlines()[0] if settings.rules else ""
    rules = first[:60] + ("..." if len(settings.rules) > len(first[:60]) else "")
    return "; ".join([
        f"limited to {', '.join(settings.commands) or 'no commands'}" if settings.limited else "full power",
        f"watches {', '.join(settings.watches) or 'none'}" if settings.watches is not None else "all watches",
        f"senders {', '.join(settings.senders) or 'none'}" if settings.senders is not None else "any sender",
        f"rules: {rules}" if rules else "no rules of its own"])


def _cost_summary(state: State, periods: dict[str, timedelta | None]) -> list[str]:
    """A table of what group, monitor and watchdog sessions in all watches cost in each period;
    None is all time."""
    since = [_since(period) if period else None for period in periods.values()]
    kinds = {"groups": [state.group_costs(s) for s in since]}
    stages = [state.stage_costs(s) for s in since]
    kinds |= {stage: [{stage: c.get(stage, 0)} for c in stages] for stage in ("monitor", "input", "watchdog")}
    rows = [[kind, *map(_money, costs)] for kind, costs in kinds.items()]
    footer = ["all", *(_money(groups | stage) for groups, stage in zip(kinds["groups"], stages))]
    return _table(["sessions", *periods], rows, footer, right=tuple(range(1, len(periods) + 1)))


def _table(header: list[str], rows: list[list[str]], footer: list[str], right: tuple[int, ...] = ()) -> list[str]:
    """An ASCII table; the columns in `right` hold numbers and are aligned to the right."""
    widths = [max(len(row[i]) for row in (header, *rows, footer)) for i in range(len(header))]
    rule = "-+-".join("-" * width for width in widths)

    def line(row: list[str]) -> str:
        cells = [cell.rjust(width) if i in right else cell.ljust(width)
                 for i, (cell, width) in enumerate(zip(row, widths))]
        return " | ".join(cells).rstrip()

    return [line(header), rule, *map(line, rows), rule, line(footer)]


def _period(state: State, watch) -> str:
    """The intervals of the watch's sources and, if it is enabled, the time left until a run monitors the next
    one: "6h (in 4h12m)", or "1m, 6h (now)"."""
    intervals = sorted({s.interval for s in watch.sources}) or [watch.interval]
    period = ", ".join(map(_every, intervals))
    if not watch.enabled:
        return period
    now = datetime.now(timezone.utc)
    at = _watch_next(state, watch, now)
    left = at - now if at else None
    return f"{period} (in {duration(left.total_seconds())})" if left and left > timedelta(0) else f"{period} (now)"


def _left(at: str) -> str:
    """The time left until a moment: "in 4h12m", or "now" once it has come."""
    left = datetime.fromisoformat(at) - datetime.now(timezone.utc)
    return f"in {duration(left.total_seconds())}" if left > timedelta(0) else "now"


def _every(interval: timedelta) -> str:
    """An interval as watch.yaml writes it: 30m, 6h, 1d."""
    seconds = int(interval.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _artifact_counts(state: State, watch: str, key: str) -> str:
    counts = {r["status"]: r["n"] for r in state.rows(
        "SELECT status, COUNT(*) AS n FROM artifacts WHERE watch = ? AND group_key = ? GROUP BY status", watch, key)}
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))


DETAIL_PERIODS = {**PERIODS, "all time": None}


def _spent(group_costs: list[dict], test) -> str:
    """What the matching groups cost in each period: "$0.40 in 24 hours, $1.10 in 30 days, $3.25 in all time"."""
    return ", ".join(f"{_money(c, test)} in {label}" for c, label in zip(group_costs, DETAIL_PERIODS))


def _status_details(cfg: Config, state: State, names: list[str]) -> None:
    group_costs = [state.group_costs(_since(p) if p else None) for p in DETAIL_PERIODS.values()]
    for name in names:
        watch = cfg.watch(name)
        last = state.watch_last_run(name)
        print(f"{name}: {'enabled' if watch.enabled else 'disabled'}, "
              f"last monitored {human(datetime.fromisoformat(last)) if last else 'never'}, "
              f"group sessions {_spent(group_costs, lambda k: k[0] == name)}")
        for src in watch.sources:
            row = state.row("SELECT * FROM source_state WHERE watch = ? AND source_id = ?", name, src.id)
            error = f", last error: {row['last_error']}" if row and row["last_error"] and (
                not row["last_ok_at"] or row["last_error_at"] > row["last_ok_at"]) else ""
            at = _next_monitoring(state, watch, src)
            print(f"  source {src.id} ({src.skill}, {src.scan} scan): every {_every(src.interval)}, next "
                  f"{human(at) if at and at > datetime.now(timezone.utc) else 'now'}{error}")
        for g in state.groups(name):
            summary = _artifact_counts(state, name, g["key"])
            status = g["status"] + (f": {g['status_reason']}" if g["status_reason"] else "")
            status += f", check after {human(datetime.fromisoformat(g['check_after']))}" if g["check_after"] else ""
            status += (f", next scheduled turn {human(datetime.fromisoformat(g['scheduled_at']))}"
                       if g["scheduled_at"] else "")
            status += f", standing check every {g['recheck_every']}" if g["recheck_every"] else ""
            questions = json.loads(g["questions"] or "[]") if g["status"] == "waiting" else []
            status += f", {plural(len(questions), 'open question')}" if questions else ""
            print(f"  group {g['key']}: {g['title']} | {status} | {summary} | "
                  f"session {g['session_id'] or 'none yet'} | "
                  f"{_spent(group_costs, lambda k: k == (name, g['key']))}")
    failed = state.row("SELECT COUNT(*) AS n FROM deliveries WHERE status != 'sent'")["n"]
    print(f"undelivered reports: {failed}")
    print()
    for line in _inputs(cfg, state, verbose=True):
        print(line)
    for line in _cost_summary(state, DETAIL_PERIODS):
        print(line)


def cmd_check(args) -> int:
    problems: list[str] = []
    try:
        cfg, _state = _load()
    except (ConfigError, OSError) as exc:
        print(f"config: {exc}")
        return 1
    for read in (skills.shared_skill, skills.agent_skills):
        try:
            read(cfg.skills_dir)
        except skills.SkillError as exc:
            problems.append(str(exc))
    for name, channel in cfg.channels.items():
        try:
            skill = skills.load(cfg.skills_dir, "channels", channel.skill)
        except skills.SkillError as exc:
            problems.append(f"channel {name}: {exc}")
            continue
        if not os.access(skill.dir / "send", os.X_OK):
            problems.append(f"channel {name}: {skill.dir / 'send'} is missing or not executable")
        needed = skill.list_meta("required_env") + (skill.list_meta("input_env") if channel.input else [])
        missing = [v for v in needed if v not in channel.env and not os.environ.get(v)]
        if missing:
            problems.append(f"channel {name}: environment variable {', '.join(missing)} not set")
        if channel.input and not os.access(skill.dir / "receive", os.X_OK):
            problems.append(f"channel {name}: it has input, but {skill.dir / 'receive'} is missing or not executable")
    for watch in cfg.watches.values():
        for path in (watch.monitor_md, watch.report_md):
            if not path.is_file():
                problems.append(f"watch {watch.name}: {path} is missing")
        if not watch.process_files():
            problems.append(f"watch {watch.name}: no process/*.md files")
        for src in watch.sources:
            try:
                skill = skills.load(cfg.skills_dir, "sources", src.skill)
            except skills.SkillError as exc:
                problems.append(f"watch {watch.name}, source {src.id}: {exc}")
                continue
            if src.scan == "script" and not os.access(skill.dir / "scan", os.X_OK):
                problems.append(f"watch {watch.name}, source {src.id}: scan is script, but {skill.dir / 'scan'} is "
                                "missing or not executable")
            missing = [p for p in skill.list_meta("required_params") if p not in src.params]
            if missing:
                problems.append(f"watch {watch.name}, source {src.id}: missing params {', '.join(missing)}")
            missing = watch.missing_secrets(src, skill.list_meta("required_env"))
            if missing:
                problems.append(f"watch {watch.name}, source {src.id}: secret {', '.join(missing)} not set; "
                                f"add it to {watch.env_file}")
    binary = shutil.which(cfg.claude.binary)
    if not binary:
        problems.append(f"claude: {cfg.claude.binary} not found on PATH")
    else:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"claude: {version} ({cfg.claude.auth} login)")
    if cfg.claude.auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("claude: auth is api-key but ANTHROPIC_API_KEY is not set")
    if Claude(cfg).trusts(cfg.data_dir) is False:
        problems.append(f"claude: Claude Code does not trust {cfg.data_dir} or a folder above it, so group sessions "
                        f"would stop at its \"trust this folder\" screen; run `claude` once in {cfg.root} and accept")
    if not Tmux.installed():
        problems.append("tmux: not found; group sessions run in tmux windows")
    print(f"watches: {', '.join(cfg.watches) or 'none'} · channels: {', '.join(cfg.channels) or 'none'}")
    for problem in problems:
        print(f"problem: {problem}")
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_test_channel(args) -> int:
    cfg, _state = _load()
    if args.name not in cfg.channels:
        raise ConfigError(f"unknown channel {args.name!r}")
    result = delivery.run_send_script(cfg, args.name, "vahtovik test",
                                      f"Test message from vahtovik, {human()}.", args.dry_run)
    dry = " (dry run)" if args.dry_run else ""
    if result.ok:
        log.info("Sent test message to %s%s", args.name, dry)
    else:
        log.error("Failed test message to %s%s: %s", args.name, dry, result.detail)
    print(("sent" if result.ok else "failed") + (f": {result.detail}" if result.detail else ""))
    return 0 if result.ok else 1


def cmd_log(args) -> int:
    cfg = config_mod.load(config_mod.find_root())
    try:
        # A limited channel's agent sees only the lines about its watches, and its own channel's.
        about = None if cfg.scope is None else [*cfg.scope, *filter(None, [os.environ.get("VAHTOVIK_CHANNEL")])]
        for line in journal.read(journal.path(cfg), args.lines, args.text, follow=args.follow, about=about):
            print(line, flush=args.follow)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:  # the reader quit early, for example `vahtovik log -n 0 | less`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def cmd_install_timer(args) -> int:
    cfg, _state = _load()
    for line in timer.install(cfg, args.every):
        print(line)
    log.info("Installed the systemd user timer: a cycle every %s", args.every)
    return 0


def cmd_uninstall_timer(args) -> int:
    _load()
    lines = timer.uninstall()
    for line in lines:
        print(line)
    if lines[0].startswith("Removed"):
        log.info("Removed the systemd user timer")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vahtovik", description="AI monitoring on Claude Code sessions")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run one monitoring cycle")
    p.add_argument("--watch", help="only this watch")
    p.add_argument("--stage", choices=["input", "monitor", "group", "deliver"], help="only this stage")
    p.add_argument("--force", action="store_true", help="ignore the intervals of sources and input channels")
    p.add_argument("--dry-run", action="store_true", help="channels print instead of sending")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("open", help="open a group's session interactively")
    p.add_argument("ref", help="<watch>/<group-key>")
    p.add_argument("--new-session", action="store_true", help="start a fresh session for the group")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("report", help="send a report (message body on stdin)")
    p.add_argument("--channel", required=True)
    p.add_argument("--subject", default="")
    p.add_argument("--watch", help="default: $VAHTOVIK_WATCH")
    p.add_argument("--group", help="default: $VAHTOVIK_GROUP")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("turn", help="end your turn with your status (for agents in group sessions)")
    p.add_argument("status", choices=["finished", "scheduled", "waiting", "background"],
                   help="finished: you need nothing from a person; scheduled: the same, and the group gets another "
                        "turn after --after, even without new artifacts; waiting: you need an answer or action; "
                        "background: you wait for background work you started, which wakes you when it ends")
    p.add_argument("--importance", choices=group_status.IMPORTANCE, help="required at the end of an automated turn")
    p.add_argument("--summary", help="the current state in one or two sentences; required at the end of an automated turn")
    p.add_argument("--question", action="append", default=[], help="an open question; repeat for each one")
    p.add_argument("--reason", help="background: what you wait for; scheduled: what the next turn should check")
    p.add_argument("--after", help="scheduled: when the group gets its next turn, for example 6h or 1d")
    p.add_argument("--check-after", help="background: when the watchdog should check on you if you are still "
                                         "waiting, for example 30m")
    p.set_defaults(func=cmd_turn)

    p = sub.add_parser("group", help="create, merge and split groups, and schedule their turns")
    group_sub = p.add_subparsers(dest="group_command", required=True)
    g = group_sub.add_parser("create", help="create an empty group")
    g.add_argument("ref", help="<watch>/<group-key>")
    g.add_argument("--title", help="the group's title (default: its key)")
    g.set_defaults(func=cmd_group_create)
    g = group_sub.add_parser("merge", help="merge groups into another group of the same watch")
    g.add_argument("refs", nargs="+", help="the groups to merge, <watch>/<group-key>")
    g.add_argument("--into", required=True, help="the group they go into, <watch>/<group-key>")
    g.set_defaults(func=cmd_group_merge)
    g = group_sub.add_parser("split", help="move some artifacts of a group to another group, new or existing")
    g.add_argument("ref", help="the group they are in, <watch>/<group-key>")
    g.add_argument("--artifact", action="append", default=[], required=True,
                   help="the id of an artifact to move; repeat for each one")
    g.add_argument("--into", required=True, help="the group they go to, <watch>/<group-key>")
    g.add_argument("--title", help="the title of the group they go to, if it is new")
    g.set_defaults(func=cmd_group_split)
    g = group_sub.add_parser("set", help="when a group gets turns without new artifacts")
    g.add_argument("ref", help="<watch>/<group-key>")
    g.add_argument("--recheck", help="a standing period, such as 1d: each finished turn is followed by a scheduled "
                                     "turn this long after; off removes it")
    g.add_argument("--next", help="when the next scheduled turn comes, such as 6h; off cancels it")
    g.add_argument("--reason", help="what the scheduled turn should check")
    g.set_defaults(func=cmd_group_set)

    p = sub.add_parser("artifact", help="add artifacts to groups")
    artifact_sub = p.add_subparsers(dest="artifact_command", required=True)
    a = artifact_sub.add_parser("add", help="add artifacts, as JSON (one object or a list), to groups")
    a.add_argument("watch")
    a.add_argument("--group", action="append", default=[], required=True,
                   help="a group key of the watch to add them to; repeat for each group")
    a.add_argument("--json", help="the artifacts (default: stdin)")
    a.set_defaults(func=cmd_artifact_add)

    p = sub.add_parser("tell", help="pass a message from the person to group sessions")
    p.add_argument("refs", nargs="+", help="the groups, <watch>/<group-key>")
    p.add_argument("--message", help="the message (default: stdin)")
    p.set_defaults(func=cmd_tell)

    p = sub.add_parser("window", help=argparse.SUPPRESS)
    p.add_argument("spec")
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("event", choices=["prompt", "stop", "guard"])
    p.add_argument("channel", nargs="?", help="guard: the limited input channel")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("status", help="show watches, groups and deliveries")
    p.add_argument("--watch")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="all details as text: sources, sessions, status reasons")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="show the journal of what vahtovik did")
    p.add_argument("text", nargs="?", default="",
                   help="only lines that contain this text, for example a watch, a group, a run or a date")
    p.add_argument("-n", "--lines", type=int, default=40, help="how many last lines to show; 0 shows all (default 40)")
    p.add_argument("-f", "--follow", action="store_true", help="keep showing new lines as they are written")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("install-timer", help="start vahtovik periodically with a systemd user timer")
    p.add_argument("--every", default="1m",
                   help="how often to start a cycle; must divide an hour or a day (default 1m)")
    p.set_defaults(func=cmd_install_timer)

    p = sub.add_parser("uninstall-timer", help="remove the systemd user timer")
    p.set_defaults(func=cmd_uninstall_timer)

    p = sub.add_parser("check", help="validate config, skills and environment")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("test-channel", help="send a test message through a channel")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_test_channel)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, delivery.DeliveryError, group_status.StatusError, skills.SkillError,
            timer.TimerError, TmuxError) as exc:
        print(f"vahtovik: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
