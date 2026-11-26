import json
import logging
import shutil
from datetime import timedelta

from conftest import artifact, give, report_action, run_turns, turn_answer, until, work_log, worker

from vahtovik import inbox, operations, watchdog, workers
from vahtovik.stages import worker_turn
from vahtovik.tmux import Tmux


def _turns(project):
    """A run's worker stage, after the watchdog's check that starts every run (it closes done windows)."""
    cfg, state, claude = project.load()
    watchdog.check(cfg, state, Tmux(cfg), claude, "run1")
    run_turns(cfg, state, claude, Tmux(cfg))
    return cfg, state


def test_first_turn_creates_the_session_and_the_agent_reports_with_its_skill(project):
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log(), report_action("Hello from the agent.")]))
    cfg, state = _turns(project)

    call = project.calls()[0]
    row = state.active_worker("r1", "g")
    assert call["cwd"] == str(cfg.workers_dir / "r1" / f"{row['id']}-g")
    assert call["args"][call["args"].index("--session-id") + 1] == row["session_id"]
    assert call["env"]["VAHTOVIK_WORKER"] == row["id"] and call["env"]["VAHTOVIK_ROLE"] == "r1"
    assert call["env"]["VAHTOVIK_TURN"]
    # the role's rules are per-session information: in CLAUDE.md, which Claude Code loads at every start
    claude_md = (cfg.workers_dir / "r1" / f"{row['id']}-g" / "CLAUDE.md").read_text()
    assert "Assess each item." in claude_md and "Report everything with the file skill." in claude_md
    assert "Assess each item." not in call["prompt"] and "## Your skills" not in call["prompt"]
    assert "## New artifacts" in call["prompt"] and "## Your task" in call["prompt"]
    assert "--effort" in call["args"] and call["args"][call["args"].index("--effort") + 1] == "xhigh"
    assert state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"] == "processed"
    outbox = (cfg.root / "data/outbox.md").read_text()
    assert "Hello from the agent." in outbox and f"_worker {row['id']}-g_" in outbox
    assert state.row("SELECT COUNT(*) AS n FROM deliveries")["n"] == 0  # vahtovik itself sent nothing
    turn = state.row("SELECT * FROM turns")
    assert turn["ok"] == 1 and json.loads(turn["output"])["status"] == "finished"
    assert row["status"] == "finished" and row["summary"] == "All good."
    assert "--settings" in call["args"] and "hook stop" in call["args"][call["args"].index("--settings") + 1]


def test_next_turn_resumes_the_same_session(project):
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    _turns(project)
    give(project, artifact("a2", "g"))
    project.queue(turn_answer([work_log("Second.")]))
    _cfg, state = _turns(project)
    second = project.calls()[1]
    assert second["args"][second["args"].index("--resume") + 1] == state.active_worker("r1", "g")["session_id"]
    assert "--append-system-prompt" not in second["args"]
    assert "a2" in second["prompt"] and "a1" not in second["prompt"].split("## New artifacts")[1].split("##")[0]


def test_lost_session_is_replaced(project):
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    _cfg, state = _turns(project)
    old = state.active_worker("r1", "g")["session_id"]
    shutil.rmtree(project.tmp / "claude-home/projects")
    give(project, artifact("a2", "g"))
    project.queue(turn_answer([work_log()]))
    _cfg, state = _turns(project)
    call = project.calls()[1]
    assert "--session-id" in call["args"] and "new session for an existing worker" in call["prompt"]
    assert state.active_worker("r1", "g")["session_id"] not in (None, old)


def test_locked_worker_is_skipped(project):
    cfg, state = give(project, artifact("a1", "g"))
    with workers.lock(workers.folder(cfg, state.active_worker("r1", "g"))):
        _turns(project)
    assert project.calls() == []
    assert state.row("SELECT status FROM artifacts WHERE id = 'a1'")["status"] == "new"


def test_turn_without_status_is_retried_then_given_up(project):
    give(project, artifact("a1", "g"))
    for attempt in range(1, 4):
        project.queue({"actions": [work_log()]})
        _cfg, state = _turns(project)
        row = state.row("SELECT status, attempts FROM artifacts WHERE id = 'a1'")
        assert row["attempts"] == attempt
    assert row["status"] == "failed"
    _turns(project)
    assert len(project.calls()) == 3  # no fourth attempt
    error = state.row("SELECT error FROM turns ORDER BY started_at DESC, rowid DESC LIMIT 1")["error"]
    assert error == "the agent stopped 4 times without running `vahtovik turn`"
    assert state.active_worker("r1", "g")["status"] == "failed"


def test_run_starts_the_turn_and_does_not_wait_for_it(project):
    cfg, state = give(project, artifact("a1", "g"))
    project.queue({"mode": "interactive", "hang": True})
    _cfg, _state, claude = project.load()
    turn_id, = worker_turn.run(cfg, state, claude, Tmux(cfg), "run1")
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session works on, after the run went on
    assert state.turn(turn_id)["finished_at"] is None and state.active_worker("r1", "g")["status"] == "running"


def test_artifacts_a_worker_adds_are_its_own_or_wait_for_another_worker(project):
    cfg, state = give(project, artifact("a1", "g"))
    other = worker(cfg, state, "h")
    found = json.dumps({"id": "extra", "title": "Found by agent", "summary": "Extra."})
    far = json.dumps({"id": "far", "title": "Belongs elsewhere", "summary": "Other."})
    project.queue(turn_answer([work_log(), {"run": ["vahtovik", "artifact", "add", "--json", found]},
                               {"run": ["vahtovik", "artifact", "add", "--worker", "r1/h", "--json", far]}]),
                  )
    _cfg, state = _turns(project)
    row = state.active_worker("r1", "g")
    assert state.row("SELECT * FROM artifacts WHERE id = 'extra'")["worker"] == row["id"]
    assert state.row("SELECT status FROM artifacts WHERE id = 'extra'")["status"] == "processed"
    assert state.row("SELECT * FROM artifacts WHERE id = 'far'")["worker"] == other["id"]
    assert (workers.folder(cfg, other) / "artifacts").is_dir()
    assert any(r["out"].startswith("added: far in") for r in project.command_runs())


def test_parallel_turns_are_limited_and_the_rest_start_at_a_later_run(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    project.config(lambda data: data["claude"].update(parallel_turns=2))
    cfg, state = give(project, artifact("a1", "g1"), artifact("a2", "g2"), artifact("a3", "g3"))
    project.queue(*[turn_answer([work_log()]) for _ in range(3)])
    cfg, state, claude = project.load()
    assert len(worker_turn.run(cfg, state, claude, Tmux(cfg), "run1")) == 2
    assert ("Held back 1 worker turn for a later run: already as many work as claude.parallel_turns (2) "
            "allows") in caplog.messages
    until(lambda: state.working_turns() == 0)
    left, = state.pending_workers(worker_turn.MAX_ATTEMPTS)
    assert len(worker_turn.run(cfg, state, claude, Tmux(cfg), "run2")) == 1
    until(lambda: state.worker(left)["status"] == "finished")


def test_a_message_starts_a_turn_with_its_sender(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    assert operations.tell(cfg, state, Tmux(cfg), ["r1/g"], "Look at the new docs.") == [
        f"{row['id']}-g: queued: it gets the message in a turn at the next run"]
    project.queue(turn_answer([work_log()]))
    _cfg, state = _turns(project)
    prompt = project.calls()[0]["prompt"]
    assert "Message from the person, sent through vahtovik:" in prompt and "Look at the new docs." in prompt
    message = state.row("SELECT * FROM inbox")
    assert message["status"] == "delivered" and message["how"] == "turn" and message["turn_id"]
    assert state.worker(row["id"])["status"] == "finished"


def test_a_message_is_typed_into_an_open_idle_window(project):
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]), turn_answer([work_log("Second.")]))
    cfg, state, claude = project.load()
    run_turns(cfg, state, claude, Tmux(cfg))  # the window stays open: the watchdog closes it later
    row = state.active_worker("r1", "g")
    lines = operations.tell(cfg, state, Tmux(cfg), [row["id"]], "One more thing.")
    assert lines == [f"{row['id']}-g: typed the message into its open window"]
    until(lambda: any("One more thing." in t.get("line", "") for t in project._lines(project.log_file.with_name(
        "calls.jsonl.typed"))))
    assert state.row("SELECT how FROM inbox")["how"] == "typed"


def test_messages_to_a_busy_worker_wait_and_arrive_together_in_order(project, monkeypatch):
    monkeypatch.setattr(inbox, "STARTING", timedelta(0))
    cfg, state = give(project, artifact("a1", "g"))
    a, b = worker(cfg, state, "a"), worker(cfg, state, "b")
    project.queue({"mode": "interactive", "hang": True})
    _cfg, _state, claude = project.load()
    tmux = Tmux(cfg)
    worker_turn.run(cfg, state, claude, tmux, "run1")
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the agent works on its turn
    row = state.active_worker("r1", "g")
    for sender, text in ((a, "First."), (b, "Second.")):
        assert operations.tell(cfg, state, tmux, ["r1/g"], text, env={"VAHTOVIK_WORKER": sender["id"]}) == [
            f"{row['id']}-g: queued: its window is busy or just started; vahtovik types the message in at a later run"]
    assert worker_turn.run(cfg, state, claude, tmux, "run2") == []
    assert len(state.pending_messages(row["id"], 3)) == 2 and not worker_turn.can_start(cfg, state, tmux)

    state.agent_stopped(row["id"])  # the agent stopped working: the worker is available
    _messages, typed = inbox.typed_text(cfg, state, row, tmux.window(row["id"]))
    places = [typed.index(part) for part in (f"Message from worker {a['id']}-a, sent", "First.",
                                             f"Message from worker {b['id']}-b, sent", "Second.")]
    assert places == sorted(places)
    worker_turn.run(cfg, state, claude, tmux, "run3")
    assert [m["how"] for m in state.rows("SELECT how FROM inbox ORDER BY id")] == ["typed", "typed"]


def test_a_failed_turn_gives_its_messages_back(project):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    inbox.send(state, row, "person", "Do it.")
    project.queue({"actions": [work_log()], "exit": 3})
    _cfg, state = _turns(project)
    message = state.row("SELECT * FROM inbox")
    assert message["status"] == "new" and message["attempts"] == 1
    project.queue(turn_answer([work_log()]))
    _turns(project)
    assert "Do it." in project.calls()[1]["prompt"]


def test_a_long_prompt_goes_to_a_file_and_a_long_message_is_not_typed(project, monkeypatch):
    cfg, state, _ = project.load()
    row = worker(cfg, state)
    long_text = "Check this. " * 6000  # over 60 kilobytes
    inbox.send(state, row, "person", long_text)
    project.queue(turn_answer([work_log()]))
    cfg, state = _turns(project)
    prompt = project.calls()[0]["prompt"]
    path = workers.folder(cfg, row) / "turn-prompt.md"
    assert prompt.startswith(f"The prompt of this turn is long, so vahtovik wrote it to the file `{path}`.")
    assert path.read_text().startswith("# Turn") and "Check this. Check this." in path.read_text()
    assert state.worker(row["id"])["status"] == "finished"

    tmux = Tmux(cfg)  # the window is still open, its agent idle
    assert "queued" in operations.tell(cfg, state, tmux, [row["id"]], "Again. " * 1000)[0]
    assert not worker_turn.can_start(cfg, state, tmux) and len(state.pending_messages(row["id"], 3)) == 1
