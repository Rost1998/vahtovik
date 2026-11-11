"""Monitoring: one throwaway session per source, then register the new artifacts.

An `ai` source's session collects the items itself, by the source skill. A `script` source's items come from the
source type's `scan` script first, and a session starts only when it finds new items."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml

from .. import artifacts, memory, skills
from ..claude_session import Claude, SessionSpec
from ..config import Config, Source, Watch
from ..state import State
from ..util import StateBackup, duration, human, load_schema, plural, render, run_json_script

log = logging.getLogger(__name__)

MAX_GROUPS_IN_PROMPT = 200
SCAN_TIMEOUT = 300
COLLECT_AI = "Collect new items from the source by following the source skill."
COLLECT_SCRIPT = """vahtovik ran the source's scan script, and it found the new items below. Do not run the source's \
scripts yourself: they would find nothing new. Open an item's link only when the monitoring rules need more detail.

```json
{items}
```"""


def _groups_text(state: State, watch: Watch) -> str:
    rows = state.groups(watch.name, MAX_GROUPS_IN_PROMPT)
    return "\n".join(f"- `{r['key']}`: {r['title']} (updated {r['updated_at'][:10]})" for r in rows) or "- none yet"


def scan(cfg: Config, watch: Watch, source: Source, skill: skills.Skill, workdir: Path) -> tuple[list | None, str | None]:
    """Run the source type's scan script. Returns (the new items, None), or (None, what went wrong)."""
    env = {**os.environ, **watch.source_env(source), "VAHTOVIK_STATE_DIR": str(workdir),
           "VAHTOVIK_PARAMS": json.dumps(source.params), "VAHTOVIK_WATCH": watch.name, "VAHTOVIK_SOURCE": source.id}
    data, error = run_json_script(skill.dir / "scan", env, cfg.root, SCAN_TIMEOUT)
    if error:
        return None, error
    items = data.get("items")
    if not isinstance(items, list) or any(not isinstance(i, dict) or not str(i.get("id") or "").strip() for i in items):
        return None, "scan printed no `items` list of objects with an `id` each"
    return items, None


def run(cfg: Config, state: State, claude: Claude, watch: Watch, run_id: str, raw_dir: Path,
        sources: list[Source] | None = None) -> int:
    """Monitor the sources (default: all of the watch's). Returns how many new artifacts they brought."""
    shared = skills.shared_skill(cfg.skills_dir).body
    schema = load_schema("monitor")
    rules = watch.monitor_md.read_text() if watch.monitor_md.is_file() else "(monitor.md is missing)"
    total = 0
    for source in watch.sources if sources is None else sources:
        scope = f"{watch.name}/{source.id}"
        try:
            skill = skills.load(cfg.skills_dir, "sources", source.skill)
        except skills.SkillError as exc:
            log.error("%s: Failed monitoring: %s", scope, exc)
            state.source_failed(watch.name, source.id, str(exc))
            continue
        workdir = cfg.source_dir(watch.name, source.id)
        # The backup stays until the new artifacts are saved. Artifacts saved already are dropped by id.
        backup = StateBackup(workdir)
        if backup.recover():
            log.warning("%s: Restored the source state: the last run stopped before it saved the artifacts", scope)
        backup.take()
        collect, found_by_script = COLLECT_AI, ""
        if source.scan == "script":
            items, error = scan(cfg, watch, source, skill, workdir)
            if error:
                backup.restore()
                log.error("%s: Failed monitoring: %s", scope, error)
                state.source_failed(watch.name, source.id, error)
                continue
            if not items:  # nothing new: no session, and no journal line
                backup.drop()
                state.source_ok(watch.name, source.id)
                continue
            collect = COLLECT_SCRIPT.format(items=json.dumps(items, indent=2, ensure_ascii=False))
            found_by_script = f": {plural(len(items), 'new item')} from its scan script"
        recent = state.recent_artifacts(watch.name, source.id)
        prompt = render(
            "monitor.md",
            watch=watch.name, source_id=source.id, run_id=run_id, now=human(), workdir=workdir,
            skill_name=skill.name, skill_dir=skill.dir, skill_text=skill.body.strip(),
            params=yaml.safe_dump(source.params, sort_keys=False, allow_unicode=True).strip() or "{}",
            monitor_rules=rules.strip(), memory=memory.prompt_text(watch), groups=_groups_text(state, watch),
            recent="\n".join(f"- `{r['id']}`: {r['title']}" for r in recent) or "- none yet",
            collect=collect,
        )
        spec = SessionSpec(
            stage="monitor", workdir=workdir, prompt=prompt, settings=cfg.stage(watch, "monitor"),
            schema=schema, system_prompt=shared, mode="throwaway",
            env={**watch.source_env(source),
                 "VAHTOVIK_WATCH": watch.name, "VAHTOVIK_SOURCE": source.id, "VAHTOVIK_RUN": run_id},
            scope=scope,
        )
        log.info("%s: Started monitoring (%s)%s", scope, skill.name, found_by_script)
        result = claude.run(spec, raw_dir)
        state.log_session(run_id, "monitor", watch.name, source.id, result)
        if not result.ok:
            backup.restore()
            log.error("%s: Failed monitoring after %s: %s", scope, duration(result.duration_s), result.error)
            state.source_failed(watch.name, source.id, result.error or "unknown error")
            continue
        found = result.output["artifacts"]
        new = sum(artifacts.register(cfg, state, watch, item, source.id) for item in found)
        state.source_ok(watch.name, source.id)
        backup.drop()
        notes = result.output.get("notes", "").strip()
        log.info("%s: Finished monitoring in %s: %d found, %d new, $%.2f%s", scope, duration(result.duration_s),
                 len(found), new, result.cost_usd, f" ({notes[:100]})" if notes else "")
        total += new
    return total
