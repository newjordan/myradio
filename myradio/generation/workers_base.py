"""What every worker looks like to the engine."""
from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .audio_api import classify
from .config import Env, FinishSpec, WorkerSpec
from .errors import WorkerError
from .runners import HttpRunner, SubprocessRunner

# How much a worker has proven (shown to the operator so "ready" is never overstated). ``test_tones`` is only ever
# the evidence of a fixture (test tone) maker: it has made sound, but never anything real.
EVIDENCE = ("none", "setup_checked", "test_clip_made", "real_job_completed", "test_tones")


@dataclasses.dataclass(frozen=True)
class GenerationRequest:
    """One attempt at one job, as a worker sees it."""
    job_id: str
    role: str
    payload: Mapping[str, Any]
    seed: int
    attempt: int
    deadline_s: float            # the most time this attempt may use in the worker
    min_s: float                 # shortest plausible result
    max_s: float                 # longest accepted result
    cancel: threading.Event
    work_dir: Path               # a private, empty directory that exists for this attempt only


@dataclasses.dataclass
class WorkerOutput:
    pcm: Any                     # a myradio.audio.Pcm, not yet finished
    meta: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ProbeResult:
    ok: bool
    state: str                   # ready, warming, unreachable, missing_files, failed, blocked
    message: str                 # plain language, safe to show
    hint: str = ""               # what to do next, plain language
    evidence: str = "none"
    version: Optional[str] = None
    needs_full: bool = False     # a real test clip is still needed before this worker counts as ready (the engine runs it by itself)
    detail: str = ""


@dataclasses.dataclass
class WorkerContext:
    audio: Callable[[], Any]
    read_private: Callable[[str], Optional[bytes]]
    clock: Callable[[], float]
    http: HttpRunner
    proc: SubprocessRunner
    work_root: Path
    finish: FinishSpec = dataclasses.field(default_factory=FinishSpec)
    max_song_s: float = 420.0    # ``limits.max_song_seconds``: the longest song a producer may ask for (a test song is judged against it)


class Worker:
    """Base class. A worker turns a :class:`GenerationRequest` into audio, or raises :class:`WorkerError`."""

    kind = ""
    default_roles: tuple = ()
    allowed_roles: tuple = ()
    fixture = False
    # How many test clips in a row may fail before the engine stops making them by itself. After that only a person's
    # "Run a full test" makes another one. A maker whose test costs a whole model run on a shared card says fewer.
    auto_test_limit = 5

    @classmethod
    def parse_options(cls, raw: Mapping, where: str, env: Env) -> dict:
        return {}

    def __init__(self, spec: WorkerSpec, ctx: WorkerContext):
        self.spec = spec
        self.id = spec.id
        self.roles = spec.roles
        self.resource = spec.resource
        self.options = spec.options
        self.ctx = ctx

    # -- what the engine calls
    def probe(self, full: bool, cancel: threading.Event) -> ProbeResult:
        raise NotImplementedError

    def generate(self, request: GenerationRequest) -> WorkerOutput:
        raise NotImplementedError

    def describe(self) -> dict:
        """Facts safe to show (no commands with secrets, no keys)."""
        return {}

    def identity(self) -> dict:
        """``{"version": ..., "hash": ...}`` when known."""
        return {"version": None, "hash": None}

    def file_problems(self) -> list:
        """Plain-language notes about required files that are missing. Local, quick, no network."""
        return []

    def invalidate(self) -> None:
        """Forget anything learned about the worker being warm (called after it stopped answering)."""

    def refresh(self) -> None:
        """An operator asked for a fresh check: forget cached answers so the next quick check looks again."""

    @property
    def warm(self) -> bool:
        return True

    def check_take(self, pcm, role: str, min_s: float, max_s: float) -> list:
        """Run the audio toolkit's check on one result. Raises on a fatal problem; returns the plain notes otherwise."""
        audio = self.ctx.audio()
        fatal, notes = classify(audio.validate_generated(pcm, role, min_s, max_s), self.ctx.finish.strict_clipping, self.ctx.finish.strict_tail)
        if fatal is not None:
            raise fatal
        return notes


def missing_files(paths) -> list:
    out = []
    for path in paths:
        try:
            if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                out.append(os.path.basename(path) or path)
        except OSError:
            out.append(os.path.basename(path) or path)
    return out


def require(condition: bool, code: str, message: str, **kw) -> None:
    if not condition:
        raise WorkerError(code, message, **kw)
