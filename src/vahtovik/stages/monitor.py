"""Monitoring: one throwaway session per source, then register the new artifacts."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

import yaml

from .. import artifacts, skills
from ..claude_session import Claude, SessionSpec
from ..config import Config, Watch
from ..state import State
from ..util import duration, human, load_schema, render

log = logging.getLogger(__name__)

MAX_GROUPS_IN_PROMPT = 200


def _groups_text(state: State, watch: Watch) -> str:
    rows = state.groups(watch.name, MAX_GROUPS_IN_PROMPT)
    return "\n".join(f"- `{r['key']}`: {r['title']} (updated {r['updated_at'][:10]})" for r in rows) or "- none yet"


def run(cfg: Config, state: State, claude: Claude, watch: Watch, run_id: str, raw_dir: Path) -> int:
    main = skills.main_skill(cfg.skills_dir).body
    schema = load_schema("monitor")
    rules = watch.monitor_md.read_text() if watch.monitor_md.is_file() else "(monitor.md is missing)"
    total = 0
    for source in watch.sources:
        try:
            skill = skills.load(cfg.skills_dir, "sources", source.skill)
        except skills.SkillError as exc:
            log.error("%s/%s: Failed monitoring: %s", watch.name, source.id, exc)
            state.source_failed(watch.name, source.id, str(exc))
            continue
        workdir = cfg.source_dir(watch.name, source.id)
        workdir.mkdir(parents=True, exist_ok=True)
        recent = state.recent_artifacts(watch.name, source.id)
        prompt = render(
            "monitor.md",
            watch=watch.name, source_id=source.id, run_id=run_id, now=human(), workdir=workdir,
            skill_name=skill.name, skill_dir=skill.dir, skill_text=skill.body.strip(),
            params=yaml.safe_dump(source.params, sort_keys=False, allow_unicode=True).strip() or "{}",
            monitor_rules=rules.strip(), groups=_groups_text(state, watch),
            recent="\n".join(f"- `{r['id']}`: {r['title']}" for r in recent) or "- none yet",
        )
        spec = SessionSpec(
            stage="monitor", workdir=workdir, prompt=prompt, settings=cfg.stage(watch, "monitor"),
            schema=schema, system_prompt=main, mode="throwaway",
            env={"VAHTOVIK_WATCH": watch.name, "VAHTOVIK_SOURCE": source.id, "VAHTOVIK_RUN": run_id},
            scope=f"{watch.name}/{source.id}",
        )
        log.info("%s: Started monitoring (%s)", spec.scope, skill.name)
        # Source scripts move their "last seen" markers during the session.
        # If the session fails, put the state folder back so no item is lost.
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "state"
            shutil.copytree(workdir, backup)
            result = claude.run(spec, raw_dir)
            state.log_session(run_id, "monitor", watch.name, source.id, result)
            if not result.ok:
                shutil.rmtree(workdir)
                shutil.copytree(backup, workdir)
        if not result.ok:
            log.error("%s: Failed monitoring after %s: %s", spec.scope, duration(result.duration_s), result.error)
            state.source_failed(watch.name, source.id, result.error or "unknown error")
            continue
        found = result.output["artifacts"]
        new = sum(artifacts.register(cfg, state, watch, item, source.id) for item in found)
        state.source_ok(watch.name, source.id)
        notes = result.output.get("notes", "").strip()
        log.info("%s: Finished monitoring in %s: %d found, %d new, $%.2f%s", spec.scope, duration(result.duration_s),
                 len(found), new, result.cost_usd, f" ({notes[:100]})" if notes else "")
        total += new
    return total
