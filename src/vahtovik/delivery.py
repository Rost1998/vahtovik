"""Send vahtovik's own messages through its channels' `send` scripts, record them, and retry failures.

vahtovik's messages are its replies to the person's messages from input channels, the first short messages of
channel agents, and its notices: the watchdog's, and the usage warnings. Workers never use vahtovik's channels:
they report with their skills' scripts."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from . import knowledge
from .config import Channel, Config
from .state import State
from .util import tail

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
SEND_TIMEOUT = 60
ACKNOWLEDGMENT = "acknowledgment"  # the origin of a channel agent's first short message: it is never retried
PLAIN = ("notice", ACKNOWLEDGMENT)  # origins of plain text, which the send script escapes


class DeliveryError(Exception):
    pass


@dataclass
class SendResult:
    ok: bool
    detail: str  # delivery identifier on success, error on failure


def channel_env(cfg: Config, channel: Channel, skill: knowledge.Skill | None = None) -> dict[str, str]:
    """The environment of a channel's scripts, over vahtovik's own: the credentials of its skill and its own
    credentials, then its `env` settings."""
    found = [skill] if skill else []
    return {**knowledge.session_env(cfg, found, channel.credentials), **channel.env}


def run_send_script(cfg: Config, channel: str, subject: str, body: str, dry_run: bool,
                    extra_env: dict[str, str] | None = None, plain: bool = False) -> SendResult:
    settings = cfg.channels[channel]
    try:
        skill = knowledge.load(cfg, settings.skill)
    except knowledge.SkillError as exc:
        return SendResult(False, str(exc))
    env = {**os.environ, **channel_env(cfg, settings, skill), "VAHTOVIK_ABOUT": "", **(extra_env or {}),
           "VAHTOVIK_CHANNEL": channel, "VAHTOVIK_SUBJECT": subject, "VAHTOVIK_WORKER_NAME": ""}
    if dry_run:
        env["VAHTOVIK_DRY_RUN"] = "1"
    if plain:
        env["VAHTOVIK_PLAIN"] = "1"
    try:
        proc = subprocess.run([str(skill.dir / "send")], input=body, text=True, capture_output=True,
                              cwd=cfg.root, env=env, timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return SendResult(False, f"timed out after {SEND_TIMEOUT} s")
    except OSError as exc:
        return SendResult(False, f"cannot run send script: {exc}")
    if proc.returncode == 0:
        return SendResult(True, proc.stdout.strip().splitlines()[-1][:200] if proc.stdout.strip() else "")
    return SendResult(False, tail(proc.stderr, 300) or f"exit code {proc.returncode}")


def kind(row) -> str:
    """What a delivery is: a `reply` or `acknowledgment` to a message from a channel, or a `notice`."""
    if row["origin"] == ACKNOWLEDGMENT:
        return ACKNOWLEDGMENT
    return "reply" if row["reply_to"] else "notice"


def scope(state: State, row) -> str:
    """Whose delivery it is, for the journal: the worker a notice is about, or the channel."""
    if row["about"]:
        worker = state.worker(row["about"])
        if worker is not None:
            return f"{worker['role']}/{worker['id']}-{worker['key']}"
    return row["channel"]


def attempt(cfg: Config, state: State, delivery_id: int, dry_run: bool):
    row = state.delivery(delivery_id)
    extra = {"VAHTOVIK_REPLY_TO": row["reply_to"]} if row["reply_to"] else {}
    worker = state.worker(row["about"]) if row["about"] else None
    if worker is not None:  # a notice about a worker: a skill that keeps a place per worker can send it there
        extra["VAHTOVIK_ABOUT"] = f"{worker['id']}-{worker['key']}"
    result = run_send_script(cfg, row["channel"], row["subject"], row["body"], dry_run, extra,
                             plain=row["origin"] in PLAIN)
    state.update_delivery(delivery_id, result.ok, None if result.ok else result.detail,
                          result.detail if result.ok else None)
    row = state.delivery(delivery_id)
    what = f"{kind(row)} {delivery_id} to {row['channel']}"
    what += f' "{row["subject"]}"' if row["subject"] else ""
    what += " (dry run)" if dry_run else ""
    if result.ok:
        log.info("%s: Sent %s", scope(state, row), what)
    else:
        tries = "not retried" if row["origin"] == ACKNOWLEDGMENT else f"attempt {row['attempts']} of {MAX_ATTEMPTS}"
        log.error("%s: Failed %s (%s): %s", scope(state, row), what, tries, result.detail)
    return row


def reply(cfg: Config, state: State, channel: str, subject: str, body: str, reply_to: str, origin: str = "reply",
          dry_run: bool = False):
    """Answer a message that came from the channel, through the same channel. `origin` "notice" sends plain text
    from vahtovik itself, and ACKNOWLEDGMENT the agent's first short message, plain too."""
    if not body.strip():
        raise DeliveryError("the reply is empty")
    delivery_id = state.add_delivery(channel, subject, body, origin, "", reply_to)
    return attempt(cfg, state, delivery_id, dry_run)


def notice(cfg: Config, state: State, channel: str, subject: str, body: str, about: str = "",
           dry_run: bool = False):
    """A plain message from vahtovik itself that answers no message: a notice `about` a worker (its id), or a
    warning about nothing in particular, such as the Claude plan's usage limits. Failed ones are retried."""
    if channel not in cfg.channels:
        raise DeliveryError(f"unknown channel {channel!r}")
    delivery_id = state.add_delivery(channel, subject, body, "notice", about)
    return attempt(cfg, state, delivery_id, dry_run)


def retry_failed(cfg: Config, state: State, dry_run: bool) -> tuple[int, int]:
    sent = failed = 0
    for row in state.retryable_deliveries(MAX_ATTEMPTS):
        if row["channel"] not in cfg.channels:
            continue
        if attempt(cfg, state, row["id"], dry_run)["status"] == "sent":
            sent += 1
        else:
            failed += 1
    return sent, failed
