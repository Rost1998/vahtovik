"""What group sessions cost.

Claude Code adds a `cost-state` line with the session's running total to its conversation file whenever the
session's window ends, however it ends. The total goes on across resumes. vahtovik keeps the last total of
every session a group had, since Claude Code deletes old conversation files. A running window's cost shows
once the window ends.
"""

from __future__ import annotations

import json
from pathlib import Path

from .claude_session import Claude
from .state import State


def last_total(path: Path) -> float | None:
    """The last running total in a conversation file; None if it has none yet."""
    total = None
    with path.open(errors="replace") as lines:
        for line in lines:
            if '"cost-state"' not in line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("type") == "cost-state":
                total = float(data.get("totalCostUSD") or 0)
    return total


def update(state: State, claude: Claude) -> None:
    """Read the totals of the group sessions whose conversation file changed since the last update."""
    for row in state.session_costs():
        path = claude.session_file(row["session_id"])
        try:
            mtime = path.stat().st_mtime if path else None
        except OSError:
            continue
        if mtime is None or mtime == row["file_mtime"]:
            continue
        total = last_total(path)
        state.set_session_cost(row["session_id"], row["cost_usd"] if total is None else total, mtime)
