"""Persistence for the hosts extension: hosts the operator created, which built-in hosts are hidden,
and the repetition memory. Everything lives in the extension's own SQLite file; nothing here is
secret and nothing here holds station facts.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from myradio.extensions import migrate

from .errors import HostsError
from .memory import MAX_RECORDS, Memory
from .personas import Persona, validate_persona
from .textsafe import EVENT_TAGS

MAX_CUSTOM = 60
MAX_MEMORY_ROWS = 20000
SCHEMA = ["""
CREATE TABLE personas(
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE hidden(
    id TEXT PRIMARY KEY,
    at REAL NOT NULL
);
CREATE TABLE memory(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    persona TEXT NOT NULL,
    kind TEXT NOT NULL,
    units TEXT NOT NULL,
    digest TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX memory_lookup ON memory(persona, kind, seq);
"""]
SAVE_FAILED = "Your host could not be saved. Check that the disk has room, then try again."


class HostsStore:
    """Built-in and custom hosts. ``conn`` may be None for an in-memory store (tests, library use)."""

    def __init__(self, conn: Optional[sqlite3.Connection], builtin: Mapping[str, Persona], voices: Mapping[str, Any],
                 event_tags: Sequence[str] = EVENT_TAGS, *, lock: Optional[threading.RLock] = None,
                 clock: Callable[[], float] = time.time, max_custom: int = MAX_CUSTOM):
        self._conn = conn
        self._builtin: Dict[str, Persona] = dict(builtin)
        self._voices = voices
        self._event_tags = tuple(event_tags)
        self.lock = lock or threading.RLock()
        self._clock = clock
        self._max = max_custom
        self._custom: Dict[str, Persona] = {}
        self._hidden: set = set()
        self.bad_rows = 0
        self.write_errors = 0
        if conn is not None:
            with self.lock:
                migrate(conn, SCHEMA)
                self._load()
        self.counts: Dict[str, int] = {}
        self._refresh()

    # ---- reading ---------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            rows = self._conn.execute("SELECT id, data FROM personas ORDER BY created_at, id").fetchall()
            hidden = [r[0] for r in self._conn.execute("SELECT id FROM hidden")]
        except sqlite3.Error:
            self.write_errors += 1
            return
        for row in rows:
            try:
                persona, _ = validate_persona(json.loads(row[1]), self._voices, event_tags=self._event_tags, lenient_tags=True)
                if persona.id != row[0] or persona.id in self._builtin:
                    raise HostsError("bad row")
                self._custom[persona.id] = persona
            except (HostsError, ValueError, TypeError):
                self.bad_rows += 1
        self._hidden = {h for h in hidden if h in self._builtin}

    def _refresh(self) -> None:
        """Rebuild the cached counts that status() reads without taking any lock."""
        self.counts = {"builtin": len(self._builtin), "custom": len(self._custom),
                       "hidden": len(self._hidden), "visible": len(self._builtin) - len(self._hidden) + len(self._custom)}

    def personas(self, include_hidden: bool = False) -> List[Persona]:
        with self.lock:
            out = [p for p in self._builtin.values() if include_hidden or p.id not in self._hidden]
            out.extend(self._custom.values())
            return out

    def is_hidden(self, persona_id: str) -> bool:
        return persona_id in self._hidden

    def get(self, persona_id: Any) -> Persona:
        """Find a host by id. Hidden built-in hosts still resolve, so a show that uses one keeps working."""
        with self.lock:
            found = self._custom.get(persona_id) if isinstance(persona_id, str) else None
            found = found or (self._builtin.get(persona_id) if isinstance(persona_id, str) else None)
        if found is None:
            raise HostsError("There is no host with that id. Open the Team page to see the available hosts.")
        return found

    # ---- changing --------------------------------------------------------------------------------
    def _unique_id(self, base: str) -> str:
        taken = set(self._builtin) | set(self._custom)
        if base not in taken:
            return base
        for n in range(2, 1000):
            candidate = f"{base[:58]}-{n}"
            if candidate not in taken:
                return candidate
        raise HostsError("Too many hosts share that name. Choose a different name.")

    def save(self, raw: Any) -> "Tuple[Persona, bool, List[str]]":
        """Create or update one of the operator's own hosts. Returns (persona, created, warnings)."""
        persona, warnings = validate_persona(raw, self._voices, event_tags=self._event_tags)
        given = raw.get("id") if isinstance(raw, Mapping) else None
        with self.lock:
            if given not in (None, ""):
                if persona.id in self._builtin:
                    raise HostsError("That id belongs to a built-in host. Save your own host under a different name.")
                created = persona.id not in self._custom
            else:
                created = True
                persona = dataclasses.replace(persona, id=self._unique_id(persona.id))
            for other in list(self._builtin.values()) + list(self._custom.values()):
                if other.id != persona.id and other.name.casefold() == persona.name.casefold():
                    raise HostsError(f"There is already a host called {other.name}. Choose a different name.")
            if created and len(self._custom) >= self._max:
                raise HostsError(f"You already have {self._max} hosts of your own. Remove one before adding another.")
            self._write(persona, created)
            self._custom[persona.id] = persona
            self._refresh()
        return persona, created, warnings

    def _write(self, persona: Persona, created: bool) -> None:
        if self._conn is None:
            return
        now = self._clock()
        data = json.dumps(persona.to_storage(), ensure_ascii=False, separators=(",", ":"))
        try:
            updated = self._conn.execute("UPDATE personas SET data=?, updated_at=? WHERE id=?", (data, now, persona.id)).rowcount
            if not updated:
                self._conn.execute("INSERT INTO personas(id, data, created_at, updated_at) VALUES(?,?,?,?)",
                                   (persona.id, data, now, now))
            self._conn.commit()
        except sqlite3.Error:
            self.write_errors += 1
            self._rollback()
            raise HostsError(SAVE_FAILED) from None

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    def remove(self, persona_id: Any) -> dict:
        """Delete one of the operator's hosts. A built-in host cannot be deleted; it is hidden instead."""
        with self.lock:
            if not isinstance(persona_id, str):
                raise HostsError("There is no host with that id.")
            if persona_id in self._builtin:
                if persona_id not in self._hidden:
                    self._set_hidden(persona_id, True)
                return {"id": persona_id, "removed": False, "hidden": True,
                        "message": f"{self._builtin[persona_id].name} is built in, so it was hidden instead of deleted. "
                                   "You can bring it back at any time."}
            if persona_id not in self._custom:
                raise HostsError("There is no host with that id.")
            if self._conn is not None:
                try:
                    self._conn.execute("DELETE FROM personas WHERE id=?", (persona_id,))
                    self._conn.commit()
                except sqlite3.Error:
                    self.write_errors += 1
                    self._rollback()
                    raise HostsError("Your host could not be removed. Check that the disk has room, then try again.") from None
            name = self._custom.pop(persona_id).name
            self._refresh()
            return {"id": persona_id, "removed": True, "hidden": False, "message": f"{name} was removed."}

    def restore(self, persona_id: Any) -> dict:
        with self.lock:
            if not isinstance(persona_id, str) or persona_id not in self._builtin:
                raise HostsError("Only built-in hosts can be restored. There is no built-in host with that id.")
            if persona_id in self._hidden:
                self._set_hidden(persona_id, False)
                return {"id": persona_id, "restored": True, "message": f"{self._builtin[persona_id].name} is back."}
            return {"id": persona_id, "restored": False, "message": f"{self._builtin[persona_id].name} was not hidden."}

    def _set_hidden(self, persona_id: str, hide: bool) -> None:
        if self._conn is not None:
            try:
                if hide:
                    self._conn.execute("INSERT OR REPLACE INTO hidden(id, at) VALUES(?,?)", (persona_id, self._clock()))
                else:
                    self._conn.execute("DELETE FROM hidden WHERE id=?", (persona_id,))
                self._conn.commit()
            except sqlite3.Error:
                self.write_errors += 1
                self._rollback()
                raise HostsError(SAVE_FAILED) from None
        (self._hidden.add if hide else self._hidden.discard)(persona_id)
        self._refresh()


class SqliteMemory(Memory):
    """Repetition memory that survives restarts. Reads come from memory; writes are best effort, so a
    full disk never stops a script from being written."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock, limit: int = MAX_RECORDS):
        super().__init__(limit)
        self._conn = conn
        self._dblock = lock
        self.write_errors = 0
        with self._dblock:
            try:
                self._conn.execute("DELETE FROM memory WHERE seq NOT IN (SELECT seq FROM memory ORDER BY seq DESC LIMIT ?)",
                                   (MAX_MEMORY_ROWS,))
                self._conn.commit()
                rows = self._conn.execute("SELECT persona, kind, units, digest, at FROM memory ORDER BY seq").fetchall()
            except sqlite3.Error:
                self.write_errors += 1
                rows = []
        for persona, kind, units, digest, at in rows:
            try:
                parsed = json.loads(units)
                if isinstance(parsed, list) and all(isinstance(u, str) for u in parsed):
                    super().remember(persona, kind, parsed, digest, at)
            except ValueError:
                continue

    def remember(self, persona: str, segment: str, units: Sequence[str], digest: str, at: float) -> None:
        super().remember(persona, segment, units, digest, at)
        with self._dblock:
            try:
                self._conn.execute("INSERT INTO memory(persona, kind, units, digest, at) VALUES(?,?,?,?,?)",
                                   (persona, segment, json.dumps(list(units)), digest, float(at)))
                self._conn.execute(
                    "DELETE FROM memory WHERE persona=? AND kind=? AND seq NOT IN "
                    "(SELECT seq FROM memory WHERE persona=? AND kind=? ORDER BY seq DESC LIMIT ?)",
                    (persona, segment, persona, segment, self._limit))
                self._conn.commit()
            except sqlite3.Error:
                self.write_errors += 1
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass

    def forget(self, persona: str) -> None:
        super().forget(persona)
        with self._dblock:
            try:
                self._conn.execute("DELETE FROM memory WHERE persona=?", (persona,))
                self._conn.commit()
            except sqlite3.Error:
                self.write_errors += 1

    def clear(self) -> None:
        super().clear()
        with self._dblock:
            try:
                self._conn.execute("DELETE FROM memory")
                self._conn.commit()
            except sqlite3.Error:
                self.write_errors += 1
