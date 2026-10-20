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
from . import delivery, group_session, group_status, journal, skills, timer, watchdog, windows
from .claude_session import Claude
from .config import Config, ConfigError
from .stages import group_turn, monitor
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


def _due(state: State, watch, now: datetime) -> bool:
    last = state.last_run(watch.name)
    return last is None or datetime.fromisoformat(last) + watch.interval <= now


def _nothing_due(state: State, watches, now: datetime, stage: str | None) -> str:
    if stage == "deliver":
        return "no failed reports to retry"
    if not watches:
        return "no enabled watches"
    waits = (datetime.fromisoformat(state.last_run(w.name)) + w.interval - now for w in watches)
    return ", ".join(f"{w.name} in {duration(wait.total_seconds())}" for w, wait in zip(watches, waits))


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


def _running(lock: Path) -> str:
    """Which run holds the run lock, from what it wrote into the lock file."""
    try:
        run_id, started = lock.read_text().split()
        return f"run {run_id} has been going for {duration(time.time() - float(started))}"
    except (OSError, ValueError):
        return "another run is in progress"


def _run(cfg: Config, state: State, args, run_id: str) -> int:
    tmux = Tmux(cfg)
    watchdog.tick(cfg, state, tmux)
    now = datetime.now(timezone.utc)
    watches = [cfg.watch(args.watch)] if args.watch else [w for w in cfg.watches.values() if w.enabled]
    due = [w for w in watches if args.force or _due(state, w, now)] if args.stage != "deliver" else []
    retry = args.stage in (None, "deliver") and bool(state.retryable_deliveries(delivery.MAX_ATTEMPTS))
    if not due and not retry:
        log.info("Nothing due: %s", _nothing_due(state, watches, now, args.stage))
        return 0
    flags = [name for name, on in (("forced", args.force), ("dry run", args.dry_run)) if on]
    flags += [f"stage {args.stage}"] if args.stage else []
    log.info("Started run: %s%s", ", ".join(w.name for w in due) or "retrying failed reports",
             f" ({', '.join(flags)})" if flags else "")
    started = time.monotonic()
    raw_dir = cfg.runs_dir(run_id)
    claude = Claude(cfg)
    new = 0
    state.start_run(run_id)
    status = "ok"
    try:
        for watch in due:
            if args.stage in (None, "monitor"):
                new += monitor.run(cfg, state, claude, watch, run_id, raw_dir)
            if args.stage in (None, "group"):
                group_turn.run(cfg, state, claude, tmux, watch, run_id, watchdog.Ticker(cfg, state, tmux))
            if args.stage is None:
                state.set_last_run(watch.name)
        if args.stage in (None, "deliver"):
            delivery.retry_failed(cfg, state, args.dry_run)
    except BaseException as exc:
        status = "failed"
        log.error("Failed run after %s: %s", duration(time.monotonic() - started), _describe(exc))
        raise
    finally:
        state.finish_run(run_id, status)
    summary = state.run_summary(run_id)
    turns = plural(summary["turns_ok"] + summary["turns_failed"], "group turn")
    turns += f" ({summary['turns_failed']} failed)" if summary["turns_failed"] else ""
    reports = plural(summary["reports_sent"], "report") + " sent"
    reports += f", {summary['reports_failed']} not sent" if summary["reports_failed"] else ""
    log.info("Finished run in %s: %s, %s, %s, $%.2f", duration(time.monotonic() - started),
             plural(new, "new artifact"), turns, reports, summary["cost"])
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
    print(group_status.declare(cfg, state, cfg.watch(watch), key, args.status, args.importance, args.summary,
                               args.question, os.environ.get("VAHTOVIK_TURN")))
    return 0


def cmd_hook(args) -> int:
    """Claude Code runs this for the hooks of group sessions (group_status.hooks)."""
    _cfg, state = _load()
    watch, key = _session_group()
    data = json.loads(sys.stdin.read() or "{}")
    if args.event == "prompt":
        group_status.on_prompt(state, watch, key, data.get("session_id"))
    else:
        answer = group_status.on_stop(state, watch, key)
        if answer:
            print(json.dumps(answer))
    return 0


def cmd_status(args) -> int:
    cfg, state = _load()
    names = [args.watch] if args.watch else sorted(cfg.watches)
    for name in names:
        watch = cfg.watch(name)
        last = state.last_run(name)
        print(f"{name}: {'enabled' if watch.enabled else 'disabled'}, every {watch.interval}, "
              f"last run {human(datetime.fromisoformat(last)) if last else 'never'}")
        for src in watch.sources:
            row = state.row("SELECT * FROM source_state WHERE watch = ? AND source_id = ?", name, src.id)
            error = f", last error: {row['last_error']}" if row and row["last_error"] and (
                not row["last_ok_at"] or row["last_error_at"] > row["last_ok_at"]) else ""
            print(f"  source {src.id} ({src.skill}){error}")
        for g in state.groups(name):
            counts = {r["status"]: r["n"] for r in state.rows(
                "SELECT status, COUNT(*) AS n FROM artifacts WHERE watch = ? AND group_key = ? GROUP BY status",
                name, g["key"])}
            summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
            status = g["status"] + (f": {g['status_reason']}" if g["status_reason"] else "")
            questions = json.loads(g["questions"] or "[]") if g["status"] == "waiting" else []
            status += f", {plural(len(questions), 'open question')}" if questions else ""
            print(f"  group {g['key']}: {g['title']} | {status} | {summary} | "
                  f"session {g['session_id'] or 'none yet'}")
    failed = state.row("SELECT COUNT(*) AS n FROM deliveries WHERE status != 'sent'")["n"]
    day_ago = utc_iso(datetime.now(timezone.utc) - timedelta(days=1))
    cost = state.row("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM sessions WHERE started_at >= ?", day_ago)["c"]
    print(f"undelivered reports: {failed} · AI cost in the last 24 hours: ${cost:.2f}")
    return 0


def cmd_check(args) -> int:
    problems: list[str] = []
    try:
        cfg, _state = _load()
    except (ConfigError, OSError) as exc:
        print(f"config: {exc}")
        return 1
    try:
        skills.main_skill(cfg.skills_dir)
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
        missing = [v for v in skill.list_meta("required_env") if v not in channel.env and not os.environ.get(v)]
        if missing:
            problems.append(f"channel {name}: environment variable {', '.join(missing)} not set")
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
        for line in journal.read(journal.path(cfg), args.lines, args.text, follow=args.follow):
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
    p.add_argument("--stage", choices=["monitor", "group", "deliver"], help="only this stage")
    p.add_argument("--force", action="store_true", help="ignore the watch interval")
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
    p.add_argument("status", choices=["finished", "waiting"],
                   help="finished: you need nothing from a person; waiting: you need an answer or action")
    p.add_argument("--importance", choices=group_status.IMPORTANCE, help="required at the end of an automated turn")
    p.add_argument("--summary", help="the current state in one or two sentences; required at the end of an automated turn")
    p.add_argument("--question", action="append", default=[], help="an open question; repeat for each one")
    p.set_defaults(func=cmd_turn)

    p = sub.add_parser("window", help=argparse.SUPPRESS)
    p.add_argument("spec")
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("event", choices=["prompt", "stop"])
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("status", help="show watches, groups and deliveries")
    p.add_argument("--watch")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="show the journal of what vahtovik did")
    p.add_argument("text", nargs="?", default="",
                   help="only lines that contain this text, for example a watch, a group, a run or a date")
    p.add_argument("-n", "--lines", type=int, default=40, help="how many last lines to show; 0 shows all (default 40)")
    p.add_argument("-f", "--follow", action="store_true", help="keep showing new lines as they are written")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("install-timer", help="start vahtovik periodically with a systemd user timer")
    p.add_argument("--every", default="15m",
                   help="how often to start a cycle; must divide an hour or a day (default 15m)")
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
