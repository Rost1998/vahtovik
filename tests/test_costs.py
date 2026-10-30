import json
import os

from vahtovik import cli, costs
from vahtovik.util import utc_iso


def _conversation(claude, session_id: str, *totals: float, other: str = ""):
    """Add `cost-state` lines to a session's conversation file, as Claude Code does when its window ends."""
    path = claude.config_dir / "projects" / "fake" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(other)
        for total in totals:
            handle.write(json.dumps({"type": "cost-state", "sessionId": session_id, "totalCostUSD": total}) + "\n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))  # a change even within one clock tick
    return path


def test_status_shows_the_cost_of_each_group_its_watch_and_all(project, capsys):
    _cfg, state, claude = project.load()
    state.add_group("w1", "g", "G")
    state.add_group("w1", "h", "H")
    state.set_session("w1", "g", "s1")
    _conversation(claude, "s1", 0.5, 1.25, other='{"type": "user", "message": "about \\"cost-state\\""}\n')
    state.set_session("w1", "g", "s2")  # a new session for the same group
    _conversation(claude, "s2", 2.0)
    state.set_session("w1", "h", "s3")  # no window has ended yet
    _conversation(claude, "s3")
    state.execute("INSERT INTO artifacts (watch, id, group_key, status) VALUES "
                  "('w1', 'a1', 'g', 'processed'), ('w1', 'a2', 'g', 'new')")
    state.set_status("w1", "h", "waiting", questions=["Why?", "When?"])
    state.execute("UPDATE groups SET updated_at = '2026-10-29T10:00:00+00:00' WHERE key = 'g'")
    state.execute("UPDATE groups SET updated_at = '2026-10-29T11:00:00+00:00' WHERE key = 'h'")  # latest first
    state.execute("INSERT INTO sessions (stage, watch, started_at, cost_usd) VALUES "
                  "('monitor', 'w1', '2000-01-01T00:00:00+00:00', 9.0), ('monitor', 'w1', ?, 0.4)", utc_iso())
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "watch        | period | groups | status                | artifacts |  cost",
        "-------------+--------+--------+-----------------------+-----------+------",
        "w1 (enabled) | 1h     | h      | waiting, 2 questions  |         0 | $0.00",
        "             |        | g      | idle                  |         1 | $3.25",
        "             |        | total  |                       |         1 | $3.25",
        "-------------+--------+--------+-----------------------+-----------+------",
        "all          |        |        | 0 undelivered reports |         1 | $3.25",
        "monitor and watchdog sessions in the last 24 hours: $0.40",
    ]
    assert cli.main(["status", "-v"]) == 0
    out = capsys.readouterr().out
    assert "last run never, group sessions $3.25\n" in out
    assert "session s2, $3.25" in out and "session s3, $0.00" in out
    assert "group sessions in all watches: $3.25" in out


def test_a_session_keeps_its_last_total(project):
    _cfg, state, claude = project.load()
    state.add_group("w1", "g", "G")
    state.set_session("w1", "g", "s1")
    path = _conversation(claude, "s1", 1.0)
    costs.update(state, claude)
    _conversation(claude, "s1", 1.5)  # a later window of the same session
    costs.update(state, claude)
    assert state.group_costs() == {("w1", "g"): 1.5}
    path.unlink()  # Claude Code deletes old conversation files
    costs.update(state, claude)
    assert state.group_costs() == {("w1", "g"): 1.5}


def test_sessions_from_before_cost_tracking_count_too(project):
    _cfg, state, claude = project.load()
    state.add_group("w1", "g", "G")
    state.execute("UPDATE groups SET session_id = 's0' WHERE key = 'g'")
    _conversation(claude, "s0", 0.75)
    costs.update(state, claude)
    assert state.group_costs() == {("w1", "g"): 0.75}
