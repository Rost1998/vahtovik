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
from . import (channel_guard, costs, delivery, journal, knowledge, memory, operations, timer, usage_limits,
               watchdog, windows, worker_status, workers)
from .claude_session import Claude
from .config import Config, ConfigError
from .stages import channel_input, monitor, worker_turn
from .state import State
from .tmux import Tmux, TmuxError
from .util import LockBusy, duration, file_lock, human, plural, utc_iso

log = logging.getLogger("vahtovik")


def _load(who: str | None = None, echo: bool = False) -> tuple[Config, State]:
    cfg = config_mod.load(config_mod.find_root())
    config_mod.load_env_file(cfg)
    journal.setup(cfg, who, echo)
    return cfg, State(cfg.db_path)


def _next_monitoring(state: State, poller, source) -> datetime | None:
    """When the source is due after it was last monitored (Source.next_run), or None if it never was."""
    last = state.source_last_run(poller.name, source.id)
    return source.next_run(datetime.fromisoformat(last)) if last else None


def _due_sources(state: State, poller, now: datetime, force: bool) -> list:
    return [s for s in poller.sources if force or (_next_monitoring(state, poller, s) or now) <= now]


def _poller_next(state: State, poller, now: datetime) -> datetime | None:
    """When the poller's next source is due, or None for a poller without sources."""
    return min(((_next_monitoring(state, poller, s) or now) for s in poller.sources), default=None)


def _nothing_due(cfg: Config, state: State, pollers, now: datetime, stage: str | None, roles) -> str:
    if stage == "deliver":
        return "no failed messages to retry"
    if stage == "workers":
        return "no worker turn can start"
    if stage == "input":
        return "no messages to answer"
    nexts = [(p, _poller_next(state, p, now)) for p in pollers]
    text = ", ".join(f"{p.name} in {duration((at - now).total_seconds())}" for p, at in nexts if at) or (
        "no enabled pollers" if not pollers else "no sources")
    first = state.next_scheduled(roles)
    if first:
        text += f"; next scheduled turn: {workers.name(first)} {_left(first['scheduled_at'])}"
    return text


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def cmd_run(args) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg, state = _load(who=f"run {run_id}", echo=sys.stderr.isatty())
    if args.dry_run:
        os.environ["VAHTOVIK_DRY_RUN"] = "1"  # the channel scripts that sessions run dry-run too
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
    usage_limits.check(cfg, state, claude, args.dry_run)  # once usage.every has passed
    now = datetime.now(timezone.utc)
    pollers = [cfg.poller(args.poller)] if args.poller else [p for p in cfg.pollers.values() if p.enabled]
    roles = list(pollers[0].roles) if args.poller else None  # --poller: only the turns of its roles' workers
    sources = {p.name: _due_sources(state, p, now, args.force) for p in pollers} \
        if args.stage in (None, "monitor") else {}
    due = [p for p in pollers if sources.get(p.name)]
    # Reading the channels comes before the run's first line: a read that finds nothing writes no line.
    reading = args.stage in (None, "input") and not args.poller
    for channel in channel_input.due(cfg, state, now, args.force) if reading else []:
        channel_input.receive(cfg, state, channel)
    inputs = channel_input.waiting(cfg, state) if reading else []
    # Turns start at every run, not only when a poller is due: workers left over by an earlier run
    # (claude.parallel_turns, or an open window) start as soon as they can.
    turns_wait = args.stage in (None, "workers") and worker_turn.can_start(cfg, state, tmux, roles)
    retry = args.stage in (None, "deliver") and bool(state.retryable_deliveries(delivery.MAX_ATTEMPTS))
    if not inputs and not due and not turns_wait and not retry:
        log.info("Nothing due: %s", _nothing_due(cfg, state, pollers, now, args.stage, roles))
        return 0
    flags = [name for name, on in (("forced", args.force), ("dry run", args.dry_run)) if on]
    flags += [f"stage {args.stage}"] if args.stage else []
    what = ", ".join([*(f"messages in {c.name}" for c in inputs), *(p.name for p in due)]) or (
        "worker turns" if turns_wait else "retrying failed messages")
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
        for poller in due:
            new += monitor.run(cfg, state, claude, poller, run_id, raw_dir, sources[poller.name])
            for source in sources[poller.name] if args.stage is None else []:
                state.set_source_run(poller.name, source.id)
        if args.stage in (None, "workers"):
            turns += len(worker_turn.run(cfg, state, claude, tmux, run_id, roles))
        if args.stage in (None, "deliver"):
            delivery.retry_failed(cfg, state, args.dry_run)
    except BaseException as exc:
        status = "failed"
        log.error("Failed run after %s: %s", duration(time.monotonic() - started), _describe(exc))
        raise
    finally:
        state.finish_run(run_id, status)
    # The turns go on in their windows: their results come to the journal as the agents report them.
    done = [plural(new, "new artifact"), f"{plural(turns, 'worker turn')} started"]
    done += [f"{plural(answered, 'message')} answered"] if inputs else []
    log.info("Finished run in %s: %s, $%.2f", duration(time.monotonic() - started), ", ".join(done),
             state.run_cost(run_id))
    return 0


def cmd_open(args) -> int:
    cfg, state = _load(who="interactive")
    return workers.open_session(cfg, state, args.ref, new_session=args.new_session)


def cmd_window(args) -> int:
    data = windows.read_spec(Path(args.spec))
    cfg, state = _load(who=data["who"])
    return windows.run_window(cfg, state, Tmux(cfg), data)


def _session_worker(state: State):
    worker_id = os.environ.get("VAHTOVIK_WORKER")
    if not worker_id:
        raise ConfigError("run this inside a worker session (VAHTOVIK_WORKER is not set)")
    row = state.worker(worker_id)
    if row is None:
        raise ConfigError(f"unknown worker {worker_id}")
    return row


def cmd_turn(args) -> int:
    cfg, state = _load()
    row = _session_worker(state)
    times = {}
    for flag in ("check_after", "after"):
        value = getattr(args, flag)
        try:
            times[flag] = config_mod.parse_interval(value) if value else None
        except ConfigError as exc:
            raise worker_status.StatusError(f"--{flag.replace('_', '-')}: {exc}") from None
    try:
        times["at"] = config_mod.parse_moment(args.at) if args.at else None
    except ConfigError as exc:
        raise worker_status.StatusError(f"--at: {exc}") from None
    print(worker_status.declare(cfg, state, row, args.status, args.importance, args.summary, args.question,
                                os.environ.get("VAHTOVIK_TURN"), args.reason, **times))
    return 0


def _print(lines: list[str]) -> int:
    for line in lines:
        print(line)
    return 0


def _stdin_text(what: str) -> str:
    if sys.stdin.isatty():
        raise ConfigError(f"pipe {what} on stdin")
    return sys.stdin.read()


def cmd_worker_spawn(args) -> int:
    cfg, state = _load()
    return _print(operations.spawn(cfg, state, args.ref, args.skill, args.title, args.message, args.watch))


def cmd_worker_watch(args) -> int:
    cfg, state = _load()
    return _print(operations.watch(cfg, state, args.refs, args.watcher))


def cmd_worker_unwatch(args) -> int:
    cfg, state = _load()
    return _print(operations.unwatch(cfg, state, args.refs, args.watcher))


def cmd_worker_list(args) -> int:
    cfg, state = _load()
    return _print(operations.list_workers(cfg, state, args.role, args.status, args.tree, args.all))


def cmd_worker_show(args) -> int:
    cfg, state = _load()
    return _print(operations.show(cfg, state, args.ref))


def cmd_worker_merge(args) -> int:
    cfg, state = _load()
    return _print(operations.merge(cfg, state, Tmux(cfg), args.refs, args.into))


def cmd_worker_split(args) -> int:
    cfg, state = _load()
    return _print(operations.split(cfg, state, Tmux(cfg), args.ref, args.artifact, args.into, args.title))


def cmd_worker_schedule(args) -> int:
    cfg, state = _load()
    return _print(operations.schedule(cfg, state, args.ref, args.after, args.at, args.off, args.reason))


def cmd_worker_archive(args) -> int:
    cfg, state = _load()
    return _print(operations.archive(cfg, state, Tmux(cfg), args.ref, args.reason))


def cmd_artifact_add(args) -> int:
    cfg, state = _load()
    text = args.json if args.json is not None else _stdin_text("the artifacts as JSON, or give --json")
    return _print(operations.add_artifacts(cfg, state, args.worker, text))


def cmd_tell(args) -> int:
    cfg, state = _load()
    message = args.message if args.message is not None else _stdin_text("the message, or give --message")
    return _print(operations.tell(cfg, state, Tmux(cfg), args.refs, message))


def cmd_memory_add(args) -> int:
    cfg, _state = _load()
    text = args.text if args.text is not None else _stdin_text("the text, or give --text")
    return _print(memory.add(cfg, args.role, text))


def cmd_memory_remove(args) -> int:
    cfg, _state = _load()
    return _print(memory.remove(cfg, args.role, args.entry, args.reason))


def cmd_hook(args) -> int:
    """Claude Code runs this for the hooks of worker sessions (worker_status.hooks), and for the guard of limited
    input channels (channel_guard.hooks)."""
    if args.event == "guard":
        try:
            cfg, _state = _load()
            print(json.dumps(channel_guard.run(cfg, args.channel or "", json.loads(sys.stdin.read() or "{}"))))
            return 0
        except Exception as exc:  # noqa: BLE001 - exit code 2 makes Claude Code deny the tool call
            print(f"vahtovik's guard failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    cfg, state = _load()
    row = _session_worker(state)
    data = json.loads(sys.stdin.read() or "{}")
    if args.event == "compact":
        print(worker_status.on_compact(cfg, row))
    elif args.event == "prompt":
        worker_status.on_prompt(state, row["id"], data.get("session_id"))
    else:
        answer = worker_status.on_stop(state, row["id"], data, os.environ.get("VAHTOVIK_TURN"))
        if answer:
            print(json.dumps(answer))
    return 0


def cmd_status(args) -> int:
    cfg, state = _load()
    costs.update(state, Claude(cfg))
    if args.role:
        cfg.role(args.role)
    roles = [args.role] if args.role else sorted(cfg.roles)
    (_status_details if args.verbose else _status_table)(cfg, state, roles)
    return 0


PERIODS = {"24 hours": timedelta(days=1), "30 days": timedelta(days=30)}


def _since(period: timedelta) -> str:
    return utc_iso(datetime.now(timezone.utc) - period)


def _money(costs: dict, test=lambda key: True) -> str:
    return f"${sum(c for key, c in costs.items() if test(key)):.2f}"


def _role_of_workers(state: State) -> dict[str, str]:
    """The role of every worker, archived ones too: their costs count for their role."""
    return {r["id"]: r["role"] for r in state.workers(archived=True)}


def _processed(state: State) -> dict[str, int]:
    return {r["worker"]: r["n"] for r in state.rows(
        "SELECT worker, COUNT(*) AS n FROM artifacts WHERE status = 'processed' GROUP BY worker")}


def _short_status(row) -> str:
    status = row["status"]
    if status == "waiting":
        status += f", {plural(len(json.loads(row['questions'] or '[]')), 'question')}"
    if status == "scheduled" and row["scheduled_at"]:
        status += f" ({_left(row['scheduled_at'])})"
    return status


def _status_table(cfg: Config, state: State, roles: list[str]) -> None:
    """A table of the pollers, then one row per worker and a total row per role, with what the workers cost in the
    last 24 hours and 30 days. Artifacts are the processed ones. Then the input channels, the usage limits, and
    what the sessions of each kind cost."""
    pollers = [p for p in cfg.pollers.values() if set(p.roles) & set(roles)] if roles != sorted(cfg.roles) \
        else list(cfg.pollers.values())
    rows = [[p.name, "enabled" if p.enabled else "disabled", _period(state, p), ", ".join(p.roles)] for p in pollers]
    if rows:
        for line in _table(["poller", "state", "schedule", "roles"], rows, None):
            print(line)
        print()
    worker_costs = [state.worker_costs(_since(period)) for period in PERIODS.values()]
    role_of = _role_of_workers(state)
    processed = _processed(state)
    rows, total = [], 0
    for name in roles:
        role_rows = []
        for row in state.workers([name]):
            parent = row["parent"] or ""
            role_rows.append(["", workers.name(row), _short_status(row), parent, str(processed.get(row["id"], 0)),
                              *(_money(c, lambda k, i=row["id"]: k == i) for c in worker_costs)])
        count = sum(n for worker_id, n in processed.items() if role_of.get(worker_id) == name)
        total += count
        role_rows.append(["", "total" if role_rows else "-", "", "", str(count),
                          *(_money(c, lambda k, r=name: role_of.get(k) == r) for c in worker_costs)])
        role_rows[0][0] = name
        rows += role_rows
    footer = ["all", "", plural(state.undelivered(), "undelivered message"), "", str(total),
              *(_money(c, lambda k: role_of.get(k) in roles) for c in worker_costs)]
    for line in _table(["role", "workers", "status", "parent", "artifacts", *PERIODS], rows, footer,
                       right=(4, 5, 6)):
        print(line)
    print()
    for line in _inputs(cfg, state) + _usage(cfg, state):
        print(line)
    for line in _cost_summary(state, dict(PERIODS)):
        print(line)


def _inputs(cfg: Config, state: State, verbose: bool = False) -> list[str]:
    """One line per input channel: how often it is read, when a new session starts, when it was last read, and
    its messages; `verbose` adds a line with its limits."""
    lines = []
    for channel in channel_input.input_channels(cfg):
        row = state.input_state(channel.name)
        counts = state.message_counts(channel.name)
        read = human(datetime.fromisoformat(row["last_read_at"])) if row and row["last_read_at"] else "never"
        error = f", last error: {row['last_error']}" if row and row["last_error"] and (
            not row["last_ok_at"] or row["last_error_at"] > row["last_ok_at"]) else ""
        after = channel.input.new_session_after
        session = f"new session after {_every(after)} quiet" if after else "one session until you clear it"
        session += ", its last reply waits for an answer" if row and row["awaits_answer"] else ""
        lines.append(f"input channel {channel.name}: every {_every(channel.input.interval)}, {session}, "
                     f"last read {read}, {counts.get('new', 0)} waiting, {counts.get('answered', 0)} answered, "
                     f"{counts.get('failed', 0)} failed{error}")
        if verbose:
            lines.append(f"  {_limits(channel.input)}")
    return lines + [""] if lines else []


def _usage(cfg: Config, state: State) -> list[str]:
    """The usage limits of each Claude login at its last check, and the error of a check that fails now."""
    lines, now = [], datetime.now(timezone.utc)
    warn = ", ".join(map(str, cfg.usage.warn_at)) if cfg.usage.every else "off"
    for account in usage_limits.logins(cfg):
        last, of = usage_limits.last(state, account), f" of account {account}" if account else ""
        if last and last[1]:
            lines.append(f"Claude usage{of} at {human(last[0])} (warnings at: {warn}):")
            lines += [f"  {m.name.removeprefix('the ')}: {round(m.percent)}%, {usage_limits.resets(m, now)}"
                      for m in last[1]]
        error = usage_limits.failure(state, account)
        if error:
            lines.append(f"Claude usage{of}: the last check failed: {error}")
    return lines + [""] if lines else []


def _limits(settings) -> str:
    """What an input channel's agent may do, and who may write to it."""
    first = settings.rules.splitlines()[0] if settings.rules else ""
    rules = first[:60] + ("..." if len(settings.rules) > len(first[:60]) else "")
    return "; ".join([
        f"limited to {', '.join(settings.commands) or 'no commands'}" if settings.limited else "full power",
        f"roles {', '.join(settings.roles) or 'none'}" if settings.roles is not None else "all roles",
        f"senders {', '.join(settings.senders) or 'none'}" if settings.senders is not None else "any sender",
        f"rules: {rules}" if rules else "no rules of its own"])


def _cost_summary(state: State, periods: dict[str, timedelta | None]) -> list[str]:
    """A table of what worker, monitoring, input and watchdog sessions cost in each period; None is all time."""
    since = [_since(period) if period else None for period in periods.values()]
    kinds = {"workers": [state.worker_costs(s) for s in since]}
    stages = [state.stage_costs(s) for s in since]
    kinds |= {stage: [{stage: c.get(stage, 0)} for c in stages] for stage in ("monitor", "input", "watchdog")}
    rows = [[kind, *map(_money, costs)] for kind, costs in kinds.items()]
    footer = ["all", *(_money(own | stage) for own, stage in zip(kinds["workers"], stages))]
    return _table(["sessions", *periods], rows, footer, right=tuple(range(1, len(periods) + 1)))


def _table(header: list[str], rows: list[list[str]], footer: list[str] | None,
           right: tuple[int, ...] = ()) -> list[str]:
    """An ASCII table; the columns in `right` hold numbers and are aligned to the right."""
    widths = [max(len(row[i]) for row in (header, *rows, *([footer] if footer else []))) for i in range(len(header))]
    rule = "-+-".join("-" * width for width in widths)

    def line(row: list[str]) -> str:
        cells = [cell.rjust(width) if i in right else cell.ljust(width)
                 for i, (cell, width) in enumerate(zip(row, widths))]
        return " | ".join(cells).rstrip()

    return [line(header), rule, *map(line, rows), *([rule, line(footer)] if footer else [])]


def _period(state: State, poller) -> str:
    """The schedules of the poller's sources and, if it is enabled, the time left until a run monitors the next
    one: "6h (in 4h12m)", "1m; 6h (now)" or "at 08:00, 20:00 (in 3h05m)"."""
    items = sorted(poller.sources or [poller], key=lambda s: (bool(s.at), s.interval, s.at))
    period = "; ".join(dict.fromkeys(map(_schedule, items)))
    if not poller.enabled:
        return period
    now = datetime.now(timezone.utc)
    at = _poller_next(state, poller, now)
    left = at - now if at else None
    return f"{period} (in {duration(left.total_seconds())})" if left and left > timedelta(0) else f"{period} (now)"


def _left(at: str) -> str:
    """The time left until a moment: "in 4h12m", or "now" once it has come."""
    left = datetime.fromisoformat(at) - datetime.now(timezone.utc)
    return f"in {duration(left.total_seconds())}" if left > timedelta(0) else "now"


def _schedule(item) -> str:
    """When a source, or a poller's sources, are monitored, as poller.yaml sets it: "6h", or "at 08:00, 20:00"."""
    return "at " + ", ".join(t.strftime("%H:%M") for t in item.at) if item.at else _every(item.interval)


def _every(interval: timedelta) -> str:
    """An interval as poller.yaml writes it: 30m, 6h, 1d."""
    seconds = int(interval.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _counts(state: State, table: str, worker_id: str) -> str:
    counts = {r["status"]: r["n"] for r in state.rows(
        f"SELECT status, COUNT(*) AS n FROM {table} WHERE worker = ? GROUP BY status", worker_id)}
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"


DETAIL_PERIODS = {**PERIODS, "all time": None}


def _spent(worker_costs: list[dict], test) -> str:
    """What the matching workers cost in each period: "$0.40 in 24 hours, $1.10 in 30 days, $3.25 in all time"."""
    return ", ".join(f"{_money(c, test)} in {label}" for c, label in zip(worker_costs, DETAIL_PERIODS))


def _status_details(cfg: Config, state: State, roles: list[str]) -> None:
    worker_costs = [state.worker_costs(_since(p) if p else None) for p in DETAIL_PERIODS.values()]
    role_of = _role_of_workers(state)
    for poller in cfg.pollers.values():
        if not set(poller.roles) & set(roles):
            continue
        last = state.poller_last_run(poller.name)
        print(f"poller {poller.name}: {'enabled' if poller.enabled else 'disabled'}, roles {', '.join(poller.roles)}, "
              + (f"account {poller.account}, " if poller.account else "")
              + f"last monitored {human(datetime.fromisoformat(last)) if last else 'never'}")
        for src in poller.sources:
            row = state.source_state(poller.name, src.id)
            error = f", last error: {row['last_error']}" if row and row["last_error"] and (
                not row["last_ok_at"] or row["last_error_at"] > row["last_ok_at"]) else ""
            at = _next_monitoring(state, poller, src)
            when = _schedule(src) if src.at else f"every {_schedule(src)}"
            print(f"  source {src.id} ({src.skill}, {src.scan} scan): {when}, next "
                  f"{human(at) if at and at > datetime.now(timezone.utc) else 'now'}{error}")
    for name in roles:
        role = cfg.roles[name]
        print(f"role {name}: skills {', '.join(role.skills) or 'shared only'}"
              + (f", account {role.account}" if role.account else "")
              + f", workers {_spent(worker_costs, lambda k, r=name: role_of.get(k) == r)}")
        for row in state.workers([name]):
            status = row["status"] + (f": {row['status_reason']}" if row["status_reason"] else "")
            status += f", check after {human(datetime.fromisoformat(row['check_after']))}" if row["check_after"] else ""
            status += (f", next scheduled turn {human(datetime.fromisoformat(row['scheduled_at']))}"
                       if row["scheduled_at"] else "")
            questions = json.loads(row["questions"] or "[]") if row["status"] == "waiting" else []
            status += f", {plural(len(questions), 'open question')}" if questions else ""
            parent = state.worker(row["parent"]) if row["parent"] else None
            print(f"  worker {workers.name(row)}: {row['title']} | {status} | "
                  f"spawned by {workers.describe_spawner(state, row['spawned_by'])}"
                  + (f", parent {workers.name(parent)}" if parent else "")
                  + f" | artifacts {_counts(state, 'artifacts', row['id'])} | "
                  f"messages {_counts(state, 'inbox', row['id'])} | session {row['session_id'] or 'none yet'} | "
                  f"{_spent(worker_costs, lambda k, i=row['id']: k == i)}")
    print(f"undelivered messages: {state.undelivered()}")
    print()
    for line in _inputs(cfg, state, verbose=True) + _usage(cfg, state):
        print(line)
    for line in _cost_summary(state, DETAIL_PERIODS):
        print(line)


def _executable(path: Path) -> bool:
    return os.access(path, os.X_OK)


def _check_skills(cfg: Config, where: str, names: list[str], more: list[str] = (),
                  env: dict[str, str] | None = None, needed: tuple[str, ...] = ("required_env",),
                  shared: bool = True) -> list[str]:
    """The problems of the skills that a role, a source or a channel uses: missing skills, credential files and
    variables. Roles and sources have the shared skills too, and channels only their own."""
    if shared:
        found, problems = knowledge.collect(cfg, names)
    else:
        found, problems = [], []
        for name in names:
            try:
                found.append(knowledge.load(cfg, name))
            except knowledge.SkillError as exc:
                problems.append(str(exc))
    problems = [f"{where}: {p}" for p in problems]
    names_needed = [*(n for s in found for n in s.credentials), *more]
    problems += [f"{where}: credentials file {config_mod.credentials_file(cfg, n)} is missing"
                 for n in knowledge.missing_credentials(cfg, names_needed)]
    have = {**knowledge.session_env(cfg, found, more), **(env or {})}
    missing = sorted({v for s in found for key in needed for v in s.list_meta(key)
                      if not have.get(v) and not os.environ.get(v)})
    if missing:
        problems.append(f"{where}: environment variable {', '.join(missing)} not set; put it in a credentials file "
                        f"of {cfg.credentials_dir}")
    return problems


def cmd_check(args) -> int:
    problems: list[str] = []
    try:
        cfg, state = _load()
    except (ConfigError, OSError) as exc:
        print(f"config: {exc}")
        return 1
    problems += knowledge.problems(cfg)
    if cfg.scope is None:
        for role in sorted({row["role"] for row in state.workers()} - set(cfg.roles)):
            problems.append(f"role {role}: it has workers, but {cfg.roles_dir / role / config_mod.ROLE_FILE} is gone; "
                            "they get no turns until the role is back, or you archive them")
    for read in (knowledge.agent_skills, knowledge.watchdog_skill):
        try:
            read(cfg)
        except knowledge.SkillError as exc:
            problems.append(str(exc))
    for name, channel in cfg.channels.items():
        try:
            skill = knowledge.load(cfg, channel.skill)
        except knowledge.SkillError as exc:
            problems.append(f"channel {name}: {exc}")
            continue
        if not _executable(skill.dir / "send"):
            problems.append(f"channel {name}: {skill.dir / 'send'} is missing or not executable")
        if channel.input and not _executable(skill.dir / "receive"):
            problems.append(f"channel {name}: it has input, but {skill.dir / 'receive'} is missing or not executable")
        needed = ("required_env", "input_env") if channel.input else ("required_env",)
        problems += _check_skills(cfg, f"channel {name}", [channel.skill], channel.credentials, channel.env, needed,
                                  shared=False)
    for role in cfg.roles.values():
        problems += _check_skills(cfg, f"role {role.name}", role.skills, role.credentials)
        if not role.description:
            problems.append(f"role {role.name}: {role.role_md} has no description in its front matter")
    for poller in cfg.pollers.values():
        if not poller.poller_md.is_file():
            problems.append(f"poller {poller.name}: {poller.poller_md} is missing")
        for src in poller.sources:
            where = f"poller {poller.name}, source {src.id}"
            try:
                skill = knowledge.load(cfg, src.skill)
            except knowledge.SkillError as exc:
                problems.append(f"{where}: {exc}")
                continue
            if src.scan == "script" and not _executable(skill.dir / "scan"):
                problems.append(f"{where}: scan is script, but {skill.dir / 'scan'} is missing or not executable")
            missing = [p for p in skill.list_meta("required_params") if p not in src.params]
            if missing:
                problems.append(f"{where}: missing params {', '.join(missing)}")
            problems += _check_skills(cfg, where, [src.skill], src.credentials)
    binary = shutil.which(cfg.claude.binary)
    if not binary:
        problems.append(f"claude: {cfg.claude.binary} not found on PATH")
    else:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"claude: {version} ({cfg.claude.auth} login)")
    if cfg.claude.auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("claude: auth is api-key but ANTHROPIC_API_KEY is not set; put it in "
                        f"{config_mod.credentials_file(cfg, config_mod.OWN_CREDENTIALS)}")
    claude = Claude(cfg)
    for account in [None, *cfg.claude.accounts]:
        problems += _check_login(cfg, claude, account, logins=bool(binary) and cfg.claude.auth == "subscription")
    if not Tmux.installed():
        problems.append("tmux: not found; workers run in tmux windows")
    if not cfg.notice_channels():
        problems.append("notices: no channel gets vahtovik's notices, so nobody hears when a worker waits for an "
                        "answer, is stuck or failed; name one in `notices` in vahtovik.yaml, or give a channel "
                        "`input`")
    print(f"roles: {', '.join(cfg.roles) or 'none'} · pollers: {', '.join(cfg.pollers) or 'none'} · "
          f"channels: {', '.join(cfg.channels) or 'none'} · skills: {', '.join(knowledge.available(cfg)) or 'none'}")
    problems = list(dict.fromkeys(problems))
    for problem in problems:
        print(f"problem: {problem}")
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _check_login(cfg: Config, claude: Claude, account: str | None, logins: bool) -> list[str]:
    """Print which Claude account a login of the subscription uses. Returns the problems of its profile: no login,
    or no trust in the data folder."""
    name = f"claude account {account}" if account else "claude default login"
    run = f"run `{claude.start_command(account)}` in {cfg.root}"
    if logins:
        status = claude.login(account)
        if status is None:
            print(f"{name}: unknown, `{cfg.claude.binary} auth status` gave no answer")
        elif not status.get("loggedIn"):
            return [f"{name}: not logged in, in {claude.profile(account)}; {run}, log in, and accept the trust screen"]
        else:
            plan = status.get("planTier") or status.get("subscriptionType") or status.get("authMethod")
            ends = str(status.get("loginExpiresAt") or "")[:10]
            print(f"{name}: logged in" + (f" as {status['email']}" if status.get("email") else "")
                  + (f" ({plan})" if plan else "") + (f", login ends {ends}" if ends else ""))
    if claude.trusts(cfg.data_dir, account) is False:
        return [f"{name}: Claude Code does not trust {cfg.data_dir} or a folder above it, so worker sessions would "
                f"stop at its \"trust this folder\" screen; {run} and accept"]
    return []


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
        # A limited channel's agent sees only the lines about its roles' workers, and its own channel's.
        about = None if cfg.scope is None else [*cfg.scope, *filter(None, [os.environ.get("VAHTOVIK_CHANNEL")])]
        for line in journal.read(journal.path(cfg), args.lines, args.text, follow=args.follow, about=about):
            print(line, flush=args.follow)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:  # the reader quit early, for example `vahtovik log -n 0 | less`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def cmd_messages(args) -> int:
    cfg, state = _load()
    name = args.channel or os.environ.get("VAHTOVIK_CHANNEL")
    if not name:
        raise ConfigError("name the channel: vahtovik messages CHANNEL")
    if name not in cfg.channels or not cfg.channels[name].input:
        inputs = ", ".join(c.name for c in channel_input.input_channels(cfg)) or "none"
        raise ConfigError(f"{name!r} is not an input channel; input channels: {inputs}")
    print(channel_input.history_text(state, name, args.last) or f"No messages in {name} yet.")
    return 0


def cmd_acknowledge(args) -> int:
    cfg, state = _load()
    name = os.environ.get("VAHTOVIK_CHANNEL")
    if not name:
        raise ConfigError("run this inside a channel session (VAHTOVIK_CHANNEL is not set)")
    sent, line = channel_input.acknowledge(cfg, state, name, args.text, args.to,
                                           dry_run=os.environ.get("VAHTOVIK_DRY_RUN") == "1")
    print(line)
    return 0 if sent else 1


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


WORKER_REF = "a worker: <id>, <id>-<key> or <role>/<key>"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vahtovik", description="AI monitoring and workers on Claude Code sessions")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run one cycle")
    p.add_argument("--poller", help="only this poller, and the turns of its roles' workers")
    p.add_argument("--stage", choices=["input", "monitor", "workers", "deliver"], help="only this stage")
    p.add_argument("--force", action="store_true",
                   help="ignore the schedules of sources and the intervals of input channels")
    p.add_argument("--dry-run", action="store_true", help="vahtovik's channels print instead of sending")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("open", help="open a worker's session interactively")
    p.add_argument("ref", help=WORKER_REF)
    p.add_argument("--new-session", action="store_true", help="start a fresh session for the worker")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("turn", help="end your turn with your status (for agents in worker sessions)")
    p.add_argument("status", choices=["finished", "scheduled", "waiting", "background"],
                   help="finished: your work is really done; scheduled: you need another turn later, after --after "
                        "or at --at, or the one you have scheduled; waiting: you need an answer or action; "
                        "background: you wait for background work you started, which wakes you when it ends")
    p.add_argument("--importance", choices=worker_status.IMPORTANCE, help="required at the end of an automated turn")
    p.add_argument("--summary", help="the current state in one or two sentences; required at the end of an automated turn")
    p.add_argument("--question", action="append", default=[], help="an open question; repeat for each one")
    p.add_argument("--reason", help="background: what you wait for; scheduled: what the next turn should check")
    p.add_argument("--after", help="scheduled: how long until your next turn, for example 6h or 1d")
    p.add_argument("--at", help="scheduled: when your next turn comes: HH:MM (its next occurrence) or "
                                "YYYY-MM-DD HH:MM, in local time")
    p.add_argument("--check-after", help="background: when the watchdog should check on you if you are still "
                                         "waiting, for example 30m")
    p.set_defaults(func=cmd_turn)

    p = sub.add_parser("worker", help="spawn, see, watch, merge, split, schedule and archive workers")
    worker_sub = p.add_subparsers(dest="worker_command", required=True)
    w = worker_sub.add_parser("spawn", help="spawn a worker; inside a worker, it becomes the new one's parent")
    w.add_argument("ref", help="the new worker: <role>/<key>")
    w.add_argument("--title", help="its title (default: its key)")
    w.add_argument("--skill", action="append", default=[], help="an extra skill of knowledge/skills; repeat for each")
    w.add_argument("--message", help="its task: it starts with this message at the next run")
    w.add_argument("--watch", action="store_true",
                   help="inside a worker: watch the new worker, as `vahtovik worker watch` does")
    w.set_defaults(func=cmd_worker_spawn)
    w = worker_sub.add_parser("watch", help="make a worker watch other workers: vahtovik tells it each time one of "
                                            "them ends a turn, needs a person, or fails for good")
    w.add_argument("refs", nargs="+", metavar="worker", help="a worker to watch")
    w.add_argument("--watcher", help="the worker that watches (default: the worker this command runs in)")
    w.set_defaults(func=cmd_worker_watch)
    w = worker_sub.add_parser("unwatch", help="end a worker's watches of other workers")
    w.add_argument("refs", nargs="+", metavar="worker", help="a worker to watch no more")
    w.add_argument("--watcher", help="the worker that watches (default: the worker this command runs in)")
    w.set_defaults(func=cmd_worker_unwatch)
    w = worker_sub.add_parser("list", help="the workers: their status, what they do, and their parent")
    w.add_argument("--role", help="only workers of this role")
    w.add_argument("--status", help="only workers with this status")
    w.add_argument("--tree", action="store_true", help="children under their parents")
    w.add_argument("--all", action="store_true", help="archived workers too")
    w.set_defaults(func=cmd_worker_list)
    w = worker_sub.add_parser("show", help="all about one worker")
    w.add_argument("ref", help=WORKER_REF)
    w.set_defaults(func=cmd_worker_show)
    w = worker_sub.add_parser("merge", help="merge workers into another worker, which keeps its role")
    w.add_argument("refs", nargs="+", help="the workers to merge")
    w.add_argument("--into", required=True, help="the worker they go into")
    w.set_defaults(func=cmd_worker_merge)
    w = worker_sub.add_parser("split", help="move some artifacts of a worker to another worker, new or existing")
    w.add_argument("ref", help="the worker they are in")
    w.add_argument("--artifact", action="append", default=[], required=True,
                   help="the id of an artifact to move; repeat for each one")
    w.add_argument("--into", required=True, help="the worker they go to; a new one as <role>/<key>")
    w.add_argument("--title", help="the title of the worker they go to, if it is new")
    w.set_defaults(func=cmd_worker_split)
    w = worker_sub.add_parser("schedule", help="schedule a worker's next turn, or cancel it")
    w.add_argument("ref", help=WORKER_REF)
    w.add_argument("--after", help="how long until the turn, such as 6h")
    w.add_argument("--at", help="when the turn comes: HH:MM (its next occurrence) or YYYY-MM-DD HH:MM, local time")
    w.add_argument("--off", action="store_true", help="cancel the next scheduled turn")
    w.add_argument("--reason", help="what the scheduled turn should check")
    w.set_defaults(func=cmd_worker_schedule)
    w = worker_sub.add_parser("archive", help="archive a worker: no more turns, and lists leave it out")
    w.add_argument("ref", help=WORKER_REF)
    w.add_argument("--reason", required=True, help="why the worker is archived")
    w.set_defaults(func=cmd_worker_archive)

    p = sub.add_parser("artifact", help="add artifacts to workers")
    artifact_sub = p.add_subparsers(dest="artifact_command", required=True)
    a = artifact_sub.add_parser("add", help="add artifacts, as JSON (one object or a list), to workers")
    a.add_argument("--worker", action="append", default=[],
                   help="a worker to add them to; repeat for each one (default inside a worker: itself)")
    a.add_argument("--json", help="the artifacts (default: stdin)")
    a.set_defaults(func=cmd_artifact_add)

    p = sub.add_parser("tell", help="pass a message to workers")
    p.add_argument("refs", nargs="+", help="the workers")
    p.add_argument("--message", help="the message (default: stdin)")
    p.set_defaults(func=cmd_tell)

    p = sub.add_parser("memory", help="add notes to a role's memory, MEMORY.md, or remove them")
    memory_sub = p.add_subparsers(dest="memory_command", required=True)
    m = memory_sub.add_parser("add", help="add a note that holds for all workers of the role")
    m.add_argument("--role", help="default: $VAHTOVIK_ROLE")
    m.add_argument("--text", help="the note (default: stdin)")
    m.set_defaults(func=cmd_memory_add)
    m = memory_sub.add_parser("remove", help="remove an entry that turned out wrong or stale")
    m.add_argument("entry", type=int, help="the entry's number, from its heading in MEMORY.md")
    m.add_argument("--reason", required=True, help="why the entry goes")
    m.add_argument("--role", help="default: $VAHTOVIK_ROLE")
    m.set_defaults(func=cmd_memory_remove)

    p = sub.add_parser("window", help=argparse.SUPPRESS)
    p.add_argument("spec")
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("event", choices=["prompt", "stop", "compact", "guard"])
    p.add_argument("channel", nargs="?", help="guard: the limited input channel")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("status", help="show pollers, workers, channels and costs")
    p.add_argument("--role", help="only this role, its workers and its pollers")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="all details as text: sources, workers, status reasons")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="show the journal of what vahtovik did")
    p.add_argument("text", nargs="?", default="",
                   help="only lines that contain this text, for example a worker, a poller, a run or a date")
    p.add_argument("-n", "--lines", type=int, default=40, help="how many last lines to show; 0 shows all (default 40)")
    p.add_argument("-f", "--follow", action="store_true", help="keep showing new lines as they are written")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("messages", help="show an input channel's earlier messages and vahtovik's replies")
    p.add_argument("channel", nargs="?", help="the input channel; inside a channel session, its own")
    p.add_argument("-n", "--last", type=int, default=20,
                   help="how many of the last messages to show, with their replies; 0 shows all (default 20)")
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("acknowledge", help="tell the person at once that their messages arrived (for channel sessions)")
    p.add_argument("--text", required=True, help="one short line that fits the messages, such as 'Let me check.'")
    p.add_argument("--to", help="the message it answers (default: the last one that waits for an answer)")
    p.set_defaults(func=cmd_acknowledge)

    p = sub.add_parser("install-timer", help="start vahtovik periodically with a systemd user timer")
    p.add_argument("--every", default="1m",
                   help="how often to start a cycle; must divide an hour or a day (default 1m)")
    p.set_defaults(func=cmd_install_timer)

    p = sub.add_parser("uninstall-timer", help="remove the systemd user timer")
    p.set_defaults(func=cmd_uninstall_timer)

    p = sub.add_parser("check", help="validate config, knowledge, roles, pollers, channels and environment")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("test-channel", help="send a test message through one of vahtovik's channels")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_test_channel)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, delivery.DeliveryError, worker_status.StatusError, knowledge.SkillError,
            timer.TimerError, TmuxError) as exc:
        print(f"vahtovik: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
