"""The prompt library's own SQLite file: a person's styles, favourites and repeat memory.

Nothing here touches the station database. The file holds three small tables:

* ``user_presets``  validated copies of a person's own styles (see ``userpresets.py``)
* ``favorites``     styles and presets marked as favourites
* ``history``       the prompts composed recently, so a station does not repeat itself

Everything is bounded: at most 200 saved styles, 500 favourites and 1,000 remembered prompts (the
oldest are forgotten first). Reads of the saved styles are served from memory and take no lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from .library import Preset, SongPrompt
from .userpresets import USER_PREFIX, UserPresetRecord, digest_of

MAX_USER_PRESETS = 200
MAX_FAVORITES = 500
MAX_HISTORY = 1000

# Each step is a tuple of statements. They use IF NOT EXISTS so a file left half-built by an older
# version (some tables made, the version number not yet written) can still be finished, and they run
# in one transaction together with the version number, so a new file can no longer be left that way.
_MIGRATIONS = (
    (
        """
        CREATE TABLE IF NOT EXISTS user_presets(
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            based_on TEXT,
            palette TEXT NOT NULL,
            arrangement TEXT NOT NULL,
            digest TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created REAL NOT NULL,
            updated REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS favorites(
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS history(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            preset_id TEXT NOT NULL,
            style_key TEXT NOT NULL,
            arrangement_id TEXT NOT NULL,
            seed INTEGER NOT NULL,
            complexity TEXT NOT NULL,
            density TEXT NOT NULL,
            mode TEXT NOT NULL,
            title TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            aired REAL
        )
        """,
    ),
)

_TROUBLE = ("Your saved styles could not be read or written just now. Nothing was changed. "
            "Check that the disk has room and the data folder can be written to, then try again.")


class StoreError(ValueError):
    """A failure with a message written for the person using the studio."""


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(_MIGRATIONS):
        raise StoreError("The saved styles were written by a newer myradio. Update myradio before opening them.")
    for index in range(current, len(_MIGRATIONS)):
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _MIGRATIONS[index]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={index + 1}")
            conn.execute("COMMIT")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise


class PromptStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], float] = time.time):
        self._conn = conn
        self._clock = clock
        self._lock = threading.RLock()
        self._presets: tuple[Preset, ...] = ()
        self._rows: dict[str, dict] = {}
        self._damaged: tuple[str, ...] = ()
        self._favorites: dict[str, str] = {}
        try:
            with self._lock:
                self._conn.row_factory = sqlite3.Row
                _migrate(self._conn)
                self._reload()
        except sqlite3.Error:
            self._discard()
            raise StoreError(_TROUBLE) from None
        except BaseException:
            self._discard()  # a store that cannot be used must not leave its connection open
            raise

    def _discard(self) -> None:
        """Close the connection after a failed start (the store owns the connection it was given)."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    @classmethod
    def open(cls, path: str | os.PathLike, *, clock: Callable[[], float] = time.time) -> PromptStore:
        """Open (and create, with owner-only permissions) a store file outside the extension host."""
        path = Path(path)
        if path.is_symlink():
            raise StoreError("The saved-styles file must not be a symbolic link.")
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
        except OSError:
            raise StoreError(_TROUBLE) from None
        conn = None
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            raise StoreError(_TROUBLE) from None
        return cls(conn, clock=clock)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            except sqlite3.Error:
                pass

    def exclusive(self) -> threading.RLock:
        """A lock to hold while choosing from the repeat memory and recording the choice.

        Every other method of this store takes the same lock for its own short work, so holding it
        around "read what was used recently, pick, record" means two requests cannot both read the
        same history and pick the same preset. It can be taken again by the thread that holds it.
        """
        return self._lock

    def checkpoint(self) -> None:
        """Fold the write-ahead log into the main file so a plain copy of it is complete."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    # ---- saved styles --------------------------------------------------------------------
    def _reload(self) -> None:
        presets, rows, damaged = [], {}, []
        for row in self._conn.execute("SELECT * FROM user_presets ORDER BY slug"):
            record = UserPresetRecord(
                slug=row["slug"], title=row["title"], based_on=row["based_on"],
                palette=_loads(row["palette"]), arrangement=_loads(row["arrangement"]), digest=row["digest"],
            )
            try:
                preset = record.preset()
                if preset.digest != _digest(record):
                    raise ValueError("content does not match its digest")
            except (ValueError, TypeError, KeyError):
                damaged.append(row["slug"])
                continue
            presets.append(preset)
            rows[USER_PREFIX + row["slug"]] = {
                "id": USER_PREFIX + row["slug"], "slug": row["slug"], "title": row["title"],
                "based_on": row["based_on"], "digest": row["digest"], "revision": row["revision"],
                "created": row["created"], "updated": row["updated"],
            }
        self._presets, self._rows, self._damaged = tuple(presets), rows, tuple(damaged)
        self._favorites = {r["id"]: r["kind"] for r in self._conn.execute("SELECT id, kind FROM favorites")}

    @property
    def damaged(self) -> tuple[str, ...]:
        """Slugs of saved styles that could not be read back (left in the file, never deleted)."""
        return self._damaged

    def user_presets(self) -> tuple[Preset, ...]:
        return self._presets

    def user_row(self, preset_id: str) -> dict | None:
        return self._rows.get(preset_id)

    def get_user_preset(self, preset_id: str) -> Preset | None:
        for preset in self._presets:
            if preset.id == preset_id:
                return preset
        return None

    def save_user_preset(self, record: UserPresetRecord) -> dict:
        """Insert or replace a saved style; returns its row (with ``created`` true for a new one)."""
        try:  # a record that could not be read back is refused before the file is touched
            if record.preset().digest != _digest(record):
                raise ValueError("content does not match its digest")
        except (ValueError, TypeError, KeyError):
            raise StoreError("This style could not be saved because it does not follow the style rules. Nothing was changed.") from None
        now = self._clock()
        with self._lock:
            try:
                existing = self._conn.execute("SELECT revision, digest, created FROM user_presets WHERE slug=?", (record.slug,)).fetchone()
                if existing is None:
                    count = self._conn.execute("SELECT COUNT(*) FROM user_presets").fetchone()[0]
                    if count >= MAX_USER_PRESETS:
                        raise StoreError(f"You have {MAX_USER_PRESETS} saved styles, which is the most. Remove one you no longer use, then save this one.")
                    revision, created = 1, now
                else:
                    revision = existing["revision"] + (1 if existing["digest"] != record.digest else 0)
                    created = existing["created"]
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO user_presets(slug, title, based_on, palette, arrangement, digest, revision, created, updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET title=excluded.title, "
                        "based_on=excluded.based_on, palette=excluded.palette, arrangement=excluded.arrangement, "
                        "digest=excluded.digest, revision=excluded.revision, updated=excluded.updated",
                        (record.slug, record.title, record.based_on, _dumps(record.palette), _dumps(record.arrangement),
                         record.digest, revision, created, now),
                    )
                self._reload()
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None
            self.checkpoint()
            return {**self._rows[record.id], "created_now": existing is None}

    def remove_user_preset(self, preset_id: str) -> bool:
        slug = preset_id[len(USER_PREFIX):] if preset_id.startswith(USER_PREFIX) else ""
        with self._lock:
            try:
                with self._conn:
                    parent = self._conn.execute("SELECT based_on FROM user_presets WHERE slug=?", (slug,)).fetchone()
                    removed = self._conn.execute("DELETE FROM user_presets WHERE slug=?", (slug,)).rowcount
                    self._conn.execute("DELETE FROM favorites WHERE id=?", (USER_PREFIX + slug,))
                    if removed:
                        # Styles copied from this one are now copies of what it was copied from, so
                        # their lineage (and the note that goes with a traditional style) is not lost.
                        self._conn.execute(
                            "UPDATE user_presets SET based_on=? WHERE based_on=?",
                            (parent["based_on"] if parent else None, USER_PREFIX + slug),
                        )
                self._reload()
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None
            self.checkpoint()
            return bool(removed)

    # ---- favourites ---------------------------------------------------------------------
    def favorites(self) -> dict[str, str]:
        """Favourite id -> kind ("style" or "preset"). A snapshot; safe to read without a lock."""
        return dict(self._favorites)

    def set_favorite(self, item_id: str, kind: str, on: bool) -> bool:
        """Mark or unmark a favourite. Returns True when something changed."""
        with self._lock:
            try:
                with self._conn:
                    if on:
                        if item_id not in self._favorites and len(self._favorites) >= MAX_FAVORITES:
                            raise StoreError(f"You have {MAX_FAVORITES} favourites, which is the most. Remove one first.")
                        changed = self._conn.execute(
                            "INSERT OR IGNORE INTO favorites(id, kind, at) VALUES(?,?,?)", (item_id, kind, self._clock()),
                        ).rowcount
                    else:
                        changed = self._conn.execute("DELETE FROM favorites WHERE id=?", (item_id,)).rowcount
                self._favorites = {r["id"]: r["kind"] for r in self._conn.execute("SELECT id, kind FROM favorites")}
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None
            self.checkpoint()
            return bool(changed)

    # ---- repeat memory ------------------------------------------------------------------
    def remember(self, prompt: SongPrompt, style_key: str) -> int:
        """Record a composed prompt; the oldest memories beyond the limit are forgotten."""
        with self._lock:
            try:
                with self._conn:
                    cursor = self._conn.execute(
                        "INSERT INTO history(at, preset_id, style_key, arrangement_id, seed, complexity, density, mode, title, fingerprint) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (self._clock(), prompt.preset_id, style_key, prompt.arrangement_id, prompt.seed,
                         prompt.complexity, prompt.density, prompt.mode, prompt.title, prompt.fingerprint),
                    )
                    self._conn.execute(
                        "DELETE FROM history WHERE seq <= (SELECT MAX(seq) FROM history) - ?", (MAX_HISTORY,),
                    )
                return int(cursor.lastrowid)
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None

    def recent(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), MAX_HISTORY))
        with self._lock:
            try:
                rows = self._conn.execute("SELECT * FROM history ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None
        return [dict(row) for row in rows]

    def recent_sets(self, preset_window: int, style_window: int, arrangement_window: int) -> tuple[list[str], list[str], list[str]]:
        """Newest-first lists: recent preset ids, then the most recent distinct styles and arrangements."""
        limit = max(preset_window, style_window * 6, arrangement_window * 6, 1)
        rows = self.recent(min(limit, MAX_HISTORY))
        presets = [row["preset_id"] for row in rows[:preset_window]]
        styles: list[str] = []
        arrangements: list[str] = []
        for row in rows:
            if row["style_key"] not in styles and len(styles) < style_window:
                styles.append(row["style_key"])
            if row["arrangement_id"] not in arrangements and len(arrangements) < arrangement_window:
                arrangements.append(row["arrangement_id"])
        return presets, styles, arrangements

    def history_count(self) -> int:
        with self._lock:
            try:
                return int(self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None

    def forget_history(self) -> int:
        with self._lock:
            try:
                with self._conn:
                    return self._conn.execute("DELETE FROM history").rowcount
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None

    def counts(self) -> dict:
        with self._lock:
            try:
                history = int(self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])
                last = self._conn.execute("SELECT MAX(at) FROM history").fetchone()[0]
            except sqlite3.Error:
                raise StoreError(_TROUBLE) from None
        return {"user_presets": len(self._presets), "favorites": len(self._favorites), "history": history, "last_composed": last}


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _loads(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _digest(record: UserPresetRecord) -> str:
    return digest_of(record.palette, record.arrangement)
