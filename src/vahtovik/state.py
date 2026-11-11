"""SQLite state: runs, groups, artifacts, sessions, turns and deliveries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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
    PRIMARY KEY (watch, id, group_key));
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
CREATE TABLE IF NOT EXISTS session_costs (
    session_id TEXT PRIMARY KEY, watch TEXT, group_key TEXT, cost_usd REAL DEFAULT 0, file_mtime REAL);
CREATE TABLE IF NOT EXISTS cost_points (
    session_id TEXT, total_usd REAL, at TEXT, PRIMARY KEY (session_id, total_usd));
CREATE TABLE IF NOT EXISTS group_aliases (
    watch TEXT, key TEXT, target TEXT, created_at TEXT, PRIMARY KEY (watch, key));
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channel_inputs (
    channel TEXT PRIMARY KEY, session_id TEXT, last_read_at TEXT, last_ok_at TEXT, last_error TEXT, last_error_at TEXT);
CREATE TABLE IF NOT EXISTS channel_messages (
    channel TEXT, id TEXT, text TEXT, sender TEXT, sent_at TEXT, received_at TEXT, status TEXT DEFAULT 'new',
    attempts INTEGER DEFAULT 0, last_error TEXT, answered_at TEXT, PRIMARY KEY (channel, id));
"""

# Columns added later. Opening an older database adds them.
COLUMNS = {
    "groups": {
        "status": "TEXT DEFAULT 'idle'", "status_at": "TEXT", "status_reason": "TEXT", "questions": "TEXT",
        "prompt_at": "TEXT", "idle": "INTEGER DEFAULT 1", "stop_blocks": "INTEGER DEFAULT 0", "notified_at": "TEXT",
        "check_after": "TEXT", "checked_at": "TEXT",
        "recheck_every": "TEXT", "scheduled_at": "TEXT", "scheduled_reason": "TEXT",
        "scheduled_attempts": "INTEGER DEFAULT 0",
    },
    "group_turns": {"artifact_ids": "TEXT", "work_log_before": "INTEGER", "long_warned_at": "TEXT",
                    "scheduled_for": "TEXT"},
    "windows": {"checked_at": "TEXT"},
    "deliveries": {"given_up_warned_at": "TEXT", "reply_to": "TEXT"},
    "source_state": {"last_run_at": "TEXT"},
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
        self._artifacts_in_many_groups()
        for table, columns in COLUMNS.items():
            have = {r["name"] for r in self.rows(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in have:
                    try:
                        self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                    except sqlite3.OperationalError as exc:  # another process added it first
                        if "duplicate column" not in str(exc):
                            raise
                        continue
                    if (table, name) == ("groups", "checked_at"):
                        # The watchdog checks what changes from now on, not every older session at once.
                        self.execute("UPDATE groups SET checked_at = ?", utc_iso())
                    if (table, name) == ("source_state", "last_run_at"):
                        # Sources had their watch's time before each source had its own.
                        self.execute("UPDATE source_state SET last_run_at = (SELECT last_run_at FROM watch_state w "
                                     "WHERE w.watch = source_state.watch)")

    def _artifact_key(self) -> list[str]:
        return [r["name"] for r in sorted(self.rows("PRAGMA table_info(artifacts)"), key=lambda r: r["pk"]) if r["pk"]]

    def _artifacts_in_many_groups(self) -> None:
        """Older databases key artifacts by (watch, id), so an artifact could be in one group only.
        Rebuild the table with the key (watch, id, group_key); the rows stay as they are."""
        if self._artifact_key() != ["watch", "id"]:
            return
        definition = SCHEMA.split("CREATE TABLE IF NOT EXISTS artifacts", 1)[1].split(";", 1)[0]
        with self.transaction():
            if self._artifact_key() != ["watch", "id"]:
                return  # another process rebuilt it meanwhile
            columns = ", ".join(r["name"] for r in self.rows("PRAGMA table_info(artifacts)"))
            self.execute(f"CREATE TABLE artifacts_rebuilt {definition}")
            self.execute(f"INSERT INTO artifacts_rebuilt ({columns}) SELECT {columns} FROM artifacts")
            self.execute("DROP TABLE artifacts")
            self.execute("ALTER TABLE artifacts_rebuilt RENAME TO artifacts")

    def close(self) -> None:
        self.db.close()

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def row(self, sql: str, *args) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def execute(self, sql: str, *args) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Changes that other processes see all at once, or not at all."""
        self.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.execute("ROLLBACK")
            raise
        self.execute("COMMIT")

    # runs and watches

    def start_run(self, run_id: str) -> None:
        self.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, 'running')", run_id, utc_iso())

    def finish_run(self, run_id: str, status: str) -> None:
        self.execute("UPDATE runs SET finished_at = ?, status = ? WHERE id = ?", utc_iso(), status, run_id)

    def run_cost(self, run_id: str) -> float:
        return self.row("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM sessions WHERE run_id = ?", run_id)["c"]

    def get_meta(self, key: str) -> str | None:
        row = self.row("SELECT value FROM meta WHERE key = ?", key)
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", key, value)

    def source_last_run(self, watch: str, source_id: str) -> str | None:
        """When the source was last monitored, or None if never."""
        row = self.row("SELECT last_run_at FROM source_state WHERE watch = ? AND source_id = ?", watch, source_id)
        return row["last_run_at"] if row else None

    def watch_last_run(self, watch: str) -> str | None:
        """When a source of the watch was last monitored, or None if never."""
        return self.row("SELECT MAX(last_run_at) AS at FROM source_state WHERE watch = ?", watch)["at"]

    def set_source_run(self, watch: str, source_id: str) -> None:
        self.execute(
            "INSERT INTO source_state (watch, source_id, last_run_at) VALUES (?, ?, ?) "
            "ON CONFLICT (watch, source_id) DO UPDATE SET last_run_at = excluded.last_run_at",
            watch, source_id, utc_iso(),
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

    def resolve_group(self, watch: str, key: str) -> str:
        """The key a group goes by now: the group it was merged into, or the key itself."""
        row = self.row("SELECT target FROM group_aliases WHERE watch = ? AND key = ?", watch, key)
        return row["target"] if row else key

    def merge_group(self, watch: str, key: str, target: str) -> None:
        """Move what the database keeps about a group to the target group, and forward the key to it.
        Artifacts the target has already are dropped; the caller moves the files first."""
        self.execute("DELETE FROM artifacts WHERE watch = ? AND group_key = ? AND id IN "
                     "(SELECT id FROM artifacts WHERE watch = ? AND group_key = ?)", watch, key, watch, target)
        for table in ("artifacts", "deliveries", "group_turns", "session_costs"):
            self.execute(f"UPDATE {table} SET group_key = ? WHERE watch = ? AND group_key = ?", target, watch, key)
        self.execute("DELETE FROM windows WHERE watch = ? AND group_key = ?", watch, key)
        self.execute("DELETE FROM groups WHERE watch = ? AND key = ?", watch, key)
        self.execute("UPDATE group_aliases SET target = ? WHERE watch = ? AND target = ?", target, watch, key)
        self.execute("INSERT OR REPLACE INTO group_aliases (watch, key, target, created_at) VALUES (?, ?, ?, ?)",
                     watch, key, target, utc_iso())

    def move_artifact(self, watch: str, artifact_id: str, key: str, target: str) -> None:
        """Move an artifact to another group, where it waits for the group's next turn. If the target has it
        already, it is only removed here."""
        if self.artifact_exists(watch, artifact_id, target):
            self.execute("DELETE FROM artifacts WHERE watch = ? AND id = ? AND group_key = ?", watch, artifact_id, key)
        else:
            self.execute("UPDATE artifacts SET group_key = ?, status = 'new', attempts = 0, processed_at = NULL "
                         "WHERE watch = ? AND id = ? AND group_key = ?", target, watch, artifact_id, key)

    def group_artifacts(self, watch: str, key: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM artifacts WHERE watch = ? AND group_key = ? ORDER BY first_seen_at, id",
                         watch, key)

    def touch_group(self, watch: str, key: str) -> None:
        self.execute("UPDATE groups SET updated_at = ? WHERE watch = ? AND key = ?", utc_iso(), watch, key)

    def set_session(self, watch: str, key: str, session_id: str) -> None:
        self.execute(
            "UPDATE groups SET session_id = ?, session_created_at = ? WHERE watch = ? AND key = ?",
            session_id, utc_iso(), watch, key,
        )
        self.execute("INSERT OR IGNORE INTO session_costs (session_id, watch, group_key) VALUES (?, ?, ?)",
                     session_id, watch, key)

    # what group sessions cost (see costs.py)

    def session_costs(self) -> list[sqlite3.Row]:
        # Sessions that groups had before this table existed.
        self.execute("INSERT OR IGNORE INTO session_costs (session_id, watch, group_key) "
                     "SELECT session_id, watch, key FROM groups WHERE session_id IS NOT NULL")
        return self.rows("SELECT * FROM session_costs")

    def set_session_cost(self, session_id: str, cost_usd: float, file_mtime: float) -> None:
        self.execute("UPDATE session_costs SET cost_usd = ?, file_mtime = ? WHERE session_id = ?",
                     cost_usd, file_mtime, session_id)

    def cost_tops(self) -> dict[str, float]:
        """The highest running total stored for each group session."""
        return {r["session_id"]: r["t"] for r in self.rows(
            "SELECT session_id, MAX(total_usd) AS t FROM cost_points GROUP BY session_id")}

    def add_cost_points(self, session_id: str, points: list[tuple[str, float]]) -> None:
        """Running totals of a group session and when they were reached: (time, total)."""
        for at, total in points:
            self.execute("INSERT OR IGNORE INTO cost_points (session_id, total_usd, at) VALUES (?, ?, ?)",
                         session_id, total, at)

    def group_costs(self, since: str | None = None) -> dict[tuple[str, str], float]:
        """What the sessions of each group cost since a time, or in all, by (watch, group key):
        each session's last total minus its last total before that time."""
        rows = self.rows(
            "SELECT watch, group_key, SUM(c) AS c FROM ("
            "  SELECT session_id, MAX(total_usd) - COALESCE(MAX(CASE WHEN at < ? THEN total_usd END), 0) AS c"
            "  FROM cost_points GROUP BY session_id"
            ") JOIN session_costs USING (session_id) GROUP BY watch, group_key", since or "")
        return {(r["watch"], r["group_key"]): r["c"] for r in rows}

    def stage_costs(self, since: str | None = None) -> dict[str, float]:
        """What monitor and watchdog sessions that started since a time, or all of them, cost, by stage."""
        rows = self.rows("SELECT stage, SUM(cost_usd) AS c FROM sessions WHERE started_at >= ? GROUP BY stage",
                         since or "")
        return {r["stage"]: r["c"] for r in rows}

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

    # scheduled turns: a turn at a set time, even without new artifacts

    def set_schedule(self, watch: str, key: str, at: str | None, reason: str | None = None) -> None:
        """The time of the group's next scheduled turn, or None for none."""
        self.execute("UPDATE groups SET scheduled_at = ?, scheduled_reason = ?, scheduled_attempts = 0 "
                     "WHERE watch = ? AND key = ?", at, reason if at else None, watch, key)

    def set_recheck(self, watch: str, key: str, every: str | None) -> None:
        """The group's standing period: a `finished` turn becomes `scheduled` this long after."""
        self.execute("UPDATE groups SET recheck_every = ? WHERE watch = ? AND key = ?", every, watch, key)

    def take_schedule(self, watch: str, key: str, turn_id: str, now: str) -> str | None:
        """A turn starts: if the group's scheduled turn is due, this turn is it. Returns the reason or None."""
        group = self.group(watch, key)
        if not group["scheduled_at"] or group["scheduled_at"] > now:
            return None
        self.execute("UPDATE group_turns SET scheduled_for = ? WHERE id = ?", group["scheduled_at"], turn_id)
        self.execute("UPDATE groups SET scheduled_at = NULL WHERE watch = ? AND key = ?", watch, key)
        return group["scheduled_reason"] or "a scheduled check"

    def restore_schedule(self, turn) -> None:
        """A scheduled turn failed: it is due again, and counts one more failed attempt."""
        if turn["scheduled_for"]:
            self.execute("UPDATE groups SET scheduled_at = ?, scheduled_attempts = scheduled_attempts + 1 "
                         "WHERE watch = ? AND key = ?", turn["scheduled_for"], turn["watch"], turn["group_key"])

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

    def artifact_exists(self, watch: str, artifact_id: str, key: str | None = None) -> bool:
        """Whether the watch has the artifact: in any group, or in the group `key` if given."""
        return self.row("SELECT 1 FROM artifacts WHERE watch = ? AND id = ? AND (? IS NULL OR group_key = ?)",
                        watch, artifact_id, key, key) is not None

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
        """Groups with new artifacts, or with a scheduled turn that is due, the longest waiting first."""
        rows = self.rows(
            "SELECT key, MIN(at) AS first FROM ("
            "  SELECT group_key AS key, first_seen_at AS at FROM artifacts "
            "  WHERE watch = ? AND status = 'new' AND attempts < ?"
            "  UNION ALL SELECT key, scheduled_at FROM groups WHERE watch = ? AND scheduled_at <= ? "
            "  AND scheduled_attempts < ? AND status IN ('scheduled', 'failed')"
            ") GROUP BY key ORDER BY first",
            watch, max_attempts, watch, utc_iso(), max_attempts,
        )
        return [r["key"] for r in rows]

    def next_scheduled(self, watch: str) -> sqlite3.Row | None:
        """The group whose scheduled turn comes first."""
        return self.row("SELECT * FROM groups WHERE watch = ? AND scheduled_at IS NOT NULL "
                        "ORDER BY scheduled_at LIMIT 1", watch)

    def pending_artifacts(self, watch: str, key: str, max_attempts: int) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM artifacts WHERE watch = ? AND group_key = ? AND status = 'new' AND attempts < ? "
            "ORDER BY first_seen_at",
            watch, key, max_attempts,
        )

    def mark_processed(self, watch: str, key: str, ids: list[str]) -> None:
        self.db.executemany(
            "UPDATE artifacts SET status = 'processed', processed_at = ? WHERE watch = ? AND group_key = ? AND id = ?",
            [(utc_iso(), watch, key, i) for i in ids],
        )

    def mark_attempt_failed(self, watch: str, key: str, ids: list[str], max_attempts: int) -> None:
        self.db.executemany(
            "UPDATE artifacts SET attempts = attempts + 1, status = CASE WHEN attempts + 1 >= ? THEN 'failed' "
            "ELSE status END WHERE watch = ? AND group_key = ? AND id = ?",
            [(max_attempts, watch, key, i) for i in ids],
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
        self.execute("UPDATE artifacts SET status = 'new', processed_at = NULL WHERE watch = ? AND group_key = ? "
                     "AND status = 'processed' AND id IN (SELECT value FROM json_each(?))",
                     turn["watch"], turn["group_key"], turn["artifact_ids"] or "[]")

    def retry_turn(self, turn_id: str, error: str) -> None:
        """Count a finished turn as failed, so a later run repeats it: its artifacts wait again."""
        turn = self.turn(turn_id)
        self.execute("UPDATE group_turns SET ok = 0, error = ? WHERE id = ?", error, turn_id)
        self.restore_schedule(turn)
        self.execute("UPDATE artifacts SET status = 'new', processed_at = NULL WHERE watch = ? AND group_key = ? "
                     "AND status = 'processed' AND id IN (SELECT value FROM json_each(?))",
                     turn["watch"], turn["group_key"], turn["artifact_ids"] or "[]")

    def working_turns(self) -> int:
        """Unfinished turns whose agent works: not those that wait for their background work or for a person."""
        return self.row("SELECT COUNT(*) AS n FROM group_turns t JOIN groups g ON g.watch = t.watch AND "
                        "g.key = t.group_key WHERE t.finished_at IS NULL AND g.status = 'running'")["n"]

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
                     origin: str, turn_id: str | None, reply_to: str | None = None) -> int:
        """A report or notice of a group, or with watch and key "" a reply to a message from a channel."""
        now = utc_iso()
        cursor = self.execute(
            "INSERT INTO deliveries (watch, group_key, channel, subject, body, origin, turn_id, status, "
            "created_at, updated_at, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            watch, key, channel, subject, body, origin, turn_id, now, now, reply_to,
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

    # channel-input (see stages/channel_input.py)

    def input_state(self, channel: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM channel_inputs WHERE channel = ?", channel)

    def input_read(self, channel: str, error: str | None = None) -> None:
        """The channel was read, with an error or without."""
        now = utc_iso()
        self.execute("INSERT OR IGNORE INTO channel_inputs (channel) VALUES (?)", channel)
        if error:
            self.execute("UPDATE channel_inputs SET last_read_at = ?, last_error = ?, last_error_at = ? "
                         "WHERE channel = ?", now, error, now, channel)
        else:
            self.execute("UPDATE channel_inputs SET last_read_at = ?, last_ok_at = ? WHERE channel = ?",
                         now, now, channel)

    def set_input_session(self, channel: str, session_id: str | None) -> None:
        """None clears it: the next answer starts a new session."""
        self.execute("INSERT OR IGNORE INTO channel_inputs (channel) VALUES (?)", channel)
        self.execute("UPDATE channel_inputs SET session_id = ? WHERE channel = ?", session_id, channel)

    def add_message(self, channel: str, message_id: str, text: str, sender: str, sent_at: str) -> bool:
        """Store a message from the channel. Returns False if it is known already."""
        cursor = self.execute(
            "INSERT OR IGNORE INTO channel_messages (channel, id, text, sender, sent_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", channel, message_id, text, sender, sent_at, utc_iso())
        return cursor.rowcount == 1

    def waiting_messages(self, channel: str, max_attempts: int) -> list[sqlite3.Row]:
        """The channel's messages that wait for an answer, the oldest first."""
        return self.rows("SELECT * FROM channel_messages WHERE channel = ? AND status = 'new' AND attempts < ? "
                         "ORDER BY received_at, rowid", channel, max_attempts)

    def message_counts(self, channel: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.rows(
            "SELECT status, COUNT(*) AS n FROM channel_messages WHERE channel = ? GROUP BY status", channel)}

    def messages_failed_attempt(self, channel: str, ids: list[str], error: str) -> None:
        self.db.executemany("UPDATE channel_messages SET attempts = attempts + 1, last_error = ? "
                            "WHERE channel = ? AND id = ?", [(error, channel, i) for i in ids])

    def set_messages(self, channel: str, ids: list[str], status: str) -> None:
        """Mark messages `answered`, or `failed` when vahtovik gave up on them."""
        self.db.executemany("UPDATE channel_messages SET status = ?, answered_at = ? WHERE channel = ? AND id = ?",
                            [(status, utc_iso(), channel, i) for i in ids])
