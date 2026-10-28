import json
import logging
import shutil

import yaml
from conftest import artifact, report_action, run_turns, turn_answer, until, work_log

from vahtovik import artifacts, groups, watchdog
from vahtovik.stages import group_turn
from vahtovik.tmux import Tmux


def _monitor(project, *items):
    cfg, state, _ = project.load()
    watch = cfg.watch("w1")
    for item in items:
        artifacts.register(cfg, state, watch, item, "src-a")
    return cfg, state


def _turns(project):
    """A run's group stage, after the watchdog's check that starts every run (it closes done windows)."""
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, "run1")
    run_turns(cfg, state, claude, Tmux(cfg))
    return cfg, state


def test_first_turn_creates_the_session_and_reports(project):
    _monitor(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log(), report_action("log", "Hello from the agent.")]))
    cfg, state = _turns(project)

    call = project.calls()[0]
    group = state.group("w1", "g")
    assert call["cwd"] == str(cfg.group_dir("w1", "g"))
    assert call["args"][call["args"].index("--session-id") + 1] == group["session_id"]
    assert call["env"]["VAHTOVIK_GROUP"] == "g" and call["env"]["VAHTOVIK_TURN"]
    assert "Assess each item." in call["prompt"] and "Report everything to log." in call["prompt"]
    assert state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"] == "processed"
    delivery = state.row("SELECT * FROM deliveries")
    assert delivery["status"] == "sent" and delivery["origin"] == "automated"
    assert "Hello from the agent." in (cfg.root / "data/outbox.md").read_text()
    assert "· log · sent" in (cfg.group_dir("w1", "g") / "context.md").read_text()
    turn = state.row("SELECT * FROM group_turns")
    assert turn["ok"] == 1 and json.loads(turn["output"])["status"] == "finished"
    assert group["status"] == "finished"
    assert "--settings" in call["args"] and "hook stop" in call["args"][call["args"].index("--settings") + 1]


def test_next_turn_resumes_the_same_session(project):
    _monitor(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    _turns(project)
    _monitor(project, artifact("a2", "g"))
    project.queue(turn_answer([work_log("Second.")]))
    _cfg, state = _turns(project)
    second = project.calls()[1]
    assert second["args"][second["args"].index("--resume") + 1] == state.group("w1", "g")["session_id"]
    assert "--append-system-prompt" not in second["args"]
    assert "a2" in second["prompt"] and "a1" not in second["prompt"].split("## New artifacts")[1].split("##")[0]


def test_lost_session_is_replaced(project):
    _monitor(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    _cfg, state = _turns(project)
    old = state.group("w1", "g")["session_id"]
    shutil.rmtree(project.tmp / "claude-home/projects")
    _monitor(project, artifact("a2", "g"))
    project.queue(turn_answer([work_log()]))
    _cfg, state = _turns(project)
    call = project.calls()[1]
    assert "--session-id" in call["args"] and "new session for an existing group" in call["prompt"]
    assert state.group("w1", "g")["session_id"] not in (None, old)


def test_locked_group_is_skipped(project):
    cfg, state = _monitor(project, artifact("a1", "g"))
    with groups.lock(cfg.group_dir("w1", "g")):
        _turns(project)
    assert project.calls() == []
    assert state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"] == "new"


def test_turn_without_status_is_retried_then_given_up(project):
    _monitor(project, artifact("a1", "g"))
    for attempt in range(1, 4):
        project.queue({"actions": [work_log()]})
        _cfg, state = _turns(project)
        row = state.row("SELECT status, attempts FROM artifacts WHERE id = 'a1'")
        assert row["attempts"] == attempt
    assert row["status"] == "failed"
    _turns(project)
    assert len(project.calls()) == 3  # no fourth attempt
    error = state.row("SELECT error FROM group_turns ORDER BY started_at DESC LIMIT 1")["error"]
    assert error == "the agent stopped 4 times without running `vahtovik turn`"
    assert state.group("w1", "g")["status"] == "failed"


def test_run_starts_the_turn_and_does_not_wait_for_it(project):
    cfg, state = _monitor(project, artifact("a1", "g"))
    project.queue({"mode": "interactive", "hang": True})
    _cfg, _state, claude = project.load()
    turn_id, = group_turn.run(cfg, state, claude, Tmux(cfg), cfg.watch("w1"), "run1")
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session works on, after the run went on
    assert state.turn(turn_id)["finished_at"] is None and state.group("w1", "g")["status"] == "running"


def test_agent_added_artifacts_are_registered_when_the_window_closes(project):
    _monitor(project, artifact("a1", "g"))
    found = {"id": "extra", "title": "Found by agent", "summary": "Extra."}
    elsewhere = {"id": "far", "title": "Belongs elsewhere", "summary": "Other.", "group_key": "h", "group_title": "H"}
    project.queue(turn_answer([work_log(), {"write": "artifacts/extra.json", "json": found},
                               {"write": "artifacts/far.json", "json": elsewhere}]))
    cfg, state = _turns(project)
    assert state.row("SELECT * FROM artifacts WHERE id = 'extra'") is None  # the window is open until a check
    watchdog.tick(cfg, state, Tmux(cfg), project.load()[2], "run2")
    assert state.row("SELECT * FROM artifacts WHERE id = 'extra'")["status"] == "processed"
    far = state.row("SELECT * FROM artifacts WHERE id = 'far'")
    assert far["group_key"] == "h" and far["status"] == "new"
    assert not (cfg.group_dir("w1", "g") / "artifacts/far.json").exists()
    assert (cfg.group_dir("w1", "h") / "context.md").is_file()


def test_agent_added_artifacts_are_registered_when_the_window_ends_by_itself(project):
    _monitor(project, artifact("a1", "g"))
    found = {"id": "extra", "title": "Found by agent", "summary": "Extra."}
    project.queue({"actions": [{"write": "artifacts/extra.json", "json": found}], "exit": 3})
    _cfg, state = _turns(project)
    assert state.row("SELECT ok FROM group_turns")["ok"] == 0
    assert state.row("SELECT * FROM artifacts WHERE id = 'extra'")["group_key"] == "g"


def test_parallel_turns_are_limited_and_the_rest_start_at_a_later_run(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"]["parallel_turns"] = 2
    path.write_text(yaml.safe_dump(data))
    cfg, state = _monitor(project, artifact("a1", "g1"), artifact("a2", "g2"), artifact("a3", "g3"))
    project.queue(*[turn_answer([work_log()]) for _ in range(3)])
    _cfg, _state, claude = project.load()
    assert len(group_turn.run(cfg, state, claude, Tmux(cfg), cfg.watch("w1"), "run1")) == 2
    assert ("w1: Held back 1 group turn for a later run: already as many work as claude.parallel_turns (2) "
            "allows") in caplog.messages
    until(lambda: state.working_turns() == 0)
    left, = state.pending_groups("w1", group_turn.MAX_ATTEMPTS)
    assert len(group_turn.run(cfg, state, claude, Tmux(cfg), cfg.watch("w1"), "run2")) == 1
    until(lambda: state.group("w1", left)["status"] == "finished")
