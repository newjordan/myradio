"""Integrated loudness in LUFS: a K-weighted, gated measurement in the style of ITU-R BS.1770 / EBU R128.

What is implemented
  * K-weighting: a high-shelf filter (+4 dB above about 2 kHz) followed by a 38 Hz high-pass. The
    filter shapes are the BS.1770 ones, built for any sample rate with the bilinear transform; at
    48 kHz they reproduce the coefficients printed in the standard.
  * Blocks of 400 ms that overlap by 75% (a 100 ms step). Each block's loudness is
    ``-0.691 + 10*log10(sum over channels of mean square)``.
  * Gating: blocks quieter than -70 LUFS are dropped, then blocks more than 10 LU below the average
    of what remains are dropped, and the integrated loudness is the power average of the rest.

What is approximated
  * Mono audio is measured as if the same signal played from both speakers (dual mono), because that is how the
    station plays it. Stereo channels both have weight 1.
  * Audio shorter than 400 ms is measured as a single block over the whole clip.
  * The result is always a finite number: silence reads as FLOOR_DB (-120.0).

The filter runs sample by sample in Python, so a four-minute stereo song takes a few seconds; it is
measured once per Pcm and remembered on the object.
"""
from __future__ import annotations

import cmath
import math
from array import array
from functools import lru_cache

from .pcm import BIG_ENDIAN, FLOOR_DB, FULL_SCALE, Pcm, require_pcm

__all__ = ["loudness_lufs", "k_coefficients", "k_weighting_gain_db", "ABSOLUTE_GATE_LUFS", "RELATIVE_GATE_LU"]

ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = -10.0
_OFFSET = -0.691          # the BS.1770 calibration constant
_BLOCK_STEPS = 4          # a 400 ms block is four 100 ms steps


@lru_cache(maxsize=16)
def k_coefficients(rate: int):
    """(b0, b1, b2, a1, a2, r1, r2): the shelf's numerator/denominator and the high-pass's denominator.

    The high-pass numerator is always (1, -2, 1). The analog prototype parameters below reproduce the
    published 48 kHz coefficients of BS.1770 to about 1e-12.
    """
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10.0 ** (gain_db / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    b0 = (vh + vb * k / q + k * k) / a0
    b1 = 2.0 * (k * k - vh) / a0
    b2 = (vh - vb * k / q + k * k) / a0
    a1 = 2.0 * (k * k - 1.0) / a0
    a2 = (1.0 - k / q + k * k) / a0
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    d = 1.0 + k / q + k * k
    return b0, b1, b2, a1, a2, 2.0 * (k * k - 1.0) / d, (1.0 - k / q + k * k) / d


def k_weighting_gain_db(rate: int, frequency: float) -> float:
    """Gain of the K-weighting filter at ``frequency`` Hz (for checks and documentation)."""
    b0, b1, b2, a1, a2, r1, r2 = k_coefficients(rate)
    z1 = cmath.exp(-2j * math.pi * frequency / rate)
    z2 = z1 * z1
    shelf = (b0 + b1 * z1 + b2 * z2) / (1.0 + a1 * z1 + a2 * z2)
    high_pass = (1.0 - 2.0 * z1 + z2) / (1.0 + r1 * z1 + r2 * z2)
    return 20.0 * math.log10(abs(shelf * high_pass))


def _step_energies(frames: bytes, channels: int, rate: int):
    """K-weighted energy of every 100 ms step: a list of (sum of squared samples over all channels, frames)."""
    b0, b1, b2, a1, a2, r1, r2 = k_coefficients(rate)
    step = max(1, round(rate * 0.1))
    width = 2 * channels
    total = len(frames) // width
    stereo = channels == 2
    energies = []
    ls1 = ls2 = lh1 = lh2 = 0.0     # left (or mono) filter state
    rs1 = rs2 = rh1 = rh2 = 0.0     # right filter state
    position = 0
    while position < total:
        count = min(step, total - position)
        block = array("h")
        block.frombytes(frames[position * width:(position + count) * width])
        if BIG_ENDIAN:  # pragma: no cover
            block.byteswap()
        left = block[0::2] if stereo else block
        acc = 0.0
        for x in left:
            y = b0 * x + ls1
            ls1 = b1 * x - a1 * y + ls2
            ls2 = b2 * x - a2 * y
            z = y + lh1
            lh1 = -2.0 * y - r1 * z + lh2
            lh2 = y - r2 * z
            acc += z * z
        if stereo:
            for x in block[1::2]:
                y = b0 * x + rs1
                rs1 = b1 * x - a1 * y + rs2
                rs2 = b2 * x - a2 * y
                z = y + rh1
                rh1 = -2.0 * y - r1 * z + rh2
                rh2 = y - r2 * z
                acc += z * z
        else:
            acc *= 2.0              # dual mono
        energies.append((acc, count))
        position += count
    return energies, step


def _gate(energies, step: int, frames: int) -> float:
    whole = [e for e, c in energies if c == step]
    if len(whole) >= _BLOCK_STEPS:
        blocks = [sum(whole[i:i + _BLOCK_STEPS]) / (_BLOCK_STEPS * step) for i in range(len(whole) - _BLOCK_STEPS + 1)]
    elif frames:
        blocks = [sum(e for e, _ in energies) / frames]
    else:
        return FLOOR_DB
    scale = FULL_SCALE * FULL_SCALE
    absolute = 10.0 ** ((ABSOLUTE_GATE_LUFS - _OFFSET) / 10.0) * scale
    kept = [z for z in blocks if z > absolute]
    if not kept:
        return FLOOR_DB
    relative = sum(kept) / len(kept) * 10.0 ** (RELATIVE_GATE_LU / 10.0)
    kept = [z for z in kept if z > relative]
    return max(FLOOR_DB, _OFFSET + 10.0 * math.log10(sum(kept) / len(kept) / scale))


def loudness_lufs(pcm: Pcm) -> float:
    """Gated integrated loudness of ``pcm`` in LUFS (FLOOR_DB when there is nothing above the -70 LUFS gate)."""
    require_pcm(pcm)
    cached = pcm._memo.get("lufs")
    if cached is not None:
        return cached
    energies, step = _step_energies(pcm.frames, pcm.channels, pcm.rate)
    value = _gate(energies, step, pcm.frame_count)
    pcm._memo["lufs"] = value
    return value
