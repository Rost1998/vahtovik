"""Register artifacts from monitoring output and from files that agents add."""

from __future__ import annotations

import json
import logging

import jsonschema

from . import context_file, groups
from .config import Config, Watch
from .state import State
from .util import artifact_filename, group_key, load_schema, utc_iso

log = logging.getLogger(__name__)

FIELDS = ("id", "title", "url", "summary", "details")


def register(cfg: Config, state: State, watch: Watch, data: dict, source_id: str,
             origin: str = "monitor", status: str = "new") -> bool:
    """Store a new artifact in its group. Returns False if it was seen before."""
    artifact_id = str(data["id"]).strip()
    if not artifact_id or state.artifact_exists(watch.name, artifact_id):
        return False
    key = group_key(data.get("group_key"))
    group_dir = groups.ensure_group(cfg, state, watch, key, str(data.get("group_title") or data.get("title") or key))
    record = {f: data.get(f, "") for f in FIELDS}
    record.update(id=artifact_id, group_key=key, source_id=source_id, origin=origin, first_seen=utc_iso())
    filename = artifact_filename(artifact_id)
    (group_dir / "artifacts" / filename).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    state.add_artifact(watch.name, artifact_id, key, source_id, record["title"], record["url"], origin, status, filename)
    context_file.append(group_dir / "context.md", "Artifacts",
                        context_file.artifact_row(source_id, record["title"], record["url"]))
    state.touch_group(watch.name, key)
    return True


def register_agent_files(cfg: Config, state: State, watch: Watch, key: str) -> list[str]:
    """Pick up JSON files an agent wrote into its artifacts/ folder.
    Artifacts for this group count as processed (the agent found them while working);
    artifacts with another group's key move there and wait for that group's next turn."""
    folder = cfg.group_dir(watch.name, key) / "artifacts"
    if not folder.is_dir():
        return []
    schema = load_schema("artifact")
    known = state.artifact_files(watch.name, key)
    notes = []
    for path in sorted(folder.glob("*.json")):
        if path.name in known:
            continue
        try:
            data = json.loads(path.read_text())
            jsonschema.validate(data, schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            message = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
            path.rename(path.with_name(path.name + ".invalid"))
            notes.append(f"{path.name}: invalid artifact file ({message}); renamed to .invalid")
            continue
        path.unlink()
        target = group_key(data.get("group_key") or key)
        if register(cfg, state, watch, {**data, "group_key": target}, str(data.get("source_id") or "agent"),
                    origin="agent", status="processed" if target == key else "new"):
            notes.append(f"registered {data['id']!r}" + (f" in group {target}" if target != key else ""))
        else:
            notes.append(f"{data['id']!r} was already known; file removed")
    for note in notes:
        log.info("%s/%s: %s", watch.name, key, note)
    return notes
