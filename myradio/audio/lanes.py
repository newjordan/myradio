"""Fast fixed-point filtering in pure Python, using big integers as "lanes".

Pure-Python per-sample loops cost about a microsecond per output sample. Python's big integers
do their arithmetic in C, so many samples can be packed side by side in one integer (one "lane"
of 48 or 64 bits each) and multiplied by a filter tap in a single operation. This module uses
that to apply a polyphase FIR filter, or a plain gain, to a whole run of samples at once.

The result is exact integer arithmetic (no floating point): every output sample is

    clamp(floor((sum(tap * input_sample) + 2**(qbits-1)) / 2**qbits))

evaluated over zero-padded input. Tests compare it bit-for-bit against a naive loop.

How the lanes work, in short
  * Each input sample is stored as an unsigned lane value ``sample + 32768`` (offset binary), so a
    lane never goes negative on its own. Taps may be negative; a large constant ("bias") is added
    to every lane before it is read back so no lane ever borrows from its neighbour.
  * An output stream whose rate is ``L/M`` times the input rate is split into ``L`` interleaved
    phases. Phase ``r`` of period ``j`` reads input frames ``M*j + e`` for fixed offsets ``e``, so
    each phase is a sum of ``tap * (every M-th input sample)`` vectors.
  * The sum is shifted down by ``qbits`` (fixed-point scale), checked for overshoot and, only
    when needed, clamped to the 16-bit range with lane-wise mask arithmetic.
"""
from __future__ import annotations

from functools import lru_cache

from .pcm import AudioError

__all__ = ["LanePlan", "make_plan", "run_plan", "gain_plan", "DEFAULT_LANES"]

DEFAULT_LANES = 4096     # lanes per big integer; 1,024 to 16,384 measured within a few percent of each other
WINDOW_BYTES = 16_000_000   # most memory the packed input windows of one segment may use
_XOR80 = bytes(i ^ 0x80 for i in range(256))


class LanePlan:
    """A prepared filter: for each of ``L`` output phases, the (input offset, integer tap) pairs."""

    __slots__ = ("L", "M", "bits", "qbits", "entries", "corr", "evict_at", "emin", "emax", "taps")

    def __init__(self, L, M, bits, qbits, entries, corr, taps):
        self.L, self.M, self.bits, self.qbits = L, M, bits, qbits
        self.entries, self.corr, self.taps = entries, corr, taps
        self.evict_at = 2 * taps + 64
        self.emin = min((row[0][0] for row in entries if row), default=0)
        self.emax = max((row[-1][0] for row in entries if row), default=0)


def make_plan(M: int, L: int, off: int, rows, qbits: int = 22, bits: int = 48) -> LanePlan:
    """Build a plan from integer tap rows.

    ``rows[r]`` holds the taps for output phase ``r`` (0 <= r < L) as integers scaled by
    ``2**qbits``; tap ``k`` multiplies input frame ``M*j + (r*M)//L + k - off``. A row that sums to
    exactly ``2**qbits`` has unit gain at DC.
    """
    if len(rows) != L:
        raise AudioError("Internal error: the resampling table has the wrong size.", "internal")
    entries, corr = [], []
    half = 1 << (qbits - 1)
    limit_u = 1 << (bits - qbits - 2)
    taps = 0
    for r, row in enumerate(rows):
        taps = max(taps, len(row))
        c = (r * M) // L
        entries.append(tuple((c + k - off, h) for k, h in enumerate(row) if h))
        pos = sum(h for h in row if h > 0)
        neg = -sum(h for h in row if h < 0)
        offset = 32768 * ((1 << qbits) - (pos - neg))     # makes the lane carry exactly sample + 32768
        corr.append(offset)
        lane_low = (1 << (bits - 2)) + half + offset - neg * 65535
        lane_high = (1 << (bits - 2)) + half + offset + pos * 65535
        swing = ((pos + neg) * 32768 >> qbits) + 65536
        if lane_low <= 0 or lane_high >= (1 << bits) or swing >= limit_u:
            raise AudioError("Internal error: a filter would overflow its working space.", "internal")
    return LanePlan(L, M, bits, qbits, tuple(entries), tuple(corr), taps)


def gain_plan(gain_fixed: int, qbits: int = 30, bits: int = 64) -> LanePlan:
    """A one-tap plan multiplying every sample by ``gain_fixed / 2**qbits`` (0 <= gain <= 128)."""
    if not 0 <= gain_fixed <= (128 << qbits):
        raise AudioError("Internal error: the gain is outside the supported range.", "internal")
    return make_plan(1, 1, 0, [[gain_fixed]], qbits=qbits, bits=bits)


class _Constants:
    """Lane-wide constants for a given lane width and count (all big integers)."""

    __slots__ = ("ones", "maskw", "bias", "chk_mask", "chk_expect", "lo", "hi", "guard", "shift", "vmask", "lane_bits")

    def __init__(self, bits: int, qbits: int, count: int):
        nb = bits // 8
        self.lane_bits = bits
        vw = bits - qbits                        # bits of each lane that survive the shift
        self.ones = ones = int.from_bytes((b"\x01" + bytes(nb - 1)) * count, "little")
        self.maskw = ones * ((1 << vw) - 1)
        self.bias = ones * ((1 << (bits - 2)) + (1 << (qbits - 1)))
        self.chk_mask = ones * ((1 << (vw - 16)) - 1)
        self.chk_expect = ones * (1 << (vw - 18))
        self.lo = ones * (1 << (vw - 2))
        self.hi = ones * ((1 << (vw - 2)) + 65535)
        self.guard = ones * (1 << (vw + 2))
        self.shift = vw + 2
        self.vmask = (1 << vw) - 1

    def clamp(self, wide: int, keep_lanes: int = 0):
        """Clamp every lane of ``wide`` into [lo, hi] using only whole-integer operations.

        Returns ``(clamped, count)`` where ``count`` is how many lanes had to be changed (only the first
        ``keep_lanes`` lanes are counted when that is given).
        """
        ones, vmask, shift = self.ones, self.vmask, self.shift
        above_lo = (((wide | self.guard) - self.lo) >> shift) & ones          # 1 in every lane that is >= lo
        mask = above_lo * vmask
        wide = (wide & mask) | (self.lo & (mask ^ self.maskw))
        below_hi = (((self.hi | self.guard) - wide) >> shift) & ones          # 1 in every lane that is <= hi
        mask = below_hi * vmask
        wide = (wide & mask) | (self.hi & (mask ^ self.maskw))
        changed = (ones ^ above_lo) | (ones ^ below_hi)
        if keep_lanes:
            changed &= (1 << (self.lane_bits * keep_lanes)) - 1
        return wide, changed.bit_count()


@lru_cache(maxsize=12)
def _constants(bits: int, qbits: int, count: int) -> _Constants:
    return _Constants(bits, qbits, count)


def _window(frames, ch_low: int, stride: int, n_in: int, first: int, M: int, count: int, nb: int) -> int:
    """Pack input frames first, first+M, ... (``count`` of them) of one channel into one big integer.

    Positions outside the signal are silence. The result holds ``sample + 32768`` in each lane.
    """
    step = stride * M
    last = first + M * (count - 1)
    if first >= 0 and last < n_in:
        base = first * stride + ch_low
        stop = base + step * (count - 1) + 1
        low = frames[base:stop:step]
        high = frames[base + 1:stop + 1:step]
    else:
        low, high = bytearray(count), bytearray(count)
        start = 0 if first >= 0 else (M - 1 - first) // M
        end = count if last < n_in else (0 if first >= n_in else (n_in - 1 - first) // M + 1)
        end = min(end, count)
        if end > start:
            base = (first + M * start) * stride + ch_low
            stop = base + step * (end - start - 1) + 1
            low[start:end] = frames[base:stop:step]
            high[start:end] = frames[base + 1:stop + 1:step]
    lanes = bytearray(nb * count)
    lanes[0::nb] = low
    lanes[1::nb] = high.translate(_XOR80)
    return int.from_bytes(lanes, "little")


def run_plan(frames, channels: int, plan: LanePlan, n_out: int, count: int = DEFAULT_LANES, overs=None) -> bytearray:
    """Filter interleaved 16-bit little-endian ``frames`` and return ``n_out`` frames of output.

    Output that would pass the 16-bit range is clamped; when ``overs`` is a one-item list, the number of
    output samples that had to be clamped is added to ``overs[0]``.
    """
    stride = 2 * channels
    n_in = len(frames) // stride
    out = bytearray(n_out * stride)
    if n_out <= 0 or n_in == 0:
        return out
    L, M, qbits = plan.L, plan.M, plan.qbits
    nb = plan.bits // 8
    count = max(1, min(count, max(256, WINDOW_BYTES // (nb * (plan.taps + M + 1)))))   # bound the live windows
    periods = (n_out + L - 1) // L
    entries, corr = plan.entries, plan.corr
    for channel in range(channels):
        ch_low = 2 * channel
        for j0 in range(0, periods, count):
            lanes = min(count, periods - j0)
            const = _constants(plan.bits, qbits, lanes)
            windows = {}
            first_input = j0 * M
            for r in range(L):
                start = r + L * j0
                if start >= n_out:
                    break
                produced = min(lanes, (n_out - start + L - 1) // L)
                acc = const.bias
                if corr[r]:
                    acc = acc + const.ones * corr[r]
                row = entries[r]
                if len(windows) > plan.evict_at and row:
                    cutoff = row[0][0]
                    for key in [k for k in windows if k < cutoff]:
                        del windows[key]
                for e, h in row:
                    w = windows.get(e)
                    if w is None:
                        w = windows[e] = _window(frames, ch_low, stride, n_in, first_input + e, M, lanes, nb)
                    acc += w * h
                wide = (acc >> qbits) & const.maskw
                if ((wide >> 16) & const.chk_mask) != const.chk_expect:
                    wide, hit = const.clamp(wide, produced if produced != lanes else 0)
                    if overs is not None:
                        overs[0] += hit
                raw = wide.to_bytes(nb * lanes, "little")
                low, high = raw[0::nb], raw[1::nb].translate(_XOR80)
                if produced != lanes:
                    low, high = low[:produced], high[:produced]
                base = start * stride + ch_low
                step = L * stride
                stop = base + step * (produced - 1) + 1
                out[base:stop:step] = low
                out[base + 1:stop + 1:step] = high
    return out
