import json
import re
from pathlib import Path

import pytest
from conftest import artifact, give, status_action, turn_answer, until, work_log, worker

from vahtovik import artifacts, cli, context_file, inbox, operations, workers
from vahtovik.config import ConfigError
from vahtovik.stages import worker_turn
from vahtovik.tmux import Tmux, Window


def _setup(project, *items):
    cfg, state = give(project, *items)
    return cfg, state, Tmux(cfg)


def _context(cfg, row) -> str:
    return workers.context_md(cfg, row).read_text()


def _files(cfg, row) -> list[str]:
    return sorted(json.loads(p.read_text())["id"] for p in (workers.folder(cfg, row) / "artifacts").glob("*.json"))


def test_spawn_a_worker(project):
    cfg, state, _ = _setup(project)
    lines = operations.spawn(cfg, state, "r1/kernel-fixes", ["web-page"], "Kernel fixes", "Find the fixes.")
    row = state.active_worker("r1", "kernel-fixes")
    assert lines == [f"spawned worker {row['id']}-kernel-fixes (r1/kernel-fixes)",
                     "it starts with the message at the next run"]
    assert (row["title"], row["spawned_by"], row["parent"], json.loads(row["skills"])) == (
        "Kernel fixes", "person", None, ["web-page"])
    assert "## Artifacts\n| First seen |" in _context(cfg, row)
    assert state.pending_messages(row["id"], 3)[0]["text"] == "Find the fixes."
    with pytest.raises(ConfigError, match="has a worker with key kernel-fixes already"):
        operations.spawn(cfg, state, "r1/kernel-fixes")
    with pytest.raises(ConfigError, match="unknown role"):
        operations.spawn(cfg, state, "nope/g")
    with pytest.raises(ConfigError, match="bad worker key"):
        operations.spawn(cfg, state, "r1/Kernel Fixes")
    with pytest.raises(ConfigError, match="unknown skill"):
        operations.spawn(cfg, state, "r1/x", ["no-such-skill"])
    with pytest.raises(ConfigError, match="only a worker can be a parent"):
        operations.spawn(cfg, state, "r1/y", report_back=True)


def test_merge_moves_everything_and_forwards_the_key(project):
    cfg, state, tmux = _setup(project, artifact("a1", "a"), artifact("b1", "b"), artifact("shared", "a"))
    a, b = state.active_worker("r1", "a"), state.active_worker("r1", "b")
    artifacts.save(cfg, state, b, artifact("shared", "b"), "src-a", origin="person")
    context = workers.context_md(cfg, b)
    context_file.replace_section(context, "Summary", "B is about bugs.\n# A heading")
    context_file.append(context, "Work log", "### 2026-10-17 10:00 · run r1\nLooked at b1.")
    inbox.send(state, b, "person", "Waiting for b.")

    lines = operations.merge(cfg, state, tmux, [f"{b['id']}-b"], "r1/a")
    assert lines[0].startswith(f"merged {b['id']}-b into {a['id']}-a: 1 artifact moved, 1 there already; "
                               "the old folder is ")
    assert lines[1] == f"3 artifacts wait for the next turn of {a['id']}-a"  # a1 and shared as before, and b1
    merged = state.worker(b["id"])
    assert merged["archived_at"] and merged["merged_into"] == a["id"]
    assert not (cfg.workers_dir / "r1" / f"{b['id']}-b").exists()
    assert (cfg.archive_dir / "r1" / f"{b['id']}-b" / "context.md").is_file()
    assert {(r["id"], r["worker"]) for r in state.rows("SELECT * FROM artifacts")} == {
        ("a1", a["id"]), ("b1", a["id"]), ("shared", a["id"])}
    assert _files(cfg, a) == ["a1", "b1", "shared"]
    assert state.pending_messages(a["id"], 3)[0]["text"] == "Waiting for b."

    work_log = context_file.section_text(workers.context_md(cfg, a), "Work log")
    assert work_log.count("\n### ") + work_log.startswith("### ") == 1  # one entry: the merge
    assert f"· merged {b['id']}-b" in work_log and "B is about bugs." in work_log and "#### A heading" in work_log
    assert "#### 2026-10-17 10:00 · run r1\nLooked at b1." in work_log
    assert _context(cfg, a).count("[link](https://example.com/") == 3  # the Artifacts section, from the database

    # monitoring still gives the key b: it goes to a
    assert artifacts.from_poller(cfg, state, cfg.poller("p1"), artifact("b2", "b"), "src-a")
    assert state.row("SELECT worker FROM artifacts WHERE id = 'b2'")["worker"] == a["id"]
    assert state.active_worker("r1", "b") is None
    assert workers.resolve(cfg, state, "r1/b")["id"] == a["id"]
    with pytest.raises(ConfigError, match=f"was merged into {a['id']}-a"):
        workers.resolve(cfg, state, b["id"])
    operations.spawn(cfg, state, "r1/c")
    c = state.active_worker("r1", "c")
    operations.merge(cfg, state, tmux, ["r1/a"], "r1/c")
    assert state.find_worker("r1", "b")["id"] == c["id"]  # a key that led to a now leads on to c


def test_merge_refuses_a_worker_in_use(project, monkeypatch):
    cfg, state, tmux = _setup(project, artifact("a1", "a"), artifact("b1", "b"))
    a, b = state.active_worker("r1", "a"), state.active_worker("r1", "b")
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window("@5", b["id"], 0)])
    with pytest.raises(ConfigError, match=f"{b['id']}-b has its session window open"):
        operations.merge(cfg, state, tmux, ["r1/b"], "r1/a")
    monkeypatch.undo()
    state.start_turn("t1", "run1", a["id"], ["a1"], [], 0)
    with pytest.raises(ConfigError, match=f"{a['id']}-a has an unfinished turn"):
        operations.merge(cfg, state, tmux, ["r1/b"], "r1/a")
    with pytest.raises(ConfigError, match="into itself"):
        operations.merge(cfg, state, tmux, ["r1/a"], a["id"])
    assert state.worker(b["id"])["archived_at"] is None


def test_split_moves_artifacts_to_a_new_worker(project):
    cfg, state, tmux = _setup(project, artifact("a1", "a"), artifact("a2", "a"), artifact("a3", "a"))
    a = state.active_worker("r1", "a")
    state.mark_processed(a["id"], ["a1", "a2", "a3"])
    context_file.replace_section(workers.context_md(cfg, a), "Summary", "Three releases.")

    lines = operations.split(cfg, state, tmux, "r1/a", ["a2", "a3"], "r1/c", "Release C")
    c = state.active_worker("r1", "c")
    assert lines == [f"moved 2 artifacts from {a['id']}-a to {c['id']}-c (a new worker); they wait for the next "
                     f"turn of {c['id']}-c"]
    assert c["title"] == "Release C" and c["spawned_by"] == "person"
    assert {r["id"]: r["status"] for r in state.worker_artifacts(c["id"])} == {"a2": "new", "a3": "new"}
    assert [r["id"] for r in state.worker_artifacts(a["id"])] == ["a1"]
    assert _files(cfg, a) == ["a1"] and _files(cfg, c) == ["a2", "a3"]
    assert f"· split from {a['id']}-a" in _context(cfg, c) and "Three releases." in _context(cfg, c)
    assert f"· split into {c['id']}-c" in _context(cfg, a)
    assert _context(cfg, a).count("[link](") == 1 and _context(cfg, c).count("[link](") == 2
    assert state.pending_workers(3) == [c["id"]]
    with pytest.raises(ConfigError, match=f"not artifacts of {a['id']}-a: a2"):
        operations.split(cfg, state, tmux, "r1/a", ["a2"], "r1/c")
    operations.split(cfg, state, tmux, c["id"], ["a3"], f"{a['id']}-a")  # into an existing worker
    assert [r["id"] for r in state.worker_artifacts(a["id"])] == ["a1", "a3"]


def test_add_artifacts_to_several_workers(project):
    cfg, state, _ = _setup(project)
    a, b = worker(cfg, state, "a"), worker(cfg, state, "b")
    items = [{"id": "x1", "title": "X one", "summary": "About x1."}, {"id": "x2", "title": "X two", "summary": "-"}]
    lines = operations.add_artifacts(cfg, state, ["r1/a", b["id"]], json.dumps(items))
    assert lines == [f"added: x1 in {a['id']}-a, for its next turn", f"added: x1 in {b['id']}-b, for its next turn",
                     f"added: x2 in {a['id']}-a, for its next turn", f"added: x2 in {b['id']}-b, for its next turn"]
    assert operations.add_artifacts(cfg, state, ["r1/b"], json.dumps(items[0])) == [f"there already: x1 in {b['id']}-b"]
    rows = state.rows("SELECT * FROM artifacts WHERE id = 'x1'")
    assert {(r["worker"], r["status"], r["origin"], r["source_id"]) for r in rows} == {
        (a["id"], "new", "person", "person"), (b["id"], "new", "person", "person")}
    assert "| person | X one |" in _context(cfg, b)
    with pytest.raises(ConfigError, match="artifact 1: 'summary' is a required property"):
        operations.add_artifacts(cfg, state, ["r1/a"], '{"id": "x3", "title": "T"}')
    with pytest.raises(ConfigError, match="unknown worker r1/nope"):
        operations.add_artifacts(cfg, state, ["r1/nope"], json.dumps(items[0]))
    with pytest.raises(ConfigError, match="name at least one worker"):
        operations.add_artifacts(cfg, state, [], json.dumps(items[0]))
    item = json.dumps({"id": "x4", "title": "X four", "summary": "-"})  # as a limited channel's agent gives them
    assert cli.main(["artifact", "add", "--worker", "r1/a", "--json", item]) == 0
    assert [r["id"] for r in state.worker_artifacts(a["id"])] == ["x1", "x2", "x4"]


def test_tell_queues_the_message_or_types_it_into_an_open_window(project, monkeypatch):
    cfg, state, tmux = _setup(project)
    row = worker(cfg, state)
    name = f"{row['id']}-g"
    assert operations.tell(cfg, state, tmux, ["r1/g"], "Check the 1.0 notes.") == [
        f"{name}: queued: it gets the message in a turn at the next run"]

    project.queue(turn_answer([work_log()]),
                  {"mode": "interactive", "actions": [status_action("finished", None, None)]})
    cfg, state, claude = project.load()
    worker_turn.run(cfg, state, claude, tmux, "run1")
    until(lambda: state.worker(row["id"])["status"] == "finished" and state.worker(row["id"])["idle"] == 1)
    assert "Message from the person, sent through vahtovik:\n\nCheck the 1.0 notes." in project.calls()[0]["prompt"]
    window = tmux.window(row["id"])
    until(lambda: "\n>" in tmux.capture(window.id))
    assert operations.tell(cfg, state, tmux, [name], "And\nthe 1.1 notes.") == [
        f"{name}: typed the message into its open window"]
    typed = Path(f"{project.log_file}.typed")
    until(lambda: typed.exists() and "1.1 notes" in typed.read_text())
    line = json.loads(typed.read_text().splitlines()[-1])["line"]
    assert re.fullmatch(r"Message from the person, sent through vahtovik: \[\d{4}-\d\d-\d\d \d\d:\d\d\] "
                        r"And the 1\.1 notes\.", line)

    monkeypatch.setattr(Tmux, "windows", lambda self: [Window(window.id, row["id"], 1)])
    assert "a person is looking at its window" in operations.tell(cfg, state, tmux, [name], "Hi.")[0]
    with pytest.raises(ConfigError, match="unknown worker r1/nope"):
        operations.tell(cfg, state, tmux, ["r1/nope"], "Hi.")
    with pytest.raises(ConfigError, match="that is you"):
        operations.tell(cfg, state, tmux, [name], "Hi.", env={"VAHTOVIK_WORKER": row["id"]})


def test_archive_frees_the_key_and_hides_the_worker(project):
    cfg, state, tmux = _setup(project, artifact("a1", "g"))
    row = state.active_worker("r1", "g")
    assert operations.archive(cfg, state, tmux, "r1/g", "the version line is over") == [
        f"archived {row['id']}-g; its folder is {cfg.archive_dir / 'r1' / (row['id'] + '-g')}"]
    assert state.pending_workers(3) == []  # its new artifact waits for nothing now
    assert "no workers" in operations.list_workers(cfg, state)[0]
    assert operations.list_workers(cfg, state, archived=True)[0].startswith(f"{row['id']}-g · r1 · archived")
    assert any("archived" in line for line in operations.show(cfg, state, row["id"]))
    with pytest.raises(ConfigError, match="is archived: the version line is over"):
        workers.resolve(cfg, state, row["id"])
    give(project, artifact("a2", "g"))  # the key is free: a new worker
    assert state.active_worker("r1", "g")["id"] != row["id"]
