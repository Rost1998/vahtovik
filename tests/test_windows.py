import logging
from datetime import timedelta

import pytest
from conftest import artifact, launch_turn, status_action, turn_answer, until, work_log

from vahtovik import artifacts, group_session, groups, watchdog, windows
from vahtovik.config import ConfigError
from vahtovik.stages import group_turn
from vahtovik.tmux import Tmux, Window
from vahtovik.util import LockBusy


def _setup(project, *ids):
    cfg, state, claude = project.load()
    for item_id in ids or ("a1",):
        artifacts.register(cfg, state, cfg.watch("w1"), artifact(item_id, "g"), "src-a")
    return cfg, state, claude, Tmux(cfg)


def _turns(project):
    cfg, state, claude = project.load()
    group_turn.run(cfg, state, claude, Tmux(cfg), cfg.watch("w1"), "run1")
    return cfg, state


def _locked(cfg) -> bool:
    try:
        with groups.lock(cfg.group_dir("w1", "g")):
            return False
    except LockBusy:
        return True


def test_turn_runs_in_a_window_that_closes_when_the_agent_is_done(project):
    cfg, state, _, tmux = _setup(project)
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert state.row("SELECT ok FROM group_turns")["ok"] == 1
    assert tmux.window("w1/g") is None and not _locked(cfg)
    assert [w.id for w in tmux.windows()] == []
    assert state.window_record("w1", "g")["purpose"] == "turn"
    assert tmux._run("list-windows", "-t", f"={tmux.session}", "-F", "#{window_name}").stdout.split() == ["journal"]
    call = project.calls()[0]
    assert "-p" not in call["args"] and call["prompt"].startswith("# Processing turn")


def test_window_holds_the_group_lock_and_runs_skip_the_group(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"hang": True})
    window_id = launch_turn(cfg, state, claude, tmux)
    until(lambda: _locked(cfg) and len(project.calls()) == 1)
    _turns(project)
    assert len(project.calls()) == 1  # the run did not start a second session
    assert "w1/g: Skipped group turn: its session window is open; a later run will process it" in caplog.messages
    tmux.close(window_id)
    until(lambda: not _locked(cfg))


def test_claude_exiting_fails_the_turn_with_its_last_screen_lines(project):
    cfg, state, _, tmux = _setup(project)
    project.queue({"exit": 3, "stderr": "Boom happened"})
    _turns(project)
    turn = state.row("SELECT ok, error FROM group_turns")
    assert turn["ok"] == 0 and turn["error"].startswith("claude exited with code 3: ")
    assert "Boom happened" in turn["error"]
    assert state.group("w1", "g")["status"] == "failed"
    assert state.row("SELECT attempts FROM artifacts WHERE id = 'a1'")["attempts"] == 1


def test_window_someone_watches_stays_open_until_they_leave(project, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, _, tmux = _setup(project)
    real = Tmux.windows
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window(w.id, w.ref, 1) for w in real(self)])
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert tmux.window("w1/g") is not None and state.row("SELECT ok FROM group_turns")["ok"] == 1
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is not None
    monkeypatch.setattr(Tmux, "windows", real)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is None
    assert "w1/g: Closed the session window: status finished" in caplog.messages


def test_tick_fails_turns_whose_window_is_gone(project):
    cfg, state, _, tmux = _setup(project)
    state.start_turn("t1", "run1", "w1", "g", ["a1"], 0)
    state.prompt_submitted("w1", "g")
    watchdog.tick(cfg, state, tmux)
    turn = state.turn("t1")
    assert turn["ok"] == 0 and turn["error"] == "the session window closed before the agent ran `vahtovik turn`"
    assert state.row("SELECT attempts FROM artifacts WHERE id = 'a1'")["attempts"] == 1


def test_open_starts_a_window_and_the_person_answers_the_agent(project, monkeypatch):
    cfg, state, _, tmux = _setup(project)
    project.queue(turn_answer([work_log()], status="waiting", questions=("Upgrade now?",)))
    _turns(project)
    shown = []
    monkeypatch.setattr(Tmux, "attach", lambda self, window_id: shown.append(window_id) or True)
    group_session.open_session(cfg, state, "w1/g")
    window = tmux.window("w1/g")
    assert shown == [window.id] and state.window_record("w1", "g")["purpose"] == "open"
    until(lambda: len(project.calls()) == 2)
    assert "--resume" in project.calls()[1]["args"] and project.calls()[1]["prompt"] == ""

    project.queue({"actions": [work_log("Upgraded."), status_action("finished", None, None)]})
    until(lambda: "\n>" in tmux.capture(window.id))  # the session waits for the person
    tmux.send_keys(window.id, ["Yes, upgrade now.", "Enter"])
    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)
    assert "- Upgrade now?" not in (cfg.group_dir("w1", "g") / "context.md").read_text()

    group_session.open_session(cfg, state, "w1/g")  # a second open shows the same window
    assert shown == [window.id, window.id] and len(project.calls()) == 2
    with pytest.raises(ConfigError, match=r"close that window first \(tmux -L vahtovik-test-\S+ kill-window -t @"):
        group_session.open_session(cfg, state, "w1/g", new_session=True)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is None


def test_only_a_window_just_started_by_open_is_left_alone(project, monkeypatch):
    monkeypatch.setattr(watchdog, "GRACE", timedelta(seconds=10))
    cfg, state, claude, tmux = _setup(project)
    project.queue(turn_answer([work_log()]))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is None  # a run's window closes as soon as its agent is done

    until(lambda: not _locked(cfg))
    monkeypatch.setattr(Tmux, "attach", lambda self, window_id: True)
    group_session.open_session(cfg, state, "w1/g")
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is not None  # the person may not have attached yet
    monkeypatch.setattr(watchdog, "GRACE", timedelta(0))
    watchdog.tick(cfg, state, tmux)
    assert tmux.window("w1/g") is None


def test_a_busy_group_fails_the_turn_without_running_claude(project):
    cfg, state, claude, tmux = _setup(project)
    with groups.lock(cfg.group_dir("w1", "g")):
        launch_turn(cfg, state, claude, tmux)
        until(lambda: tmux.window("w1/g") is None)
    watchdog.tick(cfg, state, tmux)
    assert state.turn("t1")["error"] == "another process was using the group"
    assert project.calls() == []


def test_session_window_has_no_secret_file_left(project):
    cfg, state, _, tmux = _setup(project)
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert list((cfg.data_dir / "tmp").iterdir()) == []
