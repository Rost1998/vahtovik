import json

from conftest import artifact, give, run_turns, turn_answer, work_log

from vahtovik import artifacts, operations, watchdog, workers
from vahtovik.tmux import Tmux


def test_a_poller_drops_what_it_brought_before_and_spawns_workers(project):
    cfg, state = give(project, artifact("a1", "g1"))
    row = state.active_worker("r1", "g1")
    assert row["spawned_by"] == "poller:p1" and row["title"] == "G1" and row["parent"] is None
    assert not artifacts.from_poller(cfg, state, cfg.poller("p1"), artifact("a1", "g2"), "src-a")
    assert state.active_worker("r1", "g2") is None  # nothing was new, so no worker was spawned
    data = json.loads((workers.folder(cfg, row) / "artifacts").glob("*.json").__next__().read_text())
    assert (data["worker"], data["role"], data["poller"], data["source_id"]) == (f"{row['id']}-g1", "r1", "p1",
                                                                                "src-a")


def test_one_artifact_in_two_workers_is_processed_in_each(project):
    cfg, state = give(project, artifact("a1", "g1"))
    cfg, state, claude = project.load()
    project.queue(turn_answer([work_log()]))
    watchdog.check(cfg, state, Tmux(cfg), claude, "run1")
    run_turns(cfg, state, claude, Tmux(cfg))
    first = state.active_worker("r1", "g1")
    assert state.row("SELECT status FROM artifacts WHERE worker = ?", first["id"])["status"] == "processed"

    operations.spawn(cfg, state, "r1/g2")
    item = json.dumps({"id": "a1", "title": "A1", "summary": "About a1."})
    assert operations.add_artifacts(cfg, state, ["r1/g2"], item)[0].startswith("added: a1 in")
    assert operations.add_artifacts(cfg, state, ["r1/g2"], item)[0].startswith("there already: a1 in")
    second = state.active_worker("r1", "g2")
    assert len(list((workers.folder(cfg, second) / "artifacts").glob("*.json"))) == 1
    project.queue(turn_answer([work_log()]))
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, "run2")
    run_turns(cfg, state, claude, Tmux(cfg))
    assert "a1" in project.calls()[1]["prompt"]
    rows = {r["worker"]: r["status"] for r in state.rows("SELECT * FROM artifacts WHERE id = 'a1'")}
    assert rows == {first["id"]: "processed", second["id"]: "processed"}
    person = state.row("SELECT * FROM artifacts WHERE worker = ?", second["id"])
    assert (person["origin"], person["source_id"], person["poller"]) == ("person", "person", None)
