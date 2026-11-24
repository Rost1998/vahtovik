"""Operations for the person, the vahtovik agent and the workers: the `vahtovik worker ...`, `artifact add` and
`tell` commands.

They change workers only through the database and vahtovik's own file helpers. Merging, splitting and archiving
hold the locks of the workers they change, so no turn, window or other command uses those workers meanwhile."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone

import jsonschema

from . import artifacts, context_file, inbox, workers
from .config import Config, ConfigError, parse_interval, parse_moment
from .stages.worker_turn import MAX_ATTEMPTS
from .state import State
from .tmux import Tmux
from .util import LockBusy, headings_down, human, load_schema, plural, utc_iso, write_atomic

log = logging.getLogger(__name__)


def _sender(env: Mapping[str, str]) -> str:
    """Who a message comes from: the worker the command runs in, or the person (also through a channel)."""
    return f"worker:{env['VAHTOVIK_WORKER']}" if env.get("VAHTOVIK_WORKER") else "person"


# spawning and seeing workers

def spawn(cfg: Config, state: State, ref: str, skills: list[str] = (), title: str | None = None,
          message: str | None = None, report_back: bool = False, env: Mapping[str, str] = os.environ) -> list[str]:
    """Spawn a worker, <role>/<key>. Inside a worker, that worker is its parent. With a message, the new worker
    starts with it at the next run; without one, it waits for artifacts or messages."""
    role, key = workers.parse_new(ref)
    spawned_by, parent = workers.spawned_by_env(env)
    if report_back and parent is None:
        raise ConfigError("--report-back tells the parent, and only a worker can be a parent: spawn from inside one")
    row = workers.create(cfg, state, role, key, title or "", spawned_by, parent, list(skills), report_back)
    lines = [f"spawned worker {workers.name(row)} ({role}/{key})"]
    if message and message.strip():
        inbox.send(state, row, _sender(env), message)
        lines.append("it starts with the message at the next run")
    else:
        lines.append("it waits for artifacts or a message")
    if report_back:
        lines.append("it tells you when it ends a turn as finished or waiting, or fails")
    return lines


def _ago(value: str | None) -> str:
    return human(datetime.fromisoformat(value)) if value else "never"


def status_text(row) -> str:
    """A worker's status for people: "scheduled for 2026-11-24 18:00", "waiting, 2 questions", "failed: ..."."""
    text = row["status"]
    if row["status"] == "waiting":
        text += f", {plural(len(json.loads(row['questions'] or '[]')), 'question')}"
    if row["scheduled_at"]:
        text += f", next turn {_ago(row['scheduled_at'])}"
    if row["status_reason"] and row["status"] in ("failed", "maintenance", "background"):
        text += f": {row['status_reason'][:80]}"
    if row["archived_at"]:
        text = f"archived {_ago(row['archived_at'])}: {row['archive_reason']}"
    return text


def doing(state: State, row) -> str:
    """What a worker does, in one line: the summary of its last ended turn, or, before its first turn has ended,
    the task it was spawned with."""
    if row["summary"]:
        return " ".join(row["summary"].split())
    task = state.row("SELECT text FROM inbox WHERE worker = ? ORDER BY id LIMIT 1", row["id"])
    return f"task: {' '.join(task['text'].split())}" if task else "(no turn has ended yet)"


def list_workers(cfg: Config, state: State, role: str | None = None, status: str | None = None,
                 tree: bool = False, archived: bool = False) -> list[str]:
    """Two lines per worker: its name, role, status, who spawned it (a worker that spawned it is its parent) and
    title; then what it does."""
    if role:
        cfg.role(role)
    roles = [role] if role else (None if cfg.scope is None else cfg.scope)
    rows = [r for r in state.workers(roles, archived) if not status or r["status"] == status]
    if not rows:
        return ["no workers" + (f" of role {role}" if role else "") + (f" with status {status}" if status else "")]
    by_id = {r["id"]: r for r in rows}

    def lines_for(row, depth: int) -> list[str]:
        head = (f"{'  ' * depth}{workers.name(row)} · {row['role']} · {status_text(row)} · "
                f"spawned by {workers.describe_spawner(state, row['spawned_by'])} · {row['title']}")
        return [head, f"{'  ' * depth}  {doing(state, row)[:300]}"]

    if not tree:
        return [line for row in rows for line in lines_for(row, 0)]
    lines: list[str] = []

    def walk(row, depth: int) -> None:
        lines.extend(lines_for(row, depth))
        for child in rows:
            if child["parent"] == row["id"]:
                walk(child, depth + 1)

    for row in rows:
        if row["parent"] not in by_id:  # a root here: no parent, or its parent is not listed
            walk(row, 0)
    return lines


def show(cfg: Config, state: State, ref: str) -> list[str]:
    """All about one worker, also an archived one."""
    row = workers.resolve(cfg, state, ref, archived=True)
    parent = state.worker(row["parent"]) if row["parent"] else None
    children = state.children(row["id"])
    counts = {r["status"]: r["n"] for r in state.rows(
        "SELECT status, COUNT(*) AS n FROM artifacts WHERE worker = ? GROUP BY status", row["id"])}
    messages = state.inbox_counts(row["id"])
    turn = state.latest_turn(row["id"])
    lines = [
        f"worker {workers.name(row)}: {row['title']}",
        f"  role: {row['role']}; key: {row['key']}; id: {row['id']}",
        f"  status: {status_text(row)}" + (f" ({row['status_reason']})" if row["status_reason"] and row["status"]
                                             not in ("failed", "maintenance", "background") else ""),
    ]
    questions = json.loads(row["questions"] or "[]") if row["status"] == "waiting" else []
    lines += [f"  open question: {q}" for q in questions]
    if row["scheduled_at"]:
        lines.append(f"  scheduled turn: {_ago(row['scheduled_at'])}: {row['scheduled_reason']}")
    lines.append(f"  what it does: {doing(state, row)}")
    lines.append(f"  spawned by {workers.describe_spawner(state, row['spawned_by'])} on {_ago(row['created_at'])}; "
                 f"parent: {workers.name(parent) if parent else 'none'}"
                 + ("; reports back to its parent" if row["report_back"] and parent else ""))
    lines.append("  children: " + (", ".join(f"{workers.name(c)} ({c['status']})" for c in children) or "none"))
    lines.append("  artifacts: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none")
                 + "; messages: " + (", ".join(f"{n} {s}" for s, n in sorted(messages.items())) or "none"))
    if turn:
        how = "not finished" if not turn["finished_at"] else ("finished" if turn["ok"] else f"failed: {turn['error']}")
        about = (f"{plural(len(json.loads(turn['artifact_ids'] or '[]')), 'artifact')}, "
                 f"{plural(len(json.loads(turn['message_ids'] or '[]')), 'message')}"
                 + (", scheduled" if turn["scheduled_for"] else ""))
        lines.append(f"  latest turn: {turn['id']}, started {_ago(turn['started_at'])}, for {about}, {how}")
    lines.append(f"  session: {row['session_id'] or 'none yet'}; updated {_ago(row['updated_at'])}")
    if row["merged_into"]:
        target = state.worker(row["merged_into"])
        lines.append(f"  merged into: {workers.name(target) if target else row['merged_into']}")
    lines.append(f"  folder: {workers.folder(cfg, row)}")
    lines.append(f"  record: {workers.context_md(cfg, row)}")
    return lines


# scheduling and archiving

def schedule(cfg: Config, state: State, ref: str, after: str | None = None, at: str | None = None,
             off: bool = False, reason: str | None = None) -> list[str]:
    """Schedule the worker's next turn, `after` a while or `at` a time, or cancel it with `off`."""
    if [after is not None, at is not None, off].count(True) != 1:
        raise ConfigError("give one of --after, --at and --off")
    row = workers.resolve(cfg, state, ref)
    ref = workers.name(row)
    done = row["status"] in ("idle", "finished", "scheduled")
    if off:
        state.set_schedule(row["id"], None)
        if row["status"] == "scheduled":
            state.set_status(row["id"], "finished" if state.latest_turn(row["id"]) else "idle")
        log.info("%s: Cancelled the next scheduled turn", workers.label(row))
        return [f"{ref}: next scheduled turn: none"]
    try:
        when = parse_moment(at) if at is not None else datetime.now(timezone.utc) + parse_interval(after)
    except ConfigError as exc:
        raise ConfigError(f"{'--at' if at is not None else '--after'}: {exc}") from None
    why = (reason or "").strip() or "a check the person scheduled"
    state.set_schedule(row["id"], utc_iso(when), why)
    if done:
        state.set_status(row["id"], "scheduled", why)
    log.info("%s: Scheduled a turn for %s: %s", workers.label(row), human(when), why)
    lines = [f"{ref}: next scheduled turn: {human(when)} ({why})"]
    if not done:
        lines.append(f"{ref} is {row['status']} now: the scheduled turn starts once it is done. If its agent ends "
                     "with `finished`, that cancels the scheduled turn.")
    return lines


@contextmanager
def _hold(cfg: Config, state: State, tmux: Tmux, rows: list):
    """Hold the locks of the workers. Refuse if one has its window open, is busy, or has an unfinished turn."""
    with ExitStack() as stack:
        for row in rows:
            name = workers.name(row)
            if tmux.window(row["id"]):
                raise ConfigError(f"{name} has its session window open; wait until vahtovik closes it, or close it")
            try:
                stack.enter_context(workers.lock(workers.folder(cfg, row)))
            except LockBusy:
                raise ConfigError(f"{name} is busy; try again in a moment") from None
            turn = state.latest_turn(row["id"])
            if turn and turn["finished_at"] is None:
                raise ConfigError(f"{name} has an unfinished turn; the watchdog ends it at the next run, try then")
        yield


def _move_folder(cfg: Config, state: State, row) -> None:
    """Move an archived worker's folder into data/archive/<role>/."""
    before = cfg.workers_dir / row["role"] / workers.name(row)
    after = workers.folder(cfg, state.worker(row["id"]))
    if before.is_dir():
        after.parent.mkdir(parents=True, exist_ok=True)
        before.rename(after)


def archive(cfg: Config, state: State, tmux: Tmux, ref: str, reason: str) -> list[str]:
    """Archive a worker: it gets no more turns, its folder goes to data/archive/<role>/, and lists leave it out.
    Its key is free for a new worker."""
    reason = reason.strip()
    if not reason:
        raise ConfigError("give --reason: why the worker is archived")
    row = workers.resolve(cfg, state, ref)
    with _hold(cfg, state, tmux, [row]):
        state.archive_worker(row["id"], reason)
    _move_folder(cfg, state, row)
    log.info("%s: Archived: %s", workers.label(row), reason)
    return [f"archived {workers.name(row)}; its folder is {workers.folder(cfg, state.worker(row['id']))}"]


# merging and splitting

def _move_file(cfg: Config, row, filename: str, target) -> None:
    """Move an artifact file to the target worker, with the target's name in it."""
    source = workers.folder(cfg, row) / "artifacts" / filename
    destination = workers.folder(cfg, target) / "artifacts" / filename
    if not source.is_file():
        return
    if not destination.exists():  # the same file name means the same artifact id: the target keeps its own
        data = json.loads(source.read_text())
        data.update(worker=workers.name(target), role=target["role"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(destination, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    source.unlink()


def _merged_entry(row, context) -> str:
    name = workers.name(row)
    parts = [f"### {human()} · merged {name}",
             f"vahtovik merged worker `{name}` (role `{row['role']}`, \"{row['title']}\") into this worker. Its "
             "artifacts are in the Artifacts section now, and its record follows.",
             f"**Summary of `{name}`:**\n\n{headings_down(context_file.section_text(context, 'Summary')) or '_None._'}"]
    questions = context_file.section_text(context, "Open questions")
    if questions and questions != "_None._":
        parts.append(f"**Open questions of `{name}`:**\n\n{questions}")
    work = headings_down(context_file.section_text(context, "Work log")) or "_None._"
    parts.append(f"**Work log of `{name}`:**\n\n{work}")
    return "\n\n".join(parts)


def merge(cfg: Config, state: State, tmux: Tmux, refs: list[str], into: str) -> list[str]:
    """Merge workers into the worker `into`, which keeps its role. Each merged worker's artifacts, turns, costs and
    waiting messages move there, its record goes into the target's Work log, it is archived, and its key leads to
    the target from now on, so monitoring never brings it back."""
    target = workers.resolve(cfg, state, into)
    rows = []
    for ref in refs:
        row = workers.resolve(cfg, state, ref)
        if row["id"] == target["id"]:
            raise ConfigError(f"{ref} cannot be merged into itself")
        if row["id"] not in {r["id"] for r in rows}:
            rows.append(row)
    if not rows:
        raise ConfigError("name at least one worker to merge")
    target_context = workers.context_md(cfg, target)
    lines = []
    with _hold(cfg, state, tmux, [target, *rows]):
        for row in rows:
            items = state.worker_artifacts(row["id"])
            had = {r["id"] for r in state.worker_artifacts(target["id"])}
            context = workers.context_md(cfg, row)
            context_file.append(target_context, "Work log", _merged_entry(row, context))
            with state.transaction():
                state.merge_worker(row["id"], target["id"], f"merged into {workers.name(target)}")
            for item in items:
                _move_file(cfg, row, item["file"], target)
            _move_folder(cfg, state, row)
            moved = sum(r["id"] not in had for r in items)
            where = workers.folder(cfg, state.worker(row["id"]))
            log.info("%s: Merged into %s: %s moved, %d there already; the old folder is %s", workers.label(row),
                     workers.name(target), plural(moved, "artifact"), len(items) - moved, where)
            lines.append(f"merged {workers.name(row)} into {workers.name(target)}: {plural(moved, 'artifact')} moved, "
                         f"{len(items) - moved} there already; the old folder is {where}")
        context_file.write_artifacts(target_context, state.worker_artifacts(target["id"]))
        state.touch_worker(target["id"])
    waiting = len(state.pending_artifacts(target["id"], MAX_ATTEMPTS))
    if waiting:
        lines.append(f"{plural(waiting, 'artifact')} {'waits' if waiting == 1 else 'wait'} for the next turn of "
                     f"{workers.name(target)}")
    return lines


def split(cfg: Config, state: State, tmux: Tmux, ref: str, ids: list[str], into: str, title: str | None = None,
          env: Mapping[str, str] = os.environ) -> list[str]:
    """Move some artifacts of a worker to another worker: an existing one, or a new one given as <role>/<key>.
    There they wait for its next turn."""
    row = workers.resolve(cfg, state, ref)
    items = {r["id"]: r for r in state.worker_artifacts(row["id"])}
    ids = list(dict.fromkeys(ids))
    missing = [i for i in ids if i not in items]
    if not ids or missing:
        raise ConfigError(f"not artifacts of {workers.name(row)}: {', '.join(missing)}" if missing
                          else "name the artifacts to move")
    try:
        target, created = workers.resolve(cfg, state, into), False
    except ConfigError:
        if "/" not in into:
            raise
        role, key = workers.parse_new(into)
        target, created = None, True
    if target is not None and target["id"] == row["id"]:
        raise ConfigError("the artifacts are in that worker already")
    with _hold(cfg, state, tmux, [row]), ExitStack() as stack:
        if created:
            spawned_by, parent = workers.spawned_by_env(env)
            target = workers.create(cfg, state, role, key, title or "", spawned_by, parent)
        stack.enter_context(_hold(cfg, state, tmux, [target]))
        with state.transaction():
            for artifact_id in ids:
                state.move_artifact(artifact_id, row["id"], target["id"])
        for artifact_id in ids:
            _move_file(cfg, row, items[artifact_id]["file"], target)
        titles = "; ".join(f"\"{items[i]['title'] or i}\"" for i in ids)
        source_context, target_context = workers.context_md(cfg, row), workers.context_md(cfg, target)
        summary = headings_down(context_file.section_text(source_context, "Summary")) or "_None._"
        context_file.append(target_context, "Work log", "\n\n".join([
            f"### {human()} · split from {workers.name(row)}",
            f"vahtovik moved {plural(len(ids), 'artifact')} here from worker `{workers.name(row)}` "
            f"(\"{row['title']}\"): {titles}.",
            f"**Summary of `{workers.name(row)}` at that time:**\n\n{summary}"]))
        context_file.append(source_context, "Work log", "\n\n".join([
            f"### {human()} · split into {workers.name(target)}",
            f"vahtovik moved {plural(len(ids), 'artifact')} to worker `{workers.name(target)}`: {titles}."]))
        for each, context in ((row, source_context), (target, target_context)):
            context_file.write_artifacts(context, state.worker_artifacts(each["id"]))
            state.touch_worker(each["id"])
    log.info("%s: Moved %s to %s%s", workers.label(row), plural(len(ids), "artifact"), workers.name(target),
             " (a new worker)" if created else "")
    return [f"moved {plural(len(ids), 'artifact')} from {workers.name(row)} to {workers.name(target)}"
            + (" (a new worker)" if created else "")
            + f"; {'it waits' if len(ids) == 1 else 'they wait'} for the next turn of {workers.name(target)}"]


# artifacts from the person and from workers

def add_artifacts(cfg: Config, state: State, refs: list[str], text: str,
                  env: Mapping[str, str] = os.environ) -> list[str]:
    """Add artifacts, given as JSON (one object or a list, by schemas/artifact.json), to each worker named; inside
    a worker, to that worker by default. A worker's artifacts for itself count as processed: it found them while
    working. An artifact a worker has already is skipped there."""
    own = env.get("VAHTOVIK_WORKER")
    if not refs:
        if not own:
            raise ConfigError("name at least one worker with --worker")
        refs = [own]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"the artifacts are not JSON: {exc}") from None
    items = data if isinstance(data, list) else [data]
    schema = load_schema("artifact")
    for number, item in enumerate(items, 1):
        try:
            jsonschema.validate(item, schema)
        except jsonschema.ValidationError as exc:
            raise ConfigError(f"artifact {number}: {exc.message}") from None
    targets = []
    for ref in refs:
        row = workers.resolve(cfg, state, ref)
        if row["id"] not in {t["id"] for t in targets}:
            targets.append(row)
    origin = "worker" if own else "person"
    lines = []
    for item in items:
        for row in targets:
            mine = row["id"] == own
            added = artifacts.save(cfg, state, row, item, str(item.get("source_id") or origin), origin=origin,
                                   status="processed" if mine else "new")
            if added:
                log.info("%s: Added artifact \"%s\" from %s", workers.label(row), item["title"],
                         inbox.sender_text(state, _sender(env)))
            lines.append(f"{'added' if added else 'there already'}: {item['id']} in {workers.name(row)}"
                         + ("" if mine or not added else ", for its next turn"))
    return lines


# passing messages to workers

def tell(cfg: Config, state: State, tmux: Tmux, refs: list[str], message: str,
         env: Mapping[str, str] = os.environ) -> list[str]:
    """Pass a message to workers: into their inbox, and typed into an open window at once if its agent is idle and
    nobody is looking at it. Otherwise the worker gets it in its next turn."""
    message = message.strip()
    if not message:
        raise ConfigError("give the message with --message, or on stdin")
    targets = []
    for ref in refs:
        row = workers.resolve(cfg, state, ref)
        if row["id"] == env.get("VAHTOVIK_WORKER"):
            raise ConfigError("that is you: tell other workers, not yourself")
        if row["id"] not in {t["id"] for t in targets}:
            targets.append(row)
    lines = []
    for row in targets:
        inbox.send(state, row, _sender(env), message)
        window = tmux.window(row["id"])
        if window and inbox.deliver_typed(cfg, state, tmux, row, window):
            lines.append(f"{workers.name(row)}: typed the message into its open window")
        elif window and window.clients:
            lines.append(f"{workers.name(row)}: queued: a person is looking at its window; vahtovik types the "
                         "message in once they leave")
        elif window:
            lines.append(f"{workers.name(row)}: queued: its window is busy or just started; vahtovik types the "
                         "message in at a later run")
        else:
            lines.append(f"{workers.name(row)}: queued: it gets the message in a turn at the next run")
    return lines
