import json
import sqlite3

import pytest
from conftest import artifact, status_action, turn_answer, work_log

from vahtovik import artifacts, cli, context_file, group_status, watchdog
from vahtovik.state import State
from vahtovik.stages import group_turn
from vahtovik.tmux import Tmux


def _setup(project, *ids):
    cfg, state, claude = project.load()
    for item_id in ids or ("a1",):
        artifacts.register(cfg, state, cfg.watch("w1"), artifact(item_id, "g"), "src-a")
    return cfg, state, claude


def _turns(project):
    """A run's group stage, after the watchdog's check that starts every run (it closes done windows)."""
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, "run1")
    group_turn.run(cfg, state, claude, Tmux(cfg), cfg.watch("w1"), "run1")
    return cfg, state


def _context(cfg) -> str:
    return (cfg.group_dir("w1", "g") / "context.md").read_text()


def _open_questions(cfg) -> str:
    return _context(cfg).split("## Open questions\n")[1].split("\n## ")[0].strip()


def test_waiting_turn_records_the_questions(project):
    _setup(project)
    project.queue(turn_answer([work_log()], importance="high", status="waiting",
                              questions=("Which version do we use?", "Is the\nbudget  approved?")))
    cfg, state = _turns(project)
    group = state.group("w1", "g")
    assert group["status"] == "waiting"
    assert json.loads(group["questions"]) == ["Which version do we use?", "Is the\nbudget  approved?"]
    assert _open_questions(cfg) == "- Which version do we use?\n- Is the budget approved?"
    assert state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"] == "processed"
    assert json.loads(state.row("SELECT output FROM group_turns")["output"])["importance"] == "high"


def test_finished_clears_the_questions(project):
    _setup(project)
    project.queue(turn_answer([work_log()], status="waiting", questions=("Why?",)))
    cfg, _ = _turns(project)
    _setup(project, "a2")
    project.queue(turn_answer([work_log("Answered.")]))
    cfg, state = _turns(project)
    assert state.group("w1", "g")["status"] == "finished"
    assert _open_questions(cfg) == "_None._"


def test_stop_hook_blocks_until_the_agent_reports_its_status(project):
    _setup(project)
    project.queue({"actions": [work_log()], "on_block": [status_action()]})
    _cfg, state = _turns(project)
    first = project.hook_answers()[0]
    assert first["decision"] == "block" and "vahtovik turn finished" in first["reason"]
    assert state.row("SELECT ok FROM group_turns")["ok"] == 1
    group = state.group("w1", "g")
    assert (group["status"], group["idle"], group["stop_blocks"]) == ("finished", 1, 1)


def test_prompt_hook_marks_the_group_running_and_keeps_the_session_id(project):
    cfg, state, _ = _setup(project)
    group_status.on_prompt(state, "w1", "g", "session-1")
    group = state.group("w1", "g")
    assert (group["status"], group["idle"], group["session_id"]) == ("running", 0, "session-1")
    assert group_status.on_stop(state, "w1", "g")["decision"] == "block"
    group_status.declare(cfg, state, cfg.watch("w1"), "g", "finished")
    assert group_status.on_stop(state, "w1", "g") is None
    assert state.group("w1", "g")["idle"] == 1


@pytest.mark.parametrize("kwargs, error", [
    ({"status": "waiting"}, "at least one --question"),
    ({"status": "finished", "questions": ["Why?"]}, "takes no questions"),
    ({"status": "finished", "summary": "S."}, "give --importance too"),
    ({"status": "finished", "importance": "low", "summary": "S."}, "Work log entry"),
])
def test_declare_explains_what_to_fix(project, kwargs, error):
    cfg, state, _ = _setup(project)
    context = cfg.group_dir("w1", "g") / "context.md"
    state.start_turn("t1", "run1", "w1", "g", ["a1"], context_file.fingerprint(context).work_log_entries)
    with pytest.raises(group_status.StatusError, match=error):
        group_status.declare(cfg, state, cfg.watch("w1"), "g", turn_id="t1", **kwargs)
    assert state.turn("t1")["finished_at"] is None


def test_interactive_status_needs_no_importance_or_work_log(project, monkeypatch, capsys):
    cfg, state, _ = _setup(project)
    monkeypatch.setenv("VAHTOVIK_WATCH", "w1")
    monkeypatch.setenv("VAHTOVIK_GROUP", "g")
    assert cli.main(["turn", "waiting", "--question", "Shall I go on?"]) == 0
    assert "recorded: status waiting (1 question)" in capsys.readouterr().out
    assert state.group("w1", "g")["status"] == "waiting"
    assert cli.main(["turn", "waiting"]) == 2
    assert "at least one --question" in capsys.readouterr().err
    assert "w1/g: Status waiting (1 question)" in (cfg.root / "data/journal.log").read_text()


def test_turn_command_reports_errors_to_the_agent(project):
    _setup(project)
    project.queue({"actions": [status_action(), work_log(), status_action()]})
    _cfg, state = _turns(project)
    first, second = project.command_runs()
    assert first["code"] == 2 and "Work log entry" in first["out"]
    assert second["code"] == 0 and state.row("SELECT ok FROM group_turns")["ok"] == 1


def test_open_questions_section_is_added_to_older_context_files(tmp_path):
    path = tmp_path / "context.md"
    path.write_text("# T\n\n## Summary\nS.\n\n## Artifacts\n| a |\n\n## Reports\n")
    context_file.replace_section(path, "Open questions", "- Q?", after="Summary")
    assert path.read_text() == "# T\n\n## Summary\nS.\n\n## Open questions\n- Q?\n\n## Artifacts\n| a |\n\n## Reports\n"
    context_file.replace_section(path, "Open questions", "_None._", after="Summary")
    assert "## Open questions\n_None._\n\n## Artifacts" in path.read_text()


def test_older_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE groups (watch TEXT, key TEXT, title TEXT, created_at TEXT, updated_at TEXT, "
               "session_id TEXT, session_created_at TEXT, PRIMARY KEY (watch, key))")
    db.execute("INSERT INTO groups (watch, key, title) VALUES ('w', 'k', 'T')")
    db.commit()
    db.close()
    state = State(path)
    group = state.group("w", "k")
    assert (group["status"], group["idle"], group["stop_blocks"]) == ("idle", 1, 0)
