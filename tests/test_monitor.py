from conftest import artifact, monitor_answer

from vahtovik.stages import monitor


def _run(project):
    cfg, state, claude = project.load()
    count = monitor.run(cfg, state, claude, cfg.watch("w1"), "run1", project.tmp / "raw")
    return cfg, state, count


def test_two_sources_creating_the_same_group_merge(project):
    project.queue(monitor_answer(artifact("a1", "Product X")), monitor_answer(artifact("b1", "product-x"), artifact("b2", "other")))
    cfg, state, count = _run(project)
    assert count == 3
    assert sorted(g["key"] for g in state.groups("w1")) == ["other", "product-x"]
    group_dir = cfg.group_dir("w1", "product-x")
    context = (group_dir / "context.md").read_text()
    assert context.count("https://example.com/a1") == 1 and context.count("https://example.com/b1") == 1
    assert len(list((group_dir / "artifacts").glob("*.json"))) == 2
    assert (group_dir / "CLAUDE.md").is_file()
    first = project.calls()[0]
    assert first["cwd"] == str(cfg.source_dir("w1", "src-a"))
    assert "--no-session-persistence" in first["args"]
    assert "Every item is an artifact" in first["prompt"]


def test_seen_artifacts_are_dropped(project):
    project.queue(monitor_answer(artifact("a1", "x")), monitor_answer())
    _run(project)
    project.queue(monitor_answer(artifact("a1", "x"), artifact("a2", "x")), monitor_answer())
    _cfg, state, count = _run(project)
    assert count == 1
    assert "a1" in project.calls()[2]["prompt"]  # already registered ids are listed for the agent


def test_failed_session_restores_the_source_state(project):
    cfg, _, _ = project.load()
    state_dir = cfg.source_dir("w1", "src-a")
    state_dir.mkdir(parents=True)
    (state_dir / "seen.txt").write_text("old\n")
    project.queue({"actions": [{"append": "seen.txt", "text": "new\n"}], "exit": 1}, monitor_answer())
    _cfg, state, count = _run(project)
    assert count == 0
    assert (state_dir / "seen.txt").read_text() == "old\n"
    assert state.row("SELECT last_error FROM source_state WHERE source_id = 'src-a'")["last_error"]
