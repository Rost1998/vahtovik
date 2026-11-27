"""Load and validate vahtovik.yaml, the roles (roles/<role>/ROLE.md) and the pollers (pollers/<poller>/poller.yaml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

import yaml

from .util import parse_front_matter

CONFIG_FILE = "vahtovik.yaml"
ROLE_FILE = "ROLE.md"
POLLER_FILE = "poller.yaml"
OWN_CREDENTIALS = "vahtovik"  # knowledge/credentials/vahtovik.env: the secrets of vahtovik's own process
STAGES = ("monitor", "worker", "watchdog", "input")
STAGE_DEFAULTS = {"input": {"model": "opus", "effort": "xhigh"}}  # over the claude model and effort, under its stages
SCAN_MODES = ("ai", "script")  # who collects a source's items: an AI session, or the source skill's scan script
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # what `claude --effort` takes
AUTH_MODES = ("subscription", "api-key")
INPUT_COMMANDS = ("status", "log", "tell", "artifact", "worker")  # the vahtovik commands a limited channel may get
SCOPE_ENV = "VAHTOVIK_ROLES"  # set: vahtovik commands see only workers of these roles (the sessions of limited channels)
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # the names of roles and pollers, and worker keys


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
    config_dir: Path | None = None  # the default login's Claude Code profile; None: Claude Code's own default
    accounts: dict[str, Path] = field(default_factory=dict)  # other logins that roles and pollers can use
    model: str = "opus"  # an alias: Claude Code uses the newest Opus
    effort: str = "high"
    stages: dict[str, dict] = field(default_factory=dict)
    parallel_turns: int = 5  # the most worker turns that work at once; the rest start at a later run


@dataclass
class TmuxSettings:
    session: str = "vahtovik-agents"  # the tmux session that holds a window per running worker session
    socket: str | None = "vahtovik"  # `tmux -L <socket>`, a server of its own; None uses the default tmux server


@dataclass
class WatchdogSettings:
    every: timedelta = timedelta(minutes=5)  # a run starts with the watchdog once this long has passed; 0: every run
    notify_every: timedelta = timedelta(hours=6)  # repeat a notice this often; 0 never repeats
    long_turn_after: timedelta = timedelta(hours=3)  # warn once about a turn running this long; 0 never warns


@dataclass
class UsageSettings:
    every: timedelta = timedelta(minutes=10)  # how often vahtovik asks Claude Code for the plan's usage; 0: never
    warn_at: list[int] = field(default_factory=lambda: [80, 90, 95, 99])  # one warning at each percent of a limit


@dataclass
class InputSettings:
    interval: timedelta = timedelta(minutes=1)  # how often vahtovik reads the channel
    commands: list[str] | None = None  # set: a limited channel, whose agent may run only these vahtovik commands
    roles: list[str] | None = None  # set: its agent sees only workers of these roles
    senders: list[str] | None = None  # set: only messages whose `from` is one of these reach the agent
    rules: str = ""  # the person's own rules for the channel's agent
    # after a pause this long, the next message starts a new session, unless the last reply waits for an answer;
    # 0: the session lasts until the person clears it
    new_session_after: timedelta = timedelta(hours=1)
    history: int = 5  # how many earlier messages a new session gets in its prompt

    @property
    def limited(self) -> bool:
        """A limited channel's session runs with the guard (channel_guard.py) instead of bypassPermissions."""
        return self.commands is not None


@dataclass
class Channel:
    """One of vahtovik's own channels: how vahtovik talks to the person. Workers never use them."""
    name: str
    skill: str  # a skill of knowledge/skills with a `send` script, and a `receive` script for `input`
    env: dict[str, str] = field(default_factory=dict)  # settings for the scripts, such as a chat id
    credentials: list[str] = field(default_factory=list)  # files of knowledge/credentials for the scripts
    input: InputSettings | None = None  # set: vahtovik reads the person's messages from the channel and answers them


@dataclass
class Source:
    id: str
    skill: str
    params: dict = field(default_factory=dict)
    credentials: list[str] = field(default_factory=list)  # more files of knowledge/credentials, over the skill's
    interval: timedelta = timedelta(hours=1)  # how often to monitor it; the poller's interval by default
    scan: str = "ai"  # see SCAN_MODES
    at: list[time] = field(default_factory=list)  # set: monitor it at these times of the day instead, in local time

    def next_run(self, last: datetime) -> datetime:
        """When the source is due after it was monitored at `last`: its interval later, or the first of its times
        of the day after `last`."""
        if not self.at:
            return last + self.interval
        local = last.astimezone()  # a naive datetime's astimezone() takes it as local time
        today = (datetime.combine(local.date(), at).astimezone() for at in self.at)
        return next((moment for moment in today if moment > local),
                    datetime.combine(local.date() + timedelta(days=1), self.at[0]).astimezone())


@dataclass
class Role:
    """What a worker is and how it works: roles/<role>/ROLE.md, with its memory next to it."""
    name: str
    dir: Path
    description: str
    skills: list[str]  # skills of knowledge/skills that its workers get, besides the shared ones
    credentials: list[str]  # more files of knowledge/credentials, over its skills' own
    body: str  # its working rules: how to process, what to report, where and with which skill
    model: str | None = None
    effort: str | None = None
    account: str | None = None  # set: its workers use this account of claude.accounts

    @property
    def role_md(self) -> Path:
        return self.dir / ROLE_FILE

    @property
    def memory_md(self) -> Path:
        return self.dir / "MEMORY.md"


@dataclass
class Poller:
    """What is monitored, and which workers get what it finds: pollers/<poller>/poller.yaml and poller.md."""
    name: str
    dir: Path
    enabled: bool
    interval: timedelta  # the default interval of its sources
    roles: list[str]  # the roles its new workers may have; the first is the default
    sources: list[Source]
    at: list[time] = field(default_factory=list)  # set: the default times of the day of its sources, over interval
    model: str | None = None  # the model and effort of its monitoring sessions
    effort: str | None = None
    account: str | None = None  # set: its monitoring sessions use this account of claude.accounts

    @property
    def poller_yaml(self) -> Path:
        return self.dir / POLLER_FILE

    @property
    def poller_md(self) -> Path:
        return self.dir / "poller.md"


@dataclass
class Config:
    root: Path
    claude: ClaudeSettings
    channels: dict[str, Channel]
    roles: dict[str, Role]
    pollers: dict[str, Poller]
    data_dir: Path
    knowledge_dir: Path
    skills_dir: Path  # vahtovik's own skills: the vahtovik agent's and the watchdog's
    roles_dir: Path
    pollers_dir: Path
    tmux: TmuxSettings = field(default_factory=TmuxSettings)
    watchdog: WatchdogSettings = field(default_factory=WatchdogSettings)
    usage: UsageSettings = field(default_factory=UsageSettings)
    notices: list[str] | None = None  # where notices and usage warnings go; None: every full input channel
    scope: list[str] | None = None  # set: the command runs for a limited channel that sees only these roles

    def stage(self, stage: str, owner: Role | Poller | None = None, model: str | None = None,
              effort: str | None = None) -> StageSettings:
        """A stage's settings. Later ones win: vahtovik.yaml's model and effort, the stage's own defaults
        (STAGE_DEFAULTS), vahtovik.yaml's settings for the stage, the model and effort of the role (for its
        workers) or the poller (for its monitoring sessions), and the given ones: a worker's own."""
        merged: dict = {"model": self.claude.model, "effort": self.claude.effort}
        merged.update(STAGE_DEFAULTS.get(stage, {}))
        merged.update(self.claude.stages.get(stage, {}))
        if owner is not None:
            merged.update({k: v for k, v in (("model", owner.model), ("effort", owner.effort)) if v})
        merged.update({k: v for k, v in (("model", model), ("effort", effort)) if v})
        budget = merged.get("max_budget_usd")
        return StageSettings(
            model=str(merged["model"]),
            effort=merged.get("effort"),
            max_budget_usd=float(budget) if budget is not None else None,
        )

    def role(self, name: str) -> Role:
        if name not in self.roles:
            raise ConfigError(f"unknown role {name!r}; known: {', '.join(sorted(self.roles)) or 'none'}")
        return self.roles[name]

    def poller(self, name: str) -> Poller:
        if name not in self.pollers:
            raise ConfigError(f"unknown poller {name!r}; known: {', '.join(sorted(self.pollers)) or 'none'}")
        return self.pollers[name]

    def notice_channels(self) -> list[str]:
        """Where vahtovik's notices and usage warnings go: `notices`, or every input channel that is not limited."""
        if self.notices is not None:
            return self.notices
        return [c.name for c in self.channels.values() if c.input and not c.input.limited]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vahtovik.db"

    @property
    def credentials_dir(self) -> Path:
        return self.knowledge_dir / "credentials"

    @property
    def workers_dir(self) -> Path:
        return self.data_dir / "workers"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    def poller_dir(self, poller: str) -> Path:
        return self.data_dir / "pollers" / poller

    def source_dir(self, poller: str, source_id: str) -> Path:
        return self.poller_dir(poller) / "sources" / source_id

    def channel_dir(self, channel: str) -> Path:
        return self.data_dir / "channels" / channel

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


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def credentials_file(cfg: Config, name: str) -> Path:
    return cfg.credentials_dir / f"{name}.env"


def load_env_file(cfg: Config) -> None:
    """Load vahtovik's own credentials, knowledge/credentials/vahtovik.env, without overriding the real
    environment."""
    for key, value in read_env_file(credentials_file(cfg, OWN_CREDENTIALS)).items():
        os.environ.setdefault(key, value)


def parse_interval(value: object) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(value))
    if not match:
        raise ConfigError(f"bad interval {value!r}: use a number and s, m, h or d, for example 30m")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match[2]]
    return timedelta(**{unit: int(match[1])})


def parse_times(value: object) -> list[time]:
    """`at`: one time of the day, HH:MM, or a list of them; sorted, without repeats. PyYAML reads an unquoted 20:00
    as the base-60 number 1200, so numbers are taken as minutes."""
    times = set()
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item < 24 * 60:
            times.add(time(*divmod(item, 60)))
            continue
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(item))
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise ConfigError(f"bad time {item!r}: use HH:MM, for example 08:30")
        times.add(time(int(match[1]), int(match[2])))
    if not times:
        raise ConfigError("at: give one time of the day or more, for example \"08:30\"")
    return sorted(times)


def parse_moment(value: str, now: datetime | None = None) -> datetime:
    """`--at`: a time of the day, HH:MM, meaning its next occurrence in local time, or a date and time,
    YYYY-MM-DD HH:MM, in local time. Returns an aware datetime."""
    now = (now or datetime.now()).astimezone()
    text = " ".join(str(value).split())
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})", text)
    if match:
        try:
            moment = datetime.fromisoformat(f"{match[1]} {int(match[2]):02d}:{match[3]}").astimezone()
        except ValueError:
            raise ConfigError(f"bad date and time {value!r}: use YYYY-MM-DD HH:MM, for example 2026-11-25 08:00") from None
        if moment <= now:
            raise ConfigError(f"{value!r} has passed already")
        return moment
    try:
        at = parse_times(text)[0]
    except ConfigError:
        raise ConfigError(f"bad time {value!r}: use HH:MM, or YYYY-MM-DD HH:MM") from None
    moment = datetime.combine(now.date(), at).astimezone()
    return moment if moment > now else datetime.combine(now.date() + timedelta(days=1), at).astimezone()


def _schedule(raw: dict, where: str) -> tuple[timedelta | None, list[time]]:
    """The interval or the times of the day that a poller or a source sets itself; (None, []) for neither."""
    if raw.get("interval") is not None and raw.get("at") is not None:
        raise ConfigError(f"{where}: set interval or at, not both")
    try:
        if raw.get("at") is not None:
            return None, parse_times(raw["at"])
        return (parse_interval(raw["interval"]) if raw.get("interval") is not None else None), []
    except ConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def check_effort(value: object, where: str) -> str | None:
    if value is None:
        return None
    if str(value) not in EFFORTS:
        raise ConfigError(f"{where}: unknown effort {value!r}; efforts: {', '.join(EFFORTS)}")
    return str(value)


def _stage_overrides(raw: object, where: str) -> dict[str, dict]:
    raw = raw or {}
    if not isinstance(raw, dict) or any(not isinstance(v, dict) for v in raw.values()):
        raise ConfigError(f"{where}: stage settings must be a mapping of stage name to settings")
    unknown = set(raw) - set(STAGES)
    if unknown:
        raise ConfigError(f"{where}: unknown stage {', '.join(sorted(unknown))}; stages: {', '.join(STAGES)}")
    for stage, settings in raw.items():
        check_effort(settings.get("effort"), f"{where}.{stage}")
    return raw


def _load_claude(raw: dict, root: Path) -> ClaudeSettings:
    settings = ClaudeSettings()
    settings.auth = str(raw.get("auth", settings.auth))
    if settings.auth not in AUTH_MODES:
        raise ConfigError(f"claude.auth must be one of {', '.join(AUTH_MODES)}")
    settings.binary = str(raw.get("binary", settings.binary))
    if raw.get("config_dir"):
        settings.config_dir = _folder(root, raw["config_dir"])
    settings.accounts = _load_accounts(raw.get("accounts"), root)
    if settings.accounts and settings.auth != "subscription":
        raise ConfigError("claude.accounts work only with auth: subscription")
    settings.model = str(raw.get("model", settings.model))
    settings.effort = check_effort(raw.get("effort", settings.effort), "claude") or settings.effort
    settings.stages = _stage_overrides(raw.get("stages"), "claude.stages")
    parallel = raw.get("parallel_turns", settings.parallel_turns)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise ConfigError("claude.parallel_turns must be a whole number, 1 or more")
    settings.parallel_turns = parallel
    return settings


def _folder(root: Path, value: object) -> Path:
    return (root / Path(str(value)).expanduser()).resolve()


def _load_accounts(raw: object, root: Path) -> dict[str, Path]:
    """claude.accounts: name -> {config_dir: the account's Claude Code profile}."""
    if not isinstance(raw or {}, dict):
        raise ConfigError("claude.accounts must be a mapping of account name to {config_dir: ...}")
    accounts = {}
    for name, item in (raw or {}).items():
        if not isinstance(item, dict) or not item.get("config_dir"):
            raise ConfigError(f"claude.accounts: account {name!r} needs a config_dir, its Claude Code profile")
        accounts[str(name)] = _folder(root, item["config_dir"])
    return accounts


def _load_tmux(raw: object) -> TmuxSettings:
    if not isinstance(raw or {}, dict):
        raise ConfigError("tmux must be a mapping")
    raw = raw or {}
    settings = TmuxSettings()
    settings.session = str(raw.get("session") or settings.session)
    if "socket" in raw:
        settings.socket = str(raw["socket"]) if raw["socket"] else None
    return settings


def _load_watchdog(raw: object) -> WatchdogSettings:
    if not isinstance(raw or {}, dict):
        raise ConfigError("watchdog must be a mapping")
    raw = raw or {}
    settings = WatchdogSettings()
    for name in ("every", "notify_every", "long_turn_after"):
        if name in raw:
            value = raw[name]
            setattr(settings, name, timedelta(0) if value in (0, "0") else parse_interval(value))
    return settings


def _load_usage(raw: object) -> UsageSettings:
    if not isinstance(raw or {}, dict):
        raise ConfigError("usage must be a mapping")
    raw = raw or {}
    settings = UsageSettings()
    if "every" in raw:
        try:
            settings.every = timedelta(0) if raw["every"] in (0, "0", None) else parse_interval(raw["every"])
        except ConfigError as exc:
            raise ConfigError(f"usage.every: {exc}") from None
    if "warn_at" in raw:
        value = raw["warn_at"]
        value = value if isinstance(value, list) else [value]
        if any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100 for v in value):
            raise ConfigError("usage.warn_at must be a list of whole percents from 1 to 100, such as [80, 90]")
        settings.warn_at = sorted(set(value))
    return settings


def _names(raw: dict, key: str, where: str) -> list[str] | None:
    if raw.get(key) is None:
        return None
    value = raw[key]
    if not isinstance(value, list) or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in value):
        raise ConfigError(f"{where}.{key} must be a list of names")
    return [str(v) for v in value]


def _string_map(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw or {}, dict):
        raise ConfigError(f"{where} must be a mapping")
    return {str(k): str(v) for k, v in (raw or {}).items()}


def _load_channels(raw: object) -> dict[str, Channel]:
    if not isinstance(raw or {}, dict):
        raise ConfigError("channels must be a mapping of channel name to settings")
    channels = {}
    for name, item in (raw or {}).items():
        where = f"channel {name!r}"
        if not isinstance(item, dict) or not item.get("skill"):
            raise ConfigError(f"{where} needs a skill")
        reading = item.get("input")
        if reading is not None and not isinstance(reading, dict):
            raise ConfigError(f"{where}: input must be a mapping, such as {{}} or {{interval: 5m}}")
        settings = None if reading is None else _load_input(reading, f"{where}: input")
        channels[str(name)] = Channel(str(name), str(item["skill"]), _string_map(item.get("env"), f"{where}: env"),
                                      _names(item, "credentials", where) or [], settings)
    return channels


def _load_input(raw: dict, where: str) -> InputSettings:
    settings = InputSettings()
    if raw.get("interval"):
        settings.interval = parse_interval(raw["interval"])
    settings.commands = _names(raw, "commands", where)
    unknown = [c for c in settings.commands or [] if c not in INPUT_COMMANDS]
    if unknown:
        raise ConfigError(f"{where}.commands: unknown command {', '.join(unknown)}; "
                          f"commands: {', '.join(INPUT_COMMANDS)}")
    settings.roles = _names(raw, "roles", where)
    if settings.roles is not None and settings.commands is None:
        settings.commands = list(INPUT_COMMANDS)  # roles alone limit the channel too
    settings.senders = _names(raw, "senders", where)
    rules = raw.get("rules") or ""
    if not isinstance(rules, str):
        raise ConfigError(f"{where}.rules must be text")
    settings.rules = rules.strip()
    if "new_session_after" in raw:
        value = raw["new_session_after"]
        try:
            settings.new_session_after = timedelta(0) if value in (0, "0", None) else parse_interval(value)
        except ConfigError as exc:
            raise ConfigError(f"{where}.new_session_after: {exc}") from None
    history = raw.get("history", settings.history)
    if isinstance(history, bool) or not isinstance(history, int) or history < 0:
        raise ConfigError(f"{where}.history must be a whole number, 0 or more")
    settings.history = history
    return settings


def _account(value: object, where: str, accounts: dict[str, Path]) -> str | None:
    if not value:
        return None
    if str(value) not in accounts:
        raise ConfigError(f"{where}: unknown account {value!r}; known: {', '.join(sorted(accounts)) or 'none'}; "
                          f"define it in {CONFIG_FILE} claude.accounts")
    return str(value)


def _check_name(name: str, what: str, path: Path) -> None:
    if not NAME.fullmatch(name):
        raise ConfigError(f"{path}: the {what} name {name!r} must be lowercase letters and digits, "
                          "with single hyphens between words")


def _load_role(path: Path, accounts: dict[str, Path]) -> Role:
    where = str(path / ROLE_FILE)
    _check_name(path.name, "role", path)
    try:
        meta, body = parse_front_matter((path / ROLE_FILE).read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where}: the front matter is not valid YAML: {exc}") from None
    unknown = set(meta) - {"name", "description", "skills", "credentials", "model", "effort", "account"}
    if unknown:
        raise ConfigError(f"{where}: unknown front matter {', '.join(sorted(unknown))}")
    if "name" in meta and str(meta["name"]) != path.name:
        raise ConfigError(f"{where}: its front matter names it {meta['name']!r}; use the folder name")
    return Role(
        name=path.name,
        dir=path,
        description=str(meta.get("description") or ""),
        skills=_names(meta, "skills", where) or [],
        credentials=_names(meta, "credentials", where) or [],
        body=body.strip(),
        model=str(meta["model"]) if meta.get("model") else None,
        effort=check_effort(meta.get("effort"), where),
        account=_account(meta.get("account"), where, accounts),
    )


def _load_poller(path: Path, roles: dict[str, Role], accounts: dict[str, Path]) -> Poller:
    raw = _read_yaml(path / POLLER_FILE)
    where = str(path / POLLER_FILE)
    _check_name(path.name, "poller", path)
    interval, at = _schedule(raw, where)
    interval = interval or timedelta(hours=1)
    sources = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("skill"):
            raise ConfigError(f"{where}: every source needs an id and a skill")
        own = f"{where}: source {item['id']!r}"
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ConfigError(f"{own}: params must be a mapping")
        scan = str(item.get("scan") or "ai")
        if scan not in SCAN_MODES:
            raise ConfigError(f"{own}: scan must be one of {', '.join(SCAN_MODES)}")
        own_interval, own_at = _schedule(item, own)
        if own_interval is None and not own_at:  # a source without its own schedule has its poller's
            own_interval, own_at = interval, at
        sources.append(Source(str(item["id"]), str(item["skill"]), params, _names(item, "credentials", own) or [],
                              own_interval or interval, scan, own_at))
    ids = [s.id for s in sources]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"{where}: source ids must be unique")
    poller_roles = _names(raw, "roles", where) or []
    if not poller_roles:
        raise ConfigError(f"{where}: give roles, the roles of the workers it spawns, for example [uv-tracker]")
    unknown = [r for r in poller_roles if r not in roles]
    if unknown:
        raise ConfigError(f"{where}: unknown role {', '.join(unknown)}; make it in roles/<role>/{ROLE_FILE}")
    claude = raw.get("claude") or {}
    if not isinstance(claude, dict) or set(claude) - {"model", "effort", "account"}:
        raise ConfigError(f"{where} claude: give only model, effort and account")
    return Poller(
        name=path.name,
        dir=path,
        enabled=bool(raw.get("enabled", True)),
        interval=interval,
        roles=poller_roles,
        sources=sources,
        at=at,
        model=str(claude["model"]) if claude.get("model") else None,
        effort=check_effort(claude.get("effort"), f"{where} claude"),
        account=_account(claude.get("account"), f"{where} claude", accounts),
    )


def _folders(parent: Path, marker: str) -> list[Path]:
    return sorted(p for p in parent.iterdir() if (p / marker).is_file()) if parent.is_dir() else []


def load(root: Path | None = None) -> Config:
    root = root or find_root()
    raw = _read_yaml(root / CONFIG_FILE)
    paths = raw.get("paths") or {}
    folder = {name: (root / paths.get(name, name)).resolve() for name in ("data", "knowledge", "skills", "roles",
                                                                         "pollers")}
    channels = _load_channels(raw.get("channels"))
    claude = _load_claude(raw.get("claude") or {}, root)
    roles = {p.name: _load_role(p, claude.accounts) for p in _folders(folder["roles"], ROLE_FILE)}
    pollers = {p.name: _load_poller(p, roles, claude.accounts) for p in _folders(folder["pollers"], POLLER_FILE)}
    for channel in channels.values():
        unknown = [r for r in (channel.input.roles or []) if r not in roles] if channel.input else []
        if unknown:
            raise ConfigError(f"channel {channel.name!r}: input.roles: unknown role {', '.join(unknown)}")
    notices = _names(raw, "notices", "vahtovik.yaml")
    unknown = [c for c in notices or [] if c not in channels]
    if unknown:
        raise ConfigError(f"notices: unknown channel {', '.join(unknown)}")
    # A command that a limited channel's agent runs sees only the channel's roles. The scope is for this command
    # alone: what the command starts, such as the tmux server or a worker session, must see everything.
    scope = os.environ.pop(SCOPE_ENV, None)
    scope = None if scope is None else [r for r in scope.split(",") if r]
    if scope is not None:
        roles = {name: role for name, role in roles.items() if name in scope}
        pollers = {name: p for name, p in pollers.items() if set(p.roles) & set(scope)}
    return Config(
        root=root,
        claude=claude,
        channels=channels,
        roles=roles,
        pollers=pollers,
        data_dir=folder["data"],
        knowledge_dir=folder["knowledge"],
        skills_dir=folder["skills"],
        roles_dir=folder["roles"],
        pollers_dir=folder["pollers"],
        tmux=_load_tmux(raw.get("tmux")),
        watchdog=_load_watchdog(raw.get("watchdog")),
        usage=_load_usage(raw.get("usage")),
        notices=notices,
        scope=scope,
    )
