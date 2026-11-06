"""Durable job and provenance storage, in the extension's own SQLite file.

Nothing here talks to a worker or to the station. All methods are short transactions behind one lock;
callers must never hold that lock (or call into the store) while running a worker or importing audio.

Job states: ``queued`` -> ``running`` -> ``succeeded`` | ``failed`` | ``cancelled`` | ``expired`` | ``interrupted``.
A failed attempt that may be retried goes back to ``queued`` with a later ``not_before``. After an unexpected
stop, running jobs become ``interrupted`` and are put back in line up to a limit; beyond it they stay
``interrupted`` until the operator retries them.

``jobs.fixture`` records whether the maker that ran the job (or, while it waits, the maker it was queued for)
only makes test tones. It is written when the job is queued and again when an attempt starts, so a job keeps
saying so after the test maker is removed from the settings; a finished job's flag cannot be cleared. The column
is added to a database made by an earlier version in place (see ``_ensure_fixture_column``).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from typing import Any, Callable, Iterable, Optional, Sequence

from ..extensions import migrate
from .errors import GenerationError, message_for
from .payloads import ROLES, Normalized

STATES = ("queued", "running", "succeeded", "failed", "cancelled", "expired", "interrupted")
TERMINAL = ("succeeded", "failed", "cancelled", "expired", "interrupted")
RETRYABLE_STATES = ("failed", "cancelled", "expired", "interrupted")
HISTORY_LIMIT = 2000
PROVENANCE_LIMIT = 5000
ATTEMPT_LOG_LIMIT = 8

SCHEMA = ["""
BEGIN;
CREATE TABLE IF NOT EXISTS jobs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('song','host_break','id','spot','update')),
  state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','expired','interrupted')),
  priority INTEGER NOT NULL,
  payload TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  est_seconds REAL NOT NULL DEFAULT 0,
  idempotency_key TEXT,
  resource TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  seed INTEGER NOT NULL,
  seed_pinned INTEGER NOT NULL DEFAULT 0,
  ttl_s INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  not_before REAL NOT NULL,
  expires_at REAL,
  started_at REAL,
  finished_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  interruptions INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error TEXT,
  detail TEXT,
  attempt_log TEXT NOT NULL DEFAULT '[]',
  track_id TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  note TEXT
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(state, role, priority DESC, not_before, seq);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_key ON jobs(idempotency_key)
  WHERE idempotency_key IS NOT NULL AND state IN ('queued','running','succeeded','interrupted');
CREATE TABLE IF NOT EXISTS provenance(
  track_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  worker_kind TEXT NOT NULL,
  worker_version TEXT,
  worker_hash TEXT,
  fixture INTEGER NOT NULL CHECK(fixture IN (0,1)),
  prompt_id TEXT,
  seed INTEGER,
  params TEXT NOT NULL DEFAULT '{}',
  timings TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 1,
  validation TEXT NOT NULL DEFAULT '[]',
  audio_sha256 TEXT,
  duration_s REAL NOT NULL,
  created_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','aired','gone')),
  aired_at REAL
);
CREATE INDEX IF NOT EXISTS provenance_ready ON provenance(status, role);
CREATE INDEX IF NOT EXISTS provenance_job ON provenance(job_id);
CREATE TRIGGER IF NOT EXISTS provenance_fixture_is_sticky BEFORE UPDATE OF fixture ON provenance
  WHEN OLD.fixture = 1 AND NEW.fixture = 0
  BEGIN SELECT RAISE(ABORT, 'a fixture track cannot be relabelled as real'); END;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
COMMIT;
"""]

_JSON_COLUMNS = ("payload", "attempt_log")
_PROV_JSON = ("params", "timings", "validation")


def _decode(row: sqlite3.Row, json_columns: Iterable[str]) -> dict:
    out = dict(row)
    for column in json_columns:
        try:
            out[column] = json.loads(out[column]) if out.get(column) is not None else None
        except ValueError:
            out[column] = None
    return out


class JobStore:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float], *, history: int = HISTORY_LIMIT):
        self._conn = conn
        self._clock = clock
        self._history = history
        self._lock = threading.RLock()
        self._closed = False
        migrate(conn, SCHEMA)
        self._ensure_fixture_column(conn)
        conn.isolation_level = None  # explicit transactions below

    @staticmethod
    def _ensure_fixture_column(conn: sqlite3.Connection) -> None:
        """Add ``jobs.fixture`` to a database written by an earlier version. Safe to run any number of times: the
        column is added (and filled in from the provenance rows) in one transaction, and only when it is missing,
        so a stop at the wrong moment simply repeats it at the next start."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "fixture" not in columns:
            conn.executescript("BEGIN;\n"
                               "ALTER TABLE jobs ADD COLUMN fixture INTEGER NOT NULL DEFAULT 0;\n"
                               "UPDATE jobs SET fixture=1 WHERE id IN (SELECT job_id FROM provenance WHERE fixture=1);\n"
                               "COMMIT;")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='jobs_fixture_is_sticky'").fetchone() is None:
            conn.executescript("CREATE TRIGGER IF NOT EXISTS jobs_fixture_is_sticky BEFORE UPDATE OF fixture ON jobs\n"
                               "  WHEN OLD.fixture = 1 AND NEW.fixture = 0 AND OLD.state = 'succeeded'\n"
                               "  BEGIN SELECT RAISE(ABORT, 'a fixture job cannot be relabelled as real'); END;")

    # ---- plumbing -----------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            if self._closed:
                raise GenerationError("The generation service is shutting down.")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            if self._closed:
                raise GenerationError("The generation service is shutting down.")
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            if self._closed:
                raise GenerationError("The generation service is shutting down.")
            return self._conn.execute(sql, args).fetchall()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()

    # ---- settings -------------------------------------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, json.dumps(value)))

    # ---- jobs ---------------------------------------------------------------------------------------------------------
    def enqueue(self, norm: Normalized, *, priority: int, ttl_s: Optional[int], key: Optional[str], resource: str,
                worker_id: str, max_attempts: int, seed: int, max_live: int, fixture: bool = False) -> tuple:
        """Insert a job. Returns ``(job, created)``; ``created`` is False when the key already names live or finished work."""
        now = self._clock()
        with self._tx() as conn:
            if key:
                row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=? AND state IN "
                                   "('queued','running','succeeded','interrupted') ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
                if row is not None:
                    if row["fingerprint"] != norm.fingerprint or row["role"] != norm.role:
                        raise GenerationError("That idempotency key was already used for a different request. "
                                              "Use a new key, or send the same request again.")
                    return _decode(row, _JSON_COLUMNS), False
            live = conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if live >= max_live:
                raise GenerationError(f"The queue is full ({max_live} jobs waiting). Wait for some to finish, or cancel some.")
            job_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO jobs(id,role,state,priority,payload,fingerprint,title,summary,est_seconds,idempotency_key,resource,"
                "worker_id,seed,seed_pinned,ttl_s,created_at,updated_at,not_before,expires_at,max_attempts,fixture) "
                "VALUES (?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, norm.role, priority, json.dumps(norm.data, sort_keys=True), norm.fingerprint, norm.title, norm.summary,
                 norm.est_seconds, key, resource, worker_id, seed, int(norm.seed_pinned), ttl_s, now, now, now,
                 now + ttl_s if ttl_s else None, max_attempts, 1 if fixture else 0))
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _decode(row, _JSON_COLUMNS), True

    def get(self, job_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _decode(row, _JSON_COLUMNS) if row else None

    def get_by_track(self, track_id: str) -> Optional[dict]:
        row = self._one("SELECT j.* FROM jobs j JOIN provenance p ON p.job_id=j.id WHERE p.track_id=?", (track_id,))
        return _decode(row, _JSON_COLUMNS) if row else None

    def list_jobs(self, *, state: Optional[str] = None, role: Optional[str] = None, limit: int = 50, before: Optional[int] = None) -> list:
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if role:
            where.append("role=?")
            args.append(role)
        if before is not None:
            where.append("seq<?")
            args.append(int(before))
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY seq DESC LIMIT ?"
        return [_decode(r, _JSON_COLUMNS) for r in self._all(sql, (*args, max(1, min(int(limit), 200))))]

    def counts(self) -> dict:
        out = dict.fromkeys(STATES, 0)
        for row in self._all("SELECT state, COUNT(*) FROM jobs GROUP BY state"):
            out[row[0]] = row[1]
        return out

    def live_by_role(self) -> dict:
        """``{role: {"queued": n, "running": n, "pending_seconds": s}}`` for work still on its way."""
        out = {role: {"queued": 0, "running": 0, "pending_seconds": 0.0} for role in ROLES}
        for row in self._all("SELECT role, state, COUNT(*), COALESCE(SUM(est_seconds),0) FROM jobs "
                             "WHERE state IN ('queued','running') GROUP BY role, state"):
            out[row[0]][row[1]] = row[2]
            out[row[0]]["pending_seconds"] += row[3]
        return out

    def recent_finished(self, limit: int = 5) -> list:
        rows = self._all("SELECT * FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired','interrupted') "
                         "ORDER BY finished_at DESC, seq DESC LIMIT ?", (limit,))
        return [_decode(r, _JSON_COLUMNS) for r in rows]

    def running_jobs(self) -> list:
        return [_decode(r, _JSON_COLUMNS) for r in self._all("SELECT * FROM jobs WHERE state='running' ORDER BY seq")]

    def next_wake(self, roles: Sequence[str], now: float) -> Optional[float]:
        """The earliest future time a queued job of these kinds becomes runnable, or any queued job expires."""
        marks = ",".join("?" * len(roles)) or "''"
        row = self._one(f"SELECT MIN(not_before) FROM jobs WHERE state='queued' AND role IN ({marks}) AND not_before>?", (*roles, now))
        wake = row[0] if row and row[0] is not None else None
        exp = self._one("SELECT MIN(expires_at) FROM jobs WHERE state='queued' AND expires_at IS NOT NULL AND expires_at>?", (now,))
        if exp and exp[0] is not None:
            wake = exp[0] if wake is None else min(wake, exp[0])
        return wake

    def candidates(self, roles: Sequence[str], now: float, limit: int = 300, *, allowed: Optional[Sequence[str]] = None,
                   urgent_priority: Optional[int] = None) -> list:
        """Queued jobs of these kinds that may run now, best first (priority, then oldest).

        ``allowed`` and ``urgent_priority`` apply the buffer policy *inside the query*: only jobs of the kinds in
        ``allowed``, or whose priority is at least ``urgent_priority``, are returned. That has to happen before the
        ``limit``, otherwise a long line of waiting jobs that the policy will not run could fill the whole window
        and hide a job that it would run. Leave ``allowed`` out to get every waiting job of the kinds.
        """
        if not roles:
            return []
        marks = ",".join("?" * len(roles))
        extra, args = "", [*roles, now]
        if allowed is not None:
            ok = ",".join("?" * len(allowed))
            if urgent_priority is None:
                extra = f" AND role IN ({ok})" if allowed else " AND 0"
                args += list(allowed)
            else:
                extra = f" AND (role IN ({ok}) OR priority>=?)" if allowed else " AND priority>=?"
                args += list(allowed) + [urgent_priority]
        rows = self._all(f"SELECT * FROM jobs WHERE state='queued' AND role IN ({marks}) AND not_before<=?{extra} "
                         "ORDER BY priority DESC, not_before ASC, seq ASC LIMIT ?", (*args, limit))
        return [_decode(r, _JSON_COLUMNS) for r in rows]

    def fail_unroutable(self, routable: Sequence[str], now: float) -> int:
        """Fail waiting jobs whose kind of audio has no maker any more, so they do not wait forever."""
        marks = ",".join("?" * len(routable)) or "''"
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE jobs SET state='failed', updated_at=?, finished_at=?, error_code='worker_missing', error=? "
                               f"WHERE state='queued' AND role NOT IN ({marks})", (now, now, message_for("worker_missing"), *routable))
            if cur.rowcount:
                self._prune_locked(conn)
            return cur.rowcount

    def start(self, job_id: str, now: float, *, worker_id: str, resource: str, seed_used: int,
              fixture: Optional[bool] = None) -> Optional[dict]:
        """Move a queued job to running (counting an attempt). None when it is no longer queued.
        ``fixture`` says whether the maker that is about to run it only makes test tones (left as it was when None)."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND state='queued'", (job_id,)).fetchone()
            if row is None:
                return None
            log = json.loads(row["attempt_log"] or "[]")
            attempt = row["attempts"] + 1
            log.append({"n": attempt, "started_at": now, "worker": worker_id, "seed": seed_used})
            flag = row["fixture"] if fixture is None else (1 if fixture else 0)
            conn.execute("UPDATE jobs SET state='running', attempts=?, started_at=?, updated_at=?, worker_id=?, resource=?, fixture=?, "
                         "error_code=NULL, error=NULL, detail=NULL, cancel_requested=0, attempt_log=? WHERE id=?",
                         (attempt, now, now, worker_id, resource, flag, json.dumps(log[-ATTEMPT_LOG_LIMIT:]), job_id))
            return _decode(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), _JSON_COLUMNS)

    @staticmethod
    def _close_attempt(row: sqlite3.Row, now: float, outcome: str, code: Optional[str], message: Optional[str]) -> str:
        log = json.loads(row["attempt_log"] or "[]")
        if log:
            log[-1].update({"ended_at": now, "outcome": outcome, "code": code, "message": (message or "")[:200]})
        return json.dumps(log[-ATTEMPT_LOG_LIMIT:])

    def finish_success(self, job_id: str, now: float, track_id: str, prov: dict) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise GenerationError("That job no longer exists.")
            conn.execute("UPDATE jobs SET state='succeeded', finished_at=?, updated_at=?, track_id=?, error_code=NULL, error=NULL, "
                         "detail=NULL, fixture=?, attempt_log=? WHERE id=?",
                         (now, now, track_id, 1 if prov["fixture"] else 0, self._close_attempt(row, now, "ok", None, None), job_id))
            conn.execute(
                "INSERT INTO provenance(track_id,job_id,role,kind,title,worker_id,worker_kind,worker_version,worker_hash,fixture,"
                "prompt_id,seed,params,timings,attempts,validation,audio_sha256,duration_s,created_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ready')",
                (track_id, job_id, prov["role"], prov["kind"], prov["title"], prov["worker_id"], prov["worker_kind"],
                 prov.get("worker_version"), prov.get("worker_hash"), 1 if prov["fixture"] else 0, prov.get("prompt_id"),
                 prov.get("seed"), json.dumps(prov.get("params", {})), json.dumps(prov.get("timings", {})),
                 int(prov.get("attempts", 1)), json.dumps(prov.get("validation", [])), prov.get("audio_sha256"),
                 float(prov["duration_s"]), now))
            self._prune_locked(conn)

    def finish_failure(self, job_id: str, now: float, *, code: str, message: str, detail: str, retry_at: Optional[float]) -> str:
        """Record a failed attempt. Returns the new state: ``queued`` (will retry), ``failed`` or ``expired``."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
            if row is None:
                return "gone"
            log = self._close_attempt(row, now, "failed", code, message)
            if retry_at is not None and row["expires_at"] is not None and retry_at >= row["expires_at"]:
                state, code, message = "expired", "expired", message_for("expired")
                retry_at = None
            elif retry_at is not None:
                state = "queued"
            else:
                state = "failed"
            conn.execute("UPDATE jobs SET state=?, updated_at=?, finished_at=?, not_before=?, error_code=?, error=?, detail=?, "
                         "attempt_log=? WHERE id=?",
                         (state, now, None if state == "queued" else now, retry_at if retry_at is not None else row["not_before"],
                          code, message, detail[:1500], log, job_id))
            if state != "queued":
                self._prune_locked(conn)
            return state

    def finish_cancelled(self, job_id: str, now: float, *, requeue: bool, note: str = "") -> str:
        """A running job stopped because of a cancel. ``requeue`` puts it back in line without using up an attempt."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
            if row is None:
                return "gone"
            log = self._close_attempt(row, now, "requeued" if requeue else "cancelled", "cancelled", note or message_for("cancelled"))
            if requeue:
                conn.execute("UPDATE jobs SET state='queued', updated_at=?, not_before=?, attempts=MAX(0, attempts-1), cancel_requested=0, "
                             "note=?, attempt_log=? WHERE id=?", (now, now, note[:200], log, job_id))
                return "queued"
            conn.execute("UPDATE jobs SET state='cancelled', updated_at=?, finished_at=?, cancel_requested=0, error_code='cancelled', "
                         "error=?, detail=NULL, attempt_log=? WHERE id=?", (now, now, message_for("cancelled"), log, job_id))
            self._prune_locked(conn)
            return "cancelled"

    def finish_expired(self, job_id: str, now: float) -> str:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
            if row is None:
                return "gone"
            conn.execute("UPDATE jobs SET state='expired', updated_at=?, finished_at=?, error_code='expired', error=?, detail=NULL, "
                         "attempt_log=? WHERE id=?",
                         (now, now, "It finished too late to be useful, so it was thrown away.",
                          self._close_attempt(row, now, "expired", "expired", "finished too late"), job_id))
            self._prune_locked(conn)
            return "expired"

    def cancel_queued(self, job_id: str, now: float) -> bool:
        with self._tx() as conn:
            cur = conn.execute("UPDATE jobs SET state='cancelled', updated_at=?, finished_at=?, error_code='cancelled', error=? "
                               "WHERE id=? AND state='queued'", (now, now, message_for("cancelled"), job_id))
            if cur.rowcount:
                self._prune_locked(conn)
            return bool(cur.rowcount)

    def request_cancel(self, job_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=? AND state='running'", (job_id,))
            return bool(cur.rowcount)

    def expire_due(self, now: float) -> list:
        with self._tx() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE state='queued' AND expires_at IS NOT NULL AND expires_at<=?", (now,))]
            if ids:
                conn.executemany("UPDATE jobs SET state='expired', updated_at=?, finished_at=?, error_code='expired', error=?, detail=NULL "
                                 "WHERE id=? AND state='queued'", [(now, now, message_for("expired"), i) for i in ids])
                self._prune_locked(conn)
            return ids

    def retry(self, job_id: str, now: float) -> dict:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise GenerationError("That job was not found. It may be old enough to have been cleared from the history.")
            if row["state"] not in RETRYABLE_STATES:
                raise GenerationError("Only a job that failed, was cancelled, ran out of time or was interrupted can be retried."
                                      if row["state"] != "succeeded" else "That job already succeeded; there is nothing to retry.")
            try:
                conn.execute("UPDATE jobs SET state='queued', attempts=0, interruptions=0, not_before=?, updated_at=?, finished_at=NULL, "
                             "expires_at=?, error_code=NULL, error=NULL, detail=NULL, cancel_requested=0, note='Retried by the operator.' "
                             "WHERE id=?", (now, now, now + row["ttl_s"] if row["ttl_s"] else None, job_id))
            except sqlite3.IntegrityError:
                raise GenerationError("A newer job already uses the same idempotency key, so this one was not queued again.") from None
            return _decode(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), _JSON_COLUMNS)

    def recover(self, now: float, max_requeues: int) -> dict:
        """After a restart: running jobs become interrupted and are put back in line up to ``max_requeues`` times."""
        requeued = parked = 0
        with self._tx() as conn:
            for row in conn.execute("SELECT * FROM jobs WHERE state='running'").fetchall():
                interruptions = row["interruptions"] + 1
                log = self._close_attempt(row, now, "interrupted", "interrupted", message_for("interrupted"))
                if interruptions <= max_requeues:
                    conn.execute("UPDATE jobs SET state='queued', interruptions=?, attempts=MAX(0, attempts-1), not_before=?, updated_at=?, "
                                 "cancel_requested=0, note=?, attempt_log=? WHERE id=?",
                                 (interruptions, now, now, "Put back in line after the station stopped unexpectedly.", log, row["id"]))
                    requeued += 1
                else:
                    conn.execute("UPDATE jobs SET state='interrupted', interruptions=?, updated_at=?, finished_at=?, error_code='interrupted', "
                                 "error=?, cancel_requested=0, attempt_log=? WHERE id=?",
                                 (interruptions, now, now, message_for("interrupted") + " It was tried again several times; retry it when you are ready.",
                                  log, row["id"]))
                    parked += 1
            self._prune_locked(conn)
        return {"requeued": requeued, "interrupted": parked}

    def _prune_locked(self, conn: sqlite3.Connection) -> None:
        # Keep the most recently *finished* jobs, so a job that was created long ago and has only just finished is not the one thrown away.
        conn.execute("DELETE FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired','interrupted') AND seq NOT IN "
                     "(SELECT seq FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired','interrupted') "
                     "ORDER BY COALESCE(finished_at, updated_at) DESC, seq DESC LIMIT ?)", (self._history,))
        conn.execute("DELETE FROM provenance WHERE track_id NOT IN (SELECT track_id FROM provenance ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                     (PROVENANCE_LIMIT,))

    # ---- provenance and the ready pool ------------------------------------------------------------------------------
    def provenance(self, track_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM provenance WHERE track_id=?", (track_id,))
        return self._prov(row) if row else None

    def provenance_for_job(self, job_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM provenance WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job_id,))
        return self._prov(row) if row else None

    @staticmethod
    def _prov(row: sqlite3.Row) -> dict:
        out = _decode(row, _PROV_JSON)
        out["fixture"] = bool(out["fixture"])
        return out

    def ready_level(self, role: str, include_fixture: bool = True) -> dict:
        """What is made and waiting to be played. ``include_fixture=False`` leaves out test tones, which are never
        audio a listener should be counted on to hear (leftovers must not hold back a real maker)."""
        row = self._one("SELECT COUNT(*), COALESCE(SUM(duration_s),0), MIN(created_at) FROM provenance WHERE role=? AND status='ready'"
                        + ("" if include_fixture else " AND fixture=0"), (role,))
        return {"items": row[0], "seconds": float(row[1]), "oldest": row[2]}

    def ready_track_ids(self) -> list:
        return [r[0] for r in self._all("SELECT track_id FROM provenance WHERE status='ready'")]

    def mark_aired(self, track_id: str, at: float) -> bool:
        with self._tx() as conn:
            cur = conn.execute("UPDATE provenance SET status='aired', aired_at=? WHERE track_id=? AND status='ready'", (at, track_id))
            return bool(cur.rowcount)

    def mark_gone(self, track_ids: Iterable[str]) -> int:
        ids = [(t,) for t in track_ids]
        if not ids:
            return 0
        with self._tx() as conn:
            before = conn.total_changes
            conn.executemany("UPDATE provenance SET status='gone' WHERE track_id=? AND status='ready'", ids)
            changed = conn.total_changes - before
            # The audio no longer exists, so its idempotency key may be used to make it again.
            conn.executemany("UPDATE jobs SET idempotency_key=NULL WHERE track_id=?", ids)
            return changed
