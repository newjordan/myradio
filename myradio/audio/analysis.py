"""Measurements and the quality gate for generated audio: ``analyze`` and ``validate_generated``.

Thresholds (all documented in docs/lanes/audio.md):

* A 20 ms block is *silent* when none of its samples is louder than -50 dBFS.
* Audio is *silent* when its peak is below -60 dBFS or at least 98% of it is silent blocks.
* Audio is *clipped* when more than 0.1% of its samples sit exactly at the highest or lowest
  possible value. A handful of such samples in a loud, peak-normalised file is normal and not flagged.
* Audio has a *constant offset* when the average of any channel is more than 2% of full scale.
* Audio *ends abruptly* when the loudest sample in its last 10 ms is above -50 dBFS and less than
  15 dB below the loudest sample of the whole clip. A natural ending, a fade of 100 ms or more, or a
  pause has died away well below that by the last few milliseconds; a cut-off has not.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from operator import mul

from .loudness import loudness_lufs
from .pcm import (FLOOR_DB, FULL_SCALE, AudioError, Pcm, array16, check_number, format_duration, guard_memory,
                  power_to_db, require_pcm, to_db, view16)

__all__ = ["Analysis", "Problem", "analyze", "validate_generated", "active_bounds", "ROLES", "PROBLEM_CODES",
           "SILENCE_DBFS", "SILENT_PEAK_DBFS", "SILENT_RATIO", "CLIP_RATIO_LIMIT", "DC_LIMIT", "TAIL_FLOOR_DBFS",
           "TAIL_MARGIN_DB"]

SILENCE_DBFS = -50.0      # a sample louder than this is "sound" when looking for silence
SILENT_PEAK_DBFS = -60.0  # whole clip counts as silent when its peak is below this ...
SILENT_RATIO = 0.98       # ... or when this fraction of it is silent blocks
CLIP_RATIO_LIMIT = 0.001  # more than 0.1% of samples at the rails is called clipped
DC_LIMIT = 0.02           # a channel average beyond 2% of full scale is called a constant offset
TAIL_FLOOR_DBFS = -50.0   # an ending quieter than this is never called abrupt ...
TAIL_MARGIN_DB = 15.0     # ... and neither is one at least this far below the clip's own peak
TAIL_SECONDS = 0.01
BLOCK_SECONDS = 0.02
PROBLEM_CODES = ("silent", "too_short", "too_long", "clipped", "dc_offset", "truncated_tail")
ROLES = {"song": "song", "host_break": "host break", "id": "station ID", "spot": "sponsor message",
         "update": "news update"}
_SCAN_FRAMES = 1 << 16

try:  # Python 3.12+ has a fast exact dot product
    from math import sumprod as _sumprod
except ImportError:  # pragma: no cover - Python 3.10 and 3.11
    def _sumprod(a, b):
        return sum(map(mul, a, b))


@dataclass(frozen=True)
class Analysis:
    """Plain measurements of a piece of audio. Every number is finite; silence reads as -120.0 dB."""

    duration_s: float
    peak_dbfs: float
    rms_dbfs: float
    loudness_lufs: float
    silence_ratio: float
    clipped_ratio: float
    dc_offset: float
    leading_silence_s: float
    trailing_silence_s: float

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Problem:
    """Something wrong with generated audio. ``code`` is one of PROBLEM_CODES; ``message`` is for people."""

    code: str
    message: str

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class _Scan:
    __slots__ = ("peak", "rails", "silent_frames", "sums", "sumsq")

    def __init__(self, peak, rails, silent_frames, sums, sumsq):
        self.peak, self.rails, self.silent_frames, self.sums, self.sumsq = peak, rails, silent_frames, sums, sumsq


def _scan(pcm: Pcm, want_energy: bool) -> _Scan:
    """One pass over the audio: peak, rail count, silent frames, per-channel sums (and squared sum if asked)."""
    cached = pcm._memo.get("scan")
    if cached is not None and (cached.sumsq is not None or not want_energy):
        return cached
    channels, width = pcm.channels, 2 * pcm.channels
    total = pcm.frame_count
    block = max(1, round(pcm.rate * BLOCK_SECONDS))
    chunk = block * max(1, _SCAN_FRAMES // block)
    quiet = math.floor(FULL_SCALE * 10.0 ** (SILENCE_DBFS / 20.0))
    high, low = -32768, 32767
    rails = silent = 0
    sums = [0] * channels
    sumsq = 0
    for first in range(0, total, chunk):
        count = min(chunk, total - first)
        samples = array16(pcm.frames[first * width:(first + count) * width])
        view = memoryview(samples)
        step = block * channels
        for offset in range(0, len(samples), step):
            piece = view[offset:offset + step]
            top, bottom = max(piece), min(piece)
            if top > high:
                high = top
            if bottom < low:
                low = bottom
            if top <= quiet and bottom >= -quiet:
                silent += len(piece) // channels
            if top == 32767 or bottom == -32768:
                part = samples[offset:offset + step]
                rails += part.count(32767) + part.count(-32768)
        for c in range(channels):
            sums[c] += sum(samples[c::channels]) if channels > 1 else sum(samples)
        if want_energy:
            sumsq += _sumprod(samples, samples)
    result = _Scan(max(high, -low), rails, silent, tuple(sums), sumsq if want_energy else None)
    pcm._memo["scan"] = result
    return result


def active_bounds(pcm: Pcm, threshold_dbfs: float):
    """(first, last) index of the frames whose loudest sample is above the threshold, or (None, None)."""
    total, channels = pcm.frame_count, pcm.channels
    if total == 0:
        return None, None
    limit = math.floor(FULL_SCALE * 10.0 ** (threshold_dbfs / 20.0))
    view = view16(pcm.frames)
    span = 4096
    first = None
    for start in range(0, total, span):
        piece = view[start * channels:min(total, start + span) * channels]
        if max(piece) > limit or min(piece) < -limit:
            for i, x in enumerate(piece):
                if x > limit or x < -limit:
                    first = start + i // channels
                    break
            break
    if first is None:
        return None, None
    end = total
    while end > first:
        start = max(first, end - span)
        piece = view[start * channels:end * channels]
        if max(piece) > limit or min(piece) < -limit:
            for i in range(len(piece) - 1, -1, -1):
                if piece[i] > limit or piece[i] < -limit:
                    return first, start + i // channels
        end = start
    return first, first  # unreachable in practice: the first active frame is itself active


def _ends_abruptly(pcm: Pcm, peak: int) -> bool:
    """True when the last TAIL_SECONDS still hold sound near the clip's own peak level."""
    count = min(pcm.frame_count, max(1, round(pcm.rate * TAIL_SECONDS)))
    tail = view16(pcm.frames[len(pcm.frames) - count * 2 * pcm.channels:])
    tail_db = to_db(max(max(tail), -min(tail)) / FULL_SCALE)
    return tail_db > TAIL_FLOOR_DBFS and tail_db > to_db(peak / FULL_SCALE) - TAIL_MARGIN_DB


@guard_memory
def analyze(pcm: Pcm) -> Analysis:
    """Measure peak, RMS, gated loudness, silence, clipping, constant offset and silent edges."""
    require_pcm(pcm)
    total, channels = pcm.frame_count, pcm.channels
    if total == 0:
        return Analysis(0.0, FLOOR_DB, FLOOR_DB, FLOOR_DB, 0.0, 0.0, 0.0, 0.0, 0.0)
    scan = _scan(pcm, True)
    samples = total * channels
    first, last = active_bounds(pcm, SILENCE_DBFS)
    duration = total / pcm.rate
    if first is None:
        leading = trailing = duration
    else:
        leading, trailing = first / pcm.rate, (total - 1 - last) / pcm.rate
    return Analysis(
        duration_s=duration,
        peak_dbfs=to_db(scan.peak / FULL_SCALE),
        rms_dbfs=power_to_db(scan.sumsq / samples / (FULL_SCALE * FULL_SCALE)),
        loudness_lufs=loudness_lufs(pcm),
        silence_ratio=scan.silent_frames / total,
        clipped_ratio=scan.rails / samples,
        dc_offset=max(abs(s) for s in scan.sums) / total / FULL_SCALE,
        leading_silence_s=leading,
        trailing_silence_s=trailing,
    )


def _optional_seconds(value, name):
    return None if value is None else check_number(value, name, 0.0, 86400.0, "seconds")


@guard_memory
def validate_generated(pcm: Pcm, role: str, min_s=None, max_s=None) -> list:
    """Check freshly generated audio and return a list of :class:`Problem` (empty when it looks fine).

    ``role`` is one of song, host_break, id, spot or update (it only changes the wording).
    ``min_s`` / ``max_s`` are the shortest and longest acceptable lengths in seconds (None = no limit).
    The checks are cheap (no loudness measurement), so they are safe to run on every generated item.
    """
    require_pcm(pcm)
    if not isinstance(role, str) or role not in ROLES:
        raise AudioError("The kind of audio must be one of: " + ", ".join(ROLES) + ".", "invalid")
    low, high = _optional_seconds(min_s, "The shortest allowed length"), _optional_seconds(max_s, "The longest allowed length")
    if low is not None and high is not None and high < low:
        raise AudioError("The longest allowed length cannot be shorter than the shortest.", "invalid")
    label = ROLES[role]
    problems = []
    total, channels = pcm.frame_count, pcm.channels
    seconds = pcm.seconds
    scan = _scan(pcm, False) if total else None
    silent = total == 0 or to_db(scan.peak / FULL_SCALE) < SILENT_PEAK_DBFS or scan.silent_frames / total >= SILENT_RATIO
    if silent:
        problems.append(Problem("silent", f"The {label} came out silent: there is nothing to hear. Try generating it again."))
    if low is not None and seconds < low:
        problems.append(Problem("too_short", f"The {label} is only {format_duration(seconds)} long; it should be at "
                                f"least {format_duration(low)}. Try generating it again."))
    if high is not None and seconds > high:
        problems.append(Problem("too_long", f"The {label} is {format_duration(seconds)} long, longer than the "
                                f"{format_duration(high)} allowed. Try generating a shorter one."))
    if silent:
        return problems
    ratio = scan.rails / (total * channels)
    if ratio > CLIP_RATIO_LIMIT:
        problems.append(Problem("clipped", f"The {label} is distorted: about {ratio * 100:.1f}% of its sound is stuck "
                                "at the loudest possible level. Try generating it again."))
    offset = max(abs(s) for s in scan.sums) / total / FULL_SCALE
    if offset > DC_LIMIT:
        problems.append(Problem("dc_offset", f"The {label} has a steady shift away from the centre line "
                                f"({offset * 100:.1f}% of the maximum), which wastes volume and can cause clicks. "
                                "Try generating it again."))
    if _ends_abruptly(pcm, scan.peak):
        problems.append(Problem("truncated_tail", f"The {label} seems to stop abruptly, as if it was cut off. Try "
                                "generating it again, or fade out its end."))
    return problems
