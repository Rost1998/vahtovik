"""SQLite state: runs, pollers' sources, workers, artifacts, the workers' inboxes, turns, sessions, windows, costs,
vahtovik's own deliveries, and the messages of input channels."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError
from .util import utc_iso

SCHEMA_VERSION = "2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS source_state (
    poller TEXT, source_id TEXT, last_run_at TEXT, last_ok_at TEXT, last_error TEXT, last_error_at TEXT,
    PRIMARY KEY (poller, source_id));
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY, role TEXT, key TEXT, title TEXT, skills TEXT DEFAULT '[]',
    model TEXT, effort TEXT,
    spawned_by TEXT, parent TEXT, created_at TEXT, updated_at TEXT,
    session_id TEXT, session_created_at TEXT, seen TEXT,
    status TEXT DEFAULT 'idle', status_at TEXT, status_reason TEXT, questions TEXT, summary TEXT,
    prompt_at TEXT, idle INTEGER DEFAULT 1, stop_blocks INTEGER DEFAULT 0, notified_at TEXT,
    check_after TEXT, checked_at TEXT,
    scheduled_at TEXT, scheduled_reason TEXT, scheduled_attempts INTEGER DEFAULT 0,
    archived_at TEXT, archive_reason TEXT, merged_into TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS workers_active_key ON workers (role, key) WHERE archived_at IS NULL;
CREATE TABLE IF NOT EXISTS watches (
    watcher TEXT, watched TEXT, created_at TEXT, PRIMARY KEY (watcher, watched));
CREATE TABLE IF NOT EXISTS artifacts (
    worker TEXT, id TEXT, poller TEXT, source_id TEXT, title TEXT, url TEXT, origin TEXT, status TEXT,
    attempts INTEGER DEFAULT 0, file TEXT, first_seen_at TEXT, processed_at TEXT,
    PRIMARY KEY (worker, id));
CREATE INDEX IF NOT EXISTS artifacts_poller ON artifacts (poller, id);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, sender TEXT, text TEXT, created_at TEXT,
    status TEXT DEFAULT 'new', attempts INTEGER DEFAULT 0, delivered_at TEXT, turn_id TEXT, how TEXT);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY, run_id TEXT, worker TEXT, started_at TEXT, finished_at TEXT, ok INTEGER, output TEXT,
    error TEXT, artifact_ids TEXT, message_ids TEXT, work_log_before INTEGER, long_warned_at TEXT,
    scheduled_for TEXT, snapshot TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stage TEXT, scope TEXT, target TEXT,
    claude_session_id TEXT, started_at TEXT, duration_s REAL, cost_usd REAL, ok INTEGER, error TEXT, raw_path TEXT);
CREATE TABLE IF NOT EXISTS session_costs (
    session_id TEXT PRIMARY KEY, worker TEXT, cost_usd REAL DEFAULT 0, file_mtime REAL);
CREATE TABLE IF NOT EXISTS cost_points (
    session_id TEXT, total_usd REAL, at TEXT, PRIMARY KEY (session_id, total_usd));
CREATE TABLE IF NOT EXISTS windows (
    worker TEXT PRIMARY KEY, purpose TEXT, turn_id TEXT, started_at TEXT, ended_at TEXT, exit_code INTEGER,
    exit_screen TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, subject TEXT, body TEXT, origin TEXT, about TEXT,
    status TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, external_id TEXT, created_at TEXT, updated_at TEXT,
    sent_at TEXT, reply_to TEXT, given_up_warned_at TEXT);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channel_inputs (
    channel TEXT PRIMARY KEY, session_id TEXT, last_read_at TEXT, last_ok_at TEXT, last_error TEXT,
    last_error_at TEXT, answered_at TEXT, awaits_answer INTEGER DEFAULT 0, cleared_at TEXT);
CREATE TABLE IF NOT EXISTS channel_messages (
    channel TEXT, id TEXT, text TEXT, sender TEXT, sent_at TEXT, received_at TEXT, status TEXT DEFAULT 'new',
    attempts INTEGER DEFAULT 0, last_error TEXT, answered_at TEXT, PRIMARY KEY (channel, id));
"""


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit, so no write transaction stays open while a session runs: `vahtovik turn` and the hooks
        # write to the same database from inside the sessions.
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        if self.row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'groups'"):
            self.db.close()
            raise ConfigError(f"{path} is a database of vahtovik 1. vahtovik 2 starts fresh: move the data folder "
                              f"{path.parent} away, or set another one with `paths: {{data: ...}}` in vahtovik.yaml")
        self.db.executescript(SCHEMA)
        if self._has_report_back():
            self._report_back_to_watches()
        self._add_worker_settings()
        self.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", SCHEMA_VERSION)

    def close(self) -> None:
        self.db.close()

    def _has_report_back(self) -> bool:
        return any(c["name"] == "report_back" for c in self.rows("PRAGMA table_info(workers)"))

    def _report_back_to_watches(self) -> None:
        """A database from before `vahtovik worker watch`: a child spawned with --report-back is watched by its
        parent now."""
        with self.transaction():
            if not self._has_report_back():  # another process did it meanwhile
                return
            self.execute(
                "INSERT OR IGNORE INTO watches (watcher, watched, created_at) SELECT c.parent, c.id, c.created_at "
                "FROM workers c JOIN workers p ON p.id = c.parent "
                "WHERE c.report_back = 1 AND c.archived_at IS NULL AND p.archived_at IS NULL")
            self.execute("ALTER TABLE workers DROP COLUMN report_back")

    def _add_worker_settings(self) -> None:
        """A database from before a worker could have its own model and effort."""
        for column in ("model", "effort"):
            if not any(c["name"] == column for c in self.rows("PRAGMA table_info(workers)")):
                try:
                    self.execute(f"ALTER TABLE workers ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError:  # another process added it meanwhile
                    pass

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

    # runs and pollers

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

    def source_state(self, poller: str, source_id: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM source_state WHERE poller = ? AND source_id = ?", poller, source_id)

    def source_last_run(self, poller: str, source_id: str) -> str | None:
        """When the source was last monitored, or None if never."""
        row = self.source_state(poller, source_id)
        return row["last_run_at"] if row else None

    def poller_last_run(self, poller: str) -> str | None:
        """When a source of the poller was last monitored, or None if never."""
        return self.row("SELECT MAX(last_run_at) AS at FROM source_state WHERE poller = ?", poller)["at"]

    def _source_set(self, poller: str, source_id: str, **values) -> None:
        names = ", ".join(values)
        updates = ", ".join(f"{name} = excluded.{name}" for name in values)
        self.execute(f"INSERT INTO source_state (poller, source_id, {names}) VALUES (?, ?{', ?' * len(values)}) "
                     f"ON CONFLICT (poller, source_id) DO UPDATE SET {updates}", poller, source_id, *values.values())

    def set_source_run(self, poller: str, source_id: str) -> None:
        self._source_set(poller, source_id, last_run_at=utc_iso())

    def source_ok(self, poller: str, source_id: str) -> None:
        self._source_set(poller, source_id, last_ok_at=utc_iso())

    def source_failed(self, poller: str, source_id: str, error: str) -> None:
        self._source_set(poller, source_id, last_error=error, last_error_at=utc_iso())

    # workers

    def worker(self, worker_id: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM workers WHERE id = ?", worker_id)

    def active_worker(self, role: str, key: str) -> sqlite3.Row | None:
        """The worker that has the key in the role now, not an archived one."""
        return self.row("SELECT * FROM workers WHERE role = ? AND key = ? AND archived_at IS NULL", role, key)

    def find_worker(self, role: str, key: str) -> sqlite3.Row | None:
        """The worker that the key of the role leads to: the active one, or the one that the latest worker with this
        key was merged into, following merges on."""
        row = self.active_worker(role, key)
        if row:
            return row
        row = self.row("SELECT * FROM workers WHERE role = ? AND key = ? AND merged_into IS NOT NULL "
                       "ORDER BY archived_at DESC LIMIT 1", role, key)
        return self.merge_target(row) if row else None

    def merge_target(self, row) -> sqlite3.Row | None:
        """The active worker that a merged worker's work went to, or None if that one was archived too."""
        seen = set()
        while row is not None and row["merged_into"] and row["id"] not in seen:
            seen.add(row["id"])
            row = self.worker(row["merged_into"])
        return row if row is not None and row["archived_at"] is None else None

    def workers(self, roles: list[str] | None = None, archived: bool = False) -> list[sqlite3.Row]:
        """The workers, the most recently changed first: of these roles only, if given, and without archived ones
        unless `archived`."""
        return self.rows(
            "SELECT * FROM workers WHERE (? IS NULL OR role IN (SELECT value FROM json_each(?))) "
            "AND (? OR archived_at IS NULL) ORDER BY updated_at DESC, id",
            None if roles is None else 1, json.dumps(roles or []), int(archived))

    def children(self, worker_id: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM workers WHERE parent = ? ORDER BY created_at, id", worker_id)

    def add_worker(self, worker_id: str, role: str, key: str, title: str, skills: list[str], spawned_by: str,
                   parent: str | None, model: str | None = None, effort: str | None = None) -> None:
        now = utc_iso()
        self.execute(
            "INSERT INTO workers (id, role, key, title, skills, model, effort, spawned_by, parent, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            worker_id, role, key, title, json.dumps(list(skills)), model, effort, spawned_by, parent, now, now)

    def set_worker_settings(self, worker_id: str, model: str | None, effort: str | None) -> None:
        """The worker's own model and effort; None: its role's."""
        self.execute("UPDATE workers SET model = ?, effort = ?, updated_at = ? WHERE id = ?",
                     model, effort, utc_iso(), worker_id)

    def touch_worker(self, worker_id: str) -> None:
        self.execute("UPDATE workers SET updated_at = ? WHERE id = ?", utc_iso(), worker_id)

    def set_session(self, worker_id: str, session_id: str) -> None:
        self.execute("UPDATE workers SET session_id = ?, session_created_at = ? WHERE id = ?",
                     session_id, utc_iso(), worker_id)
        self.execute("INSERT OR IGNORE INTO session_costs (session_id, worker) VALUES (?, ?)", session_id, worker_id)

    def set_seen(self, worker_id: str, seen: str | None) -> None:
        """What the worker's session has of its per-session information: the snapshot (JSON) of its last turn that
        ended well, or None for a session that has not ended a turn yet (see workers.status_note)."""
        self.execute("UPDATE workers SET seen = ? WHERE id = ?", seen, worker_id)

    def set_turn_snapshot(self, turn_id: str, snapshot: dict) -> None:
        """The per-session information as it was when the turn started; it becomes the worker's `seen` when the
        turn ends well."""
        self.execute("UPDATE turns SET snapshot = ? WHERE id = ?", json.dumps(snapshot), turn_id)

    def archive_worker(self, worker_id: str, reason: str, merged_into: str | None = None) -> None:
        """The worker gets no more turns, so its watches end: it watches nobody, and nobody watches it."""
        self.execute("UPDATE workers SET archived_at = ?, archive_reason = ?, merged_into = ?, scheduled_at = NULL, "
                     "scheduled_reason = NULL WHERE id = ?", utc_iso(), reason, merged_into, worker_id)
        self.execute("DELETE FROM watches WHERE watcher = ? OR watched = ?", worker_id, worker_id)

    def merge_worker(self, worker_id: str, target: str, reason: str) -> None:
        """Move what the database keeps about a worker to the target: its artifacts (those the target has
        already are dropped), its turns, its costs, its undelivered messages and its watches (those that would
        make the target watch itself, or close a loop, are dropped). The worker is archived, and its key leads to
        the target from now on. The caller moves the files first."""
        self.execute("DELETE FROM artifacts WHERE worker = ? AND id IN (SELECT id FROM artifacts WHERE worker = ?)",
                     worker_id, target)
        for table in ("artifacts", "turns", "session_costs"):
            self.execute(f"UPDATE {table} SET worker = ? WHERE worker = ?", target, worker_id)
        self.execute("UPDATE inbox SET worker = ? WHERE worker = ? AND status = 'new'", target, worker_id)
        self.execute("DELETE FROM windows WHERE worker = ?", worker_id)
        watches = self.rows("SELECT watcher, watched FROM watches WHERE watcher = ? OR watched = ? ORDER BY rowid",
                            worker_id, worker_id)
        self.archive_worker(worker_id, reason, target)
        for watch in watches:
            watcher, watched = (target if watch[side] == worker_id else watch[side] for side in ("watcher", "watched"))
            if watcher != watched and self.watch_path(watched, watcher) is None:
                self.add_watch(watcher, watched)

    # watches: a worker that watches another one hears each time that one stops working (worker_status.py)

    def add_watch(self, watcher: str, watched: str) -> None:
        self.execute("INSERT OR IGNORE INTO watches (watcher, watched, created_at) VALUES (?, ?, ?)",
                     watcher, watched, utc_iso())

    def remove_watch(self, watcher: str, watched: str) -> bool:
        """Returns whether there was such a watch."""
        return self.execute("DELETE FROM watches WHERE watcher = ? AND watched = ?", watcher, watched).rowcount > 0

    def watchers(self, worker_id: str) -> list[sqlite3.Row]:
        """The workers that watch this one, in the order they began to."""
        return self.rows("SELECT w.* FROM watches x JOIN workers w ON w.id = x.watcher WHERE x.watched = ? "
                         "ORDER BY x.rowid", worker_id)

    def watched(self, worker_id: str) -> list[sqlite3.Row]:
        """The workers that this one watches."""
        return self.rows("SELECT w.* FROM watches x JOIN workers w ON w.id = x.watched WHERE x.watcher = ? "
                         "ORDER BY x.rowid", worker_id)

    def watch_path(self, start: str, goal: str) -> list[str] | None:
        """The chain of watches that leads from `start` to `goal`, as worker ids: each one watches the next. None
        if there is none. A new watch of `start` by `goal` would close such a chain into a loop."""
        paths, seen = [[start]], {start}
        while paths:
            path = paths.pop()
            if path[-1] == goal:
                return path
            for row in self.rows("SELECT watched FROM watches WHERE watcher = ? ORDER BY rowid", path[-1]):
                if row["watched"] not in seen:
                    seen.add(row["watched"])
                    paths.append([*path, row["watched"]])
        return None

    # worker status (see worker_status.py)

    def set_status(self, worker_id: str, status: str, reason: str | None = None,
                   questions: list[str] | None = None, check_after: str | None = None) -> None:
        """Set the status; `questions` replaces the open questions when given."""
        self.execute(
            "UPDATE workers SET status = ?, status_at = ?, status_reason = ?, questions = COALESCE(?, questions), "
            "check_after = ?, notified_at = NULL WHERE id = ?",
            status, utc_iso(), reason, json.dumps(questions) if questions is not None else None, check_after,
            worker_id)

    def set_summary(self, worker_id: str, summary: str) -> None:
        self.execute("UPDATE workers SET summary = ? WHERE id = ?", summary, worker_id)

    def prompt_submitted(self, worker_id: str) -> None:
        now = utc_iso()
        self.execute(
            "UPDATE workers SET status = 'running', status_at = ?, status_reason = NULL, prompt_at = ?, idle = 0, "
            "stop_blocks = 0, check_after = NULL, notified_at = NULL WHERE id = ?", now, now, worker_id)

    def agent_stopped(self, worker_id: str) -> None:
        self.execute("UPDATE workers SET idle = 1 WHERE id = ?", worker_id)

    def add_stop_block(self, worker_id: str) -> int:
        self.execute("UPDATE workers SET stop_blocks = stop_blocks + 1 WHERE id = ?", worker_id)
        return self.worker(worker_id)["stop_blocks"]

    def worker_checked(self, worker_id: str) -> None:
        """The watchdog AI checked the worker's session."""
        self.execute("UPDATE workers SET checked_at = ? WHERE id = ?", utc_iso(), worker_id)

    def set_notified(self, worker_id: str) -> None:
        """A notice about the current status went out. Any status change clears this."""
        self.execute("UPDATE workers SET notified_at = ? WHERE id = ?", utc_iso(), worker_id)

    # scheduled turns: a turn at a set time, even without new artifacts or messages

    def set_schedule(self, worker_id: str, at: str | None, reason: str | None = None) -> None:
        """The time of the worker's next scheduled turn, or None for none."""
        self.execute("UPDATE workers SET scheduled_at = ?, scheduled_reason = ?, scheduled_attempts = 0 WHERE id = ?",
                     at, reason if at else None, worker_id)

    def take_schedule(self, worker_id: str, turn_id: str, now: str) -> str | None:
        """A turn starts: if the worker's scheduled turn is due, this turn is it. Returns the reason or None."""
        worker = self.worker(worker_id)
        if not worker["scheduled_at"] or worker["scheduled_at"] > now:
            return None
        self.execute("UPDATE turns SET scheduled_for = ? WHERE id = ?", worker["scheduled_at"], turn_id)
        self.execute("UPDATE workers SET scheduled_at = NULL WHERE id = ?", worker_id)
        return worker["scheduled_reason"] or "a scheduled check"

    def restore_schedule(self, turn) -> None:
        """A scheduled turn failed: it is due again, and counts one more failed attempt."""
        if turn["scheduled_for"]:
            self.execute("UPDATE workers SET scheduled_at = ?, scheduled_attempts = scheduled_attempts + 1 "
                         "WHERE id = ?", turn["scheduled_for"], turn["worker"])

    def next_scheduled(self, roles: list[str] | None = None) -> sqlite3.Row | None:
        """The worker whose scheduled turn comes first."""
        return self.row("SELECT * FROM workers WHERE scheduled_at IS NOT NULL AND archived_at IS NULL "
                        "AND (? IS NULL OR role IN (SELECT value FROM json_each(?))) ORDER BY scheduled_at LIMIT 1",
                        None if roles is None else 1, json.dumps(roles or []))

    # artifacts

    def poller_has(self, poller: str, artifact_id: str) -> bool:
        """Whether the poller brought the artifact before, to any worker."""
        return self.row("SELECT 1 FROM artifacts WHERE poller = ? AND id = ?", poller, artifact_id) is not None

    def artifact_exists(self, worker_id: str, artifact_id: str) -> bool:
        return self.row("SELECT 1 FROM artifacts WHERE worker = ? AND id = ?", worker_id, artifact_id) is not None

    def add_artifact(self, worker_id: str, artifact_id: str, poller: str | None, source_id: str, title: str,
                     url: str, origin: str, status: str, file: str) -> None:
        now = utc_iso()
        self.execute(
            "INSERT INTO artifacts (worker, id, poller, source_id, title, url, origin, status, file, first_seen_at, "
            "processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            worker_id, artifact_id, poller, source_id, title, url, origin, status, file, now,
            now if status == "processed" else None)

    def worker_artifacts(self, worker_id: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM artifacts WHERE worker = ? ORDER BY first_seen_at, id", worker_id)

    def recent_artifacts(self, poller: str, source_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.rows("SELECT id, title FROM artifacts WHERE poller = ? AND source_id = ? "
                         "ORDER BY first_seen_at DESC LIMIT ?", poller, source_id, limit)

    def pending_artifacts(self, worker_id: str, max_attempts: int) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM artifacts WHERE worker = ? AND status = 'new' AND attempts < ? "
                         "ORDER BY first_seen_at", worker_id, max_attempts)

    def move_artifact(self, artifact_id: str, worker_id: str, target: str) -> None:
        """Move an artifact to another worker, where it waits for the next turn. If the target has it already, it
        is only removed here."""
        if self.artifact_exists(target, artifact_id):
            self.execute("DELETE FROM artifacts WHERE worker = ? AND id = ?", worker_id, artifact_id)
        else:
            self.execute("UPDATE artifacts SET worker = ?, status = 'new', attempts = 0, processed_at = NULL "
                         "WHERE worker = ? AND id = ?", target, worker_id, artifact_id)

    def mark_processed(self, worker_id: str, ids: list[str]) -> None:
        self.db.executemany("UPDATE artifacts SET status = 'processed', processed_at = ? WHERE worker = ? AND id = ?",
                            [(utc_iso(), worker_id, i) for i in ids])

    def mark_attempt_failed(self, worker_id: str, ids: list[str], max_attempts: int) -> None:
        self.db.executemany(
            "UPDATE artifacts SET attempts = attempts + 1, status = CASE WHEN attempts + 1 >= ? THEN 'failed' "
            "ELSE status END WHERE worker = ? AND id = ?", [(max_attempts, worker_id, i) for i in ids])

    def _artifacts_wait_again(self, turn) -> None:
        self.execute("UPDATE artifacts SET status = 'new', processed_at = NULL WHERE worker = ? "
                     "AND status = 'processed' AND id IN (SELECT value FROM json_each(?))",
                     turn["worker"], turn["artifact_ids"] or "[]")

    # the inbox: messages to a worker from the person or from other workers

    def add_message(self, worker_id: str, sender: str, text: str) -> int:
        cursor = self.execute("INSERT INTO inbox (worker, sender, text, created_at) VALUES (?, ?, ?, ?)",
                              worker_id, sender, text, utc_iso())
        return int(cursor.lastrowid)

    def pending_messages(self, worker_id: str, max_attempts: int) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM inbox WHERE worker = ? AND status = 'new' AND attempts < ? ORDER BY id",
                         worker_id, max_attempts)

    def messages(self, ids: list[int]) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM inbox WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
                         json.dumps(ids))

    def deliver_messages(self, ids: list[int], how: str, turn_id: str | None = None) -> None:
        self.db.executemany("UPDATE inbox SET status = 'delivered', delivered_at = ?, how = ?, turn_id = ? "
                            "WHERE id = ?", [(utc_iso(), how, turn_id, i) for i in ids])

    def messages_failed(self, ids: list[int], max_attempts: int) -> None:
        """The turn that had the messages failed: they wait for the next turn, until their last attempt."""
        self.db.executemany(
            "UPDATE inbox SET attempts = attempts + 1, delivered_at = NULL, turn_id = NULL, "
            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'new' END WHERE id = ?",
            [(max_attempts, i) for i in ids])

    def inbox_counts(self, worker_id: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.rows(
            "SELECT status, COUNT(*) AS n FROM inbox WHERE worker = ? GROUP BY status", worker_id)}

    # what is due

    def pending_workers(self, max_attempts: int, roles: list[str] | None = None) -> list[str]:
        """The workers with new artifacts, with messages in their inbox, or with a scheduled turn that is due,
        the longest waiting first."""
        rows = self.rows(
            "SELECT p.worker AS worker, MIN(p.at) AS first FROM ("
            "  SELECT worker, first_seen_at AS at FROM artifacts WHERE status = 'new' AND attempts < ?"
            "  UNION ALL SELECT worker, created_at FROM inbox WHERE status = 'new' AND attempts < ?"
            "  UNION ALL SELECT id, scheduled_at FROM workers WHERE scheduled_at <= ? AND scheduled_attempts < ? "
            "  AND status IN ('scheduled', 'failed')"
            ") p JOIN workers w ON w.id = p.worker WHERE w.archived_at IS NULL "
            "AND (? IS NULL OR w.role IN (SELECT value FROM json_each(?))) GROUP BY p.worker ORDER BY first",
            max_attempts, max_attempts, utc_iso(), max_attempts, None if roles is None else 1, json.dumps(roles or []))
        return [r["worker"] for r in rows]

    # sessions and turns

    def log_session(self, run_id: str | None, stage: str, scope: str, target: str, result) -> None:
        self.execute(
            "INSERT INTO sessions (run_id, stage, scope, target, claude_session_id, started_at, duration_s, "
            "cost_usd, ok, error, raw_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            run_id, stage, scope, target, result.session_id, utc_iso(), result.duration_s, result.cost_usd,
            int(result.ok), result.error, str(result.raw_path) if result.raw_path else None)

    def start_turn(self, turn_id: str, run_id: str, worker_id: str, artifact_ids: list[str], message_ids: list[int],
                   work_log_before: int) -> None:
        self.execute(
            "INSERT INTO turns (id, run_id, worker, started_at, artifact_ids, message_ids, work_log_before) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            turn_id, run_id, worker_id, utc_iso(), json.dumps(artifact_ids), json.dumps(message_ids), work_log_before)

    def turn(self, turn_id: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM turns WHERE id = ?", turn_id)

    def finish_turn(self, turn_id: str, ok: bool, output: dict | None, error: str | None) -> None:
        self.execute("UPDATE turns SET finished_at = ?, ok = ?, output = ?, error = ? WHERE id = ?",
                     utc_iso(), int(ok), json.dumps(output) if output is not None else None, error, turn_id)

    def reopen_turn(self, turn_id: str) -> None:
        """Undo the end of a turn: its artifacts wait for the turn again. Its messages stay delivered."""
        turn = self.turn(turn_id)
        self.execute("UPDATE turns SET finished_at = NULL, ok = NULL, output = NULL, error = NULL WHERE id = ?",
                     turn_id)
        self._artifacts_wait_again(turn)

    def retry_turn(self, turn_id: str, error: str, max_attempts: int) -> None:
        """Count a finished turn as failed, so a later run repeats it: its artifacts and messages wait again."""
        turn = self.turn(turn_id)
        self.execute("UPDATE turns SET ok = 0, error = ? WHERE id = ?", error, turn_id)
        self.restore_schedule(turn)
        self._artifacts_wait_again(turn)
        self.mark_attempt_failed(turn["worker"], json.loads(turn["artifact_ids"] or "[]"), max_attempts)
        self.messages_failed(json.loads(turn["message_ids"] or "[]"), max_attempts)

    def working_turns(self) -> int:
        """Unfinished turns whose agent works: not those that wait for their background work or for a person."""
        return self.row("SELECT COUNT(*) AS n FROM turns t JOIN workers w ON w.id = t.worker "
                        "WHERE t.finished_at IS NULL AND w.status = 'running'")["n"]

    def unfinished_turns(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM turns WHERE finished_at IS NULL ORDER BY started_at")

    def latest_turn(self, worker_id: str) -> sqlite3.Row | None:
        """The worker's latest turn, finished or not."""
        return self.row("SELECT * FROM turns WHERE worker = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                        worker_id)

    def last_turn(self, worker_id: str) -> sqlite3.Row | None:
        """The worker's latest finished turn."""
        return self.row("SELECT * FROM turns WHERE worker = ? AND finished_at IS NOT NULL "
                        "ORDER BY started_at DESC, rowid DESC LIMIT 1", worker_id)

    # session windows (see windows.py)

    def window_started(self, worker_id: str, purpose: str, turn_id: str | None) -> None:
        self.execute("INSERT OR REPLACE INTO windows (worker, purpose, turn_id, started_at) VALUES (?, ?, ?, ?)",
                     worker_id, purpose, turn_id, utc_iso())

    def window_ended(self, worker_id: str, exit_code: int, screen: str) -> None:
        self.execute("UPDATE windows SET ended_at = ?, exit_code = ?, exit_screen = ? WHERE worker = ?",
                     utc_iso(), exit_code, screen, worker_id)

    def window_record(self, worker_id: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM windows WHERE worker = ?", worker_id)

    # what worker sessions cost (see costs.py)

    def session_costs(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM session_costs")

    def set_session_cost(self, session_id: str, cost_usd: float, file_mtime: float) -> None:
        self.execute("UPDATE session_costs SET cost_usd = ?, file_mtime = ? WHERE session_id = ?",
                     cost_usd, file_mtime, session_id)

    def cost_tops(self) -> dict[str, float]:
        """The highest running total stored for each worker session."""
        return {r["session_id"]: r["t"] for r in self.rows(
            "SELECT session_id, MAX(total_usd) AS t FROM cost_points GROUP BY session_id")}

    def add_cost_points(self, session_id: str, points: list[tuple[str, float]]) -> None:
        """Running totals of a worker session and when they were reached: (time, total)."""
        for at, total in points:
            self.execute("INSERT OR IGNORE INTO cost_points (session_id, total_usd, at) VALUES (?, ?, ?)",
                         session_id, total, at)

    def worker_costs(self, since: str | None = None) -> dict[str, float]:
        """What the sessions of each worker cost since a time, or in all, by worker id: each session's last total
        minus its last total before that time."""
        rows = self.rows(
            "SELECT worker, SUM(c) AS c FROM ("
            "  SELECT session_id, MAX(total_usd) - COALESCE(MAX(CASE WHEN at < ? THEN total_usd END), 0) AS c"
            "  FROM cost_points GROUP BY session_id"
            ") JOIN session_costs USING (session_id) GROUP BY worker", since or "")
        return {r["worker"]: r["c"] for r in rows}

    def stage_costs(self, since: str | None = None) -> dict[str, float]:
        """What monitoring, input and watchdog sessions that started since a time, or all of them, cost, by stage."""
        rows = self.rows("SELECT stage, SUM(cost_usd) AS c FROM sessions WHERE started_at >= ? GROUP BY stage",
                         since or "")
        return {r["stage"]: r["c"] for r in rows}

    # vahtovik's own deliveries: replies, acknowledgments and notices

    def add_delivery(self, channel: str, subject: str, body: str, origin: str, about: str = "",
                     reply_to: str | None = None) -> int:
        """A message of vahtovik's: a reply to a message from a channel, or a notice, `about` a worker or about
        nothing."""
        now = utc_iso()
        cursor = self.execute(
            "INSERT INTO deliveries (channel, subject, body, origin, about, status, created_at, updated_at, reply_to) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)", channel, subject, body, origin, about, now, now, reply_to)
        return int(cursor.lastrowid)

    def delivery(self, delivery_id: int) -> sqlite3.Row:
        return self.row("SELECT * FROM deliveries WHERE id = ?", delivery_id)

    def update_delivery(self, delivery_id: int, ok: bool, error: str | None, external_id: str | None) -> None:
        now = utc_iso()
        self.execute(
            "UPDATE deliveries SET status = ?, attempts = attempts + 1, last_error = ?, external_id = ?, "
            "updated_at = ?, sent_at = CASE WHEN ? THEN ? ELSE sent_at END WHERE id = ?",
            "sent" if ok else "failed", error, external_id, now, int(ok), now, delivery_id)

    def undelivered(self) -> int:
        """How many replies and notices have not reached their channel. A first short message does not count:
        it is never retried."""
        return self.row("SELECT COUNT(*) AS n FROM deliveries WHERE status != 'sent' "
                        "AND origin != 'acknowledgment'")["n"]

    def retryable_deliveries(self, max_attempts: int) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM deliveries WHERE status IN ('pending', 'failed') AND attempts < ? "
                         "AND origin != 'acknowledgment' ORDER BY id", max_attempts)

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
        """A new session for the channel."""
        self.execute("INSERT OR IGNORE INTO channel_inputs (channel) VALUES (?)", channel)
        self.execute("UPDATE channel_inputs SET session_id = ? WHERE channel = ?", session_id, channel)

    def clear_input_session(self, channel: str) -> None:
        """The person asked to start over: the next answer starts a new session, without the earlier messages."""
        self.execute("UPDATE channel_inputs SET session_id = NULL, awaits_answer = 0, cleared_at = ? "
                     "WHERE channel = ?", utc_iso(), channel)

    def input_answered(self, channel: str, awaits_answer: bool) -> None:
        """The channel's session answered; `awaits_answer`: its last reply waits for the person's answer."""
        self.execute("UPDATE channel_inputs SET answered_at = ?, awaits_answer = ? WHERE channel = ?",
                     utc_iso(), int(awaits_answer), channel)

    def add_channel_message(self, channel: str, message_id: str, text: str, sender: str, sent_at: str) -> bool:
        """Store a message from the channel. Returns False if it is known already."""
        cursor = self.execute(
            "INSERT OR IGNORE INTO channel_messages (channel, id, text, sender, sent_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", channel, message_id, text, sender, sent_at, utc_iso())
        return cursor.rowcount == 1

    def waiting_messages(self, channel: str, max_attempts: int) -> list[sqlite3.Row]:
        """The channel's messages that wait for an answer, the oldest first."""
        return self.rows("SELECT * FROM channel_messages WHERE channel = ? AND status = 'new' AND attempts < ? "
                         "ORDER BY received_at, rowid", channel, max_attempts)

    def channel_history(self, channel: str, last: int, since: str | None = None,
                        leave_out: list[str] = ()) -> list[tuple[str, str, sqlite3.Row]]:
        """The channel's last `last` messages (0: all) received after `since`, without `leave_out`, and
        vahtovik's replies since the first of them, oldest first: (time, "message" or "reply", row)."""
        messages = self.rows(
            "SELECT * FROM (SELECT rowid AS n, * FROM channel_messages WHERE channel = ? "
            "AND (? IS NULL OR received_at > ?) AND id NOT IN (SELECT value FROM json_each(?)) "
            "ORDER BY received_at DESC, n DESC LIMIT ?) ORDER BY received_at, n",
            channel, since, since, json.dumps(list(leave_out)), last if last > 0 else -1)
        # By time, not by reply_to: one reply may answer several messages, and it names only the first.
        # Without the agent's first short messages: only the replies matter later.
        replies = self.rows(
            "SELECT * FROM deliveries WHERE channel = ? AND reply_to IS NOT NULL "
            "AND origin != 'acknowledgment' AND created_at >= ? ORDER BY id",
            channel, messages[0]["received_at"]) if messages else []
        items = [(m["received_at"], "message", m) for m in messages] + [(r["created_at"], "reply", r) for r in replies]
        return sorted(items, key=lambda item: (item[0], item[1] == "reply"))

    def message_counts(self, channel: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.rows(
            "SELECT status, COUNT(*) AS n FROM channel_messages WHERE channel = ? GROUP BY status", channel)}

    def channel_messages_failed(self, channel: str, ids: list[str], error: str) -> None:
        self.db.executemany("UPDATE channel_messages SET attempts = attempts + 1, last_error = ? "
                            "WHERE channel = ? AND id = ?", [(error, channel, i) for i in ids])

    def set_channel_messages(self, channel: str, ids: list[str], status: str) -> None:
        """Mark messages `answered`, or `failed` when vahtovik gave up on them."""
        self.db.executemany("UPDATE channel_messages SET status = ?, answered_at = ? WHERE channel = ? AND id = ?",
                            [(status, utc_iso(), channel, i) for i in ids])
