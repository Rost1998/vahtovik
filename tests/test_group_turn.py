import shutil

from conftest import artifact, monitor_answer, report_action, turn_answer, work_log

from vahtovik import artifacts, groups
from vahtovik.stages import group_turn


def _monitor(project, *items):
    cfg, state, _ = project.load()
    watch = cfg.watch("w1")
    for item in items:
        artifacts.register(cfg, state, watch, item, "src-a")
    return cfg, state


def _turns(project):
    cfg, state, claude = project.load()
    group_turn.run(cfg, state, claude, cfg.watch("w1"), "run1", project.tmp / "raw")
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
    assert turn["ok"] == 1 and '"delivery_id": 1' in turn["output"]


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


def test_turn_without_work_log_is_retried_then_given_up(project):
    _monitor(project, artifact("a1", "g"))
    for attempt in range(1, 4):
        project.queue(turn_answer([]))
        _cfg, state = _turns(project)
        row = state.row("SELECT status, attempts FROM artifacts WHERE id = 'a1'")
        assert row["attempts"] == attempt
    assert row["status"] == "failed"
    _turns(project)
    assert len(project.calls()) == 3  # no fourth attempt
    assert "Work log" in state.row("SELECT error FROM group_turns ORDER BY started_at DESC LIMIT 1")["error"]


def test_agent_added_artifacts_are_registered_after_the_turn(project):
    _monitor(project, artifact("a1", "g"))
    found = {"id": "extra", "title": "Found by agent", "summary": "Extra."}
    elsewhere = {"id": "far", "title": "Belongs elsewhere", "summary": "Other.", "group_key": "h", "group_title": "H"}
    project.queue(turn_answer([work_log(), {"write": "artifacts/extra.json", "json": found},
                               {"write": "artifacts/far.json", "json": elsewhere}]))
    cfg, state = _turns(project)
    assert state.row("SELECT * FROM artifacts WHERE id = 'extra'")["status"] == "processed"
    far = state.row("SELECT * FROM artifacts WHERE id = 'far'")
    assert far["group_key"] == "h" and far["status"] == "new"
    assert not (cfg.group_dir("w1", "g") / "artifacts/far.json").exists()
    assert (cfg.group_dir("w1", "h") / "context.md").is_file()
