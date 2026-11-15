"""Channel-input: vahtovik reads the person's messages from the channels that have `input`, and the channel agent
answers them through the same channel.

Each channel type's `receive` script lists the new messages; vahtovik stores them, then resumes the channel's one
persistent session: the vahtovik agent, in the project folder. While it works, it may tell the person at once that
their messages arrived, with `vahtovik acknowledge`. It must return one reply for every message, and
vahtovik sends the replies with the channel's `send` script."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import channel_guard, delivery, skills
from ..claude_session import Claude, SessionSpec
from ..config import SCOPE_ENV, Channel, Config, ConfigError
from ..state import State
from ..util import StateBackup, duration, human, load_schema, plural, render, run_json_script

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RECEIVE_TIMEOUT = 60
ACKNOWLEDGMENT_LIMIT = 200  # characters: the first message is one short line
CHANNEL_SKILL = Path("vahtovik") / "channel" / "SKILL.md"
GAVE_UP = ("vahtovik could not handle this message after {attempts} attempts: {error}\n"
           "See what happened: vahtovik log {channel}")


def input_channels(cfg: Config) -> list[Channel]:
    return [c for c in cfg.channels.values() if c.input]


def due(cfg: Config, state: State, now: datetime, force: bool = False) -> list[Channel]:
    """The input channels whose interval has passed since they were last read."""
    channels = []
    for channel in input_channels(cfg):
        row = state.input_state(channel.name)
        last = row["last_read_at"] if row else None
        if force or not last or datetime.fromisoformat(last) + channel.input.interval <= now:
            channels.append(channel)
    return channels


def waiting(cfg: Config, state: State) -> list[Channel]:
    """The input channels with messages that wait for an answer."""
    return [c for c in input_channels(cfg) if state.waiting_messages(c.name, MAX_ATTEMPTS)]


def receive(cfg: Config, state: State, channel: Channel) -> int | None:
    """Read the channel with its type's `receive` script and store the new messages.
    Returns how many are new, or None if reading failed. Reading that finds nothing writes no journal line."""
    try:
        skill = skills.load(cfg.skills_dir, "channels", channel.skill)
    except skills.SkillError as exc:
        return _read_failed(state, channel, str(exc))
    folder = cfg.channel_dir(channel.name) / "state"
    backup = StateBackup(folder)  # until the messages are in the database
    if backup.recover():
        log.warning("%s: Restored the channel's read state: the last run stopped before it saved the messages",
                    channel.name)
    backup.take()
    env = {**os.environ, **channel.env, "VAHTOVIK_CHANNEL": channel.name, "VAHTOVIK_STATE_DIR": str(folder)}
    data, error = run_json_script(skill.dir / "receive", env, cfg.root, RECEIVE_TIMEOUT)
    messages = data.get("messages") if data else None
    if not error and (not isinstance(messages, list) or any(
            not isinstance(m, dict) or not str(m.get("id") or "").strip() for m in messages)):
        error = "receive printed no `messages` list of objects with an `id` each"
    if error:
        backup.restore()
        return _read_failed(state, channel, error)
    new = 0
    for m in messages:
        sender = str(m.get("from") or "")
        if channel.input.senders is not None and sender not in channel.input.senders:
            log.warning("%s: Ignored message %s from %s: not one of the channel's senders", channel.name,
                        str(m["id"]).strip(), sender or "an unknown sender")  # no reply: it may be anybody
            continue
        new += state.add_message(channel.name, str(m["id"]).strip(), str(m.get("text") or ""), sender,
                                 str(m.get("time") or ""))
    state.input_read(channel.name)
    backup.drop()
    if new:
        log.info("%s: Received %s", channel.name, plural(new, "message"))
    return new


def _read_failed(state: State, channel: Channel, error: str) -> None:
    log.error("%s: Failed reading the channel: %s", channel.name, error)
    state.input_read(channel.name, error)
    return None


def _messages_text(rows) -> str:
    return "\n\n".join(f"### Message `{r['id']}` from {r['sender'] or 'unknown'}, {r['sent_at'] or r['received_at']}"
                       f"\n\n{r['text'].strip() or '(no text)'}" for r in rows)


NOTES = {"new": "not answered yet", "failed": "given up"}


def history_text(state: State, channel: str, last: int, since: str | None = None, leave_out: list[str] = ()) -> str:
    """The channel's earlier messages and vahtovik's replies, oldest first, for people and for prompts."""
    blocks = []
    for at, kind, row in state.channel_history(channel, last, since, leave_out):
        when = human(datetime.fromisoformat(at))
        if kind == "message":
            notes = ", ".join(filter(None, [f"message {row['id']}", NOTES.get(row["status"])]))
            blocks.append(f"{when} {row['sender'] or 'unknown'} ({notes}):\n{row['text'].strip() or '(no text)'}")
        else:
            note = "" if row["status"] == "sent" else " (not sent)"
            blocks.append(f"{when} vahtovik{note}:\n{row['body'].strip()}")
    return "\n\n".join(blocks)


def _limits_text(cfg: Config, channel: Channel) -> str:
    """What the channel's agent may do, for its prompt, and the person's own rules for the channel."""
    settings = channel.input
    if not settings.limited:
        text = "Everything, as in the terminal."
    else:
        where = ", ".join(f"`{p.relative_to(cfg.root) if p.is_relative_to(cfg.root) else p}`"
                          for p in channel_guard.readable(cfg, channel))
        text = "\n".join([
            "This channel is limited (see Limited channels in your skill). vahtovik's guard denies everything else.",
            "",
            f"- Commands: {channel_guard.commands_text(channel)}.",
            f"- Watches: {', '.join(settings.watches) or 'none'}; the commands see no other watch."
            if settings.watches is not None else "- Watches: all.",
            f"- Reading: {where}; never `.env` files.",
            "- Changing files, the web and other tools: not allowed."])
    if settings.rules:
        text += f"\n\nThe person's own rules for this channel:\n\n{settings.rules}"
    return text


def acknowledge(cfg: Config, state: State, name: str, text: str, to: str | None = None,
                dry_run: bool = False) -> tuple[bool, str]:
    """The channel agent's first short message, sent at once while it works on the answer: that the person's
    messages arrived, and what it does now, with the session's context size in thousands of tokens. One for the
    waiting messages, never retried: a late one is no use. Returns whether it was sent, and what to tell the
    agent."""
    channel = cfg.channels.get(name)
    if channel is None or not channel.input:
        raise ConfigError(f"{name!r} is not an input channel")
    text = text.strip()
    if not text or "\n" in text or len(text) > ACKNOWLEDGMENT_LIMIT:
        raise delivery.DeliveryError(f"give one short line, at most {ACKNOWLEDGMENT_LIMIT} characters")
    ids = [r["id"] for r in state.waiting_messages(name, MAX_ATTEMPTS)]
    if not ids:
        raise delivery.DeliveryError(f"no message of {name} waits for an answer")
    to = to or ids[-1]
    if to not in ids:
        raise delivery.DeliveryError(f"message {to} does not wait for an answer; waiting: {', '.join(ids)}")
    if state.row("SELECT 1 FROM deliveries WHERE channel = ? AND origin = ? "
                 "AND reply_to IN (SELECT value FROM json_each(?))", name, delivery.ACKNOWLEDGMENT, json.dumps(ids)):
        raise delivery.DeliveryError("these messages have their first message already: go on with your answer")
    session = state.input_state(name)
    tokens = Claude(cfg).context_tokens(session["session_id"] if session else None)
    if tokens:  # so the person sees whether a new session answers, or the old one goes on
        text += f" ({max(1, round(tokens / 1000))}k)"
    row = delivery.reply(cfg, state, name, "", text, to, origin=delivery.ACKNOWLEDGMENT, dry_run=dry_run)
    if row["status"] == "sent":
        return True, f"sent: delivery {row['id']} to {name}, in reply to message {to}"
    return False, (f"failed: delivery {row['id']} to {name}: {row['last_error']}. "
                   "vahtovik does not retry it: go on with your answer")


def _check_replies(replies: list[dict], ids: list[str]) -> str | None:
    """Every message must be in the `to` of exactly one reply. Returns what is wrong, or None."""
    given = [str(i) for r in replies for i in r["to"]]
    missing = [i for i in ids if i not in given]
    unknown = sorted({i for i in given if i not in ids})
    twice = sorted({i for i in given if given.count(i) > 1})
    problems = [f"{what}: {', '.join(found)}" for what, found in (
        ("no reply for message", missing), ("reply to an unknown message", unknown),
        ("more than one reply for message", twice)) if found]
    return "; ".join(problems) or None


def _pause(channel: Channel, row, rows) -> str:
    """Why the next answer starts a new session after a quiet pause, or "" to resume the session. Resuming is
    cheaper while Claude Code's cache still holds the conversation; after a long pause a new session is cheaper.
    The session stays while its last reply waits for the person, and while messages are tried again: it may have
    started on them."""
    after = channel.input.new_session_after
    if not after or not row["answered_at"] or row["awaits_answer"] or any(r["attempts"] for r in rows):
        return ""
    quiet = datetime.now(timezone.utc) - datetime.fromisoformat(row["answered_at"])
    return f": the last answer was {duration(quiet.total_seconds())} ago" if quiet >= after else ""


def _history_part(state: State, channel: Channel, since: str | None, ids: list[str]) -> str:
    """For a new session's prompt: the channel's last messages before the new ones, since the person last
    cleared the conversation, and how to read more."""
    if not state.channel_history(channel.name, 1, since, ids):
        return ""
    text = history_text(state, channel.name, channel.input.history, since, ids) if channel.input.history else ""
    shown = f" The last messages before the new ones, with your replies:\n\n{text}\n\n" if text else " "
    return (f"## Earlier messages\n\nThis session is new.{shown}Read more of them with "
            f"`vahtovik messages --last N` when you need them.\n\n")


def answer(cfg: Config, state: State, claude: Claude, channel: Channel, run_id: str, raw_dir: Path,
           dry_run: bool = False) -> int:
    """Let the channel agent answer the channel's waiting messages. Returns how many it answered."""
    rows = state.waiting_messages(channel.name, MAX_ATTEMPTS)
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    try:
        rules = skills.load(cfg.skills_dir, "channels", channel.skill).body.strip()
    except skills.SkillError as exc:
        rules = f"(skill missing: {exc})"
    row = state.input_state(channel.name)
    session_id = row["session_id"] if row else None
    mode, why = ("resume", _pause(channel, row, rows)) if claude.session_exists(session_id) else ("new", "")
    history = ""
    if mode == "new" or why:
        mode, session_id = "new", str(uuid.uuid4())
        state.set_input_session(channel.name, session_id)
        history = _history_part(state, channel, row["cleared_at"] if row else None, ids)
    env = {"VAHTOVIK_CHANNEL": channel.name, "VAHTOVIK_RUN": run_id}
    if channel.input.watches is not None:
        env[SCOPE_ENV] = ",".join(channel.input.watches)
    spec = SessionSpec(
        stage="input", workdir=cfg.root, settings=cfg.stage(None, "input"), schema=load_schema("channel"),
        prompt=render("channel_turn.md", channel=channel.name, run_id=run_id, now=human(),
                      channel_skill=cfg.skills_dir / CHANNEL_SKILL, format_rules=rules,
                      limits=_limits_text(cfg, channel), history=history, messages=_messages_text(rows)),
        mode=mode, session_id=session_id, name=f"vahtovik channel {channel.name}", env=env, scope=channel.name,
        guard=channel.name if channel.input.limited else None,
    )
    log.info("%s: Started answering %s, %s session %s%s", channel.name, plural(len(ids), "message"),
             "resumed" if mode == "resume" else "new", session_id[:8], why)
    result = claude.run(spec, raw_dir)
    state.log_session(run_id, "input", "", channel.name, result)
    error = (result.error or "unknown error") if not result.ok else _check_replies(result.output["replies"], ids)
    if error:
        _failed(cfg, state, channel, ids, error, result.duration_s, dry_run)
        return 0
    for item in result.output["replies"]:
        delivery.reply(cfg, state, channel.name, item.get("subject") or "", item["text"], str(item["to"][0]),
                       dry_run=dry_run)
    state.set_messages(channel.name, ids, "answered")
    state.input_answered(channel.name, bool(result.output.get("awaits_answer")))
    log.info("%s: Answered %s in %s, $%.2f", channel.name, plural(len(ids), "message"),
             duration(result.duration_s), result.cost_usd)
    if result.output.get("new_session"):
        state.clear_input_session(channel.name)
        log.info("%s: Cleared the session at the person's request; the next message starts a new one", channel.name)
    return len(ids)


def _failed(cfg: Config, state: State, channel: Channel, ids: list[str], error: str, seconds: float,
            dry_run: bool) -> None:
    """Count a failed attempt. After the last one, vahtovik replies itself and gives the messages up."""
    state.messages_failed_attempt(channel.name, ids, error)
    rows = {r["id"]: r for r in state.rows(
        "SELECT * FROM channel_messages WHERE channel = ? AND id IN (SELECT value FROM json_each(?))",
        channel.name, json.dumps(ids))}
    attempt = max(r["attempts"] for r in rows.values())
    log.error("%s: Failed answering %s after %s (attempt %d of %d%s): %s", channel.name, plural(len(ids), "message"),
              duration(seconds), attempt, MAX_ATTEMPTS, ", giving up" if attempt >= MAX_ATTEMPTS else "", error)
    done = [i for i in ids if rows[i]["attempts"] >= MAX_ATTEMPTS]
    for message_id in done:
        delivery.reply(cfg, state, channel.name, "vahtovik",
                       GAVE_UP.format(attempts=MAX_ATTEMPTS, error=error, channel=channel.name), message_id,
                       origin="notice", dry_run=dry_run)
    state.set_messages(channel.name, done, "failed")
