"""Small helpers shared by all modules."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from string import Template


def utc_iso(dt: datetime | None = None) -> str:
    """Timestamp for the database: UTC, so string order is time order."""
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def human(dt: datetime | None = None) -> str:
    """Timestamp for people: local time, minutes."""
    return (dt or datetime.now()).astimezone().strftime("%Y-%m-%d %H:%M")


def duration(seconds: float) -> str:
    """Short duration for the journal: 28s, 1m30s, 2h05m."""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def slugify(text: str, max_len: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-")


def group_key(raw: str | None) -> str:
    return slugify(raw or "") or "misc"


def artifact_filename(artifact_id: str) -> str:
    digest = hashlib.sha1(artifact_id.encode()).hexdigest()[:8]
    return f"{slugify(artifact_id, 48) or 'artifact'}-{digest}.json"


def tail(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def render(template_name: str, **values: object) -> str:
    """Fill a template from the prompts/ package folder ($name placeholders)."""
    text = resources.files("vahtovik").joinpath("prompts", template_name).read_text()
    return Template(text).substitute({k: str(v) for k, v in values.items()})


def load_schema(name: str) -> dict:
    return json.loads(resources.files("vahtovik").joinpath("schemas", f"{name}.json").read_text())


def schema_path(name: str) -> Path:
    return Path(str(resources.files("vahtovik").joinpath("schemas", f"{name}.json")))


class LockBusy(Exception):
    pass


@contextmanager
def file_lock(path: Path, wait: bool = False):
    """Exclusive flock on `path`. Raises LockBusy at once unless `wait` is set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(str(path)) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
