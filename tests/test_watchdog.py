import json
import logging

import yaml
from conftest import artifact, launch_turn, status_action, until, watchdog_answer, work_log

from vahtovik import artifacts, delivery, watchdog
from vahtovik.stages import group_turn
from vahtovik.tmux import Tmux, Window

TRUST_SCREEN = ("Quick safety check: Is this a project you created or one you trust?\n"
                "> No, exit\n  Yes, I trust this folder\nEnter to confirm")


def _setup(project, **settings):
    config = project.root / "vahtovik.yaml"
    data = yaml.safe_load(config.read_text())
    data["watchdog"] = {"stale_after": 0, "every": 0, **settings}
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


def test_watchdog_presses_the_keys_of_a_known_screen(project, caplog):
    caplog.set_level(logging.INFO, logger="vahtovik")
    cfg, state, claude, tmux = _setup(project)
    project.queue({"screen": TRUST_SCREEN, "actions": [work_log(), status_action()]},
                  watchdog_answer({"group": "w1/g", "action": "press", "screen_id": "trust-folder",
                                   "reason": "It asks to trust the folder."}))
    window_id = launch_turn(cfg, state, claude, tmux)
    until(lambda: "Yes, I trust this folder" in tmux.capture(window_id))
    watchdog.look(cfg, state, tmux, claude, "run1")
    until(lambda: state.turn("t1")["finished_at"] is not None)
    assert state.turn("t1")["ok"] == 1
    typed = [json.loads(line) for line in open(f"{project.log_file}.typed")]
    assert typed == [{"keys": "\x1b[B\n"}]  # Down, then Enter
    call, = _watchdog_calls(project)
    assert "### `w1/g`" in call["prompt"] and "Yes, I trust this folder" in call["prompt"]
    assert "- `trust-folder`: Claude Code asks whether you trust" in call["prompt"]
    assert "--json-schema" in call["args"] and "# vahtovik watchdog" in call["args"][call["args"].index("--append-system-prompt") + 1]
    assert "w1/g: Watchdog pressed Down Enter on the screen trust-folder: It asks to trust the folder." in caplog.messages


def test_stuck_turn_pauses_for_a_person_and_the_channels_hear_of_it(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True},
                  watchdog_answer({"group": "w1/g", "action": "maintenance", "reason": "Claude Code asks to log in."}))
    group_turn.run(cfg, state, claude, tmux, cfg.watch("w1"), "run1", watchdog.Ticker(cfg, state, tmux, claude, "run1"))
    group = state.group("w1", "g")
    assert (group["status"], group["status_reason"]) == ("maintenance", "Claude Code asks to log in.")
    assert any(m.startswith("w1/g: Paused group turn after") and m.endswith(
        "Claude Code asks to log in.; its window stays open for a person") for m in caplog.messages)
    assert tmux.window("w1/g") is not None and state.unfinished_turns()[0]["id"]
    notice = state.row("SELECT * FROM deliveries WHERE origin = 'notice'")
    assert notice["body"].startswith("w1/g is stuck: Claude Code asks to log in.")
    until(lambda: len(project.calls()) == 2)  # the window's session and the watchdog
    group_turn.run(cfg, state, claude, tmux, cfg.watch("w1"), "run2")
    assert len(project.calls()) == 2  # the window still holds the group: no new session
    watchdog.tick(cfg, state, tmux, claude, "run3")
    assert tmux.window("w1/g") is not None  # a stuck window waits for the person, and is not looked at again
    assert len(_watchdog_calls(project)) == 1


def test_screen_that_is_not_on_the_list_needs_a_person(project):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True},
                  watchdog_answer({"group": "w1/g", "action": "press", "screen_id": "delete-everything",
                                   "reason": "It asks to confirm."}),)
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.look(cfg, state, tmux, claude, "run1")
    group = state.group("w1", "g")
    assert group["status"] == "maintenance"
    assert group["status_reason"] == "a screen that is not on the known list: It asks to confirm."


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


def test_no_look_at_active_watched_or_finished_sessions(project, monkeypatch):
    cfg, state, claude, tmux = _setup(project, stale_after="5m")
    project.queue({"mode": "interactive", "hang": True})
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.look(cfg, state, tmux, claude, "run1")  # active a moment ago
    cfg.watchdog.stale_after = cfg.watchdog.stale_after * 0
    real = Tmux.windows
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window(w.id, w.ref, 1) for w in real(self)])
    watchdog.look(cfg, state, tmux, claude, "run1")  # a person looks at it
    monkeypatch.setattr(Tmux, "windows", real)
    state.set_status("w1", "g", "waiting")
    watchdog.look(cfg, state, tmux, claude, "run1")  # not working
    assert _watchdog_calls(project) == []


def test_failed_watchdog_session_changes_nothing(project, caplog):
    cfg, state, claude, tmux = _setup(project)
    project.queue({"mode": "interactive", "hang": True}, {"mode": "print", "exit": 1, "stderr": "no network"})
    launch_turn(cfg, state, claude, tmux)
    until(lambda: project.hook_answers("UserPromptSubmit"))  # the session started its turn
    watchdog.look(cfg, state, tmux, claude, "run1")
    assert state.group("w1", "g")["status"] == "running"
    assert any(m.startswith("Watchdog: Failed a look after") and m.endswith("no network") for m in caplog.messages)
    cost = state.row("SELECT stage, ok FROM sessions WHERE stage = 'watchdog'")
    assert (cost["stage"], cost["ok"]) == ("watchdog", 0)


def test_long_turn_is_reported_once(project, caplog):
    cfg, state, claude, tmux = _setup(project, stale_after="1h", long_turn_after="3h")
    project.queue({"mode": "interactive", "hang": True})
    launch_turn(cfg, state, claude, tmux)
    watchdog.tick(cfg, state, tmux, claude, "run1")
    state.execute("UPDATE group_turns SET started_at = datetime('now', '-4 hours') || '+00:00'")
    watchdog.tick(cfg, state, tmux, claude, "run1")
    watchdog.tick(cfg, state, tmux, claude, "run1")
    notice, = state.rows("SELECT * FROM deliveries WHERE origin = 'notice'")
    assert notice["subject"] == "Long turn: G" and notice["body"].startswith("w1/g has been working on one turn for 4h00m")
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
    assert notice["subject"] == "Report not delivered: Weekly digest"
    assert notice["body"].startswith("vahtovik could not send report 1 of w1/g to broken and stopped retrying "
                                     "after 5 attempts. Last error: boom")
    assert notice["body"].endswith('The report, "Weekly digest":\n<b>All</b> is fine.')
    assert "w1/g: Gave up report 1 to broken after 5 attempts: boom" in caplog.messages
