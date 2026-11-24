import logging
from datetime import timedelta

import pytest
from conftest import artifact, give, launch_turn, run_turns, status_action, turn_answer, until, work_log

from vahtovik import watchdog, workers
from vahtovik.claude_session import Claude
from vahtovik.config import ConfigError
from vahtovik.tmux import Tmux, Window


def _setup(project, *ids):
    cfg, state = give(project, *(artifact(i, "g") for i in ids or ("a1",)))
    cfg, state, claude = project.load()
    return cfg, state, claude, Tmux(cfg), state.active_worker("r1", "g")


def _turns(project):
    cfg, state, claude = project.load()
    run_turns(cfg, state, claude, Tmux(cfg))
    return cfg, state


def test_turn_runs_in_a_window_that_closes_after_the_watchdog_checks_it(project):
    cfg, state, claude, tmux, row = _setup(project)
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert state.row("SELECT ok FROM turns")["ok"] == 1
    assert tmux.window(row["id"]) is not None  # done, but not checked yet
    names = tmux._run("list-windows", "-t", f"={tmux.session}", "-F", "#{window_name}").stdout.split()
    assert names == ["journal", "r1/g"]
    watchdog.tick(cfg, state, tmux, claude, "run2")
    assert tmux.window(row["id"]) is None and not workers.is_busy(cfg, row)
    assert [w.id for w in tmux.windows()] == []
    assert state.window_record(row["id"])["purpose"] == "turn"
    call = project.calls()[0]
    assert "-p" not in call["args"] and call["prompt"].startswith("# Turn")


def test_window_holds_the_worker_lock_and_runs_skip_the_worker(project, caplog):
    cfg, state, claude, tmux, row = _setup(project)
    project.queue({"hang": True})
    window_id = launch_turn(cfg, state, claude, tmux)
    until(lambda: workers.is_busy(cfg, row) and len(project.calls()) == 1)
    _turns(project)
    assert len(project.calls()) == 1  # the run did not start a second session
    assert f"r1/{row['id']}-g: Skipped turn: its session window is open; a later run will start it" in caplog.messages
    tmux.close(window_id)
    until(lambda: not workers.is_busy(cfg, row))


def test_claude_exiting_fails_the_turn_with_its_last_screen_lines(project):
    _cfg, state, _, _tmux, row = _setup(project)
    project.queue({"exit": 3, "stderr": "Boom happened"})
    _turns(project)
    turn = state.row("SELECT ok, error FROM turns")
    assert turn["ok"] == 0 and turn["error"].startswith("claude exited with code 3: ")
    assert "Boom happened" in turn["error"]
    assert state.worker(row["id"])["status"] == "failed"
    assert state.row("SELECT attempts FROM artifacts WHERE id = 'a1'")["attempts"] == 1


def test_window_someone_watches_stays_open_until_they_leave(project, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, _, tmux, row = _setup(project)
    real = Tmux.windows
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window(w.id, w.worker, 1) for w in real(self)])
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert tmux.window(row["id"]) is not None and state.row("SELECT ok FROM turns")["ok"] == 1
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is not None
    monkeypatch.setattr(Tmux, "windows", real)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is None
    assert f"r1/{row['id']}-g: Closed the session window: status finished, checked by the watchdog" in caplog.messages


def test_tick_fails_turns_whose_window_is_gone(project):
    cfg, state, _, tmux, row = _setup(project)
    state.start_turn("t1", "run1", row["id"], ["a1"], [], 0)
    state.prompt_submitted(row["id"])
    watchdog.tick(cfg, state, tmux)
    turn = state.turn("t1")
    assert turn["ok"] == 0 and turn["error"] == "the session window closed before the agent ran `vahtovik turn`"
    assert state.row("SELECT attempts FROM artifacts WHERE id = 'a1'")["attempts"] == 1


def test_open_starts_a_window_and_the_person_answers_the_agent(project, monkeypatch):
    cfg, state, _, tmux, row = _setup(project)
    project.queue(turn_answer([work_log()], status="waiting", questions=("Upgrade now?",)))
    _turns(project)
    watchdog.check(cfg, state, tmux, Claude(cfg), "run1")  # the next run's start closes the done window
    shown = []
    monkeypatch.setattr(Tmux, "attach", lambda self, window_id: shown.append(window_id) or True)
    workers.open_session(cfg, state, "r1/g")
    window = tmux.window(row["id"])
    assert shown == [window.id] and state.window_record(row["id"])["purpose"] == "open"
    until(lambda: len(project.calls()) == 2)
    assert "--resume" in project.calls()[1]["args"] and project.calls()[1]["prompt"] == ""

    project.queue({"actions": [work_log("Upgraded."), status_action("finished", None, None)]})
    until(lambda: "\n>" in tmux.capture(window.id))  # the session waits for the person
    tmux.send_keys(window.id, ["Yes, upgrade now.", "Enter"])
    until(lambda: state.worker(row["id"])["status"] == "finished" and state.worker(row["id"])["idle"] == 1)
    assert "- Upgrade now?" not in workers.context_md(cfg, row).read_text()

    workers.open_session(cfg, state, row["id"])  # a second open shows the same window
    assert shown == [window.id, window.id] and len(project.calls()) == 2
    with pytest.raises(ConfigError, match=r"close that window first \(tmux -L vahtovik-test-\S+ kill-window -t @"):
        workers.open_session(cfg, state, f"{row['id']}-g", new_session=True)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is None


def test_only_a_window_just_started_by_open_is_left_alone(project, monkeypatch):
    monkeypatch.setattr(watchdog, "GRACE", timedelta(seconds=10))
    cfg, state, claude, tmux, row = _setup(project)
    project.queue(turn_answer([work_log()]))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: state.worker(row["id"])["status"] == "finished" and state.worker(row["id"])["idle"] == 1)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is None  # a run's window closes as soon as its agent is done

    until(lambda: not workers.is_busy(cfg, row))
    monkeypatch.setattr(Tmux, "attach", lambda self, window_id: True)
    workers.open_session(cfg, state, "r1/g")
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is not None  # the person may not have attached yet
    monkeypatch.setattr(watchdog, "GRACE", timedelta(0))
    watchdog.tick(cfg, state, tmux)
    assert tmux.window(row["id"]) is None


def test_a_busy_worker_fails_the_turn_without_running_claude(project):
    cfg, state, claude, tmux, row = _setup(project)
    with workers.lock(workers.folder(cfg, row)):
        launch_turn(cfg, state, claude, tmux)
        until(lambda: tmux.window(row["id"]) is None)
    watchdog.tick(cfg, state, tmux)
    assert state.turn("t1")["error"] == "another process was using the worker"
    assert project.calls() == []


def test_session_window_has_no_secret_file_left(project):
    cfg, state, _, tmux, _row = _setup(project)
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert list((cfg.data_dir / "tmp").iterdir()) == []
