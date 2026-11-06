from datetime import datetime, timedelta, timezone

import pytest
from conftest import artifact, run_turns, turn_answer, work_log

from vahtovik import artifacts, cli, group_status, operations, watchdog
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


def _due_now(state, key: str = "g") -> None:
    state.execute("UPDATE groups SET scheduled_at = ? WHERE key = ?",
                  utc_iso(datetime.now(timezone.utc) - timedelta(seconds=1)), key)


def _in(group) -> timedelta:
    return datetime.fromisoformat(group["scheduled_at"]) - datetime.now(timezone.utc)


def test_a_scheduled_turn_comes_without_new_artifacts(project):
    cfg, state, _ = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    project.queue({"mode": "interactive", "actions": [work_log(), _scheduled_action("--after", "2h", "--reason",
                                                                                    "see whether 1.0 is out")]})
    cfg, state = _turns(project)
    group = state.group("w1", "g")
    assert group["status"] == "scheduled" and group["scheduled_reason"] == "see whether 1.0 is out"
    assert timedelta(hours=1, minutes=59) < _in(group) <= timedelta(hours=2)
    assert state.pending_groups("w1", 3) == []

    _due_now(state)
    assert state.pending_groups("w1", 3) == ["g"]
    project.queue(turn_answer([work_log("Checked.")]))
    cfg, state = _turns(project, "run2")
    prompt = project.calls()[1]["prompt"]
    assert "This is a scheduled turn: see whether 1.0 is out." in prompt and "## New artifacts\n\n- none" in prompt
    group = state.group("w1", "g")
    assert group["status"] == "finished" and group["scheduled_at"] is None
    assert state.row("SELECT scheduled_for FROM group_turns WHERE run_id = 'run2'")["scheduled_for"]


def test_a_standing_check_follows_every_finished_turn(project):
    cfg, state, _ = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    assert operations.set_schedule(cfg, state, "w1/g", recheck="1d") == ["standing check: every 1d"]
    assert state.group("w1", "g")["status"] == "idle" and state.group("w1", "g")["scheduled_at"] is None

    project.queue(turn_answer([work_log()]))
    cfg, state = _turns(project)
    group = state.group("w1", "g")
    assert group["status"] == "scheduled" and group["scheduled_reason"] == "the group's standing check every 1d"
    assert timedelta(hours=23) < _in(group) <= timedelta(days=1)
    assert "standing check every 1d" in (cfg.group_dir("w1", "g") / "CLAUDE.md").read_text()

    operations.set_schedule(cfg, state, "w1/g", recheck="off", next_turn="off")
    group = state.group("w1", "g")
    assert group["status"] == "finished" and group["scheduled_at"] is None and group["recheck_every"] is None


def test_the_person_can_schedule_and_cancel_a_turn(project):
    cfg, state, _ = project.load()
    state.add_group("w1", "g", "G")
    lines = operations.set_schedule(cfg, state, "w1/g", next_turn="3h", reason="check the release notes")
    assert lines[0].startswith("next scheduled turn:") and "check the release notes" in lines[0]
    group = state.group("w1", "g")
    assert group["status"] == "scheduled" and timedelta(hours=2) < _in(group) <= timedelta(hours=3)

    operations.set_schedule(cfg, state, "w1/g", next_turn="off")
    assert state.group("w1", "g")["status"] == "idle"  # it never had a turn

    state.set_status("w1", "g", "waiting", questions=["Which one?"])
    lines = operations.set_schedule(cfg, state, "w1/g", next_turn="1h")
    assert state.group("w1", "g")["status"] == "waiting" and "starts once it is done" in lines[1]
    with pytest.raises(cli.ConfigError):
        operations.set_schedule(cfg, state, "w1/g", next_turn="soon")


def test_finished_in_a_reply_keeps_the_scheduled_turn(project):
    cfg, state, _ = project.load()
    state.add_group("w1", "g", "G")
    (cfg.group_dir("w1", "g")).mkdir(parents=True)
    operations.set_schedule(cfg, state, "w1/g", next_turn="3h", reason="check")
    at = state.group("w1", "g")["scheduled_at"]
    state.prompt_submitted("w1", "g")  # a person wrote to the agent
    group_status.declare(cfg, state, cfg.watch("w1"), "g", "finished")
    group = state.group("w1", "g")
    assert (group["status"], group["scheduled_at"], group["scheduled_reason"]) == ("scheduled", at, "check")

    with pytest.raises(group_status.StatusError, match="--after"):
        group_status.declare(cfg, state, cfg.watch("w1"), "g", "scheduled")


def test_a_failed_scheduled_turn_is_tried_again(project):
    cfg, state, _ = project.load()
    state.add_group("w1", "g", "G")
    operations.set_schedule(cfg, state, "w1/g", next_turn="1h", reason="check")
    _due_now(state)
    for attempt in range(1, 4):
        project.queue({"actions": [work_log()]})  # never runs `vahtovik turn`
        cfg, state = _turns(project, f"run{attempt}")
        group = state.group("w1", "g")
        assert group["status"] == "failed" and group["scheduled_attempts"] == attempt and group["scheduled_at"]
    assert state.pending_groups("w1", 3) == []
    _turns(project, "run4")
    assert len(project.calls()) == 3
