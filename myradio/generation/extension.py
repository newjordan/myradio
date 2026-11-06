"""The generation extension: operations, routes, status and doctor checks for the engine.

Settings come from ``<data>/ext/generation/config.json`` (written by the operator) and never from an
operation argument. See ``docs/lanes/generation.md`` for the full contract.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..extensions import Arg, Context, Extension, ExtensionError, Operation, Request, Response, Route
from .config import Env, LoadedConfig, parse_config
from .engine import Engine
from .errors import GenerationError
from .payloads import KEY_RE, ROLES
from .store import STATES, JobStore
from .workers import KINDS

PROBE_MIN_INTERVAL_S = 10.0
DB_NAME = "generation.sqlite3"
MAX_JOB_NUMBER = 2 ** 62  # a job's list position; anything above cannot exist and would not fit the database's integers

log = logging.getLogger("myradio.generation")


def build() -> "GenerationExtension":
    return GenerationExtension()


class GenerationExtension(Extension):
    name = "generation"

    def __init__(self, *, kinds: Optional[Mapping[str, type]] = None, clock: Optional[Callable[[], float]] = None,
                 probe_min_interval: float = PROBE_MIN_INTERVAL_S, env: Optional[Env] = None):
        self._kinds = {**KINDS, **(kinds or {})}
        self._clock_override = clock
        self._probe_min_interval = probe_min_interval
        self._env = env or Env()
        self._ctx: Optional[Context] = None
        self._store: Optional[JobStore] = None
        self._engine: Optional[Engine] = None
        self._swap = threading.RLock()
        self._last_probe = float("-inf")
        self._probe_lock = threading.Lock()
        self._stopped = False

    # ---------------------------------------------------------------- lifecycle
    def _clock(self) -> float:
        return self._clock_override() if self._clock_override else self._ctx.clock()

    def _load_config(self) -> LoadedConfig:
        ctx = self._ctx
        present = (Path(ctx.state_dir) / "config.json").is_file()
        try:
            raw = ctx.config()
        except ExtensionError as exc:
            cfg = parse_config({}, self._kinds, self._env, present=present)
            return dataclasses.replace(cfg, problems=(f"The generation settings file could not be used: {exc}",) + cfg.problems,
                                       fingerprint="unreadable")
        return parse_config(raw, self._kinds, self._env, present=present)

    def start(self, ctx: Context) -> None:
        self._ctx = ctx
        conn = None
        try:
            conn = ctx.open_db(DB_NAME)
            self._store = JobStore(conn, self._clock)
        except sqlite3.DatabaseError:
            if conn is not None:
                conn.close()  # a history file that cannot be prepared must not leave its connection open
            raise ExtensionError("The generation history file could not be opened. If it is damaged, move generation.sqlite3 "
                                 "out of the station's generation folder and restart; the settings are kept in config.json.") from None
        try:
            self._engine = Engine(ctx, self._load_config(), self._kinds, self._store, clock=self._clock)
            self._engine.start()
        except BaseException:
            engine, self._engine = self._engine, None
            if engine is not None:
                engine.stop()
            else:
                self._store.close()
            raise

    def stop(self) -> None:
        with self._swap:
            if self._stopped:
                return
            self._stopped = True
            if self._engine is not None:
                self._engine.stop()
            elif self._store is not None:
                self._store.close()

    def _reload_if_idle(self) -> dict:
        """Pick up a changed settings file when nothing is being made. Returns a short note for the screen.

        The new engine is built before the old one is stopped, and if the new settings cannot be started the old ones are
        started again, so a failure here never leaves the service pointing at an engine that has been stopped."""
        with self._swap:
            engine = self._engine
            current = self._load_config()
            if current.fingerprint == engine.cfg.fingerprint:
                return {"reloaded": False, "settings_changed": False}
            if current.fingerprint == "unreadable":
                return {"reloaded": False, "settings_changed": True,
                        "note": "The settings file cannot be read right now, so the settings from the last start are still in use. Fix the file, then check again."}
            if engine.running_count():
                return {"reloaded": False, "settings_changed": True,
                        "note": "Your settings changed, but something is being made right now. Check again when it finishes, or restart myradio."}
            try:
                fresh = Engine(self._ctx, current, self._kinds, self._store, clock=self._clock)
            except Exception:
                log.exception("the new generation settings could not be loaded")
                return {"reloaded": False, "settings_changed": True,
                        "note": "The new settings could not be loaded just now, so the settings from the last start are still in use. "
                                "Try again in a moment, or restart myradio."}
            engine.stop(close_store=False)
            try:
                fresh.start()
            except Exception:
                log.exception("the new generation settings could not be started")
                fresh.stop(close_store=False)
                return self._start_again(engine.cfg)
            self._engine = fresh
            return {"reloaded": True, "settings_changed": True}

    def _start_again(self, cfg: LoadedConfig) -> dict:
        """The new settings would not start after the old engine was stopped: run the old settings again."""
        try:
            again = Engine(self._ctx, cfg, self._kinds, self._store, clock=self._clock)
            again.start()
        except Exception:
            log.exception("the previous generation settings could not be started again")
            self._engine = None  # honest: nothing is running
            return {"reloaded": False, "settings_changed": True,
                    "note": "The music and voice service could not be restarted. Restart myradio to start it again."}
        self._engine = again
        return {"reloaded": False, "settings_changed": True,
                "note": "The new settings could not be started, so the settings from the last start are in use again. "
                        "Check them for mistakes, then check again."}

    def _use(self) -> Engine:
        engine = self._engine
        if engine is None or self._stopped:
            raise GenerationError("The generation service is not running.")
        return engine

    # ---------------------------------------------------------------- station seam
    def status(self) -> dict:
        engine = self._engine
        return engine.status() if engine is not None else {"enabled": False}

    def checks(self) -> list:
        engine = self._engine
        return engine.checks() if engine is not None and not self._stopped else []

    def persistent_files(self) -> list:
        # The history database and the operator's settings. Key files named in the settings are secret and stay out.
        return [DB_NAME, DB_NAME + "-wal", "config.json"]

    def on_event(self, event: dict) -> None:
        engine = self._engine
        if engine is not None and not self._stopped:
            engine.on_event(event)

    # ---------------------------------------------------------------- operations
    def operations(self) -> list:
        return [
            Operation("enqueue",
                      "Ask the music or voice maker for something. role is song (payload: tags, optional lyrics, duration_s) or host_break, id, spot, "
                      "update (payload: text, optional speaker). Safe to repeat with the same idempotency_key.",
                      {"role": Arg("string", "What to make.", enum=ROLES),
                       "payload": Arg("object", "The details; see the generation guide for each role."),
                       "priority": Arg("integer", "0 to 100; higher goes first; 80 and up skips the buffer limit. Default 50.", required=False, minimum=0, maximum=100),
                       "ttl_s": Arg("integer", "Drop the job if it has not been made after this many seconds.", required=False, minimum=5, maximum=2592000),
                       "idempotency_key": Arg("string", "Your own label; sending the same request with the same label never makes it twice.", required=False, max_length=120)},
                      self._op_enqueue),
            Operation("cancel", "Cancel a waiting or running job by job_id.", {"job_id": Arg("string", "The job to cancel.", max_length=64)}, self._op_cancel),
            Operation("retry", "Queue a failed, cancelled, expired or interrupted job again by job_id.",
                      {"job_id": Arg("string", "The job to try again.", max_length=64)}, self._op_retry),
            Operation("pause", "Stop starting new work so the computer can be used for something else. Running jobs finish unless cancel_running is true.",
                      {"cancel_running": Arg("boolean", "Also stop jobs being made now and put them back in line.", required=False)}, self._op_pause),
            Operation("resume", "Start making things again after a pause.", {}, self._op_resume),
            Operation("set_buffer",
                      "Set how much finished audio to keep ready for a kind of audio: seconds for song, items for the others. It tops up below low and stops at high.",
                      {"role": Arg("string", "The kind of audio.", enum=ROLES),
                       "low": Arg("integer", "Start making more when less than this is ready.", minimum=0, maximum=7200),
                       "high": Arg("integer", "Stop making more when this much is ready.", minimum=1, maximum=14400)}, self._op_set_buffer),
            Operation("job", "Look up one job (state, error, and where its audio went) by job_id.",
                      {"job_id": Arg("string", "The job to look up.", max_length=64)}, self._op_job),
        ]

    def _op_enqueue(self, ctx: Context, args: dict) -> dict:
        key = args.get("idempotency_key") or None
        if key is not None and not KEY_RE.fullmatch(key):
            raise GenerationError("idempotency_key may use letters, digits and . _ : / @ - (up to 120 characters).")
        with self._swap:
            view, created = self._use().enqueue(args["role"], args["payload"], args.get("priority", 50), args.get("ttl_s"), key)
        note = "Queued." if created else "That request was already queued or made, so nothing new was added."
        return {"job": view, "created": created, "deduplicated": not created, "message": note}

    def _op_cancel(self, ctx: Context, args: dict) -> dict:
        with self._swap:
            engine = self._use()
            view, settled = engine.cancel(args["job_id"], wait_s=0.0)
        if not settled:
            view, settled = engine.settle(args["job_id"], 3.0)  # waited for outside the lock, so other operations are not held up
        message = {"cancelled": "Cancelled.", "succeeded": "It had already finished.", "running": "Stopping it now."}.get(view["state"], "Done.")
        if view["state"] == "queued":
            message = "Put back in line."
        return {"job": view, "settled": settled, "message": message}

    def _op_retry(self, ctx: Context, args: dict) -> dict:
        with self._swap:
            return {"job": self._use().retry(args["job_id"]), "message": "Queued again."}

    def _op_pause(self, ctx: Context, args: dict) -> dict:
        with self._swap:
            result = self._use().pause(bool(args.get("cancel_running", False)))
        result["message"] = ("Paused. Nothing new will start." + (" Running jobs were stopped and put back in line." if result["stopping_now"] else
                             " Jobs already running will finish first." if result["running"] else " The machine is free."))
        return result

    def _op_resume(self, ctx: Context, args: dict) -> dict:
        with self._swap:
            result = self._use().resume()
        result["message"] = "Resumed."
        return result

    def _op_set_buffer(self, ctx: Context, args: dict) -> dict:
        with self._swap:
            return {"buffer": self._use().set_buffer(args["role"], args["low"], args["high"]), "message": "Saved."}

    def _op_job(self, ctx: Context, args: dict) -> dict:
        with self._swap:
            view = self._use().get_job(args["job_id"])
        if view is None:
            raise GenerationError("That job was not found. It may be old enough to have been cleared from the history.")
        return {"job": view}

    # ---------------------------------------------------------------- routes
    def routes(self) -> list:
        return [Route("GET", "/jobs", self._route_jobs), Route("GET", "/workers", self._route_workers),
                Route("GET", "/buffer", self._route_buffer), Route("POST", "/probe", self._route_probe, max_body=1024)]

    @staticmethod
    def _one(query: Mapping, name: str) -> Optional[str]:
        values = query.get(name)
        return values[0] if values else None

    def _route_jobs(self, ctx: Context, request: Request) -> Any:
        engine = self._use()
        q = request.query
        job_id, track_id = self._one(q, "id"), self._one(q, "track_id")
        if job_id is not None or track_id is not None:
            view = engine.get_job(job_id) if job_id is not None else engine.job_for_track(track_id)
            if view is None:
                return Response({"error": "No job was found for that."}, 404)
            return {"job": view}
        state, role = self._one(q, "state"), self._one(q, "role")
        if state is not None and state not in STATES:
            raise GenerationError("state must be one of: " + ", ".join(STATES) + ".")
        if role is not None and role not in ROLES:
            raise GenerationError("role must be one of: " + ", ".join(ROLES) + ".")
        try:
            limit = int(self._one(q, "limit") or 50)
            before = self._one(q, "before")
            before = int(before) if before is not None else None
        except ValueError:
            raise GenerationError("limit and before must be whole numbers.") from None
        if before is not None and not 0 <= before <= MAX_JOB_NUMBER:
            raise GenerationError("before must be the position of a job from an earlier list (a whole number, not negative and not huge).")
        limit = max(1, min(limit, 200))
        jobs = engine.list_jobs(state=state, role=role, limit=limit, before=before)
        return {"jobs": jobs, "counts": engine.store.counts(), "next_before": jobs[-1]["seq"] if len(jobs) == limit else None}

    def _route_workers(self, ctx: Context, request: Request) -> Any:
        engine = self._use()
        snap = engine.status()
        return {"workers": engine.workers_view(), "capabilities": snap.get("capabilities"), "proven": snap.get("proven"), "paused": engine.paused,
                "config": snap.get("config"), "fixture_active": snap.get("fixture_active", False)}

    def _route_buffer(self, ctx: Context, request: Request) -> Any:
        return self._use().buffer_view()

    def _route_probe(self, ctx: Context, request: Request) -> Any:
        body = request.json or {}
        if not isinstance(body, dict):
            raise GenerationError('Send {} or {"full": true}.')
        full = body.get("full", False)
        if not isinstance(full, bool) or set(body) - {"full"}:
            raise GenerationError('Send {} or {"full": true}.')
        with self._probe_lock:
            now = time.monotonic()
            wait = self._probe_min_interval - (now - self._last_probe)
            if wait > 0:
                return Response({"error": f"The checks just ran. Try again in {int(wait) + 1} seconds.", "retry_after_s": int(wait) + 1}, 429)
            self._last_probe = now
        with self._swap:
            note = self._reload_if_idle()
            engine = self._engine
        if engine is None or self._stopped:  # the new settings would not start and neither would the old ones
            return Response({"error": note.get("note") or "The generation service is not running.", **note}, 503)
        reply = engine.request_probe(full=full)
        snap = engine.status()
        out = {"workers": reply["workers"], "capabilities": snap.get("capabilities"), "proven": snap.get("proven"),
               "full_check_queued": reply["full_check_queued"], **note}
        if full:
            if not reply["full_check_queued"]:
                out["full_check_message"] = ("No test clip was started: each maker is busy making something, not answering, or missing files. "
                                             "The makers' cards say which.")
            elif engine.paused:
                out["full_check_message"] = "A short test clip will be made when making audio is resumed."
            else:
                out["full_check_message"] = ("A short test clip will be made as soon as the machine is free. It can take up to a minute, "
                                             "and longer for a song.")
        return out
