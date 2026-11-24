"""Workers: the record, the runtime folder, the skills and the persistent Claude Code session of each worker.

A worker has an id, 6 random hexadecimal characters that are never used again, and a key that is unique within its
role. Its name is <id>-<key>, and its folder data/workers/<role>/<id>-<key>/ holds:

    .claude/skills/<skill>   links to its skills: the shared ones, its role's, and the extra ones of its spawn
    CLAUDE.md                regenerated before every start of its session
    context.md               its record
    artifacts/*.json         its artifacts

Every command takes a worker as <id>, <id>-<key> or <role>/<key>.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shlex
import uuid
from datetime import datetime
from pathlib import Path

from . import context_file, journal, knowledge, memory
from .claude_session import Claude, SessionSpec
from .config import NAME, Config, ConfigError, Role
from .state import State
from .tmux import Tmux
from .util import LockBusy, file_lock, human, render, schema_path

log = logging.getLogger(__name__)

ID = re.compile(r"[0-9a-f]{6}")
REF = re.compile(r"([0-9a-f]{6})(?:-(.+))?")
LOCK_NAME = ".lock"
NEW_FOR_EXISTING = (
    "Note: this is a new session for an existing worker; the earlier session was lost or reset. "
    "Read CLAUDE.md, context.md and the files in artifacts/ to rebuild your picture before you start."
)
RULES_CHANGED = (
    "Note: the rules of every vahtovik worker changed since your session started. Read `{path}` now. Where it "
    "differs from the rules in your system prompt, the file is right."
)
OPEN_NEW_PROMPT = (
    "A person opened this worker's session. Read CLAUDE.md and context.md, then tell them in a few lines "
    "what you work on and what has been done so far."
)


def name(row) -> str:
    return f"{row['id']}-{row['key']}"


def label(row) -> str:
    """How the journal names a worker: <role>/<id>-<key>."""
    return f"{row['role']}/{name(row)}"


def folder(cfg: Config, row) -> Path:
    """The worker's folder; an archived worker's folder is in data/archive/<role>/."""
    base = cfg.archive_dir if row["archived_at"] else cfg.workers_dir
    return base / row["role"] / name(row)


def context_md(cfg: Config, row) -> Path:
    return folder(cfg, row) / "context.md"


def lock(path: Path, wait: bool = False):
    return file_lock(path / LOCK_NAME, wait=wait)


def is_busy(cfg: Config, row) -> bool:
    """Whether another process holds the worker's lock: its window, or a command that changes it."""
    try:
        with lock(folder(cfg, row)):
            return False
    except LockBusy:
        return True


def check_key(raw: str) -> str:
    key = raw.strip()
    if not NAME.fullmatch(key):
        raise ConfigError(f"bad worker key {raw!r}: use lowercase letters and digits, with single hyphens between "
                          "words, such as uv-0-13")
    return key


def parse_new(ref: str) -> tuple[str, str]:
    """The role and the key of a new worker, from <role>/<key>."""
    role, sep, key = ref.strip().partition("/")
    if not sep or not role or not key:
        raise ConfigError(f"name a new worker as <role>/<key>, such as uv-tracker/uv-0-13, not {ref!r}")
    return role, check_key(key)


def resolve(cfg: Config, state: State, ref: str, archived: bool = False):
    """The worker that a reference names: <id>, <id>-<key> or <role>/<key>. The key of a merged worker leads to
    the worker it went into. Raises ConfigError for an unknown worker, and, unless `archived`, for an archived
    one."""
    ref = ref.strip()
    row = None
    if "/" in ref:
        role, _, key = ref.partition("/")
        row = state.find_worker(role, key)
        if row is None and archived:
            row = state.row("SELECT * FROM workers WHERE role = ? AND key = ? ORDER BY created_at DESC LIMIT 1",
                            role, key)
    else:
        match = REF.fullmatch(ref)
        if not match:
            raise ConfigError(f"a worker is <id>, <id>-<key> or <role>/<key>, such as a3f9c2 or uv-tracker/uv-0-13; "
                              f"not {ref!r}")
        row = state.worker(match[1])
        if row is not None and match[2] and match[2] != row["key"]:
            raise ConfigError(f"worker {match[1]} is {name(row)}, not {ref}")
    if row is None or (cfg.scope is not None and row["role"] not in cfg.scope):
        raise ConfigError(f"unknown worker {ref}; see `vahtovik worker list`")
    if row["archived_at"] and not archived:
        target = state.merge_target(row)
        if target is not None:
            raise ConfigError(f"{name(row)} was merged into {name(target)}; use that one")
        raise ConfigError(f"{name(row)} is archived: {row['archive_reason']}")
    return row


def new_id(state: State) -> str:
    while True:
        worker_id = secrets.token_hex(3)
        if state.worker(worker_id) is None:
            return worker_id


def spawned_by_env(env: dict[str, str]) -> tuple[str, str | None]:
    """Who spawns, from the environment of the command: (spawned_by, parent id)."""
    if env.get("VAHTOVIK_WORKER"):
        return f"worker:{env['VAHTOVIK_WORKER']}", env["VAHTOVIK_WORKER"]
    if env.get("VAHTOVIK_CHANNEL"):
        return f"channel:{env['VAHTOVIK_CHANNEL']}", None
    return "person", None


def create(cfg: Config, state: State, role_name: str, key: str, title: str, spawned_by: str,
           parent: str | None = None, skills: list[str] = (), report_back: bool = False):
    """A new worker, with its folder. Raises ConfigError if the role has an active worker with this key."""
    role = cfg.role(role_name)
    key = check_key(key)
    existing = state.active_worker(role.name, key)
    if existing is not None:
        raise ConfigError(f"role {role.name} has a worker with key {key} already: {name(existing)}")
    missing = [s for s in skills if s not in knowledge.available(cfg)]
    if missing:
        raise ConfigError(f"unknown skill {', '.join(missing)}; skills: {', '.join(knowledge.available(cfg)) or 'none'}")
    if parent is not None and state.worker(parent) is None:
        raise ConfigError(f"unknown parent worker {parent}")
    worker_id = new_id(state)
    title = title.strip() or key
    state.add_worker(worker_id, role.name, key, title, list(dict.fromkeys(skills)), spawned_by, parent, report_back)
    row = state.worker(worker_id)
    path = folder(cfg, row)
    (path / "artifacts").mkdir(parents=True, exist_ok=True)
    context_file.create(path / "context.md", title, role.name, name(row))
    prepare(cfg, state, row)
    who = describe_spawner(state, spawned_by)
    log.info("%s: Created worker \"%s\", spawned by %s", label(row), title, who)
    return row


def describe_spawner(state: State, spawned_by: str | None) -> str:
    """Who spawned a worker, for people: "the person", "channel ops", "poller uv-releases", "worker a3f9c2-uv-0-13"."""
    kind, _, who = (spawned_by or "person").partition(":")
    if kind == "worker":
        row = state.worker(who)
        return f"worker {name(row) if row else who}"
    return f"{kind} {who}" if who else "the person"


def role_of(cfg: Config, row) -> Role:
    if row["role"] not in cfg.roles:
        raise ConfigError(f"worker {name(row)} has the role {row['role']}, which is not in {cfg.roles_dir} any more")
    return cfg.roles[row["role"]]


def skills_of(cfg: Config, row) -> tuple[list[knowledge.Skill], list[str]]:
    """The worker's skills: the shared ones, its role's, and the extra ones of its spawn; and the problems of the
    ones that cannot be read."""
    return knowledge.collect(cfg, [*role_of(cfg, row).skills, *json.loads(row["skills"] or "[]")])


def env(cfg: Config, row, found: list[knowledge.Skill]) -> dict[str, str]:
    """The environment of the worker's session: the credentials of its skills and its role, and who it is."""
    return {**knowledge.session_env(cfg, found, role_of(cfg, row).credentials),
            "VAHTOVIK_WORKER": row["id"], "VAHTOVIK_WORKER_NAME": name(row), "VAHTOVIK_ROLE": row["role"]}


def prepare(cfg: Config, state: State, row) -> list[knowledge.Skill]:
    """Before every start of the session: link the worker's skills into its folder, and write its CLAUDE.md.
    Returns the skills."""
    found, problems = skills_of(cfg, row)
    for problem in problems:
        log.warning("%s: Skipped a skill: %s", label(row), problem)
    path = folder(cfg, row)
    knowledge.link(path, found)
    write_claude_md(cfg, state, row, found)
    return found


def schedule_text(row) -> str:
    """The worker's scheduled turn, for its CLAUDE.md and its prompts."""
    if not row["scheduled_at"]:
        return "You have no scheduled turn."
    return (f"Your next scheduled turn is at {human(datetime.fromisoformat(row['scheduled_at']))}: "
            f"{row['scheduled_reason'] or 'a scheduled check'}.")


def roles_text(cfg: Config) -> str:
    return "\n".join(f"- `{r.name}`: {r.description or '(no description)'}" for r in cfg.roles.values()) or "- none"


def write_claude_md(cfg: Config, state: State, row, found: list[knowledge.Skill]) -> None:
    """Regenerated before every start, so the agent always sees current paths and facts."""
    role = role_of(cfg, row)
    parent = state.worker(row["parent"]) if row["parent"] else None
    text = render(
        "worker_claude_md.md",
        title=row["title"], name=name(row), id=row["id"], key=row["key"], role=role.name,
        role_description=role.description or "(no description)", role_md=role.role_md,
        spawned_by=describe_spawner(state, row["spawned_by"]),
        parent=f"`{name(parent)}`" if parent else "none",
        report_back=("yes: when you end a turn as finished or waiting, vahtovik tells your parent"
                     if row["report_back"] and parent else "no"),
        folder=folder(cfg, row), root=cfg.root, worker_rules=knowledge.worker_rules(cfg).skill_md,
        memory_md=role.memory_md, skills=knowledge.listing(found), roles=roles_text(cfg),
        schedule=schedule_text(row), artifact_schema=schema_path("artifact"), journal=journal.path(cfg),
    )
    (folder(cfg, row) / "CLAUDE.md").write_text(text)


def rules_hash(cfg: Config) -> str:
    return hashlib.sha256(knowledge.worker_rules(cfg).body.encode()).hexdigest()[:16]


def rules_note(cfg: Config, state: State, row, spec: SessionSpec) -> str | None:
    """For a turn's prompt: a note that the worker rules changed since the worker's session was created.

    Claude Code keeps a session's system prompt as it was at its creation, and ignores a new one on resume. So a
    resumed session never sees a later change of knowledge/shared/vahtovik in its system prompt: the turn that
    follows the change tells the agent to read the file, once."""
    current = rules_hash(cfg)
    if spec.mode != "resume" or row["rules_hash"] == current:
        return None
    state.set_rules_hash(row["id"], current)
    return RULES_CHANGED.format(path=knowledge.worker_rules(cfg).skill_md) if row["rules_hash"] else None


def spec_for(cfg: Config, claude: Claude, state: State, row, force_new: bool = False) -> tuple[SessionSpec, str | None]:
    """Prepare the worker's folder and build its session spec. Returns (spec, note for the prompt or None)."""
    found = prepare(cfg, state, row)
    role = role_of(cfg, row)
    stored = row["session_id"]
    spec = SessionSpec(
        stage="worker",
        workdir=folder(cfg, row),
        prompt="",
        settings=cfg.stage("worker", role),
        env=env(cfg, row, found),
        scope=label(row),
        account=role.account,
        worker=row["id"],
    )
    # A session in another account's profile, as after the role moved to another account, cannot be resumed.
    if stored and not force_new and claude.session_exists(stored, role.account):
        spec.mode, spec.session_id = "resume", stored
        return spec, None
    spec.mode, spec.session_id = "new", str(uuid.uuid4())
    spec.name = f"vahtovik: {label(row)}"
    spec.system_prompt = knowledge.worker_rules(cfg).body
    state.set_rules_hash(row["id"], rules_hash(cfg))  # the new session gets these rules, for all its life
    return spec, NEW_FOR_EXISTING if stored else None


def describe(spec: SessionSpec, note: str | None) -> str:
    """How the session starts, for the journal: "resumed session d097ae50" or "new session ..."."""
    if spec.mode == "resume":
        return f"resumed session {spec.session_id[:8]}"
    return f"new session {spec.session_id[:8]}" + (" (the earlier one was lost or reset)" if note else "")


def open_session(cfg: Config, state: State, ref: str, new_session: bool = False) -> int:
    """Show the worker's session window to the person; start one if the session is not running."""
    from . import windows  # windows imports this module

    row = resolve(cfg, state, ref)
    tmux = Tmux(cfg)
    window = tmux.window(row["id"])
    if window and new_session:
        raise ConfigError(f"{name(row)} is running in tmux window {window.id}; close that window first "
                          f"({shlex.join([*tmux.base, 'kill-window', '-t', window.id])}) to start a new session")
    if window is None:
        if is_busy(cfg, row):
            raise ConfigError(f"{name(row)} is busy; try again in a moment")
        claude = Claude(cfg)
        spec, note = spec_for(cfg, claude, state, row, force_new=new_session)
        if spec.mode == "new":
            spec.prompt = f"{note}\n\n{OPEN_NEW_PROMPT}" if note else OPEN_NEW_PROMPT
        window_id = windows.launch(cfg, state, tmux, claude, spec, "open")
        log.info("%s: Started interactive session, %s, tmux window %s", label(row), describe(spec, note), window_id)
    else:
        window_id = window.id
    if tmux.attach(window_id):
        print(f"Switched to {name(row)} (tmux session {tmux.session}, window {window_id}). Switch back to your own "
              "window whenever you like: the agent keeps working, and vahtovik closes the window when it is done.")
    else:
        print(f"Detached. The session keeps running in tmux window {window_id}; vahtovik closes the window when "
              f"the agent is done. `vahtovik open {name(row)}` brings you back.")
    return 0


def memory_text(cfg: Config, row) -> str:
    return memory.prompt_text(role_of(cfg, row))
