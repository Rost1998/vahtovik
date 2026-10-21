import logging

import pytest
from conftest import artifact, status_action, until, work_log

from vahtovik import artifacts, cli, group_status, watchdog
from vahtovik.stages import group_turn
from vahtovik.tmux import Tmux

BUILD = {"id": "b1", "type": "shell", "status": "running", "description": "make build"}


def _setup(project):
    cfg, state, claude = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    return cfg, state, claude, Tmux(cfg)


def _turns(project):
    cfg, state, claude = project.load()
    group_turn.run(cfg, state, claude, Tmux(cfg), cfg.watch("w1"), "run1")
    return state


def background_action(reason: str = "the build runs", check_after: str | None = "30m") -> dict:
    return {"run": ["vahtovik", "turn", "background", "--reason", reason,
                    *(["--check-after", check_after] if check_after else [])]}


def _artifact_status(state) -> str:
    return state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"]


def test_agent_waits_for_its_background_work_then_ends_the_turn(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, _, _, tmux = _setup(project)
    project.queue({"mode": "interactive", "actions": [work_log(), background_action()], "background_tasks": [BUILD],
                   "wake": {"after": 1, "actions": [work_log("Built."), status_action()]}})
    state = _turns(project)
    group = state.group("w1", "g")
    assert (group["status"], group["status_reason"]) == ("background", "the build runs") and group["check_after"]
    assert any("Paused group turn" in m and "background work (the build runs)" in m for m in caplog.messages)
    assert state.row("SELECT finished_at FROM group_turns")["finished_at"] is None and _artifact_status(state) == "new"
    assert (state.run_summary("run1")["turns_paused"], state.run_summary("run1")["turns_failed"]) == (1, 0)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is not None  # the window keeps the background work alive

    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)
    assert state.row("SELECT ok FROM group_turns")["ok"] == 1 and _artifact_status(state) == "processed"
    assert state.group("w1", "g")["check_after"] is None and state.run_summary("run1")["turns_ok"] == 1
    until(lambda: len(project.hook_answers()) == 2)  # the fake logs a hook's answer after the hook has ended
    assert [a.get("decision") for a in project.hook_answers()] == [None, None]
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is None


def test_finished_while_background_work_runs_counts_as_background(project):
    _setup(project)
    project.queue({"mode": "interactive", "actions": [work_log(), status_action()],
                   "background_tasks": [BUILD, {**BUILD, "id": "b0", "status": "completed"}],
                   "session_crons": [{"id": "c1"}],
                   "wake": {"after": 1, "actions": [status_action(summary="Built and reported.")]}})
    state = _turns(project)
    group = state.group("w1", "g")
    assert group["status"] == "background"
    assert group["status_reason"] == ('Claude Code still runs background work for the agent: shell "make build"; '
                                      "1 scheduled prompt")
    journal = (project.root / "data/journal.log").read_text()  # the hook runs in a process of its own
    assert "w1/g: Status background, not finished: " + group["status_reason"] in journal
    turn = state.row("SELECT * FROM group_turns")
    assert (turn["finished_at"], turn["ok"]) == (None, None) and _artifact_status(state) == "new"

    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)
    assert state.row("SELECT ok FROM group_turns")["ok"] == 1 and _artifact_status(state) == "processed"


def test_background_with_nothing_running_is_sent_back_to_work(project):
    _setup(project)
    project.queue({"mode": "interactive", "actions": [work_log(), background_action()], "on_block": [status_action()]})
    state = _turns(project)
    first = project.hook_answers()[0]
    assert first["decision"] == "block" and "nothing will wake you" in first["reason"]
    assert state.group("w1", "g")["status"] == "finished" and state.row("SELECT ok FROM group_turns")["ok"] == 1


def test_background_that_never_ends_needs_a_person(project):
    cfg, state, _, _ = _setup(project)
    group_status.declare(cfg, state, cfg.watch("w1"), "g", "background", reason="waiting for a build")
    for _ in range(group_status.MAX_STOP_BLOCKS):
        assert group_status.on_stop(state, "w1", "g", {"background_tasks": []})["decision"] == "block"
    assert group_status.on_stop(state, "w1", "g", {"background_tasks": []}) is None
    assert state.group("w1", "g")["status"] == "maintenance"


def test_older_claude_code_that_does_not_tell_is_trusted(project):
    cfg, state, _, _ = _setup(project)
    group_status.declare(cfg, state, cfg.watch("w1"), "g", "background", reason="waiting for a build")
    assert group_status.on_stop(state, "w1", "g", {}) is None
    assert state.group("w1", "g")["status"] == "background"
    group_status.declare(cfg, state, cfg.watch("w1"), "g", "finished")
    assert group_status.on_stop(state, "w1", "g", {}) is None
    assert state.group("w1", "g")["status"] == "finished"


def test_background_work_from_the_stop_hook():
    assert group_status.background_work({}) is None
    assert group_status.background_work({"background_tasks": [], "session_crons": []}) == []
    tasks = [BUILD, {"type": "agent", "status": "pending", "description": "research"},
             {"type": "shell", "status": "killed", "command": "sleep 9"}]
    assert group_status.background_work({"background_tasks": tasks}) == ['shell "make build"', 'agent "research"']


@pytest.mark.parametrize("argv, error", [
    (["turn", "background"], "background needs --reason"),
    (["turn", "background", "--reason", "x", "--question", "Q?"], "takes no questions"),
    (["turn", "background", "--reason", "x", "--check-after", "soon"], "--check-after: bad interval"),
])
def test_background_command_explains_what_to_fix(project, monkeypatch, capsys, argv, error):
    _setup(project)
    monkeypatch.setenv("VAHTOVIK_WATCH", "w1")
    monkeypatch.setenv("VAHTOVIK_GROUP", "g")
    assert cli.main(argv) == 2
    assert error in capsys.readouterr().err
