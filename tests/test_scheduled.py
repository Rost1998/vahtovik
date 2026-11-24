from datetime import datetime, timedelta, timezone

import pytest
from conftest import artifact, give, run_turns, turn_answer, work_log, worker

from vahtovik import cli, operations, watchdog, worker_status, workers
from vahtovik.config import parse_moment
from vahtovik.tmux import Tmux
from vahtovik.util import utc_iso


def _scheduled_action(*extra: str) -> dict:
    return {"run": ["vahtovik", "turn", "scheduled", "--importance", "low", "--summary", "Waiting for 1.0.",
                    *extra]}


def _turns(project, run_id: str = "run1"):
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, run_id)
    run_turns(cfg, state, claude, Tmux(cfg), run_id=run_id)
    return cfg, state


def _due_now(state, worker_id: str) -> None:
    state.execute("UPDATE workers SET scheduled_at = ? WHERE id = ?",
                  utc_iso(datetime.now(timezone.utc) - timedelta(seconds=1)), worker_id)


def _in(row) -> timedelta:
    return datetime.fromisoformat(row["scheduled_at"]) - datetime.now(timezone.utc)


def test_a_scheduled_turn_comes_without_new_artifacts(project):
    give(project, artifact("a1", "g"))
    project.queue({"mode": "interactive", "actions": [work_log(), _scheduled_action("--after", "2h", "--reason",
                                                                                    "see whether 1.0 is out")]})
    cfg, state = _turns(project)
    row = state.active_worker("r1", "g")
    assert row["status"] == "scheduled" and row["scheduled_reason"] == "see whether 1.0 is out"
    assert timedelta(hours=1, minutes=59) < _in(row) <= timedelta(hours=2)
    assert state.pending_workers(3) == []

    _due_now(state, row["id"])
    assert state.pending_workers(3) == [row["id"]]
    project.queue(turn_answer([work_log("Checked.")]))
    cfg, state = _turns(project, "run2")
    prompt = project.calls()[1]["prompt"]
    assert "This is a scheduled turn: see whether 1.0 is out." in prompt and "## New artifacts\n\n- none" in prompt
    row = state.worker(row["id"])
    assert row["status"] == "finished" and row["scheduled_at"] is None
    assert state.row("SELECT scheduled_for FROM turns WHERE run_id = 'run2'")["scheduled_for"]


def test_a_turn_can_be_scheduled_at_a_time_of_the_day(project):
    give(project, artifact("a1", "g"))
    project.queue({"mode": "interactive", "actions": [work_log(), _scheduled_action("--at", "07:30")]})
    cfg, state = _turns(project)
    row = state.active_worker("r1", "g")
    at = datetime.fromisoformat(row["scheduled_at"]).astimezone()
    assert row["status"] == "scheduled" and at.strftime("%H:%M") == "07:30"
    assert timedelta(0) < _in(row) <= timedelta(days=1)
    assert row["scheduled_reason"] == "a check you scheduled"
    workers.prepare(cfg, state, row)  # as before the next start
    assert f"Your next scheduled turn is at {at:%Y-%m-%d} 07:30" in (workers.folder(cfg, row) / "CLAUDE.md").read_text()


def test_at_takes_a_time_of_the_day_or_a_date_and_time():
    now = datetime(2026, 11, 24, 10, 0).astimezone()
    assert parse_moment("09:30", now) == datetime(2026, 11, 25, 9, 30).astimezone()
    assert parse_moment("18:00", now) == datetime(2026, 11, 24, 18, 0).astimezone()
    assert parse_moment("2026-12-01 08:15", now) == datetime(2026, 12, 1, 8, 15).astimezone()
    for bad in ("soon", "25:00", "2026-11-24 09:00", "2026-13-01 08:00"):
        with pytest.raises(cli.ConfigError):
            parse_moment(bad, now)


def test_finished_means_done_and_cancels_the_scheduled_turn(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    operations.schedule(cfg, state, row["id"], after="3h", reason="check")
    state.prompt_submitted(row["id"])  # a person wrote to the agent
    worker_status.declare(cfg, state, state.worker(row["id"]), "finished")
    row = state.worker(row["id"])
    assert (row["status"], row["scheduled_at"], row["scheduled_reason"]) == ("finished", None, None)
    with pytest.raises(worker_status.StatusError, match="no scheduled turn to keep"):
        worker_status.declare(cfg, state, row, "scheduled")


def test_a_plain_scheduled_keeps_the_scheduled_turn_after_a_reply(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    operations.schedule(cfg, state, row["id"], after="3h", reason="check the release")
    at = state.worker(row["id"])["scheduled_at"]
    state.prompt_submitted(row["id"])  # a person wrote to the agent
    assert "recorded: status scheduled for" in worker_status.declare(cfg, state, state.worker(row["id"]), "scheduled")
    row = state.worker(row["id"])
    assert (row["status"], row["scheduled_at"], row["scheduled_reason"]) == ("scheduled", at, "check the release")


def test_waiting_keeps_the_scheduled_turn_for_after_the_answer(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    operations.schedule(cfg, state, row["id"], after="3h", reason="check")
    worker_status.declare(cfg, state, state.worker(row["id"]), "waiting", questions=["Which one?"])
    row = state.worker(row["id"])
    assert row["status"] == "waiting" and row["scheduled_at"]


def test_the_person_can_schedule_and_cancel_a_turn(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    lines = operations.schedule(cfg, state, "r1/g", after="3h", reason="check the release notes")
    assert "next scheduled turn:" in lines[0] and "check the release notes" in lines[0]
    row = state.worker(row["id"])
    assert row["status"] == "scheduled" and timedelta(hours=2) < _in(row) <= timedelta(hours=3)

    operations.schedule(cfg, state, "r1/g", off=True)
    assert state.worker(row["id"])["status"] == "idle"  # it never had a turn

    operations.schedule(cfg, state, f"{row['id']}-g", at="2099-01-01 08:00")
    assert state.worker(row["id"])["scheduled_at"].startswith("2099-01-01")

    state.set_status(row["id"], "running")
    lines = operations.schedule(cfg, state, row["id"], after="1h")
    assert state.worker(row["id"])["status"] == "running" and "cancels the scheduled turn" in lines[1]
    with pytest.raises(cli.ConfigError):
        operations.schedule(cfg, state, row["id"], after="soon")
    with pytest.raises(cli.ConfigError, match="one of"):
        operations.schedule(cfg, state, row["id"], after="1h", off=True)


def test_a_failed_scheduled_turn_is_tried_again(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    operations.schedule(cfg, state, row["id"], after="1h", reason="check")
    _due_now(state, row["id"])
    for attempt in range(1, 4):
        project.queue({"actions": [work_log()]})  # never runs `vahtovik turn`
        cfg, state = _turns(project, f"run{attempt}")
        current = state.worker(row["id"])
        assert current["status"] == "failed" and current["scheduled_attempts"] == attempt and current["scheduled_at"]
    assert state.pending_workers(3) == []
    _turns(project, "run4")
    assert len(project.calls()) == 3
