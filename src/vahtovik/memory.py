"""A role's memory, `roles/<role>/MEMORY.md`: notes that agents save for all workers of the role with
`vahtovik memory add`. Its workers read the file, and their turn prompts say which entries came, went or changed;
the monitoring sessions of its pollers get it in their prompts.

The file sits next to the role's rules, so it goes to git with them; the person commits it, never vahtovik.
Each entry is headed `## <number> · <time> · <who>`. The next entry gets the highest number plus one, so the
numbers of the other entries never change when one is removed. The person may edit the file by hand."""

from __future__ import annotations

import logging
import os
import re

from .config import Config, ConfigError, Role, read_env_file
from .util import file_lock, headings_down, human, write_atomic

log = logging.getLogger(__name__)

FILE_NAME = "MEMORY.md"
MIN_SECRET = 8  # shorter values, such as "1" or "true", are too common in ordinary text to count as secrets
HEADER = ("# Memory of role `{role}`\n\n"
          "Notes that vahtovik's agents saved for all workers of the role with `vahtovik memory add`. They are "
          "notes, not rules: the role's rules win. You may edit or delete entries by hand.\n")
ENTRY = re.compile(r"^## (\d+) · ", re.MULTILINE)
HEADING = re.compile(r"^## ", re.MULTILINE)  # an entry, also one that the person wrote by hand
NO_ENTRIES = "(no entries yet)"


def prompt_text(role: Role) -> str:
    """The memory for a session's prompt."""
    text = role.memory_md.read_text().strip() if role.memory_md.is_file() else ""
    return text if HEADING.search(text) else NO_ENTRIES


def entries(role: Role) -> dict[int, str]:
    """The numbered entries of the role's memory: each entry's number and its text, heading included."""
    text = role.memory_md.read_text() if role.memory_md.is_file() else ""
    found = {}
    for match in ENTRY.finditer(text):
        later = HEADING.search(text, match.end())
        found[int(match.group(1))] = text[match.start():later.start() if later else len(text)].strip()
    return found


def saved_by(entry: str) -> str:
    """Who saved an entry, from its heading `## <number> · <time> · <who>`."""
    return entry.split("\n", 1)[0].rpartition(" · ")[2].strip()


def _role(cfg: Config, name: str | None) -> Role:
    """The role to change: the given one, or the worker's own. A worker may change only its own role's memory."""
    own = os.environ.get("VAHTOVIK_ROLE")
    name = name or own
    if not name:
        raise ConfigError("give --role, or run this inside a worker")
    if own and name != own:
        raise ConfigError(f"you are a worker of role {own}: you may change only its memory, not that of {name}")
    return cfg.role(name)


def _who() -> tuple[str, str]:
    """Who saves, for the entry's heading, and the scope of the journal line."""
    env = os.environ
    if env.get("VAHTOVIK_WORKER_NAME"):
        return f"worker {env['VAHTOVIK_WORKER_NAME']}", f"{env.get('VAHTOVIK_ROLE', '')}/{env['VAHTOVIK_WORKER_NAME']}"
    if env.get("VAHTOVIK_CHANNEL"):
        return f"channel {env['VAHTOVIK_CHANNEL']}", env["VAHTOVIK_CHANNEL"]
    if env.get("VAHTOVIK_SOURCE"):
        scope = f"{env.get('VAHTOVIK_POLLER', '')}/{env['VAHTOVIK_SOURCE']}"
        return f"source {scope}", scope
    return "the person", ""


def _check_secrets(cfg: Config, text: str) -> None:
    """The file goes to git: refuse a text that holds a secret from any credentials file."""
    folder = cfg.credentials_dir
    secrets = {}
    for path in sorted(folder.glob("*.env")) if folder.is_dir() else []:
        secrets.update({f"{name} ({path.name})": value for name, value in read_env_file(path).items()})
    found = sorted(name for name, value in secrets.items() if len(value) >= MIN_SECRET and value in text)
    if found:
        raise ConfigError(f"the text holds the secret {', '.join(found)}; the memory goes to git, so leave it out")


def _lock(cfg: Config, role: Role):
    return file_lock(cfg.data_dir / "memory" / f"{role.name}.lock", wait=True)


def add(cfg: Config, role_name: str | None, text: str) -> list[str]:
    """Append an entry to the role's memory."""
    role = _role(cfg, role_name)
    text = text.strip()
    if not text:
        raise ConfigError("give the text with --text, or on stdin")
    _check_secrets(cfg, text)
    who, scope = _who()
    with _lock(cfg, role):
        path = role.memory_md
        old = path.read_text() if path.is_file() else HEADER.format(role=role.name)
        number = max((int(n) for n in ENTRY.findall(old)), default=0) + 1
        write_atomic(path, f"{old.rstrip()}\n\n## {number} · {human()} · {who}\n\n{headings_down(text)}\n")
    log.info("%s: Saved entry %d to the memory of role %s: %s", scope or role.name, number, role.name, text[:100])
    return [f"saved entry {number} to {_shown(cfg, path)}"]


def remove(cfg: Config, role_name: str | None, entry: int, reason: str) -> list[str]:
    """Remove an entry from the role's memory."""
    role = _role(cfg, role_name)
    reason = reason.strip()
    if not reason:
        raise ConfigError("give --reason: why the entry goes")
    _who_label, scope = _who()
    with _lock(cfg, role):
        path = role.memory_md
        text = path.read_text() if path.is_file() else ""
        entries = list(ENTRY.finditer(text))
        found = next((m for m in entries if int(m.group(1)) == entry), None)
        if found is None:
            known = ", ".join(m.group(1) for m in entries) or "none"
            raise ConfigError(f"the memory of role {role.name} has no entry {entry}; entries: {known}")
        later = HEADING.search(text, found.end())
        end = later.start() if later else len(text)
        removed = text[found.start():end]
        write_atomic(path, (text[:found.start()].rstrip() + "\n\n" + text[end:]).rstrip() + "\n")
    body = removed.split("\n", 1)[1].strip() if "\n" in removed else ""
    log.info("%s: Removed entry %d from the memory of role %s (%s): %s", scope or role.name, entry, role.name,
             reason, body[:100])
    return [f"removed entry {entry} from {_shown(cfg, path)}"]


def _shown(cfg: Config, path) -> str:
    return str(path.relative_to(cfg.root)) if path.is_relative_to(cfg.root) else str(path)
