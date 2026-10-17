"""Send reports through channel skills' `send` scripts, record them, retry failures."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from . import context_file, skills
from .config import Config
from .state import State
from .util import tail

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
SEND_TIMEOUT = 60


class DeliveryError(Exception):
    pass


@dataclass
class SendResult:
    ok: bool
    detail: str  # delivery identifier on success, error on failure


def run_send_script(cfg: Config, channel: str, subject: str, body: str, dry_run: bool,
                    extra_env: dict[str, str] | None = None) -> SendResult:
    settings = cfg.channels[channel]
    try:
        skill = skills.load(cfg.skills_dir, "channels", settings.skill)
    except skills.SkillError as exc:
        return SendResult(False, str(exc))
    env = {**os.environ, **settings.env, **(extra_env or {}),
           "VAHTOVIK_CHANNEL": channel, "VAHTOVIK_SUBJECT": subject}
    if dry_run:
        env["VAHTOVIK_DRY_RUN"] = "1"
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


def attempt(cfg: Config, state: State, delivery_id: int, dry_run: bool):
    row = state.delivery(delivery_id)
    result = run_send_script(cfg, row["channel"], row["subject"], row["body"], dry_run,
                             {"VAHTOVIK_WATCH": row["watch"], "VAHTOVIK_GROUP": row["group_key"]})
    state.update_delivery(delivery_id, result.ok, None if result.ok else result.detail,
                          result.detail if result.ok else None)
    context = cfg.group_dir(row["watch"], row["group_key"]) / "context.md"
    context_file.append(context, "Reports",
                        context_file.report_line(row["channel"], result.ok, result.detail, row["subject"], dry_run))
    return state.delivery(delivery_id)


def report(cfg: Config, state: State, watch_name: str, key: str, channel: str, subject: str, body: str,
           origin: str, turn_id: str | None = None, dry_run: bool = False):
    watch = cfg.watch(watch_name)
    if channel not in watch.channels:
        allowed = ", ".join(watch.channels) or "none"
        raise DeliveryError(f"channel {channel!r} is not allowed for watch {watch_name}; allowed: {allowed}")
    if state.group(watch_name, key) is None:
        raise DeliveryError(f"unknown group {watch_name}/{key}")
    if not body.strip():
        raise DeliveryError("the message body is empty")
    delivery_id = state.add_delivery(watch_name, key, channel, subject, body, origin, turn_id)
    return attempt(cfg, state, delivery_id, dry_run)


def retry_failed(cfg: Config, state: State, dry_run: bool) -> tuple[int, int]:
    sent = failed = 0
    for row in state.retryable_deliveries(MAX_ATTEMPTS):
        if row["channel"] not in cfg.channels:
            continue
        result = attempt(cfg, state, row["id"], dry_run)
        if result["status"] == "sent":
            sent += 1
        else:
            failed += 1
            log.warning("delivery %s to %s failed again: %s", row["id"], row["channel"], result["last_error"])
    return sent, failed
