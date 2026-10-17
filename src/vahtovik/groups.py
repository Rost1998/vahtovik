"""Group folders: creation, the generated CLAUDE.md, and the per-group lock."""

from __future__ import annotations

from pathlib import Path

import yaml

from . import context_file, skills
from .config import Config, ConfigError, Watch
from .state import State
from .util import file_lock, render, schema_path

LOCK_NAME = ".lock"


def parse_ref(ref: str) -> tuple[str, str]:
    watch, sep, key = ref.partition("/")
    if not sep or not watch or not key:
        raise ConfigError(f"group reference must look like <watch>/<group-key>, got {ref!r}")
    return watch, key


def ensure_group(cfg: Config, state: State, watch: Watch, key: str, title: str) -> Path:
    group_dir = cfg.group_dir(watch.name, key)
    (group_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    if state.group(watch.name, key) is None:
        state.add_group(watch.name, key, title)
    context_file.create(group_dir / "context.md", title, watch.name, key)
    if not (group_dir / "CLAUDE.md").exists():
        write_claude_md(cfg, state, watch, key)
    return group_dir


def lock(group_dir: Path, wait: bool = False):
    return file_lock(group_dir / LOCK_NAME, wait=wait)


def _skill_line(cfg: Config, kind: str, name: str) -> str:
    path = cfg.skills_dir / kind / name / "SKILL.md"
    return f"`{path}`" if path.is_file() else f"(missing: {path})"


def write_claude_md(cfg: Config, state: State, watch: Watch, key: str) -> None:
    """Regenerated before every turn and every `open`, so the agent always sees current paths."""
    group = state.group(watch.name, key)
    sources = "\n".join(
        f"- `{s.id}`: skill `{s.skill}`, {_skill_line(cfg, 'sources', s.skill)}, "
        f"params `{yaml.safe_dump(s.params, default_flow_style=True, sort_keys=False).strip()}`"
        for s in watch.sources
    ) or "- none"
    channels = "\n".join(
        f"- `{name}`: skill `{cfg.channels[name].skill}`, formatting rules in "
        f"{_skill_line(cfg, 'channels', cfg.channels[name].skill)}"
        for name in watch.channels
    ) or "- none: this watch has no channels"
    process = ", ".join(f"`{p}`" for p in watch.process_files()) or "none"
    text = render(
        "group_claude_md.md",
        title=group["title"] if group else key,
        watch=watch.name,
        key=key,
        group_dir=cfg.group_dir(watch.name, key),
        root=cfg.root,
        main_skill=skills.main_skill(cfg.skills_dir).skill_md,
        monitor_md=watch.monitor_md,
        process_files=process,
        report_md=watch.report_md,
        watch_yaml=watch.watch_yaml,
        sources=sources,
        channels=channels,
        artifact_schema=schema_path("artifact"),
    )
    (cfg.group_dir(watch.name, key) / "CLAUDE.md").write_text(text)
