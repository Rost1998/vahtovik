import io
import json
from datetime import timedelta

import pytest
from conftest import ROLE, artifact, give, run_turns, turn_answer, work_log, worker

from vahtovik import cli, inbox, memory, operations, watchdog, worker_status, workers
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
    assert sorted(p.name for p in links.iterdir()) == ["file", "rss", "telegram", "web-page", "workers"]
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
    assert sorted(p.name for p in links.iterdir()) == ["file", "rss", "web-page", "workers"]


def test_a_worker_spawns_a_child_and_watches_it(project):
    cfg, state = give(project, artifact("a1", "g"))
    parent = state.active_worker("r1", "g")
    spawn = ["vahtovik", "worker", "spawn", "r1/research", "--title", "Research", "--message", "Find the docs.",
             "--watch"]
    project.queue(turn_answer([work_log(), {"run": spawn}]))
    cfg, state = _turns(project)
    child = state.active_worker("r1", "research")
    assert (child["spawned_by"], child["parent"]) == (f"worker:{parent['id']}", parent["id"])
    assert [w["id"] for w in state.watchers(child["id"])] == [parent["id"]]
    assert state.pending_messages(child["id"], 3)[0]["sender"] == f"worker:{parent['id']}"

    project.queue(turn_answer([work_log("Found them.")], summary="The docs are at example.com."))
    cfg, state = _turns(project, "run2")
    prompt = project.calls()[1]["prompt"]
    assert f"Message from worker {parent['id']}-g (your parent, it watches you), sent through vahtovik:" in prompt
    message, = state.pending_messages(parent["id"], 3)
    assert message["sender"] == f"worker:{child['id']}"
    assert f"Worker {child['id']}-research (role r1, \"Research\"), which you watch, is finished now." in message["text"]
    assert "Summary: The docs are at example.com." in message["text"]
    assert f"See it: vahtovik worker show {child['id']}-research" in message["text"]

    project.queue(turn_answer([work_log("Read the notice.")]))
    _turns(project, "run3")
    assert f"Message from worker {child['id']}-research (your child, you watch it)" in project.calls()[2]["prompt"]
    claude_md = (workers.folder(cfg, parent) / "CLAUDE.md").read_text()
    assert f"- You watch: `{child['id']}-research` · Watched by: nobody" in claude_md


def test_every_watcher_hears_each_time_the_watched_worker_stops_working(project):
    cfg, state, _ = project.load()
    a, b, c, lonely = (worker(cfg, state, key) for key in ("a", "b", "c", "l"))
    lines = operations.watch(cfg, state, ["r1/c"], env={"VAHTOVIK_WORKER": a["id"]})
    assert lines[0].startswith(f"{a['id']}-a watches {c['id']}-c now: vahtovik tells you each time")
    assert lines[1] == "  it is idle now: (no turn has ended yet)"
    operations.watch(cfg, state, [c["id"], lonely["id"]], watcher="r1/b")
    operations.watch(cfg, state, [c["id"]], watcher="r1/b")  # again: still one watch
    assert [w["id"] for w in state.watchers(c["id"])] == [a["id"], b["id"]]
    assert [w["id"] for w in state.watched(b["id"])] == [c["id"], lonely["id"]]
    shown = "\n".join(operations.show(cfg, state, "r1/c") + operations.show(cfg, state, "r1/b"))
    assert f"  watches: nobody; watched by: {a['id']}-a, {b['id']}-b" in shown
    assert f"  watches: {c['id']}-c (idle), {lonely['id']}-l (idle); watched by: nobody" in shown

    def heard(row) -> list[str]:
        return [m["text"] for m in state.pending_messages(row["id"], 3)]

    worker_status.declare(cfg, state, c, "waiting", questions=["Which version?"])
    assert "is waiting now" in heard(a)[0] and "Its questions: Which version?" in heard(a)[0]
    assert heard(b) == heard(a)
    worker_status.declare(cfg, state, state.worker(c["id"]), "scheduled", after=timedelta(hours=1), reason="look again")
    assert "is scheduled now" in heard(a)[1] and ": look again" in heard(a)[1] and "Its next turn: 20" in heard(a)[1]
    worker_status.declare(cfg, state, state.worker(c["id"]), "background", reason="a download")
    assert len(heard(a)) == 2  # it still works: no message

    assert operations.unwatch(cfg, state, ["r1/c"], watcher="r1/b") == [f"{b['id']}-b does not watch {c['id']}-c any more"]
    assert operations.unwatch(cfg, state, ["r1/c"], watcher="r1/b") == [f"{b['id']}-b did not watch {c['id']}-c"]
    state.execute("UPDATE workers SET scheduled_at = NULL WHERE id = ?", c["id"])
    task = inbox.send(state, c, "person", "Do it.")
    for attempt in range(worker_turn.MAX_ATTEMPTS):
        assert len(heard(a)) == 2  # a failed turn that is tried again is no end
        state.start_turn(f"t{attempt}", "run1", c["id"], [], [task], 0)
        worker_turn.fail(cfg, state, state.turn(f"t{attempt}"), "it broke", 0)
    assert "is failed now" in heard(a)[2] and "Why: it broke" in heard(a)[2]
    assert len(heard(b)) == 2 and not heard(lonely)

    state.prompt_submitted(c["id"])
    state.set_status(c["id"], "background", "a download")
    for _ in range(worker_status.MAX_STOP_BLOCKS + 1):  # it reported background work, and none runs
        worker_status.on_stop(state, c["id"], {"background_tasks": []})
    assert state.worker(c["id"])["status"] == "maintenance" and "is maintenance now" in heard(a)[3]


def test_a_watch_needs_a_watcher_and_never_closes_a_loop(project):
    cfg, state, _ = project.load()
    a, b, c = (worker(cfg, state, key) for key in ("a", "b", "c"))
    with pytest.raises(ConfigError, match="name the worker that watches with --watcher"):
        operations.watch(cfg, state, ["r1/b"], env={})
    with pytest.raises(ConfigError, match=f"{a['id']}-a cannot watch itself"):
        operations.watch(cfg, state, ["r1/a"], watcher="r1/a")
    operations.watch(cfg, state, ["r1/b"], watcher="r1/a")
    operations.watch(cfg, state, ["r1/c"], watcher="r1/b")
    chain = f"{a['id']}-a watches {b['id']}-b watches {c['id']}-c already"
    with pytest.raises(ConfigError, match=f"{c['id']}-c cannot watch {a['id']}-a: {chain}"):
        operations.watch(cfg, state, ["r1/a"], watcher="r1/c")
    operations.watch(cfg, state, ["r1/c"], watcher="r1/a")  # two ways to c are no loop
    assert not state.watched(c["id"])


def test_watches_end_with_an_archived_worker_and_move_with_a_merged_one(project):
    cfg, state, _ = project.load()
    a, b, c, d = (worker(cfg, state, key) for key in ("a", "b", "c", "d"))
    for watcher, watched in (("a", "b"), ("b", "c"), ("d", "b"), ("b", "a")):
        state.add_watch(locals()[watcher]["id"], locals()[watched]["id"])  # b and a: a loop of an older version
    operations.merge(cfg, state, Tmux(cfg), ["r1/b"], "r1/d")  # d takes b's place; d watching itself is dropped
    assert [w["id"] for w in state.watched(d["id"])] == [c["id"]]
    assert [w["id"] for w in state.watchers(d["id"])] == [a["id"]]  # and not d watches a: that closes a loop
    assert not state.watchers(b["id"]) and not state.watched(b["id"])
    operations.archive(cfg, state, Tmux(cfg), "r1/d", "done")
    assert not state.watched(a["id"]) and not state.watchers(c["id"])


def test_a_database_with_report_back_gets_watches(project):
    cfg, state, _ = project.load()
    parent = worker(cfg, state, "p")
    child = workers.create(cfg, state, "r1", "c", "C", f"worker:{parent['id']}", parent["id"])
    quiet = workers.create(cfg, state, "r1", "q", "Q", f"worker:{parent['id']}", parent["id"])
    state.execute("ALTER TABLE workers ADD COLUMN report_back INTEGER DEFAULT 0")
    state.execute("UPDATE workers SET report_back = 1 WHERE id = ?", child["id"])
    state.close()
    cfg, state, _ = project.load()
    assert [w["id"] for w in state.watchers(child["id"])] == [parent["id"]] and not state.watchers(quiet["id"])
    assert "report_back" not in state.worker(child["id"]).keys()


def test_workers_see_each_other_from_inside(project, monkeypatch, capsys):
    cfg, state, _ = project.load()
    parent = worker(cfg, state, "p")
    child = workers.create(cfg, state, "r1", "c", "Child", f"worker:{parent['id']}", parent["id"])
    state.set_summary(child["id"], "Reading the changelog.")
    monkeypatch.setenv("VAHTOVIK_WORKER", parent["id"])
    assert cli.main(["worker", "list", "--tree"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{parent['id']}-p · r1 · idle · spawned by the person · P"
    assert out[2] == f"  {child['id']}-c · r1 · idle · spawned by worker {parent['id']}-p · Child"
    assert out[3] == "    Reading the changelog."
    assert cli.main(["worker", "show", "r1/c"]) == 0
    shown = capsys.readouterr().out
    assert f"spawned by worker {parent['id']}-p" in shown and f"parent: {parent['id']}-p" in shown
    assert str(workers.context_md(cfg, child)) in shown
    assert cli.main(["worker", "show", parent["id"]]) == 0
    assert f"children: {child['id']}-c (idle)" in capsys.readouterr().out
    assert cli.main(["worker", "watch", "r1/c"]) == 0  # the worker the command runs in watches
    assert f"{parent['id']}-p watches {child['id']}-c now" in capsys.readouterr().out
    assert cli.main(["worker", "watch", "r1/p", "--watcher", "r1/c"]) == 2  # a loop
    assert f"{child['id']}-c cannot watch {parent['id']}-p" in capsys.readouterr().err
    assert cli.main(["worker", "unwatch", "r1/c"]) == 0 and not state.watchers(child["id"])


def test_a_worker_before_its_first_turn_shows_its_task(project):
    cfg, state, _ = project.load()
    operations.spawn(cfg, state, "r1/research", message="Find the\ndocs of uv 0.13.")
    row = state.active_worker("r1", "research")
    assert operations.list_workers(cfg, state)[1] == "  task: Find the docs of uv 0.13."
    assert "  what it does: task: Find the docs of uv 0.13." in operations.show(cfg, state, row["id"])
    state.set_summary(row["id"], "The docs are found.")
    assert operations.list_workers(cfg, state)[1] == "  The docs are found."


def _next_turn(project, number: int, answer: dict | None = None) -> str:
    """Give the worker one more artifact and run its turn. Returns the turn's prompt."""
    give(project, artifact(f"a{number}", "g"))
    project.queue(answer or turn_answer([work_log(f"Turn {number}.")]))
    _turns(project, f"run{number}")
    return project.calls()[-1]["prompt"]


def test_a_turn_says_once_what_changed_in_the_per_session_information(project):
    first = _next_turn(project, 1)
    cfg, state, _ = project.load()
    assert "Changed since your last turn" not in first and "Read your role's memory before" not in first
    assert "Changed since your last turn" not in _next_turn(project, 2)

    rules = cfg.knowledge_dir / "shared/workers/SKILL.md"
    rules.write_text(rules.read_text() + "\nA new rule.\n")
    project.role("r1", ROLE.replace("Assess each item.", "Assess each item twice.").replace("[file, rss]",
                                                                                           "[file, web-page]"))
    skill = cfg.knowledge_dir / "skills/file/SKILL.md"
    skill.write_text(skill.read_text() + "\nA new step.\n")
    prompt = _next_turn(project, 3)
    assert "--resume" in project.calls()[-1]["args"]
    assert "Changed since your last turn:\n- " in prompt
    assert f"- The rules of every vahtovik worker changed. Read `{rules}` now." in prompt
    assert "- Your role's rules changed. Your CLAUDE.md, which Claude Code loaded again at this start" in prompt
    assert f"- The skill `file` changed: read `{skill}` again before you use it." in prompt
    assert f"- You have a new skill, `web-page`: `{cfg.knowledge_dir}/skills/web-page/SKILL.md`." in prompt
    assert "- You no longer have the skill `rss`: do not use it." in prompt
    assert "Assess each item twice." not in prompt
    claude_md = (workers.folder(cfg, state.active_worker("r1", "g")) / "CLAUDE.md").read_text()
    assert "Assess each item twice." in claude_md
    assert "Changed since your last turn" not in _next_turn(project, 4)  # told once


def test_a_turn_names_the_memory_entries_that_came_went_or_changed(project, monkeypatch):
    cfg, state, _ = project.load()
    memory.add(cfg, "r1", "First.")
    memory.add(cfg, "r1", "Second.")
    path = cfg.role("r1").memory_md
    assert f"Read your role's memory before you work: `{path}`." in _next_turn(project, 1)
    second = _next_turn(project, 2)
    assert "Read your role's memory before" not in second and "Your role's memory (`" not in second

    memory.add(cfg, "r1", "Third.")
    memory.remove(cfg, "r1", 1, "it was wrong")
    path.write_text(path.read_text().replace("Second.", "Second, corrected."))
    monkeypatch.setenv("VAHTOVIK_WORKER_NAME", workers.name(state.active_worker("r1", "g")))
    monkeypatch.setenv("VAHTOVIK_ROLE", "r1")
    memory.add(cfg, None, "What the worker saved itself.")  # entry 4: the worker knows it
    monkeypatch.delenv("VAHTOVIK_WORKER_NAME")
    monkeypatch.delenv("VAHTOVIK_ROLE")
    assert (f"- Your role's memory (`{path}`; each entry starts with `## <number> ·`): Entry 3 is new: read it. "
            "Entry 1 was removed: do not rely on it. Entry 2 was edited: read it again.") in _next_turn(project, 3)
    assert "Your role's memory (`" not in _next_turn(project, 4)

    memory.add(cfg, "r1", "Fifth.")
    memory.add(cfg, "r1", "Sixth.")
    assert "Entries 5, 6 are new: read them." in _next_turn(project, 5)
    path.write_text(path.read_text() + "\n## By hand\n\nA note without a number.\n")
    assert "It was edited by hand: read the whole file again." in _next_turn(project, 6)


def test_a_failed_turn_gets_its_notes_again(project):
    cfg, _, _ = project.load()
    memory.add(cfg, "r1", "First.")
    read = "Read your role's memory before you work"
    assert read in _next_turn(project, 1, {"actions": [work_log()]})  # the agent stops without `vahtovik turn`
    project.queue(turn_answer([work_log("Again.")]))
    _turns(project, "run1b")
    assert "--resume" in project.calls()[-1]["args"] and read in project.calls()[-1]["prompt"]

    project.role("r1", ROLE.replace("Assess each item.", "Assess each item twice."))
    changed = "- Your role's rules changed."
    assert changed in _next_turn(project, 2, {"actions": [work_log("Second.")]})
    project.queue(turn_answer([work_log("Second, again.")]))
    _turns(project, "run2b")
    assert changed in project.calls()[-1]["prompt"] and read not in project.calls()[-1]["prompt"]
    assert changed not in _next_turn(project, 3)


def test_a_new_session_of_an_existing_worker_reads_the_memory_again(project):
    cfg, state, _ = project.load()
    memory.add(cfg, "r1", "First.")
    _next_turn(project, 1)
    row = state.active_worker("r1", "g")
    assert json.loads(row["seen"])["memory:1"]
    claude = project.load()[2]
    claude.session_file(row["session_id"]).unlink()  # the session is lost
    prompt = _next_turn(project, 2)
    assert "--session-id" in project.calls()[-1]["args"]
    assert "Read your role's memory before you work" in prompt and "Changed since your last turn" not in prompt


def test_nothing_is_typed_into_a_window_whose_worker_has_changes_to_hear_of(project):
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    cfg, state, claude = project.load()
    tmux = Tmux(cfg)
    run_turns(cfg, state, claude, tmux)  # the window stays open, its agent idle, with the CLAUDE.md of its start
    row = state.active_worker("r1", "g")
    assert not workers.has_changes(cfg, row)
    project.role("r1", ROLE.replace("Assess each item.", "Assess each item twice."))
    cfg, state, claude = project.load()
    assert workers.has_changes(cfg, row)
    assert operations.tell(cfg, state, tmux, [row["id"]], "One more thing.") == [
        f"{row['id']}-g: queued: its rules, skills, memory, model or effort changed, so it gets the message in its "
        "next turn, which tells it what changed"]
    assert not worker_turn.can_start(cfg, state, tmux) and state.pending_messages(row["id"], 3)
    tmux.close(tmux.window(row["id"]).id)  # as the watchdog does after its check
    project.queue(turn_answer([work_log("Second.")]))
    run_turns(cfg, state, claude, tmux, run_id="run2")
    prompt = project.calls()[-1]["prompt"]
    assert "- Your role's rules changed." in prompt and "One more thing." in prompt


def _after(args: list[str], option: str) -> str:
    return args[args.index(option) + 1]


def test_a_worker_has_its_own_model_and_effort_and_a_change_counts_from_the_next_start(project, capsys):
    cfg, state, claude = project.load()
    row = worker(cfg, state, "g", model="sonnet", effort="low")
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    tmux = Tmux(cfg)
    run_turns(cfg, state, claude, tmux)  # the window stays open, with the model and effort of its start
    args = project.calls()[-1]["args"]
    assert (_after(args, "--model"), _after(args, "--effort")) == ("sonnet", "low")
    assert not workers.has_changes(cfg, state.worker(row["id"]))
    assert cli.main(["worker", "set", "r1/g", "--model", "opus", "--effort", "role"]) == 0
    assert f"{row['id']}-g: model opus (its own); effort xhigh" in capsys.readouterr().out
    assert workers.has_changes(cfg, state.worker(row["id"]))
    assert "its rules, skills, memory, model or effort changed" in operations.tell(
        cfg, state, tmux, [row["id"]], "One more thing.")[0]
    tmux.close(tmux.window(row["id"]).id)  # as the watchdog does after its check
    project.queue(turn_answer([work_log("Second.")]))
    run_turns(cfg, state, claude, tmux, run_id="run2")
    call = project.calls()[-1]
    assert "--resume" in call["args"]
    assert (_after(call["args"], "--model"), _after(call["args"], "--effort")) == ("opus", "xhigh")
    assert "- You work with the model `opus` and the effort `xhigh` now." in call["prompt"]
    assert "One more thing." in call["prompt"]
    row = state.worker(row["id"])
    assert not workers.has_changes(cfg, row)  # told once
    seen = json.loads(row["seen"])
    del seen["settings"]  # as an earlier vahtovik left it
    state.set_seen(row["id"], json.dumps(seen))
    assert not workers.has_changes(cfg, state.worker(row["id"]))


def test_a_database_without_the_model_and_effort_of_workers_gets_them(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state, "g")
    state.execute("ALTER TABLE workers DROP COLUMN model")
    state.execute("ALTER TABLE workers DROP COLUMN effort")
    state.close()
    cfg, state, _ = project.load()
    assert (state.worker(row["id"])["model"], state.worker(row["id"])["effort"]) == (None, None)
    operations.set_settings(cfg, state, "r1/g", effort="max")
    assert workers.settings_of(cfg, state.worker(row["id"])).effort == "max"


def test_after_a_compaction_the_agent_is_told_what_to_read_again(project, monkeypatch, capsys, caplog):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    hook = worker_status.hooks()["SessionStart"][0]
    assert hook["matcher"] == "compact" and hook["hooks"][0]["command"].endswith(" hook compact")
    monkeypatch.setenv("VAHTOVIK_WORKER", row["id"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "source": "compact"})))
    capsys.readouterr()
    assert cli.main(["hook", "compact"]) == 0
    out = capsys.readouterr().out
    assert f"Read your role's memory again now: `{cfg.role('r1').memory_md}`." in out
    assert "Before you use a skill, read its `SKILL.md` again." in out


def test_nothing_is_typed_into_a_window_that_has_just_started(project, monkeypatch):
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    cfg, state, claude = project.load()
    tmux = Tmux(cfg)
    run_turns(cfg, state, claude, tmux)  # the window stays open, its agent idle
    row = state.active_worker("r1", "g")
    monkeypatch.setattr(inbox, "STARTING", timedelta(hours=1))
    assert operations.tell(cfg, state, tmux, [row["id"]], "One more thing.") == [
        f"{row['id']}-g: queued: its window is busy or just started; vahtovik types the message in at a later run"]
    assert not worker_turn.can_start(cfg, state, tmux) and state.pending_messages(row["id"], 3)
    monkeypatch.setattr(inbox, "STARTING", timedelta(0))
    assert worker_turn.can_start(cfg, state, tmux)
    project.queue({"mode": "interactive", "actions": [{"run": ["vahtovik", "turn", "finished"]}]})
    worker_turn.run(cfg, state, claude, tmux, "run2")
    assert state.row("SELECT how FROM inbox")["how"] == "typed"


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
