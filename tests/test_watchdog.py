import json
import logging
import time

import pytest
import yaml
from conftest import artifact, launch_turn, status_action, turn_answer, until, watchdog_answer, work_log

from vahtovik import artifacts, delivery, groups, skills, watchdog
from vahtovik.stages import group_turn
from vahtovik.tmux import Tmux, Window

TRUST_SCREEN = ("Quick safety check: Is this a project you created or one you trust?\n"
                "> No, exit\n  Yes, I trust this folder\nEnter to confirm")
REAL_CHECK = watchdog.check


@pytest.fixture(autouse=True)
def real_check(approving_watchdog, monkeypatch):
    """These tests use the real check, with fake AI answers."""
    monkeypatch.setattr(watchdog, "check", REAL_CHECK)


def _setup(project, **settings):
    config = project.root / "vahtovik.yaml"
    data = yaml.safe_load(config.read_text())
    data["watchdog"] = settings
    config.write_text(yaml.safe_dump(data))
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"] = ["log"]
    path.write_text(yaml.safe_dump(data))
    cfg, state, claude = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    return cfg, state, claude, Tmux(cfg)


def _watchdog_calls(project):
    return [c for c in project.calls() if "-p" in c["args"]]


def _answer(action: str, reason: str = "Seen.", **extra) -> dict:
    return watchdog_answer({"group": "w1/g", "action": action, "reason": reason, **extra})


def _notices(state, prefix: str = "WATCHDOG: "):
    return [r for r in state.rows("SELECT * FROM deliveries WHERE origin = 'notice'") if r["subject"].startswith(prefix)]


def _meta(cfg) -> dict:
    return skills.watchdog_skill(cfg.skills_dir).meta


def _done_turn(project, cfg, state, claude, tmux):
    """A turn that ended `finished`; its window stays open until a check."""
    project.queue(turn_answer([work_log()]))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)


def _typed(project) -> list[dict]:
    path = project.log_file.with_name(project.log_file.name + ".typed")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_watchdog_presses_the_keys_of_a_known_screen(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    project.queue({"screen": TRUST_SCREEN, "actions": [work_log(), status_action()]},
                  _answer("press", "It asks to trust the folder.", screen_id="trust-folder"))
    window_id = launch_turn(cfg, state, claude, tmux)
    until(lambda: "Yes, I trust this folder" in tmux.capture(window_id))
    watchdog.check(cfg, state, tmux, claude, "run1")
    until(lambda: state.turn("t1")["finished_at"] is not None)
    assert state.turn("t1")["ok"] == 1
    assert _typed(project) == [{"keys": "\x1b[B\n"}]  # Down, then Enter
    call, = _watchdog_calls(project)
    assert "### `w1/g`: G" in call["prompt"] and "Yes, I trust this folder" in call["prompt"]
    assert "- Status: `running` since" in call["prompt"] and "- Agent idle (it stopped" in call["prompt"]
    assert "- `trust-folder`: Claude Code asks whether you trust" in call["prompt"]
    assert "--json-schema" in call["args"] and "# vahtovik watchdog" in call["args"][call["args"].index("--append-system-prompt") + 1]
    assert "w1/g: Watchdog pressed Down Enter on the screen trust-folder: It asks to trust the folder." in caplog.messages
    notice, = _notices(state)
    assert notice["subject"] == "WATCHDOG: Pressed keys: G"
    assert notice["body"].startswith("w1/g: vahtovik pressed Down Enter on the screen trust-folder. "
                                     "The watchdog saw: It asks to trust the folder.")


def test_stuck_turn_pauses_for_a_person_and_the_channels_hear_of_it(project):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, _answer("maintenance", "Claude Code asks to log in."))
    group_turn.run(cfg, state, claude, tmux, cfg.watch("w1"), "run1")
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.tick(cfg, state, tmux, claude, "run2")  # the next run's start
    group = state.group("w1", "g")
    assert (group["status"], group["status_reason"]) == ("maintenance", "Claude Code asks to log in.")
    assert tmux.window("w1/g") is not None and state.unfinished_turns()[0]["id"]
    notice, = _notices(state)  # one notice: the AI's pick sends the usual one at once
    assert notice["subject"] == "WATCHDOG: Needs attention: G"
    assert notice["body"] == "w1/g is stuck: Claude Code asks to log in.\n\nOpen its session: vahtovik open w1/g"
    until(lambda: len(project.calls()) == 2)  # the window's session and the watchdog
    group_turn.run(cfg, state, claude, tmux, cfg.watch("w1"), "run2")
    assert len(project.calls()) == 2  # the window still holds the group: no new session
    watchdog.tick(cfg, state, tmux, claude, "run3")
    assert tmux.window("w1/g") is not None  # a stuck window waits for the person, and is not checked again
    assert len(_watchdog_calls(project)) == 1 and len(_notices(state)) == 1  # not again before notify_every


def test_screen_that_is_not_on_the_list_needs_a_person(project):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, _answer("press", "It asks to confirm.", screen_id="rm-all"))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.check(cfg, state, tmux, claude, "run1")
    group = state.group("w1", "g")
    assert group["status"] == "maintenance"
    assert group["status_reason"] == "a screen that is not on the known list: It asks to confirm."
    notice, = _notices(state)
    assert notice["subject"] == "WATCHDOG: Needs attention: G"
    assert notice["body"].startswith("w1/g is stuck: a screen that is not on the known list: It asks to confirm.")


def test_session_that_cannot_be_resumed_needs_a_person(project):
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    tmux.close(tmux.window("w1/g").id)
    until(lambda: not _locked(cfg))
    state.prompt_submitted("w1", "g")
    claude.session_file(state.group("w1", "g")["session_id"]).unlink()
    project.queue(_answer("resume", "It was killed mid-task."))
    watchdog.check(cfg, state, tmux, claude, "run1")
    group = state.group("w1", "g")
    assert (group["status"], group["status_reason"]) == ("maintenance", "the session to resume is gone: It was killed mid-task.")
    assert [n["subject"] for n in _notices(state)] == ["WATCHDOG: Needs attention: G"]


def test_maintenance_notice_that_breaks_goes_out_at_the_next_tick(project, monkeypatch):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, _answer("maintenance", "Claude Code asks to log in."))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))
    real = delivery.report

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(delivery, "report", broken)
    with pytest.raises(OSError):
        watchdog.check(cfg, state, tmux, claude, "run1")
    assert state.group("w1", "g")["status"] == "maintenance" and _notices(state) == []
    monkeypatch.setattr(delivery, "report", real)
    watchdog.tick(cfg, state, tmux, claude, "run1")
    assert [n["subject"] for n in _notices(state)] == ["WATCHDOG: Needs attention: G"]


def test_broken_session_fails_the_turn_and_its_window_closes(project):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, watchdog_answer(
        {"group": "w1/g", "action": "failed", "reason": "The conversation is too long."},
        {"group": "w9/elsewhere", "action": "maintenance", "reason": "Not ours."}))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.tick(cfg, state, tmux, claude, "run1")
    watchdog.tick(cfg, state, tmux, claude, "run1")
    assert tmux.window("w1/g") is None
    turn = state.turn("t1")
    assert turn["ok"] == 0 and turn["error"] == "The conversation is too long."
    assert state.row("SELECT attempts FROM artifacts WHERE id = 'a1'")["attempts"] == 1


def test_done_window_closes_only_after_the_check_says_none(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    project.queue({"mode": "print", "exit": 1, "stderr": "no network"})
    watchdog.tick(cfg, state, tmux, claude, "run1")
    assert tmux.window("w1/g") is not None  # no check, no close
    assert state.group("w1", "g")["checked_at"] is None  # a failed check does not count
    time.sleep(1.1)  # times are kept to the second: the next check comes in a later second than any change
    project.queue(_answer("none", "It reported and stopped."))
    watchdog.tick(cfg, state, tmux, claude, "run1")
    assert tmux.window("w1/g") is None
    assert "w1/g: Closed the session window: status finished, checked by the watchdog" in caplog.messages
    assert _notices(state) == []
    with open(claude.session_file(state.group("w1", "g")["session_id"]), "a") as conversation:
        conversation.write('{"type": "cost-state"}\n')  # Claude Code writes to it as its window ends
    watchdog.tick(cfg, state, tmux, claude, "run1")
    assert len(_watchdog_calls(project)) == 2  # the close itself is no change to check


@pytest.mark.parametrize("status", ["finished", "waiting"])
def test_done_session_whose_window_ended_before_a_check_is_checked_once(project, caplog, status):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    if status == "waiting":
        state.set_status("w1", "g", "waiting", None, ["Which build?"])
    tmux.close(tmux.window("w1/g").id)  # a reboot, or a person, ended it
    until(lambda: not _locked(cfg))
    time.sleep(1.1)  # times are kept to the second: the check comes in a later second than the agent's stop
    project.queue(_answer("none", "It reported and stopped."))
    watchdog.check(cfg, state, tmux, claude, "run1")
    call, = _watchdog_calls(project)
    assert f"- Status: `{status}` since" in call["prompt"]
    assert "- Window: none" in call["prompt"] and "The screen:" not in call["prompt"]
    assert "w1/g: Watchdog found it right: It reported and stopped." in caplog.messages
    watchdog.check(cfg, state, tmux, claude, "run1")
    assert len(_watchdog_calls(project)) == 1 and tmux.window("w1/g") is None


def test_finished_session_with_unfinished_work_and_no_window_is_resumed(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    tmux.close(tmux.window("w1/g").id)
    until(lambda: not _locked(cfg))
    project.queue(_answer("resume", "It said finished while its build still ran."),
                  {"mode": "interactive", "actions": [status_action("finished", None, None)]})
    watchdog.check(cfg, state, tmux, claude, "run1")
    until(lambda: len(project.hook_answers("UserPromptSubmit")) == 2)
    until(lambda: state.group("w1", "g")["status"] == "finished" and state.group("w1", "g")["idle"] == 1)
    call = project.calls()[-1]
    assert "--resume" in call["args"] and call["prompt"] == " ".join(_meta(cfg)["resume"].split())
    assert [n["subject"] for n in _notices(state)] == ["WATCHDOG: Resumed: G"]
    assert state.turn("t1")["ok"] == 1


def test_working_session_is_checked_every_time_and_stays_open(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, _answer("none", "A command runs."),
                  _answer("none", "Still runs."))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))
    watchdog.check(cfg, state, tmux, claude, "run1")
    watchdog.check(cfg, state, tmux, claude, "run1")
    assert len(_watchdog_calls(project)) == 2 and tmux.window("w1/g") is not None
    assert "w1/g: Watchdog found it right: Still runs." in caplog.messages


def test_no_check_when_nothing_needs_one(project, monkeypatch):
    cfg, state, claude, tmux = _setup(project)
    watchdog.check(cfg, state, tmux, claude, "run1")  # no window, status idle
    project.queue({"mode": "interactive", "hang": True})
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))
    real = Tmux.windows
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window(w.id, w.ref, 1) for w in real(self)])
    watchdog.check(cfg, state, tmux, claude, "run1")  # a person looks at it
    monkeypatch.setattr(Tmux, "windows", real)
    state.set_status("w1", "g", "maintenance", "Needs a login.")
    watchdog.check(cfg, state, tmux, claude, "run1")  # it waits for a person
    assert _watchdog_calls(project) == []


def test_nudge_types_the_fixed_message_to_an_idle_agent(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    project.queue(_answer("nudge", "It said finished, but its build never ended."),
                  {"mode": "interactive", "actions": [status_action("finished", None, None)]})
    watchdog.check(cfg, state, tmux, claude, "run1")
    nudge = " ".join(_meta(cfg)["nudge"].split())
    until(lambda: any(t.get("line") == nudge for t in _typed(project)))
    until(lambda: len(project.hook_answers("UserPromptSubmit")) == 2)  # the nudge counts as a prompt
    assert "w1/g: Watchdog nudged the agent: It said finished, but its build never ended." in caplog.messages
    notice, = _notices(state)
    assert notice["subject"] == "WATCHDOG: Nudged: G"


def test_nudge_and_press_never_reach_a_working_agent(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, _answer("nudge", "Looks done."))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))
    watchdog.check(cfg, state, tmux, claude, "run1")
    assert not any(t.get("line") for t in _typed(project))
    assert any("Watchdog did not nudge: the agent is not idle" in m for m in caplog.messages)
    assert state.group("w1", "g")["status"] == "running" and _notices(state) == []


def test_resume_opens_an_interrupted_session_with_the_fixed_message(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    tmux.close(tmux.window("w1/g").id)
    until(lambda: not _locked(cfg))
    state.prompt_submitted("w1", "g")  # a person's message, then the window died
    project.queue(_answer("resume", "It was killed while it wrote the report."),
                  {"mode": "interactive", "actions": [status_action("finished", None, None)]})
    watchdog.check(cfg, state, tmux, claude, "run1")
    until(lambda: state.group("w1", "g")["status"] == "finished")
    call = project.calls()[-1]
    assert "--resume" in call["args"] and call["prompt"] == " ".join(_meta(cfg)["resume"].split())
    assert state.window_record("w1", "g")["purpose"] == "resume"
    assert any(m.startswith("w1/g: Watchdog resumed the session in tmux window @") for m in caplog.messages)
    assert [n["subject"] for n in _notices(state)] == ["WATCHDOG: Resumed: G"]


def test_resume_and_retry_never_start_a_second_agent(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, _answer("resume", "Gone."), _answer("retry", "Lost."))
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))
    watchdog.check(cfg, state, tmux, claude, "run1")
    watchdog.check(cfg, state, tmux, claude, "run1")
    assert len(project.calls()) == 3  # the turn's session and two checks: nothing was started
    assert sum("the session window is open or the group is busy" in m for m in caplog.messages) == 2
    assert state.turn("t1")["finished_at"] is None


def test_retry_repeats_a_turn_whose_work_was_lost(project):
    cfg, state, claude, tmux = _setup(project)
    _done_turn(project, cfg, state, claude, tmux)
    tmux.close(tmux.window("w1/g").id)
    until(lambda: not _locked(cfg))
    state.prompt_submitted("w1", "g")
    project.queue(_answer("retry", "Its build was killed with the window."))
    watchdog.check(cfg, state, tmux, claude, "run1")
    turn = state.turn("t1")
    assert (turn["ok"], turn["error"]) == (0, "the watchdog asked to repeat the turn: Its build was killed with the window.")
    row = state.row("SELECT status, attempts FROM artifacts WHERE id = 'a1'")
    assert (row["status"], row["attempts"]) == ("new", 1)
    assert state.group("w1", "g")["status"] == "failed"
    assert state.pending_groups("w1", group_turn.MAX_ATTEMPTS) == ["g"]
    assert [n["subject"] for n in _notices(state)] == ["WATCHDOG: Retrying: G"]


def test_failed_watchdog_session_changes_nothing(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, {"mode": "print", "exit": 1, "stderr": "no network"})
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.check(cfg, state, tmux, claude, "run1")
    assert state.group("w1", "g")["status"] == "running"
    assert any(m.startswith("Watchdog: Failed a check after") and m.endswith("no network") for m in caplog.messages)
    cost = state.row("SELECT stage, ok FROM sessions WHERE stage = 'watchdog'")
    assert (cost["stage"], cost["ok"]) == ("watchdog", 0)


@pytest.fixture
def kyiv_summer_time(monkeypatch):
    monkeypatch.setenv("TZ", "Etc/GMT-3")  # three hours ahead of UTC
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_conversation_tail_shows_messages_commands_and_stops(tmp_path, kyiv_summer_time):
    path = tmp_path / "s.jsonl"
    lines = [
        {"type": "mode", "mode": "x"},
        {"type": "user", "timestamp": "2026-10-21T14:43:11Z", "message": {"content": "Process the artifacts."}},
        {"type": "assistant", "timestamp": "2026-10-21T14:43:14Z", "message": {"content": [
            {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "Starting the build."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "make", "run_in_background": True}}]}},
        {"type": "user", "timestamp": "2026-10-21T14:43:15Z", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "Command running in background"}]}]}},
        {"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-21T14:43:19Z"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    assert watchdog.conversation_tail(path) == (  # in local time, like the other times in the prompt
        "17:43:11 user: Process the artifacts.\n"
        '17:43:14 assistant: Starting the build. [runs Bash] {"command": "make", "run_in_background": true}\n'
        "17:43:15 user: [result] Command running in background\n"
        "17:43:19 (the agent stopped)")
    assert watchdog.conversation_tail(None) == "(no conversation file)"


def test_long_turn_is_reported_once(project, caplog):
    cfg, state, claude, tmux = _setup(project, long_turn_after="3h")
    project.queue({"mode": "interactive", "hang": True})
    launch_turn(cfg, state, claude, tmux)
    watchdog.warn_long_turns(cfg, state, tmux)
    state.execute("UPDATE group_turns SET started_at = datetime('now', '-4 hours') || '+00:00'")
    watchdog.warn_long_turns(cfg, state, tmux)
    watchdog.warn_long_turns(cfg, state, tmux)
    notice, = state.rows("SELECT * FROM deliveries WHERE origin = 'notice'")
    assert notice["subject"] == "WATCHDOG: Long turn: G" and notice["body"].startswith("w1/g has been working on one turn for 4h00m")
    assert "w1/g: Long group turn: running for 4h00m" in caplog.messages


def test_given_up_report_is_told_through_the_other_channels_once(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"] = ["broken", "log"]
    path.write_text(yaml.safe_dump(data))
    cfg, state, claude = project.load()
    delivery.report(cfg, state, "w1", "g", "broken", "Weekly digest", "<b>All</b> is fine.", "automated")
    for _ in range(delivery.MAX_ATTEMPTS - 1):
        delivery.retry_failed(cfg, state, False)
    watchdog.tick(cfg, state, tmux, claude, "run1")
    watchdog.tick(cfg, state, tmux, claude, "run1")
    notice, = state.rows("SELECT * FROM deliveries WHERE origin = 'notice'")
    assert (notice["channel"], notice["status"]) == ("log", "sent")
    assert notice["subject"] == "WATCHDOG: Report not delivered: Weekly digest"
    assert notice["body"].startswith("vahtovik could not send report 1 of w1/g to broken and stopped retrying "
                                     "after 5 attempts. Last error: boom")
    assert notice["body"].endswith('The report, "Weekly digest":\n<b>All</b> is fine.')
    assert "w1/g: Gave up report 1 to broken after 5 attempts: boom" in caplog.messages


def _locked(cfg) -> bool:
    try:
        with groups.lock(cfg.group_dir("w1", "g")):
            return False
    except Exception:
        return True
