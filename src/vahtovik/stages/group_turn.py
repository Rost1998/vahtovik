"""Group turns: resume each group's session to process its new artifacts and report."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from .. import artifacts, context_file, group_session, groups, skills
from ..claude_session import Claude
from ..config import Config, Watch
from ..state import State
from ..util import LockBusy, human, load_schema, render

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def run(cfg: Config, state: State, claude: Claude, watch: Watch, run_id: str, raw_dir: Path) -> None:
    for key in state.pending_groups(watch.name, MAX_ATTEMPTS):
        try:
            with groups.lock(cfg.group_dir(watch.name, key)):
                turn(cfg, state, claude, watch, key, run_id, raw_dir)
        except LockBusy:
            log.info("%s/%s: the session is open elsewhere; the next run will process it", watch.name, key)


def _read(path: Path) -> str:
    return path.read_text().strip() if path.is_file() else f"({path.name} is missing)"


def _artifacts_text(rows) -> str:
    return "\n".join(f"- `artifacts/{r['file']}`: {r['title']} ({r['url'] or 'no link'}, source `{r['source_id']}`)"
                     for r in rows)


def _process_text(watch: Watch) -> str:
    files = watch.process_files()
    return "\n\n".join(f"### {p.name}\n\n{p.read_text().strip()}" for p in files) or "(no process/*.md files)"


def _channels_text(cfg: Config, watch: Watch) -> str:
    parts = []
    for name in watch.channels:
        try:
            body = skills.load(cfg.skills_dir, "channels", cfg.channels[name].skill).body.strip()
        except skills.SkillError as exc:
            body = f"(skill missing: {exc})"
        parts.append(f"### `{name}`\n\n{body}")
    return "\n\n".join(parts) or "This watch has no channels; do not report."


def _deliveries_text(rows) -> str:
    lines = []
    for r in rows:
        status = f"sent ({r['external_id']})" if r["status"] == "sent" else f"{r['status']}: {r['last_error']}"
        lines.append(f"- delivery {r['id']} to `{r['channel']}`, \"{r['subject']}\": {status}, "
                     f"attempts {r['attempts']}")
    return "\n".join(lines) or "- none"


def turn(cfg: Config, state: State, claude: Claude, watch: Watch, key: str, run_id: str, raw_dir: Path) -> bool:
    pending = state.pending_artifacts(watch.name, key, MAX_ATTEMPTS)
    ids = [r["id"] for r in pending]
    context = cfg.group_dir(watch.name, key) / "context.md"
    groups.write_claude_md(cfg, state, watch, key)
    spec, note = group_session.spec_for(cfg, claude, state, watch, key)
    last = state.last_turn(watch.name, key)
    turn_id = uuid.uuid4().hex[:12]
    now = human()
    spec.prompt = render(
        "group_turn.md",
        run_id=run_id, turn_id=turn_id, now=now, session_note=note or "",
        artifacts=_artifacts_text(pending), process_rules=_process_text(watch),
        report_rules=_read(watch.report_md), channels=_channels_text(cfg, watch),
        deliveries=_deliveries_text(state.deliveries_since(watch.name, key, last["started_at"] if last else None)),
    )
    spec.schema = load_schema("group_turn")
    spec.env["VAHTOVIK_TURN"] = turn_id
    before = context_file.fingerprint(context)
    state.start_turn(turn_id, run_id, watch.name, key)
    result = claude.run(spec, raw_dir)
    group_session.remember(claude, state, watch, key, spec)
    state.log_session(run_id, "group", watch.name, key, result)

    ok = result.ok and context_file.was_updated(before, context_file.fingerprint(context))
    error = result.error if not result.ok else (None if ok else "the agent did not add a Work log entry to context.md")
    if ok:
        state.mark_processed(watch.name, ids)
        state.touch_group(watch.name, key)
        claimed = {r["delivery_id"] for r in result.output["reports"]}
        recorded = {r["id"] for r in state.turn_deliveries(turn_id)}
        if claimed != recorded:
            log.warning("%s/%s: reports in the answer %s differ from deliveries recorded %s",
                        watch.name, key, sorted(claimed), sorted(recorded))
        log.info("%s/%s: processed %d artifact(s), importance %s, %d report(s)",
                 watch.name, key, len(ids), result.output["importance"], len(recorded))
    else:
        state.mark_attempt_failed(watch.name, ids, MAX_ATTEMPTS)
        log.error("%s/%s: turn failed: %s", watch.name, key, error)
    state.finish_turn(turn_id, ok, result.output, error)
    artifacts.register_agent_files(cfg, state, watch, key)
    return ok
