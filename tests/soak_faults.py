"""Fault injection, crash recovery and executable repros for the station engine.  Not collected by unittest.

``run_matrix`` runs every operation once cleanly to learn how many database statements, commits and filesystem
calls it makes, then re-runs it on a fresh station once per call with that call failing, for several kinds of
failure.  After each failure it holds the engine to its documented behaviour: a storage fault latches the
station stopped and rejects further changes, the data on disk is always at a commit boundary of the clean run
and passes integrity checks, nothing is left half written, and reopening the directory recovers.

Behaviour the engine has today that departs from the contract in docs/HANDOFF.md section 8 is listed in KNOWN
with the finding that describes it (reports/soak-findings.md); the expectation for those calls is what the
engine does now.  When a known departure stops reproducing the case fails and says so, so the list cannot go
stale.  ``run_crash`` kills a running service with SIGKILL and reopens the directory.  ``run_findings`` runs
the minimal repro of each defect.
"""
from __future__ import annotations

import collections
import contextlib
import datetime as dt
import errno
import json
import os
import pathlib
import random
import resource
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

import myradio.media as media_module  # noqa: E402
from myradio import station as station_module  # noqa: E402
from myradio.station import Station, StationError  # noqa: E402
from soak_core import (FRAMES, SILENCE, SIM_START, ModuleProxy, SimClock, Tapes, check_persisted, close_descriptors_of, deterministic_engine,  # noqa: E402
                       fd_count, no_converter, offline_agent_env, open_descriptors, persisted_state, relaxed_commits)

FAULTS = {
    "sqlite-io": lambda: sqlite3.OperationalError("disk I/O error"),
    "sqlite-full": lambda: sqlite3.OperationalError("database or disk is full"),
    "sqlite-corrupt": lambda: sqlite3.DatabaseError("database disk image is malformed"),
    "os-nospace": lambda: OSError(errno.ENOSPC, "No space left on device"),
    "os-io": lambda: OSError(errno.EIO, "Input/output error"),
    "os-denied": lambda: PermissionError(errno.EACCES, "Permission denied"),
}
DB_FAULTS = ("sqlite-io", "sqlite-full", "sqlite-corrupt", "os-nospace", "os-io")
OS_FAULTS = ("os-nospace", "os-io", "os-denied")
QUICK_FAULTS = ("sqlite-io", "os-nospace")
DB_KINDS = ("execute", "commit", "commit-after")  # a statement, a commit that fails, a commit that succeeded and then reports failure
# What the engine does today that departs from the contract: (operation, failing call) -> finding id.
KNOWN = {("import", "replace"): "F3", ("import", "wave"): "F3", ("import", "chmod"): "F3", ("import", "commit-after"): "F13",
         ("library.delete", "unlink"): "F2", ("library.delete", "commit-after"): "F2",
         ("tick.start", "osopen"): "F12", ("tick.start", "read"): "F12", ("tick.boundary", "osopen"): "F12",
         ("tick.boundary", "read"): "F12", ("tick.midtrack", "read"): "F12"}


class Finding:
    def __init__(self, ident, title, severity, present, evidence):
        self.ident, self.title, self.severity, self.present, self.evidence = ident, title, severity, present, evidence


@contextlib.contextmanager
def engine(seed=7):
    """Deterministic clock and ids for one station (the same seed gives the same ids on every fresh station)."""
    clock = SimClock()
    with deterministic_engine(clock, seed):
        yield clock


# --------------------------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------------------------
class Probe:
    """Counts the calls an operation makes and fails the armed one (and, if ``persistent``, every later database call)."""

    def __init__(self):
        self.calls = collections.Counter()
        self.kind = self.index = self.make = None
        self.persistent = False
        self.fired = 0
        self.broken = False

    def arm(self, kind, index, make, persistent=False):
        self.kind, self.index, self.make, self.persistent = kind, index, make, persistent

    def hit(self, kind):
        self.calls[kind] += 1
        if self.broken and kind in DB_KINDS:
            return True
        if self.kind == kind and self.calls[kind] == self.index:
            self.fired += 1
            self.broken = self.persistent
            return True
        return False


class ProbeDB:
    """Stands in for the station's sqlite connection."""

    def __init__(self, db, probe):
        self.__dict__.update(_db=db, probe=probe, on_commit=None, rollback_fails=False)

    def execute(self, sql, *params):
        if self.probe.hit("execute"):
            raise self.probe.make()
        return self._db.execute(sql, *params)

    def executemany(self, sql, rows):
        if self.probe.hit("execute"):
            raise self.probe.make()
        return self._db.executemany(sql, rows)

    def commit(self):
        if self.probe.hit("commit"):
            raise self.probe.make()
        self._db.commit()
        if self.on_commit:
            self.on_commit()
        if self.probe.hit("commit-after"):
            raise self.probe.make()

    def rollback(self):
        if self.rollback_fails:
            raise sqlite3.OperationalError("injected rollback failure")
        return self._db.rollback()

    def __getattr__(self, name):
        return getattr(self._db, name)


Proxy = ModuleProxy


def raiser(error):
    def call(*args, **kwargs):
        raise error
    return call


@contextlib.contextmanager
def injected(station, probe, kinds):
    """Count (and, when armed, fail) the calls named in ``kinds`` for the duration of the block."""
    def hook(kind, real):
        def call(*args, **kwargs):
            if probe.hit(kind):
                raise probe.make()
            return real(*args, **kwargs)
        return call

    import wave as wave_module
    with contextlib.ExitStack() as stack:
        wrapped = None
        if set(kinds) & set(DB_KINDS):
            wrapped = ProbeDB(station.db, probe)
            station.db = wrapped
            stack.callback(lambda: setattr(station, "db", wrapped._db))
        if "replace" in kinds:
            stack.enter_context(mock.patch.object(station_module, "os", Proxy(os, replace=hook("replace", os.replace))))
        if "wave" in kinds:
            stack.enter_context(mock.patch.object(station_module, "wave", Proxy(wave_module, open=hook("wave", wave_module.open))))
        for kind in ("chmod", "unlink"):
            if kind in kinds:
                real = getattr(pathlib.Path, kind)
                stack.enter_context(mock.patch.object(pathlib.Path, kind, lambda self, *a, _h=hook(kind, real), **k: _h(self, *a, **k)))
        if "open" in kinds:
            stack.enter_context(mock.patch.object(station_module, "open_media", hook("open", media_module.open_media)))
        if "osopen" in kinds:
            stack.enter_context(mock.patch.object(media_module, "os", Proxy(os, open=hook("osopen", os.open))))
        if "read" in kinds:
            real = media_module.Reader.readframes
            stack.enter_context(mock.patch.object(media_module.Reader, "readframes", lambda self, *a, _h=hook("read", real), **k: _h(self, *a, **k)))
        if "close" in kinds:  # closing reports an error but the descriptor is released, as close(2) does
            real = media_module.Reader.close

            def failing_close(self, *a, **k):
                real(self, *a, **k)
                if probe.hit("close"):
                    raise probe.make()

            stack.enter_context(mock.patch.object(media_module.Reader, "close", failing_close))
        if "broadcast" in kinds:
            stack.enter_context(mock.patch.object(station.broadcast, "stop", hook("broadcast", station.broadcast.stop)))
        yield wrapped


# --------------------------------------------------------------------------------------------
# Operations under test: a starting state, one call, and the kinds of call inside it that can fail
# --------------------------------------------------------------------------------------------
class Ctx:
    def __init__(self, station, clock):
        self.s, self.clock, self.ids, self.args = station, clock, {}, {}
        self.wav = Tapes.wav(40, FRAMES * 3)

    @property
    def now(self):
        return self.clock.now

    def at(self, seconds):
        return dt.datetime.fromtimestamp(SIM_START + seconds, dt.timezone.utc).isoformat()


def _library(c):
    for index, name in enumerate(("Alpha", "Bravo", "Charlie")):
        c.ids[name] = c.s.import_wav(Tapes.wav(index, FRAMES * 3), name)["id"]
    c.ids["Voice"] = c.s.import_wav(Tapes.wav(9, FRAMES * 2), "Voice", "", "speech")["id"]


def st_idle(c):
    _library(c)


def st_queued(c):
    _library(c)
    c.s.action("queue.add", {"track_id": c.ids["Alpha"]})
    c.args["queue_id"] = c.s.db.execute("SELECT id FROM queue").fetchone()[0]


def st_scheduled(c):
    _library(c)
    c.s.action("schedule.add", {"track_id": c.ids["Alpha"], "at": c.at(600)})
    c.args["schedule_id"] = c.s.db.execute("SELECT id FROM schedules").fetchone()[0]


def st_program(c):
    _library(c)
    c.s.action("program.add", {"track_id": c.ids["Alpha"], "timezone": "UTC", "local_time": "10:00", "weekdays": "1,2,3,4,5,6,7"})
    c.args["program_id"] = c.s.db.execute("SELECT id FROM programs").fetchone()[0]


def st_playing(c):
    _library(c)
    c.s.action("station.update", {"auto_fill": False})
    for name in ("Alpha", "Bravo"):
        c.s.action("queue.add", {"track_id": c.ids[name]})
    c.s.action("transport.start")
    c.s.tick(c.now)  # Alpha is playing, Bravo is queued


def st_boundary(c):
    c.ids["Short"] = c.s.import_wav(Tapes.wav(5, FRAMES), "Short")["id"]
    c.ids["Next"] = c.s.import_wav(Tapes.wav(6, FRAMES * 3), "Next")["id"]
    c.s.action("station.update", {"auto_fill": False})
    for name in ("Short", "Next"):
        c.s.action("queue.add", {"track_id": c.ids[name]})
    c.s.action("transport.start")
    c.s.tick(c.now)  # all of "Short" has been played; the next tick ends it and starts "Next"


def st_events(c):
    _library(c)
    c.s.action("schedule.add", {"track_id": c.ids["Voice"], "at": c.at(5)})
    c.s.action("program.add", {"track_id": c.ids["Bravo"], "timezone": "UTC", "local_time": "00:01", "weekdays": "1,2,3,4,5,6,7"})
    c.s.action("transport.start")
    c.clock.now = SIM_START + 62  # the one-shot is 57 s late and the program 2 s late: both are admitted on the next tick


def st_retry(c):
    _library(c)
    path = Path(c.s.directory) / "media" / (c.ids["Alpha"] + ".wav")
    good = path.read_bytes()
    path.write_bytes(b"broken")
    c.s.action("station.update", {"auto_fill": False})
    c.s.action("queue.add", {"track_id": c.ids["Alpha"]})
    c.s.action("transport.start")
    c.s.tick(c.now)  # Alpha cannot be opened: quarantined
    path.write_bytes(good)  # repaired; the operator now retries it


OPERATIONS = {
    "import": (st_idle, lambda c: c.s.import_audio(c.wav, "New", "Artist", "music"), DB_KINDS + ("replace", "wave", "chmod")),
    "queue.add": (st_idle, lambda c: c.s.action("queue.add", {"track_id": c.ids["Alpha"]}), DB_KINDS),
    "queue.remove": (st_queued, lambda c: c.s.action("queue.remove", {"queue_id": c.args["queue_id"]}), DB_KINDS),
    "schedule.add": (st_idle, lambda c: c.s.action("schedule.add", {"track_id": c.ids["Alpha"], "at": c.at(900)}), DB_KINDS),
    "schedule.cancel": (st_scheduled, lambda c: c.s.action("schedule.cancel", {"schedule_id": c.args["schedule_id"]}), DB_KINDS),
    "program.add": (st_idle, lambda c: c.s.action("program.add", {"track_id": c.ids["Alpha"], "timezone": "Europe/London", "local_time": "08:30", "weekdays": "1,3,5"}), DB_KINDS),
    "program.remove": (st_program, lambda c: c.s.action("program.remove", {"program_id": c.args["program_id"]}), DB_KINDS),
    "station.update": (st_idle, lambda c: c.s.action("station.update", {"name": "New name", "auto_fill": False}), DB_KINDS),
    "transport.start": (st_idle, lambda c: c.s.action("transport.start"), DB_KINDS),
    "transport.stop": (st_playing, lambda c: c.s.action("transport.stop"), DB_KINDS + ("broadcast", "close")),
    "transport.skip": (st_playing, lambda c: c.s.action("transport.skip"), DB_KINDS + ("close",)),
    "library.delete": (st_idle, lambda c: c.s.action("library.delete", {"track_id": c.ids["Charlie"]}), DB_KINDS + ("unlink",)),
    "library.retry": (st_retry, lambda c: c.s.action("library.retry", {"track_id": c.ids["Alpha"]}), DB_KINDS + ("open", "osopen")),
    "tick.start": (st_queued, lambda c: (c.s.action("transport.start"), c.s.tick(c.now))[1], DB_KINDS + ("open", "osopen", "read")),
    "tick.midtrack": (st_playing, lambda c: c.s.tick(c.now), DB_KINDS + ("read",)),
    "tick.boundary": (st_boundary, lambda c: c.s.tick(c.now), DB_KINDS + ("open", "osopen", "read", "close")),
    "tick.events": (st_events, lambda c: c.s.tick(c.now), DB_KINDS),
    "status": (st_playing, lambda c: c.s.status(), ("execute",)),
}


def expectation(op, kind):
    """What the engine is documented (or, for KNOWN entries, observed) to do when this call fails."""
    known = KNOWN.get((op, kind))
    if op == "status":  # a failed read latches and answers from the last good snapshot, marked stale
        return {"raises": None, "latched": True, "compare": True, "known": known}
    if (op, kind) == ("import", "commit-after"):  # the row is committed, then the cleanup for a failed import removes its audio file
        return {"raises": StationError, "latched": True, "compare": True, "known": known, "missing": True}
    if (op, kind) == ("library.delete", "commit-after"):  # the row is gone; the audio file is never removed
        return {"raises": StationError, "latched": True, "compare": True, "known": known, "orphans": True}
    if kind in DB_KINDS or kind == "broadcast":
        return {"raises": StationError, "latched": True, "compare": True, "known": known}
    if kind in ("replace", "wave", "chmod"):  # import: the media file is written before the transaction starts
        return {"raises": OSError, "latched": False, "compare": True, "known": known}
    if kind == "unlink":  # library.delete: the row is committed before the file is removed
        return {"raises": StationError, "latched": True, "compare": True, "known": known, "orphans": True}
    if kind == "close":  # a reader that cannot be closed is ignored
        return {"raises": None, "latched": False, "compare": True, "known": known}
    if op == "library.retry":  # opening failed: a plain refusal, nothing changes, no latch
        return {"raises": StationError, "latched": False, "compare": True, "known": known}
    if kind == "open":  # an unexpected OSError out of the opener is a storage fault
        return {"raises": StationError, "latched": True, "compare": True, "known": known}
    # os.open inside the opener, or a failed read: the track is quarantined and the clock carries on
    return {"raises": None, "latched": False, "compare": False, "known": known, "quarantines": True}


def faults_for(op, kind, names):
    if op == "status":
        return [n for n in names if n.startswith("sqlite")]
    return [n for n in names if n in (DB_FAULTS if kind in DB_KINDS else OS_FAULTS)]


class CaseResult:
    def __init__(self, op, kind, index, fault):
        self.op, self.kind, self.index, self.fault = op, kind, index, fault
        self.fired, self.problems, self.known, self.note = False, [], None, ""

    @property
    def label(self):
        return f"{self.op} / {self.kind} #{self.index} / {self.fault}"


def build(directory, op, clock):
    station = Station(directory, start_worker=False)
    ctx = Ctx(station, clock)
    OPERATIONS[op][0](ctx)
    return station, ctx


def dry_run(op, directory, clock):
    """Run the operation cleanly: how many calls of each kind it makes, and the persisted state after every commit."""
    station, ctx = build(directory, op, clock)
    probe, kinds = Probe(), set(OPERATIONS[op][2])
    states = [persisted_state(directory)]
    with injected(station, probe, kinds | set(DB_KINDS)) as wrapped:
        wrapped.on_commit = lambda: states.append(persisted_state(directory))
        OPERATIONS[op][1](ctx)
    station.close()
    return {kind: probe.calls[kind] for kind in kinds}, states


def run_case(op, kind, index, fault, directory, clock, allowed, fd_limit, persistent=False):
    result = CaseResult(op, kind, index, fault)
    spec = expectation(op, kind)
    result.known = spec["known"]
    station, ctx = build(directory, op, clock)
    probe = Probe()
    probe.arm(kind, index, FAULTS[fault], persistent)
    raised = outcome = None
    with injected(station, probe, {kind}):
        try:
            outcome = OPERATIONS[op][1](ctx)
        except BaseException as exc:  # noqa: BLE001 - whatever the engine lets escape is evidence
            raised = exc
    result.fired = probe.fired > 0
    if not result.fired:
        station.close()
        return result
    problems = result.problems
    # what the caller saw
    if spec["raises"] is None:
        if raised is not None:
            problems.append(f"raised {type(raised).__name__}: {raised}")
    elif not isinstance(raised, spec["raises"]):
        problems.append(f"expected {spec['raises'].__name__}, got {type(raised).__name__ if raised else 'no error'}")
    elif isinstance(raised, StationError):
        text = str(raised)
        if not text or "Traceback" in text or "sqlite" in text.lower() or "Errno" in text:
            problems.append(f"the message is not plain language: {text!r}")
    # what the station became
    if station.faulted != spec["latched"]:
        problems.append(f"latched={station.faulted}, expected {spec['latched']}")
    if station.faulted:
        status = station.status()
        json.dumps(status, allow_nan=False)
        if status["health"]["state"] != "fault" or not status.get("error") or status["running"]:
            problems.append("status does not report the fault")
        if station.running or station.reader is not None or station.current is not None:
            problems.append("a faulted station is still running, reading or has a current track")
        refusals = (lambda: station.action("queue.add", {"track_id": next(iter(ctx.ids.values()))}), lambda: station.action("transport.start"),
                    lambda: station.import_wav(ctx.wav, "After"))
        for refused in refusals:
            try:
                refused()
                problems.append("a faulted station accepted a change")
            except StationError as exc:
                if "restart" not in str(exc):
                    problems.append(f"the refusal does not tell the operator to restart: {exc}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"a refusal escaped as {type(exc).__name__}")
        if station.tick(ctx.now) != SILENCE:
            problems.append("a faulted station played audio")
    if op == "status" and (outcome is None or outcome["health"].get("state") != "fault" or not outcome["health"].get("stale")):
        problems.append("the answer to a failed read does not say that it is a fault answered from the last good snapshot")
    if station.db.in_transaction:
        problems.append("a transaction was left open")
    # what is on disk
    state = persisted_state(directory)
    if spec["compare"] and state not in allowed:
        problems.append("the data on disk is not at a commit boundary of the clean run (a partial change)")
    for message in check_persisted(directory):
        if spec.get("orphans") and message.startswith("orphan media file"):
            result.note = "audio file left behind"
        elif spec.get("missing") and message.startswith("media file missing"):
            result.note = "audio file removed although the row was saved"
        else:
            problems.append(message)
    if spec.get("quarantines") and not state["failures"] and raised is None:
        problems.append("the failed read or open did not quarantine the track")
    # close and reopen
    station.close()
    if fd_count() > fd_limit:
        problems.append("descriptors leaked by a failed operation and close")
    reopened = Station(directory, start_worker=False)
    try:
        if reopened.faulted or reopened.running or reopened.error:
            problems.append("a reopened station is faulted or already running")
        for message in check_persisted(directory):
            if not (spec.get("orphans") and message.startswith("orphan media file")) and not (spec.get("missing") and message.startswith("media file missing")):
                problems.append("after reopen: " + message)
        reopened.import_wav(Tapes.wav(50, FRAMES), "After recovery")
        reopened.action("station.update", {"auto_fill": True})
        reopened.action("transport.start")
        if not any(reopened.tick(SIM_START + 3600 + i) != SILENCE for i in range(4)):
            problems.append("a reopened station could not play after recovery")
        json.dumps(reopened.status(), allow_nan=False)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"a reopened station failed: {type(exc).__name__}: {exc}")
    finally:
        reopened.close()
    if problems and result.known:
        problems.insert(0, f"known departure {result.known} no longer matches: if it was fixed, update KNOWN and expectation() in soak_faults.py")
    return result


class MatrixResult:
    def __init__(self):
        self.cases, self.not_reached, self.wall, self.real, self.unraisable = [], 0, 0.0, [], collections.Counter()

    @property
    def failures(self):
        return [c for c in self.cases if c.problems]

    @property
    def ok(self):
        """No injected case and no real fault went wrong.  A real fault that could not be run here (status None) is not a failure, but it is
        not a pass either: ``skipped`` lists them and the text says so."""
        return bool(self.cases) and not self.failures and all(ok is not False for _, ok, _ in self.real)

    @property
    def skipped(self):
        return [name for name, ok, _ in self.real if ok is None]

    @property
    def departures(self):
        """Known departures from the contract whose cases still behave exactly as recorded, with how many cases each has."""
        return collections.Counter(c.known for c in self.cases if c.known and not c.problems)

    def text(self):
        by = collections.defaultdict(lambda: [0, 0, 0])
        for c in self.cases:
            row = by[(c.op, c.kind)]
            row[0] += 1
            row[1] += bool(c.problems)
            row[2] += bool(c.known and not c.problems)
        width = max((len(f"{op} / {kind}") for op, kind in by), default=10)
        departures = self.departures
        headline = "PASS" if self.ok else "FAIL"
        if self.ok and departures:
            headline += " with known departures " + ", ".join(sorted(departures))  # a pass that still records engine defects must say so
        if self.skipped:
            headline += f" ({len(self.skipped)} of {len(self.real)} real-system faults could not be run here)"
        lines = [f"result: {headline}", f"  failure cases run: {len(self.cases)}   not reached: {self.not_reached}   wall time: {self.wall:.1f} s", "",
                 f"  {'operation / failing call'.ljust(width)}  {'cases':>6}  {'failed':>6}  {'matches known departure':>24}"]
        lines += [f"  {f'{op} / {kind}'.ljust(width)}  {n:>6}  {bad:>6}  {known:>24}" for (op, kind), (n, bad, known) in sorted(by.items())]
        if departures:
            lines += ["", "  matched known departures from the contract: " + ", ".join(f"{k} x{v}" for k, v in sorted(departures.items()))]
        if self.unraisable:
            lines += ["", "  exceptions Python reported while cleaning up objects (counted, not printed): " + ", ".join(f"{k} x{v}" for k, v in sorted(self.unraisable.items()))]
        lines += ["", "faults made by the real system:"] + [f"  {'SKIP' if ok is None else 'ok  ' if ok else 'FAIL'} {name}: {detail}" for name, ok, detail in self.real]
        for c in self.failures[:12]:
            lines += ["", f"FAILED {c.label}"] + [f"    {p}" for p in c.problems]
        return "\n".join(lines)


def run_matrix(quick=False, workdir=None, ops=None):
    """The failures are injected exceptions and every check reads committed data through a second connection, so an fsync per
    commit would add time and no evidence: the matrix runs with relaxed commits (see soak_core.relaxed_commits)."""
    unraisable = []
    saved_hook, sys.unraisablehook = sys.unraisablehook, lambda arg: unraisable.append(f"{arg.exc_type.__name__} in {getattr(arg.object, '__qualname__', type(arg.object).__name__)}")
    try:
        with relaxed_commits():
            result = _run_matrix(quick, workdir, ops)
    finally:
        sys.unraisablehook = saved_hook
    result.unraisable = collections.Counter(unraisable)
    return result


def _run_matrix(quick, workdir, ops):
    began = time.perf_counter()
    result = MatrixResult()
    names = QUICK_FAULTS if quick else tuple(FAULTS)
    with tempfile.TemporaryDirectory(prefix="faults-", dir=workdir) as root:
        counter = [0]

        def fresh():
            counter[0] += 1
            return os.path.join(root, f"case{counter[0]}")

        def sweep():
            """Called between cases, when every case directory is finished with: the scratch space stays the size of one case instead of growing
            with the number of cases (the full matrix is 763 of them)."""
            for name in os.listdir(root):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)

        fresh.sweep = sweep
        fd_limit = None
        for op in (ops or OPERATIONS):
            with engine() as clock:
                counts, allowed = dry_run(op, fresh(), clock)
            sweep()
            for kind, calls in counts.items():
                for index in range(1, calls + 1):
                    for fault in faults_for(op, kind, names):
                        with engine() as clock:
                            case = run_case(op, kind, index, fault, fresh(), clock, allowed, fd_limit if fd_limit is not None else 10 ** 6)
                        sweep()
                        if fd_limit is None:
                            fd_limit = fd_count()  # calibrated after the first complete case
                        if case.fired:
                            result.cases.append(case)
                        else:
                            result.not_reached += 1
            # the database stays broken after the first failure (a disk that is gone, not a hiccup)
            for kind in DB_KINDS:
                for index in sorted({1, max(1, counts.get(kind, 1))}):
                    with engine() as clock:
                        case = run_case(op, kind, index, "sqlite-io", fresh(), clock, allowed, fd_limit or 10 ** 6, persistent=True)
                    sweep()
                    case.fault += " (persistent)"
                    if case.fired:
                        result.cases.append(case)
        result.real = real_faults(fresh)
        sweep()
    result.wall = time.perf_counter() - began
    return result


# --------------------------------------------------------------------------------------------
# Faults made by the real system rather than by a stand-in
# --------------------------------------------------------------------------------------------
SKIPPED = None  # the status of a real fault that could not be made on this machine: neither a pass nor a failure


def need(condition, message):
    """The verdicts of the real-system faults.  An explicit raise rather than ``assert``, because Python strips assert statements under -O
    (PYTHONOPTIMIZE) and every verdict would then silently pass."""
    if not condition:
        raise AssertionError(message)


def real_faults(fresh):
    """Faults made by the operating system.  Returns ``(name, status, detail)``: status True survived, False failed, None could not be run here."""
    out = []

    def latched(station, what):
        need(station.faulted, f"{what}: the station did not latch")
        need(not station.running and station.reader is None, f"{what}: still running")
        need(station.status()["health"]["state"] == "fault", f"{what}: status does not report it")

    def reopen_ok(directory):
        problems = check_persisted(directory)
        need(not problems, "; ".join(problems[:2]))
        again = Station(directory, start_worker=False)
        try:
            need(not again.faulted and not again.running, "the reopened station is faulted or already running")
            again.import_wav(Tapes.wav(51, FRAMES), "Recovered")
        finally:
            again.close()

    def read_only_database():
        d = fresh()
        with engine() as clock:
            station, ctx = build(d, "queue.add", clock)
            station.db.execute("PRAGMA query_only=ON")
            try:
                station.action("queue.add", {"track_id": ctx.ids["Alpha"]})
                raise AssertionError("a write to a read-only database was accepted")
            except StationError:
                pass
            latched(station, "read-only database")
            station.close()
        reopen_ok(d)
        return "SQLite refused the write; the station latched stopped; the directory reopened and could import"

    def full_database():
        d = fresh()
        with engine() as clock:
            station, ctx = build(d, "queue.add", clock)
            station.db.execute(f"PRAGMA max_page_count={station.db.execute('PRAGMA page_count').fetchone()[0]}")
            station.db.execute('CREATE TEMP TRIGGER fill BEFORE INSERT ON main.history BEGIN INSERT INTO history(title,at,event) VALUES (zeroblob(1048576),"t","full"); END')
            try:
                station.import_wav(ctx.wav, "Too big for the disk")
                raise AssertionError("an import into a full database was accepted")
            except StationError:
                pass
            latched(station, "full database")
            need(not any(n.endswith(".tmp") for n in os.listdir(station.media)), "a staged file was left behind")
            station.close()
        reopen_ok(d)
        return "SQLite reported a full database during an import; latched; nothing half written; reopened and could import"

    def file_size_limit():
        d = fresh()
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        with engine() as clock:
            station, _ = build(d, "queue.add", clock)
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (60000, hard))
                try:
                    station.import_wav(Tapes.wav(60, FRAMES * 8), "Too large for the file size limit")
                    raise AssertionError("an import past the file size limit was accepted")
                except OSError as exc:
                    note = f"raw {type(exc).__name__}, errno {exc.errno}"
                except StationError:
                    note = "StationError"
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            need(not any(n.endswith(".tmp") for n in os.listdir(station.media)), "a staged file was left behind")
            was_latched = station.faulted
            station.close()
        reopen_ok(d)
        return f"the operating system refused the write ({note}); latched={was_latched} (finding F3); no half-written file; reopened"

    def unwritable_media_folder():
        if os.geteuid() == 0:
            return SKIPPED, "not run: this machine is running the test as the administrator account, which ignores file permissions"
        d = fresh()
        with engine() as clock:
            station, ctx = build(d, "queue.add", clock)
            os.chmod(station.media, 0o500)
            try:
                try:
                    station.import_wav(ctx.wav, "No room to write")
                    raise AssertionError("an import into an unwritable folder was accepted")
                except PermissionError:
                    note = "raw PermissionError"
                except StationError:
                    note = "StationError"
            finally:
                os.chmod(station.media, 0o700)
            was_latched = station.faulted
            station.close()
        reopen_ok(d)
        return f"the folder refused the file ({note}); latched={was_latched} (finding F3); reopened"

    def unreadable_media_file():
        if os.geteuid() == 0:
            return SKIPPED, "not run: this machine is running the test as the administrator account, which ignores file permissions"
        d = fresh()
        with engine() as clock:
            station, ctx = build(d, "queue.add", clock)
            target = Path(d) / "media" / (ctx.ids["Alpha"] + ".wav")
            os.chmod(target, 0)
            station.action("station.update", {"auto_fill": False})
            station.action("queue.add", {"track_id": ctx.ids["Alpha"]})
            station.action("transport.start")
            station.tick(ctx.now)
            need(not station.faulted and station.running, "an unreadable file latched the station")
            need(ctx.ids["Alpha"] in {r[0] for r in station.db.execute("SELECT track_id FROM media_failures")}, "the unreadable file was not quarantined")
            os.chmod(target, 0o600)
            station.action("library.retry", {"track_id": ctx.ids["Alpha"]})
            station.close()
        reopen_ok(d)
        return "an unreadable file was quarantined, the station kept running, and it recovered after the file was repaired and retried"

    def commit_and_rollback_fail():
        d = fresh()
        with engine() as clock:
            station, ctx = build(d, "queue.add", clock)
            before = persisted_state(d)
            probe = Probe()
            probe.arm("commit", 1, FAULTS["sqlite-io"])
            with injected(station, probe, {"commit"}) as wrapped:
                wrapped.rollback_fails = True
                try:
                    station.action("queue.add", {"track_id": ctx.ids["Alpha"]})
                    raise AssertionError("a failed commit was reported as success")
                except StationError:
                    pass
            latched(station, "failed commit and failed rollback")
            station.close()
            need(persisted_state(d) == before, "the failed change reached the disk")
        reopen_ok(d)
        return "commit and rollback both failed: the station latched, closing discarded the change, and the directory reopened"

    sweep = getattr(fresh, "sweep", None)
    for name, fn in (("read-only database", read_only_database), ("full database", full_database), ("file size limit during an import", file_size_limit),
                     ("unwritable media folder", unwritable_media_folder), ("unreadable media file", unreadable_media_file),
                     ("commit and rollback both fail", commit_and_rollback_fail)):
        try:
            verdict = fn()
            out.append((name, *verdict) if isinstance(verdict, tuple) else (name, True, verdict))
        except AssertionError as exc:
            out.append((name, False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            out.append((name, False, f"{type(exc).__name__}: {exc}"))
        if sweep:
            sweep()
    return out


# --------------------------------------------------------------------------------------------
# kill -9 and reopen
# --------------------------------------------------------------------------------------------
CRASH_MARKER = ".soak-crash-scratch"  # written by run_crash into the directory it makes; the helper process refuses any directory without it
CHILD_LIFETIME = 60  # seconds: the helper ends itself even if nothing ever tells it to stop
CHILD_LIBRARY_CAP = 150  # tracks: past this the helper stops importing, so a helper that is left running cannot fill a disk
CHILD_PAUSE = 0.001  # seconds between operations: a busy loop would take a whole processor and add nothing the kill does not already test


def crash_child(directory, seed, *, lifetime=CHILD_LIFETIME, library_cap=CHILD_LIBRARY_CAP):
    """Runs in its own process until it is killed: real clock thread and a loop of operator actions.

    It cannot outlive the test that started it: it ends when its parent process is gone (checked on every pass, so SIGKILL, a ``timeout``
    wrapper or an abort of the parent cannot leave it behind), it ends by itself after ``lifetime`` seconds, and it stops importing at
    ``library_cap`` tracks.  It refuses to touch any directory that ``run_crash`` did not create."""
    if not (Path(directory) / CRASH_MARKER).is_file():
        print("This folder was not made by the crash test, so the helper will not change anything in it.", file=sys.stderr)
        return 2
    parent = os.getppid()
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)  # the default action ends the process even if every other thread is busy
        signal.alarm(max(1, int(lifetime)))
    rng = random.Random(seed)
    with no_converter():
        station = Station(directory)
        ids = [r[0] for r in station.db.execute("SELECT id FROM tracks")]
        print("ready", flush=True)
        serial = 0
        while True:
            if os.getppid() != parent:
                os._exit(0)  # the parent is gone: nobody will ever clean up after this process
            time.sleep(CHILD_PAUSE)
            try:
                roll = rng.random()
                if (roll < 0.30 or not ids) and len(ids) < library_cap:
                    serial += 1
                    ids.append(station.import_wav(Tapes.wav(serial % 500, rng.choice((FRAMES, FRAMES * 3, FRAMES * 8))), f"C{seed}-{serial}")["id"])
                elif roll < 0.55 and ids:
                    station.action("queue.add", {"track_id": rng.choice(ids)})
                elif roll < 0.62:
                    rows = station.db.execute("SELECT id FROM queue LIMIT 1").fetchall()
                    if rows:
                        station.action("queue.remove", {"queue_id": rows[0][0]})
                elif roll < 0.72 and ids:
                    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=rng.randint(1, 90))
                    station.action("schedule.add", {"track_id": rng.choice(ids), "at": when.isoformat()})
                elif roll < 0.76 and ids:
                    station.action("program.add", {"track_id": rng.choice(ids), "timezone": "UTC", "local_time": f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}",
                                                   "weekdays": "1,2,3,4,5,6,7"})
                elif roll < 0.84:
                    station.action("transport.start" if rng.random() < 0.6 else "transport.skip")
                elif roll < 0.88:
                    station.action("transport.stop")
                elif (roll < 0.92 or len(ids) >= library_cap) and len(ids) > 6:
                    victim = ids.pop(rng.randrange(len(ids)))
                    try:
                        station.action("library.delete", {"track_id": victim})
                    except StationError:
                        ids.append(victim)
                else:
                    station.action("station.update", {"name": f"Crash {rng.randint(0, 9999)}"})
            except StationError:
                pass


def spawn_crash_child(directory, seed):
    """Start the helper process for ``directory`` in a session of its own (so the whole group can be killed) and return the Popen.

    The directory is marked first: the helper refuses a folder without the mark."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    (Path(directory) / CRASH_MARKER).write_text("made by the myradio crash test; safe to delete\n")
    script = Path(__file__).resolve().parent / "soak_station.py"
    return subprocess.Popen([sys.executable, "-B", str(script), "_child", "--dir", str(directory), "--seed", str(seed)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(script.parent.parent))


def tracks_saved(directory):
    """How many library tracks the data directory holds on disk right now (0 when it cannot be read yet)."""
    try:
        conn = sqlite3.connect(str(Path(directory) / "station.sqlite3"), timeout=2)
        try:
            conn.execute("PRAGMA query_only=ON")
            return conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def kill_child_group(proc):
    """Kill the helper's process group, but only while the helper has not been reaped: once ``poll`` has returned an exit status its process
    number is free to be given to some other process, and that one must never be signalled."""
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # a process group of our own making, never anyone else's
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


@contextlib.contextmanager
def terminate_as_exception():
    """While active, SIGTERM and SIGHUP raise SystemExit, so that ``finally`` blocks (which kill the helper) run when a ``timeout`` wrapper or a
    closing terminal ends the test.  Only the main thread can install handlers; elsewhere this does nothing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    names = [name for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    saved = {name: signal.signal(getattr(signal, name), stop) for name in names}
    try:
        yield
    finally:
        for name, handler in saved.items():
            signal.signal(getattr(signal, name), handler if handler is not None else signal.SIG_DFL)


class CrashResult:
    def __init__(self):
        self.cycles, self.problems = [], []

    @property
    def ok(self):
        return bool(self.cycles) and not self.problems

    def text(self):
        lines = [f"result: {'PASS' if self.ok else 'FAIL'}", f"  cycles: {len(self.cycles)}", ""]
        lines += [f"  cycle {i + 1}: killed after {c['alive']:.2f} s; data checks ok={c['integrity']}; library {c['tracks']}, queue {c['queue']}, "
                  f"orphan media files {c['orphans']}, staged leftovers {c['staged']}; reopened and started playing a track ok={c['reopened']}" for i, c in enumerate(self.cycles)]
        lines += [""] + [f"FAILED {p}" for p in self.problems]
        return "\n".join(lines)


def run_crash(cycles=6, workdir=None, seed=11):
    result = CrashResult()
    with tempfile.TemporaryDirectory(prefix="crash-", dir=workdir) as root, terminate_as_exception():
        directory = os.path.join(root, "station")
        rng = random.Random(seed)
        for cycle in range(cycles):
            proc = spawn_crash_child(directory, seed + cycle)
            info = {"alive": 0.0, "integrity": False, "tracks": -1, "queue": -1, "orphans": -1, "staged": -1, "reopened": False}
            try:
                ready, began = False, time.monotonic()
                while time.monotonic() - began < 20 and proc.poll() is None:
                    if select.select([proc.stdout], [], [], 0.2)[0]:
                        ready = proc.stdout.readline().strip() == b"ready"
                        break
                if not ready:
                    result.problems.append(f"cycle {cycle + 1}: the service did not start (exit status {proc.poll()})")
                    break
                saved_by = time.monotonic() + 10  # a kill before the first track is saved would leave nothing to play on reopening
                while time.monotonic() < saved_by and proc.poll() is None and not tracks_saved(directory):
                    time.sleep(0.05)
                info["alive"] = rng.uniform(0.3, 1.4)
                time.sleep(info["alive"])
            finally:
                kill_child_group(proc)
            problems = check_persisted(directory)
            orphans = [p for p in problems if p.startswith("orphan media file")]
            staged = [p for p in problems if p.startswith("unexpected file in media")]
            real = [p for p in problems if p not in orphans and p not in staged]
            info.update(integrity=not real, orphans=len(orphans), staged=len(staged))
            result.problems += [f"cycle {cycle + 1}: {p}" for p in real]
            try:
                again = Station(directory, start_worker=False)
                try:
                    info["tracks"] = again.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
                    info["queue"] = again.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
                    healthy_music = again.db.execute("SELECT COUNT(*) FROM tracks WHERE kind='music' AND id NOT IN (SELECT track_id FROM media_failures)").fetchone()[0]
                    if again.faulted or again.running:
                        result.problems.append(f"cycle {cycle + 1}: the reopened station is faulted or running")
                    again.action("transport.start")
                    block = again.tick(SIM_START)
                    # "played" means audio came out: with a healthy music track in the library the first block after the start must not be silence
                    info["reopened"] = healthy_music > 0 and block != SILENCE
                    if not healthy_music:
                        result.problems.append(f"cycle {cycle + 1}: the library held no playable music track after the kill, so playing could not be checked")
                    elif block == SILENCE:
                        result.problems.append(f"cycle {cycle + 1}: the reopened station was started with {healthy_music} playable music track(s) in the library "
                                               "and produced silence instead of starting one")
                finally:
                    again.close()
            except Exception as exc:  # noqa: BLE001
                result.problems.append(f"cycle {cycle + 1}: could not reopen: {type(exc).__name__}: {exc}")
            result.cycles.append(info)
    return result


# --------------------------------------------------------------------------------------------
# Executable repros: each returns a Finding whose ``present`` is True while the defect reproduces.
# --------------------------------------------------------------------------------------------
@contextlib.contextmanager
def scratch(seed=5, worker=False):
    with tempfile.TemporaryDirectory(prefix="finding-") as directory, engine(seed) as clock, relaxed_commits():
        station = Station(directory, start_worker=worker)
        try:
            yield station, directory, clock
        finally:
            station.close()


def song(station, name="Song", slot=1, frames=FRAMES * 3, kind="music"):
    return station.import_wav(Tapes.wav(slot, frames), name, "", kind)["id"]


def f1_queue_over_limit():
    with scratch() as (s, _, clock):
        t = song(s)
        s.action("station.update", {"auto_fill": False})
        s.db.executemany("INSERT INTO queue(track_id) VALUES (?)", [(t,)] * 1000)  # fixture: a full queue
        s.db.commit()
        s.action("transport.start")
        s.tick(clock.now)
        s.action("queue.add", {"track_id": t})  # allowed: 999 queued
        s.action("transport.stop")
        rows = s.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    return Finding("F1", "Stopping while the queue is full puts the playing track back as entry 1,001", "low", rows > 1000, f"queue rows after stop: {rows} (limit 1,000)")


def f2_delete_orphan():
    with scratch() as (s, d, _):
        t = song(s)
        with mock.patch.object(pathlib.Path, "unlink", side_effect=PermissionError(errno.EACCES, "denied")):
            try:
                s.action("library.delete", {"track_id": t})
            except StationError:
                pass
        latched, rows = s.faulted, s.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        s.close()
        again = Station(d, start_worker=False)
        left = os.listdir(again.media)
        again.close()
    return Finding("F2", "A failed file removal after a library delete latches the station and leaves the audio file behind for good", "low", bool(left) and rows == 0,
                   f"latched={latched}, library rows={rows}, audio files still present after reopening: {len(left)}")


def f3_import_write_failure():
    with scratch() as (s, _, _):
        with mock.patch.object(station_module, "os", Proxy(os, replace=raiser(OSError(errno.ENOSPC, "No space left on device")))):
            try:
                s.import_wav(Tapes.wav(2, FRAMES), "X")
                seen = "accepted"
            except StationError:
                seen = "StationError"
            except OSError as exc:
                seen = f"raw {type(exc).__name__}"
        latched = s.faulted
    return Finding("F3", "A disk-full failure while writing an imported file is not treated as a storage fault", "medium", not latched and seen.startswith("raw"),
                   f"import raised {seen}; station latched={latched} (the brief and HANDOFF section 8 say storage faults latch)")


def f4_cancelled_growth():
    with scratch() as (s, _, clock):
        t = song(s)
        s.db.execute("PRAGMA synchronous=OFF")  # faster; the defect does not depend on it
        at = dt.datetime.fromtimestamp(SIM_START + 3600, dt.timezone.utc).isoformat()
        for _ in range(1100):
            s.action("schedule.add", {"track_id": t, "at": at})
            s.action("schedule.cancel", {"schedule_id": s.db.execute("SELECT id FROM schedules WHERE status='pending'").fetchone()[0]})
        s.action("transport.start")
        s.tick(clock.now)
        rows = s.db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
    return Finding("F4", "Cancelled schedules are only pruned when some event comes due, so add-and-cancel churn grows the table without bound", "low", rows > 1000,
                   f"schedule rows after 1,100 add+cancel cycles and one tick: {rows} (the keep limit is 1,000)")


def f5_schedule_view():
    with scratch() as (s, _, _):
        t = song(s)
        s.db.execute("PRAGMA synchronous=OFF")
        for i in range(150):
            s.action("schedule.add", {"track_id": t, "at": dt.datetime.fromtimestamp(SIM_START + 3600 + i * 60, dt.timezone.utc).isoformat()})
        shown = [x for x in s.status()["schedules"] if x["status"] == "pending"]
    return Finding("F5", "Status lists only the 100 furthest-away schedules, so the soonest pending events cannot be seen or cancelled from the studio", "medium", len(shown) < 150,
                   f"pending events: 150, listed in status: {len(shown)}; the soonest {150 - len(shown)} are missing from the list")


def f6_status_cost():
    rows = []
    with scratch() as (s, _, _):
        blob = Tapes.wav(3, FRAMES)
        s.db.execute("PRAGMA synchronous=OFF")
        for n in (100, 500, 1000, 2000):
            while s.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] < n:
                s.import_wav(blob, f"T{s.db.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]:05d}")
            began = time.perf_counter()
            for _ in range(5):
                s.status()
            rows.append((n, (time.perf_counter() - began) / 5 * 1000))
    return Finding("F6", "status() reads and copies the whole library while holding the playout lock, so its cost (and the clock's wait) grows with the library", "observation",
                   rows[-1][1] > 5, "; ".join(f"{n} tracks: {ms:.1f} ms" for n, ms in rows))


def f7_queue_id_overflow():
    with scratch() as (s, _, _):
        try:
            s.action("queue.remove", {"queue_id": 2 ** 70})
            seen = "accepted"
        except StationError as exc:
            seen = f"StationError: {exc}"
        except Exception as exc:  # noqa: BLE001
            seen = f"raw {type(exc).__name__}: {exc}"
    return Finding("F7", "queue.remove with an integer beyond SQLite's range escapes as a raw OverflowError (HTTP 500) instead of 'Queue entry not found'", "low", seen.startswith("raw"), seen)


def f8_surrogate_import():
    with scratch() as (s, _, _):
        try:
            s.import_wav(Tapes.wav(4, FRAMES), "\ud800")
            seen = "accepted"
        except StationError as exc:
            seen = f"StationError: {exc}"
        except Exception as exc:  # noqa: BLE001
            seen = f"raw {type(exc).__name__}"
        left = len(os.listdir(s.media))
    return Finding("F8", "An import title or artist containing a lone surrogate escapes as a raw UnicodeEncodeError", "low", seen.startswith("raw"), f"{seen}; media files left behind: {left}")


def f9_surrogate_name_breaks_status():
    import http.client
    import secrets
    from myradio.server import Server
    with scratch() as (s, _, _):
        token = secrets.token_urlsafe(32)
        with offline_agent_env():
            server = Server(("127.0.0.1", 0), s, token)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        port = server.server_address[1]

        def call(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request(method, path, body=body, headers={"X-Myradio-Token": token, "Content-Type": "application/json"})
                reply = conn.getresponse()
                reply.read()
                return reply.status
            except (OSError, http.client.HTTPException) as exc:
                return type(exc).__name__
            finally:
                conn.close()

        try:
            before = call("GET", "/api/status")
            renamed = call("POST", "/api/action", b'{"operation":"station.update","arguments":{"name":"\\ud800"}}')
            with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink):  # the server prints a traceback for the failed reply
                after = call("GET", "/api/status")
            call("POST", "/api/action", b'{"operation":"station.update","arguments":{"name":"Fine"}}')
            fixed = call("GET", "/api/status")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    return Finding("F9", "A station name or tagline containing a lone surrogate is accepted, then every /api/status request fails until it is renamed", "medium",
                   before == 200 and renamed == 200 and after != 200 and fixed == 200, f"status before: {before}; rename: {renamed}; status after: {after}; status after renaming back: {fixed}")


def f10_poison_program():
    import myradio.scheduling as scheduling

    class Gone:
        def __new__(cls, key):
            raise scheduling.ZoneInfoNotFoundError(key)

    with scratch() as (s, d, clock):
        t = song(s)
        s.action("program.add", {"track_id": t, "timezone": "Europe/London", "local_time": "10:00", "weekdays": "1,2,3,4,5,6,7"})
        due = s.db.execute("SELECT next_due FROM programs").fetchone()[0]
        with mock.patch.object(scheduling, "ZoneInfo", Gone):
            try:
                s.tick(due + 1)
                seen = "tick succeeded"
            except StationError:
                seen = "StationError"
            except Exception as exc:  # noqa: BLE001
                seen = f"raw {type(exc).__name__}"
        s.close()
        clock.now = due + 1
        latched = []
        with mock.patch.object(scheduling, "ZoneInfo", Gone):
            for _ in range(2):  # the second start shows the problem survives a restart
                again = Station(d, start_worker=True)
                try:
                    end = time.monotonic() + 3
                    while not again.faulted and time.monotonic() < end:
                        time.sleep(0.02)
                    latched.append(again.faulted)
                finally:
                    again.close()
        s.__dict__.setdefault("_closed", True)
    return Finding("F10", "One recurring program whose time zone cannot be resolved makes tick() raise a raw ValueError, and the clock thread then latches the whole station on every start", "medium",
                   seen.startswith("raw") and all(latched), f"tick with the zone unavailable: {seen}; clock thread latched the station on start 1 and 2: {latched}")


def f11_directory_descriptor_leak():
    with tempfile.TemporaryDirectory(prefix="finding-") as d:
        path = os.path.join(d, "x.wav")
        os.mkdir(path)
        before, held = fd_count(), open_descriptors()
        for _ in range(5):
            try:
                media_module.open_media(path, 1.0)
            except media_module.MediaError:
                pass
        leaked = fd_count() - before
        # The repro leaks on purpose, to show the defect; it must not leave the descriptors open in the process that ran it.  Only descriptors
        # that point at the directory used here are closed, so nothing that belongs to anyone else can be touched.
        close_descriptors_of(path, open_descriptors() - held)
    return Finding("F11", "open_media leaks one file descriptor each time the media path is a directory", "low", leaked > 0, f"descriptors leaked by 5 attempts: {leaked}")


def f12_transient_error_quarantine():
    with scratch() as (s, d, clock):
        t = song(s)
        s.action("station.update", {"auto_fill": False})
        s.action("queue.add", {"track_id": t})
        s.action("transport.start")
        with mock.patch.object(media_module, "os", Proxy(os, open=raiser(OSError(errno.EMFILE, "Too many open files")))):
            s.tick(clock.now)
        quarantined = s.db.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0]
        intact = (Path(d) / "media" / (t + ".wav")).stat().st_size == 44 + 4 * FRAMES * 3
        s.action("library.retry", {"track_id": t})
        cleared = s.db.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0] == 0
    return Finding("F12", "A momentary 'too many open files' while opening a healthy track quarantines it until someone retries it by hand", "low", quarantined == 1 and intact,
                   f"quarantined={quarantined} with an intact file; a manual retry cleared it: {cleared}")


def f13_ambiguous_commit_import():
    with scratch() as (s, _, _):
        probe = Probe()
        probe.arm("commit-after", 1, FAULTS["sqlite-io"])
        with injected(s, probe, {"commit-after"}):
            try:
                s.import_wav(Tapes.wav(8, FRAMES), "Saved then reported failed")
            except StationError:
                pass
        rows = s.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        files = len(os.listdir(s.media))
    return Finding("F13", "When an import's commit is saved but then reports an error, the cleanup removes the audio file and the saved library entry has no audio", "low", rows == 1 and files == 0,
                   f"library rows: {rows}, audio files on disk: {files}")


def f14_reset_prints_traceback():
    import http.client
    import io
    import secrets
    import socket
    from myradio.server import Server
    sink = io.StringIO()
    with scratch() as (s, _, _), contextlib.redirect_stderr(sink):
        token = secrets.token_urlsafe(32)
        with offline_agent_env():
            server = Server(("127.0.0.1", 0), s, token)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
            sock.sendall(f"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\nX-Myradio-Token: {token}\r\n\r\n".encode())
            time.sleep(0.3)  # the answer is now waiting unread in the client's buffer
            sock.close()  # closing with unread data sends a reset
            time.sleep(0.3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    text = sink.getvalue()
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("-")]
    return Finding("F14", "A client that resets an idle connection makes the server print a full traceback to stderr", "low",
                   "Exception occurred during processing of request" in text and "ConnectionResetError" in text,
                   f"lines printed to stderr: {len(lines)}; the last is: {lines[-1] if lines else 'nothing'}")


FINDINGS = (f1_queue_over_limit, f2_delete_orphan, f3_import_write_failure, f4_cancelled_growth, f5_schedule_view, f6_status_cost, f7_queue_id_overflow,
            f8_surrogate_import, f9_surrogate_name_breaks_status, f10_poison_program, f11_directory_descriptor_leak, f12_transient_error_quarantine, f13_ambiguous_commit_import, f14_reset_prints_traceback)


class FindingsResult:
    def __init__(self, items, errors):
        self.items, self.errors = items, errors

    @property
    def ok(self):
        return not self.errors

    def text(self):
        lines = ["Each repro runs against a throwaway station. REPRODUCED means the defect is present in the current engine.", ""]
        for f in self.items:
            lines += [f"{f.ident:4} {'REPRODUCED' if f.present else 'not reproduced':14} [{f.severity}] {f.title}", f"      {f.evidence}"]
        lines += [""] + [f"ERROR running {name}: {error}" for name, error in self.errors]
        lines.append(f"result: {'PASS' if self.ok else 'FAIL'} ({sum(f.present for f in self.items)} of {len(self.items)} reproduced)")
        return "\n".join(lines)


def run_findings(only=None):
    items, errors = [], []
    for fn in FINDINGS:
        if only and fn.__name__.split("_")[0].upper() not in only:
            continue
        try:
            items.append(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append((fn.__name__, f"{type(exc).__name__}: {exc}"))
    return FindingsResult(items, errors)
