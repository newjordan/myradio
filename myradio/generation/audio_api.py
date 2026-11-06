"""The engine's view of the audio toolkit (``myradio.audio``) and the finishing pipeline.

The toolkit is looked up each time it is needed (a cheap module lookup), so a missing or still-being-built
toolkit shows up as a plain "audio tools are missing" failure instead of breaking the whole extension at
import time.

Finishing order for every take: check it (``validate_generated``), convert to the station format (44.1 kHz
stereo), trim silence, fade, normalise to the station level, write a WAV. Converting comes first so that the
level and the peak ceiling are set on the samples that are actually broadcast: resampling after normalising can
raise the sample peak by several dB (measured up to +4 dB on noise at 48 kHz) and push a take past the ceiling.
Nothing here talks to the station; the engine imports the returned bytes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import threading
from typing import Optional

from .config import FinishSpec
from .errors import Cancelled, WorkerError, message_for

REQUIRED = ("AudioError", "Pcm", "read_wav", "write_wav", "to_station_format", "resample", "analyze", "validate_generated",
            "normalize", "fade", "trim_silence", "concat")
STATION_MIN_FRAMES = 4410  # the library refuses anything under 100 ms

# Problems that make a result unusable, with the engine's error code for each.
FATAL = {"silent": "silent_output", "too_short": "audio_too_short", "too_long": "audio_too_long", "truncated_tail": "audio_cut_off"}


def load_audio():
    """Return the audio module, or raise a non-retryable :class:`WorkerError` saying the tools are missing."""
    try:
        module = importlib.import_module("myradio.audio")
    except Exception as exc:  # missing, or broken while being built: either way nothing can be finished
        raise WorkerError("audio_tools_missing", message_for("audio_tools_missing"), retryable=False,
                          detail=f"{type(exc).__name__}: {exc}"[:300]) from None
    missing = [name for name in REQUIRED if not hasattr(module, name)]
    if missing:
        raise WorkerError("audio_tools_missing", message_for("audio_tools_missing"), retryable=False,
                          detail="missing: " + ", ".join(missing))
    return module


def audio_available() -> tuple:
    try:
        load_audio()
        return True, ""
    except WorkerError as exc:
        return False, exc.detail


def classify(problems, strict_clipping: bool = False, strict_tail: bool = True) -> tuple:
    """Split the toolkit's problems into (first fatal WorkerError or None, notes as plain dicts)."""
    fatal: Optional[WorkerError] = None
    notes = []
    for problem in problems or ():
        code = getattr(problem, "code", str(problem))
        message = getattr(problem, "message", "") or ""
        is_fatal = (code in FATAL and (strict_tail or code != "truncated_tail")) or (strict_clipping and code == "clipped")
        notes.append({"code": code, "message": message[:200], "fatal": bool(is_fatal)})
        if is_fatal and fatal is None:
            wc = FATAL.get(code, "audio_unusable")
            fatal = WorkerError(wc, message_for(wc), retryable=True, detail=f"{code}: {message}"[:300])
    return fatal, notes


@dataclasses.dataclass
class Finished:
    wav: bytes
    duration_s: float
    sha256: str
    validation: list
    raw_seconds: float
    steps_s: dict
    levels: dict


def finish(audio, pcm, *, role: str, min_s: float, max_s: float, spec: FinishSpec,
           cancel: Optional[threading.Event] = None) -> Finished:
    """Run the finishing pipeline on one decoded result. Raises :class:`WorkerError` for an unusable take."""
    import time

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    steps: dict = {}
    mark = time.monotonic()

    def tick(name: str) -> None:
        nonlocal mark
        now = time.monotonic()
        steps[name] = round(now - mark, 3)
        mark = now
        check()

    try:
        raw_seconds = float(pcm.seconds)
        problems = audio.validate_generated(pcm, role, min_s, max_s)
        tick("validate")
        fatal, notes = classify(problems, spec.strict_clipping, spec.strict_tail)
        if fatal is not None:
            raise fatal
        pcm = audio.to_station_format(pcm)  # first: the ceiling below must hold for the samples that go on air
        tick("convert")
        pcm = audio.trim_silence(pcm, threshold_dbfs=-50.0, keep_ms=60)
        tick("trim")
        in_ms, out_ms = (15, 250) if role == "song" else (8, 40)
        pcm = audio.fade(pcm, in_ms, out_ms)
        tick("fade")
        levels: dict = {"target_lufs": spec.target_lufs, "ceiling_dbfs": spec.ceiling_dbfs}
        if hasattr(audio, "normalize_with_report"):  # an extra of the real toolkit: keeps the measured level for provenance
            pcm, report = audio.normalize_with_report(pcm, target_lufs=spec.target_lufs, ceiling_dbfs=spec.ceiling_dbfs)
            levels.update({"input_lufs": round(report.input_lufs, 1), "gain_db": round(report.gain_db, 1), "limited_by": report.limited_by})
        else:
            pcm = audio.normalize(pcm, target_lufs=spec.target_lufs, ceiling_dbfs=spec.ceiling_dbfs)
        tick("normalize")
        if pcm.frame_count < STATION_MIN_FRAMES:
            raise WorkerError("audio_too_short", message_for("audio_too_short"), detail="under 100 ms after trimming silence")
        wav = audio.write_wav(pcm)
        tick("write")
    except (WorkerError, Cancelled):
        raise
    except Exception as exc:  # AudioError or a bug in the toolkit: the take is unusable either way
        name = type(exc).__name__
        raise WorkerError("audio_unusable", message_for("audio_unusable"), retryable=name == "AudioError",
                          detail=f"{name}: {exc}"[:300]) from None
    return Finished(wav, float(pcm.seconds), hashlib.sha256(wav).hexdigest(), notes, raw_seconds, steps, levels)
