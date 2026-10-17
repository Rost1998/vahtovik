"""The journal: one short line per key event, in data/journal.log.

    2026-10-17 14:03:40 run 20261017-140312 example/uv-0-13: Started group turn: 2 artifacts, resumed session d097ae50

The second column says who wrote the line: a timer run, an interactive session, or a person at the command line.
Modules write to the journal through ordinary `logging` loggers under "vahtovik".
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from .config import Config

FILE_NAME = "journal.log"
MAX_LINE = 300
WHO_WIDTH = len("run 20261017-140312")

_who = "manual"


def path(cfg: Config) -> Path:
    return cfg.data_dir / FILE_NAME


def who_from_env() -> str:
    if os.environ.get("VAHTOVIK_RUN"):
        return f"run {os.environ['VAHTOVIK_RUN']}"
    return "interactive" if os.environ.get("VAHTOVIK_GROUP") else "manual"


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = " ".join(record.getMessage().split())
        if len(message) > MAX_LINE:
            message = message[:MAX_LINE - 3] + "..."
        return f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {_who:<{WHO_WIDTH}} {message}"


def setup(cfg: Config, who: str | None = None, echo: bool = False) -> None:
    """Send "vahtovik" log records to the journal, and to stderr too if `echo` is set."""
    global _who
    _who = who or who_from_env()
    logger = logging.getLogger("vahtovik")
    logger.setLevel(logging.INFO)
    for handler in [h for h in logger.handlers if getattr(h, "journal", False)]:
        logger.removeHandler(handler)
        handler.close()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(path(cfg), encoding="utf-8")]
    if echo:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.journal = True
        handler.setFormatter(_Formatter())
        logger.addHandler(handler)


def read(journal: Path, count: int = 0, text: str = "", follow: bool = False,
         poll: float = 1.0) -> Iterator[str]:
    """The last `count` lines that contain `text` (all of them if `count` is 0).
    With `follow`, then keep yielding such lines as they are added."""
    data = journal.read_bytes() if journal.is_file() else b""
    position = data.rfind(b"\n") + 1  # an unfinished last line is read when it is finished
    matching = [line for line in data[:position].decode("utf-8", "replace").splitlines() if text in line]
    yield from matching[-count:] if count else matching
    if follow:
        yield from _follow(journal, text, poll, position)


def _follow(journal: Path, text: str, poll: float, position: int) -> Iterator[str]:
    pending = b""
    while True:
        size = journal.stat().st_size if journal.exists() else 0
        if size < position:  # the file was truncated or replaced
            position, pending = 0, b""
        if size > position:
            with journal.open("rb") as handle:
                handle.seek(position)
                chunk = handle.read()
                position = handle.tell()
            *lines, pending = (pending + chunk).split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8", "replace")
                if text in line:
                    yield line
        else:
            time.sleep(poll)
