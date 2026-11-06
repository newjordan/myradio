"""A deterministic tone generator for tests. It is NOT a music or speech maker.

It refuses to exist unless the environment variable ``MYRADIO_ALLOW_FIXTURE_WORKERS`` is exactly ``1``,
and the engine labels everything it produces as a fixture: in the provenance record (a flag that cannot
be cleared), in the library title and artist, in the status text, and in the doctor checks. A fixture
never counts toward the ``music_generation`` or ``speech_generation`` capabilities.
"""
from __future__ import annotations

import array
import math
import os
import re
import sys
from typing import Mapping

from .config import Env, get_float, get_int
from .errors import ConfigError, WorkerError
from .payloads import ROLES
from .workers_base import GenerationRequest, ProbeResult, Worker, WorkerOutput

ENV_VAR = "MYRADIO_ALLOW_FIXTURE_WORKERS"
RATE = 44100
DEFAULT_TONE_SECONDS = 6.0  # how long a test tone is when the settings do not say (longer only when the request's window needs it)
FREQUENCIES = (441, 490, 525, 630, 735)  # each divides 44100 exactly, so a period tiles without a click
BLOCKED_MESSAGE = ("Test audio makers are switched off. They exist only for tests and are never used on a real station. "
                   "Remove the fixture worker from your settings.")


def fixtures_allowed(environ=None) -> bool:
    return (os.environ if environ is None else environ).get(ENV_VAR) == "1"


def tone_frames(seconds: float, hz: int, amplitude: float = 0.2, fade_ms: int = 120) -> bytes:
    """Interleaved 16-bit stereo samples of a sine at ``hz`` (which must divide 44100), with smooth fades."""
    period = RATE // hz
    one = array.array("h", (int(round(amplitude * 32767 * math.sin(2 * math.pi * i / period))) for i in range(period)))
    frames = max(1, int(RATE * seconds))
    mono = (one * (frames // period + 1))[:frames]
    edge = min(frames // 2, RATE * fade_ms // 1000)
    for i in range(edge):
        gain = (i / edge) ** 2  # a quadratic fade dies away fully, so the end never sounds cut off
        mono[i] = int(mono[i] * gain)
        mono[frames - 1 - i] = int(mono[frames - 1 - i] * gain)
    stereo = array.array("h", bytes(frames * 4))
    stereo[0::2] = mono
    stereo[1::2] = mono
    if sys.byteorder == "big":
        stereo.byteswap()
    return stereo.tobytes()


class FixtureWorker(Worker):
    kind = "fixture"
    default_roles = ROLES
    allowed_roles = ROLES
    fixture = True

    @classmethod
    def parse_options(cls, raw: Mapping, where: str, env: Env) -> dict:
        if not fixtures_allowed(env.environ):
            raise ConfigError(f"{where}: {BLOCKED_MESSAGE}")
        hz = get_int(raw, "frequency_hz", 0, 0, 2205, where)
        if hz and (hz < 100 or RATE % hz):
            raise ConfigError(f"{where}: frequency_hz must divide 44100 evenly (for example 441 or 490), or be left out.")
        # ``max_seconds`` is a hard cap only when the operator writes it; left out, the tone is as short as the request allows.
        cap = get_float(raw, "max_seconds", DEFAULT_TONE_SECONDS, 1.0, 30.0, where, " seconds") if "max_seconds" in raw else None
        return {"max_seconds": cap, "frequency_hz": hz}

    def __init__(self, spec, ctx):
        if not fixtures_allowed():
            raise WorkerError("worker_blocked", BLOCKED_MESSAGE, retryable=False, detail=f"{ENV_VAR} is not 1")
        super().__init__(spec, ctx)

    def describe(self) -> dict:
        return {"fixture": True, "warning": "Makes test tones only. It never makes real music or speech."}

    def probe(self, full: bool, cancel) -> ProbeResult:
        if not fixtures_allowed():
            return ProbeResult(False, "blocked", BLOCKED_MESSAGE, "Remove the fixture worker from your settings.")
        return ProbeResult(True, "ready", "Test tones only. This is not a real music or voice maker.", evidence="setup_checked")

    def generate(self, request: GenerationRequest) -> WorkerOutput:
        if not fixtures_allowed():
            raise WorkerError("worker_blocked", BLOCKED_MESSAGE, retryable=False, detail=f"{ENV_VAR} is not 1")
        audio = self.ctx.audio()
        o = self.options
        payload = request.payload
        cap = o["max_seconds"]
        if request.role == "song":
            wanted = float(payload["duration_s"])
        else:
            spoken = re.sub(r"<\|speaker:\d+\|>|\[[a-z \-]+\]", " ", payload["text"])
            wanted = max(1.0, min(len(" ".join(spoken.split())) / 14.0, float(payload.get("max_seconds", 180.0))))
        if cap is not None:
            seconds = min(wanted, cap)  # the operator's cap wins, even over the length check (a cap that is too small makes results that are refused)
        else:
            # No cap written: stay short (a test should be quick) but never fall outside the length the engine will accept.
            seconds = min(wanted, DEFAULT_TONE_SECONDS) if request.role == "song" else wanted
            seconds = min(max(seconds, request.min_s + 0.5), max(request.min_s + 0.5, request.max_s - 0.5))
        hz = o["frequency_hz"] or FREQUENCIES[request.seed % len(FREQUENCIES)]
        frames = tone_frames(seconds, hz)
        return WorkerOutput(audio.Pcm(frames=frames, rate=RATE, channels=2), {"fixture": True, "frequency_hz": hz, "seconds": round(seconds, 3)})
