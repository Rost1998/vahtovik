from datetime import timedelta

import pytest
from conftest import ROLE, artifact, give, run_turns, turn_answer, work_log, worker

from vahtovik import cli, operations, watchdog, worker_status, workers
from vahtovik.config import ConfigError
from vahtovik.stages import worker_turn
from vahtovik.tmux import Tmux


def _turns(project, run_id: str = "run1"):
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, run_id)
    run_turns(cfg, state, claude, Tmux(cfg), run_id=run_id)
    return cfg, state


def test_a_worker_has_three_references_and_a_key_may_repeat_in_another_role(project):
    project.role("r2", ROLE)
    cfg, state, _ = project.load()
    one, two = worker(cfg, state, "g"), worker(cfg, state, "g", role="r2")
    assert one["id"] != two["id"] and len(one["id"]) == 6
    for ref in (one["id"], f"{one['id']}-g", "r1/g", " r1/g "):
        assert workers.resolve(cfg, state, ref)["id"] == one["id"]
    assert workers.resolve(cfg, state, "r2/g")["id"] == two["id"]
    assert workers.folder(cfg, two) == cfg.workers_dir / "r2" / f"{two['id']}-g"
    with pytest.raises(ConfigError, match=f"worker {one['id']} is {one['id']}-g, not {one['id']}-h"):
        workers.resolve(cfg, state, f"{one['id']}-h")
    for bad in ("g", "abc", "r1/nope", "ffffff"):
        with pytest.raises(ConfigError):
            workers.resolve(cfg, state, bad)


def test_a_limited_scope_sees_only_its_roles(project, monkeypatch):
    project.role("r2", ROLE)
    cfg, state, _ = project.load()
    other = worker(cfg, state, "g", role="r2")
    monkeypatch.setenv("VAHTOVIK_ROLES", "r1")
    cfg, state, _ = project.load()
    assert list(cfg.roles) == ["r1"]
    with pytest.raises(ConfigError, match="unknown worker"):
        workers.resolve(cfg, state, other["id"])
    with pytest.raises(ConfigError, match="unknown role"):
        operations.spawn(cfg, state, "r2/h")


def test_the_folder_has_the_skill_links_and_the_session_the_credentials(project):
    project.credentials("telegram", "TELEGRAM_BOT_TOKEN=bot-token-one\nTELEGRAM_CHAT_ID=-100\n")
    project.credentials("extra", "TELEGRAM_CHAT_ID=-200\nEXTRA=1\n")
    project.role("r1", ROLE.replace("skills: [file, rss]", "skills: [file, rss, telegram]\ncredentials: [extra]"))
    cfg, state, claude = project.load()
    row = worker(cfg, state, skills=["web-page"])
    links = workers.folder(cfg, row) / ".claude" / "skills"
    assert sorted(p.name for p in links.iterdir()) == ["file", "rss", "telegram", "vahtovik", "web-page"]
    assert (links / "rss").resolve() == (cfg.knowledge_dir / "skills" / "rss").resolve()
    spec, _ = workers.spec_for(cfg, claude, state, row)
    assert spec.env["TELEGRAM_BOT_TOKEN"] == "bot-token-one" and spec.env["TELEGRAM_CHAT_ID"] == "-200"
    assert spec.env["VAHTOVIK_WORKER_NAME"] == f"{row['id']}-g" and spec.env["VAHTOVIK_ROLE"] == "r1"
    assert "# Working inside vahtovik" in spec.system_prompt
    claude_md = (workers.folder(cfg, row) / "CLAUDE.md").read_text()
    assert f"You are the vahtovik worker `{row['id']}-g`, of role `r1`" in claude_md
    assert "`web-page`" in claude_md and "Spawned by: the person" in claude_md

    project.role("r1", ROLE)  # the role loses telegram: the next start drops its link
    cfg, state, claude = project.load()
    workers.prepare(cfg, state, state.worker(row["id"]))
    assert sorted(p.name for p in links.iterdir()) == ["file", "rss", "vahtovik", "web-page"]


def test_a_worker_spawns_a_child_that_reports_back(project):
    cfg, state = give(project, artifact("a1", "g"))
    parent = state.active_worker("r1", "g")
    spawn = ["vahtovik", "worker", "spawn", "r1/research", "--title", "Research", "--message", "Find the docs.",
             "--report-back"]
    project.queue(turn_answer([work_log(), {"run": spawn}]))
    cfg, state = _turns(project)
    child = state.active_worker("r1", "research")
    assert (child["spawned_by"], child["parent"], child["report_back"]) == (f"worker:{parent['id']}", parent["id"], 1)
    assert state.pending_messages(child["id"], 3)[0]["sender"] == f"worker:{parent['id']}"

    project.queue(turn_answer([work_log("Found them.")], summary="The docs are at example.com."))
    cfg, state = _turns(project, "run2")
    prompt = project.calls()[1]["prompt"]
    assert f"Message from worker {parent['id']}-g (your parent), sent through vahtovik:" in prompt
    message, = state.pending_messages(parent["id"], 3)
    assert message["sender"] == f"worker:{child['id']}"
    assert f"Your child worker {child['id']}-research" in message["text"] and "is finished now" in message["text"]
    assert "Summary: The docs are at example.com." in message["text"]

    project.queue(turn_answer([work_log("Read the report.")]))
    _turns(project, "run3")
    assert f"Message from worker {child['id']}-research (your child)" in project.calls()[2]["prompt"]


def test_report_back_tells_the_parent_of_a_waiting_child_and_of_one_that_failed_for_good(project):
    cfg, state, _ = project.load()
    parent = worker(cfg, state, "p")
    child = workers.create(cfg, state, "r1", "c", "C", f"worker:{parent['id']}", parent["id"], report_back=True)
    worker_status.declare(cfg, state, child, "waiting", questions=["Which version?"])
    message, = state.pending_messages(parent["id"], 3)
    assert "is waiting now" in message["text"] and "Its questions: Which version?" in message["text"]
    worker_status.declare(cfg, state, state.worker(child["id"]), "scheduled", after=timedelta(hours=1))
    assert len(state.pending_messages(parent["id"], 3)) == 1  # scheduled is no end: no message

    lonely = workers.create(cfg, state, "r1", "l", "L", f"worker:{parent['id']}", parent["id"])
    worker_status.declare(cfg, state, lonely, "finished")
    assert len(state.pending_messages(parent["id"], 3)) == 1  # no --report-back: no message

    state.execute("UPDATE workers SET scheduled_at = NULL WHERE id = ?", child["id"])
    for attempt in range(worker_turn.MAX_ATTEMPTS):
        state.start_turn(f"t{attempt}", "run1", child["id"], [], [], 0)
        worker_turn.fail(cfg, state, state.turn(f"t{attempt}"), "it broke", 0)
    last = state.pending_messages(parent["id"], 3)[-1]
    assert "is failed now" in last["text"] and "Why: it broke" in last["text"]


def test_workers_see_each_other_from_inside(project, monkeypatch, capsys):
    cfg, state, _ = project.load()
    parent = worker(cfg, state, "p")
    child = workers.create(cfg, state, "r1", "c", "Child", f"worker:{parent['id']}", parent["id"])
    state.set_summary(child["id"], "Reading the changelog.")
    monkeypatch.setenv("VAHTOVIK_WORKER", parent["id"])
    assert cli.main(["worker", "list", "--tree"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{parent['id']}-p · r1 · idle · parent none · P")
    assert out[2].startswith(f"  {child['id']}-c · r1 · idle · parent {parent['id']}-p · Child")
    assert out[3] == "    Reading the changelog."
    assert cli.main(["worker", "show", "r1/c"]) == 0
    shown = capsys.readouterr().out
    assert f"spawned by worker {parent['id']}-p" in shown and f"parent: {parent['id']}-p" in shown
    assert str(workers.context_md(cfg, child)) in shown
    assert cli.main(["worker", "show", parent["id"]]) == 0
    assert f"children: {child['id']}-c (idle)" in capsys.readouterr().out


def test_memory_belongs_to_the_role(project, monkeypatch):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    monkeypatch.setenv("VAHTOVIK_WORKER", row["id"])
    monkeypatch.setenv("VAHTOVIK_WORKER_NAME", f"{row['id']}-g")
    monkeypatch.setenv("VAHTOVIK_ROLE", "r1")
    assert cli.main(["memory", "add", "--text", "The feed repeats edited entries."]) == 0
    text = cfg.roles["r1"].memory_md.read_text()
    assert "# Memory of role `r1`" in text and "## 1 · " in text and f"· worker {row['id']}-g" in text
    assert cli.main(["memory", "add", "--role", "other", "--text", "No."]) == 2
    project.credentials("telegram", "TELEGRAM_BOT_TOKEN=secret-token-123\n")
    assert cli.main(["memory", "add", "--text", "The token is secret-token-123."]) == 2
