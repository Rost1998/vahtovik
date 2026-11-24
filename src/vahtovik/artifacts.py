"""Register artifacts: from monitoring, from the person, and from workers."""

from __future__ import annotations

import json
import logging

from . import context_file, workers
from .config import Config, Poller
from .state import State
from .util import artifact_filename, utc_iso, worker_key, write_atomic

log = logging.getLogger(__name__)

FIELDS = ("id", "title", "url", "summary", "details")


def save(cfg: Config, state: State, row, data: dict, source_id: str, poller: str | None = None,
         origin: str = "monitor", status: str = "new") -> bool:
    """Store an artifact in a worker: its file, its row in context.md, and its database row. Returns False if the
    worker has it already."""
    artifact_id = str(data["id"]).strip()
    if not artifact_id or state.artifact_exists(row["id"], artifact_id):
        return False
    folder = workers.folder(cfg, row)
    record = {f: data.get(f, "") for f in FIELDS}
    record.update(id=artifact_id, worker=workers.name(row), role=row["role"], source_id=source_id, poller=poller or "",
                  origin=origin, first_seen=utc_iso())
    filename = artifact_filename(artifact_id)
    (folder / "artifacts").mkdir(parents=True, exist_ok=True)
    write_atomic(folder / "artifacts" / filename, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    context_file.append(folder / "context.md", "Artifacts",
                        context_file.artifact_row(source_id, record["title"], record["url"]))
    # The database row comes last: if the run stops before it, the artifact counts as unseen and is saved again.
    state.add_artifact(row["id"], artifact_id, poller, source_id, record["title"], record["url"], origin, status,
                       filename)
    state.touch_worker(row["id"])
    return True


def from_poller(cfg: Config, state: State, poller: Poller, item: dict, source_id: str) -> bool:
    """A new artifact from monitoring. It is dropped if the poller brought it before. Otherwise it goes to the
    worker of its role and key (or the worker that one was merged into), which is spawned if there is none.
    Returns whether it was new."""
    artifact_id = str(item["id"]).strip()
    if not artifact_id or state.poller_has(poller.name, artifact_id):
        return False
    role = str(item.get("role") or poller.roles[0])
    if role not in poller.roles:
        log.warning("%s/%s: Gave artifact %s to role %s: the session named role %s, which the poller does not have",
                    poller.name, source_id, artifact_id, poller.roles[0], role)
        role = poller.roles[0]
    key = worker_key(item.get("worker"))
    row = state.find_worker(role, key)
    if row is None:
        row = workers.create(cfg, state, role, key, str(item.get("worker_title") or item.get("title") or key),
                             f"poller:{poller.name}")
    return save(cfg, state, row, item, source_id, poller.name)
