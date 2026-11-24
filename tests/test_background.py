import pytest
from conftest import artifact, give, run_turns, status_action, until, work_log

from vahtovik import cli, watchdog, worker_status
from vahtovik.tmux import Tmux

BUILD = {"id": "b1", "type": "shell", "status": "running", "description": "make build"}


def _setup(project):
    cfg, state = give(project, artifact("a1", "g"))
    return cfg, state, Tmux(cfg), state.active_worker("r1", "g")


def _turns(project):
    cfg, state, claude = project.load()
    run_turns(cfg, state, claude, Tmux(cfg))
    return state


def background_action(reason: str = "the build runs", check_after: str | None = "30m") -> dict:
    return {"run": ["vahtovik", "turn", "background", "--reason", reason,
                    *(["--check-after", check_after] if check_after else [])]}


def _artifact_status(state) -> str:
    return state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"]


def test_agent_waits_for_its_background_work_then_ends_the_turn(project):
    cfg, _, tmux, row = _setup(project)
    project.queue({"mode": "interactive", "actions": [work_log(), background_action()], "background_tasks": [BUILD],
                   "wake": {"after": 1, "actions": [work_log("Built."), status_action()]}})
    state = _turns(project)
    current = state.worker(row["id"])
    assert (current["status"], current["status_reason"]) == ("background", "the build runs") and current["check_after"]
    assert state.row("SELECT finished_at FROM turns")["finished_at"] is None and _artifact_status(state) == "new"
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is not None  # the window keeps the background work alive

    until(lambda: state.worker(row["id"])["status"] == "finished" and state.worker(row["id"])["idle"] == 1)
    assert state.row("SELECT ok FROM turns")["ok"] == 1 and _artifact_status(state) == "processed"
    assert state.worker(row["id"])["check_after"] is None
    until(lambda: len(project.hook_answers()) == 2)  # the fake logs a hook's answer after the hook has ended
    assert [a.get("decision") for a in project.hook_answers()] == [None, None]
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is None


def test_finished_while_background_work_runs_counts_as_background(project):
    _cfg, _state, _tmux, row = _setup(project)
    project.queue({"mode": "interactive", "actions": [work_log(), status_action()],
                   "background_tasks": [BUILD, {**BUILD, "id": "b0", "status": "completed"}],
                   "session_crons": [{"id": "c1"}],
                   "wake": {"after": 1, "actions": [status_action(summary="Built and reported.")]}})
    state = _turns(project)
    current = state.worker(row["id"])
    assert current["status"] == "background"
    assert current["status_reason"] == ('Claude Code still runs background work for the agent: shell "make build"; '
                                        "1 scheduled prompt")
    journal = (project.root / "data/journal.log").read_text()  # the hook runs in a process of its own
    assert f"r1/{row['id']}-g: Status background, not finished: " + current["status_reason"] in journal
    turn = state.row("SELECT * FROM turns")
    assert (turn["finished_at"], turn["ok"]) == (None, None) and _artifact_status(state) == "new"

    until(lambda: state.worker(row["id"])["status"] == "finished" and state.worker(row["id"])["idle"] == 1)
    assert state.row("SELECT ok FROM turns")["ok"] == 1 and _artifact_status(state) == "processed"


def test_background_with_nothing_running_is_sent_back_to_work(project):
    _cfg, _state, _tmux, row = _setup(project)
    project.queue({"mode": "interactive", "actions": [work_log(), background_action()], "on_block": [status_action()]})
    state = _turns(project)
    first = project.hook_answers()[0]
    assert first["decision"] == "block" and "nothing will wake you" in first["reason"]
    assert state.worker(row["id"])["status"] == "finished" and state.row("SELECT ok FROM turns")["ok"] == 1


def test_background_that_never_ends_needs_a_person(project):
    cfg, state, tmux, row = _setup(project)
    worker_status.declare(cfg, state, row, "background", reason="waiting for a build")
    for _ in range(worker_status.MAX_STOP_BLOCKS):
        assert worker_status.on_stop(state, row["id"], {"background_tasks": []})["decision"] == "block"
    assert worker_status.on_stop(state, row["id"], {"background_tasks": []}) is None
    assert state.worker(row["id"])["status"] == "maintenance"
    watchdog.notify(cfg, state, tmux)  # the hook sends no notice itself: the watchdog's next tick does
    notice, = state.rows("SELECT * FROM deliveries WHERE origin = 'notice'")
    assert notice["subject"] == "WATCHDOG: Needs attention: G"


def test_older_claude_code_that_does_not_tell_is_trusted(project):
    cfg, state, _tmux, row = _setup(project)
    worker_status.declare(cfg, state, row, "background", reason="waiting for a build")
    assert worker_status.on_stop(state, row["id"], {}) is None
    assert state.worker(row["id"])["status"] == "background"
    worker_status.declare(cfg, state, state.worker(row["id"]), "finished")
    assert worker_status.on_stop(state, row["id"], {}) is None
    assert state.worker(row["id"])["status"] == "finished"


def test_background_work_from_the_stop_hook():
    assert worker_status.background_work({}) is None
    assert worker_status.background_work({"background_tasks": [], "session_crons": []}) == []
    tasks = [BUILD, {"type": "agent", "status": "pending", "description": "research"},
             {"type": "shell", "status": "killed", "command": "sleep 9"}]
    assert worker_status.background_work({"background_tasks": tasks}) == ['shell "make build"', 'agent "research"']


@pytest.mark.parametrize("argv, error", [
    (["turn", "background"], "background needs --reason"),
    (["turn", "background", "--reason", "x", "--question", "Q?"], "takes no questions"),
    (["turn", "background", "--reason", "x", "--check-after", "soon"], "--check-after: bad interval"),
    (["turn", "scheduled", "--at", "25:00"], "--at: bad time"),
    (["turn", "finished", "--after", "1h"], "--after and --at are for `vahtovik turn scheduled`"),
])
def test_turn_command_explains_what_to_fix(project, monkeypatch, capsys, argv, error):
    _cfg, _state, _tmux, row = _setup(project)
    monkeypatch.setenv("VAHTOVIK_WORKER", row["id"])
    assert cli.main(argv) == 2
    assert error in capsys.readouterr().err
