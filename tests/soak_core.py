"""Shared pieces of the soak harness: deterministic audio, scoped engine replacements, an independent
statement of the scheduling rules, process resource probes and the on-disk state checker.

Nothing here changes behaviour outside the process that imports it, and nothing is applied at import time.
Not collected by unittest.
"""
from __future__ import annotations

import collections
import contextlib
import datetime as dt
import inspect
import io
import json
import math
import os
import random
import re
import resource
import shutil
import sqlite3
import stat
import struct
import sys
import textwrap
import time
import types
import uuid
import wave
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from myradio import importing as importing_module  # noqa: E402
from myradio import station as station_module  # noqa: E402
from myradio.station import FRAMES, RATE, SILENCE  # noqa: E402

BLOCK_SECONDS = FRAMES / RATE
BLOCK_BYTES = len(SILENCE)
QUEUE_LIMIT = 1000
HISTORY_LIMIT = 200
SCHEDULE_KEEP = 1000
PENDING_LIMIT = 1000
PROGRAM_LIMIT = 1000
STALE_AFTER = 60.0
ATTEMPTS_PER_TICK = 16
SIM_START = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc).timestamp()
KINDS = ("music", "speech", "ad")
SCHEDULE_STATES = ("pending", "queued", "missed", "cancelled")
# Zones with awkward rules: half-hour and 45-minute offsets, a 30-minute DST step, negative DST, a two-hour
# step, a Ramadan-dependent zone, zones without DST, and plain UTC.
ZONES = ("UTC", "America/New_York", "Europe/London", "Europe/Dublin", "Australia/Lord_Howe", "Asia/Kolkata",
         "Pacific/Chatham", "America/Sao_Paulo", "Asia/Kathmandu", "Antarctica/Troll", "Pacific/Apia",
         "America/St_Johns", "Africa/Casablanca", "Pacific/Kiritimati", "America/Havana", "Europe/Berlin")
DST_ZONES = ("America/New_York", "Europe/London", "Australia/Lord_Howe", "Europe/Dublin", "Pacific/Chatham", "Antarctica/Troll",
             "America/Havana", "Africa/Casablanca", "America/St_Johns", "Europe/Berlin")


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SoakFailure(AssertionError):
    """An invariant did not hold.  ``invariant`` names it; the message says where and why."""

    def __init__(self, message, invariant=""):
        super().__init__(message)
        self.invariant = invariant


# --------------------------------------------------------------------------------------------
# Deterministic audio: every library track is a pattern that identifies it and the position in it.
# --------------------------------------------------------------------------------------------
PERIOD = 13
STRIDE = 16
MAX_SLOTS = 32000 // STRIDE


def sample(slot, index):
    return STRIDE * slot + 1 + index % PERIOD


class Tapes:
    """Frame i of a track in slot s is the sample 16*s + 1 + (i mod 13) on both channels.

    The value says which track a frame came from (s) and where it was (i mod 13), so an exact
    comparison against ``window()`` detects wrong tracks, dropped frames and repeated frames.
    """

    def __init__(self, keep=256):
        self._tapes = collections.OrderedDict()
        self._keep = keep

    def tape(self, slot):
        found = self._tapes.get(slot)
        if found is None:
            one = b"".join(struct.pack("<hh", sample(slot, i), sample(slot, i)) for i in range(PERIOD))
            found = one * ((FRAMES + 2 * PERIOD) // PERIOD + 2)
            self._tapes[slot] = found
            if len(self._tapes) > self._keep:
                self._tapes.popitem(last=False)
        else:
            self._tapes.move_to_end(slot)
        return found

    def window(self, slot, start, count):
        phase = start % PERIOD
        return self.tape(slot)[phase * 4:(phase + count) * 4]

    @staticmethod
    def wav(slot, frames, channels=2):
        if channels == 2:
            one = b"".join(struct.pack("<hh", sample(slot, i), sample(slot, i)) for i in range(PERIOD))
            width = 4
        else:
            one = b"".join(struct.pack("<h", sample(slot, i)) for i in range(PERIOD))
            width = 2
        data = (one * (frames // PERIOD + 1))[:frames * width]
        out = io.BytesIO()
        with wave.open(out, "wb") as target:
            target.setparams((channels, 2, RATE, 0, "NONE", "not compressed"))
            target.writeframes(data)
        return out.getvalue()

    def parse(self, block, max_runs=3):
        """Split a block into runs ``(slot, first_phase, frames)`` (slot None is silence) or return None when it is not made of
        at most ``max_runs`` valid tape runs (the end of one track, the start of the next, and silence).  Real-time listeners use
        this because they cannot see engine events."""
        if len(block) % 4:
            return None
        total, at, runs = len(block) // 4, 0, []
        while at < total:
            rest = block[at * 4:]
            zeros = (len(rest) - len(rest.lstrip(b"\0"))) // 4
            if zeros:
                runs.append((None, 0, zeros))
                at += zeros
                continue
            left, right = struct.unpack_from("<hh", rest)
            if left != right or not 1 <= left < STRIDE * MAX_SLOTS or (left - 1) % STRIDE >= PERIOD:
                return None
            slot, phase = divmod(left - 1, STRIDE)
            limit = min(total - at, FRAMES)
            reference = self.tape(slot)[phase * 4:(phase + limit) * 4]
            low, high = 1, limit
            while low < high:  # longest prefix that still follows the tape
                mid = (low + high + 1) // 2
                if rest[:mid * 4] == reference[:mid * 4]:
                    low = mid
                else:
                    high = mid - 1
            runs.append((slot, phase, low))
            at += low
            if len(runs) > max_runs:
                return None
        return runs


def wav_with(rate=44100, width=2, channels=2, frames=FRAMES):
    """A well formed WAV in a format the engine cannot play natively (silence)."""
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        target.writeframes(bytes(frames * channels * width))
    return out.getvalue()


# --------------------------------------------------------------------------------------------
# Scoped replacements: only myradio.station and myradio.importing see them, nothing else in the process does.
# --------------------------------------------------------------------------------------------
class SimClock:
    def __init__(self, start=SIM_START):
        self.now = float(start)


class _SimTime:
    """Stands in for ``time`` inside myradio.station: time.time() follows the simulated clock."""

    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return self._clock.now

    def __getattr__(self, name):
        return getattr(time, name)


class _SeededUuid:
    def __init__(self, seed):
        self._rng = random.Random(f"uuid:{seed}")

    def uuid4(self):
        return uuid.UUID(int=self._rng.getrandbits(128), version=4)


class _NoFfmpeg:
    """shutil with FFmpeg hidden: imports never start a converter and status is the same on every machine."""

    @staticmethod
    def which(name, *args, **kwargs):
        return None if os.path.basename(str(name)) == "ffmpeg" else shutil.which(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(shutil, name)


class ModuleProxy:
    """A module stand-in that overrides a few attributes and passes everything else through."""

    def __init__(self, real, **overrides):
        self.__dict__.update(_real=real, _over=overrides)

    def __getattr__(self, name):
        return self._over[name] if name in self._over else getattr(self._real, name)


def source_mutant(function, old, new, module):
    """A copy of ``function`` whose source text has ``old`` replaced by ``new``: an engine that is wrong in exactly one place, for tests that
    show the harness notices.

    ``old`` must occur exactly once, so that if the engine's code changes the test fails loudly instead of quietly testing nothing.  The copy
    runs in the live namespace of ``module`` (not a snapshot of it), so the harness's replacements of the clock and of the id generator are
    seen by the mutant exactly as they are by the real code."""
    source = textwrap.dedent(inspect.getsource(function))
    if source.count(old) != 1:
        raise AssertionError(f"the engine's {function.__name__} no longer contains {old!r} exactly once ({source.count(old)} times): update this mutant")
    scratch = {}
    exec(compile(source.replace(old, new), f"<mutant of {function.__name__}>", "exec"), dict(module.__dict__), scratch)
    built = scratch[function.__name__]
    mutant = types.FunctionType(built.__code__, module.__dict__, built.__name__, built.__defaults__, built.__closure__)
    mutant.__kwdefaults__ = built.__kwdefaults__
    return mutant


@contextlib.contextmanager
def relaxed_commits():
    """Every connection the station opens skips fsync (PRAGMA synchronous=OFF, set before anything is written).

    For runs whose checks never depend on surviving a power cut: they read committed data through a second connection,
    so skipping fsync changes the speed and nothing they can observe.  The engine's own setting is synchronous=FULL.
    """
    real = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    with mock.patch.object(station_module, "sqlite3", ModuleProxy(sqlite3, connect=connect)):
        yield


@contextlib.contextmanager
def no_converter():
    """Real clock and real ids, but FFmpeg hidden (the real-time and fault modes use this)."""
    with mock.patch.object(station_module, "shutil", _NoFfmpeg()), mock.patch.object(importing_module, "shutil", _NoFfmpeg()):
        yield


@contextlib.contextmanager
def deterministic_engine(clock, seed):
    with mock.patch.object(station_module, "time", _SimTime(clock)), mock.patch.object(station_module, "uuid", _SeededUuid(seed)), no_converter():
        yield


@contextlib.contextmanager
def offline_agent_env():
    """Chat uses the offline command assistant only when no model endpoint is configured."""
    saved = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("MYRADIO_")}
    try:
        yield
    finally:
        os.environ.update(saved)


def make_chat(station):
    from myradio.chat import Chat  # imported late: the station part of the harness does not need it
    with offline_agent_env():
        chat = Chat(station)
    if chat.endpoint or station.chat_mode != "commands":
        raise SoakFailure("the chat assistant is not offline; refusing to continue", "chat-offline")
    return chat


# --------------------------------------------------------------------------------------------
# Process resources
# --------------------------------------------------------------------------------------------
def rss_bytes():
    try:
        with open("/proc/self/statm") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return peak_rss_bytes()


def peak_rss_bytes():
    """The process's high-water mark of resident memory (VmHWM; getrusage's ru_maxrss reads a little lower on Linux)."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def fd_count():
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(path)) - 1  # the listing holds one descriptor of its own
        except OSError:
            continue
    return -1


def open_descriptors():
    """The numbers of the descriptors this process holds open right now (empty when the system offers no way to list them).

    Each number is checked with fstat, so the descriptor the listing itself used (already closed by then) is not counted."""
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            numbers = [int(name) for name in os.listdir(path) if name.isdigit()]
        except OSError:
            continue
        found = set()
        for number in numbers:
            try:
                os.fstat(number)
            except OSError:
                continue
            found.add(number)
        return found
    return set()


def close_descriptors_of(path, candidates):
    """Close every descriptor among ``candidates`` that points at ``path`` and nothing else; returns how many were closed.

    A test that leaks descriptors on purpose (to show a defect) uses this to clean up after itself without ever closing a
    descriptor that belongs to somebody else."""
    try:
        target = os.stat(path)
    except OSError:
        return 0
    closed = 0
    for number in sorted(candidates):
        try:
            if os.path.samestat(os.fstat(number), target):
                os.close(number)
                closed += 1
        except OSError:
            continue
    return closed


class Histogram:
    """Fixed size log histogram (8 buckets per octave): constant memory over any run length."""

    def __init__(self):
        self.counts = [0] * 256
        self.total = 0
        self.maximum = 0.0
        self.sum = 0.0

    def add(self, seconds):
        micros = seconds * 1e6
        index = 0 if micros < 1 else min(255, int(math.log2(micros) * 8) + 1)
        self.counts[index] += 1
        self.total += 1
        self.sum += seconds
        if seconds > self.maximum:
            self.maximum = seconds

    def percentile(self, q):
        if not self.total:
            return 0.0
        goal, seen = self.total * q, 0
        for index, count in enumerate(self.counts):
            seen += count
            if seen >= goal:
                return 0.0 if index == 0 else 2 ** (index / 8) / 1e6
        return self.maximum


# --------------------------------------------------------------------------------------------
# An independent statement of the scheduling rules (it does not call myradio.scheduling)
# --------------------------------------------------------------------------------------------
_ZONE_CACHE = {}


def tz(name):
    zone = _ZONE_CACHE.get(name)
    if zone is None:
        zone = _ZONE_CACHE[name] = ZoneInfo(name)
    return zone


def available_zones(names=ZONES):
    out = []
    for name in names:
        try:
            tz(name)
            out.append(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return out


def wall_instants(zone, wall):
    """Every UTC instant, ascending, at which ``zone``'s wall clock reads the naive datetime ``wall``.

    Built from the offsets actually in force around that wall time and verified by converting back, so a
    gap gives no instant and a repeated hour gives two.
    """
    found = []
    for shift in (-1, 0, 1):
        offset = (wall + dt.timedelta(days=shift)).replace(tzinfo=zone).utcoffset()
        stamp = (wall - offset).replace(tzinfo=dt.timezone.utc).timestamp()
        back = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(zone).replace(tzinfo=None)
        if back == wall and stamp not in found:
            found.append(stamp)
    return sorted(found)


def oracle_next_due(zone_name, local_time, weekdays, after):
    """First occurrence strictly after ``after``.  A wall time that does not exist (spring forward) is skipped
    and a repeated one (fall back) airs once, at its first pass."""
    zone = tz(zone_name)
    hour, minute = int(local_time[:2]), int(local_time[3:])
    days = {int(day) for day in weekdays.split(",")}
    start = dt.datetime.fromtimestamp(after, dt.timezone.utc).astimezone(zone).date()
    for offset in range(-1, 17):
        day = start + dt.timedelta(days=offset)
        if day.isoweekday() not in days:
            continue
        instants = wall_instants(zone, dt.datetime(day.year, day.month, day.day, hour, minute))
        if instants and instants[0] > after:
            return instants[0]
    raise AssertionError("no occurrence within 16 days")


def zone_transitions(zone_name, start, end):
    """``(first_second_on_the_new_offset, offset_before, offset_after)`` for every UTC offset change."""
    zone = tz(zone_name)

    def offset(stamp):
        return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(zone).utcoffset()

    start, end = int(start), int(end)
    out, at = [], start
    previous = offset(at)
    while at < end:
        ahead = min(at + 6 * 3600, end)
        current = offset(ahead)
        if current != previous:
            low, high = at, ahead
            while high - low > 1:
                mid = (low + high) // 2
                if offset(mid) == previous:
                    low = mid
                else:
                    high = mid
            out.append((high, previous, current))
        previous, at = current, ahead
    return out


class RecurrenceResult:
    def __init__(self, compared, mismatches, zones, with_transitions, transitions, wall):
        self.compared, self.mismatches, self.zones = compared, mismatches, zones
        self.with_transitions, self.transitions, self.wall = with_transitions, transitions, wall

    @property
    def ok(self):
        return self.compared > 0 and not self.mismatches

    def text(self):
        lines = [f"result: {'PASS' if self.ok else 'FAIL'}",
                 f"  zones checked {self.zones}, of which {self.with_transitions} change their UTC offset ({self.transitions} changes) in the window",
                 f"  occurrences compared against myradio.scheduling.next_occurrence: {self.compared:,}   disagreements: {len(self.mismatches)}   wall time: {self.wall:.1f} s"]
        for zone, hhmm, weekdays, after, want, got in self.mismatches[:10]:
            lines.append(f"  DISAGREE {zone} {hhmm} days {weekdays} after {iso(after)}: independent rule {iso(want)}, engine {got if isinstance(got, str) else iso(got)}")
        return "\n".join(lines)


def cross_check_recurrence(zone_names=None, window=(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(2029, 1, 1, tzinfo=dt.timezone.utc)), seed=42, random_per_zone=40):
    """Compare ``oracle_next_due`` with the engine's own recurrence for many (zone, time, weekdays, after) combinations, concentrated
    around every UTC offset change in the window: both sides of each change, local times from 90 minutes before to 2 hours after,
    and ``after`` from five days before to three days after."""
    from myradio.scheduling import next_occurrence
    from zoneinfo import available_timezones
    began = time.perf_counter()
    rng = random.Random(seed)
    zone_names = sorted(zone_names or available_timezones())
    start, end = window[0].timestamp(), window[1].timestamp()
    compared, mismatches, with_transitions, total = 0, [], 0, 0
    for zone in zone_names:
        transitions = zone_transitions(zone, start, end)
        with_transitions += bool(transitions)
        total += len(transitions)
        samples = []
        for stamp, before, after_offset in transitions:
            utc = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).replace(tzinfo=None)
            for wall in (utc + before, utc + after_offset):
                for minutes in (-90, -61, -60, -31, -30, -1, 0, 1, 29, 30, 31, 59, 60, 61, 119, 120, 121):
                    moment = wall + dt.timedelta(minutes=minutes)
                    for shift in (-5 * 86400, -2 * 86400, -36000, -3600, -1, 0, 1, 3600, 40000, 86400, 3 * 86400):
                        samples.append((f"{moment.hour:02d}:{moment.minute:02d}", stamp + shift))
        samples += [(f"{rng.randrange(24):02d}:{rng.randrange(60):02d}", rng.uniform(start, end)) for _ in range(random_per_zone)]
        for hhmm, after in samples:
            weekdays = rng.choice(("1,2,3,4,5,6,7", "7", "1", "6,7", "1,3,5", "2,4", "5"))
            want = oracle_next_due(zone, hhmm, weekdays, after)
            try:
                got = next_occurrence(zone, hhmm, weekdays, after)
            except ValueError as exc:
                got = f"ValueError {exc}"
            compared += 1
            if got != want:
                mismatches.append((zone, hhmm, weekdays, after, want, got))
    return RecurrenceResult(compared, mismatches, len(zone_names), with_transitions, total, time.perf_counter() - began)


# --------------------------------------------------------------------------------------------
# State of a station database, read the same way for a live engine and for a closed directory
# --------------------------------------------------------------------------------------------
MISSING = "<missing>"  # what db_state reports for a settings row the engine should have written and did not


def setting_value(db, key, default=MISSING):
    """The stored (JSON text) value of one setting, or ``default`` when the row is not there."""
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def db_state(db):
    """The persistent state of a station as plain tuples.  Timestamps that depend on the wall clock are left out.

    A row the engine should have written but did not shows up as ``MISSING`` instead of an exception, so that the checks
    that compare states can name the problem."""
    return {
        "settings": tuple((r[0], r[1]) for r in db.execute("SELECT key,value FROM settings WHERE key!='playing' ORDER BY key")),
        "playing": setting_value(db, "playing"),
        "library": tuple(tuple(r) for r in db.execute("SELECT id,title,artist,duration,kind,filename FROM tracks ORDER BY rowid")),
        "failures": tuple(r[0] for r in db.execute("SELECT track_id FROM media_failures ORDER BY track_id")),
        "queue": tuple((r[0], r[1]) for r in db.execute("SELECT id,track_id FROM queue ORDER BY id")),
        "schedules": tuple(tuple(r) for r in db.execute("SELECT id,track_id,at,due,status FROM schedules ORDER BY id")),
        "programs": tuple(tuple(r) for r in db.execute("SELECT * FROM programs ORDER BY id")),
        "history": tuple(tuple(r) for r in db.execute("SELECT id,title,event FROM history ORDER BY id")),
    }


def persisted_state(directory):
    """``db_state`` of a data directory through its own connection: only committed data, as after a crash."""
    conn = sqlite3.connect(str(Path(directory) / "station.sqlite3"), timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        return db_state(conn)
    finally:
        conn.close()


def check_persisted(directory, *, library=None, damaged=frozenset(), expect_files=True):
    """Re-check everything that must hold in a station data directory.  Opens its own connection, so it sees
    only committed data (what a crash would leave).  Returns a list of plain-text problems."""
    directory = Path(directory)
    database = directory / "station.sqlite3"
    if not database.exists():
        return ["station.sqlite3 is missing"]
    problems = []
    conn = sqlite3.connect(str(database), timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        tracks = _check_rows(conn, library, problems)
    except sqlite3.DatabaseError as exc:
        return problems + [f"the database cannot be read: {exc}"]
    finally:
        conn.close()
    if expect_files:
        _check_files(directory / "media", tracks, damaged, problems)
    return problems


def _check_rows(conn, library, problems):
    verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if verdict != ["ok"]:
        problems.append("integrity_check: " + "; ".join(map(str, verdict[:3])))
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        problems.append(f"foreign_key_check reports {len(violations)} violation(s)")
    tracks = {row[0]: row for row in conn.execute("SELECT id,title,artist,duration,kind,filename FROM tracks")}
    queue = conn.execute("SELECT id,track_id FROM queue ORDER BY id").fetchall()
    if len(queue) > QUEUE_LIMIT:
        problems.append(f"queue holds {len(queue)} rows (limit {QUEUE_LIMIT})")
    if len({row[0] for row in queue}) != len(queue):
        problems.append("queue ids are not unique")
    for _, track_id in queue:
        if track_id not in tracks:
            problems.append(f"queue refers to unknown track {track_id}")
            break
    history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    if history > HISTORY_LIMIT:
        problems.append(f"history holds {history} rows (limit {HISTORY_LIMIT})")
    counts = collections.Counter(row[0] for row in conn.execute("SELECT status FROM schedules"))
    unknown = sorted(set(counts) - set(SCHEDULE_STATES), key=repr)
    if unknown:
        problems.append(f"schedules with unknown status {unknown}")
    if counts["pending"] > PENDING_LIMIT:
        problems.append(f"{counts['pending']} pending schedules (limit {PENDING_LIMIT})")
    finished = sum(counts.values()) - counts["pending"]
    if finished > SCHEDULE_KEEP:
        problems.append(f"{finished} finished or cancelled schedules kept (limit {SCHEDULE_KEEP})")
    for row in conn.execute("SELECT track_id FROM schedules"):
        if row[0] not in tracks:
            problems.append("a schedule refers to an unknown track")
            break
    programs = conn.execute("SELECT id,track_id,timezone,local_time,weekdays,next_due,last_status FROM programs").fetchall()
    if len(programs) > PROGRAM_LIMIT:
        problems.append(f"{len(programs)} recurring programs (limit {PROGRAM_LIMIT})")
    for pid, track_id, _zone, local_time, weekdays, next_due, last_status in programs:
        if track_id not in tracks:
            problems.append(f"program {pid} refers to an unknown track")
        if not (isinstance(local_time, str) and isinstance(weekdays, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time)
                and re.fullmatch(r"[1-7](?:,[1-7])*", weekdays)):
            problems.append(f"program {pid} has a malformed rule")
        if not isinstance(next_due, (int, float)) or not math.isfinite(next_due):
            problems.append(f"program {pid} has no usable next time")
        if last_status not in (None, "queued", "missed"):
            problems.append(f"program {pid} has status {last_status!r}")
    settings, present = {}, set()
    for key, value in conn.execute("SELECT key,value FROM settings"):
        present.add(key)
        try:
            settings[key] = json.loads(value)
        except (TypeError, ValueError):
            problems.append(f"setting {key!r} is not stored as readable data")
    if "playing" not in present:
        problems.append("the playing marker row is missing from the settings")
    if not isinstance(settings.get("name"), str) or not 1 <= len(settings["name"]) <= 200:
        problems.append("station name is missing or out of bounds")
    if not isinstance(settings.get("tagline"), str):
        problems.append("station tagline is missing")
    if not isinstance(settings.get("auto_fill"), bool):
        problems.append("auto_fill is missing or not a boolean")
    playing = settings.get("playing")
    if playing is not None and (not isinstance(playing, str) or playing not in tracks):
        problems.append("the playing marker names a track that is not in the library")
    for row in conn.execute("SELECT track_id FROM media_failures"):
        if row[0] not in tracks:
            problems.append("a quarantine record has no track")
            break
    for track_id, row in tracks.items():
        try:
            frames = row[3] * RATE
            consistent = row[4] in KINDS and row[5] == track_id + ".wav" and abs(frames - round(frames)) <= 1e-6 and frames >= FRAMES
        except (TypeError, ValueError, OverflowError):  # a length that is not a number
            consistent = False
        if not consistent:
            problems.append(f"track {track_id} has inconsistent fields")
    if library is not None:
        if set(tracks) != set(library):
            problems.append(f"library ids differ: database has {len(tracks)}, expected {len(library)}")
        else:
            for track_id, (title, artist, kind, duration) in library.items():
                row = tracks[track_id]
                if (row[1], row[2], row[4], row[3]) != (title, artist, kind, duration):
                    problems.append(f"track {track_id} differs from what was imported")
                    break
    return tracks


def _check_files(media, tracks, damaged, problems):
    names = sorted(os.listdir(media)) if media.is_dir() else []
    for name in names:
        if not re.fullmatch(r"[0-9a-f]{32}\.wav", name):
            problems.append(f"unexpected file in media: {name}")
        elif name[:-4] not in tracks:
            problems.append(f"orphan media file {name}")
    present = set(names)
    for track_id, row in tracks.items():
        if track_id in damaged:
            continue
        if row[5] not in present:
            problems.append(f"media file missing for track {track_id}")
            continue
        info = os.lstat(media / row[5])
        try:
            wanted = 44 + 4 * round(row[3] * RATE)
        except (TypeError, ValueError, OverflowError):
            continue  # the row itself was already reported as inconsistent
        if not stat.S_ISREG(info.st_mode) or info.st_size != wanted:
            problems.append(f"media file for track {track_id} has the wrong shape")
