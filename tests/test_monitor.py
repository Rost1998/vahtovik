import json

import pytest
import yaml
from conftest import ROLE, _script, artifact, monitor_answer

from vahtovik import artifacts, cli, workers
from vahtovik.stages import monitor


def _run(project, poller: str = "p1", sources=None):
    cfg, state, claude = project.load()
    count = monitor.run(cfg, state, claude, cfg.poller(poller), "run1", project.tmp / "raw", sources)
    return cfg, state, count


def test_two_sources_bringing_the_same_worker_key_share_the_worker(project):
    project.queue(monitor_answer(artifact("a1", "Product X")),
                  monitor_answer(artifact("b1", "product-x"), artifact("b2", "other")))
    cfg, state, count = _run(project)
    assert count == 3
    assert sorted(w["key"] for w in state.workers()) == ["other", "product-x"]
    row = state.active_worker("r1", "product-x")
    folder = workers.folder(cfg, row)
    context = (folder / "context.md").read_text()
    assert context.count("https://example.com/a1") == 1 and context.count("https://example.com/b1") == 1
    assert len(list((folder / "artifacts").glob("*.json"))) == 2
    assert (folder / "CLAUDE.md").is_file() and (folder / ".claude/skills/rss").is_symlink()
    first = project.calls()[0]
    assert first["cwd"] == str(cfg.source_dir("p1", "src-a"))
    assert "--no-session-persistence" in first["args"]
    assert "Every item is an artifact" in first["prompt"] and "- `r1` (the default): Follows one product." in first["prompt"]
    links = first["args"][first["args"].index("--add-dir") + 1]
    assert sorted(p.name for p in (cfg.data_dir / "pollers/p1/sessions/src-a/.claude/skills").iterdir()) == [
        "rss", "workers"] and links.endswith("sessions/src-a")
    schema = json.loads(first["args"][first["args"].index("--json-schema") + 1])
    assert schema["properties"]["artifacts"]["items"]["properties"]["role"]["enum"] == ["r1"]
    assert "--append-system-prompt" in first["args"] and first["env"]["VAHTOVIK_POLLER"] == "p1"


def test_a_poller_gives_artifacts_to_workers_of_several_roles(project):
    project.role("r2", ROLE.replace("Follows one product.", "Follows one vendor."))
    project.poller(interval="1h", roles=["r1", "r2"], sources=[{"id": "src-a", "skill": "rss", "params": {"url": "a"}}])
    project.queue(monitor_answer(artifact("a1", "x"), artifact("a2", "x", role="r2")))
    cfg, state, count = _run(project)
    assert count == 2 and {(w["role"], w["key"]) for w in state.workers()} == {("r1", "x"), ("r2", "x")}
    assert "- `r2`: Follows one vendor." in project.calls()[0]["prompt"]


def test_seen_artifacts_are_dropped(project):
    project.queue(monitor_answer(artifact("a1", "x")), monitor_answer())
    _run(project)
    project.queue(monitor_answer(artifact("a1", "x"), artifact("a2", "x")), monitor_answer())
    _cfg, state, count = _run(project)
    assert count == 1
    assert "a1" in project.calls()[2]["prompt"]  # already registered ids are listed for the agent
    assert "role `r1`, key `x`: X" in project.calls()[2]["prompt"]  # and the workers


def test_failed_session_restores_the_source_state(project):
    cfg, _, _ = project.load()
    state_dir = cfg.source_dir("p1", "src-a")
    state_dir.mkdir(parents=True)
    (state_dir / "seen.txt").write_text("old\n")
    project.queue({"actions": [{"append": "seen.txt", "text": "new\n"}], "exit": 1}, monitor_answer())
    _cfg, state, count = _run(project)
    assert count == 0
    assert (state_dir / "seen.txt").read_text() == "old\n"
    assert state.source_state("p1", "src-a")["last_error"]


def _stop_run(*_args, **_kwargs):
    raise KeyboardInterrupt  # stands in for a killed run


def test_monitoring_stopped_during_the_session_starts_again_from_the_old_state(project, monkeypatch):
    cfg, state, claude = project.load()
    state_dir = cfg.source_dir("p1", "src-a")
    state_dir.mkdir(parents=True)
    (state_dir / "seen.txt").write_text("old\n")

    def session_then_stop(spec, raw_dir):
        (spec.workdir / "seen.txt").write_text("new\n")  # the scripts moved the markers
        _stop_run()

    with monkeypatch.context() as patch, pytest.raises(KeyboardInterrupt):
        patch.setattr(claude, "run", session_then_stop)
        monitor.run(cfg, state, claude, cfg.poller("p1"), "run1", project.tmp / "raw")

    project.queue(monitor_answer(artifact("a1", "x")), monitor_answer())
    _cfg, state, count = _run(project)
    assert count == 1
    assert (state_dir / "seen.txt").read_text() == "old\n"  # restored; the fake agent leaves it alone
    assert sorted(p.name for p in state_dir.parent.iterdir()) == ["src-a", "src-b"]


def test_monitoring_stopped_while_saving_saves_the_rest_on_the_next_run(project, monkeypatch):
    found = monitor_answer(artifact("a1", "x"), artifact("a2", "x"))
    project.queue(found, monitor_answer())
    cfg, state, claude = project.load()
    save = artifacts.from_poller
    with monkeypatch.context() as patch, pytest.raises(KeyboardInterrupt):
        patch.setattr(artifacts, "from_poller",
                      lambda cfg, state, poller, item, *rest: (_stop_run() if item["id"] == "a2"
                                                               else save(cfg, state, poller, item, *rest)))
        monitor.run(cfg, state, claude, cfg.poller("p1"), "run1", project.tmp / "raw")

    project.queue(found, monitor_answer())
    _cfg, state, count = _run(project)
    assert count == 1
    assert [r["id"] for r in state.rows("SELECT id FROM artifacts ORDER BY id")] == ["a1", "a2"]
    context = workers.context_md(cfg, state.active_worker("r1", "x")).read_text()
    assert context.count("example.com/a1") == 1 and context.count("example.com/a2") == 1


@pytest.fixture
def scripted(project, monkeypatch):
    """Source src-a of skill `scripted` with `scan: script`; its scan prints the file $SCAN_FILE and marks the
    state folder. Returns a function that sets what the next scan prints."""
    folder = project.root / "knowledge/skills/scripted"
    _script(folder / "scan", '#!/bin/sh\necho moved >> "$VAHTOVIK_STATE_DIR/seen.txt"\ncat "$SCAN_FILE"\n')
    (folder / "SKILL.md").write_text("---\nname: scripted\ndescription: test\n---\nItems come from the scan.\n")
    path = project.root / "pollers/p1/poller.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][0].update(skill="scripted", scan="script")
    path.write_text(yaml.safe_dump(data))
    out = project.tmp / "scan.json"
    monkeypatch.setenv("SCAN_FILE", str(out))
    return lambda value: out.write_text(value if isinstance(value, str) else json.dumps(value))


def _scan_run(project):
    cfg, _state, _claude = project.load()
    return _run(project, sources=[cfg.poller("p1").sources[0]])


def test_a_script_source_without_new_items_needs_no_session(project, scripted):
    scripted({"items": [], "error": None})
    cfg, state, count = _scan_run(project)
    assert count == 0 and project.calls() == []
    assert (cfg.source_dir("p1", "src-a") / "seen.txt").read_text() == "moved\n"
    assert state.source_state("p1", "src-a")["last_ok_at"]


def test_a_script_sources_new_items_go_to_one_session(project, scripted):
    scripted({"items": [{"id": "i1", "title": "Item one"}], "error": None})
    project.queue(monitor_answer(artifact("i1", "g")))
    _cfg, state, count = _scan_run(project)
    assert count == 1
    [call] = project.calls()
    assert "vahtovik ran the source's scan script" in call["prompt"] and '"title": "Item one"' in call["prompt"]
    assert state.poller_has("p1", "i1")


@pytest.mark.parametrize("printed", ['{"items": [], "error": "feed is down"}', "not json", '{"items": [{"x": 1}]}'])
def test_a_failed_scan_restores_the_state_and_starts_no_session(project, scripted, printed):
    scripted(printed)
    cfg, state, count = _scan_run(project)
    assert count == 0 and project.calls() == []
    assert not (cfg.source_dir("p1", "src-a") / "seen.txt").exists()
    assert state.source_state("p1", "src-a")["last_error"]


def test_a_failed_session_after_a_scan_restores_the_state(project, scripted):
    scripted({"items": [{"id": "i1"}], "error": None})
    project.queue({"mode": "print", "exit": 1})
    cfg, _state, count = _scan_run(project)
    assert count == 0 and len(project.calls()) == 1
    assert not (cfg.source_dir("p1", "src-a") / "seen.txt").exists()


def test_check_wants_a_scan_script_for_a_script_source(project, scripted, capsys):
    (project.root / "knowledge/skills/scripted/scan").unlink()
    cli.main(["check"])
    assert "poller p1, source src-a: scan is script, but" in capsys.readouterr().out


def test_the_rss_scan_script_lists_each_entry_once(project):
    feed = project.tmp / "feed.xml"
    feed.write_text('<?xml version="1.0"?><rss><channel><title>T</title><item><guid>g1</guid><title>One</title>'
                    '<link>http://x/1</link><description>d</description></item></channel></rss>')
    path = project.root / "pollers/p1/poller.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][0].update(scan="script", params={"url": f"file://{feed}"})
    path.write_text(yaml.safe_dump(data))
    project.queue(monitor_answer(artifact("g1", "g")))
    _scan_run(project)
    assert '"id": "g1"' in project.calls()[0]["prompt"]
    _scan_run(project)
    assert len(project.calls()) == 1
