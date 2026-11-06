"""YuE2 music through an audio.cpp server that keeps the model loaded (``POST /v1/tasks/run``).

Where ``yue2_cli`` starts a cold program for every song, this worker talks to a running ``audiocpp_server``
that holds YuE2 in memory, so the model is loaded once. The same server can hold the Drama 3 voice as a
second model: each model runs its own request at a time, so a song and a DJ break can render together.
That is the "dual loader" setup (one server, both models resident, two resource groups in this config).

Settings::

    base_url              address of the server, for example http://127.0.0.1:8090 (this computer only,
                          unless allow_remote is true)
    model                 the server's model id for YuE2 (default "yue2")
    task_path             default /v1/tasks/run
    health_path           default /v1/models
    request_options       fixed YuE2 request options as strings, for example
                          {"cot": "full", "guidance_scale": "1.0"}; "style", "lyrics" and
                          "num_inference_steps" come from each song and cannot be set here
    tokens_per_second     semantic tokens per second of music, used for semantic_max_tokens (default 25)
    timeout_s             longest time one song may take (default 1800)
    max_response_mb       largest answer accepted (default 120; a song arrives as base64 WAV inside JSON)
    model_files           files to check before work starts (optional)
    smoke_test            make a short test song by itself before real work (default false)
"""
from __future__ import annotations

import base64
import binascii
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from .config import Env, check_base_url, check_paths, check_url_path, get_bool, get_float, get_int, get_str
from .errors import Cancelled, ConfigError, WorkerError, message_for
from .payloads import normalize_payload, window_for
from .workers_base import GenerationRequest, ProbeResult, Worker, WorkerOutput, missing_files

SMOKE_PAYLOAD = {"tags": "instrumental ambient test, slow, soft pads, 70 BPM", "duration_s": 8, "steps": 8}
SMOKE_MIN_S = 1.0
RESERVED_OPTIONS = frozenset({"style", "lyrics", "num_inference_steps"})
OPTION_KEY = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.")


def _status_error(status: int, body: bytes) -> WorkerError:
    note = " ".join(body[:300].decode("utf-8", "replace").split())
    detail = f"HTTP {status}: {note}" if note else f"HTTP {status}"
    if status in (429, 503):
        return WorkerError("worker_unreachable", "The music maker is busy or still starting. It will be tried again.", detail=detail)
    if status == 404:
        return WorkerError("worker_failed", "The music maker does not offer that model. Check the address and model id in your settings.",
                           retryable=False, detail=detail)
    if status >= 500:
        return WorkerError("worker_failed", "The music maker ran into a problem. It will be tried again.", detail=detail)
    return WorkerError("worker_failed", "The music maker did not accept the song.", retryable=False, detail=detail)


class Yue2HttpWorker(Worker):
    kind = "yue2_http"
    default_roles = ("song",)
    allowed_roles = ("song",)
    auto_test_limit = 3

    @classmethod
    def parse_options(cls, raw: Mapping, where: str, env: Env) -> dict:
        allow_remote = get_bool(raw, "allow_remote", False, where)
        address = check_base_url(raw.get("base_url"), where, allow_remote)
        fixed = raw.get("request_options", {"cot": "full", "guidance_scale": "1.0"})
        if not isinstance(fixed, dict) or len(fixed) > 32:
            raise ConfigError(f"{where}: request_options must be an object of up to 32 settings, such as {{\"cot\": \"full\"}}.")
        clean = {}
        for key, value in fixed.items():
            if not isinstance(key, str) or not key or len(key) > 60 or set(key) - OPTION_KEY:
                raise ConfigError(f"{where}: request option names use lower-case letters, digits, dots and underscores.")
            if key in RESERVED_OPTIONS:
                raise ConfigError(f"{where}: request option {key} comes from each song and cannot be fixed in the settings.")
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (int, float)):
                value = format(value, "g")
            if not isinstance(value, str) or len(value) > 200:
                raise ConfigError(f"{where}: request option {key} must be a short text, number or true/false.")
            clean[key] = value
        return {
            "host": address["host"], "port": address["port"], "path": address["path"], "base_url": address["base_url"],
            "allow_remote": allow_remote,
            "model": get_str(raw, "model", "yue2", where, max_len=80),
            "task_path": check_url_path(raw, "task_path", "/v1/tasks/run", where),
            "health_path": check_url_path(raw, "health_path", "/v1/models", where),
            "request_options": clean,
            "tokens_per_second": get_float(raw, "tokens_per_second", 25.0, 1.0, 200.0, where),
            "timeout_s": get_float(raw, "timeout_s", 1800.0, 30.0, 7200.0, where, " seconds"),
            "max_response_bytes": get_int(raw, "max_response_mb", 120, 1, 400, where) * 1024 * 1024,
            "model_files": check_paths(raw, "model_files", where),
            "smoke_test": get_bool(raw, "smoke_test", False, where),
        }

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self._smoked = False
        self._loaded: Optional[bool] = None
        self._family: Optional[str] = None

    @property
    def warm(self) -> bool:
        return self._smoked or not self.options["smoke_test"]

    def invalidate(self) -> None:
        self._smoked = False

    def identity(self) -> dict:
        return {"version": self._family, "hash": None}

    def describe(self) -> dict:
        o = self.options
        return {"address": o["base_url"], "model": o["model"], "resident": bool(self._loaded), "remote_allowed": bool(o["allow_remote"]),
                "time_limit_s": o["timeout_s"]}

    def file_problems(self) -> list:
        return [f"Missing or empty: {name}" for name in missing_files(self.options["model_files"])]

    # -- requests
    def _body(self, payload: Mapping, seed: int) -> dict:
        o = self.options
        options = dict(o["request_options"])
        options.setdefault("semantic_max_tokens", str(int(round(float(payload["duration_s"]) * o["tokens_per_second"]))))
        options.update({"style": payload["tags"], "num_inference_steps": str(int(payload["steps"]))})
        return {"model": o["model"], "request": {"lyrics": payload.get("lyrics") or "[instrumental]", "seed": int(seed), "options": options}}

    def _decode(self, body: bytes):
        try:
            answer = json.loads(body)
            data = base64.b64decode(answer["audio"], validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise WorkerError("bad_output", message_for("bad_output"), detail="the answer has no audio") from None
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise WorkerError("bad_output", message_for("bad_output"), detail="the audio is not a WAV file")
        timing = answer.get("timing") if isinstance(answer.get("timing"), dict) else {}
        return data, timing

    def generate(self, request: GenerationRequest) -> WorkerOutput:
        o = self.options
        problems = self.file_problems()
        if problems:
            raise WorkerError("worker_failed", "Files the music maker needs are missing: " + "; ".join(problems[:3]) + ".",
                              retryable=True, detail="; ".join(problems))
        began = time.monotonic()
        body = json.dumps(self._body(request.payload, request.seed)).encode("utf-8")
        try:
            response = self.ctx.http.request("POST", o["host"], o["port"], o["path"] + o["task_path"], body=body,
                                             headers={"Content-Type": "application/json"},
                                             deadline_s=max(1.0, min(request.deadline_s, o["timeout_s"])),
                                             max_response_bytes=o["max_response_bytes"], cancel=request.cancel,
                                             allow_remote=o["allow_remote"])
        except WorkerError as exc:
            if exc.code in ("worker_unreachable", "worker_timeout"):
                self.invalidate()
            raise
        if response.status != 200:
            raise _status_error(response.status, response.body)
        data, timing = self._decode(response.body)
        audio = self.ctx.audio()
        try:
            pcm = audio.read_wav(data, max_bytes=o["max_response_bytes"])
        except audio.AudioError as exc:
            raise WorkerError("bad_output", message_for("bad_output"), detail=f"AudioError: {exc}"[:300]) from None
        meta = {"worker_s": round(time.monotonic() - began, 2), "instrumental": not request.payload.get("lyrics"),
                "resident": True}
        for key, name in (("wall_ms", "server_wall_s"), ("audio_duration_ms", "server_audio_s")):
            value = timing.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                meta[name] = round(value / 1000.0, 2)
        return WorkerOutput(pcm, meta)

    # -- probing
    def probe(self, full: bool, cancel: threading.Event) -> ProbeResult:
        o = self.options
        problems = self.file_problems()
        if problems:
            return ProbeResult(False, "missing_files", "Some files the music maker needs were not found: " + "; ".join(problems[:3]) + ".",
                               "Check the file paths in your settings, then choose Check again.", detail="; ".join(problems))
        try:
            response = self.ctx.http.request("GET", o["host"], o["port"], o["path"] + o["health_path"], deadline_s=5.0,
                                             max_response_bytes=262144, cancel=cancel, allow_remote=o["allow_remote"])
        except WorkerError as exc:
            self.invalidate()
            if exc.code == "worker_blocked":
                return ProbeResult(False, "failed", "The music maker's address is not allowed.",
                                   "Use an address on this computer, or allow remote makers in your settings.", detail=exc.detail)
            return ProbeResult(False, "unreachable", f"The music maker is not answering at {o['base_url']}.",
                               "Start the music server, then choose Check again.", detail=exc.detail)
        if response.status != 200:
            return ProbeResult(False, "warming", "The music server answered but says it is not ready yet.",
                               "Give it a minute to finish starting, then choose Check again.", detail=f"HTTP {response.status}")
        try:
            models = {m.get("id"): m for m in json.loads(response.body).get("data", []) if isinstance(m, dict)}
        except (ValueError, AttributeError):
            models = {}
        entry = models.get(o["model"])
        if entry is None:
            return ProbeResult(False, "failed", f"The music server does not have a model called {o['model']}.",
                               "Check the model id in your settings against the server's configuration.",
                               detail="models: " + ", ".join(sorted(str(k) for k in models))[:200])
        self._loaded = entry.get("loaded") is True
        self._family = str(entry.get("family") or "")[:60] or None
        if not full:
            if not o["smoke_test"]:
                note = "The music maker is loaded and ready." if self._loaded else "The music maker is answering; it loads the model on first use."
                return ProbeResult(True, "ready", note + " No test song has been made yet.", evidence="setup_checked", version=self._family)
            if self._smoked:
                return ProbeResult(True, "ready", "The music maker is ready.", evidence="test_clip_made", version=self._family)
            return ProbeResult(False, "warming", "The music maker is answering but has not made a test song yet.",
                               "It will make a short one when the machine is free.", evidence="setup_checked", version=self._family,
                               needs_full=True)
        return self._smoke(cancel)

    def _smoke(self, cancel: threading.Event) -> ProbeResult:
        norm = normalize_payload("song", dict(SMOKE_PAYLOAD))
        longest = window_for("song", {"duration_s": self.ctx.max_song_s})[1]
        with tempfile.TemporaryDirectory(prefix="smoke-", dir=self.ctx.work_root) as scratch:
            request = GenerationRequest("smoke", "song", norm.data, 1, 1, self.options["timeout_s"], SMOKE_MIN_S, longest, cancel, Path(scratch))
            try:
                output = self.generate(request)
                self.check_take(output.pcm, "song", SMOKE_MIN_S, longest)
            except Cancelled:
                return ProbeResult(False, "warming", "The test song was stopped.", needs_full=self.options["smoke_test"],
                                   evidence="setup_checked", version=self._family)
            except WorkerError as exc:
                return ProbeResult(False, "failed", "The music server is running but could not make a short test song. " + exc.message,
                                   "Look at the music server's own messages to see why. When that is fixed, choose Run a full test.",
                                   evidence="setup_checked", version=self._family, needs_full=exc.retryable and self.options["smoke_test"],
                                   detail=exc.detail)
        self._smoked = True
        return ProbeResult(True, "ready", "The music maker made a test song and is ready.", evidence="test_clip_made", version=self._family)
