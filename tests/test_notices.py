import os
import subprocess
from datetime import datetime, timedelta, timezone

from conftest import artifact, give, run_turns, turn_answer, work_log

from vahtovik import watchdog
from vahtovik.tmux import Tmux, Window
from vahtovik.util import utc_iso


def _setup(project, notices=("log",), notify_every="6h"):
    project.config(lambda data: data.update(notices=list(notices), watchdog={"notify_every": notify_every}))
    cfg, state = give(project, artifact("a1", "g"))
    return cfg, state, Tmux(cfg), state.active_worker("r1", "g")


def _turn(project, *answers):
    project.queue(*answers)
    cfg, state, claude = project.load()
    run_turns(cfg, state, claude, Tmux(cfg))


def _notices(state):
    return state.rows("SELECT * FROM deliveries WHERE origin = 'notice' ORDER BY id")


def _age(state, column, hours):
    state.execute(f"UPDATE workers SET {column} = ?", utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours)))


def test_waiting_gets_a_notice_with_the_questions_that_repeats(project):
    cfg, state, tmux, row = _setup(project)
    name = f"{row['id']}-g"
    _turn(project, turn_answer([work_log()], status="waiting", questions=("Upgrade <now>?", "Who pays?")))
    watchdog.tick(cfg, state, tmux)
    notice, = _notices(state)
    assert notice["status"] == "sent" and notice["subject"] == "WATCHDOG: Waiting for your answer: G"
    assert notice["body"] == (f"{name} waits for your answer:\n- Upgrade <now>?\n- Who pays?\n\n"
                              f"Open its session: vahtovik open {name}")
    assert notice["about"] == row["id"] and "WATCHDOG: Waiting for your answer: G" in (
        cfg.root / "data/outbox.md").read_text()
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1  # not again before notify_every
    _age(state, "notified_at", 5)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1
    _age(state, "notified_at", 7)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 2


def test_notices_do_not_repeat_when_notify_every_is_zero(project):
    cfg, state, tmux, _row = _setup(project, notify_every=0)
    _turn(project, turn_answer([work_log()], status="waiting", questions=("Why?",)))
    watchdog.tick(cfg, state, tmux)
    _age(state, "notified_at", 100)
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 1


def test_no_notice_while_a_person_looks_at_the_window(project, monkeypatch):
    cfg, state, tmux, row = _setup(project)
    _turn(project, turn_answer([work_log()], status="waiting", questions=("Why?",)))
    monkeypatch.setattr(Tmux, "windows", lambda self: [Window("@9", row["id"], 1)])
    watchdog.notify(cfg, state, tmux)
    assert _notices(state) == []


def test_failed_worker_gets_a_notice_only_when_retries_are_over(project):
    cfg, state, tmux, _row = _setup(project)
    for attempt in range(3):
        _turn(project, {"actions": [work_log()]})
        watchdog.tick(cfg, state, tmux)
        assert len(_notices(state)) == (1 if attempt == 2 else 0)
    notice = _notices(state)[0]
    assert notice["subject"] == "WATCHDOG: Failed: G"
    assert "vahtovik stopped retrying: the agent stopped 4 times without running `vahtovik turn`" in notice["body"]


def test_maintenance_gets_a_notice_and_a_new_status_period_notifies_again(project):
    cfg, state, tmux, row = _setup(project, notices=("log", "other"))
    state.set_status(row["id"], "maintenance", "Claude Code asks to log in again")
    watchdog.tick(cfg, state, tmux)
    notices = _notices(state)
    assert [n["channel"] for n in notices] == ["log", "other"]
    assert notices[0]["body"].startswith(f"{row['id']}-g is stuck: Claude Code asks to log in again")
    state.set_status(row["id"], "maintenance", "the usage limit is reached")
    watchdog.tick(cfg, state, tmux)
    assert len(_notices(state)) == 4


def test_without_notice_channels_the_journal_says_so(project, caplog):
    cfg, state, tmux, row = _setup(project, notices=())
    state.set_status(row["id"], "maintenance", "Stuck.")
    watchdog.tick(cfg, state, tmux)
    assert _notices(state) == []
    assert any("Skipped notice \"WATCHDOG: Needs attention: G\": no channel" in m for m in caplog.messages)


def test_telegram_escapes_plain_notices(project):
    send = project.root / "knowledge/skills/telegram/send"
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "VAHTOVIK_DRY_RUN": "1",
           "VAHTOVIK_SUBJECT": "S"}
    plain = subprocess.run([send], input="a <b> & c", text=True, capture_output=True, env={**env, "VAHTOVIK_PLAIN": "1"})
    marked = subprocess.run([send], input="a <b>x</b>", text=True, capture_output=True, env=env)
    assert "a &lt;b&gt; &amp; c" in plain.stderr and "a <b>x</b>" in marked.stderr
