import io
import logging

import pytest
from conftest import artifact, give, monitor_answer, run_turns, turn_answer, work_log, worker

from vahtovik import cli, memory, workers
from vahtovik.config import ConfigError
from vahtovik.stages import monitor
from vahtovik.tmux import Tmux


def _in_worker(monkeypatch, name="a3f9c2-g", role="r1"):
    monkeypatch.setenv("VAHTOVIK_WORKER", name.split("-")[0])
    monkeypatch.setenv("VAHTOVIK_WORKER_NAME", name)
    monkeypatch.setenv("VAHTOVIK_ROLE", role)


def test_agents_add_and_remove_entries(project, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg, _state, _ = project.load()
    path = project.root / "roles/r1/MEMORY.md"
    _in_worker(monkeypatch)
    assert memory.add(cfg, None, "The feed repeats edited entries.\n# Why\nIts ids stay.") == [
        "saved entry 1 to roles/r1/MEMORY.md"]
    text = path.read_text()
    assert text.startswith("# Memory of role `r1`\n\nNotes that vahtovik's agents saved")
    assert "· worker a3f9c2-g\n\nThe feed repeats edited entries.\n#### Why\nIts ids stay.\n" in text
    assert caplog.records[-1].getMessage() == (
        "r1/a3f9c2-g: Saved entry 1 to the memory of role r1: The feed repeats edited entries.\n# Why\nIts ids stay.")

    for name in ("VAHTOVIK_WORKER", "VAHTOVIK_WORKER_NAME", "VAHTOVIK_ROLE"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("VAHTOVIK_CHANNEL", "chat")
    memory.add(cfg, "r1", "Second.")
    monkeypatch.delenv("VAHTOVIK_CHANNEL")
    memory.add(cfg, "r1", "Third.")
    assert [h.split(" · ")[::2] for h in path.read_text().splitlines() if h.startswith("## ")] == [
        ["## 1", "worker a3f9c2-g"], ["## 2", "channel chat"], ["## 3", "the person"]]

    assert memory.remove(cfg, "r1", 2, "it was wrong") == ["removed entry 2 from roles/r1/MEMORY.md"]
    assert caplog.records[-1].getMessage() == "r1: Removed entry 2 from the memory of role r1 (it was wrong): Second."
    text = path.read_text()
    assert "Second." not in text and "Its ids stay.\n\n## 3 · " in text and text.endswith("Third.\n")
    memory.add(cfg, "r1", "Fourth.")
    assert "## 4 · " in path.read_text()  # numbers never come back
    with pytest.raises(ConfigError, match="has no entry 2; entries: 1, 3, 4"):
        memory.remove(cfg, "r1", 2, "again")


def test_memory_refuses_empty_text_secrets_and_other_roles(project, monkeypatch):
    project.role("r2")
    cfg, _state, _ = project.load()
    project.credentials("feed", "A_TOKEN=secret-value-123\nSHORT=yes\n")
    with pytest.raises(ConfigError, match="give the text"):
        memory.add(cfg, "r1", "  \n")
    with pytest.raises(ConfigError, match=r"the secret A_TOKEN \(feed.env\)"):
        memory.add(cfg, "r1", "Use secret-value-123 for the feed.")
    memory.add(cfg, "r1", "Say yes.")  # short values are no secrets
    with pytest.raises(ConfigError, match="give --role"):
        memory.add(cfg, None, "Note.")
    _in_worker(monkeypatch)
    with pytest.raises(ConfigError, match="you may change only its memory, not that of r2"):
        memory.add(cfg, "r2", "Note.")
    with pytest.raises(ConfigError, match="give --reason"):
        memory.remove(cfg, None, 1, " ")


def test_the_command_reads_the_text_from_stdin(project, monkeypatch, capsys):
    _in_worker(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin.\n"))
    assert cli.main(["memory", "add"]) == 0
    assert capsys.readouterr().out == "saved entry 1 to roles/r1/MEMORY.md\n"
    assert cli.main(["memory", "remove", "7", "--reason", "x"]) == 2
    assert "has no entry 7; entries: 1" in capsys.readouterr().err


def test_workers_and_monitoring_sessions_see_the_memory(project):
    cfg, state, claude = project.load()
    assert memory.prompt_text(cfg.role("r1")) == "(no entries yet)"
    memory.add(cfg, "r1", "One worker per product name, not per version.")

    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, cfg.poller("p1"), "r1", project.tmp / "raw")
    assert all("## The memory of these roles (MEMORY.md)" in c["prompt"] and "not per version." in c["prompt"]
               for c in project.calls())

    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg))
    prompt = project.calls()[-1]["prompt"]
    assert "One worker per product name, not per version." in prompt and "vahtovik memory add" in prompt
    claude_md = (workers.folder(cfg, worker(cfg, state)) / "CLAUDE.md").read_text()
    assert f"`{cfg.role('r1').memory_md}` holds notes" in claude_md and "vahtovik memory remove" in claude_md
