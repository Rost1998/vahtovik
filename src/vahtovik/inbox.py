"""The inbox of each worker: messages from the person and from other workers.

`vahtovik tell`, `vahtovik worker spawn --message` and the notices to a worker's watchers put messages here. They
wait in this queue while the worker is busy. A run delivers all that wait, in the order they came: it types them
into the worker's window when the window is open, its agent is idle and nobody is looking at it; otherwise they
go into the worker's next turn. A message whose turn fails waits for the next turn, up to MAX_ATTEMPTS turns.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from . import windows, workers
from .config import Config
from .state import State
from .tmux import Tmux, TmuxError, Window
from .util import human, plural

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STARTING = timedelta(seconds=20)  # a window this young may still be starting Claude Code: nothing is typed into it
TYPED_LIMIT = 4000  # characters: longer messages are not typed into a window, they go into the worker's next turn


def sender_text(state: State, sender: str, row=None) -> str:
    """Who sent a message, for the agent: "the person", "worker a3f9c2-uv-0-13 (your parent, it watches you)"."""
    kind, _, who = (sender or "person").partition(":")
    if kind != "worker":
        return "the person"
    other = state.worker(who)
    text = f"worker {workers.name(other) if other else who}"
    if row is not None and other is not None:
        notes = ["your parent"] if row["parent"] == other["id"] else []
        notes += ["your child"] if other["parent"] == row["id"] else []
        notes += ["you watch it"] if any(w["id"] == other["id"] for w in state.watched(row["id"])) else []
        notes += ["it watches you"] if any(w["id"] == other["id"] for w in state.watchers(row["id"])) else []
        text += f" ({', '.join(notes)})" if notes else ""
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


def typed_text(cfg: Config, state: State, row, window: Window) -> tuple[list, str] | None:
    """The worker's waiting messages and the one line to type them as, or None when they cannot be typed into its
    open window now. They can when its agent is idle and nobody is looking at the window. Not into a window that
    has just started: Claude Code may still be starting in it, and would lose the keys. Not a long text either.
    And not when the worker's rules, skills or memory changed since its last turn: the session in the window still
    holds the CLAUDE.md of its start, and only a turn's prompt tells the agent what changed.
    Messages that cannot be typed wait for a later run, or for the worker's next turn."""
    messages = state.pending_messages(row["id"], MAX_ATTEMPTS)
    row = state.worker(row["id"])
    if not messages or window.clients or not row["idle"] or row["status"] == "maintenance":
        return None
    if windows.age(state, row["id"]) < STARTING or workers.has_changes(cfg, row):
        return None
    # With the time: a session has no clock, and the agent dates its Work log entry by it.
    text = " ".join(" ".join(f"{prefix(state, m['sender'], row)} [{human()}] {m['text']}".split()) for m in messages)
    return (messages, text) if len(text) <= TYPED_LIMIT else None


def deliver_typed(cfg: Config, state: State, tmux: Tmux, row, window: Window) -> bool:
    """Type the worker's waiting messages into its open window, as one prompt, if they can be typed now
    (typed_text). Returns whether it did."""
    found = typed_text(cfg, state, row, window)
    if found is None:
        return False
    messages, text = found
    try:
        tmux.send_text(window.id, text)
    except TmuxError:  # the window closed meanwhile: the messages go into the worker's next turn
        return False
    state.deliver_messages([m["id"] for m in messages], "typed")
    log.info("%s: Typed %s into the session window", workers.label(row), plural(len(messages), "message"))
    return True
