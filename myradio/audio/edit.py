"""Editing: ``normalize``, ``fade``, ``trim_silence`` and ``concat`` (plus ``apply_gain``).

Every function returns a new immutable Pcm and never changes its input. Gain changes only ever
reduce when a ceiling is in the way: there is no limiter, compressor or clipping anywhere.
"""
from __future__ import annotations

import dataclasses
import math
from array import array
from dataclasses import dataclass
from itertools import repeat
from operator import add, mul
from typing import Optional

from .analysis import _scan, active_bounds
from .lanes import gain_plan, run_plan
from .loudness import loudness_lufs
from .pcm import (FLOOR_DB, FULL_SCALE, MAX_NOTES, MAX_PCM_BYTES, AudioError, Pcm, array16, bytes16, check_number,
                  guard_memory, require_pcm, to_db)

__all__ = ["normalize", "normalize_with_report", "NormalizeReport", "apply_gain", "fade", "trim_silence", "concat",
           "MAX_BOOST_DB", "MAX_FADE_MS", "MAX_CROSSFADE_MS"]

MAX_BOOST_DB = 40.0          # the most gain apply_gain / normalize will ever add
MAX_FADE_MS = 60_000.0
MAX_CROSSFADE_MS = 30_000.0
_GAIN_BITS = 30              # fixed-point precision of a gain
_GAIN_CHUNK = 1 << 20        # samples converted per step (keeps temporary memory small)
_RAMP_CHUNK = 1 << 16        # frames per step in fades and crossfades


# --------------------------------------------------------------------------- gain

def _scale_samples(frames: bytes, fixed: int, overs=None) -> bytes:
    """Multiply every 16-bit sample by ``fixed / 2**30`` (round to nearest) with the lane engine."""
    plan = gain_plan(fixed)
    out = bytearray(len(frames))
    step = _GAIN_CHUNK * 2
    for start in range(0, len(frames), step):
        chunk = frames[start:start + step]
        out[start:start + len(chunk)] = run_plan(chunk, 1, plan, len(chunk) // 2, count=32768, overs=overs)
    return bytes(out)


@guard_memory
def apply_gain(pcm: Pcm, gain_db: float) -> Pcm:
    """Change the level by ``gain_db`` (-120 to +40). Louder results saturate at full scale (and a note says
    how many samples were cut off), so prefer ``normalize`` when the aim is a target loudness."""
    require_pcm(pcm)
    gain_db = check_number(gain_db, "The volume change", -120.0, MAX_BOOST_DB, "decibels")
    if gain_db == 0.0 or not pcm.frames:
        return pcm
    fixed = int(math.floor(10.0 ** (gain_db / 20.0) * (1 << _GAIN_BITS)))
    overs = [0]
    result = Pcm(_scale_samples(pcm.frames, fixed, overs), pcm.rate, pcm.channels, pcm.notes)
    if overs[0]:
        result = result.with_notes(f"After the volume change, {overs[0]:,} sample(s) went past the loudest possible level "
                                   "and were cut off, which adds distortion. Use a smaller change, or normalize instead.")
    return result


@dataclass(frozen=True)
class NormalizeReport:
    """What ``normalize_with_report`` did. All numbers are finite."""

    applied: bool               # False when the audio was left untouched (silent or empty)
    input_lufs: float
    target_lufs: float
    gain_db: float              # gain actually applied (negative = quieter)
    expected_lufs: float        # input_lufs + gain_db, the loudness the result should have
    peak_before_dbfs: float
    peak_after_dbfs: float
    limited_by: Optional[str]   # None, "ceiling" (peak ceiling stopped the gain) or "boost_cap"
    message: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@guard_memory
def normalize_with_report(pcm: Pcm, target_lufs: float = -16.0, ceiling_dbfs: float = -1.0):
    """Like :func:`normalize` but also returns a :class:`NormalizeReport` as ``(pcm, report)``."""
    require_pcm(pcm)
    target = check_number(target_lufs, "The target loudness", -70.0, 0.0, "LUFS")
    ceiling = check_number(ceiling_dbfs, "The peak ceiling", -30.0, 0.0, "dBFS (0 is the loudest possible level)")
    if not pcm.frames:
        report = NormalizeReport(False, FLOOR_DB, target, 0.0, FLOOR_DB, FLOOR_DB, FLOOR_DB, None,
                                 "There is no sound to adjust, so it was left as it is.")
        return pcm, report
    peak = _scan(pcm, False).peak
    peak_db = to_db(peak / FULL_SCALE)
    measured = loudness_lufs(pcm)
    if peak == 0 or measured <= FLOOR_DB:
        report = NormalizeReport(False, measured, target, 0.0, measured, peak_db, peak_db, None,
                                 "The audio is too quiet to measure, so its level was left as it is.")
        return pcm.with_notes(report.message), report
    allowed = min(int(math.floor(FULL_SCALE * 10.0 ** (ceiling / 20.0))), 32767)   # highest sample we may produce
    if allowed < 1:
        raise AudioError("The peak ceiling is too low to leave any sound.", "invalid")
    ceiling_gain = 20.0 * math.log10(allowed / peak)
    wanted = target - measured
    gain, limited = wanted, None
    if gain > MAX_BOOST_DB:
        gain, limited = MAX_BOOST_DB, "boost_cap"
    if gain > ceiling_gain:
        gain, limited = ceiling_gain, "ceiling"
    fixed = int(math.floor(10.0 ** (gain / 20.0) * (1 << _GAIN_BITS)))
    while fixed > 0 and (peak * fixed + (1 << (_GAIN_BITS - 1))) >> _GAIN_BITS > allowed:   # exact safety net
        fixed -= 1
    applied = 20.0 * math.log10(fixed / (1 << _GAIN_BITS)) if fixed else FLOOR_DB
    out_peak = (peak * fixed + (1 << (_GAIN_BITS - 1))) >> _GAIN_BITS
    expected = measured + applied
    if limited == "ceiling":
        message = (f"The sound could not reach {target:.1f} LUFS without going above the {ceiling:.1f} dBFS peak "
                   f"ceiling, so its level was changed by {applied:+.1f} dB instead of {wanted:+.1f} dB and it should "
                   f"measure about {expected:.1f} LUFS.")
    elif limited == "boost_cap":
        message = (f"The sound is very quiet; it was raised by the largest allowed amount ({applied:+.1f} dB) and "
                   f"should measure about {expected:.1f} LUFS, short of {target:.1f} LUFS.")
    else:
        message = f"Level adjusted by {applied:+.1f} dB to {expected:.1f} LUFS."
    report = NormalizeReport(True, measured, target, applied, expected, peak_db, to_db(out_peak / FULL_SCALE),
                             limited, message)
    overs = [0]
    result = Pcm(_scale_samples(pcm.frames, fixed, overs), pcm.rate, pcm.channels, pcm.notes)
    if limited:
        result = result.with_notes(message)
    if overs[0]:                       # cannot happen while the ceiling logic above holds; say so rather than hide it
        result = result.with_notes(f"{overs[0]:,} sample(s) were cut off while setting the level, which adds distortion.")
    return result, report


def normalize(pcm: Pcm, target_lufs: float = -16.0, ceiling_dbfs: float = -1.0) -> Pcm:
    """Change the level so the gated loudness hits ``target_lufs`` without any sample passing ``ceiling_dbfs``.

    Only a constant gain is applied: no limiter, compressor or clipping. When the ceiling (not the
    target) stops the gain, the audio ends up quieter than the target and a plain-language note saying
    so is added to ``Pcm.notes``; ``normalize_with_report`` returns the same facts as numbers. Silent
    audio is returned unchanged (with a note).
    """
    return normalize_with_report(pcm, target_lufs, ceiling_dbfs)[0]


# --------------------------------------------------------------------------- fades, trim, concat

def _ramp_gains(first: int, last: int, count: int, channels: int, rising: bool):
    """Per-sample gains for frames ``first`` ..< ``last`` of a ``count``-frame ramp (0 up to (count-1)/count)."""
    if rising:
        per_frame = [i / count for i in range(first, last)]
    else:
        per_frame = [(count - 1 - i) / count for i in range(first, last)]
    if channels == 1:
        return per_frame
    gains = [0.0] * (2 * len(per_frame))
    gains[0::2] = per_frame
    gains[1::2] = per_frame
    return gains


def _faded(frames: bytes, count: int, channels: int, rising: bool) -> bytes:
    width = 2 * channels
    out = []
    for first in range(0, count, _RAMP_CHUNK):
        last = min(count, first + _RAMP_CHUNK)
        samples = array16(frames[first * width:last * width])
        gains = _ramp_gains(first, last, count, channels, rising)
        out.append(bytes16(array("h", map(round, map(mul, samples, gains)))))
    return b"".join(out)


@guard_memory
def fade(pcm: Pcm, in_ms: float = 0.0, out_ms: float = 0.0) -> Pcm:
    """Linear fade in over the first ``in_ms`` and fade out over the last ``out_ms``; the length never changes.

    The very first frame of a fade-in and the very last frame of a fade-out are silent. When the two
    fades together are longer than the audio they are shortened in proportion so they just fit.
    """
    require_pcm(pcm)
    in_ms = check_number(in_ms, "The fade-in length", 0.0, MAX_FADE_MS, "milliseconds")
    out_ms = check_number(out_ms, "The fade-out length", 0.0, MAX_FADE_MS, "milliseconds")
    total, width = pcm.frame_count, 2 * pcm.channels
    head = min(total, int(round(in_ms * pcm.rate / 1000.0)))
    tail = min(total, int(round(out_ms * pcm.rate / 1000.0)))
    if head + tail > total:
        scale = total / (head + tail)
        head = int(head * scale)
        tail = min(int(tail * scale), total - head)
    if head == 0 and tail == 0:
        return pcm
    frames = memoryview(pcm.frames)       # slices of a view are not copied; only the final join copies
    pieces = [_faded(frames[:head * width], head, pcm.channels, True) if head else b"",
              frames[head * width:(total - tail) * width],
              _faded(frames[(total - tail) * width:], tail, pcm.channels, False) if tail else b""]
    return Pcm(b"".join(pieces), pcm.rate, pcm.channels, pcm.notes)


@guard_memory
def trim_silence(pcm: Pcm, threshold_dbfs: float = -50.0, keep_ms: float = 60) -> Pcm:
    """Cut silence off both ends, keeping ``keep_ms`` of it on each side (or less if there is less).

    A sample counts as sound when it is louder than ``threshold_dbfs``. Silence inside the audio is
    never touched. Audio that is silent from end to end becomes empty (with a note saying so).
    """
    require_pcm(pcm)
    threshold = check_number(threshold_dbfs, "The silence threshold", -120.0, 0.0, "dBFS (0 is the loudest possible level)")
    keep = check_number(keep_ms, "The amount of silence to keep", 0.0, 60_000.0, "milliseconds")
    total, width = pcm.frame_count, 2 * pcm.channels
    if total == 0:
        return pcm
    first, last = active_bounds(pcm, threshold)
    if first is None:
        return Pcm(b"", pcm.rate, pcm.channels, pcm.notes).with_notes("The audio was silent from start to end, so "
                                                                   "nothing was kept.")
    margin = int(round(keep * pcm.rate / 1000.0))
    start, end = max(0, first - margin), min(total, last + 1 + margin)
    if start == 0 and end == total:
        return pcm
    return Pcm(pcm.frames[start * width:end * width], pcm.rate, pcm.channels, pcm.notes)


def _mix(tail: bytes, head: bytes, count: int, channels: int) -> bytes:
    """Equal-power crossfade of ``count`` frames: ``tail`` fades out while ``head`` fades in."""
    width = 2 * channels
    out = []
    for first in range(0, count, _RAMP_CHUNK):
        last = min(count, first + _RAMP_CHUNK)
        angles = [(i + 0.5) / count * (math.pi / 2.0) for i in range(first, last)]
        down, up = [math.cos(a) for a in angles], [math.sin(a) for a in angles]
        if channels == 2:
            both_down, both_up = [0.0] * (2 * len(down)), [0.0] * (2 * len(up))
            both_down[0::2] = both_down[1::2] = down
            both_up[0::2] = both_up[1::2] = up
            down, up = both_down, both_up
        a = array16(tail[first * width:last * width])
        b = array16(head[first * width:last * width])
        mixed = map(add, map(mul, a, down), map(mul, b, up))
        clamped = map(min, map(max, mixed, repeat(-32768.0)), repeat(32767.0))
        out.append(bytes16(array("h", map(round, clamped))))
    return b"".join(out)


@guard_memory
def concat(parts, crossfade_ms: float = 0) -> Pcm:
    """Join audio end to end, optionally overlapping neighbours with an equal-power crossfade.

    All parts must share one sample rate and channel count (convert them with ``to_station_format``
    first). With ``crossfade_ms`` > 0 each join overlaps by round(crossfade_ms * rate / 1000) frames,
    so the result is that much shorter per join; a part shorter than twice the overlap only overlaps by
    half its length, so crossfades never run into each other. The total may not exceed 100 MiB.
    """
    if not isinstance(parts, (list, tuple)) or not parts:
        raise AudioError("There is nothing to join: give at least one piece of audio.", "invalid")
    for part in parts:
        require_pcm(part, "Each part")
    first = parts[0]
    if any(p.rate != first.rate or p.channels != first.channels for p in parts):
        raise AudioError("These pieces of audio do not all use the same sample rate and channels. Convert them to "
                         "the station format before joining them.", "invalid")
    crossfade = check_number(crossfade_ms, "The crossfade length", 0.0, MAX_CROSSFADE_MS, "milliseconds")
    if sum(len(p.frames) for p in parts) > MAX_PCM_BYTES:
        raise AudioError("The joined audio would be longer than the station can use (about 10 minutes). Join fewer "
                         "pieces.", "too_large")
    notes = []
    for part in parts:
        for note in part.notes:
            if note not in notes:
                notes.append(note)
    notes = tuple(notes[-MAX_NOTES:])
    if len(parts) == 1:
        return first
    width = 2 * first.channels
    overlap = int(round(crossfade * first.rate / 1000.0))
    if overlap == 0:
        return Pcm(b"".join(p.frames for p in parts), first.rate, first.channels, notes)
    pieces = []
    consumed = 0                                  # frames at the start of the current part already used by the previous join
    for index, part in enumerate(parts):
        frames, count = memoryview(part.frames), part.frame_count
        if index + 1 < len(parts):
            nxt = parts[index + 1]
            join = min(overlap, count // 2, nxt.frame_count // 2)
            pieces.append(frames[consumed * width:(count - join) * width])
            if join:
                pieces.append(_mix(frames[(count - join) * width:], memoryview(nxt.frames)[:join * width], join, first.channels))
            consumed = join
        else:
            pieces.append(frames[consumed * width:])
    return Pcm(b"".join(pieces), first.rate, first.channels, notes)
