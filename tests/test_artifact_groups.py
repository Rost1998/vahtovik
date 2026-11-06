import sqlite3

from conftest import artifact, run_turns, turn_answer, work_log

from vahtovik import artifacts, watchdog
from vahtovik.state import State
from vahtovik.tmux import Tmux


def test_an_older_database_gets_artifacts_keyed_by_group_too(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE artifacts (watch TEXT, id TEXT, group_key TEXT, source_id TEXT, title TEXT, url TEXT, "
               "origin TEXT, status TEXT, attempts INTEGER DEFAULT 0, file TEXT, first_seen_at TEXT, "
               "processed_at TEXT, PRIMARY KEY (watch, id))")
    db.execute("INSERT INTO artifacts (watch, id, group_key, title, status, attempts) "
               "VALUES ('w1', 'a1', 'g1', 'A1', 'processed', 2)")
    db.commit()
    db.close()

    state = State(path)
    assert state._artifact_key() == ["watch", "id", "group_key"]
    row = state.row("SELECT * FROM artifacts")
    assert (row["id"], row["group_key"], row["title"], row["status"], row["attempts"]) == ("a1", "g1", "A1",
                                                                                          "processed", 2)
    state.add_artifact("w1", "a1", "g2", "src", "A1", "", "person", "new", "a1.json")
    assert state.artifact_exists("w1", "a1", "g2") and not state.artifact_exists("w1", "a1", "g3")
    State(path)  # opening it again leaves it as it is
    assert len(state.rows("SELECT * FROM artifacts")) == 2


def test_one_artifact_in_two_groups_is_processed_in_each(project):
    cfg, state, claude = project.load()
    watch = cfg.watch("w1")
    assert artifacts.register(cfg, state, watch, artifact("a1", "g1"), "src-a")
    assert not artifacts.register(cfg, state, watch, artifact("a1", "g2"), "src-a")  # monitoring: seen already
    project.queue(turn_answer([work_log()]))
    watchdog.check(cfg, state, Tmux(cfg), claude, "run1")
    run_turns(cfg, state, claude, Tmux(cfg))
    assert state.row("SELECT status FROM artifacts WHERE group_key = 'g1'")["status"] == "processed"

    assert artifacts.register(cfg, state, watch, artifact("a1", "g2"), "person", origin="person", any_group=False)
    assert not artifacts.register(cfg, state, watch, artifact("a1", "g2"), "person", origin="person",
                                  any_group=False)
    assert len(list((cfg.group_dir("w1", "g2") / "artifacts").glob("*.json"))) == 1
    project.queue(turn_answer([work_log()]))
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, "run2")
    run_turns(cfg, state, claude, Tmux(cfg))
    assert "a1" in project.calls()[1]["prompt"]
    rows = {r["group_key"]: r["status"] for r in state.rows("SELECT * FROM artifacts WHERE id = 'a1'")}
    assert rows == {"g1": "processed", "g2": "processed"}
