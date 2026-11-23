import hashlib
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from conftest import monitor_answer

from vahtovik import cli
from vahtovik.stages import channel_input
from vahtovik.util import utc_iso


@pytest.fixture
def chat(project):
    """Channel `chat`: the file type with input, reading data/chat-in.md and writing data/chat-out.md.
    Returns a function that writes a message into the inbox and returns its id."""
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["chat"] = {"skill": "file", "input": {},
                                "env": {"FILE_CHANNEL_PATH": "data/chat-out.md", "FILE_CHANNEL_INBOX": "data/chat-in.md"}}
    path.write_text(yaml.safe_dump(data))
    inbox = project.root / "data/chat-in.md"

    def write(text: str) -> str:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        start = inbox.stat().st_size if inbox.exists() else 0
        with inbox.open("a") as handle:
            handle.write(f"{text}\n---\n")
        return f"{start}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"
    return write


def replies(*items: tuple[list[str], str], awaits_answer: bool = False) -> dict:
    return {"mode": "print", "structured_output": {"awaits_answer": awaits_answer, "replies": [
        {"to": to, "subject": "", "text": text} for to, text in items]}}


def _receive_and_answer(project) -> tuple:
    cfg, state, claude = project.load()
    channel = cfg.channels["chat"]
    received = channel_input.receive(cfg, state, channel)
    answered = channel_input.answer(cfg, state, claude, channel, "run1", project.tmp / "raw")
    return cfg, state, received, answered


def _outbox(project) -> str:
    path = project.root / "data/chat-out.md"
    return path.read_text() if path.exists() else ""


def test_the_channel_agent_answers_each_message_through_the_channel(project, chat):
    first = chat("What waits for me?")
    project.queue(replies(([first], "Nothing waits for you.")))
    cfg, state, received, answered = _receive_and_answer(project)
    assert (received, answered) == (1, 1)
    [call] = project.calls()
    assert call["cwd"] == str(cfg.root)  # the vahtovik agent, with the root CLAUDE.md
    args = call["args"]
    assert args[args.index("--model") + 1] == "opus" and args[args.index("--effort") + 1] == "xhigh"
    assert "--session-id" in args and "--no-session-persistence" not in args
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions" and "--tools" not in args  # full power
    assert "hooks" not in json.loads(args[args.index("--settings") + 1]) and "VAHTOVIK_WATCHES" not in call["env"]
    assert "What waits for me?" in call["prompt"] and "skills/vahtovik/channel/SKILL.md" in call["prompt"]
    assert "## What this channel may do\n\nEverything, as in the terminal.\n" in call["prompt"]
    assert "## Earlier messages" not in call["prompt"]  # the channel's first session has none
    assert f"reply to message {first}" in _outbox(project) and "Nothing waits for you." in _outbox(project)
    assert state.message_counts("chat") == {"answered": 1}
    assert state.row("SELECT reply_to, origin FROM deliveries")["reply_to"] == first

    second, third = chat("Merge a and b."), chat("Also c.")
    project.queue(replies(([second, third], "My plan: ... Answer yes to go on.")))
    _receive_and_answer(project)
    resumed = project.calls()[1]["args"]
    assert resumed[resumed.index("--resume") + 1] == args[args.index("--session-id") + 1]
    assert f"reply to message {second}" in _outbox(project)
    assert state.message_counts("chat") == {"answered": 3}


def test_the_person_can_clear_the_channels_session(project, chat, caplog):
    caplog.set_level(logging.INFO)
    first = chat("Clear our conversation.")
    answer = replies(([first], "Cleared: your next message starts a fresh conversation."))
    answer["structured_output"]["new_session"] = True
    project.queue(answer)
    _cfg, state, _received, answered = _receive_and_answer(project)
    assert answered == 1 and state.input_state("chat")["session_id"] is None
    assert caplog.records[-1].getMessage() == ("chat: Cleared the session at the person's request; "
                                               "the next message starts a new one")
    assert "Cleared: your next message" in _outbox(project)

    second = chat("What waits for me?")
    project.queue(replies(([second], "Nothing waits for you.")))
    _receive_and_answer(project)
    old, new = (call["args"] for call in project.calls())
    assert "--resume" not in new
    assert new[new.index("--session-id") + 1] != old[old.index("--session-id") + 1]
    assert state.input_state("chat")["session_id"] == new[new.index("--session-id") + 1]
    assert "## Earlier messages" not in project.calls()[1]["prompt"]  # the person asked to start over


def test_a_message_without_a_reply_is_tried_again_and_given_up_after_three_attempts(project, chat):
    first, second = chat("One."), chat("Two.")
    project.queue(replies(([first], "About one.")))
    _cfg, state, _, answered = _receive_and_answer(project)
    assert answered == 0 and state.message_counts("chat") == {"new": 2}
    assert _outbox(project) == ""  # no reply goes out until all messages have one
    project.queue({"mode": "print", "exit": 1}, replies(([first, "x9"], "Hm.")))
    _receive_and_answer(project)
    _receive_and_answer(project)
    assert state.message_counts("chat") == {"failed": 2}
    out = _outbox(project)
    assert out.count("vahtovik could not handle this message after 3 attempts") == 2
    assert f"reply to message {second}" in out and "reply to an unknown message: x9" in out
    _receive_and_answer(project)
    assert len(project.calls()) == 3


def test_the_agent_acknowledges_the_messages_at_once_before_its_reply(project, chat):
    first, second = chat("What waits for me?"), chat("And <b>this</b>?")
    project.queue({**replies(([first, second], "Nothing waits for you.")), "actions": [
        {"bash": "vahtovik acknowledge --text 'Let me check & see.'"},
        {"bash": "vahtovik acknowledge --text 'Again.'"},
        {"bash": f"vahtovik acknowledge --to {first} --text 'Again.'"},
        {"bash": "vahtovik acknowledge --to x9 --text 'Hm.'"},
        {"bash": "vahtovik acknowledge --text 'One line.\nTwo lines.'"},
    ]})
    _cfg, state, _received, answered = _receive_and_answer(project)
    assert answered == 2
    sent, again, again_first, unknown, two_lines = project.command_runs()
    assert sent["code"] == 0 and sent["out"].startswith(f"sent: delivery 1 to chat, in reply to message {second}")
    assert again["code"] == again_first["code"] == 2 and "have their first message already" in again["out"]
    assert f"message x9 does not wait for an answer; waiting: {first}, {second}" in unknown["out"]
    assert "give one short line" in two_lines["out"]
    out = _outbox(project)
    assert out.index("Let me check & see.") < out.index("Nothing waits for you.") and out.count("Again.") == 0
    ack = state.row("SELECT * FROM deliveries WHERE id = 1")
    assert (ack["origin"], ack["reply_to"], ack["status"]) == ("acknowledgment", second, "sent")
    assert "chat: Sent acknowledgment 1 to chat" in (project.root / "data/journal.log").read_text()
    history = channel_input.history_text(state, "chat", 0)
    assert "Let me check" not in history and "Nothing waits for you." in history  # only the replies matter later


def test_a_failed_acknowledgment_is_not_retried_and_outside_a_channel_session_it_is_refused(
        project, chat, monkeypatch, capsys, caplog):
    message = chat("Hello.")
    cfg, state, _claude = project.load()
    channel_input.receive(cfg, state, cfg.channels["chat"])
    cfg.channels["chat"].env.pop("FILE_CHANNEL_PATH")
    sent, line = channel_input.acknowledge(cfg, state, "chat", "One moment.")
    assert not sent and line.startswith("failed: delivery 1 to chat: ")
    assert line.endswith("FILE_CHANNEL_PATH is not set. vahtovik does not retry it: go on with your answer")
    assert any(m.startswith("chat: Failed acknowledgment 1 to chat (not retried): ") for m in caplog.messages)
    assert state.retryable_deliveries(5) == [] and state.undelivered() == 0
    assert cli.main(["acknowledge", "--text", "Hi."]) == 2
    assert "run this inside a channel session" in capsys.readouterr().err
    monkeypatch.setenv("VAHTOVIK_CHANNEL", "local-log")
    assert cli.main(["acknowledge", "--text", "Hi."]) == 2
    assert "'local-log' is not an input channel" in capsys.readouterr().err
    monkeypatch.setenv("VAHTOVIK_CHANNEL", "chat")
    monkeypatch.setenv("VAHTOVIK_DRY_RUN", "1")
    assert cli.main(["acknowledge", "--to", message, "--text", "Hi."]) == 2  # it had one, even if it failed
    assert "have their first message already" in capsys.readouterr().err


def test_the_acknowledgment_shows_the_sessions_context_size(project, chat):
    chat("Hello.")
    cfg, state, claude = project.load()
    channel_input.receive(cfg, state, cfg.channels["chat"])
    state.set_input_session("chat", "s1")
    folder = claude.profile() / "projects" / "here"
    folder.mkdir(parents=True)

    def step(tokens: tuple[int, int, int], **more) -> str:
        usage = dict(zip(("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"), tokens))
        return json.dumps({"type": "assistant", "message": {"usage": {**usage, "output_tokens": 50}}, **more})
    (folder / "s1.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "Hello."}}), step((3, 9000, 4000)), "{broken",
        step((5, 1600, 20400)), step((2, 500, 90000), isSidechain=True), step((0, 0, 0)), ""]))
    assert claude.context_tokens("s1") == 22005 and claude.context_tokens("s2") is None
    assert channel_input.acknowledge(cfg, state, "chat", "One moment.")[0]
    assert "One moment. (22k)" in _outbox(project)


def test_a_failed_read_keeps_the_messages_for_the_next_read(project, chat, monkeypatch):
    chat("Hello.")
    cfg, state, claude = project.load()
    cfg.channels["chat"].env.pop("FILE_CHANNEL_INBOX")
    assert channel_input.receive(cfg, state, cfg.channels["chat"]) is None
    assert "FILE_CHANNEL_INBOX is not set" in state.input_state("chat")["last_error"]
    cfg, state, claude = project.load()
    assert channel_input.receive(cfg, state, cfg.channels["chat"]) == 1


def _journal(project) -> list[str]:
    return [line[40:] for line in (project.root / "data/journal.log").read_text().splitlines()]


def test_a_run_answers_new_messages_before_it_monitors(project, chat):
    message = chat("What waits for me?")
    project.queue(replies(([message], "Nothing.")), monitor_answer(), monitor_answer())
    assert cli.main(["run"]) == 0
    lines = _journal(project)
    assert lines[:3] == ["chat: Received 1 message", "Started run: messages in chat, w1",
                         "chat: Started answering 1 message, new session " + lines[2][-8:]]
    assert lines[-1].startswith("Finished run in ") and "1 message answered" in lines[-1]
    time.sleep(1.05)  # a run's id is its start time, to the second
    cli.main(["run", "--force", "--stage", "input"])  # a read that finds nothing writes no line
    assert _journal(project)[-1] == "Nothing due: no messages to answer"


def test_check_wants_the_input_scripts_and_secrets(project, chat, capsys):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    del data["channels"]["chat"]["env"]["FILE_CHANNEL_INBOX"]
    data["channels"]["broken"]["input"] = {}
    path.write_text(yaml.safe_dump(data))
    cli.main(["check"])
    out = capsys.readouterr().out
    assert "problem: channel chat: environment variable FILE_CHANNEL_INBOX not set" in out
    assert "problem: channel broken: it has input, but" in out
    assert "channel log" not in out  # a channel without input needs no inbox


def _chat_input(project, **settings) -> None:
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["chat"]["input"].update(settings)
    path.write_text(yaml.safe_dump(data))


def test_messages_from_other_senders_are_ignored_without_a_reply(project, chat, monkeypatch, caplog):
    _chat_input(project, senders=["alice"])
    monkeypatch.setenv("USER", "mallory")  # the file channel's sender
    chat("Delete everything.")
    cfg, state, received, answered = _receive_and_answer(project)
    assert (received, answered) == (0, 0) and project.calls() == [] and _outbox(project) == ""
    assert state.message_counts("chat") == {}
    [line] = [r.getMessage() for r in caplog.records if "Ignored" in r.getMessage()]
    assert line.startswith("chat: Ignored message 0-") and line.endswith("from mallory: not one of the channel's senders")
    monkeypatch.setenv("USER", "alice")
    message = chat("What waits for me?")
    project.queue(replies(([message], "Nothing.")))
    assert _receive_and_answer(project)[2:] == (1, 1)


def test_a_limited_channel_runs_with_the_guard_and_sees_only_its_watches(project, chat):
    shutil.copytree(project.root / "watches/w1", project.root / "watches/w2")
    _chat_input(project, commands=["status", "tell"], watches=["w1"], rules="Answer in Ukrainian.")
    message = chat("How are things?")
    project.queue({**replies(([message], "All quiet.")), "actions": [
        {"bash": "vahtovik status"}, {"bash": "vahtovik status --watch w2"}, {"bash": "vahtovik run"},
        {"bash": "VAHTOVIK_WATCHES=w2 vahtovik status"}]})
    assert _receive_and_answer(project)[2:] == (1, 1)
    [call] = project.calls()
    args = call["args"]
    assert args[args.index("--permission-mode") + 1] == "dontAsk" and "bypassPermissions" not in args
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob,Bash" and "--strict-mcp-config" in args
    [hook] = json.loads(args[args.index("--settings") + 1])["hooks"]["PreToolUse"]
    assert hook["matcher"] == "*" and hook["hooks"][0]["command"].endswith("-m vahtovik hook guard chat")
    assert call["env"]["VAHTOVIK_WATCHES"] == "w1"
    prompt = call["prompt"]
    assert "This channel is limited" in prompt
    assert ("- Commands: `vahtovik status`, `vahtovik tell`, `vahtovik messages` (this channel only), "
            "`vahtovik acknowledge`.") in prompt
    assert "- Watches: w1; the commands see no other watch." in prompt
    assert "- Reading: `skills`, `README.md`, `CLAUDE.md`, " in prompt and "`watches/w1`, `data/watches/w1`; never" in prompt
    assert prompt.count("Answer in Ukrainian.") == 1 and "own rules for this channel:\n\nAnswer in Ukrainian." in prompt
    status, other, run, sneaky = project.command_runs()
    assert status["code"] == 0 and "w1" in status["out"] and "w2" not in status["out"]
    assert other["code"] != 0 and "unknown watch 'w2'; known: w1" in other["out"]
    assert "may not run `vahtovik run`" in run["denied"] and "denied" in sneaky


def test_status_shows_each_input_channels_limits(project, chat, capsys):
    _chat_input(project, commands=["status"], watches=["w1"], senders=["@alice"], rules="Be brief.\nNo jokes.")
    cli.main(["status", "-v"])
    out = capsys.readouterr().out
    assert "input channel chat: every 1m, new session after 1h quiet, last read never" in out
    assert "  limited to status; watches w1; senders @alice; rules: Be brief...." in out
    _chat_input(project, commands=None, watches=None, senders=None, rules="")
    cli.main(["status", "-v"])
    assert "  full power; all watches; any sender; no rules of its own" in capsys.readouterr().out


def test_messages_shows_the_channels_last_messages_with_their_replies(project, chat, capsys, monkeypatch):
    first, second = chat("Merge a and b."), chat("Also c.")
    project.queue(replies(([first, second], "My plan: merge a, b and c. Answer yes to go on.")))
    _cfg, state, _received, _answered = _receive_and_answer(project)
    state.execute("UPDATE channel_messages SET received_at = '2026-11-10T09:00:00+00:00'")  # a minute before
    state.execute("UPDATE deliveries SET created_at = '2026-11-10T09:00:30+00:00'")
    third = chat("Yes.")
    cfg, state, _claude = project.load()
    channel_input.receive(cfg, state, cfg.channels["chat"])
    assert cli.main(["messages", "chat", "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert "Merge a and b." not in out
    assert out.index(f"(message {second}):\nAlso c.") < out.index("vahtovik:\nMy plan: merge a, b and c.") \
        < out.index(f"(message {third}, not answered yet):\nYes.")
    monkeypatch.setenv("VAHTOVIK_CHANNEL", "chat")  # inside the channel's session
    assert cli.main(["messages", "-n", "0"]) == 0 and "Merge a and b." in capsys.readouterr().out
    assert cli.main(["messages", "local-log"]) == 2
    assert "'local-log' is not an input channel; input channels: chat" in capsys.readouterr().err


def _answered_ago(project, hours: float) -> None:
    _cfg, state, _claude = project.load()
    state.execute("UPDATE channel_inputs SET answered_at = ?",
                  utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours)))
    state.execute("UPDATE channel_messages SET received_at = ?",  # the history's order needs earlier times
                  utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours, minutes=1)))
    state.execute("UPDATE deliveries SET created_at = ?", utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours)))


def test_after_a_quiet_pause_a_new_session_starts_with_the_last_messages(project, chat, caplog):
    caplog.set_level(logging.INFO)
    first = chat("What waits for me?")
    project.queue(replies(([first], "Nothing waits for you.")))
    _receive_and_answer(project)
    _answered_ago(project, 2)
    second = chat("And now?")
    project.queue(replies(([second], "Still nothing.")))
    _cfg, state, _received, _answered = _receive_and_answer(project)
    old, new = project.calls()
    assert "--resume" not in new["args"] and new["args"][new["args"].index("--session-id") + 1] != \
        old["args"][old["args"].index("--session-id") + 1]
    assert any("new session" in r.getMessage() and ": the last answer was 2h00m ago" in r.getMessage()
               for r in caplog.records)
    history = new["prompt"].split("## Earlier messages\n\nThis session is new.", 1)[1].split("## New messages")[0]
    assert history.index(f"(message {first}):\nWhat waits for me?") < history.index("vahtovik:\nNothing waits")
    assert "`vahtovik messages --last N`" in history and "And now?" not in history
    assert state.input_state("chat")["answered_at"] > utc_iso(datetime.now(timezone.utc) - timedelta(minutes=1))


@pytest.mark.parametrize("hours, awaits, settings", [
    (5, True, {}),  # the last reply waits for the person's answer
    (0.5, False, {}),  # within the pause
    (5, False, {"new_session_after": 0}),  # one session until the person clears it
])
def test_the_session_is_resumed_while_it_waits_for_an_answer_or_the_pause_is_short(project, chat, hours, awaits,
                                                                                  settings):
    _chat_input(project, **settings)
    first = chat("Merge a and b.")
    project.queue(replies(([first], "My plan: ... Answer yes to go on."), awaits_answer=awaits))
    _receive_and_answer(project)
    assert project.load()[1].input_state("chat")["awaits_answer"] == int(awaits)
    _answered_ago(project, hours)
    second = chat("Yes.")
    project.queue(replies(([second], "Merged.")))
    _receive_and_answer(project)
    old, new = (call["args"] for call in project.calls())
    assert new[new.index("--resume") + 1] == old[old.index("--session-id") + 1]
    assert "## Earlier messages" not in project.calls()[1]["prompt"]
