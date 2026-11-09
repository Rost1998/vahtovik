import time
from datetime import datetime, timedelta, timezone

import yaml
from conftest import monitor_answer

from vahtovik import cli, watchdog
from vahtovik.util import utc_iso


def _monitored(project) -> list[str]:
    """The sources the monitoring sessions ran for, by their working folders."""
    return [call["cwd"].rsplit("/", 1)[1] for call in project.calls()]


def _set_interval(project, source: int, interval: str) -> None:
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][source]["interval"] = interval
    path.write_text(yaml.safe_dump(data))


def _run(*args: str) -> None:
    cli.main(["run", *args])
    time.sleep(1.05)  # a run's id is its start time, to the second


def _ago(project, source_id: str, minutes: int) -> None:
    _cfg, state, _ = project.load()
    state.execute("UPDATE source_state SET last_run_at = ? WHERE source_id = ?",
                  utc_iso(datetime.now(timezone.utc) - timedelta(minutes=minutes)), source_id)


def test_each_source_is_monitored_on_its_own_interval(project):
    _set_interval(project, 1, "1m")
    project.queue(monitor_answer(), monitor_answer())
    _run()
    assert _monitored(project) == ["src-a", "src-b"]
    _ago(project, "src-b", 2)
    project.queue(monitor_answer())
    _run()
    assert _monitored(project) == ["src-a", "src-b", "src-b"]
    _run()  # src-b was monitored just now, src-a an hour from the first run
    assert len(project.calls()) == 3
    project.queue(monitor_answer(), monitor_answer())
    _run("--force")
    assert _monitored(project)[3:] == ["src-a", "src-b"]


def test_a_stage_run_does_not_move_the_sources_times(project):
    project.queue(monitor_answer(), monitor_answer())
    cli.main(["run", "--stage", "monitor"])
    _cfg, state, _ = project.load()
    assert state.source_last_run("w1", "src-a") is None


def test_the_sources_take_their_watchs_time_from_an_older_database(project):
    cfg, state, _ = project.load()
    state.execute("INSERT INTO watch_state (watch, last_run_at) VALUES ('w1', '2026-10-01T10:00:00+00:00')")
    state.source_ok("w1", "src-a")
    state.execute("ALTER TABLE source_state DROP COLUMN last_run_at")
    state.close()
    _cfg, state, _ = project.load()
    assert state.source_last_run("w1", "src-a") == "2026-10-01T10:00:00+00:00"


def test_the_watchdog_runs_once_its_period_has_passed(project, monkeypatch):
    ticks = []
    monkeypatch.setattr(watchdog, "tick", lambda *args: ticks.append(1))
    project.queue(monitor_answer(), monitor_answer())
    _run()
    _run()
    assert len(ticks) == 1
    _cfg, state, _ = project.load()
    state.set_meta("watchdog_at", utc_iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
    _run()
    assert len(ticks) == 2
