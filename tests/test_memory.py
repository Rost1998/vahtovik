import io
import logging

import pytest
from conftest import artifact, monitor_answer, run_turns, turn_answer, work_log

from vahtovik import artifacts, cli, groups, memory
from vahtovik.config import ConfigError
from vahtovik.stages import monitor
from vahtovik.tmux import Tmux


def _in_group(monkeypatch, key="g"):
    monkeypatch.setenv("VAHTOVIK_WATCH", "w1")
    monkeypatch.setenv("VAHTOVIK_GROUP", key)


def test_agents_add_and_remove_entries(project, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg, _state, _ = project.load()
    path = project.root / "watches/w1/MEMORY.md"
    _in_group(monkeypatch)
    assert memory.add(cfg, None, "The feed repeats edited entries.\n# Why\nIts ids stay.") == [
        "saved entry 1 to watches/w1/MEMORY.md"]
    text = path.read_text()
    assert text.startswith("# Memory of watch `w1`\n\nNotes that vahtovik's agents saved")
    assert "· group g\n\nThe feed repeats edited entries.\n#### Why\nIts ids stay.\n" in text
    assert caplog.records[-1].getMessage() == (
        "w1/g: Saved entry 1 to the memory of watch w1: The feed repeats edited entries.\n# Why\nIts ids stay.")

    monkeypatch.delenv("VAHTOVIK_GROUP")
    monkeypatch.setenv("VAHTOVIK_CHANNEL", "chat")
    memory.add(cfg, "w1", "Second.")
    monkeypatch.delenv("VAHTOVIK_CHANNEL")
    monkeypatch.delenv("VAHTOVIK_WATCH")
    memory.add(cfg, "w1", "Third.")
    assert [h.split(" · ")[::2] for h in path.read_text().splitlines() if h.startswith("## ")] == [
        ["## 1", "group g"], ["## 2", "channel chat"], ["## 3", "the person"]]

    assert memory.remove(cfg, "w1", 2, "it was wrong") == ["removed entry 2 from watches/w1/MEMORY.md"]
    assert caplog.records[-1].getMessage() == "w1: Removed entry 2 from the memory of watch w1 (it was wrong): Second."
    text = path.read_text()
    assert "Second." not in text and "Its ids stay.\n\n## 3 · " in text and text.endswith("Third.\n")
    memory.add(cfg, "w1", "Fourth.")
    assert "## 4 · " in path.read_text()  # numbers never come back
    with pytest.raises(ConfigError, match="has no entry 2; entries: 1, 3, 4"):
        memory.remove(cfg, "w1", 2, "again")


def test_memory_refuses_empty_text_secrets_and_other_watches(project, monkeypatch):
    cfg, _state, _ = project.load()
    (project.root / "watches/w1/.env").write_text("A_TOKEN=secret-value-123\nSHORT=yes\n")
    with pytest.raises(ConfigError, match="give the text"):
        memory.add(cfg, "w1", "  \n")
    with pytest.raises(ConfigError, match="the secret A_TOKEN"):
        memory.add(cfg, "w1", "Use secret-value-123 for the feed.")
    memory.add(cfg, "w1", "Say yes.")  # short values are no secrets
    with pytest.raises(ConfigError, match="give --watch"):
        memory.add(cfg, None, "Note.")
    _in_group(monkeypatch)
    with pytest.raises(ConfigError, match="you may change only its memory, not that of w2"):
        memory.add(cfg, "w2", "Note.")
    with pytest.raises(ConfigError, match="give --reason"):
        memory.remove(cfg, None, 1, " ")


def test_the_command_reads_the_text_from_stdin(project, monkeypatch, capsys):
    _in_group(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin.\n"))
    assert cli.main(["memory", "add"]) == 0
    assert capsys.readouterr().out == "saved entry 1 to watches/w1/MEMORY.md\n"
    assert cli.main(["memory", "remove", "7", "--reason", "x"]) == 2
    assert "has no entry 7; entries: 1" in capsys.readouterr().err


def test_group_and_monitoring_sessions_see_the_memory(project, monkeypatch):
    cfg, state, claude = project.load()
    watch = cfg.watch("w1")
    assert memory.prompt_text(watch) == "(no entries yet)"
    memory.add(cfg, "w1", "Group by the product name, not the version.")

    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, watch, "r1", project.tmp / "raw")
    assert all("## The watch's memory (MEMORY.md)" in c["prompt"] and "not the version." in c["prompt"]
               for c in project.calls())

    artifacts.register(cfg, state, watch, artifact("a1", "g"), "src-a")
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg))
    prompt = project.calls()[-1]["prompt"]
    assert "Group by the product name, not the version." in prompt and "vahtovik memory add" in prompt
    groups.write_claude_md(cfg, state, watch, "g")
    claude_md = (cfg.group_dir("w1", "g") / "CLAUDE.md").read_text()
    assert f"`{watch.memory_md}` holds notes" in claude_md and "vahtovik memory remove" in claude_md
