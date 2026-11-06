"""Real-time mode: the real clock thread, a real Server on 127.0.0.1 port 0, and clients that use it at once.  Not collected
by unittest.

Clients (all bounded, all with timeouts, all stopped and joined at the end): status pollers, fast /stream.wav listeners that
check every block, slow listeners that stop reading, a listener inside the process that never reads, an uploader, an offline
command assistant chatter, and an operator that queues, schedules and renames.  The clock thread is timed at the moment
each block is handed to listeners.  Nothing leaves 127.0.0.1 and no process other than this one is started.
"""
from __future__ import annotations

import collections
import contextlib
import datetime as dt
import http.client
import io
import json
import os
import random
import secrets
import socket
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from myradio.extensions import Extension  # noqa: E402
from myradio.server import Server  # noqa: E402
from myradio.station import Station  # noqa: E402
from soak_core import (BLOCK_BYTES, BLOCK_SECONDS, FRAMES, MAX_SLOTS, Histogram, Tapes, check_persisted, fd_count, no_converter,  # noqa: E402
                       offline_agent_env, peak_rss_bytes, rss_bytes)

# What "the clock keeps time" means here, in seconds.  The engine's own loop absorbs lateness up to 0.5 s and then gives up
# and restarts its schedule; these bounds are well inside that.
MAX_LATENESS = 0.25      # JITTER: no block may be further than this from its place in the schedule (anchored at the median block of its stretch)
MAX_GAP = 0.35           # the longest interval between two consecutive blocks
P99_GAP = 0.15           # 99 % of intervals are shorter than this
MAX_DRIFT = 0.10         # DRIFT: over any unbroken stretch the schedule itself may move by this much (a line fitted through the block times)
MIN_BLOCKS_FOR_DRIFT = 20  # a stretch shorter than this (two seconds) is too short to fit a line through
MIN_CLIENT_SECONDS = 4.0   # the shortest time the clients run
COMMAND_LINE_OVERHEAD = 3.0  # seconds the command line leaves for set-up, shutdown and the reopen check inside its wall-time budget
STREAM_RATE = 44100 * 4  # bytes per second of one listener's audio
LIBRARY_CAP = 40
ZONES = ("UTC", "America/New_York", "Europe/London", "Asia/Kolkata", "Australia/Lord_Howe")


class StallingExtension(Extension):
    """A subsystem that misbehaves on purpose: each event takes 0.4 s to handle and status takes 20 ms.  The extension seam promises
    that neither can delay the audio clock."""

    name = "soakstall"

    def __init__(self):
        self.seen = 0

    def on_event(self, event):
        self.seen += 1
        time.sleep(0.4)

    def status(self):
        time.sleep(0.02)
        return {"seen": self.seen}


class Sink(io.TextIOBase):
    """Collects what the server writes to stderr (it prints a traceback when a client resets a connection) so the report stays readable."""

    def __init__(self):
        self.text, self.size = [], 0

    def writable(self):
        return True

    def write(self, data):
        if self.size < 200000:
            self.text.append(data)
            self.size += len(data)
        return len(data)

    def value(self):
        return "".join(self.text)


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = collections.Counter()
        self.latency = collections.defaultdict(Histogram)
        self.errors = []

    def count(self, key, n=1):
        with self.lock:
            self.counts[key] += n

    def timed(self, key, seconds):
        with self.lock:
            self.latency[key].add(seconds)

    def error(self, who, message):
        with self.lock:
            self.counts["errors"] += 1
            if len(self.errors) < 20:
                self.errors.append(f"{who}: {message}")


class Client:
    """Small HTTP helper that never raises: failures are counted and described."""

    def __init__(self, port, token, stats, who):
        self.port, self.token, self.stats, self.who = port, token, stats, who
        self.conn = None

    def call(self, method, path, body=None, *, expect=(200,), key=None, content_type="application/json", keep=True):
        began = time.perf_counter()
        try:
            if self.conn is None:
                self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            self.conn.request(method, path, body=body, headers={"X-Myradio-Token": self.token, "Content-Type": content_type})
            reply = self.conn.getresponse()
            data = reply.read()
            if not keep or reply.status >= 400 or reply.getheader("Connection", "").lower() == "close":  # the service closes after a refusal
                self.conn.close()
                self.conn = None
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            self.stats.error(self.who, f"{method} {path.split('?')[0]} failed: {type(exc).__name__}: {exc}")
            return None, None
        self.stats.timed(key or self.who, time.perf_counter() - began)
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            payload = None
        if reply.status not in expect:
            self.stats.error(self.who, f"{method} {path.split('?')[0]} answered {reply.status}, expected one of {expect}: {data[:120]!r}")
        elif reply.status == 400 and not (isinstance(payload, dict) and isinstance(payload.get("error"), str) and payload["error"]):
            self.stats.error(self.who, f"{method} {path.split('?')[0]} refused without a plain message")
        return reply.status, payload

    def close(self):
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.close()
            self.conn = None


class Run:
    """Everything one real-time run shares between its threads."""

    def __init__(self, station, server, token, seed, duration):
        self.station, self.server, self.token = station, server, token
        self.port = server.server_address[1]
        self.stats, self.stop, self.rng = Stats(), threading.Event(), random.Random(seed)
        self.duration, self.tapes = duration, Tapes()
        self.slots = set(range(0, 6))  # signatures of every track that may be audible
        self.next_slot = 100
        self.tracks = []  # ids that can be queued
        self.lock = threading.Lock()
        self.listeners = []
        self.threads = []

    def client(self, who):
        return Client(self.port, self.token, self.stats, who)

    def spawn(self, name, target, *args):
        def guarded():
            try:
                target(*args)
            except Exception as exc:  # noqa: BLE001 - a crashed client is a finding, never a silent exit
                self.stats.error(name, f"client thread crashed: {type(exc).__name__}: {exc}")

        thread = threading.Thread(target=guarded, name=f"soak-{name}", daemon=True)
        self.threads.append(thread)
        thread.start()

    def new_slot(self):
        with self.lock:
            slot = self.next_slot
            self.next_slot += 1
            if self.next_slot >= MAX_SLOTS:
                self.next_slot = 100
            self.slots.add(slot)
        return slot


# ---- clients ---------------------------------------------------------------------------
def poller(run, index):
    api = run.client(f"poller{index}")
    while not run.stop.is_set():
        status, body = api.call("GET", "/api/status", key="status")
        if status == 200:
            run.stats.count("status_polls")
            if not (isinstance(body, dict) and len(body["queue"]) <= 1000 and len(body["history"]) <= 30 and isinstance(body["running"], bool)):
                run.stats.error(api.who, "status has the wrong shape or exceeds its bounds")
        run.stop.wait(0.05)
    api.close()


def fast_listener(run, index):
    stats, name = run.stats, f"fast{index}"
    try:
        conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=10)
        conn.request("GET", "/stream.wav", headers={"X-Myradio-Token": run.token})
        reply = conn.getresponse()
        header = reply.read(44)
        if reply.status != 200 or header[:4] != b"RIFF" or header[8:12] != b"WAVE" or header[36:40] != b"data" or len(header) != 44:
            stats.error(name, f"bad stream header (status {reply.status})")
            return
        began, blocks, bad, slowest, last = time.monotonic(), 0, 0, 0.0, time.monotonic()
        while not run.stop.is_set():
            block = reply.read(BLOCK_BYTES)
            now = time.monotonic()
            if len(block) != BLOCK_BYTES:
                if not run.stop.is_set():
                    stats.error(name, f"stream ended early after {blocks} blocks")
                break
            blocks += 1
            slowest, last = max(slowest, now - last), now
            runs = run.tapes.parse(block)
            if runs is None or sum(r[2] for r in runs) != FRAMES or any(r[0] is not None and r[0] not in run.slots for r in runs):
                bad += 1
                stats.error(name, f"block {blocks} is not valid audio of any imported track: {block[:8].hex()}")
        elapsed = time.monotonic() - began
        with stats.lock:
            run.listeners.append({"name": name, "blocks": blocks, "bad": bad, "seconds": elapsed, "slowest": slowest})
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        if not run.stop.is_set():
            stats.error(name, f"{type(exc).__name__}: {exc}")


def slow_listener(run, index, pause):
    """Connects, takes the header, then stops reading.  The service must drop it; this thread then drains what the operating
    system buffered and expects the stream to end."""
    name, stats = f"slow{index}", run.stats
    sock = None
    try:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        sock.settimeout(10)
        sock.connect(("127.0.0.1", run.port))
        sock.sendall(f"GET /stream.wav HTTP/1.1\r\nHost: 127.0.0.1:{run.port}\r\nX-Myradio-Token: {run.token}\r\n\r\n".encode())
        first = sock.recv(400)
        if not first.startswith(b"HTTP/1.1 200"):
            stats.error(name, f"stream refused: {first[:40]!r}")
            return
        began = time.monotonic()
        run.stop.wait(pause)  # not reading at all
        total, ended = len(first), False
        sock.settimeout(3)
        limit = time.monotonic() + max(6.0, run.duration - (time.monotonic() - run.began) - 2)
        while time.monotonic() < limit and not run.stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                ended = True
                break
            total += len(chunk)
        with stats.lock:
            run.listeners.append({"name": name, "dropped": ended, "bytes": total, "stalled": pause, "seconds": time.monotonic() - began})
    except OSError as exc:
        if not run.stop.is_set():
            stats.error(name, f"{type(exc).__name__}: {exc}")
    finally:
        if sock:
            sock.close()


def uploader(run):
    api = run.client("uploader")
    rng, serial = random.Random(run.rng.random()), 0
    mine = []
    while not run.stop.wait(rng.uniform(0.8, 1.6)):
        serial += 1
        if len(mine) > LIBRARY_CAP:  # keep the library bounded
            victim = mine.pop(rng.randrange(len(mine)))
            status, _ = api.call("POST", "/api/action", json.dumps({"operation": "library.delete", "arguments": {"track_id": victim}}), expect=(200, 400), key="action")
            if status != 200:
                mine.append(victim)
        if serial % 5 == 0:
            status, _ = api.call("POST", f"/api/upload?title=Bad{serial}", b"this is not audio", expect=(400,), key="upload-refused", content_type="application/octet-stream")
            run.stats.count("uploads_refused" if status == 400 else "uploads_odd")
            continue
        slot = run.new_slot()
        frames = rng.randint(FRAMES * 2, FRAMES * 15)
        data = Tapes.wav(slot, frames, rng.choice((1, 2)))
        query = urllib.parse.urlencode({"title": f"Upload {serial}", "artist": "Soak", "kind": rng.choice(("music", "music", "speech", "ad"))})
        status, body = api.call("POST", f"/api/upload?{query}", data, expect=(201,), key="upload", content_type="application/octet-stream")
        if status == 201 and isinstance(body, dict) and body.get("id"):
            mine.append(body["id"])
            with run.lock:
                run.tracks.append(body["id"])
            run.stats.count("uploads_ok")
    api.close()


def chatter(run):
    api = run.client("chat")
    rng = random.Random(run.rng.random())
    script = ["status", "help", "status", "skip", "what's playing", "name Soak FM", "status", "stop", "start", "status", "tell me a joke", "start", "STATUS!"]
    index = 0
    while not run.stop.wait(rng.uniform(0.4, 0.9)):
        index += 1
        message = script[index % len(script)]
        bad = index % 11 == 0
        status, body = api.call("POST", "/api/chat", json.dumps({"message": "" if bad else message}), expect=(400,) if bad else (200,), key="chat")
        if status == 200:
            run.stats.count("chat_ok")
            if body.get("mode") != "commands" or body.get("actions") != [] or not isinstance(body.get("reply"), str):
                run.stats.error(api.who, f"offline assistant answered with the wrong shape: {str(body)[:100]}")
        elif status == 400:
            run.stats.count("chat_refused")
    api.close()


def operator(run):
    api = run.client("operator")
    rng = random.Random(run.rng.random())
    programs, serial = [], 0
    while not run.stop.wait(rng.uniform(0.3, 0.7)):
        serial += 1
        with run.lock:
            tracks = list(run.tracks)
        roll = rng.random()
        if not tracks or roll < 0.35:
            op, args = "queue.add", {"track_id": rng.choice(tracks) if tracks else "0" * 32}
        elif roll < 0.5:
            _, body = api.call("GET", "/api/status", key="status")
            queue = body["queue"] if isinstance(body, dict) else []
            op, args = "queue.remove", {"queue_id": rng.choice(queue)["id"] if queue else 987654}
        elif roll < 0.65:
            when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=rng.uniform(3, 20))
            op, args = "schedule.add", {"track_id": rng.choice(tracks), "at": when.isoformat().replace("+00:00", "Z")}
        elif roll < 0.8:
            zone = rng.choice(ZONES)
            from zoneinfo import ZoneInfo
            local = dt.datetime.now(ZoneInfo(zone)) + dt.timedelta(minutes=rng.randint(1, 3))
            op, args = "program.add", {"track_id": rng.choice(tracks), "timezone": zone, "local_time": local.strftime("%H:%M"), "weekdays": "1,2,3,4,5,6,7"}
        elif roll < 0.9 and programs:
            op, args = "program.remove", {"program_id": programs.pop(rng.randrange(len(programs)))}
        else:
            op, args = "station.update", {"name": f"Soak {serial}", "tagline": "load test"}
        status, body = api.call("POST", "/api/action", json.dumps({"operation": op, "arguments": args}), expect=(200, 400), key="action")
        run.stats.count(f"action_{'ok' if status == 200 else 'refused'}")
        if status == 200 and op == "program.add":
            _, state = api.call("GET", "/api/status", key="status")
            if isinstance(state, dict) and state["programs"]:
                programs.append(state["programs"][-1]["id"])
    api.close()


def probe_stream_cap(run):
    """Open streams until the service refuses one: the limit is 16 listeners and the refusal is a plain 400."""
    sockets, refused = [], None
    try:
        for _ in range(24):
            sock = socket.create_connection(("127.0.0.1", run.port), timeout=5)
            sockets.append(sock)
            sock.sendall(f"GET /stream.wav HTTP/1.1\r\nHost: 127.0.0.1:{run.port}\r\nX-Myradio-Token: {run.token}\r\n\r\n".encode())
            head = sock.recv(300)
            if not head.startswith(b"HTTP/1.1 200"):
                refused = (head.split(b"\r\n")[0].decode("latin-1"), len(run.station.subscribers))
                break
        else:
            return "no refusal after 24 streams"
    finally:
        for sock in sockets:
            sock.close()
    ok = refused is not None and refused[0].endswith("400 Bad Request") and refused[1] == 16
    return None if ok else f"refusal was {refused}"


# ---- the run ----------------------------------------------------------------------------------
class Timing:
    """How regularly the clock thread produced blocks (see judge_clock)."""

    def __init__(self, gaps, lateness, drifts, stretches, across, resumed):
        self.gaps, self.lateness, self.drifts, self.stretches, self.across, self.resumed = gaps, lateness, drifts, stretches, across, resumed

    @property
    def worst_lateness(self):
        return max((abs(x) for x in self.lateness), default=0.0)

    @property
    def worst_drift(self):
        """``(drift, seconds)`` of the stretch whose schedule moved the most: the drift keeps its sign (positive: the clock ran slow) and ``seconds``
        is how long that stretch lasted.  ``(0.0, 0.0)`` when no stretch was long enough to judge."""
        if not self.drifts:
            return 0.0, 0.0
        index = max(range(len(self.drifts)), key=lambda i: abs(self.drifts[i]))
        return self.drifts[index], self.stretches[index]

    @property
    def longest_stretch(self):
        return max(self.stretches, default=0.0)


def judge_clock(stamps, hold=None):
    """Timing of a run, from the moment each block was handed to the listeners.

    ``hold`` is ``(began, ended)`` of the deliberate stall, if there was one.  The engine restarts its schedule after a long stall, so the blocks
    before it and after it (leaving out the burst right after it) are judged as separate stretches.

    Two different things are measured, because they fail differently:

    * ``lateness`` is JITTER.  Each block is compared with a schedule of one block every 100 ms anchored at the median block of its stretch.  A clock
      that steadily runs fast or slow is early at one end and late at the other, so half of its total drift shows up here and it can hide below the limit.
    * ``drifts`` is DRIFT.  A least-squares line is fitted through the block times of each stretch of at least ``MIN_BLOCKS_FOR_DRIFT`` blocks; its slope
      minus the nominal 100 ms, times the length of the stretch, is how far the schedule itself moved (positive: the clock ran slow).  Noise in single
      blocks hardly changes a fitted line, so this stays small on a busy machine and grows only when the clock really runs at the wrong rate.
    """
    stamps = list(stamps)
    if hold:
        segments = [[t for t in stamps if t < hold[0]], [t for t in stamps if t > hold[1] + 0.35]]
        across = [b - a for a, b in zip(stamps, stamps[1:]) if a < hold[0] <= b]
        resumed = sum(1 for t in stamps if hold[1] <= t < hold[1] + 0.5)
    else:
        segments, across, resumed = [stamps], [], 0
    gaps, lateness, drifts, stretches = [], [], [], []
    for part in segments:
        gaps += [b - a for a, b in zip(part, part[1:])]
        anchor = statistics.median(t - i * BLOCK_SECONDS for i, t in enumerate(part)) if part else 0.0
        lateness += [t - i * BLOCK_SECONDS - anchor for i, t in enumerate(part)]
        if len(part) >= MIN_BLOCKS_FOR_DRIFT:
            fit = statistics.linear_regression(range(len(part)), part)
            drifts.append((fit.slope - BLOCK_SECONDS) * (len(part) - 1))
            stretches.append(part[-1] - part[0])
    return Timing(gaps, lateness, drifts, stretches, across, resumed)


class RealtimeResult:
    def __init__(self):
        self.rows, self.problems, self.notes = [], [], []
        self.facts = {}  # what this run actually asserted, for the statement of what it proves (see soak_station.claims_and_gaps)

    @property
    def ok(self):
        return not self.problems

    def text(self):
        width = max((len(str(r[0])) for r in self.rows), default=10)
        lines = [f"result: {'PASS' if self.ok else 'FAIL'}"] + [f"  {str(k).ljust(width)}  {v}" for k, v in self.rows]
        lines += [""] + [f"note: {n}" for n in self.notes]
        lines += [""] + [f"FAILED {p}" for p in self.problems]
        return "\n".join(lines)


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


def stop_clients(run):
    run.stop.set()
    for thread in run.threads:
        thread.join(timeout=12)


def run_realtime(seconds=120.0, *, seed=1, workdir=None, pollers=3, fast=3, slow=2, max_lateness=MAX_LATENESS, max_gap=MAX_GAP, p99_gap=P99_GAP,
                 max_drift=MAX_DRIFT, check_slow_listeners=None, stream_cap=True, stall=None, stalling_extension=True, overhead=0.0):
    """Run the real-time mode and return a RealtimeResult.  The clients run for ``seconds - overhead``; ``overhead`` is the time
    set-up, shutdown and the reopen check take, so that ``seconds`` can be the whole wall-time budget (the command line uses 3 s).
    The clients need at least ``MIN_CLIENT_SECONDS``; a shorter budget raises ValueError rather than quietly running longer.

    ``max_lateness`` limits jitter and ``max_drift`` limits drift (see judge_clock).  ``stall``: seconds to hold the playout lock once, 40 %
    of the way through (default 1.2 s for runs of 15 s or more); the engine is documented to resume live afterwards.  ``stalling_extension``:
    load an extension that handles events slowly and answers status slowly.

    Whatever goes wrong inside the run, the clock thread, the server and the client threads are stopped before the exception leaves."""
    if seconds - overhead < MIN_CLIENT_SECONDS:
        raise ValueError(f"The real-time mode needs at least {MIN_CLIENT_SECONDS:g} seconds for its clients; a budget of {seconds:g} seconds leaves {seconds - overhead:g} "
                         f"after {overhead:g} seconds for starting and stopping. Nothing was run.")
    result = RealtimeResult()
    thread_base, fd_base = threading.active_count(), fd_count()
    began_wall = time.perf_counter()
    budget, seconds = seconds, seconds - overhead
    pause = max(3.0, min(14.0, seconds * 0.7))  # how long the slow listeners stop reading
    if check_slow_listeners is None:
        # The operating system buffers about 1.4 MB per connection here (about 8 s of audio) before the service sees a blocked
        # listener, so the pause has to be comfortably longer than that for the drop to be a fair thing to demand.
        check_slow_listeners = pause >= 11.0
    if stall is None:
        stall = 1.2 if seconds >= 15 else 0.0
    extension = StallingExtension() if stalling_extension else None
    sink = Sink()
    with tempfile.TemporaryDirectory(prefix="realtime-", dir=workdir) as directory, no_converter(), offline_agent_env(), contextlib.redirect_stderr(sink), \
            contextlib.ExitStack() as cleanup:  # cleanup runs first on the way out, newest resource first, whatever happened inside
        directory = os.path.join(directory, "station")
        station = Station(directory, extensions=[extension] if extension else ())  # the real clock thread
        cleanup.callback(station.close)
        stamps = []
        deliver = station._deliver

        def timed(block):
            stamps.append(time.monotonic())
            return deliver(block)

        station._deliver = timed
        token = secrets.token_urlsafe(32)  # private to this run; never printed
        server = Server(("127.0.0.1", 0), station, token)
        cleanup.callback(server.server_close)
        if server.chat.endpoint:
            raise RuntimeError("the chat assistant is not offline")
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="soak-server", daemon=True)
        serving.start()
        cleanup.callback(lambda: (server.shutdown(), serving.join(timeout=5)))
        run = Run(station, server, token, seed, seconds)
        cleanup.callback(stop_clients, run)
        run.began = time.monotonic()
        for slot in range(6):  # seed library: the clock always has something to play
            run.tracks.append(station.import_wav(Tapes.wav(slot, FRAMES * random.Random(slot).randint(8, 30)), f"Seed {slot}", "Soak", "music")["id"])
        station.action("transport.start")
        stalled = station.subscribe()  # a listener inside the process that never reads
        stalled_at = time.monotonic()
        rss_samples, started = [], time.monotonic()
        next_db_check = started + 5
        for index in range(pollers):
            run.spawn(f"poller{index}", poller, run, index)
        for index in range(fast):
            run.spawn(f"fast{index}", fast_listener, run, index)
        for index in range(slow):
            run.spawn(f"slow{index}", slow_listener, run, index, pause)
        run.spawn("uploader", uploader, run)
        run.spawn("chat", chatter, run)
        run.spawn("operator", operator, run)
        end = started + seconds
        cap_at = started + seconds * 0.6
        stalled_gone_after, cap_problem, cap_done = None, None, not stream_cap
        hold_at, hold = started + seconds * 0.4, None
        peak_threads = threading.active_count()
        try:
            while time.monotonic() < end:
                time.sleep(0.25)
                now = time.monotonic()
                peak_threads = max(peak_threads, threading.active_count())
                if stalled_gone_after is None and stalled not in station.subscribers:
                    stalled_gone_after = now - stalled_at
                if stall and hold is None and now >= hold_at:
                    began_hold = time.monotonic()
                    with station.lock:  # as if a disk stall or a suspended host had stopped the clock thread inside tick()
                        time.sleep(stall)
                    hold = (began_hold, time.monotonic())
                if not cap_done and now >= cap_at:
                    cap_problem, cap_done = probe_stream_cap(run), True
                if not rss_samples or now - started - rss_samples[-1][0] >= 1.0:
                    rss_samples.append((now - started, rss_bytes()))
                if now >= next_db_check:
                    next_db_check += 5
                    conn = sqlite3.connect(os.path.join(directory, "station.sqlite3"), timeout=5)
                    try:
                        conn.execute("PRAGMA query_only=ON")
                        verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
                        queue = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
                        history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                    finally:
                        conn.close()
                    if verdict != "ok" or queue > 1000 or history > 200:
                        result.problems.append(f"database check during the run: quick_check={verdict}, queue={queue}, history={history}")
                if not station.worker.is_alive():
                    result.problems.append("the clock thread died")
                    break
        finally:
            if stalled_gone_after is None and stalled not in station.subscribers:
                stalled_gone_after = time.monotonic() - stalled_at
            stop_clients(run)
        load_end = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        stuck = [t.name for t in run.threads if t.is_alive()]
        if stuck:
            result.problems.append(f"client threads did not stop: {stuck}")
        block_stamps = list(stamps)
        dropped = station.dropped
        faulted = station.faulted
        with station.lock:
            library = station.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] if not faulted else -1
        ext_info = None
        if extension:
            slot = station.extensions._slots["soakstall"]
            ext_info = (extension.seen, slot.mailbox.dropped if slot.mailbox else 0, slot.state)
        # Quiet now: the whole on-disk state must be consistent with the files.
        for problem in check_persisted(directory):
            result.problems.append(f"after the load stopped: {problem}")
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)
        station.close()
        deadline = time.monotonic() + 8
        while threading.active_count() > thread_base and time.monotonic() < deadline:
            time.sleep(0.05)
        threads_after, fds_after = threading.active_count(), fd_count()
        # Reopen: the directory must be usable by a fresh process-equivalent start.
        again = Station(directory, start_worker=False)
        try:
            if again.faulted or again.running:
                result.problems.append("the reopened station is faulted or already running")
            for problem in check_persisted(directory):
                result.problems.append(f"after reopening: {problem}")
        finally:
            again.close()

    # ---- verdicts ----------------------------------------------------------------------------
    stats = run.stats
    n = len(block_stamps)
    lost = int(round(stall / BLOCK_SECONDS)) if hold else 0
    expected = int((seconds + 0.5) / BLOCK_SECONDS) - lost
    timing = judge_clock(block_stamps, hold)  # each side of a stall is judged against its own schedule: the engine restarts its schedule after a long stall
    gaps, lateness, across, resumed, worst = timing.gaps, timing.lateness, timing.across, timing.resumed, timing.worst_lateness
    drift, stretch = timing.worst_drift
    rows = result.rows
    rows += [("wall-time budget / clients ran for / blocks produced / blocks expected", f"{budget:g} s / {seconds:g} s / {n} / about {expected}"),
             ("clock jitter, a block's distance from its place in the schedule: worst / p99 / p50", f"{worst * 1000:.1f} / {percentile([abs(x) for x in lateness], .99) * 1000:.1f} / {percentile([abs(x) for x in lateness], .5) * 1000:.1f} ms (limit {max_lateness * 1000:.0f} ms)"),
             ("clock drift, how far the schedule itself moved over an unbroken stretch: worst / that stretch / rate", (f"{drift * 1000:+.1f} ms / {stretch:.1f} s / {drift / stretch * 100:+.3f} % (limit {max_drift * 1000:.0f} ms, a rate of {max_drift / stretch * 100:.2f} % over this stretch; positive means slow)" if timing.drifts else "not judged: no unbroken stretch of at least two seconds")),
             ("interval between blocks: longest / p99 / p50", f"{max(gaps, default=0) * 1000:.1f} / {percentile(gaps, .99) * 1000:.1f} / {percentile(gaps, .5) * 1000:.1f} ms (limits {max_gap * 1000:.0f} / {p99_gap * 1000:.0f} ms)"),
             ("intervals longer than 110 ms / 150 ms / 250 ms", f"{sum(g > .11 for g in gaps)} / {sum(g > .15 for g in gaps)} / {sum(g > .25 for g in gaps)}")]
    if hold:
        rows.append((f"playout lock held for {stall:g} s: longest wait between blocks / blocks in the half second after", f"{max(across, default=0) * 1000:.0f} ms / {resumed} (live pace is 5; the engine restarts its schedule and runs one extra tick)"))
    if ext_info:
        rows.append(("misbehaving extension: events handled (0.4 s each) / dropped from its mailbox; state", f"{ext_info[0]} / {ext_info[1]}; {ext_info[2]}"))
    for name in ("status", "upload", "chat", "action"):
        h = stats.latency.get(name)
        if h:
            rows.append((f"{name} request time: count / p50 / p99 / max", f"{h.total} / {h.percentile(.5) * 1000:.1f} / {h.percentile(.99) * 1000:.1f} / {h.maximum * 1000:.1f} ms"))
    c = stats.counts
    rows += [("status polls", f"{c['status_polls']}"), ("uploads accepted / refused as expected", f"{c['uploads_ok']} / {c['uploads_refused']}"), ("chat answers / refused as expected", f"{c['chat_ok']} / {c['chat_refused']}"),
             ("operator actions accepted / refused", f"{c['action_ok']} / {c['action_refused']}"), ("client errors", c["errors"]), ("listeners dropped by the service (status counter)", dropped),
             ("library size at the end", library)]
    fast_rows = [x for x in run.listeners if x["name"].startswith("fast")]
    for x in sorted(fast_rows, key=lambda r: r["name"]):
        rate = x["blocks"] * BLOCK_BYTES / max(x["seconds"], 1e-9)
        rows.append((f"{x['name']} listener: blocks / invalid / slowest wait / audio rate", f"{x['blocks']} / {x['bad']} / {x['slowest'] * 1000:.0f} ms / {rate / STREAM_RATE * 100:.1f} % of real time"))
    for x in sorted((r for r in run.listeners if r["name"].startswith("slow")), key=lambda r: r["name"]):
        rows.append((f"{x['name']} listener: stopped reading for {x['stalled']:g} s", f"{'dropped by the service' if x['dropped'] else 'NOT dropped'}; {x['bytes']:,} bytes were waiting in the operating system's buffers; ended after {x['seconds']:.1f} s"))
    rows.append(("listener inside the process that never reads", f"removed by the service after {stalled_gone_after:.1f} s" if stalled_gone_after is not None else "NOT removed"))
    rows.append(("sixteen-listener limit", "a 17th stream was refused with a plain 400" if cap_done and not cap_problem else (cap_problem or "not probed")))
    noise = sink.value()
    resets = noise.count("Exception occurred during processing of request")
    kinds = collections.Counter(line.split(":")[0] for line in noise.splitlines() if line and not line.startswith((" ", "-", "Traceback", "Exception occurred")) and line[0].isalpha())
    rows.append(("tracebacks the server printed to stderr (client resets, see F14)", f"{resets}" + (f" ({', '.join(f'{k} x{v}' for k, v in kinds.most_common(3))})" if resets else "")))
    rss = [s[1] for s in rss_samples]
    growth = None
    if len(rss) >= 8:
        growth = max(rss[len(rss) // 4:]) - rss[len(rss) // 4]
    rows += [("threads before / peak / after shutdown", f"{thread_base} / {peak_threads} / {threads_after}"), ("open descriptors before / after", f"{fd_base} / {fds_after}"),
             ("resident memory first / last / peak of process", f"{rss[0] / 1048576:.1f} / {rss[-1] / 1048576:.1f} / {max([peak_rss_bytes()] + rss) / 1048576:.1f} MiB" if rss else "n/a"),
             ("memory growth after warm-up (limit 64 MiB)", "n/a (run too short)" if growth is None else f"{growth / 1048576:.1f} MiB"),
             ("machine load average at the end (1, 5, 15 min)", "%.2f %.2f %.2f" % tuple(load_end)), ("wall time", f"{time.perf_counter() - began_wall:.1f} s")]
    problems = result.problems
    if faulted:
        problems.append("the station latched a fault during the run")
    if n < expected - 5:
        problems.append(f"the clock produced {n} blocks, expected about {expected}")
    if hold:
        if max(across, default=0) < stall * 0.8:
            problems.append("the stall did not stop the clock thread: the test did not do what it says")
        if resumed > 7:
            problems.append(f"after the stall the clock thread delivered {resumed} blocks in half a second instead of resuming live")
    if worst > max_lateness:
        problems.append(f"a block was {worst * 1000:.0f} ms away from its place in the schedule (limit {max_lateness * 1000:.0f} ms)")
    if abs(drift) > max_drift:
        problems.append(f"the clock {'ran slow' if drift > 0 else 'ran fast'}: over {stretch:.1f} seconds its schedule moved by {abs(drift) * 1000:.0f} ms, "
                        f"a rate error of {abs(drift) / stretch * 100:.2f} % (limit {max_drift * 1000:.0f} ms)")
    if not timing.drifts:
        result.notes.append("clock drift was not judged: the run had no unbroken stretch of at least two seconds")
    if gaps and max(gaps) > max_gap:
        problems.append(f"the longest wait between blocks was {max(gaps) * 1000:.0f} ms (limit {max_gap * 1000:.0f} ms)")
    if gaps and percentile(gaps, .99) > p99_gap:
        problems.append(f"99 % of intervals should be under {p99_gap * 1000:.0f} ms, p99 was {percentile(gaps, .99) * 1000:.0f} ms")
    if stats.counts["errors"]:
        problems += [f"client error: {e}" for e in stats.errors[:6]] + ([f"... {stats.counts['errors'] - 6} more"] if stats.counts["errors"] > 6 else [])
    if len(fast_rows) != fast:
        problems.append(f"only {len(fast_rows)} of {fast} fast listeners finished cleanly")
    for x in fast_rows:
        if x["bad"] or x["blocks"] < max(0, seconds - 6 - stall) * 10 or not 0.85 <= x["blocks"] * BLOCK_BYTES / max(x["seconds"], 1e-9) / STREAM_RATE <= 1.1:
            problems.append(f"{x['name']} did not receive real-time, valid audio: {x}")
    if stalled_gone_after is None or stalled_gone_after > 8:
        problems.append("a listener that never reads was not removed within 8 seconds")
    if check_slow_listeners:
        slow_rows = [x for x in run.listeners if x["name"].startswith("slow")]
        if len(slow_rows) != slow or not all(x["dropped"] for x in slow_rows):
            problems.append(f"a listener that stopped reading for {pause:g} s did not see its stream end (it was not dropped by the service; "
                            "the operating system's buffers may be larger than the pause, so try a longer run)")
        if dropped < 1 + slow:
            problems.append(f"the service counted {dropped} dropped listeners, expected at least {1 + slow}")
    else:
        seen = [x for x in run.listeners if x["name"].startswith("slow")]
        result.notes.append(f"slow HTTP listeners were not asserted: the pause ({pause:g} s) is not long enough to demand it; observed dropped: {sum(x['dropped'] for x in seen)} of {len(seen)}")
    if cap_problem:
        problems.append(f"sixteen-listener limit: {cap_problem}")
    unexpected = [k for k in kinds if k not in ("ConnectionResetError", "BrokenPipeError", "TimeoutError", "ConnectionAbortedError")]
    if unexpected:
        problems.append(f"the server printed tracebacks for unexpected errors: {unexpected}")
    if threads_after > thread_base:
        problems.append(f"{threads_after - thread_base} thread(s) outlived the run")
    if fds_after > fd_base:
        problems.append(f"{fds_after - fd_base} descriptor(s) outlived the run")
    if growth is not None and growth > 64 * 1048576:
        problems.append(f"resident memory grew {growth / 1048576:.0f} MiB after warm-up")
    result.facts.update(seconds=seconds, max_lateness=max_lateness, max_drift=max_drift, longest_stretch=timing.longest_stretch, extension=bool(extension), slow_asserted=bool(check_slow_listeners),
                        cap_probed=bool(stream_cap and cap_done), memory_measured=growth is not None, drift_judged=bool(timing.drifts))
    return result
