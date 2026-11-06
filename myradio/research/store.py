"""The research extension's own SQLite file (``research.sqlite3`` in the extension's directory).

Nothing here is secret: it holds researcher settings, briefs, a list of what has already been used, per-source cache
validators and a per-day request counter. Credentials are never stored (the fetcher sends none).

Retention is enforced on every committed run, so the file cannot grow without bound:
newest 30 ready briefs and 10 other outcomes per researcher, used-item memory for 60 days or 2,000 items, request
counters for 30 days.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import threading
from typing import Optional

from ..extensions import ExtensionError, migrate
from .selection import SeenIndex

KEEP_READY = 30
KEEP_OTHER = 10
SEEN_SECONDS = 60 * 86400
SEEN_MAX = 2000
SEEN_SIGNATURES = 500
USAGE_DAYS = 30

SCHEMA_V1 = """
CREATE TABLE researchers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    spec TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    next_run_ts REAL,
    last_run_ts REAL,
    last_outcome TEXT,
    last_message TEXT,
    last_brief_id TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE briefs (
    id TEXT PRIMARY KEY,
    researcher_id TEXT NOT NULL REFERENCES researchers(id) ON DELETE CASCADE,
    created_ts REAL NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ready', 'nothing_new', 'error')),
    review TEXT CHECK (review IS NULL OR review IN ('pending', 'approved', 'rejected', 'auto')),
    reviewed_ts REAL,
    aired_ts REAL,
    fresh_until_ts REAL,
    body TEXT NOT NULL
);
CREATE INDEX briefs_by_researcher ON briefs(researcher_id, created_ts DESC);
CREATE INDEX briefs_airable ON briefs(outcome, review, aired_ts);
CREATE TABLE seen (
    researcher_id TEXT NOT NULL REFERENCES researchers(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    title_sig TEXT NOT NULL,
    used_ts REAL NOT NULL,
    brief_id TEXT,
    PRIMARY KEY (researcher_id, item_key)
);
CREATE INDEX seen_by_time ON seen(researcher_id, used_ts);
CREATE TABLE source_state (
    researcher_id TEXT NOT NULL REFERENCES researchers(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    final_url TEXT,
    feed_url TEXT,
    etag TEXT,
    last_modified TEXT,
    name TEXT,
    format TEXT,
    checked_ts REAL,
    status TEXT,
    message TEXT,
    entries INTEGER,
    PRIMARY KEY (researcher_id, url)
);
CREATE TABLE usage (
    day TEXT PRIMARY KEY,
    requests INTEGER NOT NULL
);
"""
MIGRATIONS = [SCHEMA_V1]

_RESEARCHER_COLUMNS = ("id", "name", "spec", "paused", "version", "created_ts", "updated_ts", "next_run_ts", "last_run_ts",
                       "last_outcome", "last_message", "last_brief_id", "consecutive_errors")


def _researcher(row) -> dict:
    out = {key: row[key] for key in _RESEARCHER_COLUMNS}
    out["spec"] = json.loads(out["spec"])
    out["paused"] = bool(out["paused"])
    return out


def _brief(row) -> dict:
    body = json.loads(row["body"])
    body.update({"id": row["id"], "researcher_id": row["researcher_id"], "created_ts": row["created_ts"], "outcome": row["outcome"],
                 "review": row["review"], "reviewed_ts": row["reviewed_ts"], "aired_ts": row["aired_ts"],
                 "fresh_until_ts": row["fresh_until_ts"]})
    return body


class Store:
    def __init__(self, conn: sqlite3.Connection, steps=None):
        self._conn = conn
        self._lock = threading.RLock()
        self._closed = False
        try:
            conn.execute("PRAGMA synchronous=NORMAL")      # briefs can be regenerated; this avoids a disk flush on every commit
        except sqlite3.Error:
            pass
        migrate(conn, steps if steps is not None else MIGRATIONS)

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            if self._closed:
                raise ExtensionError("The research service is shutting down.")
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _read(self, sql: str, params=()):
        with self._lock:
            if self._closed:
                raise ExtensionError("The research service is shutting down.")
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            finally:
                self._conn.close()

    # ---- researchers ----------------------------------------------------------------------------------

    def count(self) -> int:
        return self._read("SELECT COUNT(*) FROM researchers")[0][0]

    def create(self, rid: str, spec: dict, paused: bool, now: float, next_run: Optional[float], limit: int = 50) -> None:
        with self._tx() as db:
            if db.execute("SELECT COUNT(*) FROM researchers").fetchone()[0] >= limit:        # counted inside the transaction: no race
                raise ExtensionError(f"You can have up to {limit} researchers. Remove one before adding another.")
            db.execute("INSERT INTO researchers(id, name, spec, paused, created_ts, updated_ts, next_run_ts) VALUES (?,?,?,?,?,?,?)",
                       (rid, spec["name"], json.dumps(spec), int(paused), now, now, None if paused else next_run))

    def update(self, rid: str, spec: dict, paused: Optional[bool], now: float, next_run: Optional[float]) -> Optional[dict]:
        """Replace the settings, bump the version, forget cached validators. Returns the new row or ``None``."""
        with self._tx() as db:
            row = db.execute("SELECT paused FROM researchers WHERE id=?", (rid,)).fetchone()
            if row is None:
                return None
            is_paused = bool(row["paused"]) if paused is None else paused
            db.execute("UPDATE researchers SET name=?, spec=?, paused=?, version=version+1, updated_ts=?, next_run_ts=? WHERE id=?",
                       (spec["name"], json.dumps(spec), int(is_paused), now, None if is_paused else next_run, rid))
            db.execute("UPDATE source_state SET etag=NULL, last_modified=NULL WHERE researcher_id=?", (rid,))
            db.execute("DELETE FROM source_state WHERE researcher_id=? AND url NOT IN (%s)" % ",".join("?" * len(spec["sources"])),
                       (rid, *[s["url"] for s in spec["sources"]]))
        return self.get(rid)

    def get(self, rid: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM researchers WHERE id=?", (rid,))
        return _researcher(rows[0]) if rows else None

    def list_all(self) -> list:
        return [_researcher(r) for r in self._read("SELECT * FROM researchers ORDER BY created_ts, id")]

    def delete(self, rid: str) -> bool:
        with self._tx() as db:
            return db.execute("DELETE FROM researchers WHERE id=?", (rid,)).rowcount > 0

    def set_paused(self, rid: str, paused: bool, next_run: Optional[float], now: float) -> bool:
        with self._tx() as db:
            return db.execute("UPDATE researchers SET paused=?, next_run_ts=?, updated_ts=? WHERE id=?",
                              (int(paused), None if paused else next_run, now, rid)).rowcount > 0

    def next_due(self, now: float) -> Optional[dict]:
        rows = self._read("SELECT * FROM researchers WHERE paused=0 AND next_run_ts IS NOT NULL AND next_run_ts<=? "
                          "ORDER BY next_run_ts, id LIMIT 1", (now,))
        return _researcher(rows[0]) if rows else None

    def set_next_run(self, rid: str, ts: Optional[float]) -> None:
        with self._tx() as db:
            db.execute("UPDATE researchers SET next_run_ts=? WHERE id=? AND paused=0", (ts, rid))

    def latest_run_ts(self) -> Optional[float]:
        return self._read("SELECT MAX(last_run_ts) FROM researchers")[0][0]

    # ---- runs ---------------------------------------------------------------------------------------

    def commit_run(self, rid: str, version: int, brief: dict, used: list, states: list, *, now: float,
                   next_run: Optional[float], consecutive_errors: int) -> str:
        """Store a finished run atomically. Returns ``"ok"``, ``"gone"`` (researcher removed) or ``"changed"``
        (settings edited while the run was in flight, so the result is discarded)."""
        with self._tx() as db:
            row = db.execute("SELECT version FROM researchers WHERE id=?", (rid,)).fetchone()
            if row is None:
                return "gone"
            if row["version"] != version:
                return "changed"
            body = {k: v for k, v in brief.items() if k not in ("id", "researcher_id", "created_ts", "outcome", "review",
                                                                  "reviewed_ts", "aired_ts", "fresh_until_ts", "airable")}
            db.execute("INSERT INTO briefs(id, researcher_id, created_ts, outcome, review, fresh_until_ts, body) VALUES (?,?,?,?,?,?,?)",
                       (brief["id"], rid, brief["created_ts"], brief["outcome"], brief["review"], brief["fresh_until_ts"], json.dumps(body)))
            for key, signature in used:
                db.execute("INSERT OR REPLACE INTO seen(researcher_id, item_key, title_sig, used_ts, brief_id) VALUES (?,?,?,?,?)",
                           (rid, key, signature, now, brief["id"]))
            for state in states:
                db.execute("INSERT OR REPLACE INTO source_state(researcher_id, url, final_url, feed_url, etag, last_modified, name, format,"
                           " checked_ts, status, message, entries) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (rid, state["url"], state.get("final_url"), state.get("feed_url"), state.get("etag"), state.get("last_modified"),
                            state.get("name"), state.get("format"), state.get("checked_ts"), state.get("status"), state.get("message"),
                            state.get("entries")))
            db.execute("UPDATE researchers SET last_run_ts=?, last_outcome=?, last_message=?, last_brief_id=?, consecutive_errors=?, "
                       "next_run_ts=CASE WHEN paused=1 THEN NULL WHEN ? IS NULL THEN next_run_ts ELSE ? END WHERE id=?",
                       (now, brief["outcome"], brief["message"], brief["id"], consecutive_errors, next_run, next_run, rid))
            self._prune(db, rid, now)
        return "ok"

    def note_failure(self, rid: str, message: str, now: float, next_run: Optional[float]) -> None:
        """Record a run that crashed before it could make a brief."""
        with self._tx() as db:
            db.execute("UPDATE researchers SET last_run_ts=?, last_outcome='error', last_message=?, consecutive_errors=consecutive_errors+1, "
                       "next_run_ts=CASE WHEN paused=1 THEN NULL WHEN ? IS NULL THEN next_run_ts ELSE ? END WHERE id=?",
                       (now, message, next_run, next_run, rid))

    def _prune(self, db, rid: str, now: float) -> None:
        # "Newest" means last inserted (rowid), not latest by the wall clock: after the computer's clock is set back the brief a run
        # has just made would otherwise sort as the oldest and be deleted in the same transaction that stored it.
        db.execute("DELETE FROM briefs WHERE researcher_id=? AND outcome='ready' AND id NOT IN "
                   "(SELECT id FROM briefs WHERE researcher_id=? AND outcome='ready' ORDER BY rowid DESC LIMIT ?)", (rid, rid, KEEP_READY))
        db.execute("DELETE FROM briefs WHERE researcher_id=? AND outcome!='ready' AND id NOT IN "
                   "(SELECT id FROM briefs WHERE researcher_id=? AND outcome!='ready' ORDER BY rowid DESC LIMIT ?)", (rid, rid, KEEP_OTHER))
        db.execute("DELETE FROM seen WHERE researcher_id=? AND used_ts<?", (rid, now - SEEN_SECONDS))
        db.execute("DELETE FROM seen WHERE researcher_id=? AND item_key NOT IN "
                   "(SELECT item_key FROM seen WHERE researcher_id=? ORDER BY rowid DESC LIMIT ?)", (rid, rid, SEEN_MAX))
        day = int(now // 86400) - USAGE_DAYS
        db.execute("DELETE FROM usage WHERE day<?", (_day(day * 86400),))

    # ---- briefs ---------------------------------------------------------------------------------------

    def get_brief(self, brief_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM briefs WHERE id=?", (brief_id,))
        return _brief(rows[0]) if rows else None

    def list_briefs(self, *, researcher: Optional[str] = None, review: Optional[str] = None, outcome: Optional[str] = None,
                    limit: int = 20, brief_id: Optional[str] = None, airable_at: Optional[float] = None) -> list:
        """Newest first (last stored first). With ``airable_at``, only briefs that may air at that time: the condition is part of the
        query, so the limit applies to the briefs that qualify and not to the newest few of every kind."""
        where, params = [], []
        for column, value in (("researcher_id", researcher), ("review", review), ("outcome", outcome), ("id", brief_id)):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        if airable_at is not None:
            where.append(f"({self._AIRABLE})")
            params.append(airable_at)
        sql = "SELECT * FROM briefs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY rowid DESC LIMIT ?"
        return [_brief(r) for r in self._read(sql, (*params, max(1, min(int(limit), 50))))]

    def decide(self, brief_id: str, decision: str, now: float) -> tuple:
        """The review state machine. Returns ``(changed, brief)`` or raises ``ExtensionError`` with a plain reason.

        ready briefs may move between pending, approved and rejected until they air or go stale; approving
        a stale brief is refused (rejecting is always allowed); aired briefs are final."""
        with self._tx() as db:
            rows = db.execute("SELECT * FROM briefs WHERE id=?", (brief_id,)).fetchall()
            if not rows:
                raise ExtensionError("That update no longer exists. It may have been replaced by a newer run.")
            brief = rows[0]
            if brief["outcome"] != "ready":
                raise ExtensionError("That run found nothing to review.")
            if brief["aired_ts"] is not None:
                raise ExtensionError("That update has already aired, so it can no longer be changed.")
            if decision == "approved" and brief["fresh_until_ts"] is not None and now > brief["fresh_until_ts"]:
                raise ExtensionError("These updates are now older than you asked for, so they can no longer air as new. "
                                     "Run the researcher again to get fresh ones.")
            if brief["review"] == decision:
                return False, _brief(brief)
            db.execute("UPDATE briefs SET review=?, reviewed_ts=? WHERE id=?", (decision, now, brief_id))
            updated = db.execute("SELECT * FROM briefs WHERE id=?", (brief_id,)).fetchone()
        return True, _brief(updated)

    _AIRABLE = ("outcome='ready' AND review IN ('approved','auto') AND aired_ts IS NULL AND (fresh_until_ts IS NULL OR fresh_until_ts>=?)")

    def airable(self, now: float, researcher: Optional[str] = None, limit: int = 20) -> list:
        sql = f"SELECT * FROM briefs WHERE {self._AIRABLE}" + (" AND researcher_id=?" if researcher else "") + " ORDER BY rowid LIMIT ?"
        params = (now, researcher, limit) if researcher else (now, limit)
        return [_brief(r) for r in self._read(sql, params)]

    def mark_aired(self, brief_id: str, now: float) -> bool:
        with self._tx() as db:
            return db.execute(f"UPDATE briefs SET aired_ts=? WHERE id=? AND {self._AIRABLE}", (now, brief_id, now)).rowcount == 1

    def release(self, brief_id: str) -> bool:
        """Undo ``mark_aired``/``claim`` for a brief whose broadcast never happened."""
        with self._tx() as db:
            return db.execute("UPDATE briefs SET aired_ts=NULL WHERE id=? AND aired_ts IS NOT NULL", (brief_id,)).rowcount == 1

    def claim(self, now: float, researcher: Optional[str] = None) -> Optional[dict]:
        for _ in range(3):
            found = self.airable(now, researcher, limit=1)
            if not found:
                return None
            if self.mark_aired(found[0]["id"], now):
                return self.get_brief(found[0]["id"])
        return None

    def pending_counts(self) -> dict:
        return {r[0]: r[1] for r in self._read("SELECT researcher_id, COUNT(*) FROM briefs WHERE outcome='ready' AND review='pending' "
                                                "AND aired_ts IS NULL GROUP BY researcher_id")}

    # ---- memory of what was used ---------------------------------------------------------------------

    def seen_index(self, rid: str) -> SeenIndex:
        rows = self._read("SELECT item_key, title_sig FROM seen WHERE researcher_id=? ORDER BY rowid DESC LIMIT ?", (rid, SEEN_MAX))
        index = SeenIndex(keys={r[0] for r in rows})
        index.signatures = [frozenset(r[1].split()) for r in rows[:SEEN_SIGNATURES] if r[1]]
        return index

    # ---- sources ----------------------------------------------------------------------------------------

    def source_states(self, rid: str) -> dict:
        return {r["url"]: dict(r) for r in self._read("SELECT * FROM source_state WHERE researcher_id=?", (rid,))}

    def all_source_states(self) -> list:
        return [dict(r) for r in self._read("SELECT * FROM source_state")]

    # ---- request counter -----------------------------------------------------------------------------------

    def take_request(self, day: str, limit: int) -> bool:
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO usage(day, requests) VALUES (?, 0)", (day,))
            return db.execute("UPDATE usage SET requests=requests+1 WHERE day=? AND requests<?", (day, limit)).rowcount == 1

    def requests_on(self, day: str) -> int:
        rows = self._read("SELECT requests FROM usage WHERE day=?", (day,))
        return rows[0][0] if rows else 0


def day_of(ts: float) -> str:
    """The UTC calendar day used to count requests."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


_day = day_of
