"""Group turns: resume each group's session to process its new artifacts and report."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from .. import artifacts, context_file, group_session, groups, skills
from ..claude_session import Claude
from ..config import Config, Watch
from ..state import State
from ..util import LockBusy, duration, human, plural, render

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def run(cfg: Config, state: State, claude: Claude, watch: Watch, run_id: str, raw_dir: Path) -> None:
    for key in state.pending_groups(watch.name, MAX_ATTEMPTS):
        try:
            with groups.lock(cfg.group_dir(watch.name, key)):
                turn(cfg, state, claude, watch, key, run_id, raw_dir)
        except LockBusy:
            log.warning("%s/%s: Skipped group turn: the session is open; the next run will process it",
                        watch.name, key)


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
    spec.env.update(VAHTOVIK_TURN=turn_id, VAHTOVIK_RUN=run_id)
    log.info("%s: Started group turn: %s, %s", spec.scope, plural(len(ids), "artifact"),
             group_session.describe(spec, note))
    state.start_turn(turn_id, run_id, watch.name, key, ids, context_file.fingerprint(context).work_log_entries)
    state.set_status(watch.name, key, "running")
    result = claude.run(spec, raw_dir)
    group_session.remember(claude, state, watch, key, spec)
    state.log_session(run_id, "group", watch.name, key, result)
    ok = _finish(state, watch, key, turn_id, pending, result.duration_s, result.error)
    artifacts.register_agent_files(cfg, state, watch, key)
    return ok


def _finish(state: State, watch: Watch, key: str, turn_id: str, pending, seconds: float, error: str | None) -> bool:
    """Close the turn after the session ended. The turn is done only if the agent ran `vahtovik turn`."""
    scope = f"{watch.name}/{key}"
    turn = state.turn(turn_id)
    if turn["finished_at"]:
        output = json.loads(turn["output"])
        status = output["status"] + (f" ({plural(len(output['questions']), 'question')})" if output["questions"] else "")
        log.info("%s: Finished group turn in %s: status %s, %s, $%.2f", scope, duration(seconds), status,
                 plural(len(state.turn_deliveries(turn_id)), "report"), _cost(state, turn))
        return True
    group = state.group(watch.name, key)
    if group["status"] == "failed" and group["status_reason"]:
        error = group["status_reason"]
    error = error or "the session ended before the agent ran `vahtovik turn`"
    state.mark_attempt_failed(watch.name, [r["id"] for r in pending], MAX_ATTEMPTS)
    state.finish_turn(turn_id, False, None, error)
    state.set_status(watch.name, key, "failed", error)
    attempt = max(r["attempts"] for r in pending) + 1
    log.error("%s: Failed group turn after %s (attempt %d of %d%s): %s", scope, duration(seconds),
              attempt, MAX_ATTEMPTS, ", giving up" if attempt >= MAX_ATTEMPTS else "", error)
    return False


def _cost(state: State, turn) -> float:
    row = state.row("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM sessions WHERE run_id = ? AND stage = 'group' "
                    "AND watch = ? AND target = ? AND started_at >= ?",
                    turn["run_id"], turn["watch"], turn["group_key"], turn["started_at"])
    return row["c"]
