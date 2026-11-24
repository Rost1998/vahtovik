"""The knowledge layer: the skills that workers and monitoring sessions get, and the credentials that skills need.

    knowledge/shared/<skill>/          every worker and every monitoring session has these
    knowledge/skills/<skill>/          specialized skills: a role, a spawn, a poller source or a channel names them
    knowledge/credentials/<name>.env   secrets, never in git; skills, roles, sources and channels name them

A skill is a folder with SKILL.md (front matter with `name`, `description`, and optionally `credentials`,
`required_env` and `required_params`) and any docs and scripts. vahtovik's own skills, which no worker gets, are in
skills/: the vahtovik agent's and the watchdog's.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .config import credentials_file, read_env_file
from .util import parse_front_matter

if TYPE_CHECKING:
    from .config import Config

SHARED = "shared"
SPECIALIZED = "skills"
WORKER_RULES = "vahtovik"  # knowledge/shared/vahtovik: the rules of every worker, also in its system prompt
AGENT_SKILL = "vahtovik"
AGENT_TASKS = ("knowledge", "roles", "pollers", "workers", "artifacts", "tell", "channel")
WATCHDOG_SKILL = "watchdog"
LINKS = Path(".claude") / "skills"  # where Claude Code finds a session's skills, in its folder


class SkillError(Exception):
    pass


@dataclass
class Skill:
    kind: str  # shared, skills, agent or watchdog
    name: str
    dir: Path
    meta: dict
    body: str

    @property
    def skill_md(self) -> Path:
        return self.dir / "SKILL.md"

    @property
    def description(self) -> str:
        return str(self.meta.get("description", ""))

    def list_meta(self, key: str) -> list[str]:
        value = self.meta.get(key) or []
        return [str(v) for v in (value if isinstance(value, list) else [value])]

    @property
    def credentials(self) -> list[str]:
        return self.list_meta("credentials")


def _read(kind: str, name: str, path: Path) -> Skill:
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        raise SkillError(f"skill {name} not found: {skill_md} is missing")
    try:
        meta, body = parse_front_matter(skill_md.read_text())
    except yaml.YAMLError as exc:
        raise SkillError(f"skill {name}: the front matter of {skill_md} is not valid YAML: {exc}") from None
    return Skill(kind, name, path, meta, body)


def load(cfg: Config, name: str) -> Skill:
    """A specialized skill, from knowledge/skills."""
    return _read(SPECIALIZED, name, cfg.knowledge_dir / SPECIALIZED / name)


def _folders(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


def shared(cfg: Config) -> list[Skill]:
    """The shared skills. A folder without a readable SKILL.md is left out; `vahtovik check` reports it."""
    found = []
    for folder in _folders(cfg.knowledge_dir / SHARED):
        try:
            found.append(_read(SHARED, folder.name, folder))
        except SkillError:
            pass
    return found


def worker_rules(cfg: Config) -> Skill:
    """The rules of every worker and monitoring session: knowledge/shared/vahtovik."""
    return _read(SHARED, WORKER_RULES, cfg.knowledge_dir / SHARED / WORKER_RULES)


def available(cfg: Config) -> list[str]:
    """The names of the specialized skills."""
    return [p.name for p in _folders(cfg.knowledge_dir / SPECIALIZED) if (p / "SKILL.md").is_file()]


def agent_skills(cfg: Config) -> list[Skill]:
    """The vahtovik agent's skill and its sub-skills, one per kind of task."""
    folder = cfg.skills_dir / AGENT_SKILL
    return [_read("agent", AGENT_SKILL, folder), *(_read("agent", task, folder / task) for task in AGENT_TASKS)]


def watchdog_skill(cfg: Config) -> Skill:
    return _read("watchdog", WATCHDOG_SKILL, cfg.skills_dir / WATCHDOG_SKILL)


def collect(cfg: Config, names: list[str]) -> tuple[list[Skill], list[str]]:
    """The shared skills and the named specialized ones, each once, and the problems of the named ones that
    cannot be read. A specialized skill with a shared skill's name stands in for it."""
    found: dict[str, Skill] = {s.name: s for s in shared(cfg)}
    problems = []
    for name in dict.fromkeys(names):
        try:
            found[name] = load(cfg, name)
        except SkillError as exc:
            problems.append(str(exc))
    return list(found.values()), problems


def listing(found: list[Skill]) -> str:
    """One line per skill, for prompts and a worker's CLAUDE.md."""
    return "\n".join(f"- `{s.name}` (`{s.skill_md}`): {s.description or '(no description)'}" for s in found) or "- none"


def credentials(cfg: Config, names: list[str]) -> dict[str, str]:
    """The variables of the named credential files, in order: later files win. A missing file gives nothing;
    `vahtovik check` reports it."""
    values: dict[str, str] = {}
    for name in dict.fromkeys(names):
        values.update(read_env_file(credentials_file(cfg, name)))
    return values


def session_env(cfg: Config, found: list[Skill], more: list[str] = ()) -> dict[str, str]:
    """The credentials of a session that has these skills: each skill's own, then `more` (a role's or a source's),
    which win."""
    return credentials(cfg, [*(name for s in found for name in s.credentials), *more])


def missing_env(cfg: Config, found: list[Skill], more: list[str] = (), env: dict[str, str] | None = None) -> list[str]:
    """The variables that the skills' `required_env` names but neither their credentials, `env`, nor vahtovik's
    own environment set."""
    have = {**session_env(cfg, found, more), **(env or {})}
    needed = [var for s in found for var in s.list_meta("required_env")]
    return sorted({v for v in needed if not have.get(v) and not os.environ.get(v)})


def missing_credentials(cfg: Config, names: list[str]) -> list[str]:
    """The named credential files that do not exist."""
    return [n for n in dict.fromkeys(names) if not credentials_file(cfg, n).is_file()]


def link(folder: Path, found: list[Skill]) -> None:
    """Make folder/.claude/skills hold one link to each skill and nothing else, so Claude Code finds exactly
    these skills in a session that starts in the folder or gets it with --add-dir."""
    links = folder / LINKS
    links.mkdir(parents=True, exist_ok=True)
    wanted = {s.name: s.dir.resolve() for s in found}
    for path in links.iterdir():
        if path.name not in wanted or not path.is_symlink() or path.resolve() != wanted[path.name]:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    for name, target in wanted.items():
        if not (links / name).is_symlink():
            (links / name).symlink_to(target, target_is_directory=True)


def problems(cfg: Config) -> list[str]:
    """What is wrong with the knowledge layer: unreadable skills, a skill without a description or with another
    name in its front matter, and a specialized skill with a shared skill's name. Missing credential files count
    only for the skills that are used, so `vahtovik check` looks for them where roles, sources and channels use
    them."""
    found = []
    for kind in (SHARED, SPECIALIZED):
        for folder in _folders(cfg.knowledge_dir / kind):
            try:
                skill = _read(kind, folder.name, folder)
            except SkillError as exc:
                found.append(f"{kind} {exc}")
                continue
            where = f"{kind} skill {skill.name}"
            if not skill.description:
                found.append(f"{where}: {skill.skill_md} has no description in its front matter")
            if "name" in skill.meta and str(skill.meta["name"]) != skill.name:
                found.append(f"{where}: its front matter names it {skill.meta['name']!r}; use the folder name")
            if kind == SPECIALIZED and (cfg.knowledge_dir / SHARED / skill.name).is_dir():
                found.append(f"{where}: a shared skill has the same name")
    try:
        worker_rules(cfg)
    except SkillError as exc:
        found.append(f"the worker rules: {exc}")
    return found
