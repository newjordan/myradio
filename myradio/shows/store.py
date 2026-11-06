"""Persistence: the shows extension keeps everything in its own SQLite file (``shows.sqlite3``).

Nothing here is secret, and nothing here is stored in the station database. The file is opened with
``Context.open_db`` (private, write-ahead log) and migrated with ``PRAGMA user_version``. After every
change the log is folded back into the main file, so copying ``shows.sqlite3`` alone is a complete
backup.

A file that cannot be read as a database is set aside under a new name (never deleted) and a fresh
week is started; the reason is kept for the status line and the doctor check.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..extensions import ExtensionError, migrate
from .model import MAX_SHOWS, Show, ShowError, Style, default_show
from .timeline import check_zone
from .week import Week

DB_FILE = "shows.sqlite3"

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS shows (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        days TEXT NOT NULL,
        start_min INTEGER NOT NULL,
        length_min INTEGER NOT NULL,
        style TEXT NOT NULL,
        host_id TEXT,
        talk TEXT NOT NULL,
        spots_allowed INTEGER NOT NULL,
        updates_allowed INTEGER NOT NULL,
        update_researcher_id TEXT,
        color TEXT NOT NULL,
        clock TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        updated REAL NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS shows_one_default ON shows(is_default) WHERE is_default = 1;
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]

COLUMNS = ("id", "name", "days", "start_min", "length_min", "style", "host_id", "talk", "spots_allowed",
           "updates_allowed", "update_researcher_id", "color", "clock", "is_default")


@dataclass(frozen=True)
class Loaded:
    week: Week
    revision: int
    seed: int
    zone_source: str               # detected | fallback | chosen | configured
    skipped: int                   # saved shows that could not be read (or were over the limit)
    problems: tuple                # plain-language notes about anything unusual found while loading


def set_aside(directory: Path, now: float) -> str:
    """Move a damaged database (and its log files) out of the way. Returns the new file name."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    target, count = f"{DB_FILE}.damaged-{stamp}", 1
    while (directory / target).exists():
        count += 1
        target = f"{DB_FILE}.damaged-{stamp}-{count}"
    for suffix in ("", "-wal", "-shm"):
        source = directory / (DB_FILE + suffix)
        if source.exists() and not source.is_symlink():
            os.replace(source, directory / (target + suffix))
    return target


SQLITE_HEADER = b"SQLite format 3\x00"
MAX_FILE_BYTES = 64 * 1024 * 1024        # 200 shows take well under a megabyte; anything this big is not ours


def _plainly_not_a_database(path: Path) -> bool:
    """True when the file (or its log) cannot be a shows database: it does not start like an SQLite file, or it is
    absurdly large, which would make starting the station slow."""
    try:
        log = os.lstat(f"{path}-wal")
        if stat.S_ISREG(log.st_mode) and log.st_size > MAX_FILE_BYTES:
            return True
    except OSError:
        pass
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return False                      # missing, a link or something odd: let the normal open report it
        if info.st_size > MAX_FILE_BYTES:
            return True
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    return bool(head) and head != SQLITE_HEADER


def open_database(ctx, now: float):
    """Return ``(connection, note)``; ``note`` says so when a damaged file had to be set aside."""
    note: Optional[str] = None
    if _plainly_not_a_database(ctx.state_dir / DB_FILE):
        note = _set_aside_note(set_aside(ctx.state_dir, now))
    try:
        conn = ctx.open_db(DB_FILE)
        try:
            migrate(conn, MIGRATIONS)
            verdict = conn.execute("PRAGMA quick_check").fetchone()
            if not verdict or verdict[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
        except BaseException:
            conn.close()
            raise
    except sqlite3.OperationalError:
        raise ExtensionError("The shows file could not be opened right now. Check that the disk has free space "
                             "and that myradio may write to its data folder, then restart myradio.") from None
    except sqlite3.DatabaseError:
        note = _set_aside_note(set_aside(ctx.state_dir, now))
        conn = ctx.open_db(DB_FILE)
        migrate(conn, MIGRATIONS)
    return conn, note


def _set_aside_note(name: str) -> str:
    return (f"Your saved shows could not be read, so a new empty week was started. The old file was kept as "
            f"{name}; restore it from a backup if you need those shows.")


def _row_to_show(row) -> Show:
    style = json.loads(row["style"])
    return Show(
        id=row["id"], name=row["name"], days=row["days"], start_min=row["start_min"], length_min=row["length_min"],
        style=Style(tuple(style["palettes"]), tuple(style["tags"]), style["energy"], style["adventurousness"],
                    style["vocals"]),
        host_id=row["host_id"], talk=row["talk"], spots_allowed=bool(row["spots_allowed"]),
        updates_allowed=bool(row["updates_allowed"]), update_researcher_id=row["update_researcher_id"],
        color=row["color"], clock=row["clock"], is_default=bool(row["is_default"]))


def _show_to_row(show: Show) -> tuple:
    return (show.id, show.name, show.days, show.start_min, show.length_min, json.dumps(show.style.to_dict()),
            show.host_id, show.talk, int(show.spots_allowed), int(show.updates_allowed), show.update_researcher_id,
            show.color, show.clock, int(show.is_default))


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- start-up ----------------------------------------------------------------------------------
    def bootstrap(self, first_zone, now: float) -> None:
        """Create what a brand-new file needs: the default show, the time zone, the plan seed and the first
        revision. Anything already saved is kept. ``first_zone()`` returns ``(zone, source)`` and is only
        called when no time zone has been saved yet."""
        def action():
            if self.conn.execute("SELECT 1 FROM shows WHERE is_default = 1").fetchone() is None:
                self._upsert(default_show(), now)
            if self.conn.execute("SELECT 1 FROM settings WHERE key = 'timezone'").fetchone() is None:
                timezone, source = first_zone()
                for key, value in (("timezone", timezone), ("zone_source", source)):
                    self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
            for key, value in (("zone_source", "configured"), ("plan_seed", str(secrets.randbits(62))), ("revision", "1")):
                self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self._write(action)

    def load(self) -> Loaded:
        settings = {row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM settings")}
        problems = []
        try:
            timezone = check_zone(settings.get("timezone", ""))
        except ShowError:
            timezone = "UTC"
            problems.append(f"The saved time zone ({str(settings.get('timezone', ''))[:40]}) is not available on this "
                            "computer, so shows follow UTC until you choose a time zone.")
        try:
            seed = int(settings["plan_seed"])
            revision = int(settings["revision"])
        except (KeyError, ValueError):
            seed, revision = secrets.randbits(62), 1
        total = self.conn.execute("SELECT COUNT(*) FROM shows WHERE is_default = 0").fetchone()[0]
        rows = self.conn.execute("SELECT * FROM shows WHERE is_default = 0 ORDER BY created, id LIMIT ?",
                                 (MAX_SHOWS,)).fetchall()
        shows, unreadable = [], 0
        for row in rows:
            try:
                shows.append(_row_to_show(row))
            except Exception:
                unreadable += 1
        default = None
        row = self.conn.execute("SELECT * FROM shows WHERE is_default = 1").fetchone()
        if row is not None:
            try:
                default = _row_to_show(row)
            except Exception:
                problems.append("The saved default show could not be read, so the built-in one is used.")
        over = max(0, total - MAX_SHOWS)
        if unreadable:
            problems.append(f"{unreadable} saved show{'s' if unreadable != 1 else ''} could not be read and "
                            f"{'were' if unreadable != 1 else 'was'} left out. Add "
                            f"{'them' if unreadable != 1 else 'it'} again if you need {'them' if unreadable != 1 else 'it'}.")
        if over:
            problems.append(f"{over} saved show{'s' if over != 1 else ''} {'were' if over != 1 else 'was'} left out because "
                            f"a week holds at most {MAX_SHOWS} shows.")
        skipped = unreadable + over
        week = Week(timezone, shows, default or default_show())
        return Loaded(week, revision, seed, settings.get("zone_source", "configured"), skipped, tuple(problems))

    # -- changes (each one is a single transaction and ends with a new revision) -----------------------
    def _upsert(self, show: Show, now: float) -> None:
        names = ", ".join(COLUMNS)
        marks = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("id", "is_default"))
        self.conn.execute(
            f"INSERT INTO shows({names}, created, updated) VALUES ({marks}, ?, ?) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}, updated=excluded.updated",
            _show_to_row(show) + (now, now))

    def _bump(self) -> int:
        self.conn.execute("INSERT INTO settings(key, value) VALUES ('revision', '2') "
                          "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
        return int(self.conn.execute("SELECT value FROM settings WHERE key = 'revision'").fetchone()[0])

    def _write(self, action):
        """Run ``action`` as one transaction, then fold the log into the main file so that a plain copy
        of ``shows.sqlite3`` is a complete backup."""
        with self.conn:
            result = action()
        self.checkpoint()
        return result

    def save_show(self, show: Show, now: float) -> int:
        def action():
            self._upsert(show, now)
            return self._bump()
        return self._write(action)

    def delete_show(self, show_id: str) -> int:
        def action():
            self.conn.execute("DELETE FROM shows WHERE id = ? AND is_default = 0", (show_id,))
            return self._bump()
        return self._write(action)

    def replace_shows(self, shows: Sequence[Show], now: float) -> int:
        def action():
            self.conn.execute("DELETE FROM shows WHERE is_default = 0")
            for index, show in enumerate(shows):
                self._upsert(show, now + index / 1000.0)      # keeps their order stable when re-read
            return self._bump()
        return self._write(action)

    def set_timezone(self, timezone: str, source: str) -> int:
        def action():
            for key, value in (("timezone", timezone), ("zone_source", source)):
                self.conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            return self._bump()
        return self._write(action)

    def checkpoint(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        self.checkpoint()
        self.conn.close()
