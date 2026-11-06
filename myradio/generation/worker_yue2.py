"""YuE2 music through a command line program, one cold process per song.

The command is written by the operator in ``config.json`` as a list of arguments. Each argument may use
the placeholders below; a placeholder is replaced by exactly one value inside that one argument, so job
text can never add arguments or reach a shell (none is used).

Placeholders for ``command`` and ``instrumental_command``:

``{output_file}``  full path where the program must write the finished WAV (required)
``{work_dir}``     the private empty folder made for this attempt (deleted afterwards)
``{tags}``         the style description of the song
``{lyrics}``       the lyrics, or ``[instrumental]`` when the song has none
``{lyrics_file}``  full path of a text file holding the same lyrics
``{seed}``         the seed, a whole number from 0 to 4294967295
``{steps}``        the number of refinement steps, a whole number
``{duration_s}``   the target length in seconds, a whole number
``{instrumental}`` ``true`` or ``false``
``{prompt_id}``    the preset name the song came from, or an empty argument

``plan_command`` (optional, instrumental songs only) runs first with the same placeholders plus
``{plan_file}``, a full path where it must write a non-empty plan; ``instrumental_command`` then runs in
place of ``command`` and may use ``{plan_file}`` too. Write ``{{`` and ``}}`` for literal braces.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from .config import (Env, check_command, check_env, check_paths, get_bool, get_float, get_int, render_arg, template_names)
from .errors import Cancelled, ConfigError, WorkerError, message_for
from .payloads import normalize_payload, window_for
from .runners import build_env
from .workers_base import GenerationRequest, ProbeResult, Worker, WorkerOutput, missing_files

PLACEHOLDERS = frozenset({"output_file", "work_dir", "tags", "lyrics", "lyrics_file", "seed", "steps", "duration_s", "instrumental", "prompt_id"})
PLAN_PLACEHOLDERS = PLACEHOLDERS | {"plan_file"}
TEXT_PLACEHOLDERS = frozenset({"tags", "lyrics", "prompt_id"})
SMOKE_PAYLOAD = {"tags": "instrumental ambient test, slow, soft pads, 70 BPM", "duration_s": 8, "steps": 8}
SMOKE_MIN_S = 1.0   # the shortest test song that counts as sound from a program that runs; the longest is the real window's (see ``_smoke``)
HASH_LIMIT = 1024 ** 3


def _tail(result, limit: int = 600) -> str:
    text = result.stderr.strip() or result.stdout.strip()
    return " ".join(text.split())[-limit:]


class Yue2CliWorker(Worker):
    kind = "yue2_cli"
    default_roles = ("song",)
    allowed_roles = ("song",)
    auto_test_limit = 3   # a test song is a whole program run on the shared card: three failed ones in a row, then only a person asks again

    @classmethod
    def parse_options(cls, raw: Mapping, where: str, env: Env) -> dict:
        plan = raw.get("plan_command")
        instrumental = raw.get("instrumental_command")
        if plan is not None and instrumental is None:
            raise ConfigError(f"{where}: plan_command needs an instrumental_command that uses the plan it writes.")
        main = check_command(raw.get("command"), "command", where, PLACEHOLDERS, env, required=("output_file",))
        options = {"command": main, "plan_command": None, "instrumental_command": None}
        if plan is not None:
            options["plan_command"] = check_command(plan, "plan_command", where, PLAN_PLACEHOLDERS, env, required=("plan_file",))
        if instrumental is not None:
            options["instrumental_command"] = check_command(
                instrumental, "instrumental_command", where, PLAN_PLACEHOLDERS if plan is not None else PLACEHOLDERS, env,
                required=("output_file", "plan_file") if plan is not None else ("output_file",))
        version = raw.get("version_command")
        options["version_command"] = check_command(version, "version_command", where, frozenset(), env) if version is not None else None
        values, passthrough = check_env(raw, where)
        options.update({
            "timeout_s": get_float(raw, "timeout_s", 1800.0, 30.0, 7200.0, where, " seconds"),
            "plan_timeout_s": get_float(raw, "plan_timeout_s", 600.0, 10.0, 3600.0, where, " seconds"),
            "version_timeout_s": get_float(raw, "version_timeout_s", 20.0, 1.0, 60.0, where, " seconds"),
            "kill_grace_s": get_float(raw, "kill_grace_s", 5.0, 1.0, 30.0, where, " seconds"),
            "max_output_bytes": get_int(raw, "max_output_mb", 100, 1, 200, where) * 1024 * 1024,
            "env": values, "env_passthrough": passthrough,
            "model_files": check_paths(raw, "model_files", where),
            "smoke_test": get_bool(raw, "smoke_test", False, where),
        })
        return options

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self._smoked = False
        self._version: Optional[str] = None
        self._version_checked = False
        self._hash: Optional[str] = None
        self._hash_key = None

    @property
    def warm(self) -> bool:
        return self._smoked or not self.options["smoke_test"]

    def invalidate(self) -> None:
        self._smoked = False

    def refresh(self) -> None:
        self._version_checked = False

    def identity(self) -> dict:
        return {"version": self._version, "hash": self._hash}

    def describe(self) -> dict:
        o = self.options
        return {"program": os.path.basename(o["command"][0]), "has_plan_stage": bool(o["plan_command"]),
                "files_checked": len(o["model_files"]), "time_limit_s": o["timeout_s"]}

    def file_problems(self) -> list:
        o = self.options
        notes = []
        programs = [o["command"][0]] + ([o["instrumental_command"][0]] if o["instrumental_command"] else [])
        for path in dict.fromkeys(programs):
            if not (os.path.isfile(path) and os.access(path, os.X_OK)):
                notes.append(f"Program not found or not runnable: {os.path.basename(path)}")
        notes += [f"Missing or empty: {name}" for name in missing_files(o["model_files"])]
        return notes

    # -- running
    def _render(self, template, values: Mapping[str, str]) -> list:
        argv = [template[0]]
        for arg in template[1:]:
            text = render_arg(arg, values)
            names = template_names(arg, "")
            if text.startswith("-") and len(names) == 1 and arg == "{" + next(iter(names)) + "}" and next(iter(names)) in TEXT_PLACEHOLDERS:
                raise WorkerError("worker_failed", "The job text starts with a dash, which a command could mistake for an option.",
                                  retryable=False, detail=f"value for {{{next(iter(names))}}}")
            argv.append(text)
        return argv

    def _stage(self, name: str, template, values, work: Path, cancel, deadline_s: float):
        o = self.options
        env = build_env(o["env_passthrough"], o["env"], work)
        result = self.ctx.proc.run(self._render(template, values), cwd=work, env=env, deadline_s=deadline_s, cancel=cancel,
                                   max_dir_bytes=o["max_output_bytes"], grace_s=o["kill_grace_s"])
        if result.reason == "cancelled":
            raise Cancelled()
        if result.reason == "timeout":
            raise WorkerError("worker_timeout", "The music maker took too long and was stopped.",
                              detail=f"{name} ran {result.elapsed_s:.0f}s without finishing. {_tail(result)}")
        if result.reason == "too_large":
            raise WorkerError("output_too_large", message_for("output_too_large"), retryable=False, detail=f"{name} wrote more than the allowed size")
        if result.leaked:
            raise WorkerError("worker_failed", "The music maker left a process running that could not be stopped. Check the computer for stuck programs.",
                              detail=f"{name}: process group did not stop")
        if result.returncode != 0:
            code = result.returncode
            if code is not None and code < 0:
                raise WorkerError("worker_failed", "The music maker was stopped by the system. It may have run out of memory.",
                                  detail=f"{name} ended with signal {-code}. {_tail(result)}")
            raise WorkerError("worker_failed", "The music maker stopped with a problem and did not finish the song.",
                              detail=f"{name} exit code {code}. {_tail(result)}")
        return result

    def _read_output(self, path: Path, audio):
        cap = self.options["max_output_bytes"]
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise WorkerError("no_output", message_for("no_output"), detail="the output file was not written") from None
        if not stat.S_ISREG(info.st_mode):
            raise WorkerError("bad_output", message_for("bad_output"), detail="the output is not a regular file")
        if info.st_size > cap:
            raise WorkerError("output_too_large", message_for("output_too_large"), retryable=False, detail=f"{info.st_size} bytes")
        if info.st_size < 44:
            raise WorkerError("no_output", message_for("no_output"), detail=f"the output file has {info.st_size} bytes")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(cap + 1)
        if len(data) > cap:
            raise WorkerError("output_too_large", message_for("output_too_large"), retryable=False, detail="grew while being read")
        try:
            return audio.read_wav(data, max_bytes=cap)
        except audio.AudioError as exc:
            raise WorkerError("bad_output", message_for("bad_output"), detail=f"AudioError: {exc}"[:300]) from None

    def generate(self, request: GenerationRequest) -> WorkerOutput:
        o = self.options
        payload = request.payload
        problems = self.file_problems()
        if problems:
            raise WorkerError("worker_failed", "Files the music maker needs are missing: " + "; ".join(problems[:3]) + ".",
                              retryable=True, detail="; ".join(problems))
        audio = self.ctx.audio()
        work = Path(request.work_dir)
        instrumental = not payload.get("lyrics")
        lyrics = payload.get("lyrics") or "[instrumental]"
        lyrics_path, out_path, plan_path = work / "lyrics.txt", work / "output.wav", work / "plan.dat"
        fd = os.open(lyrics_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(lyrics)
        values = {"output_file": str(out_path), "work_dir": str(work), "tags": payload["tags"], "lyrics": lyrics,
                  "lyrics_file": str(lyrics_path), "seed": str(request.seed), "steps": str(payload["steps"]),
                  "duration_s": str(payload["duration_s"]), "instrumental": "true" if instrumental else "false",
                  "prompt_id": payload.get("prompt_id") or "", "plan_file": str(plan_path)}
        began = time.monotonic()
        stages = {}
        use_plan = instrumental and o["plan_command"] is not None
        template = o["instrumental_command"] if instrumental and o["instrumental_command"] is not None else o["command"]
        budget_end = began + min(request.deadline_s, o["timeout_s"] + (o["plan_timeout_s"] if use_plan else 0))
        if use_plan:
            self._stage("the planning step", o["plan_command"], values, work, request.cancel,
                        max(1.0, min(o["plan_timeout_s"], budget_end - time.monotonic())))
            stages["plan_s"] = round(time.monotonic() - began, 2)
            try:
                size = os.lstat(plan_path).st_size
            except FileNotFoundError:
                size = 0
            if size <= 0:
                raise WorkerError("no_output", "The music maker's planning step finished without a plan.", detail="plan file missing or empty")
        mark = time.monotonic()
        self._stage("the music maker", template, values, work, request.cancel, max(1.0, budget_end - time.monotonic()))
        stages["render_s"] = round(time.monotonic() - mark, 2)
        pcm = self._read_output(out_path, audio)
        return WorkerOutput(pcm, {"stages": stages, "worker_s": round(time.monotonic() - began, 2), "instrumental": instrumental})

    # -- probing
    def _runtime_hash(self) -> Optional[str]:
        path = self.options["command"][0]
        try:
            info = os.stat(path)
        except OSError:
            return None
        key = (path, info.st_mtime_ns, info.st_size)
        if key == self._hash_key:
            return self._hash
        if info.st_size > HASH_LIMIT:
            return None
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
        except OSError:
            return None
        self._hash_key, self._hash = key, digest.hexdigest()
        return self._hash

    def probe(self, full: bool, cancel: threading.Event) -> ProbeResult:
        o = self.options
        problems = self.file_problems()
        if problems:
            return ProbeResult(False, "missing_files", "The music maker is not installed completely: " + "; ".join(problems[:3]) + ".",
                               "Check the paths in your settings, then choose Check again.", detail="; ".join(problems))
        self._runtime_hash()
        if o["version_command"] is not None and (full or not self._version_checked):  # not every minute: it can wake the GPU runtime
            with tempfile.TemporaryDirectory(prefix="probe-", dir=self.ctx.work_root) as scratch:
                try:
                    result = self.ctx.proc.run(list(o["version_command"]), cwd=scratch, env=build_env(o["env_passthrough"], o["env"], scratch),
                                               deadline_s=o["version_timeout_s"], cancel=cancel, grace_s=min(o["kill_grace_s"], 3.0))
                except WorkerError as exc:
                    return ProbeResult(False, "failed", "The music maker program could not be started.", "Check that it is installed and allowed to run.",
                                       detail=exc.detail)
            if result.reason != "exit" or result.returncode != 0:
                return ProbeResult(False, "failed", "The music maker program started but did not answer its version check.",
                                   "Check that it is installed completely, then choose Check again.",
                                   detail=f"{result.reason}, exit {result.returncode}. {_tail(result)}")
            first = (result.stdout.strip().splitlines() or [""])[0]
            self._version = " ".join(first.split())[:120] or self._version
            self._version_checked = True
        if not full:
            if not o["smoke_test"]:
                return ProbeResult(True, "ready", "The music maker is installed. No test song has been made yet.", evidence="setup_checked",
                                   version=self._version)
            if self._smoked:
                return ProbeResult(True, "ready", "The music maker is ready.", evidence="test_clip_made", version=self._version)
            return ProbeResult(False, "warming", "The music maker is installed but has not made a test song yet.", "It will make a short one when the machine is free.",
                               evidence="setup_checked", version=self._version, needs_full=True)
        # A full check makes a real short song, whatever ``smoke_test`` says: that setting only decides whether the engine
        # does it by itself (and holds the maker back until it passes). Asked for by a person, it always runs.
        return self._smoke(cancel)

    def _smoke(self, cancel: threading.Event) -> ProbeResult:
        norm = normalize_payload("song", dict(SMOKE_PAYLOAD))
        # The test song only has to prove that the program starts, runs the model and writes sound. Its length proves nothing: the
        # program is told 8 seconds only when the command has {duration_s} (the documented example has none), and a real build makes
        # songs of their own natural length, a minute or more. So the test is judged on length like a real result is, not tighter:
        # at least one second, and no longer than a real job for the longest allowed song would be accepted at.
        longest = window_for("song", {"duration_s": self.ctx.max_song_s})[1]
        with tempfile.TemporaryDirectory(prefix="smoke-", dir=self.ctx.work_root) as scratch:
            request = GenerationRequest("smoke", "song", norm.data, 1, 1, self.options["timeout_s"], SMOKE_MIN_S, longest, cancel, Path(scratch))
            try:
                output = self.generate(request)
                self.check_take(output.pcm, "song", SMOKE_MIN_S, longest)
            except Cancelled:
                return ProbeResult(False, "warming", "The test song was stopped.", needs_full=self.options["smoke_test"], evidence="setup_checked",
                                   version=self._version)
            except WorkerError as exc:
                return ProbeResult(False, "failed", "The music maker is installed but could not make a short test song. " + exc.message,
                                   "Run the music program by hand with the same settings to see what it reports. When that is fixed, "
                                   "choose Run a full test.", evidence="setup_checked", version=self._version,
                                   needs_full=exc.retryable and self.options["smoke_test"], detail=exc.detail)
        self._smoked = True
        return ProbeResult(True, "ready", "The music maker is ready (a short test song was made).", evidence="test_clip_made", version=self._version)
