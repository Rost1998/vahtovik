"""Operations for the person and the vahtovik agent: the `vahtovik group ...`, `artifact add` and `tell` commands.

They change groups only through the database and vahtovik's own file helpers, and only while they hold the
locks of the groups they change, so no turn, window or other command uses those groups meanwhile."""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from . import artifacts, context_file, group_session, groups, windows
from .claude_session import Claude
from .config import Config, ConfigError, parse_interval
from .stages.group_turn import MAX_ATTEMPTS
from .state import State
from .tmux import Tmux
from .util import LockBusy, group_key, headings_down, human, load_schema, plural, utc_iso, write_atomic

log = logging.getLogger(__name__)

OFF = "off"
TELL_PREFIX = "Message from the person, sent through the vahtovik agent:"


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


# creating, merging and splitting groups

def _same_watch(ref: str, watch: str) -> str:
    other, key = groups.parse_ref(ref)
    if other != watch:
        raise ConfigError(f"{ref} is in another watch than {watch}: groups of different watches cannot mix")
    return key


def _new_key(state: State, watch: str, raw: str) -> str:
    key = group_key(raw)
    target = state.resolve_group(watch, key)
    if target != key:
        raise ConfigError(f"{watch}/{key} was merged into {watch}/{target}; use that group")
    return key


@contextmanager
def _hold(cfg: Config, state: State, tmux: Tmux, watch: str, keys: list[str]):
    """Hold the locks of the groups. Refuse if one has its window open, is busy, or has an unfinished turn."""
    with ExitStack() as stack:
        for key in keys:
            ref = f"{watch}/{key}"
            if tmux.window(ref):
                raise ConfigError(f"{ref} has its session window open; wait until vahtovik closes it, or close it")
            try:
                stack.enter_context(groups.lock(cfg.group_dir(watch, key)))
            except LockBusy:
                raise ConfigError(f"{ref} is busy; try again in a moment") from None
            turn = state.latest_turn(watch, key)
            if turn and turn["finished_at"] is None:
                raise ConfigError(f"{ref} has an unfinished turn; the watchdog ends it at the next run, try then")
        yield


def _move_file(cfg: Config, watch: str, key: str, filename: str, target: str) -> None:
    """Move an artifact file to the target group, with the new group key in it."""
    source = cfg.group_dir(watch, key) / "artifacts" / filename
    destination = cfg.group_dir(watch, target) / "artifacts" / filename
    if not source.is_file():
        return
    if not destination.exists():  # the same file name means the same artifact id: the target keeps its own
        data = json.loads(source.read_text())
        data["group_key"] = target
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(destination, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    source.unlink()


def create_group(cfg: Config, state: State, ref: str, title: str | None = None) -> list[str]:
    watch_name, raw = groups.parse_ref(ref)
    watch = cfg.watch(watch_name)
    key = _new_key(state, watch_name, raw)
    if state.group(watch_name, key):
        raise ConfigError(f"{watch_name}/{key} exists already")
    groups.ensure_group(cfg, state, watch, key, (title or "").strip() or key)
    lines = [f"created group {watch_name}/{key}"]
    if key != raw:
        lines.append(f"the key {raw!r} became {key!r}: group keys are lowercase words joined by hyphens")
    return lines


def _merged_entry(group, context: Path) -> str:
    key = group["key"]
    parts = [f"### {human()} · merged {key}",
             f"vahtovik merged group `{key}` (\"{group['title']}\") into this group. Its artifacts are in the "
             "Artifacts section now, and its record follows.",
             f"**Summary of `{key}`:**\n\n{headings_down(context_file.section_text(context, 'Summary')) or '_None._'}"]
    questions = context_file.section_text(context, "Open questions")
    if questions and questions != "_None._":
        parts.append(f"**Open questions of `{key}`:**\n\n{questions}")
    work = headings_down(context_file.section_text(context, "Work log")) or "_None._"
    parts.append(f"**Work log of `{key}`:**\n\n{work}")
    return "\n\n".join(parts)


def merge(cfg: Config, state: State, tmux: Tmux, refs: list[str], into: str) -> list[str]:
    """Merge groups into the group `into`. Each merged group's artifacts, reports, turns and costs move there,
    its record goes into the target's Work log, its folder goes to the watch's archive folder, and its key
    leads to the target from now on, so monitoring never brings it back."""
    watch_name, target, target_group = _group(cfg, state, into)
    keys = []
    for ref in refs:
        key = _same_watch(ref, watch_name)
        if key == target:
            raise ConfigError(f"{ref} cannot be merged into itself")
        if state.group(watch_name, key) is None:
            raise ConfigError(f"unknown group {ref}; see `vahtovik status --watch {watch_name}`")
        keys.append(key)
    if not keys:
        raise ConfigError("name at least one group to merge")
    target_context = cfg.group_dir(watch_name, target) / "context.md"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = []
    with _hold(cfg, state, tmux, watch_name, [target, *dict.fromkeys(keys)]):
        for key in dict.fromkeys(keys):
            group, folder = state.group(watch_name, key), cfg.group_dir(watch_name, key)
            rows = state.group_artifacts(watch_name, key)
            had = {r["id"] for r in state.group_artifacts(watch_name, target)}
            with state.transaction():
                state.merge_group(watch_name, key, target)
            for row in rows:
                _move_file(cfg, watch_name, key, row["file"], target)
            context = folder / "context.md"
            context_file.append(target_context, "Work log", _merged_entry(group, context))
            reports = context_file.section_text(context, "Reports")
            if reports:
                context_file.append(target_context, "Reports", reports)
            archive = cfg.data_dir / "watches" / watch_name / "archive" / f"{key}-{stamp}"
            archive.parent.mkdir(parents=True, exist_ok=True)
            folder.rename(archive)
            moved = sum(r["id"] not in had for r in rows)
            log.info("%s/%s: Merged into %s: %s moved, %d there already; the old folder is %s", watch_name, key,
                     target, plural(moved, "artifact"), len(rows) - moved, archive)
            lines.append(f"merged {watch_name}/{key} into {watch_name}/{target}: {plural(moved, 'artifact')} moved, "
                         f"{len(rows) - moved} there already; the old folder is {archive}")
        context_file.write_artifacts(target_context, state.group_artifacts(watch_name, target))
        state.touch_group(watch_name, target)
    waiting = len(state.pending_artifacts(watch_name, target, MAX_ATTEMPTS))
    if waiting:
        lines.append(f"{plural(waiting, 'artifact')} {'waits' if waiting == 1 else 'wait'} for the next turn of "
                     f"{watch_name}/{target}")
    return lines


def split(cfg: Config, state: State, tmux: Tmux, ref: str, ids: list[str], into: str,
          title: str | None = None) -> list[str]:
    """Move some artifacts of a group to another group, new or existing. There they wait for its next turn."""
    watch_name, key, group = _group(cfg, state, ref)
    watch = cfg.watch(watch_name)
    target = _new_key(state, watch_name, _same_watch(into, watch_name))
    if target == key:
        raise ConfigError("the artifacts are in that group already")
    rows = {r["id"]: r for r in state.group_artifacts(watch_name, key)}
    ids = list(dict.fromkeys(ids))
    missing = [i for i in ids if i not in rows]
    if not ids or missing:
        raise ConfigError(f"not artifacts of {ref}: {', '.join(missing)}" if missing else "name the artifacts to move")
    created = state.group(watch_name, target) is None
    with _hold(cfg, state, tmux, watch_name, [key]), ExitStack() as stack:
        if created:
            groups.ensure_group(cfg, state, watch, target, (title or "").strip() or target)
        stack.enter_context(_hold(cfg, state, tmux, watch_name, [target]))
        with state.transaction():
            for artifact_id in ids:
                state.move_artifact(watch_name, artifact_id, key, target)
        for artifact_id in ids:
            _move_file(cfg, watch_name, key, rows[artifact_id]["file"], target)
        titles = "; ".join(f"\"{rows[i]['title'] or i}\"" for i in ids)
        source_context = cfg.group_dir(watch_name, key) / "context.md"
        target_context = cfg.group_dir(watch_name, target) / "context.md"
        summary = headings_down(context_file.section_text(source_context, "Summary")) or "_None._"
        context_file.append(target_context, "Work log", "\n\n".join([
            f"### {human()} · split from {key}",
            f"vahtovik moved {plural(len(ids), 'artifact')} here from group `{key}` (\"{group['title']}\"): {titles}.",
            f"**Summary of `{key}` at that time:**\n\n{summary}"]))
        context_file.append(source_context, "Work log", "\n\n".join([
            f"### {human()} · split into {target}",
            f"vahtovik moved {plural(len(ids), 'artifact')} to group `{target}`: {titles}."]))
        for k, context in ((key, source_context), (target, target_context)):
            context_file.write_artifacts(context, state.group_artifacts(watch_name, k))
            state.touch_group(watch_name, k)
    log.info("%s: Moved %s to %s%s", ref, plural(len(ids), "artifact"), target, " (a new group)" if created else "")
    return [f"moved {plural(len(ids), 'artifact')} from {ref} to {watch_name}/{target}"
            + (" (a new group)" if created else "")
            + f"; {'it waits' if len(ids) == 1 else 'they wait'} for the next turn of {watch_name}/{target}"]


# artifacts from the person

def add_artifacts(cfg: Config, state: State, watch_name: str, keys: list[str], text: str) -> list[str]:
    """Add artifacts, given as JSON (one object or a list, by schemas/artifact.json), to each group named.
    An artifact a group has already is skipped there."""
    watch = cfg.watch(watch_name)
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
    for raw in keys:
        key = state.resolve_group(watch_name, group_key(raw))
        if state.group(watch_name, key) is None:
            raise ConfigError(f"unknown group {watch_name}/{raw}; create it first with `vahtovik group create`")
        targets.append(key)
    if not targets:
        raise ConfigError("name at least one group with --group")
    lines = []
    for item in items:
        for key in dict.fromkeys(targets):
            added = artifacts.register(cfg, state, watch, {**item, "group_key": key},
                                       str(item.get("source_id") or "person"), origin="person", any_group=False)
            if added:
                log.info("%s/%s: Added artifact \"%s\" from the person", watch_name, key, item["title"])
            lines.append(f"{'added' if added else 'there already'}: {item['id']} in {watch_name}/{key}")
    return lines


# passing instructions to group sessions

def tell(cfg: Config, state: State, tmux: Tmux, refs: list[str], message: str) -> list[str]:
    """Pass the person's message to group sessions: type it into an open window, or start the session with it."""
    message = message.strip()
    if not message:
        raise ConfigError("give the message with --message, or on stdin")
    found = []
    for ref in refs:
        watch_name, raw = groups.parse_ref(ref)
        watch = cfg.watch(watch_name)
        key = state.resolve_group(watch_name, raw)
        if state.group(watch_name, key) is None:
            raise ConfigError(f"unknown group {ref}; see `vahtovik status --watch {watch_name}`")
        if (watch.name, key) not in {(w.name, k) for w, k in found}:
            found.append((watch, key))
    text = f"{TELL_PREFIX} {message}"
    claude = Claude(cfg)
    lines = []
    for watch, key in found:
        ref = f"{watch.name}/{key}"
        group, window = state.group(watch.name, key), tmux.window(ref)
        if window and window.clients:
            lines.append(f"{ref}: skipped: a person is looking at its window; ask them, or type the message there")
        elif window and group["status"] == "maintenance":
            lines.append(f"{ref}: skipped: it needs a person first ({group['status_reason']}); "
                         f"see `vahtovik open {ref}`")
        elif window:
            tmux.send_text(window.id, " ".join(text.split()))
            log.info("%s: Typed a message from the person into the session window: %s", ref, message[:100])
            lines.append(f"{ref}: typed the message into its open window")
        else:
            try:
                with groups.lock(cfg.group_dir(watch.name, key)):
                    pass
            except LockBusy:
                lines.append(f"{ref}: skipped: the group is busy; try again in a moment")
                continue
            groups.write_claude_md(cfg, state, watch, key)
            spec, note = group_session.spec_for(cfg, claude, state, watch, key)
            spec.prompt = f"{note}\n\n{text}" if note else text
            window_id = windows.launch(cfg, state, tmux, claude, spec, "tell")
            log.info("%s: Started the session with a message from the person, %s, tmux window %s: %s", ref,
                     group_session.describe(spec, note), window_id, message[:100])
            lines.append(f"{ref}: started its session with the message, tmux window {window_id}")
    return lines
