import json
import os
from datetime import datetime, timedelta, timezone

from vahtovik import cli, costs
from vahtovik.util import utc_iso


def _ago(**delta) -> str:
    """A time as Claude Code writes it in conversation files."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat().replace("+00:00", "Z")


def _conversation(claude, session_id: str, *totals: float | tuple[str, float], other: str = ""):
    """Add `cost-state` lines to a session's conversation file, as Claude Code does when its window ends.
    A total given as (time, total) comes after a message of that time."""
    path = claude.profile() / "projects" / "fake" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(other)
        for total in totals:
            if isinstance(total, tuple):
                at, total = total
                handle.write(json.dumps({"type": "assistant", "timestamp": at}) + "\n")
            handle.write(json.dumps({"type": "cost-state", "sessionId": session_id, "totalCostUSD": total}) + "\n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))  # a change even within one clock tick
    return path


def _worker(state, worker_id: str, key: str, parent: str | None = None) -> None:
    state.add_worker(worker_id, "r1", key, key.upper(), [], "person", parent)


def test_status_shows_the_cost_of_each_worker_its_role_and_all(project, capsys):
    _cfg, state, claude = project.load()
    _worker(state, "aaaaaa", "g")
    _worker(state, "bbbbbb", "h", parent="aaaaaa")
    state.set_session("aaaaaa", "s1")
    _conversation(claude, "s1", (_ago(days=40), 0.5), (_ago(days=10), 1.0), (_ago(hours=1), 1.25),
                  other='{"type": "user", "message": "about \\"cost-state\\""}\n')
    state.set_session("aaaaaa", "s2")  # a new session for the same worker
    _conversation(claude, "s2", 2.0)  # no message before it: the file's time
    state.set_session("bbbbbb", "s3")  # no window has ended yet
    _conversation(claude, "s3")
    state.execute("INSERT INTO artifacts (worker, id, status) VALUES "
                  "('aaaaaa', 'a1', 'processed'), ('aaaaaa', 'a2', 'new')")
    state.set_status("bbbbbb", "waiting", questions=["Why?", "When?"])
    state.execute("UPDATE workers SET updated_at = '2026-10-29T10:00:00+00:00' WHERE key = 'g'")
    state.execute("UPDATE workers SET updated_at = '2026-10-29T11:00:00+00:00' WHERE key = 'h'")  # latest first
    state.execute("INSERT INTO sessions (stage, scope, started_at, cost_usd) VALUES "
                  "('monitor', 'p1', '2000-01-01T00:00:00+00:00', 9.0), ('monitor', 'p1', ?, 1.0), "
                  "('monitor', 'p1', ?, 0.4), ('watchdog', '', ?, 0.1), ('input', '', ?, 0.2)",
                  utc_iso(datetime.now(timezone.utc) - timedelta(days=10)), utc_iso(), utc_iso(), utc_iso())
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "poller | state   | schedule | roles",
        "-------+---------+----------+------",
        "p1     | enabled | 1h (now) | r1",
        "",
        "role | workers  | status                 | parent | artifacts | 24 hours | 30 days",
        "-----+----------+------------------------+--------+-----------+----------+--------",
        "r1   | bbbbbb-h | waiting, 2 questions   | aaaaaa |         0 |    $0.00 |   $0.00",
        "     | aaaaaa-g | idle                   |        |         1 |    $2.25 |   $2.75",
        "     | total    |                        |        |         1 |    $2.25 |   $2.75",
        "-----+----------+------------------------+--------+-----------+----------+--------",
        "all  |          | 0 undelivered messages |        |         1 |    $2.25 |   $2.75",
        "",
        "sessions | 24 hours | 30 days",
        "---------+----------+--------",
        "workers  |    $2.25 |   $2.75",
        "monitor  |    $0.40 |   $1.40",
        "input    |    $0.20 |   $0.20",
        "watchdog |    $0.10 |   $0.10",
        "---------+----------+--------",
        "all      |    $2.95 |   $4.45",
    ]
    assert cli.main(["status", "-v"]) == 0
    out = capsys.readouterr().out
    assert "role r1: skills file, rss, workers $2.25 in 24 hours, $2.75 in 30 days, $3.25 in all time\n" in out
    assert "session s2 | $2.25 in 24 hours, $2.75 in 30 days, $3.25 in all time" in out
    assert "session s3 | $0.00 in 24 hours, $0.00 in 30 days, $0.00 in all time" in out
    assert out.endswith("sessions | 24 hours | 30 days | all time\n"
                        "---------+----------+---------+---------\n"
                        "workers  |    $2.25 |   $2.75 |    $3.25\n"
                        "monitor  |    $0.40 |   $1.40 |   $10.40\n"
                        "input    |    $0.20 |   $0.20 |    $0.20\n"
                        "watchdog |    $0.10 |   $0.10 |    $0.10\n"
                        "---------+----------+---------+---------\n"
                        "all      |    $2.95 |   $4.45 |   $13.95\n")


def test_a_session_keeps_its_last_total(project):
    _cfg, state, claude = project.load()
    _worker(state, "aaaaaa", "g")
    state.set_session("aaaaaa", "s1")
    path = _conversation(claude, "s1", 1.0)
    costs.update(state, claude)
    _conversation(claude, "s1", 1.5)  # a later window of the same session
    costs.update(state, claude)
    assert state.worker_costs() == {"aaaaaa": 1.5}
    path.unlink()  # Claude Code deletes old conversation files
    costs.update(state, claude)
    assert state.worker_costs() == {"aaaaaa": 1.5}


def test_status_shows_when_a_poller_is_monitored_next(project, capsys):
    _cfg, state, _claude = project.load()
    for source_id, minutes in (("src-a", 30), ("src-b", 10)):
        state.execute("INSERT INTO source_state (poller, source_id, last_run_at) VALUES ('p1', ?, ?)",
                      source_id, utc_iso(datetime.now(timezone.utc) - timedelta(minutes=minutes)))
    assert cli.main(["status"]) == 0
    period = capsys.readouterr().out.splitlines()[2].split(" | ")[2].strip()
    assert period in ("1h (in 30m00s)", "1h (in 29m59s)")


def test_a_total_is_dated_by_the_message_before_it(project, tmp_path):
    _cfg, _state, claude = project.load()
    path = _conversation(claude, "s1", ("2026-10-06T11:00:40.695Z", 0.5), 0.5, ("2026-10-07T09:30:00Z", 0.8))
    assert costs.points(path) == [("2026-10-06T11:00:40+00:00", 0.5), ("2026-10-07T09:30:00+00:00", 0.8)]
    undated = _conversation(claude, "s2", 0.3)
    mtime = datetime.fromtimestamp(undated.stat().st_mtime, timezone.utc)
    assert costs.points(undated) == [(utc_iso(mtime), 0.3)]


def test_cost_since_a_time_leaves_out_older_growth(project):
    _cfg, state, claude = project.load()
    _worker(state, "aaaaaa", "g")
    state.set_session("aaaaaa", "s1")
    _conversation(claude, "s1", (_ago(days=2), 1.0), (_ago(hours=2), 1.5))
    costs.update(state, claude)
    _conversation(claude, "s1", (_ago(minutes=1), 1.75))  # a later window
    costs.update(state, claude)
    day_ago = utc_iso(datetime.now(timezone.utc) - timedelta(days=1))
    assert state.worker_costs(day_ago) == {"aaaaaa": 0.75}
    assert state.worker_costs() == {"aaaaaa": 1.75}


def test_totals_kept_before_times_were_kept_get_times(project):
    _cfg, state, claude = project.load()
    _worker(state, "aaaaaa", "g")
    state.set_session("aaaaaa", "s1")
    state.set_session("aaaaaa", "s2")
    kept = _conversation(claude, "s1", (_ago(days=40), 0.5), (_ago(hours=1), 1.25))
    gone = _conversation(claude, "s2", 2.0)
    # What an older vahtovik stored: the last totals and file times, and no cost points.
    state.set_session_cost("s1", 1.25, kept.stat().st_mtime)
    state.set_session_cost("s2", 2.0, 1_700_000_000.0)  # 2023-11-14
    gone.unlink()
    costs.update(state, claude)
    assert state.worker_costs() == {"aaaaaa": 3.25}
    month_ago = utc_iso(datetime.now(timezone.utc) - timedelta(days=30))
    assert state.worker_costs(month_ago) == {"aaaaaa": 0.75}
    assert state.row("SELECT at FROM cost_points WHERE session_id = 's2'")["at"] == "2023-11-14T22:13:20+00:00"
