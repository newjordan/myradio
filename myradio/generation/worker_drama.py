"""Drama 3 speech through an audio.cpp style speech server (``POST /v1/audio/speech``).

The server is started by the operator; this worker only talks to it. The first request after the
server starts pays a one-time warm-up of up to about a minute, so a short test clip is made before real
work is trusted (``probe(full=True)``), and a connection failure marks the worker cold again.

Long scripts are split into takes at sentence ends (each below ``max_take_chars``) and the takes are
joined with a short crossfade, which keeps every request quick and avoids the model's known habit of
dropping the end of long inputs.
"""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Mapping, Optional

from .config import Env, check_base_url, check_url_path, get_bool, get_file_name, get_float, get_int, get_str, check_paths
from .errors import ConfigError, WorkerError, message_for
from .payloads import CHARS_PER_SECOND, SPEECH_ROLES, spoken_text
from .speech import split_takes
from .workers_base import GenerationRequest, ProbeResult, Worker, WorkerOutput, missing_files

FRAMES_PER_SECOND = 21.5  # Drama 3 audio frames per second of speech, used to turn seconds into a token limit
AUDIO_TYPES = ("audio/",)
OPTION_RANGES = {"temperature": (0.0, 2.0), "top_p": (0.0, 1.0), "top_k": (1, 1000)}
RUN_ON_FACTOR = 2.5     # a take this many times longer than its words need has kept talking past the script
RUN_ON_SLACK_S = 6.0    # ...and at least this many seconds over, so short lines with a natural pause are not refused


def ran_on(text: str, seconds: float) -> bool:
    """True when a take is far longer than its script: the voice kept going after the last word."""
    expected = len(spoken_text(text)) / CHARS_PER_SECOND
    return seconds > max(expected * RUN_ON_FACTOR, expected + RUN_ON_SLACK_S)


def _status_error(status: int, body: bytes) -> WorkerError:
    note = " ".join(body[:200].decode("utf-8", "replace").split())
    detail = f"HTTP {status}: {note}" if note else f"HTTP {status}"
    if status in (401, 403):
        return WorkerError("worker_failed", "The voice maker refused the request. Check the access key in your settings.",
                           retryable=False, detail=detail)
    if status == 404:
        return WorkerError("worker_failed", "The voice maker does not offer that service. Check the address and model name in your settings.",
                           retryable=False, detail=detail)
    if 300 <= status < 400:
        return WorkerError("worker_failed", "The voice maker tried to send the request somewhere else, which is not allowed.",
                           retryable=False, detail=detail)
    if status in (429, 503):
        return WorkerError("worker_unreachable", "The voice maker is busy or still starting. It will be tried again.", detail=detail)
    if status >= 500:
        return WorkerError("worker_failed", "The voice maker ran into a problem. It will be tried again.", detail=detail)
    return WorkerError("worker_failed", "The voice maker did not accept the text.", retryable=False, detail=detail)


class DramaHttpWorker(Worker):
    kind = "drama_http"
    default_roles = SPEECH_ROLES
    allowed_roles = SPEECH_ROLES

    @classmethod
    def parse_options(cls, raw: Mapping, where: str, env: Env) -> dict:
        allow_remote = get_bool(raw, "allow_remote", False, where)
        address = check_base_url(raw.get("base_url"), where, allow_remote)
        options = raw.get("options", {})
        if not isinstance(options, dict):
            raise ConfigError(f"{where}: options must be an object such as {{\"temperature\": 1.0}}.")
        clean = {}
        for key, value in options.items():
            if key not in OPTION_RANGES:
                raise ConfigError(f"{where}: option {str(key)[:30]!r} is not supported. Supported: {', '.join(OPTION_RANGES)}.")
            lo, hi = OPTION_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise ConfigError(f"{where}: option {key} must be a number from {lo:g} to {hi:g}.")
            clean[key] = int(value) if key == "top_k" else float(value)
        request_timeout = get_float(raw, "request_timeout_s", 90.0, 5.0, 600.0, where, " seconds")
        warmup_timeout = get_float(raw, "warmup_timeout_s", 180.0, 10.0, 900.0, where, " seconds")
        return {
            "host": address["host"], "port": address["port"], "path": address["path"], "base_url": address["base_url"],
            "allow_remote": allow_remote,
            "model": get_str(raw, "model", "drama-3", where, max_len=80),
            "speech_path": check_url_path(raw, "speech_path", "/v1/audio/speech", where),
            "health_path": check_url_path(raw, "health_path", "/v1/models", where),
            "request_timeout_s": request_timeout,
            "warmup_timeout_s": warmup_timeout,
            "job_timeout_s": get_float(raw, "job_timeout_s", 600.0, 10.0, 1800.0, where, " seconds"),
            "max_take_chars": get_int(raw, "max_take_chars", 380, 80, 1000, where),
            "crossfade_ms": get_int(raw, "crossfade_ms", 60, 0, 500, where),
            "max_response_bytes": get_int(raw, "max_response_mb", 60, 1, 100, where) * 1024 * 1024,
            "auth_token_file": get_file_name(raw, "auth_token_file", where),
            "warmup_text": get_str(raw, "warmup_text", "<|speaker:0|> Ready.", where, max_len=200),
            "smoke_test": get_bool(raw, "smoke_test", True, where),
            "model_files": check_paths(raw, "model_files", where),
            "generation_options": clean,
        }

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self._warm = False
        self._server: Optional[str] = None
        self._lock = threading.Lock()

    # -- state
    @property
    def warm(self) -> bool:
        return self._warm or not self.options["smoke_test"]

    def invalidate(self) -> None:
        self._warm = False

    def identity(self) -> dict:
        return {"version": self._server, "hash": None}

    def describe(self) -> dict:
        o = self.options
        return {"address": o["base_url"], "model": o["model"], "warm": self.warm, "protects_with_key": bool(o["auth_token_file"]),
                "remote_allowed": bool(o["allow_remote"])}

    def file_problems(self) -> list:
        notes = [f"Missing or empty: {name}" for name in missing_files(self.options["model_files"])]
        token = self.options["auth_token_file"]
        if token:
            try:
                present = bool(self.ctx.read_private(token))
            except Exception:
                present = False
            if not present:
                notes.append(f"The access key file {token} is missing or empty")
        return notes

    # -- requests
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        token = self.options["auth_token_file"]
        if token:
            raw = self.ctx.read_private(token)
            value = (raw or b"").decode("utf-8", "replace").strip()
            if not value or any(ord(c) < 33 or ord(c) == 127 for c in value):
                raise WorkerError("worker_failed", "The access key for the voice maker is missing or not valid. Check your settings.",
                                  retryable=False, detail=f"key file {token} unreadable")
            headers["Authorization"] = f"Bearer {value}"
        return headers

    def _speech_body(self, text: str, seed: int, max_seconds: float) -> dict:
        o = self.options
        body = {"model": o["model"], "input": text, "response_format": "wav", "seed": int(seed),
                "max_tokens": int(math.ceil(max_seconds * FRAMES_PER_SECOND))}
        body.update(o["generation_options"])
        return body

    def _post_speech(self, text: str, seed: int, max_seconds: float, deadline_s: float, cancel: threading.Event):
        o = self.options
        body = self._speech_body(text, seed, max_seconds)
        try:
            response = self.ctx.http.request("POST", o["host"], o["port"], o["path"] + o["speech_path"],
                                             body=json.dumps(body).encode("utf-8"), headers=self._headers(), deadline_s=deadline_s,
                                             max_response_bytes=o["max_response_bytes"], cancel=cancel, allow_remote=o["allow_remote"])
        except WorkerError as exc:
            if exc.code in ("worker_unreachable", "worker_timeout"):
                self.invalidate()
            raise
        if response.status != 200:
            raise _status_error(response.status, response.body)
        self._server = (response.headers.get("server") or self._server or "")[:120] or None
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind and not (kind.startswith(AUDIO_TYPES) or kind == "application/octet-stream"):
            raise WorkerError("bad_output", message_for("bad_output"), detail=f"content-type {kind[:60]}")
        if response.body[:4] != b"RIFF" or response.body[8:12] != b"WAVE":
            raise WorkerError("bad_output", message_for("bad_output"), detail="the answer is not a WAV file")
        audio = self.ctx.audio()
        try:
            return audio.read_wav(response.body, max_bytes=o["max_response_bytes"])
        except audio.AudioError as exc:
            raise WorkerError("bad_output", message_for("bad_output"), detail=f"AudioError: {exc}"[:300]) from None

    def _take(self, role: str, text: str, seed: int, max_seconds: float, deadline_s: float, cancel: threading.Event):
        pcm = self._post_speech(text, seed, max_seconds, deadline_s, cancel)
        self.check_take(pcm, role, 0.1, max_seconds + 3.0)
        if ran_on(text, pcm.seconds):
            raise WorkerError("bad_output", "The voice kept talking past the script. It will be tried again with a new seed.",
                              detail=f"{pcm.seconds:.1f} s of audio for {len(spoken_text(text))} characters")
        return pcm

    def _warm_up(self, cancel: threading.Event) -> float:
        started = time.monotonic()
        o = self.options
        self._take("id", o["warmup_text"], 1, 8.0, o["warmup_timeout_s"], cancel)
        self._warm = True
        return time.monotonic() - started

    # -- engine entry points
    def probe(self, full: bool, cancel: threading.Event) -> ProbeResult:
        o = self.options
        problems = self.file_problems()
        if problems:
            return ProbeResult(False, "missing_files", "Some files the voice maker needs were not found: " + "; ".join(problems[:3]) + ".",
                               "Check the file paths in your settings, then choose Check again.", detail="; ".join(problems))
        try:
            response = self.ctx.http.request("GET", o["host"], o["port"], o["path"] + o["health_path"], headers=self._headers_for_get(),
                                             deadline_s=5.0, max_response_bytes=262144, cancel=cancel, allow_remote=o["allow_remote"])
        except WorkerError as exc:
            self.invalidate()
            if exc.code == "worker_blocked":
                return ProbeResult(False, "failed", "The voice maker's address is not allowed.", "Use an address on this computer, or allow remote makers in your settings.",
                                   detail=exc.detail)
            if exc.code == "worker_failed":
                return ProbeResult(False, "failed", exc.message, "Check the voice maker settings, then choose Check again.", detail=exc.detail)
            if exc.code == "worker_timeout":
                return ProbeResult(False, "unreachable", f"The voice maker at {o['base_url']} did not answer within 5 seconds. It may be busy or still starting.",
                                   "Give it a moment, then choose Check again.", detail=exc.detail)
            return ProbeResult(False, "unreachable", f"The voice maker is not answering at {o['base_url']}.",
                               "Start the voice maker, then choose Check again.", detail=exc.detail)
        self._server = (response.headers.get("server") or "")[:120] or self._server
        if response.status in (401, 403):
            return ProbeResult(False, "failed", "The voice maker refused the request. It may need an access key, or the one in your settings is wrong.",
                               "Check the access key file in your settings, then choose Check again.", version=self._server, detail=f"HTTP {response.status}")
        if response.status >= 500 or response.status in (429, 503):
            return ProbeResult(False, "warming", "The voice maker answered but says it is not ready yet.",
                               "Give it a minute to finish starting, then choose Check again.", version=self._server, detail=f"HTTP {response.status}")
        if not full:
            if not o["smoke_test"]:
                return ProbeResult(True, "ready", "The voice maker is answering. No test clip has been made yet.", evidence="setup_checked",
                                   version=self._server)
            if self._warm:
                return ProbeResult(True, "ready", "The voice maker is ready.", evidence="test_clip_made", version=self._server)
            return ProbeResult(False, "warming", "The voice maker is answering but has not made a test clip yet. The first one can take about a minute.",
                               "Nothing to do. It will warm up by itself.", evidence="setup_checked", version=self._server, needs_full=True)
        # A full check is a real test clip, whatever ``smoke_test`` says: that setting only decides whether the engine
        # makes the clip by itself (and holds the maker back until it passes). Asked for by a person, it always runs.
        try:
            seconds = self._warm_up(cancel)
        except WorkerError as exc:
            return ProbeResult(False, "warming" if exc.code in ("worker_timeout", "worker_unreachable") else "failed",
                               "The voice maker is running but could not make a test clip. " + exc.message,
                               "Look at the voice maker's own messages to see why it cannot speak. When that is fixed, choose Run a full test.",
                               evidence="setup_checked", version=self._server, needs_full=exc.retryable and o["smoke_test"], detail=exc.detail)
        return ProbeResult(True, "ready", f"The voice maker is ready (warm-up took {seconds:.0f} seconds).", evidence="test_clip_made",
                           version=self._server)

    def _headers_for_get(self) -> dict:
        headers = self._headers()
        headers.pop("Content-Type", None)
        return headers

    def generate(self, request: GenerationRequest) -> WorkerOutput:
        o = self.options
        payload = request.payload
        takes = split_takes(payload["text"], payload.get("speaker", 0), o["max_take_chars"])
        if not takes:
            raise WorkerError("worker_failed", "There was no text to speak.", retryable=False, detail="empty after splitting")
        began = time.monotonic()
        warm_s = 0.0
        if not self.warm:
            warm_s = self._warm_up(request.cancel)
        job_deadline = time.monotonic() + min(request.deadline_s, o["job_timeout_s"])
        max_seconds = float(payload.get("max_seconds") or request.max_s)
        parts = []
        for index, take in enumerate(takes):
            remaining = job_deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker_timeout", message_for("worker_timeout"), detail=f"took longer than {o['job_timeout_s']:.0f}s in total")
            parts.append(self._take(request.role, take, (request.seed + index) % (2 ** 32), max_seconds,
                                    min(o["request_timeout_s"], remaining), request.cancel))
        audio = self.ctx.audio()
        try:
            joined = parts[0] if len(parts) == 1 else audio.concat([audio.to_station_format(p) for p in parts], crossfade_ms=o["crossfade_ms"])
        except Exception as exc:
            raise WorkerError("bad_output", message_for("bad_output"), detail=f"could not join takes: {type(exc).__name__}: {exc}"[:300]) from None
        return WorkerOutput(joined, {"takes": len(takes), "characters": len(payload["text"]), "server": self._server,
                                     "warmup_s": round(warm_s, 2), "worker_s": round(time.monotonic() - began, 2)})
