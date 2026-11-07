"""The generate-ahead engine: supervisors, probes, buffer policy and the status snapshot.

Threads (all owned here, all joined by ``stop()``):

* one *slot* thread per allowed concurrent job of each resource group. A slot picks the next job under
  the buffer policy, runs the worker with its hard deadline, finishes the audio and imports it;
* one *maintenance* thread: cheap worker checks, expiry, library reconciliation and the status snapshot.

Core playout never calls in here. Station events arrive through ``on_event`` on the host's own mailbox
thread. ``status()`` returns a prebuilt snapshot: no lock, no I/O, no call into the station.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import audio_api
from .buffer import BufferPolicy, choose, level_value, unit_for
from .config import LoadedConfig, fingerprint
from .errors import Cancelled, GenerationError, WorkerError, message_for
from .payloads import KIND_FOR_ROLE, Limits, ROLES, ROLE_LABEL_PLURAL, SPEECH_ROLES, normalize_payload, window_for
from .runners import HttpRunner, SubprocessRunner
from .store import JobStore
from .util import free_bytes, iso, short
from .views import WORKER_LABELS, compact_job, job_view
from .workers_base import EVIDENCE, GenerationRequest, ProbeResult, Worker, WorkerContext

log = logging.getLogger("myradio.generation")

# The most one attempt may use in a maker. It is only a ceiling above every maker's own limit (a music program may be
# given up to two hours, plus up to one hour for a planning step); each maker applies its own, smaller limit.
MAX_ATTEMPT_SECONDS = 3 * 3600.0
SEED_STEP = 7919
RECONCILE_EVERY_S = 60.0
FAILURE_STREAK = 3
FULL_RETRY_BASE_S = 15.0       # after a test clip fails, wait this long before the engine tries again by itself ...
FULL_RETRY_CAP_S = 300.0       # ... doubling each time, but never more than this
# ... and after ``Worker.auto_test_limit`` failed test clips in a row (3 for the music maker, 5 for the others) it stops trying by itself:
# only a person's "Run a full test" makes another one, so a maker that cannot pass is not started again for ever on a shared card.
SUCCESS_WRITE_TRIES = 5        # how often to try writing down a finished job whose audio is already in the library
KEEP_TAKE_BYTES = 128 * 1024 * 1024  # the largest finished take that is kept in memory to retry only its import
PROVEN = ("test_clip_made", "real_job_completed")
FIXTURE_TITLE_PREFIX = "[FIXTURE] "
FIXTURE_ARTIST = "FIXTURE - not real music"
KIND_LABELS = {"drama_http": "Voice maker (Drama 3)", "speech_http": "Voice maker", "yue2_cli": "Music maker (YuE2)",
               "yue2_http": "Music maker (YuE2, kept loaded)", "fixture": "Test tones (not real audio)"}
EVIDENCE_LABELS = {"none": "Not checked yet", "setup_checked": "Set up and checked; nothing made yet",
                   "test_clip_made": "A short test was made", "real_job_completed": "Real work has been made",
                   "test_tones": "Test tones only; nothing real has been made"}
PUT_BACK_NOTE = "Put back in line because the maker was paused or the station stopped."
UNPROVEN_HINT = "Nothing has been made yet to prove it. Choose Run a full test to be sure."
WORK_PREFIXES = ("gen-", "probe-", "smoke-")
# Failures that say nothing about whether the maker itself is healthy:
NOT_WORKER_FAULT = frozenset({"cancelled", "worker_blocked", "worker_missing", "audio_tools_missing", "low_disk", "import_failed", "expired", "internal"})


def full_retry_delay(failures: int) -> float:
    """How long the engine waits before it tries a failed test clip again by itself: 15 s, 30 s, 60 s ... at most 5 minutes."""
    return min(FULL_RETRY_CAP_S, FULL_RETRY_BASE_S * 2 ** min(20, max(0, failures - 1)))  # 2 ** 20 is far past the cap; a huge count cannot make a huge number


def json_safe(value: Any) -> Any:
    """``value`` as plain JSON data (anything JSON cannot hold becomes text), so a stray value from a maker can never make a save fail."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError, RecursionError):
        return None


@dataclasses.dataclass
class WorkerState:
    state: str = "checking"
    message: str = "Checking. This takes a few seconds."
    hint: str = ""
    evidence: str = "none"
    version: Optional[str] = None
    hash: Optional[str] = None
    last_probe_at: Optional[float] = None
    last_ok_at: Optional[float] = None
    last_job_ok_at: Optional[float] = None
    needs_full: bool = False       # the last check says a real test clip is still needed before the maker counts as ready
    full_pending: bool = False     # a test clip should be made as soon as the machine is free and ``next_full_at`` has passed
    next_full_at: float = 0.0      # not before this time (clock seconds): the wait that follows a failed test clip
    full_failures: int = 0         # test clips that failed in a row (sets the length of that wait)
    test_failed: bool = False      # the last test clip failed and nothing has worked since: a cheap check cannot overrule that
    probing: bool = False
    probe_soon: bool = True
    failure_streak: int = 0
    last_error: Optional[str] = None
    last_error_code: Optional[str] = None
    probe_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


@dataclasses.dataclass
class Take:
    """A finished result that has not been imported yet (kept only so an import that failed can be tried again without making it again)."""
    finished: Any
    meta: dict
    worker_s: float
    finish_s: float
    seed: int


@dataclasses.dataclass
class RunningJob:
    job_id: str
    worker_id: str
    resource: str
    started: float
    cancel: threading.Event = dataclasses.field(default_factory=threading.Event)
    requeue: bool = False


class Engine:
    def __init__(self, ctx, cfg: LoadedConfig, kinds: Mapping[str, type], store: JobStore, *, clock: Callable[[], float],
                 http: Optional[HttpRunner] = None, proc: Optional[SubprocessRunner] = None):
        self.ctx = ctx
        self.cfg = cfg
        self.kinds = kinds
        self.store = store
        self.clock = clock
        self.work_root = Path(ctx.state_dir) / "work"
        self._http = http or HttpRunner()
        self._proc = proc or SubprocessRunner()
        self._cond = threading.Condition(threading.RLock())
        self._stopping = threading.Event()
        self._stopped = False
        self._started = False
        self._wake = threading.Event()
        self._snap_lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._threads: list = []
        self._running: dict = {}
        self._resource_loaded: dict = {name: None for name in cfg.resources}
        self._resource_running: dict = {name: 0 for name in cfg.resources}
        self.workers: dict = {}
        self.wstate: dict = {}
        self._paused = bool(store.get_setting("paused", False))
        overrides = dict(store.get_setting("buffer_overrides", {}) or {})
        self._buffer_bases = dict(store.get_setting("buffer_override_bases", {}) or {})
        self.policy = BufferPolicy(cfg.buffer, cfg.urgent_priority, self._drop_stale_overrides(overrides))
        self._imported: dict = {}   # job id -> (track id, provenance): the audio is in the library but the job is not written down as done yet
        self._kept: dict = {}       # job id -> Take: the one finished result held back after the library refused it
        self._limits = Limits(max_song_seconds=cfg.limits.max_song_seconds)
        self._snapshot_at: Optional[float] = None  # when the snapshot was built (epoch); kept off the public shape, which carries ISO times only
        self._snapshot: dict = {"enabled": True, "configured": False, "capabilities": self._pack(dict.fromkeys(ROLES, False)),
                                "proven": self._pack(dict.fromkeys(ROLES, False))}
        self._station_running: Optional[bool] = None
        self._last_reconcile = 0.0
        self._audio_ok: tuple = (True, "")
        self._audio_checked = 0.0
        self._config_changed = False
        self._recovery = {"requeued": 0, "interrupted": 0}

    def _drop_stale_overrides(self, overrides: dict) -> dict:
        """Marks set with ``set_buffer`` give way when the person later edits the same kind's marks in config.json: the file was
        changed after the override was made, so the newer decision wins. (An override saved by an older version cannot be compared and is kept.)"""
        stale = [role for role in overrides if role in self._buffer_bases and tuple(self._buffer_bases[role]) != tuple(self.cfg.buffer.get(role, ()))]
        if stale:
            for role in stale:
                overrides.pop(role, None)
                self._buffer_bases.pop(role, None)
            log.info("buffer marks set earlier were dropped because the settings file now has its own for: %s", ", ".join(stale))
            self.store.set_setting("buffer_overrides", overrides)
            self.store.set_setting("buffer_override_bases", self._buffer_bases)
        return overrides

    # ================================================================== lifecycle
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for entry in self.work_root.iterdir():
            if entry.name.startswith(WORK_PREFIXES):
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() and not entry.is_symlink() else entry.unlink(missing_ok=True)
        self._recovery = self.store.recover(self.clock(), self.cfg.retry.max_requeues)
        self.store.fail_unroutable(self._roles_with_a_maker_or_a_fixable_one(), self.clock())
        wctx = WorkerContext(audio_api.load_audio, self.ctx.read_private, self.clock, self._http, self._proc, self.work_root, self.cfg.finish,
                             max_song_s=float(self.cfg.limits.max_song_seconds))
        for spec in self.cfg.workers.values():
            self.wstate[spec.id] = WorkerState()
            try:
                self.workers[spec.id] = self.kinds[spec.kind](spec, wctx)
            except WorkerError as exc:
                self.wstate[spec.id].state, self.wstate[spec.id].message = "blocked", exc.message
                self.wstate[spec.id].probe_soon = False
            except Exception as exc:
                log.warning("worker %s could not be created: %s", spec.id, type(exc).__name__)
                self.wstate[spec.id].state = "failed"
                self.wstate[spec.id].message = "This maker could not be started. Check its settings."
                self.wstate[spec.id].probe_soon = False
        for rejected in self.cfg.rejected:
            self.wstate[rejected.id] = WorkerState(state="misconfigured", message=rejected.message, probe_soon=False,
                                                   hint="Fix this entry in the generation settings, then choose Check again.")
        self._check_audio()
        self._publish()
        for name, resource in self.cfg.resources.items():
            for index in range(resource.max_concurrent):
                self._spawn(f"myradio-gen-slot-{name}-{index}", self._slot_loop, name)
        self._spawn("myradio-gen-maint", self._maintenance_loop)

    def _roles_with_a_maker_or_a_fixable_one(self) -> list:
        """Kinds of audio whose waiting jobs may stay in line: they have a maker, or their maker's settings merely have a problem
        (a typo must not throw away the queue). Only a kind with no maker at all, written or attempted, is given up on."""
        keep = {role for spec in self.cfg.workers.values() for role in spec.roles}
        for off in self.cfg.disabled:  # switched off on purpose: its jobs wait until it is switched on again
            keep |= set(off.roles)
        for rejected in self.cfg.rejected:
            cls = self.kinds.get(rejected.kind)
            keep |= set(cls.default_roles) if cls is not None else set(ROLES)
        if self.cfg.fingerprint == "unreadable":
            keep = set(ROLES)
        return sorted(keep)

    def _spawn(self, name: str, target: Callable, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def stop(self, *, close_store: bool = True, timeout: float = 20.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stopping.set()
        with self._cond:
            for running in self._running.values():
                running.requeue = True
                running.cancel.set()
            self._cond.notify_all()
        self._wake.set()
        end = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.1, end - time.monotonic()))
        self._kept.clear()
        if close_store:
            self.store.close()

    def poke(self) -> None:
        with self._cond:
            self._cond.notify_all()
        self._wake.set()

    def alive_threads(self) -> list:
        return [t.name for t in self._threads if t.is_alive()]

    # ================================================================== operations
    def _spec_for(self, role: str):
        spec = self.cfg.worker_for(role)
        if spec is not None:
            return spec
        noun = "music maker" if role == "song" else "voice maker"
        switched_off = [d for d in self.cfg.disabled if role in d.roles]
        if switched_off:
            raise GenerationError(f"The {noun} is switched off in the generation settings, so {ROLE_LABEL_PLURAL[role]} cannot be made. "
                                  f"Switch it on there, then choose Check again.")
        broken = [r for r in self.cfg.rejected if role in getattr(self.kinds.get(r.kind), "default_roles", ())]
        if broken:
            raise GenerationError(f"The {noun} has a problem in its settings, so nothing can be made yet: {short(broken[0].message, 200)}")
        raise GenerationError(f"No {noun} is set up yet, so {ROLE_LABEL_PLURAL[role]} cannot be made. Add one in the generation settings.")

    def enqueue(self, role: str, payload: Any, priority: int = 50, ttl_s: Optional[int] = None, key: Optional[str] = None) -> tuple:
        norm = normalize_payload(role, payload, self._limits)
        spec = self._spec_for(role)
        seed = norm.data["seed"] if "seed" in norm.data else secrets.randbits(32)
        job, created = self.store.enqueue(norm, priority=priority, ttl_s=ttl_s, key=key, resource=spec.resource, worker_id=spec.id,
                                          max_attempts=self.cfg.retry.max_attempts, seed=seed, max_live=self.cfg.limits.max_queued,
                                          fixture=bool(spec.fixture))
        if created:
            self.poke()
            self._publish()
        return self._view(job), created

    def get_job(self, job_id: str) -> Optional[dict]:
        job = self.store.get(job_id)
        return self._view(job, detail=True) if job else None

    def job_for_track(self, track_id: str) -> Optional[dict]:
        job = self.store.get_by_track(track_id)
        return self._view(job, detail=True) if job else None

    def list_jobs(self, **kw) -> list:
        return [self._view(j) for j in self.store.list_jobs(**kw)]

    def cancel(self, job_id: str, wait_s: float = 3.0) -> tuple:
        job = self.store.get(job_id)
        if job is None:
            raise GenerationError("That job was not found. It may be old enough to have been cleared from the history.")
        if job["state"] == "queued" and self.store.cancel_queued(job_id, self.clock()):
            self._kept.pop(job_id, None)
            self._publish()
            return self._view(self.store.get(job_id)), True
        job = self.store.get(job_id)
        if job["state"] != "running":
            raise GenerationError("That job has already finished, so there is nothing to cancel." if job["state"] != "queued"
                                  else "That job just started; try cancelling again.")
        self.store.request_cancel(job_id)
        with self._cond:
            running = self._running.get(job_id)
            if running is not None:
                running.requeue = False  # the operator's wish to cancel wins over being put back in line
                running.cancel.set()
        return self.settle(job_id, wait_s)

    def settle(self, job_id: str, wait_s: float = 3.0) -> tuple:
        """Wait up to ``wait_s`` for a job that was asked to stop to leave ``running``. Returns ``(view, settled)``."""
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            job = self.store.get(job_id)
            if job is None or job["state"] != "running":
                break
            time.sleep(0.02)
        job = self.store.get(job_id)
        self._publish()
        return self._view(job), job is not None and job["state"] != "running"

    def retry(self, job_id: str) -> dict:
        found = self.store.get(job_id)
        if found is not None:
            self._spec_for(found["role"])  # say so plainly if its maker has been removed from the settings
        job = self.store.retry(job_id, self.clock())
        self.poke()
        self._publish()
        return self._view(job)

    def pause(self, cancel_running: bool = False) -> dict:
        with self._cond:
            self._paused = True
            stopped = 0
            if cancel_running:
                for running in self._running.values():
                    running.requeue = True
                    running.cancel.set()
                    stopped += 1
        self.store.set_setting("paused", True)
        self._publish()
        return {"paused": True, "stopping_now": stopped, "running": len(self._running)}

    def resume(self) -> dict:
        with self._cond:
            self._paused = False
            self._cond.notify_all()
        self.store.set_setting("paused", False)
        self._publish()
        return {"paused": False}

    @property
    def paused(self) -> bool:
        return self._paused

    def set_buffer(self, role: str, low: int, high: int) -> dict:
        self.policy.set_marks(role, low, high)
        self._buffer_bases[role] = list(self.cfg.buffer.get(role, ()))  # what config.json said when this was set, to notice a later edit of it
        self.store.set_setting("buffer_overrides", self.policy.overrides())
        self.store.set_setting("buffer_override_bases", self._buffer_bases)
        self.poke()
        self._publish()
        return self.buffer_view()["roles"][role]

    # ================================================================== scheduling
    def _slot_loop(self, resource: str) -> None:
        while not self._stopping.is_set():
            try:
                task = self._take_task(resource)
                if task is None:
                    continue
                kind, item = task
                if kind == "job":
                    self._run_job(item, resource)
                else:
                    self._run_full_probe(item)
            except Exception:  # a bug must never kill the slot; pause briefly so a persistent one cannot spin
                log.exception("generation slot error")
                self._stopping.wait(1.0)

    def _take_task(self, resource: str) -> Optional[tuple]:
        with self._cond:
            while not self._stopping.is_set():
                now = self.clock()
                if not self._paused:
                    if self.store.expire_due(now):
                        self._wake.set()
                    worker_id = self._full_probe_due(resource, now)
                    if worker_id is not None:
                        self.wstate[worker_id].probing = True
                        return "probe", worker_id
                    job = self._pick_job(resource, now)
                    if job is not None:
                        return "job", job
                wake = self.store.next_wake(self._roles_of(resource), now)
                later = self._next_full_check(resource, now)
                if later is not None:
                    wake = later if wake is None else min(wake, later)
                self._cond.wait(2.0 if wake is None else min(2.0, max(0.02, wake - now)))
        return None

    def _full_probe_due(self, resource: str, now: float) -> Optional[str]:
        """A maker on this resource whose test clip is wanted and whose wait (after an earlier failed one) is over."""
        for worker_id, worker in self.workers.items():
            state = self.wstate[worker_id]
            if worker.resource == resource and state.full_pending and not state.probing and now >= state.next_full_at:
                return worker_id
        return None

    def _next_full_check(self, resource: str, now: float) -> Optional[float]:
        """When the next test clip that is wanted but still waiting becomes due (so an idle slot wakes for it)."""
        times = [state.next_full_at for worker_id, worker in self.workers.items() if worker.resource == resource
                 for state in (self.wstate[worker_id],) if state.full_pending and not state.probing and state.next_full_at > now]
        return min(times) if times else None

    def _derive_seed(self, job: Mapping) -> int:
        if job["seed_pinned"]:
            return job["seed"]
        return (job["seed"] + SEED_STEP * job["attempts"]) % (2 ** 32)

    def _roles_of(self, resource: str) -> list:
        """The kinds of audio whose maker runs on this resource group (jobs follow the current settings)."""
        return [role for spec in self.cfg.workers.values() if spec.resource == resource for role in spec.roles]

    def running_count(self) -> int:
        """Jobs being made, plus finished ones whose audio is already in the library but that are not written down as done yet:
        the settings must not be swapped under either."""
        return len(self._running) + len(self._imported)

    def _ready_level(self, role: str) -> dict:
        """What is made and waiting for ``role``. Test tones count only while the maker for the role is itself a test maker:
        leftovers from one must not hold back a real maker that took its place."""
        spec = self.cfg.worker_for(role)
        return self.store.ready_level(role, include_fixture=bool(spec is not None and spec.fixture))

    def _pick_job(self, resource: str, now: float) -> Optional[dict]:
        roles = list(dict.fromkeys(self._roles_of(resource)))
        if not roles:
            return None
        # Decide which kinds may run *first*, and ask the queue only for those (and for urgent jobs). If the queue were read
        # first, a long line of waiting jobs that the buffer will not run could fill the whole window and hide one it would run.
        filling = {role: self.policy.update(role, level_value(role, self._ready_level(role))) for role in roles}
        candidates = self.store.candidates(roles, now, allowed=[role for role in roles if filling[role]], urgent_priority=self.cfg.urgent_priority)
        if not candidates:
            return None
        spec = self.cfg.resources[resource]
        chosen = choose(candidates, now=now, loaded_worker=self._resource_loaded.get(resource), switch_after_s=spec.switch_after_s,
                        allowed=lambda j: self.policy.allows(j["role"], j["priority"], filling[j["role"]]), urgent_priority=self.cfg.urgent_priority)
        if chosen is None:
            return None
        worker_spec = self.cfg.worker_for(chosen["role"])
        worker_id = worker_spec.id if worker_spec else chosen["worker_id"]
        started = self.store.start(chosen["id"], now, worker_id=worker_id, resource=resource,
                                   seed_used=self._derive_seed({**chosen, "attempts": chosen["attempts"]}),
                                   fixture=bool(worker_spec.fixture) if worker_spec else None)
        if started is None:
            return None
        self._running[started["id"]] = RunningJob(started["id"], worker_id, resource, now)
        self._resource_running[resource] = self._resource_running.get(resource, 0) + 1
        self._resource_loaded[resource] = worker_id
        return started

    # ================================================================== running one job
    def _check_disk(self) -> None:
        free = free_bytes(self.work_root)
        if free is not None and free < self.cfg.limits.min_free_disk_mb * 1024 * 1024:
            raise WorkerError("low_disk", message_for("low_disk"), detail=f"{free // (1024 * 1024)} MB free")

    def _finish_slot(self, running: RunningJob) -> bool:
        while not self._finish_lock.acquire(timeout=0.2):
            if running.cancel.is_set():
                raise Cancelled()
        return True

    def _run_job(self, job: dict, resource: str) -> None:
        job_id = job["id"]
        running = self._running[job_id]
        self._publish()
        work_dir: Optional[Path] = None
        try:
            state = self.wstate.get(running.worker_id)
            if state is not None and state.state == "blocked":
                raise WorkerError("worker_blocked", message_for("worker_blocked"), retryable=False)
            worker = self.workers.get(running.worker_id)
            if worker is None:
                raise WorkerError("worker_missing", message_for("worker_missing"), retryable=False, detail=f"no worker {running.worker_id}")
            self._check_disk()
            audio = audio_api.load_audio()
            take = self._kept.pop(job_id, None)  # a finished result the library refused last time: only the import is left to do
            if take is None:
                try:
                    work_dir = Path(tempfile.mkdtemp(prefix="gen-", dir=self.work_root))
                except OSError:
                    raise WorkerError("low_disk", message_for("low_disk"), detail="could not make a working folder") from None
                min_s, max_s = window_for(job["role"], job["payload"])
                seed = self._derive_seed({**job, "attempts": job["attempts"] - 1})
                request = GenerationRequest(job_id, job["role"], job["payload"], seed, job["attempts"], MAX_ATTEMPT_SECONDS, min_s, max_s,
                                            running.cancel, work_dir)
                began = time.monotonic()
                output = worker.generate(request)
                worker_s = time.monotonic() - began
                pcm, meta = output.pcm, output.meta
                del output
                if running.cancel.is_set():
                    raise Cancelled()
            self._finish_slot(running)
            try:
                if take is None:
                    mark = time.monotonic()
                    finished = audio_api.finish(audio, pcm, role=job["role"], min_s=min_s, max_s=max_s, spec=self.cfg.finish,
                                                cancel=running.cancel)
                    pcm = None  # the raw result is no longer needed; free it before the import
                    take = Take(finished, meta, worker_s, time.monotonic() - mark, seed)
                if running.cancel.is_set():
                    raise Cancelled()
                now = self.clock()
                if job.get("expires_at") is not None and now > job["expires_at"]:
                    self.store.finish_expired(job_id, now)
                    self._after_finish(running, None)
                    return
                mark = time.monotonic()
                try:
                    track = self._import(take.finished, job, worker)
                except WorkerError as exc:
                    if exc.retryable and len(take.finished.wav) <= KEEP_TAKE_BYTES:
                        with self._cond:
                            self._kept.clear()  # one finished result at a time is held: it is large
                            self._kept[job_id] = take
                    raise
                import_s = time.monotonic() - mark
            finally:
                self._finish_lock.release()
            self._settle_imported(job, running, worker, take, track, import_s)
        except Cancelled:
            self.store.finish_cancelled(job_id, self.clock(), requeue=running.requeue, note=PUT_BACK_NOTE if running.requeue else "")
            self._after_finish(running, None)
        except WorkerError as exc:
            self._handle_failure(job, running, exc)
        except Exception as exc:
            log.exception("job %s failed unexpectedly", job_id)
            self._handle_failure(job, running, WorkerError("internal", message_for("internal"), detail=f"{type(exc).__name__}: {exc}"[:300]))
        finally:
            with self._cond:
                self._running.pop(job_id, None)
                self._resource_running[resource] = max(0, self._resource_running.get(resource, 1) - 1)
                self._cond.notify_all()
            self._publish()
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)

    def _import(self, finished, job: Mapping, worker: Worker) -> dict:
        title = job["title"]
        artist = self.cfg.artist
        if worker.fixture:
            title, artist = (FIXTURE_TITLE_PREFIX + title)[:200], FIXTURE_ARTIST
        try:
            track = self.ctx.station.import_audio(finished.wav, title, artist, KIND_FOR_ROLE[job["role"]])
        except Exception as exc:
            raise WorkerError("import_failed", message_for("import_failed"), detail=f"{type(exc).__name__}: {exc}"[:300]) from None
        if not isinstance(track, dict) or not isinstance(track.get("id"), str):
            raise WorkerError("import_failed", message_for("import_failed"), detail="the station did not return a track")
        return {"id": track["id"], "title": track.get("title") or title}

    def _provenance(self, job: Mapping, worker: Worker, take: Take, track: Mapping, import_s: float) -> dict:
        finished, identity = take.finished, worker.identity()
        timings = {"queue_wait_s": round(max(0.0, (job.get("started_at") or 0) - job["created_at"]), 2), "worker_s": round(take.worker_s, 2),
                   "finish_s": round(take.finish_s, 2), "finish_steps": finished.steps_s, "import_s": round(import_s, 2),
                   "audio_s": round(finished.duration_s, 2), "realtime_x": round(finished.duration_s / take.worker_s, 2) if take.worker_s > 0 else None,
                   "levels": finished.levels, "worker_meta": take.meta}
        prov = {"role": job["role"], "kind": KIND_FOR_ROLE[job["role"]], "title": track["title"], "worker_id": worker.id,
                "worker_kind": worker.kind, "worker_version": identity.get("version"), "worker_hash": identity.get("hash"),
                "fixture": bool(worker.fixture), "prompt_id": job["payload"].get("prompt_id"), "seed": take.seed, "params": job["payload"],
                "timings": timings, "attempts": job["attempts"], "validation": finished.validation,
                "audio_sha256": finished.sha256, "duration_s": finished.duration_s}
        safe = json_safe(prov)
        if safe is None:  # something a maker returned cannot be written down: keep everything else
            timings["worker_meta"] = {}
            safe = json_safe({**prov, "timings": timings})
        if safe is None:
            raise ValueError("the provenance cannot be written as JSON")  # the caller keeps a short record instead
        return safe

    def _settle_imported(self, job: Mapping, running: RunningJob, worker: Worker, take: Take, track: Mapping, import_s: float) -> None:
        """The audio is in the library. From here nothing may lead to making it again: writing the job down as done is
        retried, and if it still cannot be written the engine keeps trying (see ``_reap_orphans``) instead of making a second copy."""
        try:
            prov = self._provenance(job, worker, take, track, import_s)
        except Exception:
            log.exception("job %s: could not build its provenance", job["id"])
            prov = {"role": job["role"], "kind": KIND_FOR_ROLE[job["role"]], "title": track["title"], "worker_id": worker.id, "worker_kind": worker.kind,
                    "fixture": bool(worker.fixture), "seed": take.seed, "attempts": job["attempts"], "duration_s": float(take.finished.duration_s),
                    "audio_sha256": take.finished.sha256}
        self._imported[job["id"]] = (track["id"], prov)
        if self._record_success(job["id"], track["id"], prov, SUCCESS_WRITE_TRIES):
            self._after_finish(running, None, success=True)

    def _record_success(self, job_id: str, track_id: str, prov: dict, tries: int) -> bool:
        for attempt in range(tries):
            try:
                self.store.finish_success(job_id, self.clock(), track_id, prov)
            except GenerationError:
                return False  # the store is closed (the station is stopping): nothing more can be written
            except Exception:
                log.warning("job %s: could not write down that it finished (try %s of %s)", job_id, attempt + 1, tries, exc_info=True)
                if attempt + 1 < tries and self._stopping.wait(0.2 * (attempt + 1)):
                    return False
                continue
            self._imported.pop(job_id, None)
            return True
        log.error("job %s: its audio is in the library as track %s but the job could not be written down as done; will keep trying", job_id, track_id)
        return False

    def _backoff(self, attempts: int) -> float:
        retry = self.cfg.retry
        return min(retry.cap_s, retry.base_s * (2 ** max(0, attempts - 1)))

    def _handle_failure(self, job: Mapping, running: RunningJob, exc: WorkerError) -> None:
        now = self.clock()
        if running.cancel.is_set():
            # A cancel, a pause that stops running work, or a stop arrived while the attempt was failing. What the person
            # asked for wins over a retry: cancelled for good, or put back in line without using up an attempt.
            self.store.finish_cancelled(job["id"], now, requeue=running.requeue, note=PUT_BACK_NOTE if running.requeue else "")
            if not running.requeue:
                self._kept.pop(job["id"], None)
            self._after_finish(running, None)
            return
        attempts = job["attempts"]
        retry_at = now + self._backoff(attempts) if exc.retryable and attempts < job["max_attempts"] else None
        state = self.store.finish_failure(job["id"], now, code=exc.code, message=exc.message, detail=exc.detail, retry_at=retry_at)
        if state != "queued":
            self._kept.pop(job["id"], None)  # no automatic retry is left, so the held result is not needed
        log.info("job %s attempt %s failed: %s (%s)", job["id"], attempts, exc.code, state)
        self._after_finish(running, exc)

    def _after_finish(self, running: RunningJob, exc: Optional[WorkerError], success: bool = False) -> None:
        state = self.wstate.get(running.worker_id)
        if state is None:
            return
        with self._cond:
            if success:
                worker = self.workers.get(running.worker_id)
                state.failure_streak = 0
                state.test_failed = False
                state.last_job_ok_at = self.clock()
                state.evidence = "test_tones" if worker is not None and worker.fixture else "real_job_completed"
                state.last_error = state.last_error_code = None
                if state.state in ("warming", "unreachable", "failed"):  # real work succeeded, so older probe news is out of date
                    state.state, state.message, state.hint = "ready", "Ready.", ""
            elif exc is not None and exc.code not in NOT_WORKER_FAULT:
                state.failure_streak += 1
                state.last_error, state.last_error_code = exc.message, exc.code
                if exc.code == "worker_unreachable":
                    state.state, state.message = "unreachable", exc.message
                    state.hint = "Start the maker, then choose Check again."
                    state.probe_soon = True
                    state.full_pending = False
                    state.test_failed = False
                    state.full_failures, state.next_full_at = 0, 0.0  # it was away: the first test after it is back should not wait
                    state.evidence = "none"
                    worker = self.workers.get(running.worker_id)
                    if worker is not None:
                        worker.invalidate()
        self._wake.set()

    # ================================================================== probes
    def _run_full_probe(self, worker_id: str) -> None:
        worker = self.workers[worker_id]
        state = self.wstate[worker_id]
        try:
            with state.probe_lock:
                self._probe(worker, True)
        finally:
            state.probing = False
            self.poke()
            self._publish()

    def _probe(self, worker: Worker, full: bool, cancel: Optional[threading.Event] = None) -> ProbeResult:
        cancel = cancel or self._stopping
        try:
            result = worker.probe(full, cancel)
        except Cancelled:
            result = ProbeResult(False, "warming", "The check was interrupted.", needs_full=full)
        except WorkerError as exc:
            result = ProbeResult(False, "failed", exc.message, "Check the maker's settings, then choose Check again.", detail=exc.detail)
        except Exception as exc:
            log.warning("probe of %s failed: %s", worker.id, type(exc).__name__)
            result = ProbeResult(False, "failed", "The check could not be completed.", "Check the maker's settings, then choose Check again.",
                                 detail=type(exc).__name__)
        self._apply_probe(worker, result, full)
        return result

    def _apply_probe(self, worker: Worker, result: ProbeResult, full: bool) -> None:
        state = self.wstate[worker.id]
        now = self.clock()
        with self._cond:
            state.last_probe_at = now
            state.probe_soon = False
            state.needs_full = bool(result.needs_full and not result.ok)
            if full:
                state.full_pending = False
                if result.ok:
                    state.full_failures, state.next_full_at = 0, 0.0
                    state.test_failed = False
                    state.failure_streak = 0
                    state.last_error = state.last_error_code = None
                else:
                    # A test clip that failed is not tried again at once: the wait doubles each time (15 s up to 5 minutes), so a maker
                    # that answers but cannot finish is not hammered, and jobs for the other makers in its group keep their turn.
                    state.full_failures += 1
                    state.next_full_at = now + full_retry_delay(state.full_failures)
                    state.full_pending = state.needs_full and self._auto_tests_left(worker, state)
                    if result.state not in ("unreachable", "missing_files", "blocked"):
                        state.test_failed = True
                        state.last_error, state.last_error_code = result.message, "test_failed"
            elif result.needs_full and not result.ok:
                state.full_pending = self._auto_tests_left(worker, state)
            elif not result.ok:
                state.full_pending = False
            elif state.test_failed or state.failure_streak >= FAILURE_STREAK:
                # it passes the cheap check but has real trouble on record: prove that it works again (unless it already failed too often)
                state.full_pending = self._auto_tests_left(worker, state)
            if result.ok:
                state.state, state.last_ok_at = "ready", now
                state.message = result.message
                state.hint = ""
                if state.last_job_ok_at is not None:
                    state.evidence = "test_tones" if worker.fixture else "real_job_completed"
                else:
                    state.evidence = result.evidence if result.evidence in EVIDENCE else "none"
            else:
                state.state, state.message, state.hint = result.state, result.message, result.hint
                state.evidence = result.evidence if result.evidence in EVIDENCE else "none"
            if result.version:
                state.version = result.version
            identity = worker.identity()
            state.hash = identity.get("hash") or state.hash
            if state.full_pending:
                self._cond.notify_all()
        self._wake.set()

    @staticmethod
    def _auto_tests_left(worker: Worker, state: WorkerState) -> bool:
        """True while the engine may still make a test clip by itself: fewer than ``worker.auto_test_limit`` failed in a row.
        After that only a person's "Run a full test" makes one (a test song is a whole model run on a shared card)."""
        return state.full_failures < max(1, int(getattr(worker, "auto_test_limit", 5)))

    def probe(self, *, full: bool = False) -> list:
        """Check every worker now and return the workers view. See :meth:`request_probe`."""
        return self.request_probe(full=full)["workers"]

    def request_probe(self, *, full: bool = False) -> dict:
        """Check every worker now. The quick check runs here. With ``full`` a real test clip is also queued for every maker that
        is answering and not busy; it runs when the machine is free, at once, whatever wait followed an earlier failed test.
        Returns ``{"workers": [...], "full_check_queued": bool, "full_check_workers": [ids]}``: ``full_check_queued`` is true
        only when a test clip is queued or already being made."""
        queued = []
        for worker_id, worker in list(self.workers.items()):
            state = self.wstate[worker_id]
            if state.probing:
                if full:
                    queued.append(worker_id)  # its test clip is being made right now
                continue
            if self._busy(worker_id):
                continue  # it is busy making something, which is the best proof it works; do not interrupt it
            if state.probe_lock.acquire(blocking=False):
                try:
                    worker.refresh()
                    result = self._probe(worker, False)
                finally:
                    state.probe_lock.release()
                if full and result.state not in ("unreachable", "missing_files", "blocked"):
                    with self._cond:
                        state.full_pending = True
                        state.next_full_at = 0.0  # asked for by a person: not held back by the wait that follows a failed test
                    queued.append(worker_id)
        self._check_config_file()  # so the answer to "did the settings change?" is the same here and in the status
        if full:
            self.poke()
        self._publish()
        return {"workers": self.workers_view(), "full_check_queued": bool(queued), "full_check_workers": queued}

    def _busy(self, worker_id: str) -> bool:
        """True while a job is being made by this worker. A single-lane maker may not answer other requests meanwhile."""
        return any(r.worker_id == worker_id for r in list(self._running.values()))

    def _cheap_probes(self) -> None:
        now = self.clock()
        interval = self.cfg.limits.probe_interval_s
        for worker_id, worker in list(self.workers.items()):
            state = self.wstate[worker_id]
            if state.state == "blocked" or state.probing or self._busy(worker_id):
                continue
            retry_after = min(interval, 15.0) if state.state in ("unreachable", "failed", "missing_files") else interval
            due = state.probe_soon or state.last_probe_at is None or now - state.last_probe_at >= retry_after
            if due and state.probe_lock.acquire(blocking=False):
                try:
                    self._probe(worker, False)
                finally:
                    state.probe_lock.release()
            if self._stopping.is_set():
                return

    # ================================================================== maintenance
    def _maintenance_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if self.store.expire_due(self.clock()):
                    self.poke()
                self._reap_orphans()
                self._cheap_probes()
                self._check_audio()
                self._reconcile()
                self._check_config_file()
                self._publish()
            except GenerationError:
                return  # the store is closed: shutting down
            except Exception:
                log.exception("generation maintenance error")
            self._wake.wait(1.0)
            self._wake.clear()

    def _reap_orphans(self) -> None:
        """A job the database calls running but no thread is running (a failed write at the wrong moment) goes back in line,
        unless its audio is already in the library: then the job is written down as done instead, never made a second time."""
        with self._cond:
            stuck = [j for j in self.store.running_jobs() if j["id"] not in self._running]
            now = self.clock()
            for job in stuck:
                held = self._imported.get(job["id"])
                if held is not None:
                    if self._record_success(job["id"], held[0], held[1], 1):
                        self._after_finish(RunningJob(job["id"], job["worker_id"], job["resource"], now), None, success=True)
                    continue
                self.store.finish_cancelled(job["id"], now, requeue=True, note="Put back in line after an unexpected problem.")
            alive = {j["id"] for j in stuck}
            for job_id in [i for i in self._imported if i not in alive and i not in self._running]:
                self._imported.pop(job_id, None)  # the job is gone or finished some other way: nothing left to write down
            for job_id in list(self._kept):
                job = self.store.get(job_id)
                if job is None or job["state"] not in ("queued", "running"):
                    self._kept.pop(job_id, None)  # the job it was held for was cancelled, expired or finished
        if stuck:
            self.poke()

    def _check_audio(self) -> None:
        now = time.monotonic()
        if now - self._audio_checked >= 30.0:
            self._audio_checked = now
            self._audio_ok = audio_api.audio_available()

    def _reconcile(self) -> None:
        """Forget ready tracks that were deleted from the library, so they do not count as audio on hand."""
        now = time.monotonic()
        if now - self._last_reconcile < RECONCILE_EVERY_S:
            return
        self._last_reconcile = now
        ids = self.store.ready_track_ids()
        if not ids:
            return
        try:
            library = self.ctx.station.status().get("library")
        except Exception:
            return
        if not isinstance(library, list):
            return
        present = {t.get("id") for t in library if isinstance(t, dict) and t.get("available", True)}
        gone = [i for i in ids if i not in present]
        if gone and self.store.mark_gone(gone):
            self.poke()

    def _check_config_file(self) -> None:
        """Notice when config.json no longer matches what was loaded, so the screen can say "check again"."""
        try:
            current = self.ctx.config()
        except Exception:
            self._config_changed = True
            return
        self._config_changed = fingerprint(current) != self.cfg.fingerprint

    # ================================================================== events
    def on_event(self, event: Mapping) -> None:
        kind = event.get("type")
        if kind in ("track_started", "track_finished"):
            track = event.get("track")
            track_id = track.get("id") if isinstance(track, dict) else None
            at = event.get("at")
            try:
                if isinstance(track_id, str) and self.store.mark_aired(track_id, float(at) if isinstance(at, (int, float)) else self.clock()):
                    self.poke()
            except GenerationError:
                pass  # shutting down
        elif kind == "transport":
            self._station_running = event.get("state") == "started"

    # ================================================================== views and status
    def _job_is_fixture(self, job: Mapping) -> bool:
        """Whether the job's audio is (or, while it waits, will be) a test tone. A job that has started keeps the flag of the maker that
        ran it, so it stays true after the test maker is taken out of the settings; a waiting job follows the maker that will run it."""
        if job["state"] == "queued":
            spec = self.cfg.worker_for(job["role"])
            if spec is not None:
                return bool(spec.fixture)
        return bool(job.get("fixture"))

    def _view(self, job: Mapping, detail: bool = False) -> dict:
        prov = self.store.provenance_for_job(job["id"]) if detail and job["state"] == "succeeded" else None
        return job_view(job, detail=detail, fixture=self._job_is_fixture(job), provenance=prov)

    def _effective(self, state: WorkerState) -> str:
        """``degraded``: it passes the cheap check, but real attempts or its last test clip failed and nothing has worked since."""
        if state.state == "ready" and (state.failure_streak >= FAILURE_STREAK or state.test_failed):
            return "degraded"
        return state.state

    def _worker_entry(self, worker_id: str, spec) -> dict:
        state = self.wstate.get(worker_id) or WorkerState(state="checking")
        worker = self.workers.get(worker_id)
        effective = self._effective(state)
        message, hint = state.message, state.hint
        if effective == "degraded":
            if state.failure_streak >= FAILURE_STREAK:
                message = "The last few attempts failed. " + (state.last_error or "")
            else:
                message = "The last test did not work. " + (state.last_error or "")
            hint = "Look at the failed jobs for the reason. When it is fixed, choose Run a full test to prove that it works again."
        if worker is not None and state.full_failures and not self._auto_tests_left(worker, state):
            hint = (hint + " " if hint else "") + (f"{state.full_failures} test clips failed in a row, so it has stopped making them by itself; "
                                                  "only Run a full test makes another.")
        return {
            "id": worker_id, "kind": spec.kind, "kind_label": KIND_LABELS.get(spec.kind, spec.kind), "roles": list(spec.roles),
            "resource": spec.resource, "state": effective, "state_label": WORKER_LABELS.get(effective, effective), "message": message.strip(),
            "hint": hint, "ready": effective == "ready", "evidence": state.evidence, "evidence_label": EVIDENCE_LABELS[state.evidence],
            "fixture": bool(spec.fixture), "version": state.version, "runtime_sha256": state.hash,
            "last_checked": iso(state.last_probe_at), "last_ok": iso(state.last_ok_at), "last_work_ok": iso(state.last_job_ok_at),
            "recent_failures": state.failure_streak, "last_error": state.last_error, "warm": bool(worker.warm) if worker else False,
            "details": worker.describe() if worker else {},
        }

    def workers_view(self) -> list:
        out = [self._worker_entry(spec.id, spec) for spec in self.cfg.workers.values()]
        for off in self.cfg.disabled:
            out.append({"id": off.id, "kind": off.kind, "kind_label": KIND_LABELS.get(off.kind, off.kind), "roles": list(off.roles),
                        "resource": None, "state": "blocked", "state_label": WORKER_LABELS["blocked"],
                        "message": "This maker is switched off in the generation settings.",
                        "hint": 'To use it, set "enabled" to true for it in the settings, then choose Check again.', "ready": False,
                        "evidence": "none", "evidence_label": EVIDENCE_LABELS["none"], "fixture": False, "version": None, "runtime_sha256": None,
                        "last_checked": None, "last_ok": None, "last_work_ok": None, "recent_failures": 0, "last_error": None, "warm": False,
                        "details": {}})
        for rejected in self.cfg.rejected:
            state = self.wstate.get(rejected.id) or WorkerState(state="misconfigured", message=rejected.message)
            out.append({"id": rejected.id, "kind": rejected.kind, "kind_label": KIND_LABELS.get(rejected.kind, rejected.kind), "roles": [],
                        "resource": None, "state": "misconfigured", "state_label": WORKER_LABELS["misconfigured"], "message": rejected.message,
                        "hint": state.hint, "ready": False, "evidence": "none", "evidence_label": EVIDENCE_LABELS["none"], "fixture": False,
                        "version": None, "runtime_sha256": None, "last_checked": None, "last_ok": None, "last_work_ok": None,
                        "recent_failures": 0, "last_error": None, "warm": False, "details": {}})
        return out

    def buffer_view(self) -> dict:
        live = self.store.live_by_role()
        roles = {}
        for role in ROLES:
            low, high = self.policy.marks(role)
            level = self._ready_level(role)
            ready = level_value(role, level)
            filling = self.policy.update(role, ready)
            pending = live[role]["pending_seconds"] if role == "song" else live[role]["queued"] + live[role]["running"]
            roles[role] = {
                "label": ROLE_LABEL_PLURAL[role], "unit": unit_for(role), "ready": round(ready, 1) if role == "song" else int(ready),
                "ready_items": level["items"],
                "ready_seconds": round(level["seconds"], 1), "low": low, "high": high, "marks_from": self.policy.marks_from(role),
                "filling": bool(filling),
                "queued": live[role]["queued"], "running": live[role]["running"], "pending": round(pending, 1) if role == "song" else int(pending),
                "wanted": (round(max(0.0, high - ready - pending), 1) if role == "song" else int(max(0, high - ready - pending))) if filling else 0,
                "oldest_ready": iso(level["oldest"]),
            }
        return {"roles": roles, "paused": self._paused, "urgent_priority": self.policy.urgent_priority, "overrides": self.policy.overrides()}

    def _role_flags(self) -> tuple:
        """``(roles, proven)`` for each kind of audio. ``roles`` is true only for a real (non-fixture) maker whose last check passed and
        that has not failed lately, and only while the audio tools needed to finish its work exist. ``proven`` is true only when,
        on top of that, the maker has really made something: a test clip or a finished job (not just "its files and address look right")."""
        roles, proven = {}, {}
        tools = self._audio_ok[0]
        for role in ROLES:
            spec = self.cfg.worker_for(role)
            state = self.wstate.get(spec.id) if spec else None
            ok = bool(tools and spec and not spec.fixture and state is not None and self._effective(state) == "ready")
            roles[role] = ok
            proven[role] = bool(ok and state.evidence in PROVEN)
        return roles, proven

    @staticmethod
    def _pack(roles: Mapping) -> dict:
        return {"music_generation": bool(roles["song"]), "speech_generation": any(roles[r] for r in SPEECH_ROLES), "roles": {r: bool(roles[r]) for r in ROLES}}

    def _capabilities(self) -> dict:
        return self._pack(self._role_flags()[0])

    def _headline(self, workers: list, counts: Mapping) -> tuple:
        """One plain sentence about the makers and what to do next, for the screen's next-step bar and for agents."""
        cfg = self.cfg
        if not cfg.workers and not cfg.rejected and not cfg.disabled:
            return "No music or voice maker is set up.", "Add them in the generation settings. Your own audio works without them."
        if not cfg.workers and not cfg.rejected:
            return "The music and voice makers are switched off.", 'Set "enabled" to true for them in the generation settings, then choose Check again.'
        if not cfg.workers:
            return "The music and voice maker settings have a problem.", "Fix the settings listed in the checks, then choose Check again."
        if not self._audio_ok[0]:
            return "The audio tools are missing.", "Reinstall myradio."
        switched_off = {d.id for d in cfg.disabled}  # off on purpose: not trouble
        bad = [w for w in workers if w["state"] not in ("ready", "checking", "warming") and w["id"] not in switched_off]
        if bad:
            return f"{bad[0]['kind_label']}: {bad[0]['state_label'].lower()}.", bad[0]["hint"] or "Choose Check again."
        if any(w["state"] in ("checking", "warming") for w in workers):
            return "The makers are warming up.", "Nothing to do. The first test can take about a minute."
        if self._paused:
            return "Making audio is paused.", "Choose Resume when the computer is free."
        if any(w["fixture"] for w in workers):
            return "Only test tones are set up.", "Remove the test maker and add real ones before going on air."
        if counts["running"] or counts["queued"]:
            return f"Making audio: {counts['running']} in progress, {counts['queued']} waiting.", ""
        if any(w["ready"] and not w["fixture"] and w["evidence"] not in PROVEN for w in workers):
            return "The makers are ready.", UNPROVEN_HINT
        return "The makers are ready.", ""

    def _build_snapshot(self) -> dict:
        now = self.clock()
        counts = self.store.counts()
        workers = self.workers_view()
        fixture_roles = [r for spec in self.cfg.workers.values() if spec.fixture for r in spec.roles]
        recent = [compact_job(j, self._job_is_fixture(j)) for j in self.store.recent_finished(5)]
        buffer = self.buffer_view()
        headline, next_step = self._headline(workers, counts)
        roles, proven = self._role_flags()
        return {
            "enabled": True, "generated": iso(now), "configured": bool(self.cfg.workers),
            "headline": headline, "next_step": next_step,
            "paused": self._paused, "capabilities": self._pack(roles), "proven": self._pack(proven),
            "fixture_active": bool(fixture_roles), "fixture_roles": sorted(set(fixture_roles)),
            "workers": workers,
            "resources": {name: {"max_concurrent": r.max_concurrent, "running": self._resource_running.get(name, 0),
                                 "loaded_worker": self._resource_loaded.get(name)} for name, r in self.cfg.resources.items()},
            "buffer": {role: {k: v[k] for k in ("unit", "ready", "low", "high", "filling", "queued", "running", "wanted")}
                       for role, v in buffer["roles"].items()},
            "jobs": {"queued": counts["queued"], "running": counts["running"], "succeeded": counts["succeeded"], "failed": counts["failed"],
                     "cancelled": counts["cancelled"], "expired": counts["expired"], "interrupted": counts["interrupted"]},
            "recent": recent,
            "audio_tools": {"available": self._audio_ok[0]},
            "config": {"present": self.cfg.present, "problems": list(self.cfg.problems)[:6], "changed_since_start": self._config_changed},
            "recovered": dict(self._recovery),
            "station_running": self._station_running,
        }

    def _publish(self) -> None:
        if self._stopped and self.store.closed:
            return
        with self._snap_lock:
            try:
                built_at = self.clock()
                self._snapshot = self._build_snapshot()
                self._snapshot_at = built_at
            except GenerationError:
                pass
            except Exception:
                log.exception("could not build the generation status")

    def status(self) -> dict:
        """O(1) and lock-free: the last snapshot. When the maintenance thread has stopped updating it, the snapshot is marked
        ``stale`` and claims nothing: no capability, nothing proven, and a headline that says the news is old."""
        snap, built = self._snapshot, self._snapshot_at
        if built is not None and self.clock() - built > 30.0 and not self._stopped:
            nothing = dict.fromkeys(ROLES, False)
            return {**snap, "stale": True, "capabilities": self._pack(nothing), "proven": self._pack(nothing),
                    "workers": [{**w, "ready": False} for w in snap.get("workers", [])],
                    "headline": "The news about the music and voice makers is out of date.",
                    "next_step": "Choose Check again. If this stays, restart myradio."}
        return snap

    # ================================================================== doctor checks
    def checks(self) -> list:
        out = []

        def add(check_id: str, title: str, status: str, detail: str, hint: str = "") -> None:
            out.append({"id": check_id, "title": title, "status": status, "detail": detail, "hint": hint})

        cfg = self.cfg
        if not cfg.present and not cfg.workers:
            add("config", "Music and voice makers", "warn", "No music or voice maker is set up yet, so the station cannot make its own songs or spoken links.",
                "Add your music maker and voice maker in the generation settings. Your own imported audio works without them.")
        elif cfg.problems:
            add("config", "Music and voice maker settings", "warn", short("; ".join(cfg.problems), 380),
                "Fix the settings listed above, then choose Check again.")
        else:
            add("config", "Music and voice maker settings", "ok", "The settings were read without problems.")
        for title, check_id, roles in (("Music maker", "music", ("song",)), ("Voice maker", "voice", SPEECH_ROLES)):
            spec = next((s for s in cfg.workers.values() if any(r in s.roles for r in roles)), None)
            if spec is None:
                rejected = next((r for r in cfg.rejected if any(role in getattr(self.kinds.get(r.kind), "default_roles", ()) for role in roles)), None)
                if rejected:
                    add(check_id, title, "fail", short(rejected.message, 380), "Fix this entry in the generation settings.")
                elif any(role in off.roles for off in cfg.disabled for role in roles):
                    add(check_id, title, "warn", f"The {title.lower()} is switched off in your settings.",
                        'Set "enabled" to true for it in the generation settings to use it.')
                elif cfg.workers or cfg.present:
                    add(check_id, title, "warn", f"No {title.lower()} is set up.", "Add one in the generation settings.")
                continue
            entry = self._worker_entry(spec.id, spec)
            status = {"ready": "ok", "checking": "unknown", "warming": "warn"}.get(entry["state"], "fail")
            if spec.fixture:
                status = "warn"
            unproven = status == "ok" and not spec.fixture and entry["evidence"] not in PROVEN
            add(check_id, title, status, short(entry["message"], 380), entry["hint"] or (UNPROVEN_HINT if unproven else ""))
            problems = self.workers[spec.id].file_problems() if spec.id in self.workers else []
            add(f"{check_id}_files", f"{title} files", "fail" if problems else "ok",
                "; ".join(problems[:4]) if problems else "Everything the maker needs was found.",
                "Check the paths in the generation settings." if problems else "")
        voice = next((self.workers[s.id] for s in cfg.workers.values() if s.kind == "drama_http" and s.id in self.workers), None)
        if voice is not None:
            add("warmup", "Voice maker warm-up", "ok" if voice.warm else "warn",
                "The voice maker has made a test clip." if voice.warm else "The voice maker has not made its first clip yet. The first one can take about a minute.",
                "" if voice.warm else "This happens by itself when the machine is free.")
        free = free_bytes(self.work_root if self.work_root.exists() else self.ctx.state_dir)
        need = cfg.limits.min_free_disk_mb * 1024 * 1024
        if free is None:
            add("disk", "Space for generated audio", "unknown", "The free space could not be read.")
        else:
            gb = free / 1024 ** 3
            add("disk", "Space for generated audio", "fail" if free < need else "warn" if free < need * 4 else "ok",
                f"{gb:.1f} GB free." if gb >= 0.1 else f"{free // (1024 * 1024)} MB free.",
                "Free up disk space so new songs and spoken links can be saved." if free < need * 4 else "")
        ok, why = self._audio_ok
        add("audio", "Audio tools", "ok" if ok else "fail", "The tools that check and finish audio are installed." if ok else message_for("audio_tools_missing"),
            "" if ok else "Reinstall myradio.")
        if any(s.fixture for s in cfg.workers.values()):
            add("fixture", "Test tones", "warn", "A test tone maker is switched on. Its audio is labelled as a test and is not real music or speech.",
                "Remove the fixture worker from the settings before going on air.")
        recent = self.store.recent_finished(5)
        failed = sum(1 for j in recent if j["state"] in ("failed", "interrupted"))
        if recent:
            many = f"{len(recent)} jobs" if len(recent) > 1 else "job"
            add("recent", "Recent jobs", "warn" if failed >= 3 else "ok",
                f"{failed} of the last {len(recent)} jobs did not finish." if failed else f"The last {many} finished.",
                "Open the job list for the reasons." if failed >= 3 else "")
        return out
