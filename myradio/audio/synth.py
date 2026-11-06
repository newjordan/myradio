"""Synthetic test signals: sine waves, square waves, constants, noise and silence.

These exist so tests and fixtures can build audio with known properties. They are plain
mathematics, not music or speech, and they are not output from any generation model. Nothing in
the station should present them as generated programming.
"""
from __future__ import annotations

import math
import random
from array import array

from .pcm import BIG_ENDIAN, AudioError, Pcm, bytes16, check_number, is_int

__all__ = ["sine", "square", "constant", "noise", "silence"]

_MAX_SECONDS = 600.0


def _count(seconds: float, rate: int) -> int:
    seconds = check_number(seconds, "The length", 0.0, _MAX_SECONDS, "seconds")
    if not is_int(rate):
        raise AudioError("The sample rate must be a whole number.", "rate")
    return int(round(seconds * rate))


def _amplitude(level_dbfs: float) -> int:
    level = check_number(level_dbfs, "The level", -120.0, 0.0, "dBFS")
    return min(32767, int(round(32768.0 * 10.0 ** (level / 20.0))))


def _pack(mono: array, rate: int, channels: int) -> Pcm:
    data = bytes16(mono)
    if channels == 2:
        both = bytearray(len(data) * 2)
        low, high = data[0::2], data[1::2]
        both[0::4], both[1::4], both[2::4], both[3::4] = low, high, low, high
        data = bytes(both)
    return Pcm(data, rate, channels)


def sine(freq_hz: float, seconds: float, rate: int = 44100, channels: int = 2, level_dbfs: float = -20.0,
         phase: float = 0.0) -> Pcm:
    """A sine wave whose peak is ``level_dbfs`` below full scale (both channels identical)."""
    count = _count(seconds, rate)
    freq = check_number(freq_hz, "The frequency", 0.0, rate / 2.0, "Hz")
    amplitude = _amplitude(level_dbfs)
    step = 2.0 * math.pi * freq / rate
    period = count
    if float(freq).is_integer():          # a whole-number frequency repeats exactly every rate/gcd samples
        period = min(count, rate // math.gcd(int(freq), rate))
    sin = math.sin
    values = array("h", [int(round(amplitude * sin(step * i + phase))) for i in range(period)])
    if period < count:
        values = values * (count // period) + values[:count % period]
    return _pack(values, rate, channels)


def square(freq_hz: float, seconds: float, rate: int = 44100, channels: int = 2, level_dbfs: float = -6.0) -> Pcm:
    """A square wave of the given peak level."""
    count = _count(seconds, rate)
    freq = check_number(freq_hz, "The frequency", 0.0, rate / 2.0, "Hz")
    amplitude = _amplitude(level_dbfs)
    low = -32768 if amplitude == 32767 else -amplitude        # a full-scale square wave touches both rails
    period = rate / freq if freq else float("inf")
    values = array("h", [amplitude if (i % period) < period / 2.0 else low for i in range(count)])
    return _pack(values, rate, channels)


def constant(value: int, seconds: float, rate: int = 44100, channels: int = 2) -> Pcm:
    """Every sample equal to ``value`` (a pure DC offset when it is not zero)."""
    count = _count(seconds, rate)
    if not is_int(value) or not -32768 <= value <= 32767:
        raise AudioError("The value must be a whole number from -32768 to 32767.", "invalid")
    return _pack(array("h", [value]) * count, rate, channels)


def noise(seconds: float, rate: int = 44100, channels: int = 2, level_dbfs: float = -20.0, seed: int = 0) -> Pcm:
    """Uniform white noise between plus and minus the given peak level (the same seed gives the same noise)."""
    count = _count(seconds, rate)
    amplitude = _amplitude(level_dbfs)
    span = 2 * amplitude + 1
    source = random.Random(seed)

    def one_channel() -> array:
        raw = array("H")
        raw.frombytes(source.randbytes(2 * count))
        if BIG_ENDIAN:  # pragma: no cover - keeps the noise identical on every machine
            raw.byteswap()
        return array("h", [(v * span >> 16) - amplitude for v in raw])

    mono = one_channel()
    if channels == 2:  # two independent channels
        both = array("h", bytes(4 * count))
        both[0::2], both[1::2] = mono, one_channel()
        return Pcm(bytes16(both), rate, 2)
    return _pack(mono, rate, channels)


def silence(seconds: float, rate: int = 44100, channels: int = 2) -> Pcm:
    """Digital silence."""
    count = _count(seconds, rate)
    return Pcm(bytes(count * 2 * channels), rate, channels)
