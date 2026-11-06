"""The ads database (``ext/ads/ads.sqlite3``): campaigns and their messages, the as-run log, plays
already planned for coming breaks, counts of why a spot was held back, and the two confirmations.

Nothing here is secret. The file belongs in backups. Callers hold ``store.lock`` around anything
that reads and then writes, so a save, an event and a report never interleave half-way.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import threading
from typing import Iterable, Optional

from myradio.extensions import migrate

from .model import AdsError, Campaign, parse_campaign
from .terms import KINDS, TERMS_VERSION

RETENTION_DAYS = 800
MAX_REMOVED_KEPT = 2000
MAX_PLANNED_KEPT = 500
MAX_OLD_MESSAGES_KEPT = 20  # messages edited out of a live spot, kept per spot
MAX_ACK_ROWS = 1000  # the confirmation log is trimmed when it grows past this
KEEP_ACK_ROWS = 200  # ... down to the newest of these (plus anything a play or a spot points to)
_PLACEHOLDER_DAY = dt.date(2000, 1, 1)  # stored flights always carry their own start date

SCHEMA_V1 = """
BEGIN;
CREATE TABLE IF NOT EXISTS campaigns(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    advertiser TEXT NOT NULL,
    category TEXT,
    house INTEGER NOT NULL CHECK(house IN (0, 1)),
    priority INTEGER NOT NULL,
    pacing TEXT NOT NULL,
    timezone TEXT NOT NULL,
    settings TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0, 1)),
    commercial_ack_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    removed_at REAL
);
CREATE TABLE IF NOT EXISTS creatives(
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('recorded', 'script')),
    title TEXT NOT NULL,
    track_id TEXT,
    body TEXT NOT NULL DEFAULT '',
    disclosure TEXT NOT NULL DEFAULT '',
    voice TEXT,
    removed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS creatives_live_track ON creatives(track_id) WHERE track_id IS NOT NULL AND removed_at IS NULL;
CREATE INDEX IF NOT EXISTS creatives_campaign ON creatives(campaign_id);
CREATE TABLE IF NOT EXISTS acknowledgements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('voice_rights', 'commercial_use')),
    acknowledged INTEGER NOT NULL CHECK(acknowledged IN (0, 1)),
    version INTEGER NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS acknowledgements_kind_time ON acknowledgements(kind, at);
CREATE TABLE IF NOT EXISTS planned(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    break_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    planned_at REAL NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned', 'aired'))
);
CREATE INDEX IF NOT EXISTS planned_state_time ON planned(state, planned_at);
CREATE TABLE IF NOT EXISTS plays(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    paid INTEGER NOT NULL CHECK(paid IN (0, 1)),
    spoken INTEGER NOT NULL CHECK(spoken IN (0, 1)),
    started_at REAL NOT NULL,
    ended_at REAL,
    outcome TEXT CHECK(outcome IN ('finished', 'interrupted')),
    detail TEXT,
    source TEXT NOT NULL CHECK(source IN ('event', 'reconciled')),
    planned_id INTEGER,
    commercial_ack_id INTEGER,
    voice_ack_id INTEGER,
    UNIQUE(track_id, started_at)
);
CREATE INDEX IF NOT EXISTS plays_campaign_time ON plays(campaign_id, started_at);
CREATE INDEX IF NOT EXISTS plays_time ON plays(started_at);
CREATE TABLE IF NOT EXISTS skips(
    day TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY(day, campaign_id, reason)
);
COMMIT;
"""

# Step 2 remembers when each recording was attached to a message, so a play of that file from before the spot
# (or before the recording) existed is never credited to it. Existing recordings count from the day their
# spot was created. It is a separate table, not a new column, so that running the step again is harmless.
SCHEMA_V2 = """
BEGIN;
CREATE TABLE IF NOT EXISTS attachments(
    creative_id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL,
    attached_at REAL NOT NULL
);
INSERT OR IGNORE INTO attachments(creative_id, track_id, attached_at)
    SELECT c.id, c.track_id, k.created_at FROM creatives c JOIN campaigns k ON k.id = c.campaign_id
    WHERE c.track_id IS NOT NULL AND c.removed_at IS NULL;
COMMIT;
"""

# One transaction per step, and written so that running a step again after a crash (the version number is
# written just after the script) is harmless.
STEPS = [SCHEMA_V1, SCHEMA_V2]


class AdsStore:
    def __init__(self, conn: sqlite3.Connection, *, steps=STEPS):
        self.conn = conn
        self.lock = threading.RLock()
        self.damaged = 0  # campaigns whose stored data could not be read back
        migrate(conn, steps)

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()

    # ---- campaigns ----------------------------------------------------------------------------
    def _build(self, row, creative_rows) -> Campaign:
        settings = json.loads(row["settings"])
        creatives = []
        for c in creative_rows:
            item = {"id": c["id"], "kind": c["kind"], "title": c["title"]}
            if c["track_id"]:
                item["track_id"] = c["track_id"]
            if c["kind"] == "script":
                item.update(text=c["body"], disclosure=c["disclosure"])
                if c["voice"]:
                    item["voice"] = c["voice"]
            creatives.append(item)
        data = {"name": row["name"], "advertiser": {"name": row["advertiser"], "category": row["category"]},
                "category": row["category"], "house": bool(row["house"]), "priority": row["priority"],
                "pacing": row["pacing"], "timezone": row["timezone"], "flights": settings["flights"],
                "caps": settings["caps"], "separation": settings["separation"], "plan": settings.get("plan"),
                "frequency": settings.get("frequency", "custom"), "creatives": creatives, "paused": bool(row["paused"])}
        campaign, _ = parse_campaign(data, today=_PLACEHOLDER_DAY, campaign_id=row["id"],
                                     own_creatives=frozenset(c["id"] for c in creative_rows), check_zone=False, check_names=False)
        return dataclasses.replace(campaign, created_at=row["created_at"], updated_at=row["updated_at"],
                                   removed_at=row["removed_at"])

    def load(self, *, include_removed: bool = False, campaign_id: Optional[str] = None) -> list:
        with self.lock:
            where, args = [], []
            if not include_removed:
                where.append("removed_at IS NULL")
            if campaign_id is not None:
                where.append("id = ?")
                args.append(campaign_id)
            rows = self.conn.execute("SELECT * FROM campaigns" + (" WHERE " + " AND ".join(where) if where else "")
                                     + " ORDER BY created_at, id", args).fetchall()
            by_campaign: dict = {}
            for c in self.conn.execute("SELECT * FROM creatives ORDER BY campaign_id, position"):
                by_campaign.setdefault(c["campaign_id"], []).append(c)
            out, damaged = [], 0
            for row in rows:
                # A live spot has its live messages; a removed spot keeps the messages it had when it was removed.
                mine = [c for c in by_campaign.get(row["id"], []) if c["removed_at"] == row["removed_at"]]
                try:
                    out.append(self._build(row, mine))
                except (AdsError, ValueError, KeyError, TypeError):
                    damaged += 1
            if campaign_id is None and not include_removed:
                self.damaged = damaged
            return out

    def get(self, campaign_id: str, *, include_removed: bool = False) -> Optional[Campaign]:
        found = self.load(include_removed=include_removed, campaign_id=campaign_id)
        return found[0] if found else None

    def count_active(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM campaigns WHERE removed_at IS NULL").fetchone()[0]

    def save_campaign(self, c: Campaign, *, now: float, commercial_ack_id: Optional[int]) -> None:
        settings = json.dumps({"flights": [f.to_dict() for f in c.flights], "caps": c.caps.to_dict(),
                               "separation": c.separation.to_dict(), "plan": c.plan.to_dict(), "frequency": c.frequency},
                              separators=(",", ":"))
        values = (c.name, c.advertiser, c.category, int(c.house), c.priority, c.pacing, c.timezone, settings,
                  int(c.paused), commercial_ack_id, now)
        with self.lock:
            try:
                with self.conn:
                    if self.conn.execute("SELECT 1 FROM campaigns WHERE id=? AND removed_at IS NULL", (c.id,)).fetchone():
                        self.conn.execute("UPDATE campaigns SET name=?, advertiser=?, category=?, house=?, priority=?, pacing=?,"
                                          " timezone=?, settings=?, paused=?, commercial_ack_id=?, updated_at=? WHERE id=?",
                                          values + (c.id,))
                    else:
                        self.conn.execute("INSERT INTO campaigns(name, advertiser, category, house, priority, pacing, timezone,"
                                          " settings, paused, commercial_ack_id, created_at, id, updated_at)"
                                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values + (c.id, now))
                    keep = [x.id for x in c.creatives]
                    self.conn.execute("UPDATE creatives SET removed_at=? WHERE campaign_id=? AND removed_at IS NULL AND id NOT IN (%s)"
                                      % ",".join("?" * len(keep)), [now, c.id] + keep)
                    # Free the recordings first so two messages can swap recordings in one save.
                    self.conn.execute("UPDATE creatives SET track_id=NULL WHERE campaign_id=? AND removed_at IS NULL", (c.id,))
                    for x in c.creatives:
                        fields = (x.position, x.kind, x.title, x.track_id, x.text, x.disclosure, x.voice)
                        done = self.conn.execute("UPDATE creatives SET position=?, kind=?, title=?, track_id=?, body=?, disclosure=?,"
                                                 " voice=? WHERE id=? AND campaign_id=?", fields + (x.id, c.id))
                        if not done.rowcount:
                            self.conn.execute("INSERT INTO creatives(position, kind, title, track_id, body, disclosure, voice, id,"
                                              " campaign_id) VALUES (?,?,?,?,?,?,?,?,?)", fields + (x.id, c.id))
                    # Messages edited out of a live spot are only kept for a while, so repeated edits cannot grow the file.
                    self.conn.execute("DELETE FROM creatives WHERE campaign_id=? AND removed_at IS NOT NULL AND id NOT IN ("
                                      "SELECT id FROM creatives WHERE campaign_id=? AND removed_at IS NOT NULL"
                                      " ORDER BY removed_at DESC, id LIMIT ?)", (c.id, c.id, MAX_OLD_MESSAGES_KEPT))
                    self._note_attachments(c, now)
            except sqlite3.IntegrityError:
                raise AdsError("One of those recordings is already used by another spot. "
                               "Import a second copy if you want to use it twice.") from None

    def _note_attachments(self, c: Campaign, now: float) -> None:
        """Keep the attachment times in step with a saved spot (inside its transaction): a recording that stays on
        the same message keeps its time, a new or different one starts counting now, and a message left without a
        recording, or taken out of the spot, forgets it."""
        before = {r[0]: r[1] for r in self.conn.execute(
            "SELECT creative_id, track_id FROM attachments WHERE creative_id IN (SELECT id FROM creatives WHERE campaign_id=?)", (c.id,))}
        for x in c.creatives:
            if x.track_id:
                if before.get(x.id) != x.track_id:
                    self.conn.execute("INSERT OR REPLACE INTO attachments(creative_id, track_id, attached_at) VALUES (?,?,?)",
                                      (x.id, x.track_id, now))
            else:
                self.conn.execute("DELETE FROM attachments WHERE creative_id=?", (x.id,))
        self.conn.execute("DELETE FROM attachments WHERE creative_id NOT IN (SELECT id FROM creatives WHERE removed_at IS NULL)"
                          " AND creative_id IN (SELECT id FROM creatives WHERE campaign_id=?)", (c.id,))
        self.conn.execute("DELETE FROM attachments WHERE creative_id NOT IN (SELECT id FROM creatives)")

    def attached_times(self) -> dict:
        """Message id -> (recording id, when that recording was attached to it)."""
        with self.lock:
            return {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT creative_id, track_id, attached_at FROM attachments")}

    def set_paused(self, campaign_id: str, paused: bool, now: float) -> bool:
        with self.lock, self.conn:
            return bool(self.conn.execute("UPDATE campaigns SET paused=?, updated_at=? WHERE id=? AND removed_at IS NULL",
                                          (int(paused), now, campaign_id)).rowcount)

    def remove(self, campaign_id: str, now: float) -> bool:
        """Take a campaign out of rotation. Its past plays stay so reports still add up."""
        with self.lock, self.conn:
            done = self.conn.execute("UPDATE campaigns SET removed_at=?, updated_at=? WHERE id=? AND removed_at IS NULL",
                                     (now, now, campaign_id))
            if not done.rowcount:
                return False
            self.conn.execute("UPDATE creatives SET removed_at=? WHERE campaign_id=? AND removed_at IS NULL", (now, campaign_id))
            self.conn.execute("DELETE FROM planned WHERE campaign_id=?", (campaign_id,))
            old = [r[0] for r in self.conn.execute(
                "SELECT id FROM campaigns WHERE removed_at IS NOT NULL ORDER BY removed_at DESC, id LIMIT -1 OFFSET ?",
                (MAX_REMOVED_KEPT,))]
            for gone in old:
                for table in ("plays", "skips", "planned"):
                    self.conn.execute(f"DELETE FROM {table} WHERE campaign_id=?", (gone,))
                self.conn.execute("DELETE FROM campaigns WHERE id=?", (gone,))
            if old:
                self.conn.execute("DELETE FROM attachments WHERE creative_id NOT IN (SELECT id FROM creatives)")
            return True

    def set_creative_track(self, campaign_id: str, creative_id: str, track_id: str, now: float) -> bool:
        with self.lock:
            try:
                with self.conn:
                    done = self.conn.execute("UPDATE creatives SET track_id=? WHERE id=? AND campaign_id=? AND removed_at IS NULL",
                                             (track_id, creative_id, campaign_id))
                    self.conn.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (now, campaign_id))
                    if done.rowcount:
                        known = self.conn.execute("SELECT track_id FROM attachments WHERE creative_id=?", (creative_id,)).fetchone()
                        if known is None or known[0] != track_id:  # attaching the recording it already has changes nothing
                            self.conn.execute("INSERT OR REPLACE INTO attachments(creative_id, track_id, attached_at) VALUES (?,?,?)",
                                              (creative_id, track_id, now))
                    return bool(done.rowcount)
            except sqlite3.IntegrityError:
                raise AdsError("That recording is already used by another spot.") from None

    def commercial_ack_for(self, campaign_id: str) -> Optional[int]:
        with self.lock:
            row = self.conn.execute("SELECT commercial_ack_id FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            return row[0] if row else None

    # ---- confirmations ----------------------------------------------------------------------------
    def ack_state(self) -> dict:
        """Current state of each confirmation. ``acknowledged`` is true only for the current wording.

        The newest row by insertion wins, not the newest by time: the computer's clock can be put back, and a
        withdrawal written afterwards must still be the last word."""
        out = {}
        with self.lock:
            for kind in KINDS:
                row = self.conn.execute("SELECT id, acknowledged, version, at FROM acknowledgements WHERE kind=?"
                                        " ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
                if row is None:
                    out[kind] = {"acknowledged": False, "at": None, "version": TERMS_VERSION, "ack_id": None,
                                 "needs_reconfirm": False}
                else:
                    current = bool(row["acknowledged"])
                    valid = current and row["version"] == TERMS_VERSION
                    out[kind] = {"acknowledged": valid, "at": row["at"] if current else None, "version": row["version"],
                                 "ack_id": row["id"] if valid else None, "needs_reconfirm": current and not valid}
        return out

    def set_ack(self, kind: str, value: bool, now: float) -> bool:
        """Record a confirmation or its withdrawal. Returns False if nothing changed."""
        if kind not in KINDS:
            raise AdsError("Unknown confirmation.")
        with self.lock:
            state = self.ack_state()[kind]
            if value and state["acknowledged"]:
                return False  # already confirmed for the current wording
            if not value and not (state["acknowledged"] or state["needs_reconfirm"]):
                return False  # nothing to withdraw
            with self.conn:
                self.conn.execute("INSERT INTO acknowledgements(kind, acknowledged, version, at) VALUES (?,?,?,?)",
                                  (kind, int(value), TERMS_VERSION, now))
                # Keep the log bounded; rows that a play or a campaign points to are never removed.
                total = self.conn.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0]
                if total > MAX_ACK_ROWS:
                    self.conn.execute(
                        "DELETE FROM acknowledgements WHERE id NOT IN (SELECT MAX(id) FROM acknowledgements GROUP BY kind)"
                        " AND id NOT IN (SELECT commercial_ack_id FROM plays WHERE commercial_ack_id IS NOT NULL)"
                        " AND id NOT IN (SELECT voice_ack_id FROM plays WHERE voice_ack_id IS NOT NULL)"
                        " AND id NOT IN (SELECT commercial_ack_id FROM campaigns WHERE commercial_ack_id IS NOT NULL)"
                        " AND id NOT IN (SELECT id FROM acknowledgements ORDER BY id DESC LIMIT ?)", (KEEP_ACK_ROWS,))
            return True

    def ack_id_at(self, kind: str, at: float) -> Optional[int]:
        """The confirmation that was in force at a moment, or None if there was none: the last one written
        (by insertion, so a clock that was put back cannot undo a withdrawal) among those dated by then."""
        with self.lock:
            row = self.conn.execute("SELECT id, acknowledged, version FROM acknowledgements WHERE kind=? AND at<=?"
                                    " ORDER BY id DESC LIMIT 1", (kind, at)).fetchone()
            return row["id"] if row and row["acknowledged"] and row["version"] == TERMS_VERSION else None

    # ---- the as-run log -----------------------------------------------------------------------------
    def find_play(self, track_id: str, at: float, tolerance: float):
        with self.lock:
            return self.conn.execute("SELECT * FROM plays WHERE track_id=? AND started_at BETWEEN ? AND ?"
                                     " ORDER BY ABS(started_at - ?) LIMIT 1", (track_id, at - tolerance, at + tolerance, at)).fetchone()

    def add_play(self, *, campaign_id, creative_id, track_id, title, category, paid, spoken, started_at, source,
                 ended_at=None, outcome=None, detail=None, planned_id=None, commercial_ack_id=None, voice_ack_id=None) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO plays(campaign_id, creative_id, track_id, title, category, paid, spoken, started_at, ended_at,"
                " outcome, detail, source, planned_id, commercial_ack_id, voice_ack_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (campaign_id, creative_id, track_id, title, category, int(paid), int(spoken), started_at, ended_at, outcome,
                 detail, source, planned_id, commercial_ack_id, voice_ack_id))
            return cur.lastrowid

    def open_play(self, track_id: str, at: float):
        """The latest play of a track that started by ``at`` and has no ending recorded yet."""
        with self.lock:
            return self.conn.execute("SELECT * FROM plays WHERE track_id=? AND outcome IS NULL AND started_at<=?"
                                     " ORDER BY started_at DESC LIMIT 1", (track_id, at + 1.0)).fetchone()

    def close_play(self, play_id: int, ended_at: float, outcome: str, detail: Optional[str]) -> bool:
        with self.lock, self.conn:
            return bool(self.conn.execute("UPDATE plays SET ended_at=?, outcome=?, detail=? WHERE id=? AND outcome IS NULL",
                                          (ended_at, outcome, detail, play_id)).rowcount)

    def close_open_plays(self, now: float, detail: str) -> int:
        """Mark every play whose ending was never seen as cut short. Used when the station starts again: nothing
        can still be on air then, so a play that never finished was cut off. ``now`` (when that was noticed) is
        stored as its end, which is the latest it can have ended (never before it started)."""
        with self.lock, self.conn:
            return self.conn.execute("UPDATE plays SET outcome='interrupted', detail=?, ended_at=MAX(?, started_at)"
                                     " WHERE outcome IS NULL", (detail, now)).rowcount

    def newest_play_time(self) -> Optional[float]:
        """When the latest logged play started (cut short or not), or None if nothing was ever logged."""
        with self.lock:
            return self.conn.execute("SELECT MAX(started_at) FROM plays").fetchone()[0]

    def recent_plays(self, since: float, until: float) -> list:
        with self.lock:
            return self.conn.execute("SELECT campaign_id, creative_id, category, started_at, outcome FROM plays"
                                     " WHERE started_at>=? AND started_at<=? ORDER BY started_at", (since, until)).fetchall()

    def delivered_totals(self) -> dict:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute(
                "SELECT campaign_id, COUNT(*) FROM plays WHERE outcome IS NOT 'interrupted' GROUP BY campaign_id")}

    def plays_since(self, since: float) -> dict:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute(
                "SELECT campaign_id, COUNT(*) FROM plays WHERE started_at>=? AND outcome IS NOT 'interrupted'"
                " GROUP BY campaign_id", (since,))}

    def last_plays(self) -> dict:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute("SELECT campaign_id, MAX(started_at) FROM plays GROUP BY campaign_id")}

    def play_times(self, campaign_id: str, low: float, high: float) -> list:
        """The as-run rows of one campaign that started in [low, high), oldest first."""
        with self.lock:
            return self.conn.execute("SELECT started_at, outcome, detail, source, title, paid, commercial_ack_id FROM plays"
                                     " WHERE campaign_id=? AND started_at>=? AND started_at<? ORDER BY started_at",
                                     (campaign_id, low, high)).fetchall()

    def campaigns_with_plays(self, low: float, high: float) -> set:
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT DISTINCT campaign_id FROM plays WHERE started_at>=? AND started_at<?",
                                                    (low, high))}

    def play_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]

    # ---- planned plays ----------------------------------------------------------------------------------
    def add_planned(self, break_id: str, rows: Iterable, now: float) -> None:
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO planned(break_id, campaign_id, creative_id, track_id, planned_at, created_at)"
                                  " VALUES (?,?,?,?,?,?)", [(break_id, *r, now) for r in rows])
            self.conn.execute("DELETE FROM planned WHERE id NOT IN (SELECT id FROM planned ORDER BY id DESC LIMIT ?)",
                              (MAX_PLANNED_KEPT,))

    def active_planned(self, now: float, grace: float) -> list:
        with self.lock:
            return self.conn.execute("SELECT campaign_id, creative_id, planned_at FROM planned WHERE state='planned' AND planned_at>?"
                                     " ORDER BY planned_at", (now - grace,)).fetchall()

    def link_planned(self, creative_id: str, at: float, grace: float) -> Optional[int]:
        """Mark the oldest still-waiting planned play of this message as aired."""
        with self.lock, self.conn:
            row = self.conn.execute("SELECT id FROM planned WHERE creative_id=? AND state='planned' AND planned_at>?"
                                    " ORDER BY planned_at, id LIMIT 1", (creative_id, at - grace)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE planned SET state='aired' WHERE id=?", (row[0],))
            return row[0]

    # ---- why a spot was held back -------------------------------------------------------------------------
    def bump_skips(self, rows: Iterable) -> None:
        with self.lock, self.conn:
            for day, campaign_id, reason in rows:
                done = self.conn.execute("UPDATE skips SET count=count+1 WHERE day=? AND campaign_id=? AND reason=?",
                                         (day, campaign_id, reason))
                if not done.rowcount:
                    self.conn.execute("INSERT INTO skips(day, campaign_id, reason, count) VALUES (?,?,?,1)",
                                      (day, campaign_id, reason))

    def skip_counts(self, campaign_id: str, first_day: str, last_day: str) -> dict:
        out: dict = {}
        with self.lock:
            for day, reason, count in self.conn.execute(
                    "SELECT day, reason, count FROM skips WHERE campaign_id=? AND day>=? AND day<=?",
                    (campaign_id, first_day, last_day)):
                out.setdefault(day, {})[reason] = count
        return out

    # ---- housekeeping -------------------------------------------------------------------------------------------
    def _newest_record(self) -> Optional[float]:
        newest = None
        for sql in ("SELECT MAX(started_at) FROM plays", "SELECT MAX(at) FROM acknowledgements",
                    "SELECT MAX(updated_at) FROM campaigns", "SELECT MAX(created_at) FROM planned"):
            value = self.conn.execute(sql).fetchone()[0]
            if value is not None and (newest is None or value > newest):
                newest = value
        return newest

    def prune(self, now: float) -> None:
        """Forget old history. The clock is not trusted blindly: if the computer's date is far ahead of the newest
        thing ever written (a dead clock battery, a restored virtual machine), nothing recent is thrown away; the
        cut-off is worked out from a day after the newest record instead."""
        with self.lock, self.conn:
            newest = self._newest_record()
            reference = now if newest is None else min(now, newest + 86400)
            cutoff = reference - RETENTION_DAYS * 86400
            day = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).date().isoformat()
            self.conn.execute("DELETE FROM plays WHERE started_at<?", (cutoff,))
            self.conn.execute("DELETE FROM skips WHERE day<?", (day,))
            self.conn.execute("DELETE FROM planned WHERE planned_at<?", (reference - 2 * 86400,))
