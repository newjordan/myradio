"""Core audio types for myradio: ``AudioError``, ``Pcm`` and a few shared helpers.

Everything in ``myradio.audio`` works on :class:`Pcm`, an immutable block of interleaved signed
16-bit little-endian samples. Python standard library only; nothing here needs ``audioop``
(removed in Python 3.13) or any third-party package.
"""
from __future__ import annotations

import functools
import math
import sys
from array import array
from dataclasses import dataclass, field

__all__ = [
    "AudioError", "Pcm", "STATION_RATE", "STATION_CHANNELS", "MIN_RATE", "MAX_RATE",
    "MAX_PCM_BYTES", "FULL_SCALE", "FLOOR_DB",
]

STATION_RATE = 44100            # the station clock plays 44.1 kHz ...
STATION_CHANNELS = 2            # ... stereo ...
MIN_RATE = 8000                 # lowest sample rate the toolkit accepts
MAX_RATE = 384000               # highest sample rate the toolkit accepts
MAX_PCM_BYTES = 100 * 1024 * 1024   # same ceiling as the station's own import limit
FULL_SCALE = 32768.0            # dBFS reference: a sample of +-32768 is 0 dBFS
FLOOR_DB = -120.0               # every dB figure is clamped here, so silence never becomes -inf / NaN
MAX_NOTES = 12                  # a Pcm remembers at most this many plain-language notes

BIG_ENDIAN = sys.byteorder == "big"
_INT32_CODE = "i" if array("i").itemsize == 4 else "l"
if array(_INT32_CODE).itemsize != 4 or array("h").itemsize != 2:  # pragma: no cover - no supported platform has this
    raise ImportError("This computer's Python has no 16-bit and 32-bit integer array types.")


class AudioError(ValueError):
    """A problem with audio data or an audio request.

    The message is written for a person: it says what happened and what to do next. ``code`` is a
    short machine-readable reason (``"truncated"``, ``"not_wav"``, ``"too_large"`` ...) for code
    that wants to react differently; callers may ignore it. ``detail`` is an optional technical
    remark for logs and support (never shown to the person running the station).
    """

    def __init__(self, message: str, code: str = "invalid", detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def __reduce__(self):  # keep code and detail when the error crosses a process boundary
        return (self.__class__, (str(self), self.code, self.detail))


def guard_memory(function):
    """Turn "the computer ran out of memory" into a plain AudioError for a public function."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except MemoryError:
            raise AudioError("There is not enough free memory to process this audio. Close other programs or use a "
                             "shorter recording.", "too_large") from None
    return wrapper


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_number(value, label: str, low: float, high: float, unit: str = "") -> float:
    """Return ``value`` as a finite float inside [low, high] or raise a plain AudioError naming ``label``."""
    suffix = f" {unit}" if unit else ""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < low or value > high:
        raise AudioError(f"{label} must be a number from {low:g} to {high:g}{suffix}.", "invalid")
    return float(value)


def format_duration(seconds: float) -> str:
    """Short human wording such as '12.3 seconds' or '3 minutes 5 seconds'."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    whole = int(round(seconds))
    minutes, rest = divmod(whole, 60)
    word = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {word} {rest} seconds" if rest else f"{minutes} {word}"


def to_db(amplitude_ratio: float) -> float:
    """20*log10 of a non-negative amplitude ratio, floored at FLOOR_DB (never -inf or NaN)."""
    if not amplitude_ratio > 0.0:
        return FLOOR_DB
    return max(FLOOR_DB, 20.0 * math.log10(amplitude_ratio))


def power_to_db(power_ratio: float) -> float:
    """10*log10 of a non-negative power ratio, floored at FLOOR_DB."""
    if not power_ratio > 0.0:
        return FLOOR_DB
    return max(FLOOR_DB, 10.0 * math.log10(power_ratio))


@dataclass(frozen=True, repr=False)
class Pcm:
    """Immutable interleaved signed 16-bit little-endian audio.

    ``frames`` is the raw bytes (left, right, left, right ... for stereo), ``rate`` the sample rate
    in Hz and ``channels`` is 1 or 2. ``notes`` holds plain-language remarks about anything that was
    repaired or limited while the audio was read or processed; it does not take part in equality.
    """

    frames: bytes
    rate: int
    channels: int
    notes: tuple = field(default=(), compare=False)

    def __post_init__(self):
        frames = self.frames
        if type(frames) is not bytes:
            if not isinstance(frames, (bytes, bytearray, memoryview)):
                raise AudioError("Audio data must be given as bytes.", "invalid")
            try:
                frames = bytes(frames)
            except (TypeError, ValueError):
                raise AudioError("Audio data must be given as bytes.", "invalid") from None
            object.__setattr__(self, "frames", frames)
        if not is_int(self.channels) or self.channels not in (1, 2):
            raise AudioError("The station can only use mono or stereo audio.", "channels")
        if not is_int(self.rate) or not MIN_RATE <= self.rate <= MAX_RATE:
            raise AudioError(f"The sample rate must be a whole number from {MIN_RATE:,} to {MAX_RATE:,} Hz.", "rate")
        if len(frames) % (2 * self.channels):
            raise AudioError("The audio data ends in the middle of a sample and cannot be used.", "damaged")
        notes = self.notes
        if not isinstance(notes, tuple) or len(notes) > MAX_NOTES or any(type(n) is not str for n in notes):
            try:
                notes = tuple(str(n) for n in notes)[-MAX_NOTES:]
            except TypeError:
                notes = ()
            object.__setattr__(self, "notes", notes)
        object.__setattr__(self, "_memo", {})  # cache for expensive measurements (loudness, statistics)

    @property
    def frame_count(self) -> int:
        return len(self.frames) // (2 * self.channels)

    @property
    def seconds(self) -> float:
        return self.frame_count / self.rate

    def with_notes(self, *extra: str) -> "Pcm":
        """The same audio with more notes appended (older notes are dropped past MAX_NOTES)."""
        if not extra:
            return self
        return Pcm(self.frames, self.rate, self.channels, (self.notes + tuple(extra))[-MAX_NOTES:])

    def __repr__(self) -> str:
        kind = "stereo" if self.channels == 2 else "mono"
        return f"Pcm({kind}, {self.rate} Hz, {self.seconds:.3f} s, {len(self.frames):,} bytes)"


def require_pcm(value, name: str = "pcm") -> Pcm:
    if not isinstance(value, Pcm):
        raise AudioError(f"{name} must be audio produced by this toolkit (a Pcm).", "invalid")
    return value


def view16(frames: bytes):
    """Read-only sequence of the signed 16-bit samples in little-endian ``frames`` (no copy on
    little-endian machines). Iterating or slicing it yields plain ints."""
    if not frames:
        return ()
    if not BIG_ENDIAN:
        return memoryview(frames).cast("h")
    swapped = array("h")  # pragma: no cover - big-endian hosts only
    swapped.frombytes(frames)
    swapped.byteswap()
    return swapped


def array16(frames) -> array:
    """A mutable native-endian ``array('h')`` copy of little-endian ``frames`` (any bytes-like object)."""
    out = array("h")
    out.frombytes(frames)
    if BIG_ENDIAN:
        out.byteswap()  # pragma: no cover
    return out


def bytes16(samples: array) -> bytes:
    """Little-endian bytes of a native-endian ``array('h')``."""
    if BIG_ENDIAN:  # pragma: no cover
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def new_pcm(source: Pcm, frames, rate: int = None, channels: int = None, notes: tuple = None) -> Pcm:
    """A Pcm derived from ``source`` (rate/channels/notes default to the source's)."""
    return Pcm(frames, source.rate if rate is None else rate,
               source.channels if channels is None else channels,
               source.notes if notes is None else notes)
