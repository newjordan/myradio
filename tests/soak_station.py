#!/usr/bin/env python3
"""Load and fault harness for the myradio station engine.  Not collected by unittest (the file name does
not match test_*.py); run it directly:

    python3 tests/soak_station.py accelerated --seed 7 --steps 300000 --max-wall 110
    python3 tests/soak_station.py realtime --seconds 20
    python3 tests/soak_station.py faults
    python3 tests/soak_station.py crash --cycles 6
    python3 tests/soak_station.py findings

Accelerated mode drives ``Station(start_worker=False)`` with ``tick(now)`` against a simulated clock.
Nothing sleeps, no clock thread runs and no network is touched.  What the engine reads from the clock
(``time.time``) and the ids it generates (uuid4) are replaced inside ``myradio.station`` only, so a run is a
pure function of ``--seed`` and ``--steps``.

It checks the engine's decisions (the audio it emits, the queue, schedules, recurrence, quarantine,
restart recovery) against a second, independent statement of the rules.  What a run does and does not prove
is written in docs/lanes/soak.md.  It is not a multi-day run on a real clock, and it never touches a model, a
GPU or a real listener.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import gc
import hashlib
import json
import os
import random
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from soak_core import (ATTEMPTS_PER_TICK, BLOCK_BYTES, BLOCK_SECONDS, DST_ZONES, FRAMES, KINDS, QUEUE_LIMIT, RATE, SILENCE,  # noqa: E402
                       SIM_START, STALE_AFTER, Histogram, SimClock, SoakFailure, Tapes, available_zones, check_persisted, db_state,
                       deterministic_engine, fd_count, iso, make_chat, oracle_next_due, peak_rss_bytes, relaxed_commits, rss_bytes, setting_value, tz,
                       wall_instants, wav_with, zone_transitions)
from myradio.station import Station, StationError  # noqa: E402

HARNESS_VERSION = 2  # 2: new damage mode and directed scenarios (a valid file of another length, programs off air, delete guards, capacity in every run)
# A track whose media path has become a directory leaks one descriptor per open attempt (reports/soak-findings.md), so
# the random operator does not create that case; it is covered by the findings repro instead.
DAMAGE_MODES = ("delete", "garbage", "wrong_rate", "symlink", "fifo", "extra_bytes", "truncate_header", "truncate_half", "wrong_length")
IN_PLACE = ("extra_bytes", "truncate_header", "truncate_half")
# How far a "wrong_length" file is from the length the library recorded, in frames (44,100 frames are one second).  The engine allows one frame
# of rounding; every one of these is outside it, and the large ones are the kind of error a loosened tolerance would let through.
LENGTH_ERRORS = (2, 441, 4410, 44100 + 1, 3 * 44100, -2, -441)
RSS_EVERY_SWEEPS = 10  # memory is sampled at least this often (in sweeps) as well as once per wall second, so that short runs take enough samples
TITLE_TAILS = ("", " Ünïcödé", " 日本語", " 🎵", " it's \"quoted\"", " <b>x</b>", " 100% %s", "   ", " tab\there")
ARTISTS = ("", "Artist", "Ünïcödé Band", "A" * 200)
# Things an operator, a chat message or an agent might send that the engine must refuse in plain words.  Inputs that
# currently escape as raw exceptions are listed in reports/soak-findings.md and covered by the findings repros.
BAD_ACTIONS = (
    ("transport.start", {"x": 1}), ("queue.add", {}), ("queue.add", {"track_id": 5}), ("queue.add", {"track_id": "0" * 32}),
    ("nope.nothing", {}), (None, {}), ([], {}), ("queue.remove", {"queue_id": "abc"}), ("queue.remove", {"queue_id": True}),
    ("queue.remove", {"queue_id": 987654321}), ("queue.remove", {"queue_id": 1.5}), ("station.update", {"auto_fill": "yes"}),
    ("station.update", {"name": ""}), ("station.update", {"name": "x" * 201}), ("station.update", {"name": 5}),
    ("station.update", {"unknown": 1}), ("station.update", {"tagline": "   "}), ("station.update", {"name": "ok", "tagline": ""}),
    ("schedule.cancel", {"schedule_id": "zzz"}), ("program.remove", {"program_id": "zzz"}), ("library.retry", {"track_id": "0" * 32}),
    ("library.delete", {"track_id": "0" * 32}), ("queue.add", None), ("transport.skip", []), ("schedule.add", {"track_id": "x"}),
)
OPERATOR_WEIGHTS = {"import": 12, "import_invalid": 3, "queue_add": 12, "queue_burst": 1, "queue_remove": 6, "schedule_add": 7,
                    "schedule_bad": 2, "schedule_cancel": 3, "program_add": 4, "program_bad": 2, "program_remove": 3, "start": 3, "stop": 3,
                    "skip": 6, "retire": 3, "delete_in_use": 2, "damage": 4, "repair": 5, "retry_damaged": 2, "retry_healthy": 1, "update": 3,
                    "update_bad": 1, "chat": 7, "bad_args": 2, "status": 3}
HOP_OFFSETS = (-1.0, -0.25, 0.0, 0.0, 0.3, 4.0, 17.5, 33.0, 59.9, 60.0, 60.0, 60.2, 61.5, 90.0)


def file_block_size(path):
    """The block size the file system reports for ``path`` (4,096 on most local file systems; a network file system may report a megabyte).
    A file that is open is read through a buffer of this size, which decides how far behind the read position a cut has a certain effect."""
    return os.stat(path).st_blksize


# --------------------------------------------------------------------------------------------
# What the harness knows about the world it created
# --------------------------------------------------------------------------------------------
class Track:
    __slots__ = ("id", "title", "artist", "kind", "frames", "slot", "channels", "damaged")

    def __init__(self, track_id, title, artist, kind, frames, slot, channels):
        self.id, self.title, self.artist, self.kind = track_id, title, artist, kind
        self.frames, self.slot, self.channels, self.damaged = frames, slot, channels, None


class Play:
    __slots__ = ("track", "played")

    def __init__(self, track):
        self.track, self.played = track, 0

    @property
    def remaining(self):
        return self.track.frames - self.played


class World:
    def __init__(self):
        self.tracks = {}  # id -> Track; insertion order is import order, which is the engine's rowid order
        self.by_title = {}
        self.free_slots = []
        self.next_slot = 0
        self.ordinal = 0
        self.quarantined = set()  # membership tests only: never iterated for a decision
        self.doomed = set()  # tracks whose open file was truncated behind the playhead
        self.play = None
        self.last_track = None
        self.running = False

    def alloc_slot(self):
        if self.free_slots:
            return self.free_slots.pop(0)
        from soak_core import MAX_SLOTS
        if self.next_slot >= MAX_SLOTS:
            raise SoakFailure("the harness ran out of distinct audio signatures", "harness")
        self.next_slot += 1
        return self.next_slot - 1

    def release_slot(self, slot):
        self.free_slots.append(slot)
        self.free_slots.sort()

    def add(self, track):
        self.tracks[track.id] = track
        self.by_title[track.title] = track

    def remove(self, track_id):
        track = self.tracks.pop(track_id)
        self.by_title.pop(track.title, None)
        self.quarantined.discard(track_id)
        self.release_slot(track.slot)

    def library_view(self):
        return {t.id: (t.title, t.artist, t.kind, t.frames / RATE) for t in self.tracks.values()}

    def healthy_music(self):
        return [t for t in self.tracks.values() if t.kind == "music" and not t.damaged and t.id not in self.quarantined]


class EventLog:
    """Records what the engine tells extensions (track started, track finished, transport) without running one."""

    def __init__(self):
        self.items = []

    def hook(self, station):
        original = station._emit

        def record(kind, **data):
            self.items.append((kind, data))
            return original(kind, **data)

        station._emit = record

    def drain(self):
        items, self.items = self.items, []
        return items


class Metrics:
    def __init__(self):
        self.counters = collections.Counter()
        self.checks = collections.Counter()
        self.failed = collections.Counter()
        self.ops = collections.Counter()
        self.tick_time = Histogram()
        self.sweep_time = Histogram()
        self.queue_peak = self.library_peak = self.pending_peak = self.programs_peak = 0
        self.fd_min = self.thread_min = 10 ** 9
        self.fd_max = self.thread_max = 0
        self.rss_samples = []  # (wall seconds, bytes)


# --------------------------------------------------------------------------------------------
# The harness
# --------------------------------------------------------------------------------------------
class Harness:
    """One accelerated run: a seeded operator, a simulated clock and a long list of checks.

    Checks come in tiers.  After every tick: audio exactness, elapsed bounds, reader/current agreement, no open
    transaction, queue and history limits.  After every tick that starts or ends a track or admits an event: the
    queue law, the admission rule, recurrence against an independent rule, quarantine verdicts, the playing marker.
    After every operator step and every ``sweep_every`` ticks: the on-disk state, status, descriptors, threads
    and memory.
    """

    def __init__(self, directory, *, seed=1, steps=20000, max_wall=None, keep_going=False, trace=False,
                 sweep_every=400, rss_growth_mb=64.0, strict=False, fast_commits=False, zones=None, out=None):
        self.seed, self.max_steps, self.max_wall = seed, steps, max_wall
        self.fast_commits = fast_commits
        self.keep_going, self.trace, self.strict, self.out = keep_going, trace, strict, out or sys.stdout
        self.sweep_every, self.rss_growth = sweep_every, rss_growth_mb * 1024 * 1024
        self.rng = random.Random(f"operator:{seed}")
        self.dir = Path(directory) / "station"
        self.clock = SimClock()
        self.tapes = Tapes()
        self.world = World()
        self.metrics = Metrics()
        self.events = EventLog()
        self.zones = list(zones) if zones else available_zones()
        self.digest = hashlib.sha256()
        self.audio_crc = 0
        self.failures = []
        self.recent = collections.deque(maxlen=40)
        self.steps = 0
        self.station = None
        self.chat = None
        self.truncated = False
        self.started_at = time.perf_counter()
        self.fd_idle = None
        self.thread_base = threading.active_count()
        self.fd_base = fd_count()
        self.ticks_since_sweep = 0
        self.sweeps_since_sample = 0
        self.program_log = []  # (program id, instant) for every recurring program that was queued or missed

    # ---- bookkeeping -------------------------------------------------------------------
    def log(self, line):
        self.digest.update(line.encode("utf-8", "replace") + b"\n")
        self.recent.append(line)
        if self.trace:
            print(line, file=self.out)

    def check(self, name, ok, detail=""):
        self.metrics.checks[name] += 1
        if not ok:
            self.fail(name, detail() if callable(detail) else detail)

    def fail(self, name, detail):
        record = {"invariant": name, "detail": str(detail)[:1500], "step": self.steps, "tick": self.metrics.counters["ticks"],
                  "sim": iso(self.clock.now), "recent": list(self.recent)[-12:]}
        if len(self.failures) < 50:
            self.failures.append(record)
        self.metrics.failed[name] += 1
        if not self.keep_going:
            raise SoakFailure(f"{name} at step {self.steps} (simulated {record['sim']}): {record['detail']}", name)

    def over_budget(self):
        return self.max_wall is not None and time.perf_counter() - self.started_at > self.max_wall

    def at_most(self, name, have, want, what):
        """Descriptors and threads: exact in a dedicated process, an upper bound inside a shared test process
        where unrelated objects may be released at any time."""
        self.check(name, have == want if self.strict else have <= want, lambda: f"{have} {what}, expected {want}")

    # ---- station lifecycle -------------------------------------------------------------
    def open_station(self):
        self.station = Station(self.dir, start_worker=False)
        self.events.hook(self.station)
        self.chat = make_chat(self.station)
        self.world.play = None
        self.world.running = False
        self.world.doomed.clear()
        self.world.last_track = None

    def close_station(self):
        station, self.station = self.station, None
        if station is not None:
            station.close()

    # ---- snapshots ---------------------------------------------------------------------
    def snapshot(self):
        st = self.station
        state = db_state(st.db)
        state.update(running=st.running, current=st.current["id"] if st.current else None, last_track=st.last_track)
        return state

    @staticmethod
    def changed(before, after, allowed):
        return [key for key in before if key not in allowed and before[key] != after[key]]

    @staticmethod
    def new_history(before, after):
        top = before["history"][-1][0] if before["history"] else 0
        return [(row[1], row[2]) for row in after["history"] if row[0] > top]

    # ---- one tick ----------------------------------------------------------------------
    def tick(self):
        st, w, now = self.station, self.world, self.clock.now
        db = st.db
        sched = db.execute("SELECT id,track_id,due FROM schedules WHERE status='pending' AND due<=?", (now,)).fetchall()
        progs = db.execute("SELECT id,track_id,next_due,timezone,local_time,weekdays FROM programs WHERE next_due<=?", (now,)).fetchall()
        running = st.running
        pre = None
        if sched or progs or (running and (w.play is None or w.play.remaining <= FRAMES or w.doomed)):
            pre = self.capture(sched, progs, running)
        began = time.perf_counter()
        try:
            block = st.tick(now)
        except Exception as exc:
            self.fail("tick-raised", f"{type(exc).__name__}: {exc}")
            raise
        self.metrics.tick_time.add(time.perf_counter() - began)
        events = self.events.drain()
        self.verify_audio(block, events, running)
        if pre is not None:
            self.verify_deep(pre, now)
        self.verify_light()
        self.audio_crc = zlib.crc32(block, self.audio_crc)
        self.clock.now = now + BLOCK_SECONDS
        self.steps += 1
        self.metrics.counters["ticks"] += 1
        self.ticks_since_sweep += 1
        if self.ticks_since_sweep >= self.sweep_every:
            self.sweep("periodic")
        return block

    def capture(self, sched, progs, running):
        db = self.station.db
        due = sorted([(r[2], r[0], r[1], None) for r in sched] + [(r[2], r[0], r[1], (r[3], r[4], r[5])) for r in progs], key=lambda e: (e[0], e[1]))
        return {
            "running": running,
            "queue": [(r[0], r[1]) for r in db.execute("SELECT id,track_id FROM queue ORDER BY id")],
            "failures": {r[0] for r in db.execute("SELECT track_id FROM media_failures")},
            "history": db.execute("SELECT COALESCE(MAX(id),0) FROM history").fetchone()[0],
            "due": due,
            "auto_fill": json.loads(setting_value(db, "auto_fill", "true")),  # a missing row is the engine's problem to report, not a crash here
            "last_track": self.world.last_track,
        }

    # ---- audio: the block must be exactly what gapless playout of the reported tracks produces ----
    def verify_audio(self, block, events, running):
        w, m = self.world, self.metrics
        self.check("audio-block-size", type(block) is bytes and len(block) == BLOCK_BYTES, lambda: f"block is {len(block)} bytes")
        if not running:
            self.check("audio-silent-when-stopped", block == SILENCE and not events, "audio or events while stopped")
            m.counters["silence_blocks"] += 1
            return
        segments, need, play = [], FRAMES, w.play
        for kind, data in events:
            if kind == "track_finished":
                track_id, result = data["track"]["id"], data["result"]
                self.check("event-order", play is not None and play.track.id == track_id, lambda: f"finish of {track_id} but playing {play and play.track.id}")
                if result == "finished":
                    take = play.remaining
                    self.check("audio-finish-fits", take <= need, lambda: f"track claims to finish with {take} frames unplayed but only {need} fit")
                    self.check("audio-truncated-file", not take or track_id not in w.doomed, "audio was delivered from a file truncated behind the playhead")
                    if take:
                        segments.append((play.track.slot, play.played, take))
                        need, play.played = need - take, play.played + take
                    m.counters["tracks_finished"] += 1
                elif result == "playback failed":
                    take = min(need, play.remaining)
                    self.check("playback-failure-cause", track_id in w.doomed and take > 0,
                               lambda: f"playback failed for {play.track.title} but the harness had not damaged its open file")
                    m.counters["playback_failures"] += 1
                else:
                    self.fail("event-order", f"unexpected finish reason {result!r} inside a tick")
                w.last_track = track_id
                w.doomed.discard(track_id)
                w.play = play = None
            elif kind == "track_started":
                track_id = data["track"]["id"]
                self.check("event-order", play is None and track_id in w.tracks, lambda: f"start of {track_id} while {play and play.track.id} is playing")
                w.play = play = Play(w.tracks[track_id])
                self.check("event-metadata", abs(data["track"]["duration"] - play.track.frames / RATE) < 1e-9, "started track reports another duration")
                m.counters["tracks_started"] += 1
            else:
                self.fail("event-order", f"unexpected {kind!r} event inside a tick")
        if play is not None and need > 0 and play.remaining > 0:
            take = min(need, play.remaining)
            self.check("audio-truncated-file", play.track.id not in w.doomed, "audio was delivered from a file truncated behind the playhead")
            segments.append((play.track.slot, play.played, take))
            need, play.played = need - take, play.played + take
        expected = self.tapes.window(*segments[0]) if len(segments) == 1 and need == 0 else b"".join(self.tapes.window(*s) for s in segments) + bytes(need * 4)
        m.checks["audio-exact"] += 1
        if block != expected:
            at = next((i for i in range(0, BLOCK_BYTES, 4) if block[i:i + 4] != expected[i:i + 4]), 0) // 4
            self.fail("audio-exact", f"frame {at} of the block differs: got {block[at * 4:at * 4 + 4].hex()} expected {expected[at * 4:at * 4 + 4].hex()}; "
                                     f"segments {segments} silence {need}")
        m.counters["music_blocks" if segments else "silence_blocks"] += 1
        m.counters["audio_frames_verified"] += FRAMES

    # ---- cheap checks after every tick -------------------------------------------------------
    def verify_light(self):
        st, w = self.station, self.world
        self.check("no-open-transaction", not st.db.in_transaction, "a database transaction is still open after the tick")
        self.check("not-faulted", not st.faulted and st.error is None, lambda: f"station latched a fault: {st.error}")
        current = st.current
        self.check("reader-matches-current", (st.reader is None) == (current is None), "reader and current track disagree")
        self.check("elapsed-bounds", 0.0 <= st.elapsed <= (current["duration"] + 1e-6 if current else 0.0),
                   lambda: f"elapsed {st.elapsed} outside 0..{current and current['duration']}")
        play = w.play
        self.check("model-matches-engine", (play is None) == (current is None) and (play is None or (play.track.id == current["id"] and abs(st.elapsed - play.played / RATE) < 1e-6)),
                   lambda: f"engine plays {current and current['id']} at {st.elapsed}, model {play and (play.track.id, play.played / RATE)}")
        self.check("last-track", st.last_track == w.last_track, "engine and model disagree about the last finished track")
        queued = st.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        self.check("queue-limit", queued <= QUEUE_LIMIT, lambda: f"queue holds {queued} rows")
        history = st.db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        self.check("history-limit", history <= 200, lambda: f"history holds {history} rows")
        self.metrics.queue_peak = max(self.metrics.queue_peak, queued)

    # ---- full checks after a boundary or an event --------------------------------------------
    def verify_deep(self, pre, now):
        st, w, db, m = self.station, self.world, self.station.db, self.metrics
        plan, slots = [], max(0, QUEUE_LIMIT - len(pre["queue"])) if pre["due"] else 0
        for due, event_id, track_id, rule in pre["due"]:
            if pre["running"] and now - due <= STALE_AFTER and slots:
                slots -= 1
                status = "queued"
            else:
                status = "missed"
            plan.append((due, event_id, track_id, rule, status))
        admitted = [track_id for _, _, track_id, _, status in plan if status == "queued"]
        history = db.execute("SELECT id,title,event FROM history WHERE id>? ORDER BY id", (pre["history"],)).fetchall()
        program_rows = collections.Counter()
        for due, event_id, track_id, rule, status in plan:
            if rule is None:
                got = db.execute("SELECT status FROM schedules WHERE id=?", (event_id,)).fetchone()
                self.check("schedule-admission", got is not None and got[0] == status, lambda: f"one-shot due {iso(due)} became {got and got[0]}, expected {status}")
                m.counters[f"schedules_{status}"] += 1
            else:
                got = db.execute("SELECT next_due,last_due,last_status FROM programs WHERE id=?", (event_id,)).fetchone()
                want = oracle_next_due(rule[0], rule[1], rule[2], now)
                self.check("program-admission", got is not None and got[2] == status and got[1] == due,
                           lambda: f"program due {iso(due)} recorded {got and (got[2], got[1])}, expected {status}")
                self.check("program-next-due", got is not None and got[0] == want,
                           lambda: f"{rule[0]} {rule[1]} {rule[2]}: next time {got and iso(got[0])}, independent rule gives {iso(want)}")
                program_rows[(w.tracks[track_id].title, "program " + status)] += 1
                m.counters[f"programs_{status}"] += 1
                self.program_log.append((event_id, due))
        got_program_rows = collections.Counter((r[1], r[2]) for r in history if r[2].startswith("program "))
        self.check("program-history", got_program_rows == program_rows, "history rows for recurring programs do not match the plan")
        self.check("nothing-overdue", db.execute("SELECT COUNT(*) FROM schedules WHERE status='pending' AND due<=?", (now,)).fetchone()[0] == 0
                   and db.execute("SELECT COUNT(*) FROM programs WHERE next_due<=?", (now,)).fetchone()[0] == 0, "an event that was due is still waiting")
        # Replay the selections the engine recorded against the queue it should have had.
        waiting = admitted + [t for _, t in pre["queue"]]
        quarantined = set(pre["failures"])
        last, started, selections, finishes = pre["last_track"], [], 0, 0
        rows, i = list(history), 0
        while i < len(rows):
            _, title, event = rows[i]
            i += 1
            track = w.by_title.get(title)
            if event in ("finished", "playback failed"):
                finishes += 1
                if track:
                    last = track.id
                continue
            if event == "media unavailable" and i < len(rows) and rows[i][2] == "playback failed" and rows[i][1] == title:
                quarantined.add(track.id)  # the open file became unreadable while playing
                w.quarantined.add(track.id)
                m.counters["quarantined"] += 1
                continue
            if event not in ("playing", "skipped unavailable", "media unavailable"):
                continue
            selections += 1
            self.check("selection-known-track", track is not None, f"history names an unknown track {title!r}")
            if waiting:
                head = waiting.pop(0)
                self.check("queue-order", head == track.id, lambda: f"engine took {title!r} but the queue head was {w.tracks[head].title!r}")
            else:
                self.check("autofill-allowed", pre["auto_fill"], "a track was chosen from an empty queue with auto-fill off")
                self.check("autofill-music-only", track.kind == "music", f"auto-fill chose a {track.kind} track")
                self.check("autofill-not-quarantined", track.id not in quarantined, f"auto-fill chose quarantined {title!r}")
                pick = self.rotation(last, quarantined)
                self.check("autofill-rotation", pick is not None and pick == track.id, lambda: f"auto-fill chose {title!r}, rotation says {pick and w.tracks[pick].title!r}")
            if track.id in quarantined:
                want_event = "skipped unavailable"
            elif track.damaged:
                want_event = "media unavailable"
            else:
                want_event = "playing"
            self.check("media-verdict", event == want_event, lambda: f"{title!r} (damage: {track.damaged}) was reported {event!r}, expected {want_event!r}")
            if event == "media unavailable":
                quarantined.add(track.id)
                w.quarantined.add(track.id)
                m.counters["quarantined"] += 1
            elif event == "skipped unavailable":
                m.counters["skipped_unavailable"] += 1
            else:
                started.append(track.id)
        self.check("attempt-bound", selections + finishes <= ATTEMPTS_PER_TICK + 1, f"{selections} selections and {finishes} finishes in one tick")
        got_queue = [(r[0], r[1]) for r in db.execute("SELECT id,track_id FROM queue ORDER BY id")]
        self.check("queue-law", [t for _, t in got_queue] == waiting, lambda: f"queue after the tick is {len(got_queue)} long, replay expects {len(waiting)}")
        old_ids = {qid for qid, _ in pre["queue"]}
        kept = [row for row in got_queue if row[0] in old_ids]
        self.check("queue-ids-kept", kept == pre["queue"][len(pre["queue"]) - len(kept):], "surviving queue rows changed identity or order")
        fresh = [row[0] for row in got_queue if row[0] not in old_ids]
        self.check("queue-prepend-ids", fresh == sorted(fresh) and (not kept or not fresh or fresh[-1] < kept[0][0]), "prepended events are not ahead of the old queue")
        marker = setting_value(db, "playing")
        self.check("playing-marker", marker == json.dumps(st.current["id"] if st.current else None), lambda: f"marker {marker} but current is {st.current and st.current['id']}")
        if st.running and st.current is None and selections + finishes < ATTEMPTS_PER_TICK:
            healthy = [t for t in w.tracks.values() if t.kind == "music" and t.id not in quarantined]
            self.check("silence-is-justified", not waiting and (not pre["auto_fill"] or not healthy), "silence while a playable track was queued or available for auto-fill")
        db_failures = {r[0] for r in db.execute("SELECT track_id FROM media_failures")}
        self.check("quarantine-matches", db_failures == w.quarantined, lambda: f"database quarantines {len(db_failures)}, model {len(w.quarantined)}")
        m.counters["deep_checks"] += 1

    def rotation(self, last, quarantined):
        """The next auto-fill track by the documented rule: music only, import order, after the last one played."""
        order = [t.id for t in self.world.tracks.values()]
        candidates = [tid for tid in order if self.world.tracks[tid].kind == "music" and tid not in quarantined]
        if not candidates:
            return None
        if last in self.world.tracks:
            index = order.index(last)
            for tid in candidates:
                if order.index(tid) > index:
                    return tid
        return candidates[0]

    # ---- full sweep ----------------------------------------------------------------------
    def sweep(self, reason):
        began = time.perf_counter()
        st, w, m = self.station, self.world, self.metrics
        self.ticks_since_sweep = 0
        damaged = {t.id for t in w.tracks.values() if t.damaged}
        for problem in check_persisted(self.dir, library=w.library_view(), damaged=damaged):
            self.fail("persisted-state", problem)
        m.checks["persisted-state"] += 1
        self.check("no-open-transaction", not st.db.in_transaction, "a database transaction is open between steps")
        self.check("not-faulted", not st.faulted and st.error is None, lambda: f"latched: {st.error}")
        status = st.status()
        try:
            encoded, serialisable = json.dumps(status, allow_nan=False), True
        except (TypeError, ValueError) as exc:
            encoded, serialisable = "", False
            self.fail("status-json", f"status cannot be serialised: {exc}")
        self.check("status-json", serialisable and json.loads(encoded) == status, "status JSON did not round trip: it holds something that is not plain JSON data")
        db = st.db
        queue_ids = [r[0] for r in db.execute("SELECT id FROM queue ORDER BY id")]
        self.check("status-queue", [q["id"] for q in status["queue"]] == queue_ids, "status queue differs from the database")
        self.check("status-library", len(status["library"]) == len(w.tracks), "status library size differs from what was imported")
        self.check("status-running", status["running"] == st.running == w.running, "running flag differs between status, engine and model")
        self.check("status-bounds", len(status["history"]) <= 30 and len(status["schedules"]) <= 100, "status lists exceed their stated bounds")
        quarantined = {r[0] for r in db.execute("SELECT track_id FROM media_failures")}
        self.check("quarantine-matches", quarantined == w.quarantined, "quarantine records differ from the model")
        want = "stopped" if not st.running else "waiting" if not st.current else "degraded" if quarantined else "playing"
        self.check("health-state", status["health"]["state"] == want, lambda: f"health {status['health']['state']!r}, expected {want!r}")
        self.check("health-counts", status["health"]["ready_tracks"] + status["health"]["unavailable_tracks"] == len(w.tracks), "ready + unavailable != library")
        self.check("stream-idle", status["stream"] == {"listeners": 0, "dropped": 0}, "listener counters moved with no listeners")
        self.check("broadcast-idle", status["broadcast"]["active"] is False and status["broadcast"]["state"] == "disconnected", "external broadcast changed on its own")
        m.pending_peak = max(m.pending_peak, db.execute("SELECT COUNT(*) FROM schedules WHERE status='pending'").fetchone()[0])
        m.library_peak = max(m.library_peak, len(w.tracks))
        m.programs_peak = max(m.programs_peak, db.execute("SELECT COUNT(*) FROM programs").fetchone()[0])
        fds, threads = fd_count(), threading.active_count()
        if self.fd_idle is None and w.tracks and st.reader is None:
            self.fd_idle = fds  # calibrated once the first import has opened every long-lived file
        if self.fd_idle is not None:
            self.at_most("file-descriptors", fds, self.fd_idle + (1 if st.reader is not None else 0), "open descriptors")
        self.at_most("threads", threads, self.thread_base, "threads")
        m.fd_min, m.fd_max = min(m.fd_min, fds), max(m.fd_max, fds)
        m.thread_min, m.thread_max = min(m.thread_min, threads), max(m.thread_max, threads)
        wall = time.perf_counter() - self.started_at
        self.sweeps_since_sample += 1
        # Once per wall second, and also every RSS_EVERY_SWEEPS sweeps: a short run (the in-suite smoke takes about two seconds) would
        # otherwise take too few samples for the memory check to run at all.
        if not m.rss_samples or wall - m.rss_samples[-1][0] >= 1.0 or self.sweeps_since_sample >= RSS_EVERY_SWEEPS:
            gc.collect()
            m.rss_samples.append((wall, rss_bytes()))
            self.sweeps_since_sample = 0
        m.sweep_time.add(time.perf_counter() - began)
        m.counters["sweeps"] += 1

    def rss_growth_after_warmup(self):
        """Growth between the end of warm-up (first quarter of the samples) and the highest later sample."""
        samples = self.metrics.rss_samples
        if len(samples) < 4:
            return None
        warm = samples[len(samples) // 4][1]
        return max(s[1] for s in samples[len(samples) // 4:]) - warm

    # ---- operator steps ------------------------------------------------------------------
    def do(self, name, label, call, *, fail=False, touches=(), expect=None, success=None):
        """One operator step.  A refused step must change nothing; an accepted step may change only what it
        documents (``touches``) and must have the documented effect (``expect``); then every sweep check runs."""
        self.steps += 1
        self.metrics.ops[name] += 1
        before = self.snapshot()
        error = result = None
        try:
            result = call()
        except StationError as exc:
            error = str(exc)
        except Exception as exc:
            self.fail("unexpected-exception", f"{name} {label}: {type(exc).__name__}: {exc}")
            raise
        self.apply_action_events()
        after = self.snapshot()
        if fail:
            self.check("rejection-expected", error is not None, f"{name} {label} was accepted")
        else:
            self.check("acceptance-expected", error is None, f"{name} {label} was refused: {error}")
        if error is not None:
            moved = self.changed(before, after, ())
            self.check("rejection-changes-nothing", not moved, lambda: f"{name} {label} was refused but changed {moved}")
            self.check("error-is-plain", bool(error) and "Traceback" not in error and "sqlite" not in error.lower(), f"unfriendly message: {error!r}")
            self.metrics.counters["rejected_as_expected" if fail else "rejected_unexpectedly"] += 1
        else:
            if success:
                success(result)
            moved = self.changed(before, after, touches)
            self.check("accepted-step-frame", not moved, lambda: f"{name} {label} changed {moved}, which it does not document")
            for problem in (expect(before, after, result) if expect else ()):
                self.fail(f"{name}-effect", problem)
            self.metrics.counters["accepted"] += 1
        self.log(f"{self.steps}|{name}|{label}|{'ok' if error is None else error}")
        self.sweep(name)
        return error is None, result

    def apply_action_events(self):
        for kind, data in self.events.drain():
            if kind == "track_finished":
                self.check("action-finish-reason", data["result"] in ("stopped", "skipped"), f"unexpected finish {data['result']!r} from an operator step")
                self.world.last_track = data["track"]["id"]
                self.world.doomed.discard(data["track"]["id"])
                self.world.play = None
                self.metrics.counters["tracks_finished"] += 1
            elif kind == "transport":
                self.world.running = data["state"] == "started"
            else:
                self.fail("event-order", f"unexpected {kind!r} event from an operator step")

    def pick(self, predicate=None):
        pool = [t for t in self.world.tracks.values() if predicate is None or predicate(t)]
        return self.rng.choice(pool) if pool else None

    def queue_len(self):
        return self.station.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def in_use(self, track_id):
        db, st = self.station.db, self.station
        return bool((st.current and st.current["id"] == track_id)
                    or db.execute("SELECT 1 FROM queue WHERE track_id=?", (track_id,)).fetchone()
                    or db.execute("SELECT 1 FROM schedules WHERE track_id=? AND status='pending'", (track_id,)).fetchone()
                    or db.execute("SELECT 1 FROM programs WHERE track_id=?", (track_id,)).fetchone())

    # ---- the individual operator actions ---------------------------------------------------
    def op_import(self, kind=None, frames=None, channels=None):
        w, rng = self.world, self.rng
        kind = kind or rng.choices(KINDS, (70, 15, 15))[0]
        frames = frames or rng.choice((FRAMES, FRAMES + 1, rng.randint(FRAMES, FRAMES * 4), rng.randint(FRAMES, FRAMES * 12), rng.randint(FRAMES * 10, FRAMES * 30)))
        channels = channels or (1 if rng.random() < 0.3 else 2)
        w.ordinal += 1
        slot = w.alloc_slot()
        title = f"T{w.ordinal:05d}{rng.choice(TITLE_TAILS)}"
        artist = rng.choice(ARTISTS)
        data = Tapes.wav(slot, frames, channels)
        made = []

        def effect(before, after, result):
            row = after["library"][-1] if after["library"] else None
            problems = []
            if len(after["library"]) != len(before["library"]) + 1 or row is None or row[0] != result["id"]:
                problems.append("library did not gain exactly the imported track at the end")
            elif row[1:] != (title.strip(), artist.strip(), frames / RATE, kind, result["id"] + ".wav"):
                problems.append(f"stored fields differ from the import: {row[1:]}")
            if self.new_history(before, after) != [(title.strip(), "imported")]:
                problems.append("history does not show exactly one import")
            if (result["title"], result["kind"], result["available"], result["playback_error"]) != (title.strip(), kind, True, None) or "filename" in result:
                problems.append(f"import reply has the wrong shape: {sorted(result)}")
            return problems

        def success(result):
            track = Track(result["id"], title.strip(), artist.strip(), kind, frames, slot, channels)
            w.add(track)
            made.append(track)

        ok, _ = self.do("import", f"{title!r} {kind} {frames}f {channels}ch", lambda: self.station.import_audio(data, title, artist, kind),
                        touches=("library", "history"), expect=effect, success=success)
        if not ok:
            w.release_slot(slot)
        return made[0] if made else None

    def op_import_invalid(self):
        rng = self.rng
        good = Tapes.wav(0, FRAMES)
        variants = [
            ("empty", b"", "Bad", "", "music"), ("garbage", bytes(rng.getrandbits(8) for _ in range(300)), "Bad", "", "music"),
            ("truncated", good[:-4000], "Bad", "", "music"), ("too short", Tapes.wav(0, FRAMES - 1), "Bad", "", "music"),
            ("48kHz needs converter", wav_with(rate=48000), "Bad", "", "music"), ("8-bit needs converter", wav_with(width=1), "Bad", "", "music"),
            ("three channels", wav_with(channels=3), "Bad", "", "music"), ("bad kind", good, "Bad", "", "podcast"),
            ("blank title", good, "   ", "", "music"), ("long title", good, "t" * 201, "", "music"), ("long artist", good, "ok", "a" * 201, "music"),
            ("title not text", good, None, "", "music"), ("artist not text", good, "ok", 7, "music"), ("header only", good[:44], "Bad", "", "music"),
        ]
        name, data, title, artist, kind = variants[rng.randrange(len(variants))]
        self.do("import_invalid", name, lambda: self.station.import_audio(data, title, artist, kind), fail=True)

    def op_queue_add(self, track=None):
        track = track or self.pick()
        if track is None or self.queue_len() >= 300:
            return

        def effect(before, after, _):
            tail = [t for _, t in before["queue"]] + [track.id]
            return [] if [t for _, t in after["queue"]] == tail and after["queue"][:-1] == before["queue"] else ["queue did not gain exactly one entry at the tail"]

        self.do("queue_add", track.title, lambda: self.station.action("queue.add", {"track_id": track.id}), touches=("queue",), expect=effect)

    def op_queue_burst(self):
        for _ in range(self.rng.randint(5, 40)):
            self.op_queue_add()

    def op_queue_remove(self):
        rows = self.station.db.execute("SELECT id FROM queue ORDER BY id").fetchall()
        if not rows:
            return
        qid = rows[self.rng.randrange(len(rows))][0]
        self.do("queue_remove", str(qid), lambda: self.station.action("queue.remove", {"queue_id": qid}), touches=("queue",),
                expect=lambda b, a, r: [] if a["queue"] == tuple(x for x in b["queue"] if x[0] != qid) else ["queue did not lose exactly that entry"])

    def op_schedule_add(self):
        track = self.pick()
        if track is None or self.station.db.execute("SELECT COUNT(*) FROM schedules WHERE status='pending'").fetchone()[0] >= 60:
            return
        rng = self.rng
        delta = rng.choice((2, 9, 31, 75, 200, 600, 3000, 86400 * 3, 86400 * 200))
        zone = dt.timezone(dt.timedelta(hours=rng.choice((0, 0, -8, 5, 13, -3)), minutes=rng.choice((0, 0, 30))))
        text = dt.datetime.fromtimestamp(self.clock.now + delta + rng.random(), zone).isoformat()
        if text.endswith("+00:00") and rng.random() < 0.5:
            text = text[:-6] + "Z"
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))

        def effect(before, after, result):
            fresh = [r for r in after["schedules"] if r not in before["schedules"]]
            ok = len(fresh) == 1 and fresh[0][1:] == (track.id, parsed.isoformat(), parsed.timestamp(), "pending")
            return [] if ok else [f"schedule row is wrong: {fresh}"]

        self.do("schedule_add", f"{track.title} +{delta}s", lambda: self.station.action("schedule.add", {"track_id": track.id, "at": text}),
                touches=("schedules",), expect=effect)

    def op_schedule_bad(self):
        track = self.pick()
        if track is None:
            return
        now, utc = self.clock.now, dt.timezone.utc
        cases = [("naive", dt.datetime.fromtimestamp(now + 600, utc).replace(tzinfo=None).isoformat()), ("not a date", "tomorrow at noon"), ("empty", ""),
                 ("in the past", dt.datetime.fromtimestamp(now - 5, utc).isoformat()), ("a microsecond ago", dt.datetime.fromtimestamp(now - 1e-6, utc).isoformat()),
                 ("too far", dt.datetime.fromtimestamp(now + 366 * 86400 + 5, utc).isoformat())]
        name, text = cases[self.rng.randrange(len(cases))]
        self.do("schedule_bad", name, lambda: self.station.action("schedule.add", {"track_id": track.id, "at": text}), fail=True)

    def op_schedule_cancel(self):
        rows = self.station.db.execute("SELECT id FROM schedules WHERE status='pending' ORDER BY id").fetchall()
        if not rows:
            return
        sid = rows[self.rng.randrange(len(rows))][0]
        self.do("schedule_cancel", sid[:8], lambda: self.station.action("schedule.cancel", {"schedule_id": sid}), touches=("schedules",),
                expect=lambda b, a, r: [] if [x[4] for x in a["schedules"] if x[0] == sid] == ["cancelled"] else ["schedule was not cancelled"])
        self.do("schedule_cancel_again", sid[:8], lambda: self.station.action("schedule.cancel", {"schedule_id": sid}), fail=True)

    def op_program_add(self, zone=None, local_time=None, weekdays=None, track=None, label="random"):
        track = track or self.pick()
        if track is None or self.station.db.execute("SELECT COUNT(*) FROM programs").fetchone()[0] >= 12 or not self.zones:
            return None
        rng = self.rng
        zone = zone or rng.choice(self.zones)
        if local_time is None:
            local_now = dt.datetime.fromtimestamp(self.clock.now, dt.timezone.utc).astimezone(tz(zone))
            local_time = (local_now + dt.timedelta(minutes=rng.choice((1, 2, 5, 17, 45, 130, 700)))).strftime("%H:%M")
        if weekdays is None:
            weekdays = rng.choice(("1,2,3,4,5,6,7", "1,2,3,4,5,6,7", "7,1,3,3", "1,3,5", "6,7", str(rng.randint(1, 7))))
        want = ",".join(map(str, sorted({int(d) for d in weekdays.split(",")})))
        due = oracle_next_due(zone, local_time, want, self.clock.now)

        def effect(before, after, result):
            fresh = [r for r in after["programs"] if r not in before["programs"]]
            ok = len(fresh) == 1 and fresh[0][1:] == (track.id, zone, local_time, want, due, None, None)
            return [] if ok else [f"program row is wrong: {fresh}, independent rule gives {iso(due)}"]

        args = {"track_id": track.id, "timezone": zone, "local_time": local_time, "weekdays": weekdays}
        ok, _ = self.do("program_add", f"{label} {zone} {local_time} {weekdays}", lambda: self.station.action("program.add", args),
                        touches=("programs",), expect=effect)
        return self.station.db.execute("SELECT id FROM programs ORDER BY rowid DESC LIMIT 1").fetchone()[0] if ok else None

    def op_program_bad(self):
        track = self.pick()
        if track is None:
            return
        cases = [("unknown zone", "Mars/Phobos", "10:00", "1"), ("24:00", "UTC", "24:00", "1"), ("short time", "UTC", "7:5", "1"), ("minute 60", "UTC", "12:60", "1"),
                 ("weekday 0", "UTC", "10:00", "0"), ("weekday 8", "UTC", "10:00", "8"), ("empty weekdays", "UTC", "10:00", ""), ("double comma", "UTC", "10:00", "1,,2"),
                 ("space", "UTC", "10:00", "1 2"), ("lowercase zone", "utc", "10:00", "1"), ("path zone", "../../etc/passwd", "10:00", "1"),
                 ("long zone", "A" * 101, "10:00", "1"), ("zone not text", 5, "10:00", "1"), ("time not text", "UTC", 1000, "1")]
        name, zone, local_time, weekdays = cases[self.rng.randrange(len(cases))]
        args = {"track_id": track.id, "timezone": zone, "local_time": local_time, "weekdays": weekdays}
        self.do("program_bad", name, lambda: self.station.action("program.add", args), fail=True)

    def op_program_remove(self):
        rows = self.station.db.execute("SELECT id FROM programs ORDER BY id").fetchall()
        if not rows:
            return
        pid = rows[self.rng.randrange(len(rows))][0]
        self.do("program_remove", pid[:8], lambda: self.station.action("program.remove", {"program_id": pid}), touches=("programs",),
                expect=lambda b, a, r: [] if a["programs"] == tuple(x for x in b["programs"] if x[0] != pid) else ["wrong program removed"])

    def op_start(self):
        library = bool(self.world.tracks)
        self.do("start", "library" if library else "empty library", lambda: self.station.action("transport.start"), fail=not library, touches=("running",),
                expect=lambda b, a, r: [] if a["running"] else ["not running after start"])

    def op_stop(self):
        st, w = self.station, self.world
        current = st.current["id"] if st.current else None
        track = w.tracks[current] if current else None

        def effect(before, after, result):
            problems = []
            if after["running"] or after["current"] is not None or after["playing"] != "null":
                problems.append("still running, playing or marked playing after stop")
            if current:
                if [t for _, t in after["queue"]] != [current] + [t for _, t in before["queue"]] or after["queue"][1:] != before["queue"]:
                    problems.append("interrupted track did not return to the head of the queue")
                if self.new_history(before, after) != [(track.title, "stopped")]:
                    problems.append("history does not show the stop")
            elif after["queue"] != before["queue"] or self.new_history(before, after):
                problems.append("stop with nothing playing changed the queue or history")
            return problems

        self.do("stop", track.title if track else "idle", lambda: st.action("transport.stop"),
                touches=("running", "queue", "history", "playing", "current", "last_track"), expect=effect)

    def op_skip(self):
        st = self.station
        track = self.world.tracks[st.current["id"]] if st.current else None

        def effect(before, after, result):
            problems = []
            if self.new_history(before, after) != ([(track.title, "skipped")] if track else []) or after["current"] is not None or after["playing"] != "null":
                problems.append("skip did not finish the current track exactly once")
            if after["queue"] != before["queue"]:
                problems.append("skip changed the queue")
            return problems

        self.do("skip", track.title if track else "idle", lambda: st.action("transport.skip"),
                touches=("history", "playing", "current", "last_track"), expect=effect)

    def op_retire(self):
        """Take a track out of every place that uses it, then delete it (the order a person must follow)."""
        st = self.station
        track = self.pick(lambda t: not (st.current and st.current["id"] == t.id))
        if track is None or len(self.world.tracks) < 8:
            return
        for (qid,) in st.db.execute("SELECT id FROM queue WHERE track_id=?", (track.id,)).fetchall():
            self.do("queue_remove", str(qid), lambda: st.action("queue.remove", {"queue_id": qid}), touches=("queue",))
        for (sid,) in st.db.execute("SELECT id FROM schedules WHERE track_id=? AND status='pending'", (track.id,)).fetchall():
            self.do("schedule_cancel", sid[:8], lambda: st.action("schedule.cancel", {"schedule_id": sid}), touches=("schedules",))
        for (pid,) in st.db.execute("SELECT id FROM programs WHERE track_id=?", (track.id,)).fetchall():
            self.do("program_remove", pid[:8], lambda: st.action("program.remove", {"program_id": pid}), touches=("programs",))
        path = st.directory / "media" / (track.id + ".wav")

        def effect(before, after, result):
            problems = []
            if tuple(r for r in before["library"] if r[0] != track.id) != after["library"]:
                problems.append("library did not lose exactly that track")
            if os.path.lexists(path):
                problems.append("media file still on disk after delete")
            if track.id in after["failures"]:
                problems.append("quarantine record survived the delete")
            return problems

        self.do("delete", track.title, lambda: st.action("library.delete", {"track_id": track.id}), touches=("library", "schedules", "failures"),
                expect=effect, success=lambda r: self.world.remove(track.id))

    def op_delete_in_use(self):
        used = self.pick(lambda t: self.in_use(t.id))
        if used is not None:
            self.do("delete_in_use", used.title, lambda: self.station.action("library.delete", {"track_id": used.id}), fail=True)

    def damage(self, track, mode, delta=None):
        """Break a media file the way a disk, a backup tool or a person might, and tell the model.  Returns the mode that was applied, which is
        not always the one asked for: see below.  ``delta`` (frames, only for "wrong_length") says how far the new length is from the old one."""
        st, w = self.station, self.world
        path = st.directory / "media" / (track.id + ".wav")
        playing = bool(st.current and st.current["id"] == track.id)
        if playing and mode in IN_PLACE:
            # With the file open, only a truncation far behind the read position has a certain outcome (the reader may already hold up to
            # a block of the file in its buffer).  When the file is too short for that, a delete is used instead, and the caller is told.
            room = w.play.remaining * 4 if w.play else 0
            mode = "truncate_header" if room > 4 * max(file_block_size(path), 8192) + 2 * BLOCK_BYTES else "delete"
        self.steps += 1
        self.metrics.ops["damage"] += 1
        if mode == "delete":
            os.unlink(path)
        elif mode == "truncate_header":
            os.truncate(path, 20)
        elif mode == "truncate_half":
            os.truncate(path, os.stat(path).st_size // 2)
        elif mode == "extra_bytes":
            with open(path, "ab") as handle:
                handle.write(b"junk")
        else:
            swap = path.with_name(track.id + ".swap")
            if mode == "garbage":
                swap.write_bytes(bytes(self.rng.getrandbits(8) for _ in range(512)))
            elif mode == "wrong_rate":
                swap.write_bytes(wav_with(rate=48000))
            elif mode == "wrong_length":
                # A perfectly valid file in the engine's own format, only not the length the library recorded.
                delta = delta if delta is not None else self.rng.choice(LENGTH_ERRORS)
                frames = track.frames + delta if track.frames + delta >= FRAMES else track.frames + abs(delta)
                swap.write_bytes(Tapes.wav(track.slot, frames, 2))
            elif mode == "symlink":
                os.symlink("/dev/zero", swap)
            elif mode == "fifo":
                os.mkfifo(swap)
            os.replace(swap, path)  # a new file: a reader that already has the old one open keeps playing it
        track.damaged = mode
        if playing and mode == "truncate_header":
            w.doomed.add(track.id)
        self.log(f"{self.steps}|damage|{track.title} {mode}|{'playing' if playing else 'idle'}")
        self.sweep("damage")
        return mode

    def op_damage(self):
        track = self.pick(lambda t: not t.damaged)
        if track is None or (track.kind == "music" and len(self.world.healthy_music()) <= 2):
            return
        self.damage(track, self.rng.choice(DAMAGE_MODES))

    def op_repair(self):
        w, st = self.world, self.station
        track = self.pick(lambda t: t.damaged or t.id in w.quarantined)
        if track is None:
            return
        if track.damaged:
            path = st.directory / "media" / (track.id + ".wav")
            swap = path.with_name(track.id + ".swap")
            swap.write_bytes(Tapes.wav(track.slot, track.frames, 2))
            os.replace(swap, path)
            track.damaged = None
            self.steps += 1
            self.log(f"{self.steps}|repair|{track.title}|rewritten")
        self.do("retry", f"{track.title} quarantined={track.id in w.quarantined}", lambda: st.action("library.retry", {"track_id": track.id}),
                touches=("failures", "history"), success=lambda r: w.quarantined.discard(track.id),
                expect=lambda b, a, r: [] if self.new_history(b, a) == [(track.title, "media ready")] and track.id not in a["failures"] else ["retry did not clear the quarantine exactly once"])

    def op_retry_damaged(self):
        track = self.pick(lambda t: t.damaged)
        if track is not None:
            self.do("retry_damaged", f"{track.title} {track.damaged}", lambda: self.station.action("library.retry", {"track_id": track.id}), fail=True)

    def op_retry_healthy(self):
        track = self.pick(lambda t: not t.damaged)
        if track is not None:
            self.do("retry_healthy", track.title, lambda: self.station.action("library.retry", {"track_id": track.id}), touches=("failures", "history"),
                    expect=lambda b, a, r: [] if self.new_history(b, a) == [(track.title, "media ready")] else ["no 'media ready' entry"],
                    success=lambda r: self.world.quarantined.discard(track.id))

    def op_update(self):
        rng, args = self.rng, {}
        if rng.random() < 0.6:
            args["name"] = rng.choice(("Radio " + str(rng.randint(1, 99)), "  padded name  ", "Ünïcödé FM 日本語", "N" * 200, "FM 🎵"))
        if rng.random() < 0.4:
            args["tagline"] = rng.choice(("Your station", "t" * 200, "  spaced  ", "quote \" and ' <i>"))
        if rng.random() < 0.5:
            args["auto_fill"] = rng.random() < 0.8

        def effect(before, after, result):
            want = dict(before["settings"])
            for key, value in args.items():
                want[key] = json.dumps(value.strip() if isinstance(value, str) else value)
            return [] if dict(after["settings"]) == want else ["settings differ from the update"]

        self.do("update", ",".join(sorted(args)) or "empty", lambda: self.station.action("station.update", args), touches=("settings",), expect=effect)

    def op_update_bad(self):
        cases = [{"name": ""}, {"name": "x" * 201}, {"auto_fill": "yes"}, {"name": "fine", "tagline": ""}, {"name": "fine", "auto_fill": 1}, {"bogus": True}]
        args = cases[self.rng.randrange(len(cases))]
        self.do("update_bad", json.dumps(args)[:40], lambda: self.station.action("station.update", args), fail=True)

    def op_bad_args(self):
        op, args = BAD_ACTIONS[self.rng.randrange(len(BAD_ACTIONS))]
        self.do("bad_args", f"{op!r} {json.dumps(args)[:40]}", lambda: self.station.action(op, args), fail=True)

    def op_status(self):
        before = self.snapshot()
        self.steps += 1
        self.metrics.ops["status"] += 1
        self.station.status()
        self.metrics.counters["status_polls"] += 1
        self.check("status-is-read-only", not self.changed(before, self.snapshot(), ()), "status() changed station state")

    def op_chat(self):
        rng, st = self.rng, self.station
        said = rng.choice(("start", "stop", "skip", "status", "help", "name Chat FM", "what's playing", "  START  ", "stop station", "skip track", "Status!",
                           "what is playing?", "name " + "n" * 201, "name ", "x" * 4001, "", "   ", "tell me a joke", "start station"))
        command = said.strip().lower().rstrip(".!?")
        direct = {"start": "start", "start station": "start", "stop": "stop", "stop station": "stop", "skip": "skip", "skip track": "skip"}.get(command)
        rename = command.startswith("name ")
        bad = (not 1 <= len(said) <= 4000 or not said.strip() or (direct == "start" and not self.world.tracks)
               or (rename and not 1 <= len(said.strip()[5:].strip()) <= 200))
        touches = ()
        if not bad:
            touches = {"start": ("running",), "stop": ("running", "queue", "history", "playing", "current", "last_track"),
                       "skip": ("history", "playing", "current", "last_track")}.get(direct, ("settings",) if rename else ())
        current = st.current["id"] if st.current else None

        def success(reply):
            self.check("chat-offline", reply["mode"] == "commands" and reply["actions"] == [], "chat left the offline command mode")
            self.check("chat-reply", isinstance(reply["reply"], str) and bool(reply["reply"]), "empty chat reply")

        def effect(before, after, reply):
            problems = []
            if command in ("status", "what's playing", "what is playing"):
                words = f"{len(after['queue'])} queued; {len(after['library'])} in your library."
                if words not in reply["reply"] or reply["reply"].split(".")[0] != ("Running" if after["running"] else "Stopped"):
                    problems.append(f"status reply does not match the station: {reply['reply']!r}")
            if direct == "stop" and current and [t for _, t in after["queue"]][:1] != [current]:
                problems.append("chat stop did not return the playing track to the queue")
            if rename and dict(after["settings"]).get("name") != json.dumps(said.strip()[5:].strip()):
                problems.append("chat rename did not store the new name")
            return problems

        self.do("chat", repr(said[:30]), lambda: self.chat.message(said), fail=bad, touches=touches, expect=effect, success=success)

    # ---- time ---------------------------------------------------------------------------
    def next_event_time(self):
        db = self.station.db
        values = [v for v in (db.execute("SELECT MIN(due) FROM schedules WHERE status='pending'").fetchone()[0],
                              db.execute("SELECT MIN(next_due) FROM programs").fetchone()[0]) if v is not None]
        return min(values) if values else None

    def jump_to(self, target, kind):
        self.steps += 1
        self.metrics.counters[kind] += 1
        self.metrics.counters["sim_seconds_jumped"] += int(abs(target - self.clock.now))
        self.log(f"{self.steps}|{kind}|{iso(self.clock.now)}->{iso(target)}")
        self.clock.now = target

    def hop(self, offsets=HOP_OFFSETS, ticks=None, quiet=False):
        """Jump to the next scheduled event (a moment before, at or after it) and play through its 60-second window."""
        due = self.next_event_time()
        if due is None:
            return False
        self.jump_to(min(max(self.clock.now, due + self.rng.choice(offsets)), self.clock.now + 2 * 86400), "hops")
        self.dense(ticks or self.rng.randint(15, 90), operator_rate=0 if quiet else 1 / 120, bounded=False)
        return True

    def play(self, count):
        """Tick without random operator steps: directed scenarios use this so nothing disturbs what they set up."""
        self.dense(count, operator_rate=0, bounded=False)

    def dense(self, count, operator_rate=1 / 120, bounded=True):
        for _ in range(count):
            if bounded and (self.steps >= self.max_steps or self.over_budget()):
                return
            self.tick()
            if operator_rate and self.rng.random() < operator_rate:
                self.operator_step()

    # ---- the operator ----------------------------------------------------------------------
    def operator_step(self):
        w, rng = self.world, self.rng
        if len(w.healthy_music()) < 3:
            return self.op_import(kind="music")
        weights = dict(OPERATOR_WEIGHTS)
        if not w.running:
            weights["start"], weights["stop"] = 40, 0
        else:
            weights["start"] = 1
        if len(w.tracks) >= 70:
            weights["import"], weights["retire"] = 0, 30
        if len(w.tracks) < 6:
            weights["import"] = 40
        getattr(self, "op_" + rng.choices(list(weights), list(weights.values()))[0])()

    def restart(self):
        """Close the service and open the same directory: nothing may be lost and nothing may start transmitting."""
        st = self.station
        before = self.snapshot()
        had_current = st.current["id"] if st.current else None
        self.steps += 1
        self.metrics.ops["restart"] += 1
        self.events.drain()
        self.close_station()
        self.at_most("close-releases-descriptors", fd_count(), self.fd_base, "descriptors after Station.close()")
        self.at_most("close-releases-threads", threading.active_count(), self.thread_base, "threads after Station.close()")
        self.open_station()
        after = self.snapshot()
        self.check("restart-not-running", not self.station.running and after["current"] is None, "the station came back transmitting")
        want_queue = ([had_current] if had_current else []) + [t for _, t in before["queue"]]
        self.check("restart-queue", [t for _, t in after["queue"]] == want_queue, "the interrupted track did not return to the head of the queue")
        for key in ("settings", "library", "failures", "schedules", "programs", "history"):
            self.check(f"restart-keeps-{key}", before[key] == after[key], f"{key} changed across a restart")
        self.check("restart-marker-cleared", after["playing"] == "null", "playing marker survived a restart")
        self.log(f"{self.steps}|restart|{'with' if had_current else 'without'} current")
        self.metrics.counters["restarts"] += 1
        self.sweep("restart")

    # ---- directed scenarios ------------------------------------------------------------------
    def ensure_running(self):
        if not self.world.running:
            self.op_start()

    def scheduled_at(self, track, when, label):
        """Add a one-shot schedule and return (id, due) of the new pending row."""
        at = dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat()
        self.do("schedule_add", label, lambda: self.station.action("schedule.add", {"track_id": track.id, "at": at}), touches=("schedules",))
        return self.station.db.execute("SELECT id,due FROM schedules WHERE status='pending' ORDER BY rowid DESC LIMIT 1").fetchone()

    def status_of(self, schedule_id):
        return self.station.db.execute("SELECT status FROM schedules WHERE id=?", (schedule_id,)).fetchone()[0]

    def scn_stale_boundary(self):
        """An event is queued up to 60 s after its time and missed after that; events due while off air are missed."""
        self.ensure_running()
        ad = self.pick(lambda t: t.kind == "ad" and not t.damaged) or self.pick(lambda t: not t.damaged)
        for delta, want in ((-0.1, "pending"), (0.0, "queued"), (59.9, "queued"), (60.0, "queued"), (60.1, "missed"), (3600.0, "missed")):
            sid, due = self.scheduled_at(ad, self.clock.now + 30, f"boundary {delta}")
            self.jump_to(due + delta, "hops")
            self.tick()
            self.check("stale-boundary", self.status_of(sid) == want, lambda: f"event {delta}s after its time is {self.status_of(sid)}, expected {want}")
            if want == "pending":
                self.play(3)
                self.check("stale-boundary", self.status_of(sid) == "queued", "an event not yet due did not queue once its time came")
            self.play(5)
        self.op_stop()
        sid, due = self.scheduled_at(ad, self.clock.now + 20, "off air")
        self.jump_to(due + 1, "hops")
        self.tick()
        self.check("off-air-missed", self.status_of(sid) == "missed", "an event due while the station was stopped was queued")
        self.op_start()

    def scn_capacity(self):
        """Admission with only four free queue slots, then the real 1000 entry limit.  Fixture rows reach the limit quickly."""
        st = self.station
        self.ensure_running()
        self.do("update", "auto_fill off", lambda: st.action("station.update", {"auto_fill": False}), touches=("settings",))
        filler = self.pick(lambda t: t.kind == "music" and not t.damaged)
        extras = [self.pick(lambda t: not t.damaged) for _ in range(5)]
        st.db.execute("DELETE FROM queue")
        st.db.executemany("INSERT INTO queue(track_id) VALUES (?)", [(filler.id,)] * 996)
        st.db.commit()
        self.log(f"{self.steps}|fixture|queue set to 996 entries")
        base = self.clock.now
        for index, track in enumerate(extras):
            self.scheduled_at(track, base + 5 + index * 2, f"capacity {index}")
        self.jump_to(base + 20, "hops")
        self.tick()
        for _ in range(QUEUE_LIMIT - self.queue_len()):
            st.action("queue.add", {"track_id": filler.id})
        self.check("queue-fills-to-limit", self.queue_len() == QUEUE_LIMIT, "the queue could not be filled to its limit")
        self.do("queue_add", "past the limit", lambda: st.action("queue.add", {"track_id": filler.id}), fail=True)
        st.db.execute("DELETE FROM queue")
        st.db.commit()
        self.log(f"{self.steps}|fixture|queue emptied")
        self.do("update", "auto_fill on", lambda: st.action("station.update", {"auto_fill": True}), touches=("settings",))

    def scn_dst(self, zone, kind):
        """Programs set inside a spring-forward gap and a fall-back repeat: the gap is skipped, the repeat airs once."""
        found = [t for t in zone_transitions(zone, self.clock.now, self.clock.now + 400 * 86400) if (t[2] > t[1]) == (kind == "gap")]
        if not found or zone not in self.zones:
            return False
        stamp, before, after = found[0]
        utc = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).replace(tzinfo=None)
        low, high = sorted((utc + before, utc + after))  # the gap, or the hour that happens twice
        minute = lambda moment: moment.replace(second=0, microsecond=0)  # noqa: E731
        inside = minute(low + (high - low) / 2)
        edges = {"inside": inside if low <= inside < high else minute(low), "first": minute(low), "before": minute(low - dt.timedelta(minutes=1)), "after": minute(high)}
        local_day = edges["inside"].date()
        track = self.pick(lambda t: t.kind == "speech" and not t.damaged) or self.pick(lambda t: not t.damaged)
        for (pid,) in self.station.db.execute("SELECT id FROM programs").fetchall():
            self.do("program_remove", pid[:8], lambda: self.station.action("program.remove", {"program_id": pid}), touches=("programs",))
        self.ensure_running()
        self.jump_to(stamp - 40 * 3600, "warps")
        ids = {label: self.op_program_add(zone=zone, local_time=wall.strftime("%H:%M"), weekdays="1,2,3,4,5,6,7", track=track, label=f"{kind}-{label}")
               for label, wall in edges.items()}
        mark = len(self.program_log)
        for _ in range(60):
            if not self.hop(offsets=(0.0, 1.0, 20.0), quiet=True) or self.clock.now > stamp + 40 * 3600:
                break  # (never cut short by the wall-time limit: a half-played scenario would fail its own verdict for no engine fault)
        info = tz(zone)
        by_day = {label: collections.Counter(dt.datetime.fromtimestamp(d, dt.timezone.utc).astimezone(info).date() for pid, d in self.program_log[mark:] if pid == program_id)
                  for label, program_id in ids.items()}
        day = dt.timedelta(days=1)
        if kind == "gap":
            self.check("dst-gap-skipped", local_day not in by_day["inside"], f"{zone}: a time inside the spring-forward gap aired on {local_day}")
            self.check("dst-gap-neighbours", by_day["inside"][local_day - day] == 1 and by_day["inside"][local_day + day] == 1,
                       lambda: f"{zone}: the day before and after the gap did not each air once: {dict(by_day['inside'])}")
        else:
            self.check("dst-fold-once", by_day["inside"][local_day] == 1, lambda: f"{zone}: a repeated time aired {by_day['inside'][local_day]} times on {local_day}")
            first = wall_instants(info, edges["inside"])[0]
            aired = [d for pid, d in self.program_log[mark:] if pid == ids["inside"]]
            self.check("dst-fold-first-pass", any(abs(d - first) < 1 for d in aired), f"{zone}: the repeated time did not air at its first pass")
        for label in ("before", "after"):
            self.check("dst-edges", by_day[label][local_day] <= 1, f"{zone}: the {label} edge aired more than once on the transition day")
        self.metrics.counters[f"dst_{kind}_scenarios"] += 1
        for pid in ids.values():
            if pid:
                self.do("program_remove", pid[:8], lambda: self.station.action("program.remove", {"program_id": pid}), touches=("programs",))
        return True

    def scn_quarantine_modes(self):
        """Every way a file can go bad: it must be quarantined once, never played, and other tracks must keep playing.  A file of another
        length is tried at several distances from the right one, so that a loosened length check shows up whatever size it was loosened to."""
        self.ensure_running()
        plan = [(mode, None) for mode in DAMAGE_MODES if mode != "wrong_length"] + [("wrong_length", delta) for delta in LENGTH_ERRORS]
        for mode, delta in plan:
            track = self.op_import(kind="music", frames=FRAMES * 3)
            self.op_queue_add(track)
            self.op_queue_add(track)
            self.damage(track, mode, delta)
        self.play(200)
        for _ in range(3 * len(plan)):
            if not any(t.damaged or t.id in self.world.quarantined for t in self.world.tracks.values()):
                break
            self.op_repair()
            self.play(5)

    def scn_midplay_truncation(self):
        """A file cut short while it is playing: the read fails, the playback path itself quarantines the track, the clock and the next track
        carry on.

        The track is a spoken one, which auto-fill never chooses, and it is checked that it is not waiting in the queue a second time: otherwise
        the engine would pick it again in the very tick that failed it, find the damaged file, and quarantine it by that other route, and a
        playback path that forgot to quarantine would go unnoticed.  The quarantine record is read from the database right after the failing
        tick for the same reason."""
        st, w = self.station, self.world
        self.ensure_running()
        # Long enough that the reader cannot already hold the whole file in its buffer (see damage()); on a file system with huge blocks the
        # track is made proportionally longer.
        frames = max(FRAMES * 40, max(file_block_size(st.directory / "media"), 8192) + 8 * FRAMES)
        track = self.op_import(kind="speech", frames=frames)
        sid, _ = self.scheduled_at(track, self.clock.now + 3, "play next")  # a due event goes to the head of the queue
        for _ in range(60):
            if self.status_of(sid) == "queued":
                break
            self.tick()
        self.check("midplay-setup", self.status_of(sid) == "queued", "the scheduled track never reached the queue")
        if not (w.play is not None and w.play.track is track):
            self.op_skip()  # the next selection is the head of the queue: our track
        for _ in range(10):
            self.tick()
            if w.play is not None and w.play.track is track and w.play.played >= 2 * FRAMES:
                break
        self.check("midplay-setup", w.play is not None and w.play.track is track, "the long track did not start playing")
        waiting = st.db.execute("SELECT COUNT(*) FROM queue WHERE track_id=?", (track.id,)).fetchone()[0]
        self.check("midplay-setup", waiting == 0, f"the track under test is also waiting in the queue {waiting} time(s)")
        failures = self.metrics.counters["playback_failures"]
        applied = self.damage(track, "truncate_header")
        if applied != "truncate_header":
            # The file was too short for a cut to be certain to show (a file system with very large blocks): the scenario cannot do what it
            # says, so it says so instead of blaming the engine.
            self.metrics.counters["midplay_not_possible"] += 1
            self.play(30)
            self.op_repair()
            return
        for _ in range(4):
            self.tick()
            if self.metrics.counters["playback_failures"] > failures:
                break
        recorded = st.db.execute("SELECT 1 FROM media_failures WHERE track_id=?", (track.id,)).fetchone() is not None
        self.check("midplay-failure", self.metrics.counters["playback_failures"] == failures + 1 and recorded and track.id in w.quarantined,
                   lambda: f"a file truncated while playing: failures {self.metrics.counters['playback_failures'] - failures}, quarantine record {'present' if recorded else 'missing'}")
        self.check("midplay-continues", w.play is None or w.play.track is not track, "the broken track kept playing")
        self.play(30)
        self.op_repair()

    def scn_waiting(self):
        """Nothing queued and auto-fill off: the station waits in silence and says so; a queued track ends the wait on the next tick."""
        st = self.station
        self.ensure_running()
        self.do("update", "auto_fill off", lambda: st.action("station.update", {"auto_fill": False}), touches=("settings",))
        self.do("skip", "make room", lambda: st.action("transport.skip"), touches=("history", "playing", "current", "last_track"))
        st.db.execute("DELETE FROM queue")  # fixture: the queue may hold many entries
        st.db.commit()
        self.log(f"{self.steps}|fixture|queue emptied")
        self.play(30)
        self.check("waiting-is-silent", self.world.play is None and st.current is None and st.status()["health"]["state"] == "waiting", "the station did not wait for audio")
        track = self.pick(lambda t: not t.damaged and t.id not in self.world.quarantined)
        self.op_queue_add(track)
        self.tick()  # one tick: a track as short as 100 ms is already finished two ticks later
        self.check("waiting-ends", self.world.play is not None and self.world.play.track is track, "a queued track did not start on the next tick")
        self.play(3)
        self.do("update", "auto_fill on", lambda: st.action("station.update", {"auto_fill": True}), touches=("settings",))

    def scn_schedule_pruning(self):
        """More than 1,000 finished events: the table is cut back to the newest 1,000 as soon as any event comes due."""
        st = self.station
        self.op_stop()  # events due while off air are only marked missed, so nothing floods the queue
        track = self.pick(lambda t: not t.damaged)
        base = self.clock.now
        earlier = [(r[0], r[1]) for r in st.db.execute("SELECT id,due FROM schedules WHERE status!='pending'")]
        fixture = [(f"fx{i:04d}", track.id, "fixture", base - 500 + i * 0.1, "missed") for i in range(1150)]
        st.db.executemany("INSERT INTO schedules VALUES (?,?,?,?,?)", fixture)  # fixture rows: reaching 1,150 through the API would take minutes
        st.db.execute("INSERT INTO schedules VALUES (?,?,?,?,?)", ("fxdue", track.id, "fixture", base + 1, "pending"))
        st.db.commit()
        self.log(f"{self.steps}|fixture|1150 finished schedules and one pending")
        self.jump_to(base + 2, "hops")
        self.tick()
        kept = [r[0] for r in st.db.execute("SELECT id FROM schedules WHERE status!='pending' ORDER BY due DESC")]
        want = [r[0] for r in sorted(earlier + [(r[0], r[3]) for r in fixture] + [("fxdue", base + 1)], key=lambda r: -r[1])[:1000]]
        self.check("schedule-pruning", kept == want, lambda: f"{len(kept)} finished schedules kept; the newest 1,000 were expected")
        st.db.execute("DELETE FROM schedules WHERE id LIKE 'fx%'")
        st.db.commit()
        self.log(f"{self.steps}|fixture|fixture schedules removed")
        self.op_start()

    def scn_program_off_air(self):
        """A recurring program that comes due while the station is stopped, or more than a minute late, is missed and never queued; one that
        is only seconds late is queued.  The next time is worked out from now, so a program that was missed by days does not fire again at once
        (program-next-due is checked on every one of these ticks)."""
        w = self.world
        zone = "UTC" if "UTC" in self.zones else (self.zones[0] if self.zones else None)
        track = self.pick(lambda t: not t.damaged and t.id not in w.quarantined)
        if zone is None or track is None:
            return
        db = self.station.db
        cases = ((False, 1.0, "missed"), (False, 2.5 * 86400, "missed"), (True, 2.5 * 86400, "missed"), (True, 30.0, "queued"))
        for running, late, want in cases:
            if running:
                self.ensure_running()
            elif w.running:
                self.op_stop()
            local_now = dt.datetime.fromtimestamp(self.clock.now, dt.timezone.utc).astimezone(tz(zone))
            local_time = (local_now + dt.timedelta(minutes=3)).strftime("%H:%M")
            pid = self.op_program_add(zone=zone, local_time=local_time, weekdays="1,2,3,4,5,6,7", track=track,
                                      label=f"{'running' if running else 'stopped'}, {late:g} s late")
            if pid is None:
                continue
            due = db.execute("SELECT next_due FROM programs WHERE id=?", (pid,)).fetchone()[0]
            self.jump_to(due + late, "hops")
            self.tick()
            got = db.execute("SELECT last_status FROM programs WHERE id=?", (pid,)).fetchone()
            self.check("program-off-air", got is not None and got[0] == want,
                       lambda: f"a program that came due {late:g} s late with the station {'running' if running else 'stopped'} was {got and got[0]}, expected {want}")
            self.play(3)
            self.do("program_remove", pid[:8], lambda: self.station.action("program.remove", {"program_id": pid}), touches=("programs",))
        self.ensure_running()

    def scn_delete_guards(self):
        """A track that is waiting in the queue, scheduled, used by a recurring program or playing cannot be deleted; the refusal changes nothing."""
        st, w = self.station, self.world
        self.ensure_running()
        spare = self.pick(lambda t: not t.damaged and not self.in_use(t.id))
        if spare is None:
            return

        def refused(why):
            self.do("delete_in_use", f"{spare.title} ({why})", lambda: st.action("library.delete", {"track_id": spare.id}), fail=True)

        self.do("queue_add", spare.title, lambda: st.action("queue.add", {"track_id": spare.id}), touches=("queue",))
        refused("waiting in the queue")
        for (qid,) in st.db.execute("SELECT id FROM queue WHERE track_id=?", (spare.id,)).fetchall():
            self.do("queue_remove", str(qid), lambda: st.action("queue.remove", {"queue_id": qid}), touches=("queue",))
        sid, _ = self.scheduled_at(spare, self.clock.now + 5 * 86400, "far ahead")
        refused("scheduled")
        self.do("schedule_cancel", sid[:8], lambda: st.action("schedule.cancel", {"schedule_id": sid}), touches=("schedules",))
        pid = self.op_program_add(zone="UTC" if "UTC" in self.zones else None, local_time="03:30", weekdays="1,2,3,4,5,6,7", track=spare, label="delete guard") if self.zones else None
        if pid:
            refused("used by a recurring program")
            self.do("program_remove", pid[:8], lambda: st.action("program.remove", {"program_id": pid}), touches=("programs",))
        if st.current and st.current["id"] in w.tracks:
            playing = w.tracks[st.current["id"]]
            self.do("delete_in_use", f"{playing.title} (playing)", lambda: st.action("library.delete", {"track_id": playing.id}), fail=True)

    def scn_restart_recovery(self):
        self.ensure_running()
        self.play(25)
        self.restart()
        self.op_start()
        self.play(30)
        self.op_stop()
        self.restart()

    def scn_clock_step_back(self):
        """A clock stepped back must not make anything fire twice."""
        self.ensure_running()
        self.play(5)
        fired = len(self.program_log)
        self.jump_to(self.clock.now - 3 * 3600, "back_steps")
        self.play(40)
        self.check("clock-back-no-refire", len(self.program_log) == fired, "a recurring program fired again after the clock was stepped back")

    def prelude(self, level):
        """The directed scenarios.  Every run starts with all of them (about twelve thousand steps with every daylight saving zone), and the wall-time
        limit is only looked at between scenarios (a scenario cut in half would fail its own verdict for no engine fault), so ``--max-wall`` can overrun
        by the length of the scenario that is running."""
        for _ in range(6):
            self.op_import(kind="music")
        self.op_import(kind="speech")
        self.op_import(kind="speech")
        self.op_import(kind="ad")
        self.op_start()
        self.play(30)
        for scenario in (self.scn_stale_boundary, self.scn_restart_recovery, self.scn_quarantine_modes, self.scn_midplay_truncation, self.scn_waiting,
                         self.scn_clock_step_back, self.scn_schedule_pruning, self.scn_capacity, self.scn_program_off_air, self.scn_delete_guards):
            scenario()
            if self.over_budget():
                return
        pool = [z for z in DST_ZONES if z in self.zones]
        for zone in pool[:2] if level == "smoke" else pool:
            for kind in ("gap", "fold"):
                self.scn_dst(zone, kind)
                if self.over_budget():
                    return

    # ---- the long random part ---------------------------------------------------------------------
    def drive(self):
        rng = self.rng
        while self.steps < self.max_steps and not self.over_budget():
            if self.clock.now > SIM_START + 3 * 365 * 86400:  # a long run: the machine's clock is set back instead of running on for decades
                self.jump_to(SIM_START + rng.uniform(0, 30 * 86400), "back_steps")
            roll = rng.random()
            if roll < 0.60:
                self.dense(rng.choice((20, 60, 150, 400, 1200)))
            elif roll < 0.76:
                for _ in range(rng.randint(1, 3)):
                    self.operator_step()
            elif roll < 0.89:
                if not self.hop():
                    self.dense(50)
            elif roll < 0.93:
                self.jump_to(self.clock.now + rng.uniform(300, 3 * 86400), "warps")
                self.dense(20)
            elif roll < 0.945:
                if self.zones:
                    self.scn_dst(rng.choice(self.zones), rng.choice(("gap", "fold")))
                else:  # no time zone data on this machine: recurring programs cannot be made, so there is no daylight saving to test
                    self.dense(50)
            elif roll < 0.96:
                self.restart()
            elif roll < 0.965:
                self.jump_to(self.clock.now - rng.uniform(0.1, 7200), "back_steps")
                self.dense(30)
            else:
                self.operator_step()

    def finish(self):
        """Final sweep, one last restart (persisted state equals live state), everything released."""
        self.sweep("final")
        self.restart()
        gc.collect()
        self.close_station()
        self.at_most("final-descriptors", fd_count(), self.fd_base, "descriptors after the last close")
        self.at_most("final-threads", threading.active_count(), self.thread_base, "threads after the last close")
        for problem in check_persisted(self.dir, damaged={t.id for t in self.world.tracks.values() if t.damaged}):
            self.fail("final-persisted-state", problem)

    def run(self, level):
        try:
            self.open_station()
            if level != "none":
                self.prelude(level)
            self.drive()
            self.truncated = self.over_budget() and self.steps < self.max_steps
            self.check("ticks-verified", self.metrics.counters["ticks"] > 0, "the run did not verify a single block of audio, so it proves nothing")
            growth = self.rss_growth_after_warmup()
            if growth is not None:
                self.check("memory-growth", growth <= self.rss_growth, lambda: f"resident memory grew {growth / 1048576:.1f} MiB after warm-up (limit {self.rss_growth / 1048576:.0f})")
            self.finish()
        except SoakFailure:
            pass
        except Exception as exc:  # noqa: BLE001 - data in a shape the harness did not expect is reported by name, not as a crash of the harness
            if not any(f["invariant"] in ("unexpected-exception", "tick-raised") for f in self.failures):  # those already say what happened
                self.failures.append({"invariant": "unexpected-state", "detail": f"{type(exc).__name__}: {exc} (the engine left its data in a shape this check does not expect, "
                                      "or the check itself has a fault)", "step": self.steps, "tick": self.metrics.counters["ticks"], "sim": iso(self.clock.now),
                                      "recent": list(self.recent)[-12:]})
                self.metrics.failed["unexpected-state"] += 1
        finally:
            try:
                self.close_station()
            except Exception as exc:  # reported, but never allowed to hide the first failure
                if not self.failures:
                    self.failures.append({"invariant": "close", "detail": f"{type(exc).__name__}: {exc}", "step": self.steps, "tick": 0, "sim": iso(self.clock.now), "recent": []})


class AccelResult:
    def __init__(self, harness, wall):
        self.harness, self.wall = harness, wall
        self.failures, self.truncated, self.metrics = harness.failures, harness.truncated, harness.metrics
        self.ok = not harness.failures
        h = hashlib.sha256()
        h.update(harness.digest.digest())
        h.update(str(harness.audio_crc).encode())
        h.update(json.dumps(sorted(harness.metrics.ops.items())).encode())
        self.digest = h.hexdigest()

    def table(self):
        h, m = self.harness, self.metrics
        c, rss, growth = m.counters, [s[1] for s in m.rss_samples], h.rss_growth_after_warmup()
        rows = [
            ("seed", h.seed), ("commit mode", "synchronous=OFF (harness option --fast-commits)" if h.fast_commits else "as shipped (synchronous=FULL, fsync on every commit)"),
            ("steps (ticks + operator steps + clock jumps)", f"{h.steps:,}"), ("ticks (100 ms blocks played and verified)", f"{c['ticks']:,}"),
            ("dense playout simulated", f"{c['ticks'] * BLOCK_SECONDS / 3600:.2f} hours"),
            ("simulated calendar span (clock hops and warps included)", f"{(h.clock.now - SIM_START) / 86400:.1f} days"),
            ("operator steps", f"{sum(m.ops.values()):,}"), ("  accepted as expected", f"{c['accepted']:,}"), ("  refused as expected", f"{c['rejected_as_expected']:,}"),
            ("  other (breaking files, restarts, status polls)", f"{sum(m.ops.values()) - c['accepted'] - c['rejected_as_expected'] - c['rejected_unexpectedly']:,}"),
            ("restarts (close and reopen)", c["restarts"]), ("clock hops / warps / steps back", f"{c['hops']:,} / {c['warps']} / {c['back_steps']}"),
            ("tracks started / finished", f"{c['tracks_started']:,} / {c['tracks_finished']:,}"), ("quarantined / playback failures", f"{c['quarantined']} / {c['playback_failures']}"),
            ("one-shot events queued / missed", f"{c['schedules_queued']} / {c['schedules_missed']}"), ("recurring events queued / missed", f"{c['programs_queued']} / {c['programs_missed']}"),
            ("DST scenarios (spring-forward / fall-back)", f"{c['dst_gap_scenarios']} / {c['dst_fold_scenarios']}"),
            ("audio verified byte for byte", f"{c['audio_frames_verified'] * 4 / 1048576:,.0f} MiB"), ("queue / library / pending peak", f"{m.queue_peak} / {m.library_peak} / {m.pending_peak}"),
            ("deep checks / full sweeps", f"{c['deep_checks']:,} / {c['sweeps']:,}"),
            ("tick time p50 / p99 / max (engine only)", f"{m.tick_time.percentile(.5) * 1e6:.0f} / {m.tick_time.percentile(.99) * 1e6:.0f} / {m.tick_time.maximum * 1e6:.0f} us"),
            ("sweep time p50 / max", f"{m.sweep_time.percentile(.5) * 1e3:.2f} / {m.sweep_time.maximum * 1e3:.2f} ms"),
            ("threads min / max (expected constant)", f"{m.thread_min} / {m.thread_max}"), ("open descriptors min / max", f"{m.fd_min} / {m.fd_max}"),
            ("resident memory first / last / highest sample", f"{rss[0] / 1048576:.1f} / {rss[-1] / 1048576:.1f} / {max(rss) / 1048576:.1f} MiB" if rss else "n/a"),
            ("peak resident memory (kernel high-water mark or highest sample, the larger)", f"{max([peak_rss_bytes()] + rss) / 1048576:.1f} MiB"),
            ("memory growth after warm-up (limit %.0f MiB)" % (h.rss_growth / 1048576), "n/a (run too short)" if growth is None else f"{growth / 1048576:.1f} MiB"),
            ("wall time", f"{self.wall:.1f} s"), ("ticks per wall second", f"{c['ticks'] / max(self.wall, 1e-9):,.0f}"), ("failures", len(self.failures)),
        ]
        width = max(len(str(r[0])) for r in rows)
        return "\n".join(f"  {str(k).ljust(width)}  {v}" for k, v in rows)

    def checks_table(self):
        m = self.metrics
        names = sorted(m.checks)
        width = max((len(n) for n in names), default=10)
        lines = [f"  {'invariant'.ljust(width)}  {'evaluated':>12}  {'failed':>6}"]
        return "\n".join(lines + [f"  {n.ljust(width)}  {m.checks[n]:>12,}  {m.failed[n]:>6}" for n in names])

    def text(self, with_checks=True):
        h = self.harness
        out = [f"result: {'PASS' if self.ok else 'FAIL'}{' (stopped early by the wall-time limit)' if self.truncated else ''}", self.table()]
        if with_checks:
            out += ["", "checks evaluated:", self.checks_table()]
        out += ["", f"run digest (same seed and steps give the same digest): {self.digest}"]
        for failure in self.failures[:5]:
            out += ["", f"FAILED {failure['invariant']} at step {failure['step']}, tick {failure['tick']}, simulated {failure['sim']}", f"  {failure['detail']}", "  last steps:"]
            out += [f"    {line}" for line in failure["recent"]]
            out.append(f"  reproduce: python3 tests/soak_station.py accelerated --seed {h.seed} --steps {h.max_steps}")
        return "\n".join(out)


@contextlib.contextmanager
def session(seed=1, *, workdir=None, steps=10 ** 9, strict=False, keep_going=False, **kwargs):
    """An open station with the deterministic clock and ids, for tests that drive one scenario or one mutation."""
    with tempfile.TemporaryDirectory(prefix="soak-", dir=workdir) as directory:
        harness = Harness(directory, seed=seed, steps=steps, strict=strict, keep_going=keep_going, **kwargs)
        with deterministic_engine(harness.clock, seed), (relaxed_commits() if harness.fast_commits else contextlib.nullcontext()):
            harness.open_station()
            try:
                yield harness
            finally:
                harness.close_station()


def run_accelerated(seed=1, steps=20000, *, max_wall=None, workdir=None, scenarios="smoke", keep_going=False, trace=False,
                    rss_growth_mb=64.0, sweep_every=400, strict=False, fast_commits=False, out=None):
    """Run one deterministic accelerated soak and return an AccelResult.  ``scenarios``: none, smoke or full."""
    began = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="soak-", dir=workdir) as directory:
        harness = Harness(directory, seed=seed, steps=steps, max_wall=max_wall, keep_going=keep_going, trace=trace,
                          sweep_every=sweep_every, rss_growth_mb=rss_growth_mb, strict=strict, fast_commits=fast_commits, out=out)
        with deterministic_engine(harness.clock, seed), (relaxed_commits() if fast_commits else contextlib.nullcontext()):
            harness.run(scenarios)
        return AccelResult(harness, time.perf_counter() - began)


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------
def environment_header():
    try:
        load = "%.2f %.2f %.2f" % os.getloadavg()
    except OSError:
        load = "unknown"
    return [f"harness version {HARNESS_VERSION}  python {sys.version.split()[0]}  {sys.platform}  cpus {os.cpu_count()}  load average {load}",
            f"started {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}  argv: {' '.join(sys.argv[1:])}"]


MIB = 1024 * 1024


def claims_and_gaps(mode, result):
    """``(claims, gaps)`` for a run: what it asserted and passed, and what it did not (or could not) look at.

    A claim is listed only when this very run asserted it.  A clause whose check did not run (memory growth in a run too short to take
    enough samples, slow listeners in a run too short to demand that they be dropped, the sixteen-listener limit when it was not probed, real
    faults that could not be made on this machine) moves from the claims to the gaps, so a short or partial run never reads as a full one."""
    if mode == "accelerated":
        h, m = result.harness, result.metrics
        c, checks = m.counters, m.checks
        growth = h.rss_growth_after_warmup()
        claims = [f"with seed {h.seed}, the real station engine (no extensions, FFmpeg hidden, nothing on a network) played {c['ticks']:,} blocks "
                  f"({c['ticks'] * BLOCK_SECONDS / 3600:.1f} hours of dense playout) and every block matched, byte for byte, what an independent statement of the rules says it should be; "
                  f"its decisions matched that statement too, on {c['deep_checks']:,} ticks that started or ended a track, admitted an event or found nothing playing "
                  f"({c['schedules_queued'] + c['schedules_missed']:,} one-shot and {c['programs_queued'] + c['programs_missed']:,} recurring events decided; "
                  f"{c['restarts']} restart(s); {c['quarantined']} damaged file(s) quarantined) and after each of {sum(m.ops.values()):,} operator steps"]
        gaps = [f"anything about a real clock (the clock here is simulated; see the real-time mode), multi-day operation (the calendar span of {(h.clock.now - SIM_START) / 86400:.0f} days "
                "comes from clock jumps during which no audio was played), power loss, other kernels or file systems, the extensions, broadcast output, the browser, models, a GPU or any real listener"]
        if checks["queue-limit"] and checks["history-limit"] and checks["persisted-state"]:
            claims.append("the queue, history and schedule limits and the saved data held")
        if checks["file-descriptors"] and checks["threads"]:
            claims.append("descriptors and threads stayed within their bounds")
        else:
            gaps.append("descriptor and thread counts (this run was too short to check them)")
        if growth is None:
            gaps.append("memory growth (the run was too short to take enough samples, so that check did not run)")
        else:
            claims.append(f"resident memory grew {growth / MIB:.1f} MiB after warm-up, under the {h.rss_growth / MIB:.0f} MiB limit")
        if result.truncated:
            gaps.append(f"the rest of the requested run (it was stopped early by the wall-time limit after {h.steps:,} of {h.max_steps:,} steps)")
        if c["midplay_not_possible"]:
            gaps.append("a file cut short while it was playing (this file system could not make that cut reliably)")
        gaps.append("days of operation: a short run is evidence of absence of the failures it looked for, not a soak of days")
        return claims, gaps
    if mode == "realtime":
        facts = result.facts
        stretch = facts.get("longest_stretch", 0.0)
        claims = [f"on this machine, at this load, for {facts.get('seconds', 0):g} seconds of clients, the real clock thread kept time with listeners, uploads, chat and polling using it at once"
                  f"{' and a misbehaving extension' if facts.get('extension') else ''}: no block was further than {facts.get('max_lateness', 0) * 1000:.0f} ms from its place in the schedule",
                  "shutdown released every thread and descriptor"]
        gaps = ["drift over hours or days, behaviour under more clients than were started, a real browser or external listener decoding the audio, or anything about the network "
                "(everything stayed on 127.0.0.1)"]
        if facts.get("drift_judged"):
            claims[0] += (f", and the schedule itself drifted by less than {facts.get('max_drift', 0) * 1000:.0f} ms over each unbroken stretch (the longest was {stretch:.0f} s, so a clock "
                          f"running fast or slow by more than about {facts.get('max_drift', 0) / max(stretch, 1e-9) * 100:.1f} % would have been caught; a smaller error would not)")
        else:
            gaps.append("clock drift (no unbroken stretch of the run was long enough to judge it)")
        if facts.get("slow_asserted"):
            claims.append("listeners that stopped reading were dropped without disturbing the clock")
        else:
            gaps.append("whether slow listeners are dropped (the run was too short to demand it)")
        if facts.get("cap_probed"):
            claims.append("the seventeenth stream was refused in plain words")
        else:
            gaps.append("the sixteen-listener limit (not probed in this run)")
        if facts.get("memory_measured"):
            claims.append("resident memory stayed under its growth limit")
        else:
            gaps.append("memory growth (the run was too short to take enough samples)")
        if facts.get("seconds", 0) < 100:
            gaps.append("the 120 second run, which is scheduled for the hardening workflow")
        return claims, gaps
    if mode == "faults":
        survived = [name for name, ok, _ in result.real if ok]
        claims = [f"for each database statement, commit and file operation inside each operation, an injected failure latched or refused as documented, left the data at a commit boundary and reopened cleanly ({len(result.cases)} cases)"]
        if result.departures:
            claims.append("the recorded departures from the contract (" + ", ".join(sorted(result.departures)) + ") still behave exactly as recorded")
        if survived:
            claims.append(f"{len(survived)} of {len(result.real)} failures made by the operating system were survived")
        gaps = ["behaviour on a real failing disk, power loss (these failures are exceptions, not hardware), or operations that were not listed"]
        if result.skipped:
            gaps.append(f"{len(result.skipped)} failure(s) made by the operating system could not be run here: " + ", ".join(result.skipped))
        return claims, gaps
    if mode == "crash":
        claims = [f"after each of {len(result.cycles)} forced stops of a running service (killed without warning), the data directory passed integrity checks, reopened, and the reopened service started playing a track"]
        return claims, ["power-loss durability (a killed process still has its writes in the operating system's cache)"]
    if mode == "recurrence":
        claims = [f"the engine's next-occurrence rule agreed with an independently written rule on all {result.compared:,} samples, around every UTC offset change in the window"]
        return claims, ["anything outside the window, or zones the machine's time zone database does not contain"]
    return [], []


def scope_text(mode, result):
    """The honest limits of what was just run, printed with every result so they travel with the evidence.

    A failed run proves nothing and says so; a passing run lists only the claims it asserted (see claims_and_gaps)."""
    claims, gaps = claims_and_gaps(mode, result)
    if not claims and not gaps:
        return ""
    does_not = "Does not prove: " + "; ".join(gaps) + "."
    if not result.ok:
        return "This run FAILED, so none of the claims a passing run would make are established. What failed is listed above.\n" + does_not
    return "Proves: " + "; ".join(claims) + ".\n" + does_not


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "_child":
        # Internal: the crash test starts this helper process.  It is not part of the documented interface (so it is not in --help), and it
        # refuses any folder that the crash test did not make.
        helper = argparse.ArgumentParser(prog="soak_station.py _child", add_help=False)
        helper.add_argument("mode")
        helper.add_argument("--dir", required=True)
        helper.add_argument("--seed", type=int, default=1)
        args = helper.parse_args(argv)
        import soak_faults
        return soak_faults.crash_child(args.dir, args.seed)
    parser = argparse.ArgumentParser(description="myradio soak harness")
    sub = parser.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("accelerated", help="deterministic simulated-clock run")
    a.add_argument("--seed", type=int, default=1)
    a.add_argument("--steps", type=int, default=300000)
    a.add_argument("--max-wall", type=float, default=None, help="stop early after this many seconds of wall time")
    a.add_argument("--scenarios", choices=("none", "smoke", "full"), default="full")
    a.add_argument("--workdir", default=None)
    a.add_argument("--keep-going", action="store_true")
    a.add_argument("--trace", action="store_true")
    a.add_argument("--rss-growth-mb", type=float, default=64.0)
    a.add_argument("--fast-commits", action="store_true", help="do not wait for the disk to confirm every save (more simulated time per second; the default keeps the engine's own, slower, setting)")
    r = sub.add_parser("realtime", help="real clock thread, real HTTP server, concurrent clients")
    r.add_argument("--seconds", type=float, default=120.0, help="wall-time budget for the whole run, set-up and shutdown included; at least 7 "
                   "(4 seconds is the shortest time the clients run, and 3 more are needed to start and stop)")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--workdir", default=None)
    f = sub.add_parser("faults", help="fault injection matrix")
    f.add_argument("--quick", action="store_true")
    f.add_argument("--workdir", default=None)
    c = sub.add_parser("crash", help="stop the running service abruptly, without warning, several times and reopen the data directory each time")
    c.add_argument("--cycles", type=int, default=6)
    c.add_argument("--workdir", default=None)
    rec = sub.add_parser("recurrence", help="check the engine's recurrence rule against an independent one, around every UTC offset change")
    rec.add_argument("--zones", choices=("all", "pool"), default="all")
    sub.add_parser("findings", help="run the minimal repros for engine defects")
    args = parser.parse_args(argv)
    if args.mode == "accelerated" and args.steps < 1:
        parser.error("--steps must be 1 or more: a run with no steps checks nothing.")
    if args.mode == "accelerated" and args.max_wall is not None and args.max_wall <= 0:
        parser.error("--max-wall must be more than 0 seconds: a run that is stopped before it starts checks nothing.")
    if args.mode == "crash" and args.cycles < 1:
        parser.error("--cycles must be 1 or more: a run with no kill and reopen cycles checks nothing.")
    if args.mode == "realtime":
        import soak_realtime
        shortest = soak_realtime.MIN_CLIENT_SECONDS + soak_realtime.COMMAND_LINE_OVERHEAD
        if args.seconds < shortest:
            parser.error(f"--seconds must be at least {shortest:g}: the clients run for at least {soak_realtime.MIN_CLIENT_SECONDS:g} seconds, "
                         f"and {soak_realtime.COMMAND_LINE_OVERHEAD:g} more are needed to start and stop. Nothing was run.")
    print("\n".join(environment_header()), flush=True)
    if args.mode == "accelerated":
        result = run_accelerated(args.seed, args.steps, max_wall=args.max_wall, workdir=args.workdir, scenarios=args.scenarios,
                                 keep_going=args.keep_going, trace=args.trace, rss_growth_mb=args.rss_growth_mb, strict=True,
                                 fast_commits=args.fast_commits)
    elif args.mode == "recurrence":
        from soak_core import ZONES, cross_check_recurrence
        result = cross_check_recurrence(None if args.zones == "all" else available_zones(ZONES))
    elif args.mode == "realtime":
        result = soak_realtime.run_realtime(args.seconds, seed=args.seed, workdir=args.workdir, overhead=soak_realtime.COMMAND_LINE_OVERHEAD)
    else:
        import soak_faults
        if args.mode == "faults":
            result = soak_faults.run_matrix(quick=args.quick, workdir=args.workdir)
        elif args.mode == "crash":
            result = soak_faults.run_crash(args.cycles, workdir=args.workdir)
        else:
            result = soak_faults.run_findings()
    print(result.text())
    scope = scope_text(args.mode, result)
    if scope:
        print("\nwhat this run does and does not prove\n" + "\n".join("  " + line for line in scope.splitlines()))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
