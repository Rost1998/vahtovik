"""SQLite state: runs, groups, artifacts, sessions, turns and deliveries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS watch_state (
    watch TEXT PRIMARY KEY, last_run_at TEXT);
CREATE TABLE IF NOT EXISTS source_state (
    watch TEXT, source_id TEXT, last_ok_at TEXT, last_error TEXT, last_error_at TEXT,
    PRIMARY KEY (watch, source_id));
CREATE TABLE IF NOT EXISTS groups (
    watch TEXT, key TEXT, title TEXT, created_at TEXT, updated_at TEXT,
    session_id TEXT, session_created_at TEXT,
    PRIMARY KEY (watch, key));
CREATE TABLE IF NOT EXISTS artifacts (
    watch TEXT, id TEXT, group_key TEXT, source_id TEXT, title TEXT, url TEXT,
    origin TEXT, status TEXT, attempts INTEGER DEFAULT 0, file TEXT,
    first_seen_at TEXT, processed_at TEXT,
    PRIMARY KEY (watch, id));
CREATE TABLE IF NOT EXISTS group_turns (
    id TEXT PRIMARY KEY, run_id TEXT, watch TEXT, group_key TEXT,
    started_at TEXT, finished_at TEXT, ok INTEGER, output TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stage TEXT, watch TEXT, target TEXT,
    claude_session_id TEXT, started_at TEXT, duration_s REAL, cost_usd REAL,
    ok INTEGER, error TEXT, raw_path TEXT);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, watch TEXT, group_key TEXT, channel TEXT,
    subject TEXT, body TEXT, origin TEXT, turn_id TEXT, status TEXT,
    attempts INTEGER DEFAULT 0, last_error TEXT, external_id TEXT,
    created_at TEXT, updated_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS windows (
    watch TEXT, group_key TEXT, purpose TEXT, turn_id TEXT, started_at TEXT,
    ended_at TEXT, exit_code INTEGER, exit_screen TEXT,
    PRIMARY KEY (watch, group_key));
"""

# Columns added later. Opening an older database adds them.
COLUMNS = {
    "groups": {
        "status": "TEXT DEFAULT 'idle'", "status_at": "TEXT", "status_reason": "TEXT", "questions": "TEXT",
        "prompt_at": "TEXT", "idle": "INTEGER DEFAULT 1", "stop_blocks": "INTEGER DEFAULT 0", "notified_at": "TEXT",
        "check_after": "TEXT", "checked_at": "TEXT",
    },
    "group_turns": {"artifact_ids": "TEXT", "work_log_before": "INTEGER", "long_warned_at": "TEXT"},
    "windows": {"checked_at": "TEXT"},
    "deliveries": {"given_up_warned_at": "TEXT"},
}


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit, so no write transaction stays open while a session runs:
        # `vahtovik report` writes to the same database from inside the session.
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, columns in COLUMNS.items():
            have = {r["name"] for r in self.rows(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in have:
                    try:
                        self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                    except sqlite3.OperationalError as exc:  # another process added it first
                        if "duplicate column" not in str(exc):
                            raise

    def close(self) -> None:
        self.db.close()

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def row(self, sql: str, *args) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def execute(self, sql: str, *args) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    # runs and watches

    def start_run(self, run_id: str) -> None:
        self.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, 'running')", run_id, utc_iso())

    def finish_run(self, run_id: str, status: str) -> None:
        self.execute("UPDATE runs SET finished_at = ?, status = ? WHERE id = ?", utc_iso(), status, run_id)

    def run_summary(self, run_id: str) -> dict:
        turns = self.row("SELECT COUNT(*) AS n, COALESCE(SUM(ok = 1), 0) AS ok, COALESCE(SUM(ok = 0), 0) AS failed "
                         "FROM group_turns WHERE run_id = ?", run_id)
        reports = self.row(
            "SELECT COALESCE(SUM(d.status = 'sent'), 0) AS sent, COALESCE(SUM(d.status != 'sent'), 0) AS failed "
            "FROM deliveries d JOIN group_turns t ON d.turn_id = t.id WHERE t.run_id = ?", run_id)
        cost = self.row("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM sessions WHERE run_id = ?", run_id)["c"]
        return {"turns_ok": turns["ok"], "turns_failed": turns["failed"],
                "turns_paused": turns["n"] - turns["ok"] - turns["failed"],
                "reports_sent": reports["sent"], "reports_failed": reports["failed"], "cost": cost}

    def last_run(self, watch: str) -> str | None:
        row = self.row("SELECT last_run_at FROM watch_state WHERE watch = ?", watch)
        return row["last_run_at"] if row else None

    def set_last_run(self, watch: str) -> None:
        self.execute(
            "INSERT INTO watch_state (watch, last_run_at) VALUES (?, ?) "
            "ON CONFLICT (watch) DO UPDATE SET last_run_at = excluded.last_run_at",
            watch, utc_iso(),
        )

    def source_ok(self, watch: str, source_id: str) -> None:
        self.execute(
            "INSERT INTO source_state (watch, source_id, last_ok_at) VALUES (?, ?, ?) "
            "ON CONFLICT (watch, source_id) DO UPDATE SET last_ok_at = excluded.last_ok_at",
            watch, source_id, utc_iso(),
        )

    def source_failed(self, watch: str, source_id: str, error: str) -> None:
        self.execute(
            "INSERT INTO source_state (watch, source_id, last_error, last_error_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (watch, source_id) DO UPDATE SET "
            "last_error = excluded.last_error, last_error_at = excluded.last_error_at",
            watch, source_id, error, utc_iso(),
        )

    # groups

    def group(self, watch: str, key: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM groups WHERE watch = ? AND key = ?", watch, key)

    def groups(self, watch: str, limit: int = 1000) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM groups WHERE watch = ? ORDER BY updated_at DESC LIMIT ?", watch, limit)

    def add_group(self, watch: str, key: str, title: str) -> None:
        now = utc_iso()
        self.execute(
            "INSERT OR IGNORE INTO groups (watch, key, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            watch, key, title, now, now,
        )

    def touch_group(self, watch: str, key: str) -> None:
        self.execute("UPDATE groups SET updated_at = ? WHERE watch = ? AND key = ?", utc_iso(), watch, key)

    def set_session(self, watch: str, key: str, session_id: str) -> None:
        self.execute(
            "UPDATE groups SET session_id = ?, session_created_at = ? WHERE watch = ? AND key = ?",
            session_id, utc_iso(), watch, key,
        )

    # group status (see group_status.py)

    def set_status(self, watch: str, key: str, status: str, reason: str | None = None,
                   questions: list[str] | None = None, check_after: str | None = None) -> None:
        """Set the status; `questions` replaces the open questions when given."""
        self.execute(
            "UPDATE groups SET status = ?, status_at = ?, status_reason = ?, questions = COALESCE(?, questions), "
            "check_after = ?, notified_at = NULL WHERE watch = ? AND key = ?",
            status, utc_iso(), reason, json.dumps(questions) if questions is not None else None, check_after,
            watch, key,
        )

    def prompt_submitted(self, watch: str, key: str) -> None:
        now = utc_iso()
        self.execute(
            "UPDATE groups SET status = 'running', status_at = ?, status_reason = NULL, prompt_at = ?, idle = 0, "
            "stop_blocks = 0, check_after = NULL, notified_at = NULL WHERE watch = ? AND key = ?",
            now, now, watch, key,
        )

    def agent_stopped(self, watch: str, key: str) -> None:
        self.execute("UPDATE groups SET idle = 1 WHERE watch = ? AND key = ?", watch, key)

    def add_stop_block(self, watch: str, key: str) -> int:
        self.execute("UPDATE groups SET stop_blocks = stop_blocks + 1 WHERE watch = ? AND key = ?", watch, key)
        return self.group(watch, key)["stop_blocks"]

    def group_checked(self, watch: str, key: str) -> None:
        """The watchdog AI checked the group's session."""
        self.execute("UPDATE groups SET checked_at = ? WHERE watch = ? AND key = ?", utc_iso(), watch, key)

    def set_notified(self, watch: str, key: str) -> None:
        """A notice about the current status went out. Any status change clears this."""
        self.execute("UPDATE groups SET notified_at = ? WHERE watch = ? AND key = ?", utc_iso(), watch, key)

    def reported_since(self, watch: str, key: str, since: str | None) -> bool:
        """Whether the agent sent a report (not a notice) for the group since that time."""
        return self.row(
            "SELECT 1 FROM deliveries WHERE watch = ? AND group_key = ? AND origin != 'notice' AND created_at >= ?",
            watch, key, since or "",
        ) is not None

    # artifacts

    def artifact_exists(self, watch: str, artifact_id: str) -> bool:
        return self.row("SELECT 1 FROM artifacts WHERE watch = ? AND id = ?", watch, artifact_id) is not None

    def add_artifact(self, watch: str, artifact_id: str, group_key: str, source_id: str, title: str,
                     url: str, origin: str, status: str, file: str) -> None:
        now = utc_iso()
        self.execute(
            "INSERT INTO artifacts (watch, id, group_key, source_id, title, url, origin, status, file, "
            "first_seen_at, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            watch, artifact_id, group_key, source_id, title, url, origin, status, file, now,
            now if status == "processed" else None,
        )

    def artifact_files(self, watch: str, key: str) -> set[str]:
        return {r["file"] for r in self.rows("SELECT file FROM artifacts WHERE watch = ? AND group_key = ?", watch, key)}

    def recent_artifacts(self, watch: str, source_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT id, title FROM artifacts WHERE watch = ? AND source_id = ? ORDER BY first_seen_at DESC LIMIT ?",
            watch, source_id, limit,
        )

    def pending_groups(self, watch: str, max_attempts: int) -> list[str]:
        rows = self.rows(
            "SELECT group_key, MIN(first_seen_at) AS first FROM artifacts "
            "WHERE watch = ? AND status = 'new' AND attempts < ? GROUP BY group_key ORDER BY first",
            watch, max_attempts,
        )
        return [r["group_key"] for r in rows]

    def pending_artifacts(self, watch: str, key: str, max_attempts: int) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM artifacts WHERE watch = ? AND group_key = ? AND status = 'new' AND attempts < ? "
            "ORDER BY first_seen_at",
            watch, key, max_attempts,
        )

    def mark_processed(self, watch: str, ids: list[str]) -> None:
        self.db.executemany(
            "UPDATE artifacts SET status = 'processed', processed_at = ? WHERE watch = ? AND id = ?",
            [(utc_iso(), watch, i) for i in ids],
        )

    def mark_attempt_failed(self, watch: str, ids: list[str], max_attempts: int) -> None:
        self.db.executemany(
            "UPDATE artifacts SET attempts = attempts + 1, "
            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE watch = ? AND id = ?",
            [(max_attempts, watch, i) for i in ids],
        )

    # sessions and turns

    def log_session(self, run_id: str, stage: str, watch: str, target: str, result) -> None:
        self.execute(
            "INSERT INTO sessions (run_id, stage, watch, target, claude_session_id, started_at, duration_s, "
            "cost_usd, ok, error, raw_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            run_id, stage, watch, target, result.session_id, utc_iso(), result.duration_s, result.cost_usd,
            int(result.ok), result.error, str(result.raw_path) if result.raw_path else None,
        )

    def start_turn(self, turn_id: str, run_id: str, watch: str, key: str, artifact_ids: list[str],
                   work_log_before: int) -> None:
        self.execute(
            "INSERT INTO group_turns (id, run_id, watch, group_key, started_at, artifact_ids, work_log_before) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            turn_id, run_id, watch, key, utc_iso(), json.dumps(artifact_ids), work_log_before,
        )

    def turn(self, turn_id: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM group_turns WHERE id = ?", turn_id)

    def finish_turn(self, turn_id: str, ok: bool, output: dict | None, error: str | None) -> None:
        self.execute(
            "UPDATE group_turns SET finished_at = ?, ok = ?, output = ?, error = ? WHERE id = ?",
            utc_iso(), int(ok), json.dumps(output) if output is not None else None, error, turn_id,
        )

    def reopen_turn(self, turn_id: str) -> None:
        """Undo the end of a turn: its artifacts wait for the turn again."""
        turn = self.turn(turn_id)
        self.execute("UPDATE group_turns SET finished_at = NULL, ok = NULL, output = NULL, error = NULL WHERE id = ?",
                     turn_id)
        self.execute("UPDATE artifacts SET status = 'new', processed_at = NULL WHERE watch = ? AND status = 'processed' "
                     "AND id IN (SELECT value FROM json_each(?))", turn["watch"], turn["artifact_ids"] or "[]")

    def retry_turn(self, turn_id: str, error: str) -> None:
        """Count a finished turn as failed, so a later run repeats it: its artifacts wait again."""
        turn = self.turn(turn_id)
        self.execute("UPDATE group_turns SET ok = 0, error = ? WHERE id = ?", error, turn_id)
        self.execute("UPDATE artifacts SET status = 'new', processed_at = NULL WHERE watch = ? AND status = 'processed' "
                     "AND id IN (SELECT value FROM json_each(?))", turn["watch"], turn["artifact_ids"] or "[]")

    def unfinished_turns(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM group_turns WHERE finished_at IS NULL ORDER BY started_at")

    def latest_turn(self, watch: str, key: str) -> sqlite3.Row | None:
        """The group's latest turn, finished or not."""
        return self.row("SELECT * FROM group_turns WHERE watch = ? AND group_key = ? ORDER BY started_at DESC LIMIT 1",
                        watch, key)

    def last_turn(self, watch: str, key: str) -> sqlite3.Row | None:
        return self.row(
            "SELECT * FROM group_turns WHERE watch = ? AND group_key = ? AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            watch, key,
        )

    # session windows (see windows.py)

    def window_started(self, watch: str, key: str, purpose: str, turn_id: str | None) -> None:
        self.execute(
            "INSERT OR REPLACE INTO windows (watch, group_key, purpose, turn_id, started_at) VALUES (?, ?, ?, ?, ?)",
            watch, key, purpose, turn_id, utc_iso(),
        )

    def window_ended(self, watch: str, key: str, exit_code: int, screen: str) -> None:
        self.execute(
            "UPDATE windows SET ended_at = ?, exit_code = ?, exit_screen = ? WHERE watch = ? AND group_key = ?",
            utc_iso(), exit_code, screen, watch, key,
        )

    def window_record(self, watch: str, key: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM windows WHERE watch = ? AND group_key = ?", watch, key)

    # deliveries

    def add_delivery(self, watch: str, key: str, channel: str, subject: str, body: str,
                     origin: str, turn_id: str | None) -> int:
        now = utc_iso()
        cursor = self.execute(
            "INSERT INTO deliveries (watch, group_key, channel, subject, body, origin, turn_id, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            watch, key, channel, subject, body, origin, turn_id, now, now,
        )
        return int(cursor.lastrowid)

    def delivery(self, delivery_id: int) -> sqlite3.Row:
        return self.row("SELECT * FROM deliveries WHERE id = ?", delivery_id)

    def update_delivery(self, delivery_id: int, ok: bool, error: str | None, external_id: str | None) -> None:
        now = utc_iso()
        self.execute(
            "UPDATE deliveries SET status = ?, attempts = attempts + 1, last_error = ?, external_id = ?, "
            "updated_at = ?, sent_at = CASE WHEN ? THEN ? ELSE sent_at END WHERE id = ?",
            "sent" if ok else "failed", error, external_id, now, int(ok), now, delivery_id,
        )

    def deliveries_since(self, watch: str, key: str, since: str | None) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM deliveries WHERE watch = ? AND group_key = ? AND (? IS NULL OR updated_at >= ?) "
            "ORDER BY id",
            watch, key, since, since,
        )

    def turn_deliveries(self, turn_id: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM deliveries WHERE turn_id = ? ORDER BY id", turn_id)

    def retryable_deliveries(self, max_attempts: int) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM deliveries WHERE status IN ('pending', 'failed') AND attempts < ? ORDER BY id",
            max_attempts,
        )
