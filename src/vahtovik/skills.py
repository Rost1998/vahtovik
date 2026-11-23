"""Find and read skills: skills/shared, skills/watchdog, skills/sources/*, skills/channels/*, and the watches'
own skills in watches/<name>/skills/*."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .config import Watch

KINDS = ("sources", "channels")
SHARED_SKILL = "shared"
AGENT_SKILL = "vahtovik"
AGENT_TASKS = ("create-watch", "groups", "artifacts", "tell", "channel")
WATCHDOG_SKILL = "watchdog"


class SkillError(Exception):
    pass


@dataclass
class Skill:
    kind: str
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
        return [str(v) for v in self.meta.get(key) or []]


def parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}, text
    meta = yaml.safe_load(parts[0].removeprefix("---")) or {}
    return (meta if isinstance(meta, dict) else {}), parts[1].lstrip("-").lstrip("\n")


def _read(kind: str, name: str, path: Path) -> Skill:
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        raise SkillError(f"skill {kind}/{name} not found: {skill_md} is missing")
    try:
        meta, body = parse_front_matter(skill_md.read_text())
    except yaml.YAMLError as exc:
        raise SkillError(f"skill {kind}/{name}: the front matter of {skill_md} is not valid YAML: {exc}") from None
    return Skill(kind, name, path, meta, body)


def load(skills_dir: Path, kind: str, name: str) -> Skill:
    if kind not in KINDS:
        raise SkillError(f"unknown skill kind {kind!r}")
    return _read(kind, name, skills_dir / kind / name)


def shared_skill(skills_dir: Path) -> Skill:
    return _read("shared", SHARED_SKILL, skills_dir / SHARED_SKILL)


def agent_skills(skills_dir: Path) -> list[Skill]:
    """The vahtovik agent's skill and its sub-skills, one per kind of task."""
    return [_read("agent", AGENT_SKILL, skills_dir / AGENT_SKILL),
            *(_read("agent", task, skills_dir / AGENT_SKILL / task) for task in AGENT_TASKS)]


def watchdog_skill(skills_dir: Path) -> Skill:
    return _read("watchdog", WATCHDOG_SKILL, skills_dir / WATCHDOG_SKILL)


def available(skills_dir: Path, kind: str) -> list[str]:
    folder = skills_dir / kind
    return sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").is_file()) if folder.is_dir() else []


def _own_skill_dirs(watch: Watch) -> list[Path]:
    folder = watch.own_skills_dir
    return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


def watch_skills(watch: Watch) -> list[Skill]:
    """The watch's own skills. A folder without a readable SKILL.md is left out; `vahtovik check` reports it."""
    found = []
    for folder in _own_skill_dirs(watch):
        try:
            found.append(_read("watch", folder.name, folder))
        except SkillError:
            pass
    return found


def listing(found: list[Skill]) -> str:
    """One line per skill, for prompts and the group's CLAUDE.md."""
    return "\n".join(f"- `{s.name}` (`{s.skill_md}`): {s.description or '(no description)'}" for s in found) or "- none"


def watch_skill_problems(watch: Watch) -> list[str]:
    """What is wrong with the watch's own skills and their links in .claude/skills/."""
    problems = []
    own = watch.own_skills_dir.resolve()
    links = watch.skill_links_dir
    for folder in _own_skill_dirs(watch):
        name = folder.name
        if not (folder / "SKILL.md").is_file():
            problems.append(f"skill {name}: {folder / 'SKILL.md'} is missing")
            continue
        try:
            skill = _read("watch", name, folder)
        except SkillError as exc:
            problems.append(str(exc))
            continue
        if not skill.description:
            problems.append(f"skill {name}: {skill.skill_md} has no description in its front matter")
        if "name" in skill.meta and str(skill.meta["name"]) != name:
            problems.append(f"skill {name}: its front matter names it {skill.meta['name']!r}; use the folder name")
        link = links / name
        if not link.is_symlink():
            problems.append(f"skill {name}: no link {link}; make it with "
                            f"`ln -s ../../skills/{name} {link}` in the vahtovik folder")
    for link in sorted(links.iterdir()) if links.is_dir() else []:
        if not link.is_symlink():
            problems.append(f"{link} is not a link; keep the skill in {watch.own_skills_dir / link.name} "
                            "and link to it")
        elif not link.exists():
            problems.append(f"link {link} is broken: it points to {os.readlink(link)}")
        elif link.resolve() != (own / link.name).resolve():  # the skill folder may itself be a link to a skill
            problems.append(f"link {link} points to {link.resolve()}, not to {own / link.name}")
    return problems
