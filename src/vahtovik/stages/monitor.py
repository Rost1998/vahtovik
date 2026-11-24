"""Monitoring: one throwaway session per source of a poller, then give the new artifacts to workers.

An `ai` source's session collects the items itself, by the source skill. A `script` source's items come from the
source skill's `scan` script first, and a session starts only when it finds new items."""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

import yaml

from .. import artifacts, knowledge, memory
from ..claude_session import Claude, SessionSpec
from ..config import Config, Poller, Source
from ..state import State
from ..util import StateBackup, duration, human, load_schema, plural, render, run_json_script

log = logging.getLogger(__name__)

MAX_WORKERS_IN_PROMPT = 200
SCAN_TIMEOUT = 300
COLLECT_AI = "Collect new items from the source by following the source skill."
COLLECT_SCRIPT = """vahtovik ran the source's scan script, and it found the new items below. Do not run the source's \
scripts yourself: they would find nothing new. Open an item's link only when the poller's rules need more detail.

```json
{items}
```"""


def _workers_text(state: State, poller: Poller) -> str:
    rows = state.workers(poller.roles)[:MAX_WORKERS_IN_PROMPT]
    return "\n".join(f"- role `{r['role']}`, key `{r['key']}`: {r['title']} ({r['status']}, updated "
                     f"{r['updated_at'][:10]})" for r in rows) or "- none yet"


def _roles_text(cfg: Config, poller: Poller) -> str:
    return "\n".join(f"- `{name}`{' (the default)' if index == 0 else ''}: "
                     f"{cfg.roles[name].description or '(no description)'}"
                     for index, name in enumerate(poller.roles))


def _memory_text(cfg: Config, poller: Poller) -> str:
    return "\n\n".join(f"### Role `{name}`\n\n{memory.prompt_text(cfg.roles[name])}" for name in poller.roles)


def schema(poller: Poller) -> dict:
    """The monitoring answer's schema, with the poller's roles as the only roles an artifact may name."""
    found = copy.deepcopy(load_schema("monitor"))
    found["properties"]["artifacts"]["items"]["properties"]["role"]["enum"] = list(poller.roles)
    return found


def source_skills(cfg: Config, source: Source) -> list[knowledge.Skill]:
    """A monitoring session's skills: the shared ones and the source's. Raises SkillError for a missing source
    skill."""
    skill = knowledge.load(cfg, source.skill)
    return [*(s for s in knowledge.shared(cfg) if s.name != skill.name), skill]


def scan(cfg: Config, poller: Poller, source: Source, skill: knowledge.Skill, found: list[knowledge.Skill],
         workdir: Path) -> tuple[list | None, str | None]:
    """Run the source skill's scan script. Returns (the new items, None), or (None, what went wrong)."""
    env = {**os.environ, **knowledge.session_env(cfg, found, source.credentials), "VAHTOVIK_STATE_DIR": str(workdir),
           "VAHTOVIK_PARAMS": json.dumps(source.params), "VAHTOVIK_POLLER": poller.name, "VAHTOVIK_SOURCE": source.id}
    data, error = run_json_script(skill.dir / "scan", env, cfg.root, SCAN_TIMEOUT)
    if error:
        return None, error
    items = data.get("items")
    if not isinstance(items, list) or any(not isinstance(i, dict) or not str(i.get("id") or "").strip() for i in items):
        return None, "scan printed no `items` list of objects with an `id` each"
    return items, None


def run(cfg: Config, state: State, claude: Claude, poller: Poller, run_id: str, raw_dir: Path,
        sources: list[Source] | None = None) -> int:
    """Monitor the sources (default: all of the poller's). Returns how many new artifacts they brought."""
    rules = poller.poller_md.read_text() if poller.poller_md.is_file() else "(poller.md is missing)"
    total = 0
    for source in poller.sources if sources is None else sources:
        scope = f"{poller.name}/{source.id}"
        try:
            found = source_skills(cfg, source)
            rules_skill = knowledge.worker_rules(cfg)
        except knowledge.SkillError as exc:
            log.error("%s: Failed monitoring: %s", scope, exc)
            state.source_failed(poller.name, source.id, str(exc))
            continue
        skill = found[-1]
        workdir = cfg.source_dir(poller.name, source.id)
        # The backup stays until the new artifacts are saved. Artifacts saved already are dropped by id.
        backup = StateBackup(workdir)
        if backup.recover():
            log.warning("%s: Restored the source state: the last run stopped before it saved the artifacts", scope)
        backup.take()
        collect, found_by_script = COLLECT_AI, ""
        if source.scan == "script":
            items, error = scan(cfg, poller, source, skill, found, workdir)
            if error:
                backup.restore()
                log.error("%s: Failed monitoring: %s", scope, error)
                state.source_failed(poller.name, source.id, error)
                continue
            if not items:  # nothing new: no session, and no journal line
                backup.drop()
                state.source_ok(poller.name, source.id)
                continue
            collect = COLLECT_SCRIPT.format(items=json.dumps(items, indent=2, ensure_ascii=False))
            found_by_script = f": {plural(len(items), 'new item')} from its scan script"
        links = cfg.poller_dir(poller.name) / "sessions" / source.id  # where Claude Code finds the session's skills
        knowledge.link(links, found)
        recent = state.recent_artifacts(poller.name, source.id)
        prompt = render(
            "monitor.md",
            poller=poller.name, source_id=source.id, run_id=run_id, now=human(), workdir=workdir,
            skill_name=skill.name, skill_dir=skill.dir, skill_text=skill.body.strip(),
            params=yaml.safe_dump(source.params, sort_keys=False, allow_unicode=True).strip() or "{}",
            poller_rules=rules.strip(), skills=knowledge.listing(found), roles=_roles_text(cfg, poller),
            memory=_memory_text(cfg, poller), workers=_workers_text(state, poller),
            recent="\n".join(f"- `{r['id']}`: {r['title']}" for r in recent) or "- none yet",
            collect=collect,
        )
        spec = SessionSpec(
            stage="monitor", workdir=workdir, prompt=prompt, settings=cfg.stage("monitor", poller),
            schema=schema(poller), system_prompt=rules_skill.body, mode="throwaway",
            env={**knowledge.session_env(cfg, found, source.credentials),
                 "VAHTOVIK_POLLER": poller.name, "VAHTOVIK_SOURCE": source.id, "VAHTOVIK_RUN": run_id},
            scope=scope, add_dirs=[links], account=poller.account,
        )
        log.info("%s: Started monitoring (%s)%s", scope, skill.name, found_by_script)
        result = claude.run(spec, raw_dir)
        state.log_session(run_id, "monitor", poller.name, source.id, result)
        if not result.ok:
            backup.restore()
            log.error("%s: Failed monitoring after %s: %s", scope, duration(result.duration_s), result.error)
            state.source_failed(poller.name, source.id, result.error or "unknown error")
            continue
        items = result.output["artifacts"]
        new = sum(artifacts.from_poller(cfg, state, poller, item, source.id) for item in items)
        state.source_ok(poller.name, source.id)
        backup.drop()
        notes = result.output.get("notes", "").strip()
        log.info("%s: Finished monitoring in %s: %d found, %d new, $%.2f%s", scope, duration(result.duration_s),
                 len(items), new, result.cost_usd, f" ({notes[:100]})" if notes else "")
        total += new
    return total

