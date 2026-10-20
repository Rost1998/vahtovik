"""The group record context.md: skeleton, section appends, and the turn check."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util import human, write_atomic

TEMPLATE = """# {title}
Watch: {watch} · Group key: {key} · Created: {created}

## Summary
_Not processed yet._

## Open questions
_None._

## Artifacts
| First seen | Source | Artifact | Link |
|---|---|---|---|

## Work log

## Reports
"""


def create(path: Path, title: str, watch: str, key: str) -> None:
    if not path.exists():
        write_atomic(path, TEMPLATE.format(title=title, watch=watch, key=key, created=human()))


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    """(heading index, end index) of a `## name` section, or None."""
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"## {name}".lower()), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


def append(path: Path, section: str, text: str) -> None:
    """Add text at the end of a section; add the section if it is missing."""
    lines = path.read_text().splitlines() if path.exists() else []
    found = _section(lines, section)
    if found is None:
        lines += ["", f"## {section}"]
        found = (len(lines) - 1, len(lines))
    start, end = found
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = text.splitlines()
    if end < len(lines):
        new_lines.append("")
    lines[insert_at:end] = new_lines
    write_atomic(path, "\n".join(lines) + "\n")


def replace_section(path: Path, section: str, text: str, after: str | None = None) -> None:
    """Replace the text of a section; add the section after `after` (or at the end) if it is missing."""
    lines = path.read_text().splitlines() if path.exists() else []
    body = text.splitlines()
    found = _section(lines, section)
    if found is None:
        anchor = _section(lines, after) if after else None
        at = anchor[1] if anchor else len(lines)
        block = [f"## {section}", *body]
        if at < len(lines):
            block.append("")
        elif lines and lines[-1].strip():
            block.insert(0, "")
        lines[at:at] = block
    else:
        start, end = found
        lines[start + 1:end] = body + ([""] if end < len(lines) else [])
    write_atomic(path, "\n".join(lines) + "\n")


def questions_text(questions: list[str]) -> str:
    return "\n".join(f"- {' '.join(q.split())}" for q in questions) or "_None._"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def artifact_row(source: str, title: str, url: str) -> str:
    link = f"[link]({url})" if url else ""
    return f"| {human()} | {_cell(source)} | {_cell(title)} | {link} |"


def report_line(channel: str, ok: bool, detail: str, subject: str, dry_run: bool = False) -> str:
    status = ("sent" if ok else "failed") + (" (dry run)" if dry_run else "")
    detail = f" ({_cell(detail)})" if detail else ""
    return f"- {human()} · {channel} · {status}{detail} · {_cell(subject)}"


@dataclass(frozen=True)
class Fingerprint:
    work_log_entries: int


def fingerprint(path: Path) -> Fingerprint:
    lines = path.read_text().splitlines() if path.exists() else []
    found = _section(lines, "Work log")
    if found is None:
        return Fingerprint(0)
    start, end = found
    return Fingerprint(sum(1 for line in lines[start + 1:end] if line.startswith("### ")))


def was_updated(before: Fingerprint, after: Fingerprint) -> bool:
    """A turn counts as done only if the agent added a Work log entry."""
    return after.work_log_entries > before.work_log_entries
