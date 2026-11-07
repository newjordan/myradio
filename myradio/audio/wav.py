"""WAV reading and writing that is safe on hostile files.

``read_wav`` understands integer sound of 8, 16, 24 and 32 bits, 32/64-bit floating point sound,
and the WAVE_FORMAT_EXTENSIBLE wrapper, in mono or stereo. It never trusts a size written in the
file: every length is checked against what the file really holds before anything is read, the size
limit is applied to the file (and to the converted audio) before the sound is read, and every
problem becomes an ``AudioError`` with a plain-language message.
"""
from __future__ import annotations

import math
import os
import stat
import struct
import tempfile
from array import array
from itertools import repeat
from operator import add, mul, rshift

from .._platform import O_BINARY
from .pcm import (BIG_ENDIAN, MAX_RATE, MIN_RATE, AudioError, Pcm, _INT32_CODE, bytes16, format_duration,
                  guard_memory, is_int, require_pcm)

__all__ = ["read_wav", "write_wav", "write_wav_file", "MAX_WAV_BYTES"]

MAX_WAV_BYTES = 104857600           # 100 MiB, the station's own import limit
_ABSOLUTE_MAX_BYTES = 1 << 30       # a caller may raise max_bytes, but never past 1 GiB
_MAX_FMT_BYTES = 1024               # a real "fmt " chunk is 16, 18 or 40 bytes
_MAX_HEADER_CHUNKS = 256            # how many chunk headers we walk before giving up on finding the sound
_CONVERT_BYTES = 1 << 20            # source bytes converted per step, so temporary memory stays small
_GUID_TAIL = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
_XOR80 = bytes(i ^ 0x80 for i in range(256))
_FLOAT_SAFE_HIGH = 32767.0 / 32768.0  # at or below this a float sample cannot round past +32767
_FORMAT_NAMES = {
    0x0002: "Microsoft ADPCM", 0x0006: "A-law", 0x0007: "mu-law", 0x0011: "IMA ADPCM",
    0x0031: "GSM", 0x0050: "MP2", 0x0055: "MP3", 0x00FF: "AAC", 0x0160: "Windows Media",
    0x0161: "Windows Media", 0x0162: "Windows Media", 0x2000: "AC-3", 0xF1AC: "FLAC",
}


# --------------------------------------------------------------------------- sources

class _Source:
    """Random access to the bytes of a WAV file, whether it lives in memory or on disk."""

    __slots__ = ("size", "_view", "_file")

    def __init__(self, size: int, view=None, fileobj=None):
        self.size, self._view, self._file = size, view, fileobj

    def read(self, offset: int, count: int) -> bytes:
        if offset < 0 or count <= 0 or offset >= self.size:
            return b""
        count = min(count, self.size - offset)
        if self._view is not None:
            return bytes(self._view[offset:offset + count])
        try:
            self._file.seek(offset)
            return self._file.read(count)
        except (OSError, ValueError):
            raise AudioError("The audio file could not be read. Check that it is still there and try again.",
                             "unreadable") from None

    def block(self, offset: int, count: int):
        """Bytes-like view of the data (no copy when the file is already in memory)."""
        if self._view is not None:
            return self._view[offset:offset + count]
        return self.read(offset, count)

    def close(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass


def _megabytes(count: int) -> str:
    return f"{count / 1048576:.0f} MB" if count >= 1048576 else f"{count:,} bytes"


def _open_source(source, limit: int) -> _Source:
    if isinstance(source, (bytes, bytearray, memoryview)):
        try:
            view = memoryview(source)
            if view.ndim != 1 or view.itemsize != 1 or not view.contiguous:
                view = memoryview(bytes(source))
        except (TypeError, ValueError):
            raise AudioError("The audio must be given as a file path or as the file's bytes.", "invalid") from None
        if len(view) > limit:
            raise AudioError(f"This audio is {_megabytes(len(view))}, more than the {_megabytes(limit)} the station "
                             "accepts. Use a shorter recording.", "too_large")
        return _Source(len(view), view=view)
    if isinstance(source, (str, os.PathLike)):
        return _open_path(source, limit)
    raise AudioError("The audio must be given as a file path or as the file's bytes.", "invalid")


def _open_path(path, limit: int) -> _Source:
    flags = os.O_RDONLY | O_BINARY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise AudioError("The audio file could not be found. Check the name and try again.", "missing") from None
    except (OSError, ValueError):
        raise AudioError("The audio file could not be opened. Check that it exists and that this computer is "
                         "allowed to read it.", "unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AudioError("That is not an ordinary audio file (it may be a folder or a special file).", "unreadable")
        if info.st_size > limit:
            raise AudioError(f"This audio file is {_megabytes(info.st_size)}, more than the {_megabytes(limit)} the "
                             "station accepts. Use a shorter recording.", "too_large")
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return _Source(info.st_size, fileobj=handle)


# --------------------------------------------------------------------------- container

class _Fmt:
    __slots__ = ("kind", "bits", "channels", "rate", "align")

    def __init__(self, kind, bits, channels, rate, align):
        self.kind, self.bits, self.channels, self.rate, self.align = kind, bits, channels, rate, align


def _plausible_id(tag: bytes) -> bool:
    return len(tag) == 4 and all(0x20 <= b < 0x7F for b in tag)


def _parse_fmt(body: bytes) -> _Fmt:
    tag, channels, rate, _byte_rate, align, bits = struct.unpack_from("<HHIIHH", body)
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the real encoding is the first bytes of a GUID
        if len(body) < 40 or struct.unpack_from("<H", body, 16)[0] < 22:
            raise AudioError("This WAV file's format information is damaged. Save or export it again.", "damaged")
        valid_bits = struct.unpack_from("<H", body, 18)[0]
        guid = body[24:40]
        if guid[2:] != _GUID_TAIL:
            raise AudioError("This WAV file uses a sound encoding the station does not know. Save it as a standard "
                             "WAV (16-bit) and try again.", "unsupported")
        tag = struct.unpack_from("<H", guid)[0]
        if not 0 < valid_bits <= bits:
            raise AudioError("This WAV file's format information is damaged. Save or export it again.", "damaged")
    if tag == 1:
        kind = "pcm"
    elif tag == 3:
        kind = "float"
    else:
        name = _FORMAT_NAMES.get(tag, "unknown")
        raise AudioError("This WAV file is compressed or uses a special sound format that the station cannot read "
                         "directly. Save or export it as a standard WAV (16-bit) and try again.", "unsupported",
                         f"format number 0x{tag:04x} ({name})")
    if channels == 0:
        raise AudioError("This WAV file says it has no audio channels, so it cannot be used. Export it again.", "damaged")
    if channels > 2:
        raise AudioError(f"This audio has {channels} channels. The station can use mono or stereo; export it as "
                         "stereo and try again.", "channels")
    if rate == 0:
        raise AudioError("This WAV file does not say how fast it plays (its sample rate is 0), so it cannot be used. "
                         "Export it again.", "rate")
    if not MIN_RATE <= rate <= MAX_RATE:
        raise AudioError(f"This audio plays at {rate:,} samples per second. The station can use {MIN_RATE:,} to "
                         f"{MAX_RATE:,}; convert it to 44,100 or 48,000 and try again.", "rate")
    if (kind == "pcm" and bits not in (8, 16, 24, 32)) or (kind == "float" and bits not in (32, 64)):
        raise AudioError(f"This WAV file stores each sample in {bits} bits, which the station cannot read. Save it as "
                         "a standard 16-bit WAV and try again.", "unsupported")
    if align != channels * bits // 8:
        raise AudioError("This WAV file's format information contradicts itself, so it cannot be trusted. Save or "
                         "export it again.", "damaged")
    return _Fmt(kind, bits, channels, rate, align)


def _locate(src: _Source):
    """Walk the container and return (fmt, offset of the sound, number of sound bytes the file claims)."""
    head = src.read(0, 12)
    if len(head) < 12 or head[:4] not in (b"RIFF", b"RIFX", b"RF64", b"BW64") or head[8:12] != b"WAVE":
        raise AudioError("This is not a WAV audio file. Save or export the audio as a WAV file and try again.", "not_wav")
    if head[:4] != b"RIFF":
        raise AudioError("This WAV file uses a layout the station cannot read (unusual byte order or a very large "
                         "file). Save it as a standard WAV and try again.", "unsupported")
    fmt = None
    pos = 12
    for _ in range(_MAX_HEADER_CHUNKS):
        header = src.read(pos, 8)
        if len(header) < 8:
            break
        tag, size = struct.unpack("<4sI", header)
        body = pos + 8
        if tag == b"fmt ":
            if fmt is not None:
                raise AudioError("This WAV file's format information appears twice. Save or export it again.", "damaged")
            if size < 16 or size > _MAX_FMT_BYTES or body + size > src.size:
                raise AudioError("This WAV file's format information is damaged. Save or export it again.", "damaged")
            fmt = _parse_fmt(src.read(body, size))
        elif tag == b"data":
            if fmt is None:
                raise AudioError("This WAV file's sound comes before its format information, so it cannot be "
                                 "trusted. Save or export it again.", "damaged")
            return fmt, body, size
        nxt = body + size
        if size & 1:  # chunks are padded to an even length; tolerate writers that forgot the pad byte
            if not _plausible_id(src.read(nxt + 1, 4)) and _plausible_id(src.read(nxt, 4)):
                pass
            else:
                nxt += 1
        if nxt > src.size:
            break
        pos = nxt
    if fmt is None:
        raise AudioError("This WAV file has no format information, so it cannot be used. Save or export it again.", "damaged")
    raise AudioError("This WAV file has no sound in it, or its layout is too damaged to find the sound.", "damaged")


# --------------------------------------------------------------------------- conversion

class _Tally:
    __slots__ = ("bad", "over")

    def __init__(self):
        self.bad = self.over = 0


def _u8_to_16(block) -> bytes:
    data = bytes(block)
    out = bytearray(2 * len(data))
    out[1::2] = data.translate(_XOR80)
    return bytes(out)


def _i32_array_to_16(values: array) -> bytes:
    """Round native-endian 32-bit integers to 16 bits (nearest, ties up) and saturate."""
    shifted = map(rshift, map(add, values, repeat(0x8000)), repeat(16))
    if max(values) >= 0x7FFF8000:  # the top few values would round to +32768
        shifted = map(min, shifted, repeat(32767))
    return bytes16(array("h", shifted))


def _i24_to_16(block) -> bytes:
    data = memoryview(block)
    lanes = bytearray(4 * (len(data) // 3))   # each 24-bit sample placed in the top three bytes of a 32-bit lane
    lanes[1::4], lanes[2::4], lanes[3::4] = bytes(data[0::3]), bytes(data[1::3]), bytes(data[2::3])
    values = array(_INT32_CODE)
    values.frombytes(lanes)
    if BIG_ENDIAN:  # pragma: no cover - the lanes above are little-endian
        values.byteswap()
    return _i32_array_to_16(values)


def _i32_to_16(block) -> bytes:
    values = array(_INT32_CODE)
    values.frombytes(bytes(block))
    if BIG_ENDIAN:  # pragma: no cover
        values.byteswap()
    return _i32_array_to_16(values)


def _make_float_converter(bits: int, tally: _Tally):
    code = "f" if bits == 32 else "d"

    def convert(block) -> bytes:
        values = array(code)
        values.frombytes(bytes(block))
        if BIG_ENDIAN:  # pragma: no cover
            values.byteswap()
        total = sum(values)  # NaN or infinity anywhere makes this non-finite
        if math.isfinite(total) and max(values) <= _FLOAT_SAFE_HIGH and min(values) >= -1.0:
            return bytes16(array("h", map(round, map(mul, values, repeat(32768.0)))))
        out = array("h", bytes(2 * len(values)))
        bad = over = 0
        for index, x in enumerate(values):
            if x != x:
                bad += 1                      # NaN stays silence
            elif x > 1.0:
                out[index] = 32767
                if x == math.inf:
                    bad += 1
                else:
                    over += 1
            elif x < -1.0:
                out[index] = -32768
                if x == -math.inf:
                    bad += 1
                else:
                    over += 1
            else:
                scaled = round(x * 32768.0)
                out[index] = 32767 if scaled > 32767 else scaled
        tally.bad += bad
        tally.over += over
        return bytes16(out)

    return convert


def _decode(src: _Source, fmt: _Fmt, offset: int, frames: int, tally: _Tally) -> bytes:
    nbytes = frames * fmt.align
    if fmt.kind == "pcm" and fmt.bits == 16:
        data = src.read(offset, nbytes)
        if len(data) != nbytes:
            raise _cut_short(frames, len(data) // fmt.align, fmt.rate)
        return data
    if fmt.kind == "float":
        convert = _make_float_converter(fmt.bits, tally)
    else:
        convert = {8: _u8_to_16, 24: _i24_to_16, 32: _i32_to_16}[fmt.bits]
    step = max(1, _CONVERT_BYTES // fmt.align)
    out = bytearray(frames * 2 * fmt.channels)
    done = 0
    while done < frames:
        count = min(step, frames - done)
        block = src.block(offset + done * fmt.align, count * fmt.align)
        if len(block) != count * fmt.align:
            raise _cut_short(frames, done + len(block) // fmt.align, fmt.rate)
        pcm = convert(block)
        out[done * 2 * fmt.channels:(done + count) * 2 * fmt.channels] = pcm
        done += count
    return bytes(out)


def _cut_short(wanted: int, have: int, rate: int) -> AudioError:
    return AudioError(f"The audio file looks cut short: it should hold {format_duration(wanted / rate)} of sound but "
                      f"only {format_duration(have / rate)} is there. If it was still being saved, wait for it to "
                      "finish; otherwise create it again.", "truncated")


# --------------------------------------------------------------------------- public API

@guard_memory
def read_wav(source, max_bytes: int = MAX_WAV_BYTES) -> Pcm:
    """Read a WAV file (a path, or the file's bytes) into a :class:`Pcm`.

    Anything that is not trustworthy raises :class:`AudioError` instead: files that are too big,
    cut short, damaged, silent-by-lie (sizes that do not match the file), unsupported encodings,
    more than two channels or a sample rate outside 8,000 to 384,000 Hz. The size limit is checked
    against the file before its sound is read, and against the converted audio too.

    Repairs that do not change what is heard (for example non-numbers in floating point sound being
    replaced by silence) are listed in ``Pcm.notes``.
    """
    if not is_int(max_bytes) or not 1 <= max_bytes <= _ABSOLUTE_MAX_BYTES:
        raise AudioError("The size limit must be a whole number of bytes between 1 and 1 GiB.", "invalid")
    src = _open_source(source, max_bytes)
    try:
        fmt, offset, declared = _locate(src)
        available = src.size - offset
        if declared == 0xFFFFFFFF:
            raise AudioError("This WAV file does not say how long its sound is (it looks like a recording that was "
                             "still being made). Save or export it again.", "damaged")
        if declared > available:
            raise _cut_short(declared // fmt.align, available // fmt.align, fmt.rate)
        frames = declared // fmt.align
        if frames == 0:
            raise AudioError("This WAV file contains no sound.", "empty")
        out_bytes = frames * 2 * fmt.channels
        if declared > max_bytes or out_bytes > max_bytes:
            raise AudioError(f"This audio is longer than the {_megabytes(max_bytes)} the station accepts "
                             f"(about {format_duration(frames / fmt.rate)}). Use a shorter recording.", "too_large")
        tally = _Tally()
        data = _decode(src, fmt, offset, frames, tally)
    finally:
        src.close()
    notes = []
    leftover = declared - frames * fmt.align
    if leftover:
        notes.append(f"The last {leftover} byte(s) did not make up a whole sample and were ignored.")
    if tally.bad:
        notes.append(f"{tally.bad:,} sample value(s) in the file were not valid numbers and were replaced with silence "
                     "or the nearest loudest level.")
    if tally.over:
        notes.append(f"{tally.over:,} sample(s) were louder than the highest possible level and were limited to it.")
    return Pcm(data, fmt.rate, fmt.channels, tuple(notes))


@guard_memory
def write_wav(pcm: Pcm) -> bytes:
    """The audio as a standard 16-bit WAV file (the layout the station's own importer expects)."""
    require_pcm(pcm)
    size = len(pcm.frames)
    if size > 0xFFFFFFFF - 36:
        raise AudioError("This audio is too long to save as one WAV file.", "too_large")
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + size, b"WAVE", b"fmt ", 16, 1, pcm.channels, pcm.rate,
                         pcm.rate * pcm.channels * 2, pcm.channels * 2, 16, b"data", size)
    return header + pcm.frames


@guard_memory
def write_wav_file(pcm: Pcm, path) -> int:
    """Save the audio as a WAV file at ``path`` (readable only by its owner) and return its size.

    The file is written under a temporary name in the same folder and then renamed, so a reader
    never sees a half-written file and a failed save leaves any existing file untouched.
    """
    require_pcm(pcm)
    data = write_wav(pcm)
    try:
        target = os.fspath(path)
        folder = os.path.dirname(os.path.abspath(target))
        fd, temp = tempfile.mkstemp(prefix=".audio-", suffix=".tmp", dir=folder)
    except (OSError, TypeError, ValueError):
        raise AudioError("The audio could not be saved there. Check that the folder exists and can be written to.",
                         "unwritable") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise AudioError("The audio could not be saved. Check that the disk has room and the folder can be written to.",
                         "unwritable") from None
    return len(data)
