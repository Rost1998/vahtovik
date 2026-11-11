"""The watch's memory, `watches/<watch>/MEMORY.md`: notes that agents save for the whole watch with
`vahtovik memory add`, and that group and monitoring sessions get in their prompts.

The file sits next to the watch's rules, so it goes to git with them; the person commits it, never vahtovik.
Each entry is headed `## <number> · <time> · <who>`. The next entry gets the highest number plus one, so the
numbers of the other entries never change when one is removed. The person may edit the file by hand."""

from __future__ import annotations

import logging
import os
import re

from .config import Config, ConfigError, Watch, read_env_file
from .util import file_lock, headings_down, human, write_atomic

log = logging.getLogger(__name__)

FILE_NAME = "MEMORY.md"
MIN_SECRET = 8  # shorter values, such as "1" or "true", are too common in ordinary text to count as secrets
HEADER = ("# Memory of watch `{watch}`\n\n"
          "Notes that vahtovik's agents saved for the whole watch with `vahtovik memory add`. They are notes, not "
          "rules: the watch's rules win. You may edit or delete entries by hand.\n")
ENTRY = re.compile(r"^## (\d+) · ", re.MULTILINE)
HEADING = re.compile(r"^## ", re.MULTILINE)  # an entry, also one that the person wrote by hand
NO_ENTRIES = "(no entries yet)"


def prompt_text(watch: Watch) -> str:
    """The memory for a session's prompt."""
    text = watch.memory_md.read_text().strip() if watch.memory_md.is_file() else ""
    return text if HEADING.search(text) else NO_ENTRIES


def _watch(cfg: Config, name: str | None) -> Watch:
    """The watch to change: the given one, or the session's own. A session may change only its own watch's memory."""
    own = os.environ.get("VAHTOVIK_WATCH")
    name = name or own
    if not name:
        raise ConfigError("give --watch, or run this inside a session of a watch")
    if own and name != own:
        raise ConfigError(f"you work for watch {own}: you may change only its memory, not that of {name}")
    return cfg.watch(name)


def _who() -> tuple[str, str]:
    """Who saves, for the entry's heading, and the scope of the journal line."""
    env = os.environ
    watch = env.get("VAHTOVIK_WATCH", "")
    if env.get("VAHTOVIK_GROUP"):
        return f"group {env['VAHTOVIK_GROUP']}", f"{watch}/{env['VAHTOVIK_GROUP']}"
    if env.get("VAHTOVIK_CHANNEL"):
        return f"channel {env['VAHTOVIK_CHANNEL']}", env["VAHTOVIK_CHANNEL"]
    if env.get("VAHTOVIK_SOURCE"):
        return f"source {env['VAHTOVIK_SOURCE']}", f"{watch}/{env['VAHTOVIK_SOURCE']}"
    return "the person", ""


def _check_secrets(cfg: Config, watch: Watch, text: str) -> None:
    """The file goes to git: refuse a text that holds a secret from the watch's or the shared .env."""
    secrets = {**read_env_file(cfg.root / ".env"), **watch.secrets()}
    found = sorted(name for name, value in secrets.items() if len(value) >= MIN_SECRET and value in text)
    if found:
        raise ConfigError(f"the text holds the secret {', '.join(found)}; the memory goes to git, so leave it out")


def _lock(cfg: Config, watch: Watch):
    return file_lock(cfg.data_dir / "watches" / watch.name / "memory.lock", wait=True)


def add(cfg: Config, watch_name: str | None, text: str) -> list[str]:
    """Append an entry to the watch's memory."""
    watch = _watch(cfg, watch_name)
    text = text.strip()
    if not text:
        raise ConfigError("give the text with --text, or on stdin")
    _check_secrets(cfg, watch, text)
    who, scope = _who()
    with _lock(cfg, watch):
        path = watch.memory_md
        old = path.read_text() if path.is_file() else HEADER.format(watch=watch.name)
        number = max((int(n) for n in ENTRY.findall(old)), default=0) + 1
        write_atomic(path, f"{old.rstrip()}\n\n## {number} · {human()} · {who}\n\n{headings_down(text)}\n")
    log.info("%s: Saved entry %d to the memory of watch %s: %s", scope or watch.name, number, watch.name, text[:100])
    return [f"saved entry {number} to {_shown(cfg, path)}"]


def remove(cfg: Config, watch_name: str | None, entry: int, reason: str) -> list[str]:
    """Remove an entry from the watch's memory."""
    watch = _watch(cfg, watch_name)
    reason = reason.strip()
    if not reason:
        raise ConfigError("give --reason: why the entry goes")
    _who_label, scope = _who()
    with _lock(cfg, watch):
        path = watch.memory_md
        text = path.read_text() if path.is_file() else ""
        entries = list(ENTRY.finditer(text))
        found = next((m for m in entries if int(m.group(1)) == entry), None)
        if found is None:
            known = ", ".join(m.group(1) for m in entries) or "none"
            raise ConfigError(f"the memory of watch {watch.name} has no entry {entry}; entries: {known}")
        later = HEADING.search(text, found.end())
        end = later.start() if later else len(text)
        removed = text[found.start():end]
        write_atomic(path, (text[:found.start()].rstrip() + "\n\n" + text[end:]).rstrip() + "\n")
    body = removed.split("\n", 1)[1].strip() if "\n" in removed else ""
    log.info("%s: Removed entry %d from the memory of watch %s (%s): %s", scope or watch.name, entry, watch.name,
             reason, body[:100])
    return [f"removed entry {entry} from {_shown(cfg, path)}"]


def _shown(cfg: Config, path) -> str:
    return str(path.relative_to(cfg.root)) if path.is_relative_to(cfg.root) else str(path)
