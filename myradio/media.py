"""Cheap, bounded PCM preflight for playout; never decode or scan audio on the clock."""
import os
import stat
import struct
import wave

from ._platform import O_BINARY, O_NOFOLLOW, O_NONBLOCK


class MediaError(ValueError):
    pass


class Reader:
    def __init__(self, source, audio):
        self.source, self.audio = source, audio

    def readframes(self, count):
        return self.audio.readframes(count)

    def close(self):
        try:
            self.audio.close()
        finally:
            self.source.close()


def open_media(path, duration):
    """Return an owned reader only for complete, native PCM at the expected duration.

    O_NONBLOCK avoids hanging on a substituted FIFO; O_NOFOLLOW rejects symlinks. Windows has neither
    flag (and no FIFOs in the file system), so there a symbolic link is refused before opening.
    Validate the opened descriptor, not a path that might change before open.
    WAV parsing requires our canonical import header; no unbounded chunk or full-file scan.
    """
    source = audio = None
    try:
        if not O_NOFOLLOW and os.path.islink(path):
            raise MediaError()
        fd = os.open(path, os.O_RDONLY | O_BINARY | O_NONBLOCK | O_NOFOLLOW)
        source = os.fdopen(fd, 'rb')
        size = os.fstat(source.fileno())
        if not stat.S_ISREG(size.st_mode) or not 44 <= size.st_size <= 100 * 1024 * 1024 + 4096:
            raise MediaError()
        # Imports are always written by our canonical 44-byte WAV writer. Require
        # that layout before invoking wave: a replaced file with millions of tiny
        # ancillary chunks must not make header parsing monopolize the clock.
        header = source.read(44)
        if len(header) != 44:
            raise MediaError()
        riff, riff_size, form, fmt, fmt_size, codec, channels, rate, byte_rate, align, bits, data, data_size = struct.unpack('<4sI4s4sIHHIIHH4sI', header)
        if ((riff, form, fmt, fmt_size, codec, channels, rate, byte_rate, align, bits, data)
                != (b'RIFF', b'WAVE', b'fmt ', 16, 1, 2, 44100, 176400, 4, 16, b'data')
                or data_size % 4 or riff_size != data_size + 36 or size.st_size != data_size + 44):
            raise MediaError()
        source.seek(0)
        audio = wave.open(source, 'rb')
        frames = audio.getnframes()
        if ((audio.getframerate(), audio.getsampwidth(), audio.getnchannels(), audio.getcomptype()) != (44100, 2, 2, 'NONE')
                or frames < 4410 or frames * 4 > 100 * 1024 * 1024
                or abs(frames / 44100 - duration) > 1 / 44100
                or source.tell() + frames * 4 > size.st_size):
            raise MediaError()
        return Reader(source, audio)
    except (OSError, ValueError, wave.Error, EOFError):
        if audio is not None:
            audio.close()
        if source is not None:
            source.close()
        raise MediaError('Audio is missing, unreadable, truncated or no longer matches its imported PCM format. Repair the original file or remove and reimport it, then retry.') from None
