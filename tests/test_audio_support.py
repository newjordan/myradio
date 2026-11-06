"""Shared helpers for the test_audio_*.py files. Contains no tests.

``build_wav`` writes WAV files with ``struct`` so the tests do not depend on the reader under test.
"""
import math
import struct
from array import array

from myradio.audio import Pcm


def chunk(tag, payload, pad=True):
    """A RIFF chunk. Odd-length payloads get their pad byte unless ``pad`` is false."""
    out = tag + struct.pack('<I', len(payload)) + payload
    return out + (b'\x00' if len(payload) % 2 and pad else b'')


def fmt_payload(tag, channels, rate, bits, align=None, byte_rate=None, extensible=None, valid_bits=None):
    align = channels * bits // 8 if align is None else align
    byte_rate = rate * align if byte_rate is None else byte_rate
    body = struct.pack('<HHIIHH', 0xFFFE if extensible else tag, channels, rate, byte_rate, align, bits)
    if extensible:
        guid = struct.pack('<H', tag) + extensible
        body += struct.pack('<HHI', 22, valid_bits or bits, 3 if channels == 2 else 4) + guid
    return body


STANDARD_GUID_TAIL = b'\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71'


def encode(samples, kind, bits):
    """Encode integer 16-bit-scale samples (-32768..32767) in the given sample format."""
    if kind == 'float':
        code = 'f' if bits == 32 else 'd'
        return struct.pack('<%d%s' % (len(samples), code), *[s / 32768.0 for s in samples])
    if bits == 8:
        return bytes(((s >> 8) + 128) & 0xFF for s in samples)
    if bits == 16:
        return struct.pack('<%dh' % len(samples), *samples)
    if bits == 24:
        return b''.join((s * 256).to_bytes(3, 'little', signed=True) for s in samples)
    if bits == 32:
        return struct.pack('<%di' % len(samples), *[s * 65536 for s in samples])
    raise ValueError(bits)


def build_wav(samples, rate=44100, channels=1, bits=16, kind='pcm', extensible=False, before=b'', after=b'',
              riff_size=None, data_size=None, fmt_payload_override=None, data_pad=True, tag=None):
    """A complete WAV file. ``samples`` are interleaved ints on the 16-bit scale (floats are written as s/32768)."""
    format_tag = tag if tag is not None else (3 if kind == 'float' else 1)
    ext = STANDARD_GUID_TAIL if extensible else None
    fmt = fmt_payload_override if fmt_payload_override is not None else fmt_payload(format_tag, channels, rate, bits, extensible=ext)
    data = encode(samples, kind, bits)
    declared = len(data) if data_size is None else data_size
    body = b'WAVE' + chunk(b'fmt ', fmt) + before + b'data' + struct.pack('<I', declared) + data
    if data_pad and len(data) % 2:
        body += b'\x00'
    body += after
    size = len(body) if riff_size is None else riff_size
    return b'RIFF' + struct.pack('<I', size) + body


def tone(n, channels=1, freq=440.0, rate=44100, amp=8000):
    """Deterministic interleaved ints: the same sine in every channel."""
    one = [int(round(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)]
    out = []
    for value in one:
        out.extend([value] * channels)
    return out


def samples_of(pcm):
    out = array('h')
    out.frombytes(pcm.frames)
    return list(out)


def pcm_from_samples(values, rate=44100, channels=2):
    return Pcm(array('h', values).tobytes(), rate, channels)


def amplitude_at(pcm, freq, start_s=0.2, seconds=0.1, channel=0):
    """Peak amplitude of the component at ``freq`` Hz, measured over an exact window (use frequencies whose
    period divides ``seconds`` so there is no leakage)."""
    values = samples_of(pcm)
    step = pcm.channels
    first = int(round(start_s * pcm.rate))
    count = int(round(seconds * pcm.rate))
    xs = [values[(first + i) * step + channel] for i in range(count)]
    w = 2.0 * math.pi * freq / pcm.rate
    re = sum(x * math.cos(w * i) for i, x in enumerate(xs))
    im = sum(x * math.sin(w * i) for i, x in enumerate(xs))
    return 2.0 * math.hypot(re, im) / count


def zero_crossings(values):
    """How many times the signal changes sign (zero counts as non-negative)."""
    return sum(1 for a, b in zip(values, values[1:]) if (a < 0) != (b < 0))


def db(ratio):
    return 20.0 * math.log10(ratio) if ratio > 0 else -999.0
