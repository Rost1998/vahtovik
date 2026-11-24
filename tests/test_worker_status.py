import json
import sqlite3

import pytest
from conftest import artifact, give, run_turns, status_action, turn_answer, work_log

from vahtovik import cli, context_file, watchdog, worker_status, workers
from vahtovik.config import ConfigError
from vahtovik.state import State
from vahtovik.tmux import Tmux


def _setup(project, *ids):
    cfg, state = give(project, *(artifact(i, "g") for i in ids or ("a1",)))
    return cfg, state, state.active_worker("r1", "g")


def _turns(project):
    """A run's worker stage, after the watchdog's check that starts every run (it closes done windows)."""
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, "run1")
    run_turns(cfg, state, claude, Tmux(cfg))
    return cfg, state


def _context(cfg, state) -> str:
    return workers.context_md(cfg, state.active_worker("r1", "g")).read_text()


def _open_questions(cfg, state) -> str:
    return _context(cfg, state).split("## Open questions\n")[1].split("\n## ")[0].strip()


def test_waiting_turn_records_the_questions(project):
    _setup(project)
    project.queue(turn_answer([work_log()], importance="high", status="waiting",
                              questions=("Which version do we use?", "Is the\nbudget  approved?")))
    cfg, state = _turns(project)
    row = state.active_worker("r1", "g")
    assert row["status"] == "waiting"
    assert json.loads(row["questions"]) == ["Which version do we use?", "Is the\nbudget  approved?"]
    assert _open_questions(cfg, state) == "- Which version do we use?\n- Is the budget approved?"
    assert state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"] == "processed"
    assert json.loads(state.row("SELECT output FROM turns")["output"])["importance"] == "high"


def test_finished_clears_the_questions(project):
    _setup(project)
    project.queue(turn_answer([work_log()], status="waiting", questions=("Why?",)))
    _turns(project)
    _setup(project, "a2")
    project.queue(turn_answer([work_log("Answered.")]))
    cfg, state = _turns(project)
    assert state.active_worker("r1", "g")["status"] == "finished"
    assert _open_questions(cfg, state) == "_None._"


def test_stop_hook_blocks_until_the_agent_reports_its_status(project):
    _setup(project)
    project.queue({"actions": [work_log()], "on_block": [status_action()]})
    _cfg, state = _turns(project)
    first = project.hook_answers()[0]
    assert first["decision"] == "block" and "vahtovik turn finished" in first["reason"]
    assert state.row("SELECT ok FROM turns")["ok"] == 1
    row = state.active_worker("r1", "g")
    assert (row["status"], row["idle"], row["stop_blocks"]) == ("finished", 1, 1)


def test_prompt_hook_marks_the_worker_running_and_keeps_the_session_id(project):
    cfg, state, row = _setup(project)
    worker_status.on_prompt(state, row["id"], "session-1")
    row = state.worker(row["id"])
    assert (row["status"], row["idle"], row["session_id"]) == ("running", 0, "session-1")
    assert worker_status.on_stop(state, row["id"])["decision"] == "block"
    worker_status.declare(cfg, state, row, "finished")
    assert worker_status.on_stop(state, row["id"]) is None
    assert state.worker(row["id"])["idle"] == 1


@pytest.mark.parametrize("kwargs, error", [
    ({"status": "waiting"}, "at least one --question"),
    ({"status": "finished", "questions": ["Why?"]}, "takes no questions"),
    ({"status": "finished", "summary": "S."}, "give --importance too"),
    ({"status": "finished", "importance": "low", "summary": "S."}, "Work log entry"),
    ({"status": "scheduled", "importance": "low", "summary": "S."}, "no scheduled turn to keep"),
    ({"status": "finished", "after": object()}, "for `vahtovik turn scheduled`"),
])
def test_declare_explains_what_to_fix(project, kwargs, error):
    cfg, state, row = _setup(project)
    context = workers.context_md(cfg, row)
    state.start_turn("t1", "run1", row["id"], ["a1"], [], context_file.fingerprint(context).work_log_entries)
    with pytest.raises(worker_status.StatusError, match=error):
        worker_status.declare(cfg, state, row, turn_id="t1", **kwargs)
    assert state.turn("t1")["finished_at"] is None


def test_interactive_status_needs_no_importance_or_work_log(project, monkeypatch, capsys):
    cfg, state, row = _setup(project)
    monkeypatch.setenv("VAHTOVIK_WORKER", row["id"])
    assert cli.main(["turn", "waiting", "--question", "Shall I go on?"]) == 0
    assert "recorded: status waiting (1 question)" in capsys.readouterr().out
    assert state.worker(row["id"])["status"] == "waiting"
    assert cli.main(["turn", "waiting"]) == 2
    assert "at least one --question" in capsys.readouterr().err
    assert f"r1/{row['id']}-g: Status waiting (1 question)" in (cfg.root / "data/journal.log").read_text()


def test_turn_command_reports_errors_to_the_agent(project):
    _setup(project)
    project.queue({"actions": [status_action(), work_log(), status_action()]})
    _cfg, state = _turns(project)
    first, second = project.command_runs()
    assert first["code"] == 2 and "Work log entry" in first["out"]
    assert second["code"] == 0 and state.row("SELECT ok FROM turns")["ok"] == 1


def test_open_questions_section_is_added_to_older_context_files(tmp_path):
    path = tmp_path / "context.md"
    path.write_text("# T\n\n## Summary\nS.\n\n## Artifacts\n| a |\n\n## Work log\n")
    context_file.replace_section(path, "Open questions", "- Q?", after="Summary")
    assert path.read_text() == "# T\n\n## Summary\nS.\n\n## Open questions\n- Q?\n\n## Artifacts\n| a |\n\n## Work log\n"
    context_file.replace_section(path, "Open questions", "_None._", after="Summary")
    assert "## Open questions\n_None._\n\n## Artifacts" in path.read_text()


def test_a_database_of_vahtovik_1_is_refused(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE groups (watch TEXT, key TEXT, PRIMARY KEY (watch, key))")
    db.commit()
    db.close()
    with pytest.raises(ConfigError, match="a database of vahtovik 1"):
        State(path)
    assert State(tmp_path / "new.db").get_meta("schema") == "2"


def test_other_processes_see_a_turns_end_all_at_once(project):
    _cfg, state, row = _setup(project)
    other = State(project.load()[0].db_path)  # as a run in another process
    state.set_status(row["id"], "running")
    with state.transaction():
        state.set_status(row["id"], "finished")
        assert other.worker(row["id"])["status"] == "running"
    assert other.worker(row["id"])["status"] == "finished"
    with pytest.raises(ValueError), state.transaction():
        state.set_status(row["id"], "failed")
        raise ValueError
    assert other.worker(row["id"])["status"] == "finished"
