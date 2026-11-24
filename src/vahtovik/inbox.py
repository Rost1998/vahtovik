"""The inbox of each worker: messages from the person and from other workers.

`vahtovik tell`, `vahtovik worker spawn --message` and report-back put messages here. A run delivers them: it types
them into the worker's window when the window is open, its agent is idle and nobody is looking at it; otherwise
they go into the worker's next turn. A message whose turn fails waits for the next turn, up to MAX_ATTEMPTS turns.
"""

from __future__ import annotations

import logging

from . import workers
from .config import Config
from .state import State
from .tmux import Tmux, Window
from .util import plural

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def sender_text(state: State, sender: str, row=None) -> str:
    """Who sent a message, for the agent: "the person", "worker a3f9c2-uv-0-13 (your parent)"."""
    kind, _, who = (sender or "person").partition(":")
    if kind != "worker":
        return "the person"
    other = state.worker(who)
    text = f"worker {workers.name(other) if other else who}"
    if row is not None and other is not None:
        if row["parent"] == other["id"]:
            text += " (your parent)"
        elif other["parent"] == row["id"]:
            text += " (your child)"
    return text


def prefix(state: State, sender: str, row=None) -> str:
    """How a delivered message starts; the worker rules say that such prompts are real messages."""
    return f"Message from {sender_text(state, sender, row)}, sent through vahtovik:"


def send(state: State, row, sender: str, text: str) -> int:
    """Put a message into the worker's inbox. Returns its id."""
    message_id = state.add_message(row["id"], sender, text.strip())
    log.info("%s: Got a message from %s: %s", workers.label(row), sender_text(state, sender), " ".join(text.split())[:100])
    return message_id


def prompt_text(state: State, row, messages) -> str:
    """The messages, for a turn's prompt."""
    return "\n\n".join(f"### {prefix(state, m['sender'], row)}\n\n{m['text'].strip()}" for m in messages) or "- none"


def deliver_typed(cfg: Config, state: State, tmux: Tmux, row, window: Window) -> bool:
    """Type the worker's waiting messages into its open window, as one prompt, when its agent is idle and nobody
    is looking at the window. Returns whether it did."""
    messages = state.pending_messages(row["id"], MAX_ATTEMPTS)
    row = state.worker(row["id"])
    if not messages or window.clients or not row["idle"] or row["status"] == "maintenance":
        return False
    text = " ".join(" ".join(f"{prefix(state, m['sender'], row)} {m['text']}".split()) for m in messages)
    tmux.send_text(window.id, text)
    state.deliver_messages([m["id"] for m in messages], "typed")
    log.info("%s: Typed %s into the session window", workers.label(row), plural(len(messages), "message"))
    return True
