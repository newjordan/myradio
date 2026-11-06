"""Persistent station state and a single-clock, bounded-memory PCM playout engine."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import datetime as dt
import io
import json
import os
from pathlib import Path
import queue
import sqlite3
import shutil
import threading
import time
import uuid
import wave

from .media import open_media, MediaError
from .scheduling import next_occurrence, recurrence
from .broadcast import Broadcast, BroadcastError
from .extensions import ExtensionError, ExtensionHost, StationFacade
from .importing import decode_audio, ImportError as AudioImportError

RATE = 44100
CHANNELS = 2
WIDTH = 2
FRAMES = 4410  # 100 ms; one clock drives every listener.
SILENCE = bytes(FRAMES * CHANNELS * WIDTH)
MAX_UPLOAD = 100 * 1024 * 1024


class StationError(ValueError):
    pass


def text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StationError(f"{name} must be 1–{limit} characters.")
    return value.strip()


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


OPERATIONS = {
    "broadcast.start": "Go live to the saved Icecast destination. Requires running playout and explicit user intent; may transmit publicly. Settings must be saved privately in the dashboard first.",
    "broadcast.stop": "Disconnect external broadcast without stopping local playout.",
    "station.update": "Set name, tagline and/or auto_fill (boolean).",
    "transport.start": "Start station playout. Does not open an external broadcast.",
    "transport.stop": "Stop station playout and return the current track to the queue.",
    "transport.skip": "Skip the current track.",
    "queue.add": "Add a library track by track_id: at the end, or at position (0 plays next) when one is given.",
    "queue.move": "Move a queued entry (queue_id) to position: 0 plays next; a position past the end moves it to the end.",
    "queue.remove": "Remove a queued entry by queue_id.",
    "library.delete": "Delete an unused library track by track_id.",
    "library.retry": "Recheck repaired audio by track_id and clear its quarantine only if native PCM validation succeeds. Never downloads or generates replacement audio.",
    "schedule.add": "Schedule track_id at an ISO-8601 timestamp with timezone offset.",
    "schedule.cancel": "Cancel a pending event by schedule_id.",
    "program.add": "Repeat a track at local_time (HH:MM) in an IANA timezone on weekdays (comma-separated ISO days, Monday=1). DST gaps skip; folds air once. Does not interrupt current audio.",
    "program.remove": "Permanently remove a recurring program by program_id.",
}

ARGUMENTS = {
    "station.update": {"name": "string", "tagline": "string", "auto_fill": "boolean"},
    "transport.start": {}, "transport.stop": {}, "transport.skip": {},
    "broadcast.start": {}, "broadcast.stop": {},
    "queue.add": {"track_id": "string", "position": "integer"}, "queue.remove": {"queue_id": "string"},
    "queue.move": {"queue_id": "string", "position": "integer"},
    "library.delete": {"track_id": "string"}, "library.retry": {"track_id": "string"},
    "schedule.add": {"track_id": "string", "at": "string"},
    "schedule.cancel": {"schedule_id": "string"},
    "program.add": {"track_id": "string", "timezone": "string", "local_time": "string", "weekdays": "string"},
    "program.remove": {"program_id": "string"},
}


# Arguments an operation may leave out (``station.update`` takes any subset of its own).
OPTIONAL = {"queue.add": ("position",)}
MAX_POSITION = 1_000_000


def required_arguments(name):
    return [] if name == "station.update" else [key for key in ARGUMENTS[name] if key not in OPTIONAL.get(name, ())]


# The queue plays in ``pos`` order. Front inserts (a stopped track, due schedules, a recovered track) take the lowest
# position and the lowest id; appends take the highest of both, so until something is moved by hand play order and id
# order agree, as they always did before positions existed. Ids never change: they are the handle clients hold.
QUEUE_FRONT = "INSERT INTO queue(id,track_id,pos) VALUES ((SELECT COALESCE(MIN(id),1)-1 FROM queue),?,(SELECT COALESCE(MIN(pos),1)-1 FROM queue))"
QUEUE_APPEND = "INSERT INTO queue(track_id,pos) VALUES (?,(SELECT COALESCE(MAX(pos),0)+1 FROM queue))"


def ensure_queue_order(db):
    """Give every queue entry a play position (added for reordering). Older stations had only the row id, which was
    also the play order, so it is copied over."""
    if "pos" not in [row[1] for row in db.execute("PRAGMA table_info(queue)")]:
        db.execute("ALTER TABLE queue ADD COLUMN pos REAL")
    db.execute("UPDATE queue SET pos=id WHERE pos IS NULL")
    db.execute("CREATE INDEX IF NOT EXISTS queue_order ON queue(pos,id)")
    # Anything that writes the table without a position (an older tool, a test fixture, a restore) still gets the order its
    # id meant: a row below every other id plays first, any other row plays last. Without this a NULL position sorts first.
    db.execute("""CREATE TRIGGER IF NOT EXISTS queue_position AFTER INSERT ON queue WHEN NEW.pos IS NULL BEGIN
        UPDATE queue SET pos = CASE WHEN NEW.id < (SELECT MIN(id) FROM queue WHERE id <> NEW.id)
                                    THEN (SELECT MIN(pos) FROM queue WHERE id <> NEW.id) - 1
                                    ELSE COALESCE((SELECT MAX(pos) FROM queue WHERE id <> NEW.id), 0) + 1 END
        WHERE id = NEW.id; END""")


SCHEMA = '''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY,title TEXT NOT NULL,
                artist TEXT NOT NULL,duration REAL NOT NULL,kind TEXT NOT NULL,filename TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media_failures(track_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                message TEXT NOT NULL, failed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id TEXT NOT NULL REFERENCES tracks(id));
            CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,
                track_id TEXT NOT NULL REFERENCES tracks(id),at TEXT NOT NULL,
                due REAL NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,at TEXT NOT NULL,event TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS programs(id TEXT PRIMARY KEY,
                track_id TEXT NOT NULL REFERENCES tracks(id), timezone TEXT NOT NULL,
                local_time TEXT NOT NULL, weekdays TEXT NOT NULL, next_due REAL NOT NULL,
                last_due REAL, last_status TEXT);
            CREATE INDEX IF NOT EXISTS programs_next_due ON programs(next_due);
            CREATE INDEX IF NOT EXISTS schedules_pending_due ON schedules(status,due);
        '''

class Station:
    def __init__(self, directory, *, start_worker=True, extensions=()):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.process_lock = open(self.directory / 'station.lock', 'a')
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.process_lock.close()
            raise StationError('Another myradio service is using this data directory.') from None
        self.media = self.directory / "media"
        self.media.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / "station.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        ensure_queue_order(self.db)
        defaults = {"name": "My radio", "tagline": "Your station. Your frequency.", "auto_fill": True}
        for key, value in defaults.items():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()
        self.running = False  # Restarts never silently start transmitting.
        self.current = None
        self.reader = None
        self.elapsed = 0.0
        self.last_track = None
        self.subscribers = set()
        self.dropped = 0
        self.error = None
        self.faulted = False
        self._last_status = None
        self.stop_event = threading.Event()
        self.worker = None
        self.agent_model = False
        self.chat_mode = "commands"
        self.chat_lock = threading.Lock()
        self.broadcast = Broadcast(self.directory)
        # Optional subsystems. Core playout never calls into them (see extensions.py).
        self.extensions = ExtensionHost(self.directory, StationFacade(self))
        for extension in extensions:
            self.extensions.register_or_fail(extension)  # a broken extension is reported, never fatal
        # Recover only in-progress playout: retain it ahead of pending queue entries.
        recovered = self.db.execute("SELECT value FROM settings WHERE key='playing'").fetchone()
        if recovered and json.loads(recovered[0]):
            track_id = json.loads(recovered[0])
            if self.db.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
                self.db.execute(QUEUE_FRONT, (track_id,))
        self._setting("playing", None)
        self.db.commit()
        if start_worker:
            self.worker = threading.Thread(target=self._run, name="myradio-playout", daemon=True)
            self.worker.start()
        # Extensions start after the clock so a slow setup can never delay playout.
        self.extensions.start()

    def _emit(self, kind, **data):
        """Tell extensions something happened. Never blocks and never raises: the audio clock
        must not depend on them."""
        try:
            self.extensions.emit({"type": kind, "at": time.time(), **data})
        except Exception:
            pass

    def operations(self):
        """Every operation (core and extension) with typed arguments, for chat, MCP and the UI."""
        core = [{"name": name, "description": OPERATIONS[name],
                 "arguments": {key: {"type": typ} for key, typ in ARGUMENTS[name].items()},
                 "required": required_arguments(name)} for name in OPERATIONS]
        return core + self.extensions.catalog()

    def _fail_safe(self):
        """Latch a fault without depending on a working filesystem or database."""
        self.faulted = True
        self.running = False
        self.error = ("Station stopped after a storage or playout fault. External broadcast is disconnected. "
                      "Check free disk space and storage permissions, then restart the myradio service. "
                      "Pending changes were rolled back where possible; review the queue after restart.")
        reader, self.reader = self.reader, None
        self.current = None
        self.elapsed = 0.0
        # Each cleanup is independent: rollback/close failures must not kill the clock.
        for cleanup in (self.broadcast.stop, reader.close if reader else lambda: None, self.db.rollback):
            try:
                cleanup()
            except Exception:
                pass

    @contextmanager
    def _transaction(self):
        # Roll back before releasing the shared connection lock. Never let another
        # request or tick commit part of an operation that reported failure.
        with self.lock:
            if self.faulted:
                raise StationError(self.error)
            try:
                yield
            except (sqlite3.Error, OSError):
                self._fail_safe()
                raise StationError(self.error) from None
            except Exception:
                try:
                    self.db.rollback()
                except Exception:
                    self._fail_safe()
                raise

    def _setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def _event(self, title, event):
        self.db.execute("INSERT INTO history(title,at,event) VALUES (?,?,?)", (title, utcnow(), event))
        self.db.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 200)")

    def _track(self, track_id):
        if not isinstance(track_id, str):
            raise StationError("Choose a library track.")
        row = self.db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            raise StationError("That track is no longer in the library.")
        return dict(row)

    @staticmethod
    def public(track):
        result = {k: v for k, v in track.items() if k != "filename"}
        result["available"] = bool(result.get("available", True))
        result.setdefault("playback_error", None)
        return result

    def status(self):
        result = self._status_locked()
        # Extension status is gathered after the playout lock is released, so a slow
        # extension can never hold the clock's lock.
        result["extensions"] = self.extensions.status()
        return result

    def _status_locked(self):
        with self.lock:
            try:
                result = self._read_status()
                self._last_status = copy.deepcopy(result)
            except sqlite3.Error:
                self._fail_safe()
                # Cached data is explicitly marked stale, never presented as live.
                result = copy.deepcopy(self._last_status) if self._last_status else {
                    "station": {}, "queue": [], "library": [], "schedules": [],
                    "programs": [], "history": [], "capabilities": {}, "imports": {},
                    "stream": {"listeners": len(self.subscribers), "dropped": self.dropped},
                    "chat_mode": self.chat_mode,
                    "health": {"ready_tracks": 0, "unavailable_tracks": 0},
                }
                result["health"]["stale"] = True
            if self.faulted:
                result.update(running=False, current=None, elapsed=0.0, error=self.error)
                result["health"].update(state="fault", message=self.error)
                result["broadcast"] = self.broadcast.status()
            return result

    def _read_status(self):
        with self.lock:
            settings = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT key,value FROM settings") if r[0] != "playing"}
            unavailable = self.db.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0]
            ready = self.db.execute("SELECT COUNT(*) FROM tracks WHERE id NOT IN (SELECT track_id FROM media_failures)").fetchone()[0]
            health_state = "stopped" if not self.running else "waiting" if not self.current else "degraded" if unavailable else "playing"
            return {
                "health": {"state": health_state, "unavailable_tracks": unavailable, "ready_tracks": ready,
                           "message": ("Waiting for playable audio. Queue a ready track, import audio or repair and retry unavailable tracks. The stream carries silence while waiting." if self.running and not self.current else
                                       "Unavailable audio is excluded from rotation. Repair and retry it in the library." if unavailable else "")},
                "station": settings, "running": self.running,
                "current": self.public(self.current) if self.current else None,
                "elapsed": round(self.elapsed, 2), "error": self.error,
                "queue": [dict(r) for r in self.db.execute("SELECT q.id,t.id AS track_id,t.title,t.artist,t.duration,t.kind FROM queue q JOIN tracks t ON t.id=q.track_id ORDER BY q.pos,q.id")],
                "library": [self.public(dict(r)) for r in self.db.execute("SELECT t.*, (f.track_id IS NULL) AS available, f.message AS playback_error FROM tracks t LEFT JOIN media_failures f ON f.track_id=t.id ORDER BY t.title,t.id")],
                "schedules": [dict(r) for r in self.db.execute("SELECT s.id,s.track_id,t.title,s.at,s.status FROM schedules s JOIN tracks t ON t.id=s.track_id ORDER BY s.due DESC LIMIT 100")],
                "programs": [dict(r) for r in self.db.execute("SELECT p.*,t.title FROM programs p JOIN tracks t ON t.id=p.track_id ORDER BY p.next_due,p.id")],
                "history": [dict(r) for r in self.db.execute("SELECT title,at,event FROM history ORDER BY id DESC LIMIT 30")],
                "capabilities": {"music_generation": False, "speech_generation": False,
                                 "external_broadcast": True, "agent_model": self.agent_model},
                "stream": {"listeners": len(self.subscribers), "dropped": self.dropped},
                "chat_mode": self.chat_mode,
                "broadcast": self.broadcast.status(),
                "imports": {"conversion_available": bool(shutil.which("ffmpeg")), "max_upload_bytes": MAX_UPLOAD, "max_decoded_bytes": MAX_UPLOAD},
            }

    def import_audio(self, data, title, artist="", kind="music"):
        # Validate metadata before starting a potentially expensive decoder.
        title = text(title, "Title")
        if not isinstance(artist, str) or len(artist) > 200:
            raise StationError("Artist must be at most 200 characters.")
        if kind not in ("music", "speech", "ad"):
            raise StationError("Choose music, speech, or ad.")
        if not data or len(data) > MAX_UPLOAD:
            raise StationError("Audio must be nonempty and no larger than 100 MiB.")
        try:
            with wave.open(io.BytesIO(data), "rb") as source:
                native = (source.getframerate(), source.getsampwidth(), source.getnchannels(), source.getcomptype()) in (
                    (RATE, WIDTH, 1, "NONE"), (RATE, WIDTH, 2, "NONE"))
        except (wave.Error, EOFError):
            native = False
        if native:
            # Retain strict truncation/size validation and no-dependency import.
            return self.import_wav(data, title, artist, kind)
        try:
            pcm = decode_audio(data, self.directory)
        except AudioImportError as exc:
            raise StationError(str(exc)) from None
        return self._store_pcm(pcm, title, artist, kind)

    def import_wav(self, data, title, artist="", kind="music"):
        title = text(title, "Title")
        if not isinstance(artist, str) or len(artist) > 200:
            raise StationError("Artist must be at most 200 characters.")
        if kind not in ("music", "speech", "ad"):
            raise StationError("Choose music, speech, or ad.")
        if not data or len(data) > MAX_UPLOAD:
            raise StationError("Audio must be a nonempty WAV smaller than 100 MiB.")
        try:
            with wave.open(io.BytesIO(data), "rb") as source:
                if (source.getframerate(), source.getsampwidth(), source.getnchannels(), source.getcomptype()) not in (
                    (RATE, WIDTH, 1, "NONE"), (RATE, WIDTH, 2, "NONE")
                ):
                    raise StationError("Use uncompressed 44.1 kHz, 16-bit mono or stereo WAV.")
                channels = source.getnchannels()
                frames = source.getnframes()
                if frames < FRAMES or frames > MAX_UPLOAD // (WIDTH * CHANNELS):
                    raise StationError("Audio must be at least 100 ms and within the normalized 100 MiB limit.")
                pcm = source.readframes(frames)
                if len(pcm) != frames * channels * WIDTH:
                    raise StationError("WAV is truncated. Export it again before importing.")
        except (wave.Error, EOFError) as exc:
            raise StationError("This is not a supported PCM WAV file.") from exc
        if channels == 1:
            # Byte-level duplication preserves signed sample values on any host endianness.
            stereo = bytearray(len(pcm) * 2)
            stereo[0::4], stereo[1::4] = pcm[0::2], pcm[1::2]
            stereo[2::4], stereo[3::4] = pcm[0::2], pcm[1::2]
            pcm = stereo
        return self._store_pcm(pcm, title, artist, kind)

    def _store_pcm(self, pcm, title, artist, kind):
        frames = len(pcm) // (WIDTH * CHANNELS)
        track_id = uuid.uuid4().hex
        target = self.media / f"{track_id}.wav"
        staged = target.with_suffix(".tmp")
        try:
            with wave.open(str(staged), "wb") as dest:
                dest.setparams((CHANNELS, WIDTH, RATE, 0, "NONE", "not compressed"))
                dest.writeframes(pcm)
            os.replace(staged, target)
            target.chmod(0o600)
            with self._transaction():
                self.db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?)", (track_id, title, artist.strip(), frames / RATE, kind, target.name))
                self._event(title, "imported")
                self.db.commit()
                return self.public({"id": track_id, "title": title, "artist": artist.strip(),
                                    "duration": frames / RATE, "kind": kind})
        except Exception:
            staged.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    def action(self, operation, arguments=None):
        if isinstance(operation, str) and self.extensions.has_operation(operation):
            # Extension operations run outside the core transaction and playout lock.
            if self.faulted:
                raise StationError(self.error)
            try:
                return self.extensions.call(operation, arguments)
            except ExtensionError as exc:
                raise StationError(str(exc)) from None
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise StationError("Unknown station operation.")
        args = {} if arguments is None else arguments
        if not isinstance(args, dict) or set(args) - set(ARGUMENTS[operation]):
            raise StationError("Invalid operation arguments.")
        for key, typ in ARGUMENTS[operation].items():
            if key not in args:
                if operation != "station.update" and key not in OPTIONAL.get(operation, ()):
                    raise StationError(f"Missing {key}.")
            elif typ == "integer":
                if type(args[key]) is not int or not 0 <= args[key] <= MAX_POSITION:
                    raise StationError(f"Invalid {key}.")
            elif not isinstance(args[key], bool if typ == "boolean" else str):
                # Queue IDs originate in SQLite, and the UI may send the number.
                if not (key == "queue_id" and type(args[key]) is int):
                    raise StationError(f"Invalid {key}.")
        # Disconnect remains available even after a latched storage fault.
        if operation == "broadcast.stop":
            return self.broadcast.stop()
        with self._transaction():
            if operation in ("broadcast.start", "broadcast.stop"):
                if operation == "broadcast.start" and not self.running:
                    raise StationError("Start local station playout before going live.")
                try:
                    return self.broadcast.start() if operation == "broadcast.start" else self.broadcast.stop()
                except BroadcastError as exc:
                    raise StationError(str(exc)) from None
            if operation == "station.update":
                clean = {}
                for key, value in args.items():
                    clean[key] = text(value, key) if key != "auto_fill" else value
                for key, value in clean.items():
                    self._setting(key, value)
            elif operation == "transport.start":
                if not self.db.execute("SELECT 1 FROM tracks LIMIT 1").fetchone():
                    raise StationError("Import at least one audio track before starting your station.")
                self.running = True
                self.error = None
            elif operation == "transport.stop":
                self.broadcast.stop()
                self.running = False
                if self.current:
                    self.db.execute(QUEUE_FRONT, (self.current["id"],))
                self._finish("stopped")
            elif operation == "transport.skip":
                self._finish("skipped")
            elif operation == "queue.add":
                self._track(args["track_id"])
                if self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0] >= 1000:
                    raise StationError("Queue is full (1,000 items).")
                added = self.db.execute(QUEUE_APPEND, (args["track_id"],)).lastrowid
                if "position" in args:
                    self._queue_place(added, args["position"])
            elif operation == "queue.move":
                entry = self.db.execute("SELECT id FROM queue WHERE id=?", (args["queue_id"],)).fetchone()
                if not entry:
                    raise StationError("Queue entry not found.")
                self._queue_place(entry["id"], args["position"])
            elif operation == "queue.remove":
                cur = self.db.execute("DELETE FROM queue WHERE id=?", (args["queue_id"],))
                if not cur.rowcount:
                    raise StationError("Queue entry not found.")
            elif operation == "library.retry":
                track = self._track(args["track_id"])
                try:
                    with_reader = open_media(self.media / track["filename"], track["duration"])
                    with_reader.close()
                except (MediaError, OSError) as exc:
                    raise StationError(str(exc) if isinstance(exc, MediaError) else "Cannot read repaired audio.") from None
                self.db.execute("DELETE FROM media_failures WHERE track_id=?", (track["id"],))
                self._event(track["title"], "media ready")
            elif operation == "library.delete":
                track = self._track(args["track_id"])
                if (self.current and self.current["id"] == track["id"]) or self.db.execute("SELECT 1 FROM queue WHERE track_id=?", (track["id"],)).fetchone() or self.db.execute("SELECT 1 FROM schedules WHERE track_id=? AND status='pending'", (track["id"],)).fetchone() or self.db.execute("SELECT 1 FROM programs WHERE track_id=?", (track["id"],)).fetchone():
                    raise StationError("Remove this track from playout, queue, pending schedules and recurring programs first.")
                self.db.execute("DELETE FROM schedules WHERE track_id=?", (track["id"],))
                self.db.execute("DELETE FROM tracks WHERE id=?", (track["id"],))
                self.db.commit()
                (self.media / track["filename"]).unlink(missing_ok=True)
            elif operation == "schedule.add":
                self._track(args["track_id"])
                try:
                    when = dt.datetime.fromisoformat(args["at"].replace("Z", "+00:00"))
                    if when.utcoffset() is None:
                        raise ValueError()
                    due = when.timestamp()
                except (ValueError, OverflowError):
                    raise StationError("Use an ISO timestamp with a timezone offset.") from None
                if not time.time() < due <= time.time() + 366 * 86400:
                    raise StationError("Schedule within the next 366 days.")
                if self.db.execute("SELECT COUNT(*) FROM schedules WHERE status='pending'").fetchone()[0] >= 1000:
                    raise StationError("Schedule is full (1,000 pending events).")
                self.db.execute("INSERT INTO schedules VALUES (?,?,?,?,?)", (uuid.uuid4().hex, args["track_id"], when.isoformat(), due, "pending"))
            elif operation == "program.add":
                self._track(args["track_id"])
                try:
                    _, _, days = recurrence(args["timezone"], args["local_time"], args["weekdays"])
                    weekdays = ",".join(map(str, days))
                    due = next_occurrence(args["timezone"], args["local_time"], weekdays, time.time())
                except ValueError as exc:
                    raise StationError(str(exc)) from None
                if self.db.execute("SELECT COUNT(*) FROM programs").fetchone()[0] >= 1000:
                    raise StationError("Recurring programs are full (1,000 programs).")
                self.db.execute("INSERT INTO programs VALUES (?,?,?,?,?,?,NULL,NULL)",
                                (uuid.uuid4().hex, args["track_id"], args["timezone"], args["local_time"], weekdays, due))
            elif operation == "program.remove":
                if not self.db.execute("DELETE FROM programs WHERE id=?", (args["program_id"],)).rowcount:
                    raise StationError("Recurring program not found.")
            elif operation == "schedule.cancel":
                if not self.db.execute("UPDATE schedules SET status='cancelled' WHERE id=? AND status='pending'", (args["schedule_id"],)).rowcount:
                    raise StationError("Pending schedule not found.")
            self.db.commit()
        if operation in ("transport.start", "transport.stop"):
            self._emit("transport", state="started" if operation == "transport.start" else "stopped")
        return {"ok": True, "operation": operation}

    def _finish(self, event):
        if self.current:
            self.last_track = self.current["id"]
            self._event(self.current["title"], event)
            self._emit("track_finished", result=event, track=self.public(self.current))
        if self.reader:
            try:
                self.reader.close()
            except OSError:
                pass
        self.current = self.reader = None
        self.elapsed = 0.0
        self._setting("playing", None)

    def _queue_place(self, entry_id, index):
        """Put queue entry ``entry_id`` at play position ``index`` (0 plays next; past the end means last). Positions are
        renumbered 1..n in one pass: the queue holds at most about a thousand entries, and whole numbers never run out of
        room the way halving the gap between neighbours would."""
        order = [row[0] for row in self.db.execute("SELECT id FROM queue ORDER BY pos,id")]
        order.remove(entry_id)
        order.insert(min(index, len(order)), entry_id)
        self.db.executemany("UPDATE queue SET pos=? WHERE id=?", [(place, qid) for place, qid in enumerate(order, 1)])

    def _quarantine(self, track, message):
        self.db.execute("INSERT OR IGNORE INTO media_failures VALUES (?,?,?)", (track["id"], message, utcnow()))
        self._event(track["title"], "media unavailable")

    def _next(self):
        # True: ready, None: failed candidate consumed, False: no candidates.

        entry = self.db.execute("SELECT id,track_id FROM queue ORDER BY pos,id LIMIT 1").fetchone()
        if entry:
            track = self._track(entry["track_id"])
            self.db.execute("DELETE FROM queue WHERE id=?", (entry["id"],))
        else:
            auto = json.loads(self.db.execute("SELECT value FROM settings WHERE key='auto_fill'").fetchone()[0])
            if not auto:
                return False
            # Music-only rotation. Ads and spoken links never repeat without scheduling.
            row = self.db.execute("SELECT * FROM tracks WHERE kind='music' AND id NOT IN (SELECT track_id FROM media_failures) ORDER BY (rowid > COALESCE((SELECT rowid FROM tracks WHERE id=?),0)) DESC, rowid ASC LIMIT 1", (self.last_track or "",)).fetchone()
            if row is None:
                return False
            track = dict(row)
        if self.db.execute("SELECT 1 FROM media_failures WHERE track_id=?", (track["id"],)).fetchone():
            self._event(track["title"], "skipped unavailable")
            return None
        try:
            self.reader = open_media(self.media / track["filename"], track["duration"])
        except MediaError as exc:
            self._quarantine(track, str(exc))
            return None
        self.current = track
        self.elapsed = 0.0
        self.error = None
        self._setting("playing", track["id"])
        self._event(track["title"], "playing")
        self.db.commit()
        self._emit("track_started", track=self.public(track))
        return True

    def tick(self, now=None):
        """Advance exactly one PCM block. Public for deterministic offline verification."""
        with self.lock:
            if self.faulted:
                self._deliver(SILENCE)
                return SILENCE
            return self._tick(now)

    def _tick(self, now):
        with self._transaction():
            now = time.time() if now is None else now
            programs = self.db.execute("SELECT * FROM programs WHERE next_due<=?", (now,)).fetchall()
            due = list(self.db.execute("SELECT * FROM schedules WHERE status='pending' AND due<=? ORDER BY due DESC,id DESC", (now,)).fetchall())
            # Admit earliest events first when capacity is tight, then prepend
            # admitted events in reverse order so playback is chronological too.
            due.extend({"id": p["id"], "track_id": p["track_id"], "due": p["next_due"], "program": p} for p in programs)
            due.sort(key=lambda e: (e["due"], e["id"]))
            slots = max(0, 1000 - self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]) if due else 0
            planned = []
            for event in due:
                # Stale programs are never rebroadcast on restart. Due events while off-air are missed.
                if self.running and now - event["due"] <= 60 and slots:
                    slots -= 1
                    status = "queued"
                else:
                    status = "missed"
                planned.append((event, status))
            for event, status in reversed(planned):
                if status == "queued":
                    self.db.execute(QUEUE_FRONT, (event["track_id"],))
                if isinstance(event, dict) and "program" in event:
                    p = event["program"]
                    # A suspended/offline host never floods the queue with catch-up programs.
                    upcoming = next_occurrence(p["timezone"], p["local_time"], p["weekdays"], now)
                    self.db.execute("UPDATE programs SET next_due=?,last_due=?,last_status=? WHERE id=?",
                                    (upcoming, event["due"], status, event["id"]))
                    self._event(self._track(event["track_id"])["title"], "program " + status)
                else:
                    self.db.execute("UPDATE schedules SET status=? WHERE id=?", (status, event["id"]))
            if due:
                self.db.execute("DELETE FROM schedules WHERE status!='pending' AND id NOT IN (SELECT id FROM schedules WHERE status!='pending' ORDER BY due DESC LIMIT 1000)")
            block = bytearray()
            if self.running:
                # Fill across track boundaries without inserting a silence block.
                attempts = 0
                while len(block) < len(SILENCE) and attempts < 16:
                    if not self.reader:
                        attempts += 1
                        ready = self._next()
                        if ready is False:
                            break
                        if ready is None:
                            continue
                    want = (len(SILENCE) - len(block)) // (WIDTH * CHANNELS)
                    try:
                        pcm = self.reader.readframes(want)
                        remaining = round((self.current["duration"] - self.elapsed) * RATE)
                        expected = min(want, max(0, remaining)) * WIDTH * CHANNELS
                        if len(pcm) != expected:
                            raise OSError("Audio length changed during playback")
                    except (OSError, ValueError, EOFError, wave.Error):
                        self._quarantine(self.current, "Audio could not be read during playback. Repair the original file or reimport it, then retry.")
                        self._finish("playback failed")
                        attempts += 1
                        continue
                    if not pcm:
                        self._finish("finished")
                        attempts += 1
                        continue
                    block.extend(pcm)
                    self.elapsed += len(pcm) / (RATE * CHANNELS * WIDTH)
            block.extend(bytes(len(SILENCE) - len(block)))
            self.db.commit()
            if not self.running and self.broadcast.active:
                self.broadcast.stop()
            self.broadcast.offer(bytes(block))
            self._deliver(bytes(block))
            return bytes(block)

    def _deliver(self, block):
        for subscriber in tuple(self.subscribers):
            try:
                subscriber.put_nowait(block)
            except queue.Full:
                # Slow listeners never block the clock, including in fault mode.
                self.subscribers.discard(subscriber)
                self.dropped += 1
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                subscriber.put_nowait(None)

    def subscribe(self):
        with self.lock:
            if len(self.subscribers) >= 16:
                raise StationError("Local monitor is full (16 connections).")
            subscriber = queue.Queue(maxsize=30)
            self.subscribers.add(subscriber)
            return subscriber

    def unsubscribe(self, subscriber):
        with self.lock:
            self.subscribers.discard(subscriber)

    def _run(self):
        deadline = time.monotonic()
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                with self.lock:
                    self._fail_safe()
            deadline += FRAMES / RATE
            # A suspended host resumes live instead of emitting a CPU-bound catch-up burst.
            if deadline < time.monotonic() - 0.5:
                deadline = time.monotonic()
            self.stop_event.wait(max(0, deadline - time.monotonic()))

    def close(self):
        if getattr(self, "_closed", False):
            return  # closing twice must be harmless
        self._closed = True
        # Extensions may still use the station while stopping, so they stop first.
        self.extensions.stop()
        self.stop_event.set()
        self.broadcast.close()
        if self.worker:
            self.worker.join(timeout=5)
        with self.lock:
            # Keep playing marker for restart recovery; stop the local clock only.
            if self.reader:
                self.reader.close()
            for subscriber in tuple(self.subscribers):
                try:
                    subscriber.put_nowait(None)
                except queue.Full:
                    pass
            self.subscribers.clear()
            self.db.close()
            fcntl.flock(self.process_lock, fcntl.LOCK_UN)
            self.process_lock.close()
