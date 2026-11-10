import json
from pathlib import Path

import pytest
from conftest import artifact, status_action, until

from vahtovik import artifacts, cli, context_file, operations
from vahtovik.config import ConfigError
from vahtovik.tmux import Tmux, Window


def _setup(project, *items):
    cfg, state, claude = project.load()
    for item in items:
        artifacts.register(cfg, state, cfg.watch("w1"), item, "src-a")
    return cfg, state, claude, Tmux(cfg)


def _context(cfg, key: str) -> str:
    return (cfg.group_dir("w1", key) / "context.md").read_text()


def _files(cfg, key: str) -> list[str]:
    return sorted(json.loads(p.read_text())["id"] for p in (cfg.group_dir("w1", key) / "artifacts").glob("*.json"))


def test_create_group(project):
    cfg, state, _, _ = _setup(project)
    assert operations.create_group(cfg, state, "w1/Kernel Fixes", "Kernel fixes") == [
        "created group w1/kernel-fixes",
        "the key 'Kernel Fixes' became 'kernel-fixes': group keys are lowercase words joined by hyphens"]
    assert state.group("w1", "kernel-fixes")["title"] == "Kernel fixes"
    assert "## Artifacts\n| First seen |" in _context(cfg, "kernel-fixes")
    with pytest.raises(ConfigError, match="exists already"):
        operations.create_group(cfg, state, "w1/kernel-fixes")
    with pytest.raises(ConfigError, match="unknown watch|no watch"):
        operations.create_group(cfg, state, "nope/g")


def test_merge_moves_everything_and_forwards_the_key(project):
    cfg, state, _, tmux = _setup(project, artifact("a1", "a"), artifact("b1", "b"), artifact("shared", "a"))
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("shared", "b"), "src-a", any_group=False)
    context = cfg.group_dir("w1", "b") / "context.md"
    context_file.replace_section(context, "Summary", "B is about bugs.\n# A heading")
    context_file.append(context, "Work log", "### 2026-10-17 10:00 · run r1\nLooked at b1.")
    context_file.append(context, "Reports", "- 2026-10-17 10:01 · log · sent · About b1")
    state.add_delivery("w1", "b", "log", "About b1", "Body", "automated", None)

    lines = operations.merge(cfg, state, tmux, ["w1/b"], "w1/a")
    assert lines[0].startswith("merged w1/b into w1/a: 1 artifact moved, 1 there already; the old folder is ")
    assert lines[1] == "3 artifacts wait for the next turn of w1/a"  # a1 and shared as before, and b1
    assert state.group("w1", "b") is None and not cfg.group_dir("w1", "b").exists()
    assert len(list((cfg.data_dir / "watches/w1/archive").glob("b-*"))) == 1
    assert {(r["id"], r["group_key"]) for r in state.rows("SELECT * FROM artifacts")} == {
        ("a1", "a"), ("b1", "a"), ("shared", "a")}
    assert _files(cfg, "a") == ["a1", "b1", "shared"]
    assert state.row("SELECT group_key FROM deliveries")["group_key"] == "a"

    text = _context(cfg, "a")
    work_log = context_file.section_text(cfg.group_dir("w1", "a") / "context.md", "Work log")
    assert work_log.count("\n### ") + work_log.startswith("### ") == 1  # one entry: the merge
    assert "· merged b" in work_log and "B is about bugs." in work_log and "#### A heading" in work_log
    assert "#### 2026-10-17 10:00 · run r1\nLooked at b1." in work_log
    assert "- 2026-10-17 10:01 · log · sent · About b1" in context_file.section_text(
        cfg.group_dir("w1", "a") / "context.md", "Reports")
    assert text.count("[link](https://example.com/") == 3  # the Artifacts section, from the database

    # monitoring still gives the key b: it goes to a
    assert artifacts.register(cfg, state, cfg.watch("w1"), artifact("b2", "b"), "src-a")
    assert state.row("SELECT group_key FROM artifacts WHERE id = 'b2'")["group_key"] == "a"
    assert state.group("w1", "b") is None
    operations.create_group(cfg, state, "w1/c")
    operations.merge(cfg, state, tmux, ["w1/a"], "w1/c")
    assert state.resolve_group("w1", "b") == "c"  # an alias that led to a now leads on to c
    with pytest.raises(ConfigError, match="was merged into w1/c"):
        operations.create_group(cfg, state, "w1/b")


def test_merge_refuses_a_group_in_use(project, monkeypatch):
    cfg, state, _, tmux = _setup(project, artifact("a1", "a"), artifact("b1", "b"))
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window("@5", "w1/b", 0)])
    with pytest.raises(ConfigError, match="w1/b has its session window open"):
        operations.merge(cfg, state, tmux, ["w1/b"], "w1/a")
    monkeypatch.undo()
    state.start_turn("t1", "run1", "w1", "a", ["a1"], 0)
    with pytest.raises(ConfigError, match="w1/a has an unfinished turn"):
        operations.merge(cfg, state, tmux, ["w1/b"], "w1/a")
    with pytest.raises(ConfigError, match="into itself"):
        operations.merge(cfg, state, tmux, ["w1/a"], "w1/a")
    assert state.group("w1", "b") is not None


def test_split_moves_artifacts_to_a_new_group(project):
    cfg, state, _, tmux = _setup(project, artifact("a1", "a"), artifact("a2", "a"), artifact("a3", "a"))
    state.mark_processed("w1", "a", ["a1", "a2", "a3"])
    context_file.replace_section(cfg.group_dir("w1", "a") / "context.md", "Summary", "Three releases.")

    lines = operations.split(cfg, state, tmux, "w1/a", ["a2", "a3"], "w1/c", "Release C")
    assert lines == ["moved 2 artifacts from w1/a to w1/c (a new group); they wait for the next turn of w1/c"]
    assert state.group("w1", "c")["title"] == "Release C"
    assert {r["id"]: r["status"] for r in state.group_artifacts("w1", "c")} == {"a2": "new", "a3": "new"}
    assert [r["id"] for r in state.group_artifacts("w1", "a")] == ["a1"]
    assert _files(cfg, "a") == ["a1"] and _files(cfg, "c") == ["a2", "a3"]
    assert "· split from a" in _context(cfg, "c") and "Three releases." in _context(cfg, "c")
    assert "· split into c" in _context(cfg, "a")
    assert _context(cfg, "a").count("[link](") == 1 and _context(cfg, "c").count("[link](") == 2
    assert state.pending_groups("w1", 3) == ["c"]
    with pytest.raises(ConfigError, match="not artifacts of w1/a: a2"):
        operations.split(cfg, state, tmux, "w1/a", ["a2"], "w1/c")


def test_add_artifacts_to_several_groups(project):
    cfg, state, _, _ = _setup(project)
    for key in ("a", "b"):
        operations.create_group(cfg, state, f"w1/{key}")
    items = [{"id": "x1", "title": "X one", "summary": "About x1."}, {"id": "x2", "title": "X two", "summary": "-"}]
    lines = operations.add_artifacts(cfg, state, "w1", ["a", "b"], json.dumps(items))
    assert lines == ["added: x1 in w1/a", "added: x1 in w1/b", "added: x2 in w1/a", "added: x2 in w1/b"]
    assert operations.add_artifacts(cfg, state, "w1", ["b"], json.dumps(items[0])) == ["there already: x1 in w1/b"]
    rows = state.rows("SELECT * FROM artifacts WHERE id = 'x1'")
    assert {(r["group_key"], r["status"], r["origin"], r["source_id"]) for r in rows} == {
        ("a", "new", "person", "person"), ("b", "new", "person", "person")}
    assert "| person | X one |" in _context(cfg, "b")
    with pytest.raises(ConfigError, match="artifact 1: 'summary' is a required property"):
        operations.add_artifacts(cfg, state, "w1", ["a"], '{"id": "x3", "title": "T"}')
    with pytest.raises(ConfigError, match="unknown group w1/nope"):
        operations.add_artifacts(cfg, state, "w1", ["nope"], json.dumps(items[0]))
    item = json.dumps({"id": "x4", "title": "X four", "summary": "-"})  # as a limited channel's agent gives them
    assert cli.main(["artifact", "add", "w1", "--group", "a", "--json", item]) == 0
    assert [r["id"] for r in state.group_artifacts("w1", "a")] == ["x1", "x2", "x4"]


def test_tell_starts_the_session_or_types_into_its_window(project, monkeypatch):
    cfg, state, _, tmux = _setup(project)
    operations.create_group(cfg, state, "w1/g")
    project.queue({"mode": "interactive", "actions": [status_action("finished", None, None)]})
    assert operations.tell(cfg, state, tmux, ["w1/g"], "Check the 1.0 notes.")[0].startswith(
        "w1/g: started its session with the message, tmux window @")
    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)
    assert project.calls()[0]["prompt"] == f"{operations.TELL_PREFIX} Check the 1.0 notes."
    assert state.window_record("w1", "g")["purpose"] == "tell"

    project.queue({"mode": "interactive", "actions": [status_action("finished", None, None)]})
    window = tmux.window("w1/g")
    until(lambda: "\n>" in tmux.capture(window.id))
    assert operations.tell(cfg, state, tmux, ["w1/g"], "And\nthe 1.1 notes.") == [
        "w1/g: typed the message into its open window"]
    typed = Path(f"{project.log_file}.typed")
    until(lambda: typed.exists() and "1.1 notes" in typed.read_text())
    assert json.loads(typed.read_text().splitlines()[-1])["line"] == f"{operations.TELL_PREFIX} And the 1.1 notes."

    monkeypatch.setattr(Tmux, "windows", lambda self: [Window(window.id, "w1/g", 1)])
    assert "a person is looking at its window" in operations.tell(cfg, state, tmux, ["w1/g"], "Hi.")[0]
    with pytest.raises(ConfigError, match="unknown group w1/nope"):
        operations.tell(cfg, state, tmux, ["w1/nope"], "Hi.")
