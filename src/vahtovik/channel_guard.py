"""The guard of a limited input channel: a Claude Code PreToolUse hook that allows its agent to read only the
rules and the allowed watches, and to run only the channel's vahtovik commands. Everything else is denied.

Only the sessions of limited channels get it (channel_input.answer). They run with `--permission-mode dontAsk` and
only the tools in TOOLS, so a tool call that the guard does not allow is denied by Claude Code too."""

from __future__ import annotations

import json
import logging
import shlex
import sys
from pathlib import Path

from .config import Channel, Config
from .util import schema_path

log = logging.getLogger(__name__)

TOOLS = ("Read", "Grep", "Glob", "Bash")
ANSWER_TOOLS = ("StructuredOutput",)  # how Claude Code takes the `--json-schema` answer
UNQUOTED = set(";&|<>()`$\\\n\r")  # what lets bash run, chain or expand something outside quotes
IN_DOUBLE_QUOTES = set("$`\\")


def hooks(channel: str) -> dict:
    """The Claude Code hooks for the `--settings` of a limited channel's session."""
    command = shlex.join([sys.executable, "-m", "vahtovik", "hook", "guard", channel])
    return {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}


def readable(cfg: Config, channel: Channel) -> list[Path]:
    """The files and folders the channel's agent may read: the rules, the schemas and its watches, never a .env
    file."""
    paths = [cfg.skills_dir, cfg.root / "README.md", cfg.root / "CLAUDE.md", schema_path("artifact").parent]
    if channel.input.watches is None:
        return paths + [cfg.watches_dir, cfg.data_dir / "watches"]
    for watch in channel.input.watches:
        paths += [cfg.watches_dir / watch, cfg.data_dir / "watches" / watch]
    return paths


def commands_text(channel: Channel) -> str:
    return ", ".join(f"`vahtovik {c}`" for c in channel.input.commands) or "none"


def decide(cfg: Config, channel: Channel, data: dict) -> tuple[bool, str]:
    """Whether the hook's tool call is allowed, and why."""
    tool, given = data.get("tool_name") or "", data.get("tool_input") or {}
    if tool in ANSWER_TOOLS:
        return True, "your answer"
    if tool == "Bash":
        return _command(channel, str(given.get("command") or ""))
    if tool in ("Read", "Grep", "Glob"):
        key = "file_path" if tool == "Read" else "path"
        return _path(cfg, channel, tool, given.get(key), Path(data.get("cwd") or cfg.root))
    return False, (f"This channel is limited: you may use only {', '.join(TOOLS)}, and Bash only for "
                   f"{commands_text(channel)}.")


def _command(channel: Channel, command: str) -> tuple[bool, str]:
    problem = _shell_problem(command)
    if problem:
        return False, (f"This channel is limited: run one vahtovik command and nothing else, without {problem}. "
                       "Put text in single quotes.")
    words = shlex.split(command)
    rest = words[1:] if words[:1] == ["vahtovik"] else words[3:] if words[:3] == ["uv", "run", "vahtovik"] else None
    if rest is None:
        return False, f"This channel is limited: Bash may run only {commands_text(channel)}."
    if rest and rest[0] in channel.input.commands:
        return True, f"vahtovik {rest[0]} is one of the channel's commands"
    if not rest or rest[0] in ("-h", "--help"):
        return True, "vahtovik's help"
    return False, f"This channel may not run `vahtovik {rest[0]}`; it may run only {commands_text(channel)}."


def _shell_problem(command: str) -> str | None:
    """What in the command could make bash do more than run one program with its arguments, or None."""
    quote = None
    for char in command:
        if quote == "'":
            quote = None if char == "'" else quote
        elif quote == '"':
            if char in IN_DOUBLE_QUOTES:
                return f"{char!r} inside double quotes"
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char in UNQUOTED:
            return "a newline" if char in "\n\r" else f"{char!r}"
    return "an unclosed quote" if quote else None


def _path(cfg: Config, channel: Channel, tool: str, given: object, cwd: Path) -> tuple[bool, str]:
    allowed = readable(cfg, channel)
    where = ", ".join(str(p.relative_to(cfg.root)) if p.is_relative_to(cfg.root) else str(p) for p in allowed)
    if not given:
        return False, f"This channel is limited: give {tool} a path inside {where}."
    path = (cwd / Path(str(given)).expanduser()).resolve()
    if _is_env(path):
        return False, "This channel may not read .env files: they hold secrets."
    if not any(path == p.resolve() or p.resolve() in path.parents for p in allowed):
        return False, f"This channel may read only {where}, never .env files."
    if tool != "Read" and path.is_dir() and any(_is_env(p) for p in path.rglob(".env*")):
        return False, f"{path} holds a .env file, so {tool} may not search it: give a folder or file inside it."
    return True, "inside what the channel may read"


def _is_env(path: Path) -> bool:
    return path.name == ".env" or path.name.startswith(".env.")


def answer(allowed: bool, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow" if allowed else "deny",
                                   "permissionDecisionReason": reason}}


def run(cfg: Config, channel_name: str, data: dict) -> dict:
    """The hook's answer. Any trouble denies the call."""
    try:
        channel = cfg.channels[channel_name]
        if not channel.input or not channel.input.limited:
            return answer(False, f"channel {channel_name} is not a limited input channel")
        allowed, reason = decide(cfg, channel, data)
    except Exception as exc:  # noqa: BLE001 - a guard that fails must deny
        allowed, reason = False, f"vahtovik's guard failed: {type(exc).__name__}: {exc}"
    if not allowed:
        log.warning("%s: Denied %s %s: %s", channel_name, data.get("tool_name") or "a tool",
                    json.dumps(data.get("tool_input") or {}, ensure_ascii=False), reason)
    return answer(allowed, reason)
