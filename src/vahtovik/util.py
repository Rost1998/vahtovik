"""Small helpers shared by all modules."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
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


def headings_down(text: str) -> str:
    """Make every heading level 4, so text put under a level-2 or level-3 heading stays inside that one part."""
    return re.sub(r"^#+ ", "#### ", text, flags=re.MULTILINE)


def render(template_name: str, **values: object) -> str:
    """Fill a template from the prompts/ package folder ($name placeholders)."""
    text = resources.files("vahtovik").joinpath("prompts", template_name).read_text()
    return Template(text).substitute({k: str(v) for k, v in values.items()})


def load_schema(name: str) -> dict:
    return json.loads(resources.files("vahtovik").joinpath("schemas", f"{name}.json").read_text())


def schema_path(name: str) -> Path:
    return Path(str(resources.files("vahtovik").joinpath("schemas", f"{name}.json")))


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step, so a stopped run never leaves half a file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class LockBusy(Exception):
    pass


@contextmanager
def file_lock(path: Path, wait: bool = False):
    """Exclusive flock on `path`, yielding the open file. Raises LockBusy at once unless `wait` is set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(str(path)) from None
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class StateBackup:
    """A whole copy of a script's state folder, kept while a script and an AI session work: the scripts move their
    "already seen" markers, so until vahtovik has saved what they found, the old folder must come back on failure.
    A backup found at the start means the last run stopped before it saved: `recover()` puts it back.
    Renames are atomic, so the backup folder only ever holds a whole copy."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.backup = folder.with_name(f".{folder.name}.backup")
        self.partial = folder.with_name(f".{folder.name}.partial")

    def recover(self) -> bool:
        """Put back a backup that a stopped run left. Returns whether there was one."""
        shutil.rmtree(self.partial, ignore_errors=True)
        found = self.backup.exists()
        if found:
            self.restore()
        self.folder.mkdir(parents=True, exist_ok=True)
        return found

    def take(self) -> None:
        shutil.copytree(self.folder, self.partial)
        self.partial.rename(self.backup)

    def restore(self) -> None:
        """Put the folder back as it was when the backup was taken."""
        shutil.rmtree(self.folder, ignore_errors=True)
        self.backup.rename(self.folder)

    def drop(self) -> None:
        self.backup.rename(self.partial)  # from here a half-removed backup is never taken for a whole one
        shutil.rmtree(self.partial)


def run_json_script(path: Path, env: dict[str, str], cwd: Path, timeout: int) -> tuple[dict | None, str | None]:
    """Run a skill's script that prints one JSON object with an `error` field.
    Returns (the object, None), or (None, what went wrong)."""
    try:
        proc = subprocess.run([str(path)], capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{path.name} timed out after {timeout} s"
    except OSError as exc:
        return None, f"cannot run {path}: {exc}"
    if proc.returncode != 0:
        return None, f"{path.name} failed: {tail(proc.stderr, 300) or f'exit code {proc.returncode}'}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, f"{path.name} did not print JSON: {tail(proc.stdout, 200) or 'no output'}"
    if not isinstance(data, dict):
        return None, f"{path.name} did not print a JSON object"
    if data.get("error"):
        return None, f"{path.name}: {data['error']}"
    return data, None
