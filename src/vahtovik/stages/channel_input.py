"""Channel-input: vahtovik reads the person's messages from the channels that have `input`, and the channel agent
answers them through the same channel.

Each channel type's `receive` script lists the new messages; vahtovik stores them, then resumes the channel's one
persistent session: the vahtovik agent, in the project folder. It must return one reply for every message, and
vahtovik sends the replies with the channel's `send` script."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from .. import delivery, skills
from ..claude_session import Claude, SessionSpec
from ..config import Channel, Config
from ..state import State
from ..util import StateBackup, duration, human, load_schema, plural, render, run_json_script

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RECEIVE_TIMEOUT = 60
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
    mode = "resume" if claude.session_exists(session_id) else "new"
    if mode == "new":
        session_id = str(uuid.uuid4())
        state.set_input_session(channel.name, session_id)
    spec = SessionSpec(
        stage="input", workdir=cfg.root, settings=cfg.stage(None, "input"), schema=load_schema("channel"),
        prompt=render("channel_turn.md", channel=channel.name, run_id=run_id, now=human(),
                      channel_skill=cfg.skills_dir / CHANNEL_SKILL, format_rules=rules,
                      messages=_messages_text(rows)),
        mode=mode, session_id=session_id, name=f"vahtovik channel {channel.name}",
        env={"VAHTOVIK_CHANNEL": channel.name, "VAHTOVIK_RUN": run_id}, scope=channel.name,
    )
    log.info("%s: Started answering %s, %s session %s", channel.name, plural(len(ids), "message"),
             "resumed" if mode == "resume" else "new", session_id[:8])
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
    log.info("%s: Answered %s in %s, $%.2f", channel.name, plural(len(ids), "message"),
             duration(result.duration_s), result.cost_usd)
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
