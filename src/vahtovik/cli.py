"""vahtovik command line."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import config as config_mod
from . import delivery, group_session, skills
from .claude_session import Claude
from .config import Config, ConfigError
from .stages import group_turn, monitor
from .state import State
from .util import LockBusy, file_lock, human, utc_iso

log = logging.getLogger("vahtovik")


def _load() -> tuple[Config, State]:
    root = config_mod.find_root()
    config_mod.load_env_file(root)
    cfg = config_mod.load(root)
    return cfg, State(cfg.db_path)


def _due(state: State, watch, now: datetime) -> bool:
    last = state.last_run(watch.name)
    return last is None or datetime.fromisoformat(last) + watch.interval <= now


def cmd_run(args) -> int:
    cfg, state = _load()
    if args.dry_run:
        os.environ["VAHTOVIK_DRY_RUN"] = "1"  # sessions inherit it, so `vahtovik report` dry-runs too
    try:
        with file_lock(cfg.data_dir / ".run.lock"):
            return _run(cfg, state, args)
    except LockBusy:
        log.info("another vahtovik run is in progress; exiting")
        return 0


def _run(cfg: Config, state: State, args) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_dir = cfg.runs_dir(run_id)
    claude = Claude(cfg)
    now = datetime.now(timezone.utc)
    watches = [cfg.watch(args.watch)] if args.watch else [w for w in cfg.watches.values() if w.enabled]
    state.start_run(run_id)
    status = "ok"
    try:
        for watch in watches:
            if not args.force and not _due(state, watch, now):
                log.info("%s: not due yet (interval %s)", watch.name, watch.interval)
                continue
            if args.stage in (None, "monitor"):
                monitor.run(cfg, state, claude, watch, run_id, raw_dir)
            if args.stage in (None, "group"):
                group_turn.run(cfg, state, claude, watch, run_id, raw_dir)
            if args.stage is None:
                state.set_last_run(watch.name)
        if args.stage in (None, "deliver"):
            sent, failed = delivery.retry_failed(cfg, state, args.dry_run)
            if sent or failed:
                log.info("retried deliveries: %d sent, %d still failing", sent, failed)
    except BaseException:
        status = "failed"
        raise
    finally:
        state.finish_run(run_id, status)
    return 0


def cmd_open(args) -> int:
    cfg, state = _load()
    return group_session.open_session(cfg, state, args.ref, new_session=args.new_session)


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
            print(f"  group {g['key']}: {g['title']} | {summary} | session {g['session_id'] or 'none yet'}")
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
    binary = shutil.which(cfg.claude.binary)
    if not binary:
        problems.append(f"claude: {cfg.claude.binary} not found on PATH")
    else:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"claude: {version} ({cfg.claude.auth} login)")
    if cfg.claude.auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("claude: auth is api-key but ANTHROPIC_API_KEY is not set")
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
    print(("sent" if result.ok else "failed") + (f": {result.detail}" if result.detail else ""))
    return 0 if result.ok else 1


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

    p = sub.add_parser("status", help="show watches, groups and deliveries")
    p.add_argument("--watch")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check", help="validate config, skills and environment")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("test-channel", help="send a test message through a channel")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_test_channel)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, delivery.DeliveryError, skills.SkillError) as exc:
        print(f"vahtovik: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
