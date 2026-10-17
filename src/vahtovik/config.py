"""Load and validate vahtovik.yaml and watches/<name>/watch.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

CONFIG_FILE = "vahtovik.yaml"
WATCH_FILE = "watch.yaml"
STAGES = ("monitor", "group")
AUTH_MODES = ("subscription", "api-key")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class StageSettings:
    model: str
    effort: str | None = None
    max_budget_usd: float | None = None


@dataclass
class ClaudeSettings:
    auth: str = "subscription"
    binary: str = "claude"
    config_dir: Path | None = None
    model: str = "claude-opus-5-5"
    timeout_seconds: int = 900
    stages: dict[str, dict] = field(default_factory=dict)


@dataclass
class Channel:
    name: str
    skill: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Source:
    id: str
    skill: str
    params: dict = field(default_factory=dict)


@dataclass
class Watch:
    name: str
    dir: Path
    enabled: bool
    interval: timedelta
    sources: list[Source]
    channels: list[str]
    claude: dict[str, dict]

    @property
    def watch_yaml(self) -> Path:
        return self.dir / WATCH_FILE

    @property
    def monitor_md(self) -> Path:
        return self.dir / "monitor.md"

    @property
    def report_md(self) -> Path:
        return self.dir / "report.md"

    def process_files(self) -> list[Path]:
        process_dir = self.dir / "process"
        return sorted(process_dir.glob("*.md")) if process_dir.is_dir() else []


@dataclass
class Config:
    root: Path
    claude: ClaudeSettings
    channels: dict[str, Channel]
    watches: dict[str, Watch]
    data_dir: Path
    skills_dir: Path
    watches_dir: Path

    def stage(self, watch: Watch | None, stage: str) -> StageSettings:
        merged: dict = {"model": self.claude.model}
        merged.update(self.claude.stages.get(stage, {}))
        if watch is not None:
            merged.update(watch.claude.get(stage, {}))
        budget = merged.get("max_budget_usd")
        return StageSettings(
            model=str(merged["model"]),
            effort=merged.get("effort"),
            max_budget_usd=float(budget) if budget is not None else None,
        )

    def watch(self, name: str) -> Watch:
        if name not in self.watches:
            raise ConfigError(f"unknown watch {name!r}; known: {', '.join(sorted(self.watches)) or 'none'}")
        return self.watches[name]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vahtovik.db"

    def group_dir(self, watch: str, key: str) -> Path:
        return self.data_dir / "watches" / watch / "groups" / key

    def source_dir(self, watch: str, source_id: str) -> Path:
        return self.data_dir / "watches" / watch / "sources" / source_id

    def runs_dir(self, run_id: str) -> Path:
        return self.data_dir / "runs" / run_id


def find_root(start: Path | None = None) -> Path:
    if os.environ.get("VAHTOVIK_HOME"):
        return Path(os.environ["VAHTOVIK_HOME"]).resolve()
    here = (start or Path.cwd()).resolve()
    for path in (here, *here.parents):
        if (path / CONFIG_FILE).is_file():
            return path
    raise ConfigError(f"{CONFIG_FILE} not found in {here} or its parents (or set VAHTOVIK_HOME)")


def load_env_file(root: Path) -> None:
    """Load KEY=VALUE lines from .env without overriding the real environment."""
    path = root / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def parse_interval(value: object) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(value))
    if not match:
        raise ConfigError(f"bad interval {value!r}: use a number and s, m, h or d, for example 30m")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match[2]]
    return timedelta(**{unit: int(match[1])})


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _stage_overrides(raw: object, where: str) -> dict[str, dict]:
    raw = raw or {}
    if not isinstance(raw, dict) or any(not isinstance(v, dict) for v in raw.values()):
        raise ConfigError(f"{where}: stage settings must be a mapping of stage name to settings")
    unknown = set(raw) - set(STAGES)
    if unknown:
        raise ConfigError(f"{where}: unknown stage {', '.join(sorted(unknown))}; stages: {', '.join(STAGES)}")
    return raw


def _load_claude(raw: dict, root: Path) -> ClaudeSettings:
    settings = ClaudeSettings()
    settings.auth = str(raw.get("auth", settings.auth))
    if settings.auth not in AUTH_MODES:
        raise ConfigError(f"claude.auth must be one of {', '.join(AUTH_MODES)}")
    settings.binary = str(raw.get("binary", settings.binary))
    if raw.get("config_dir"):
        settings.config_dir = (root / Path(raw["config_dir"]).expanduser()).resolve()
    settings.model = str(raw.get("model", settings.model))
    settings.timeout_seconds = int(raw.get("timeout_seconds", settings.timeout_seconds))
    settings.stages = _stage_overrides(raw.get("stages"), "claude.stages")
    return settings


def _load_channels(raw: object) -> dict[str, Channel]:
    if not isinstance(raw or {}, dict):
        raise ConfigError("channels must be a mapping of channel name to settings")
    channels = {}
    for name, item in (raw or {}).items():
        if not isinstance(item, dict) or not item.get("skill"):
            raise ConfigError(f"channel {name!r} needs a skill")
        env = item.get("env") or {}
        if not isinstance(env, dict):
            raise ConfigError(f"channel {name!r}: env must be a mapping")
        channels[name] = Channel(name, str(item["skill"]), {str(k): str(v) for k, v in env.items()})
    return channels


def _load_watch(path: Path, channels: dict[str, Channel]) -> Watch:
    raw = _read_yaml(path / WATCH_FILE)
    where = str(path / WATCH_FILE)
    sources = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("skill"):
            raise ConfigError(f"{where}: every source needs an id and a skill")
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ConfigError(f"{where}: params of source {item['id']!r} must be a mapping")
        sources.append(Source(str(item["id"]), str(item["skill"]), params))
    ids = [s.id for s in sources]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"{where}: source ids must be unique")
    watch_channels = [str(c) for c in raw.get("channels") or []]
    unknown = [c for c in watch_channels if c not in channels]
    if unknown:
        raise ConfigError(f"{where}: unknown channel {', '.join(unknown)}; define it in {CONFIG_FILE}")
    return Watch(
        name=path.name,
        dir=path,
        enabled=bool(raw.get("enabled", True)),
        interval=parse_interval(raw.get("interval", "1h")),
        sources=sources,
        channels=watch_channels,
        claude=_stage_overrides(raw.get("claude"), f"{where} claude"),
    )


def load(root: Path | None = None) -> Config:
    root = root or find_root()
    raw = _read_yaml(root / CONFIG_FILE)
    paths = raw.get("paths") or {}
    data_dir = (root / paths.get("data", "data")).resolve()
    skills_dir = (root / paths.get("skills", "skills")).resolve()
    watches_dir = (root / paths.get("watches", "watches")).resolve()
    channels = _load_channels(raw.get("channels"))
    watches = {}
    if watches_dir.is_dir():
        for path in sorted(p for p in watches_dir.iterdir() if (p / WATCH_FILE).is_file()):
            watches[path.name] = _load_watch(path, channels)
    return Config(
        root=root,
        claude=_load_claude(raw.get("claude") or {}, root),
        channels=channels,
        watches=watches,
        data_dir=data_dir,
        skills_dir=skills_dir,
        watches_dir=watches_dir,
    )
