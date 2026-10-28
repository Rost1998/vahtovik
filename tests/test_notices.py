import os
import subprocess
from datetime import datetime, timedelta, timezone

import yaml
from conftest import artifact, report_action, run_turns, turn_answer, work_log

from vahtovik import artifacts, watchdog
from vahtovik.tmux import Tmux, Window
from vahtovik.util import utc_iso


def _setup(project, channels=("log",), notify_every="6h"):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"] = list(channels)
    path.write_text(yaml.safe_dump(data))
    config = project.root / "vahtovik.yaml"
    data = yaml.safe_load(config.read_text())
    data["watchdog"] = {"notify_every": notify_every}
    config.write_text(yaml.safe_dump(data))
    cfg, state, claude = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    return cfg, state, claude, Tmux(cfg)


def _turn(project, *answers):
    project.queue(*answers)
    cfg, state, claude = project.load()
    run_turns(cfg, state, claude, Tmux(cfg))


def _notices(state):
    return state.rows("SELECT * FROM deliveries WHERE origin = 'notice' ORDER BY id")


def _age(state, column, hours):
    state.execute(f"UPDATE groups SET {column} = ?", utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours)))


def test_waiting_without_a_report_gets_a_notice_with_the_questions(project):
    cfg, state, _, tmux = _setup(project)
    _turn(project, turn_answer([work_log()], status="waiting", questions=("Upgrade <now>?", "Who pays?")))
    watchdog.tick(cfg, state, tmux)
    notice, = _notices(state)
    assert notice["status"] == "sent" and notice["subject"] == "WATCHDOG: Waiting for your answer: G"
    assert notice["body"] == "w1/g waits for your answer:\n- Upgrade <now>?\n- Who pays?\n\nOpen its session: vahtovik open w1/g"
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1  # not again before notify_every


def test_the_agents_report_counts_as_the_first_notice_then_it_repeats(project):
    cfg, state, _, tmux = _setup(project)
    _turn(project, turn_answer([work_log(), report_action("log", "Upgrade now?")], status="waiting",
                               questions=("Upgrade now?",)))
    watchdog.tick(cfg, state, tmux)
    assert _notices(state) == []
    _age(state, "status_at", 7)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1
    _age(state, "notified_at", 5)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1
    _age(state, "notified_at", 7)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 2


def test_notices_do_not_repeat_when_notify_every_is_zero(project):
    cfg, state, _, tmux = _setup(project, notify_every=0)
    _turn(project, turn_answer([work_log()], status="waiting", questions=("Why?",)))
    watchdog.tick(cfg, state, tmux)
    _age(state, "notified_at", 100)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1


def test_no_notice_while_a_person_looks_at_the_window(project, monkeypatch):
    cfg, state, _, tmux = _setup(project)
    _turn(project, turn_answer([work_log()], status="waiting", questions=("Why?",)))
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window("@9", "w1/g", 1)])
    watchdog.notify(cfg, state, tmux)
    assert _notices(state) == []


def test_failed_group_gets_a_notice_only_when_retries_are_over(project):
    cfg, state, _, tmux = _setup(project)
    for attempt in range(3):
        _turn(project, {"actions": [work_log()]})
        watchdog.tick(cfg, state, tmux)
        assert len(_notices(state)) == (1 if attempt == 2 else 0)
    notice = _notices(state)[0]
    assert notice["subject"] == "WATCHDOG: Failed: G"
    assert "vahtovik stopped retrying: the agent stopped 4 times without running `vahtovik turn`" in notice["body"]


def test_maintenance_gets_a_notice_and_a_new_status_period_notifies_again(project):
    cfg, state, _, tmux = _setup(project, channels=("log", "other"))
    state.set_status("w1", "g", "maintenance", "Claude Code asks to log in again")
    watchdog.tick(cfg, state, tmux)
    notices = _notices(state)
    assert [n["channel"] for n in notices] == ["log", "other"]
    assert notices[0]["body"].startswith("w1/g is stuck: Claude Code asks to log in again")
    state.set_status("w1", "g", "maintenance", "the usage limit is reached")
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 4


def test_telegram_escapes_plain_notices(project):
    send = project.root / "skills/channels/telegram/send"
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "VAHTOVIK_DRY_RUN": "1",
           "VAHTOVIK_SUBJECT": "S"}
    plain = subprocess.run([send], input="a <b> & c", text=True, capture_output=True, env={**env, "VAHTOVIK_PLAIN": "1"})
    marked = subprocess.run([send], input="a <b>x</b>", text=True, capture_output=True, env=env)
    assert "a &lt;b&gt; &amp; c" in plain.stderr and "a <b>x</b>" in marked.stderr
