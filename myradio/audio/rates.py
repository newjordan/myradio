"""Sample-rate conversion: ``resample`` and ``to_station_format`` (module ``rates``, so the name
``myradio.audio.resample`` is only ever the function).

Two qualities are offered.

``"good"`` (the default) is a windowed-sinc polyphase filter: a Kaiser-windowed sinc low-pass
(32 zero crossings each side of the output position, beta 8.6, about 87 dB of attenuation) with
one prepared set of taps for every distinct fractional position the conversion visits. The tables
are built once per ratio and cached. It is the default because everything the station plays is
44.1 kHz, so a conversion error (images, aliasing, level changes) would be heard on air every
time, and the measured cost is small (a few seconds for a four-minute stereo song, see the contract
document).

``"fast"`` is plain linear interpolation. It does no anti-aliasing, so it is only suitable for
previews and for audio that has no content near the top of its band.

Both run on the fixed-point lane engine in ``lanes.py``; the tap tables are rounded to 22 bits and
normalised to unit gain at every fractional position.
"""
from __future__ import annotations

import math
from fractions import Fraction
from functools import lru_cache

from .lanes import LanePlan, make_plan, run_plan
from .pcm import (MAX_PCM_BYTES, MAX_RATE, MIN_RATE, STATION_RATE, AudioError, Pcm, format_duration, guard_memory,
                  is_int, require_pcm)

__all__ = ["resample", "to_station_format", "build_plan", "QUALITIES", "HALF_WIDTH", "KAISER_BETA", "MAX_PHASES"]

QUALITIES = ("good", "fast")
HALF_WIDTH = 32          # zero crossings of the cutoff sinc on each side (so 64 taps per phase when upsampling)
KAISER_BETA = 8.6        # window shape: about 87 dB stopband attenuation
MAX_PHASES = 1024        # most distinct filter phases a conversion may use (see _ratio)
MAX_TAP_WORK = 5_000_000_000   # output samples x taps; a safety net so one call stays within about a minute
_QBITS = 22


def _bessel_i0(x: float) -> float:
    total = term = 1.0
    quarter = x * x / 4.0
    k = 1
    while True:
        term *= quarter / (k * k)
        total += term
        if term < total * 1e-17:
            return total
        k += 1


def _sinc(x: float) -> float:
    if x == 0.0:
        return 1.0
    px = math.pi * x
    return math.sin(px) / px


@lru_cache(maxsize=4)
def build_plan(M: int, L: int, quality: str) -> LanePlan:
    """Prepared taps for converting by the ratio ``M/L`` (input rate over output rate), cached."""
    scale = 1 << _QBITS
    if quality == "fast":
        count, off = 2, 0

        def real_taps(fraction):
            return [1.0 - fraction, fraction]
    else:
        keep = min(1.0, L / M)                         # output Nyquist relative to input Nyquist
        half = max(HALF_WIDTH, math.ceil(HALF_WIDTH / keep))
        count, off = 2 * half, half - 1
        attenuation = KAISER_BETA / 0.1102 + 8.7
        transition = (attenuation - 7.95) / (14.36 * count)   # cycles per input sample
        cutoff = 0.5 * keep - 0.5 * transition                # stopband starts at the output Nyquist
        norm = _bessel_i0(KAISER_BETA)

        def real_taps(fraction):
            values = []
            for k in range(count):
                d = k - off - fraction
                u = d / half
                window = _bessel_i0(KAISER_BETA * math.sqrt(1.0 - u * u)) / norm if abs(u) < 1.0 else 1.0 / norm
                values.append(2.0 * cutoff * _sinc(2.0 * cutoff * d) * window)
            total = sum(values)
            return [v / total for v in values]

    rows = [None] * L
    for r in range(L):
        if r and rows[L - r] is not None:
            rows[r] = rows[L - r][::-1]       # fractions f and 1-f give mirror-image taps, so build only half of them
            continue
        values = real_taps(((r * M) % L) / L)
        ints = [round(v * scale) for v in values]
        ints[max(range(count), key=lambda i: abs(ints[i]))] += scale - sum(ints)   # exact unit gain at DC
        rows[r] = ints
    return make_plan(M, L, off, rows, qbits=_QBITS, bits=48)


def _ratio(rate_in: int, rate_out: int):
    """(M, L, relative error) with M/L = rate_in/rate_out, using at most MAX_PHASES phases.

    Every ordinary rate pair (8, 11.025, 12, 16, 22.05, 24, 32, 44.1, 48, 88.2, 96, 192 kHz ...) reduces to an
    exact small fraction. A rate with an unusual ratio is converted with the nearest fraction that
    has at most MAX_PHASES phases; the speed error is below 0.05% in the worst case.
    """
    exact = Fraction(rate_in, rate_out)
    if exact.denominator <= MAX_PHASES:
        return exact.numerator, exact.denominator, 0.0
    near = exact.limit_denominator(MAX_PHASES)
    return near.numerator, near.denominator, float(abs(near - exact) / exact)


def _check_target(rate) -> int:
    if not is_int(rate) or not MIN_RATE <= rate <= MAX_RATE:
        raise AudioError(f"The target sample rate must be a whole number from {MIN_RATE:,} to {MAX_RATE:,} Hz.", "rate")
    return rate


def _too_long(seconds: float) -> AudioError:
    return AudioError(f"This audio ({format_duration(seconds)}) is too long to convert in one piece. Cut it into "
                      "shorter parts first.", "too_large")


@guard_memory
def resample(pcm: Pcm, rate: int, quality: str = "good") -> Pcm:
    """Convert to ``rate`` Hz, keeping the channels and the duration (length is round(frames*new/old))."""
    require_pcm(pcm)
    _check_target(rate)
    if quality not in QUALITIES:
        raise AudioError("The conversion quality must be 'good' or 'fast'.", "invalid")
    if rate == pcm.rate:
        return pcm
    n_in = pcm.frame_count
    n_out = (n_in * rate + pcm.rate // 2) // pcm.rate
    if n_out * 2 * pcm.channels > MAX_PCM_BYTES:
        raise _too_long(n_in / pcm.rate)
    if n_in == 0 or n_out == 0:
        return Pcm(b"", rate, pcm.channels, pcm.notes)
    M, L, error = _ratio(pcm.rate, rate)
    plan = build_plan(M, L, quality)
    if n_out * pcm.channels * plan.taps > MAX_TAP_WORK:
        raise _too_long(n_in / pcm.rate)
    overs = [0]
    out = run_plan(pcm.frames, pcm.channels, plan, n_out, overs=overs)
    notes = pcm.notes
    if error > 1e-6:
        notes = notes + (f"Converting from {pcm.rate:,} Hz used the nearest simple speed ratio, accurate to about "
                         f"{error * 1e6:.0f} parts per million (far below anything audible).",)
    if overs[0]:
        notes = notes + (f"After the sample rate change, {overs[0]:,} sample(s) went past the loudest possible level and "
                         "were cut off, which adds a little distortion. Lower the level before converting, or convert "
                         "first and set the level afterwards.",)
    return Pcm(bytes(out), rate, pcm.channels, notes)


def _duplicate_channel(frames: bytes) -> bytes:
    out = bytearray(len(frames) * 2)
    low, high = frames[0::2], frames[1::2]
    out[0::4], out[1::4], out[2::4], out[3::4] = low, high, low, high
    return bytes(out)


@guard_memory
def to_station_format(pcm: Pcm) -> Pcm:
    """44.1 kHz stereo, the one format the station plays. Mono is played from both speakers."""
    require_pcm(pcm)
    n_out = (pcm.frame_count * STATION_RATE + pcm.rate // 2) // pcm.rate
    if n_out * 4 > MAX_PCM_BYTES:
        raise _too_long(pcm.frame_count / pcm.rate)
    out = pcm if pcm.rate == STATION_RATE else resample(pcm, STATION_RATE)
    if out.channels == 2:
        return out
    return Pcm(_duplicate_channel(out.frames), out.rate, 2, out.notes)
