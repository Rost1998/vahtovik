"""Find and read skills: skills/vahtovik (main), skills/watchdog, skills/sources/*, skills/channels/*."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

KINDS = ("sources", "channels")
MAIN_SKILL = "vahtovik"
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
    meta, body = parse_front_matter(skill_md.read_text())
    return Skill(kind, name, path, meta, body)


def load(skills_dir: Path, kind: str, name: str) -> Skill:
    if kind not in KINDS:
        raise SkillError(f"unknown skill kind {kind!r}")
    return _read(kind, name, skills_dir / kind / name)


def main_skill(skills_dir: Path) -> Skill:
    return _read("main", MAIN_SKILL, skills_dir / MAIN_SKILL)


def watchdog_skill(skills_dir: Path) -> Skill:
    return _read("watchdog", WATCHDOG_SKILL, skills_dir / WATCHDOG_SKILL)


def available(skills_dir: Path, kind: str) -> list[str]:
    folder = skills_dir / kind
    return sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").is_file()) if folder.is_dir() else []
