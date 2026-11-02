"""What group sessions cost, and when.

Claude Code adds a `cost-state` line with the session's running total to its conversation file whenever the
session's window ends, however it ends. The total goes on across resumes. The line has no time of its own, so
vahtovik dates each total by the last message before it. vahtovik keeps every total a group session reached,
with its time, since Claude Code deletes old conversation files. A running window's cost shows once the window
ends, and all of it counts at that time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .claude_session import Claude
from .state import State
from .util import utc_iso


def points(path: Path) -> list[tuple[str, float]]:
    """Each time the running total in a conversation file grew: (time, total)."""
    found: list[tuple[str, float]] = []
    last_at = None
    with path.open(errors="replace") as lines:
        for line in lines:
            if '"cost-state"' not in line and '"timestamp"' not in line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "cost-state":
                total = float(data.get("totalCostUSD") or 0)
                if total > (found[-1][1] if found else 0):
                    found.append((last_at or _file_time(path.stat().st_mtime), total))
            elif isinstance(data.get("timestamp"), str):
                try:
                    last_at = utc_iso(datetime.fromisoformat(data["timestamp"]))
                except ValueError:
                    pass
    return found


def update(state: State, claude: Claude) -> None:
    """Read the totals of the group sessions whose conversation file changed since the last update. A session
    whose total vahtovik kept before it kept times gets its totals and times once, from its file if Claude Code
    still has it, or else its last total at the time its file last changed."""
    tops = state.cost_tops()
    for row in state.session_costs():
        session_id, top = row["session_id"], tops.get(row["session_id"])
        undated = top is None and bool(row["cost_usd"])
        path = claude.session_file(session_id)
        try:
            mtime = path.stat().st_mtime if path else None
        except OSError:
            mtime = None
        if mtime is not None and (mtime != row["file_mtime"] or undated):
            found = points(path)
            state.add_cost_points(session_id, [(at, total) for at, total in found if total > (top or 0)])
            state.set_session_cost(session_id, found[-1][1] if found else row["cost_usd"], mtime)
        elif mtime is None and undated:
            state.add_cost_points(session_id, [(_file_time(row["file_mtime"]), row["cost_usd"])])


def _file_time(mtime: float | None) -> str:
    return utc_iso(datetime.fromtimestamp(mtime, timezone.utc)) if mtime else utc_iso()
