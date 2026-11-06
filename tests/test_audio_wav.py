"""WAV reading and writing: a zoo of valid formats, round trips and hostile files.

Every WAV here is built with ``struct`` (see test_audio_support.py), never with the code under test.
"""
import os
import random
import stat
import struct
import tempfile
import time
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

import myradio.audio as audio
from myradio.audio import AudioError, Pcm, read_wav, write_wav, write_wav_file
from myradio.audio import wav as wav_module

try:
    from test_audio_support import STANDARD_GUID_TAIL, build_wav, chunk, fmt_payload, samples_of, tone
except ImportError:  # run as tests.<module> from the repository root
    from tests.test_audio_support import STANDARD_GUID_TAIL, build_wav, chunk, fmt_payload, samples_of, tone

RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000, 96000)
FORMATS = (('pcm', 8), ('pcm', 16), ('pcm', 24), ('pcm', 32), ('float', 32), ('float', 64))


def expect_error(test, data, code=None, **kwargs):
    with test.assertRaises(AudioError) as caught:
        read_wav(data, **kwargs)
    if code is not None:
        test.assertEqual(caught.exception.code, code, str(caught.exception))
    return caught.exception


class ZooTests(unittest.TestCase):
    def test_every_format_channel_count_and_rate_round_trips(self):
        for kind, bits in FORMATS:
            for channels in (1, 2):
                for rate in RATES:
                    with self.subTest(kind=kind, bits=bits, channels=channels, rate=rate):
                        # multiples of 256 so even the 8-bit file holds the values exactly
                        values = [(v // 256) * 256 for v in tone(400, channels, 300.0, rate, 12000)]
                        pcm = read_wav(build_wav(values, rate, channels, bits, kind))
                        self.assertEqual((pcm.rate, pcm.channels, pcm.frame_count), (rate, channels, 400))
                        self.assertEqual(samples_of(pcm), values)
                        self.assertEqual(pcm.notes, ())

    def test_extensible_header_variants(self):
        values = [(v // 256) * 256 for v in tone(300, 2, 500.0, 48000, 9000)]
        for kind, bits in (('pcm', 16), ('pcm', 24), ('pcm', 32), ('float', 32)):
            with self.subTest(kind=kind, bits=bits):
                data = build_wav(values, 48000, 2, bits, kind, extensible=True)
                self.assertEqual(samples_of(read_wav(data)), values)
        # fewer valid bits than the container (20 valid bits in 24) is fine
        tag = 1
        fmt = fmt_payload(tag, 2, 48000, 24, extensible=STANDARD_GUID_TAIL, valid_bits=20)
        self.assertEqual(samples_of(read_wav(build_wav(values, 48000, 2, 24, fmt_payload_override=fmt))), values)

    def test_24_and_32_bit_rounding_is_nearest_with_saturation(self):
        rnd = random.Random(99)
        v24 = [rnd.randint(-8388608, 8388607) for _ in range(2000)] + [8388607, -8388608, 0x7FFF80, 0x7FFF7F, 128, 127, -128, -129]
        raw = b''.join(v.to_bytes(3, 'little', signed=True) for v in v24)
        file24 = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(1, 1, 44100, 24)) + b'data' + struct.pack('<I', len(raw)) + raw
        got = samples_of(read_wav(file24))
        self.assertEqual(got, [max(-32768, min(32767, (v + 128) >> 8)) for v in v24])
        v32 = [rnd.randint(-2**31, 2**31 - 1) for _ in range(2000)] + [2**31 - 1, -2**31, 0x7FFF8000, 0x7FFF7FFF, 0x8000, 0x7FFF]
        raw = struct.pack('<%di' % len(v32), *v32)
        file32 = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(1, 1, 44100, 32)) + b'data' + struct.pack('<I', len(raw)) + raw
        got = samples_of(read_wav(file32))
        self.assertEqual(got, [max(-32768, min(32767, (v + 32768) >> 16)) for v in v32])

    def test_8_bit_is_unsigned_and_centred(self):
        raw = bytes([0, 128, 255, 127, 129])
        file8 = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(1, 1, 8000, 8)) + chunk(b'data', raw)
        self.assertEqual(samples_of(read_wav(file8)), [-32768, 0, 32512, -256, 256])

    def test_float_values_outside_the_range_are_clamped_and_reported(self):
        nan = float('nan')
        inf = float('inf')
        for code in ('f', 'd'):
            with self.subTest(code=code):
                values = [0.5, nan, inf, -inf, 2.5, -3.0, 1.0, -1.0, 0.25, 0.99999]
                raw = struct.pack('<%d%s' % (len(values), code), *values)
                bits = 32 if code == 'f' else 64
                data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(3, 1, 44100, bits)) + chunk(b'data', raw)
                pcm = read_wav(data)
                self.assertEqual(samples_of(pcm), [16384, 0, 32767, -32768, 32767, -32768, 32767, -32768, 8192, 32767])
                self.assertEqual(len(pcm.notes), 2)
                self.assertIn('3', pcm.notes[0])           # NaN, +inf, -inf
                self.assertIn('2', pcm.notes[1])           # 2.5 and -3.0 were louder than the maximum
        quiet = struct.pack('<4f', 0.1, -0.1, 0.0, 0.5)
        data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(3, 1, 44100, 32)) + chunk(b'data', quiet)
        self.assertEqual(read_wav(data).notes, ())

    def test_float_nan_flood_stays_finite_and_fast(self):
        count = 200_000
        raw = struct.pack('<f', float('nan')) * count
        data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(3, 1, 44100, 32)) + chunk(b'data', raw)
        started = time.perf_counter()
        pcm = read_wav(data)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(set(pcm.frames), {0})
        self.assertEqual(len(pcm.notes), 1)


class RandomSoundDataTests(unittest.TestCase):
    def test_random_bytes_as_sound_never_break_a_conversion(self):
        rnd = random.Random(31)
        for kind, bits in FORMATS:
            for channels in (1, 2):
                align = channels * bits // 8
                for frames in (1, 2, 33, 5000):
                    with self.subTest(kind=kind, bits=bits, channels=channels, frames=frames):
                        raw = rnd.randbytes(frames * align)
                        fmt = fmt_payload(3 if kind == 'float' else 1, channels, 44100, bits)
                        data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt) + chunk(b'data', raw)
                        pcm = read_wav(data)
                        self.assertEqual((pcm.frame_count, pcm.channels), (frames, channels))
                        self.assertEqual(len(pcm.frames), frames * channels * 2)


class FormatRejectionTests(unittest.TestCase):
    def test_compressed_and_unknown_encodings_are_rejected_politely(self):
        values = tone(200)
        for tag in (2, 6, 7, 0x11, 0x55, 0x1234):
            with self.subTest(tag=tag):
                error = expect_error(self, build_wav(values, tag=tag), 'unsupported')
                self.assertNotIn('PCM', str(error))
        with self.subTest('float of the wrong size'):
            expect_error(self, build_wav(values, 44100, 1, 16, 'float'), 'unsupported')

    def test_extensible_with_unknown_or_broken_subformat(self):
        values = tone(200)
        bad_tail = b'\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x72'
        fmt = fmt_payload(1, 1, 44100, 16, extensible=bad_tail)
        expect_error(self, build_wav(values, fmt_payload_override=fmt), 'unsupported')
        fmt = fmt_payload(0x7777, 1, 44100, 16, extensible=STANDARD_GUID_TAIL)       # unknown format number in the GUID
        expect_error(self, build_wav(values, fmt_payload_override=fmt), 'unsupported')
        fmt = fmt_payload(1, 1, 44100, 16, extensible=STANDARD_GUID_TAIL, valid_bits=24)   # more valid bits than container
        expect_error(self, build_wav(values, fmt_payload_override=fmt), 'damaged')
        short = struct.pack('<HHIIHH', 0xFFFE, 1, 44100, 88200, 2, 16)                # extensible but no extension block
        expect_error(self, build_wav(values, fmt_payload_override=short), 'damaged')
        no_room = fmt_payload(1, 1, 44100, 16, extensible=STANDARD_GUID_TAIL)
        broken = no_room[:16] + struct.pack('<H', 0) + no_room[18:]                    # extension size zero
        expect_error(self, build_wav(values, fmt_payload_override=broken), 'damaged')

    def test_absurd_channel_counts(self):
        values = tone(200)
        for channels in (0, 3, 6, 255, 65535):
            with self.subTest(channels=channels):
                fmt = struct.pack('<HHIIHH', 1, channels, 44100, 176400, 4, 16)
                error = expect_error(self, build_wav(values, fmt_payload_override=fmt))
                self.assertIn(error.code, ('channels', 'damaged'))
                self.assertNotIn('Traceback', str(error))
        error = expect_error(self, build_wav(values, 44100, 1, 16, fmt_payload_override=struct.pack('<HHIIHH', 1, 6, 44100, 0, 12, 16)), 'channels')
        self.assertIn('6 channels', str(error))

    def test_bad_sample_rates(self):
        values = tone(200)
        for rate in (0, 1, 7999, 384001, 0xFFFFFFFF):
            with self.subTest(rate=rate):
                fmt = struct.pack('<HHIIHH', 1, 1, rate, rate * 2 & 0xFFFFFFFF, 2, 16)
                expect_error(self, build_wav(values, fmt_payload_override=fmt), 'rate')
        for rate in (8000, 384000):
            fmt = struct.pack('<HHIIHH', 1, 1, rate, rate * 2, 2, 16)
            self.assertEqual(read_wav(build_wav(values, fmt_payload_override=fmt)).rate, rate)

    def test_unsupported_sample_sizes_and_contradicting_block_size(self):
        values = tone(200)
        for bits in (0, 4, 12, 20, 48):
            with self.subTest(bits=bits):
                fmt = struct.pack('<HHIIHH', 1, 1, 44100, 88200, max(1, bits // 8), bits)
                expect_error(self, build_wav(values, fmt_payload_override=fmt), 'unsupported')
        lying = struct.pack('<HHIIHH', 1, 2, 44100, 176400, 2, 16)       # stereo 16-bit must have block size 4
        expect_error(self, build_wav(values, fmt_payload_override=lying), 'damaged')

    def test_not_a_wav_file(self):
        expect_error(self, b'', 'not_wav')
        expect_error(self, b'RIFF', 'not_wav')
        expect_error(self, b'RIFF\x00\x00\x00\x00WAVx', 'not_wav')
        expect_error(self, b'\x89PNG\r\n\x1a\n' + bytes(100), 'not_wav')
        expect_error(self, b'ID3\x03' + bytes(200), 'not_wav')
        good = build_wav(tone(100))
        expect_error(self, b'RIFX' + good[4:], 'unsupported')
        expect_error(self, b'RF64' + good[4:], 'unsupported')


class LyingHeaderTests(unittest.TestCase):
    def setUp(self):
        self.values = tone(1000, 2, 440.0, 44100, 9000)

    def build(self, **kwargs):
        return build_wav(self.values, 44100, 2, 16, **kwargs)

    def test_riff_size_is_advisory(self):
        for riff in (0, 4, 36, 0xFFFFFFFF, 10 ** 6):
            with self.subTest(riff=riff):
                self.assertEqual(samples_of(read_wav(self.build(riff_size=riff))), self.values)

    def test_data_size_larger_than_the_file_is_a_cut_short_error(self):
        full = len(self.values) * 2
        for claimed in (full + 4, full + 100000, 0x7FFFFFF0):
            with self.subTest(claimed=claimed):
                error = expect_error(self, self.build(data_size=claimed), 'truncated')
                self.assertIn('cut short', str(error))

    def test_unknown_length_marker_and_zero_length(self):
        error = expect_error(self, self.build(data_size=0xFFFFFFFF), 'damaged')
        self.assertIn('does not say how long', str(error))
        expect_error(self, self.build(data_size=0), 'empty')

    def test_data_size_smaller_than_the_bytes_present_reads_only_what_is_declared(self):
        pcm = read_wav(self.build(data_size=400))
        self.assertEqual(samples_of(pcm), self.values[:200])           # 400 bytes = 100 stereo frames = 200 values

    def test_partial_trailing_frame_is_dropped_with_a_note(self):
        pcm = read_wav(self.build(data_size=403))
        self.assertEqual(pcm.frame_count, 100)
        self.assertEqual(len(pcm.notes), 1)
        self.assertIn('3 byte', pcm.notes[0])

    def test_data_chunk_smaller_than_one_frame_is_empty(self):
        expect_error(self, self.build(data_size=3), 'empty')

    def test_huge_or_oversized_format_chunks(self):
        for size in (0x7FFFFFFF, 0xFFFFFFFF, 1025, 15, 0):
            with self.subTest(size=size):
                data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + b'fmt ' + struct.pack('<I', size) + fmt_payload(1, 1, 44100, 16) + chunk(b'data', bytes(100))
                expect_error(self, data, 'damaged')
        # an 18-byte format block (size field says 18) is fine
        eighteen = fmt_payload(1, 1, 44100, 16) + struct.pack('<H', 0)
        data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', eighteen) + chunk(b'data', bytes(200))
        self.assertEqual(read_wav(data).frame_count, 100)

    def test_chunk_before_the_sound_that_claims_more_than_the_file_holds(self):
        data = (b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(1, 1, 44100, 16))
                + b'LIST' + struct.pack('<I', 0xFFFFFFF0) + bytes(50) + chunk(b'data', bytes(100)))
        expect_error(self, data, 'damaged')

    def test_missing_duplicated_or_misordered_parts(self):
        fmt = chunk(b'fmt ', fmt_payload(1, 1, 44100, 16))
        sound = chunk(b'data', bytes(200))
        head = b'RIFF' + struct.pack('<I', 0) + b'WAVE'
        expect_error(self, head + sound + fmt, 'damaged')               # sound before its format
        expect_error(self, head + fmt + chunk(b'LIST', bytes(8)), 'damaged')   # no sound at all
        expect_error(self, head + sound, 'damaged')                      # no format
        expect_error(self, head + fmt + fmt + sound, 'damaged')          # format twice
        expect_error(self, head, 'damaged')

    def test_chunk_walk_is_bounded(self):
        fmt = chunk(b'fmt ', fmt_payload(1, 1, 44100, 16))
        filler = chunk(b'junk', b'\x00\x00')
        head = b'RIFF' + struct.pack('<I', 0) + b'WAVE'
        ok = head + fmt + filler * 100 + chunk(b'data', bytes(200))
        self.assertEqual(read_wav(ok).frame_count, 100)
        started = time.perf_counter()
        expect_error(self, head + fmt + filler * 5000 + chunk(b'data', bytes(200)), 'damaged')
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_odd_length_chunks_and_missing_pad_bytes(self):
        padded = chunk(b'LIST', b'12345')                     # 5 bytes + pad
        unpadded = chunk(b'LIST', b'12345', pad=False)       # writer forgot the pad byte
        for label, extra in (('padded', padded), ('unpadded', unpadded)):
            with self.subTest(label):
                data = build_wav(self.values, 44100, 2, 16, before=extra)
                self.assertEqual(samples_of(read_wav(data)), self.values)
        # an odd-sized sound chunk (8-bit mono, 5 samples) followed by its pad byte
        raw = bytes([128, 129, 130, 131, 132])
        data = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(1, 1, 8000, 8)) + chunk(b'data', raw) + chunk(b'LIST', b'abcd')
        self.assertEqual(samples_of(read_wav(data)), [0, 256, 512, 768, 1024])

    def test_trailing_garbage_is_ignored(self):
        for tail in (b'\x00', b'garbage' * 50, os.urandom(300), build_wav(tone(50)), chunk(b'LIST', b'INFOabcd')):
            with self.subTest(tail=tail[:12]):
                self.assertEqual(samples_of(read_wav(self.build(after=tail))), self.values)

    def test_every_truncation_of_a_valid_file_is_a_clean_error(self):
        data = self.build()
        for cut in range(len(data)):
            with self.assertRaises(AudioError, msg=f'cut at {cut}'):
                read_wav(data[:cut])
        self.assertEqual(samples_of(read_wav(data)), self.values)

    def test_header_fuzz_only_ever_raises_audio_error(self):
        rnd = random.Random(20240601)
        base = self.build()
        accepted = 0
        for _ in range(1200):
            data = bytearray(base)
            for _ in range(rnd.randint(1, 4)):
                data[rnd.randrange(0, 60)] = rnd.randrange(256)
            roll = rnd.random()
            if roll < 0.2:
                data = data[:rnd.randrange(0, len(data))]
            elif roll < 0.3:
                data += rnd.randbytes(rnd.randrange(1, 40))
            try:
                pcm = read_wav(bytes(data))
            except AudioError as error:
                self.assertTrue(str(error))
                continue
            accepted += 1
            self.assertIn(pcm.channels, (1, 2))
            self.assertTrue(8000 <= pcm.rate <= 384000)
            self.assertEqual(len(pcm.frames) % (2 * pcm.channels), 0)
        self.assertGreater(accepted, 10)

    def test_random_bytes_only_ever_raise_audio_error(self):
        rnd = random.Random(7)
        for _ in range(500):
            data = rnd.randbytes(rnd.randrange(0, 200))
            if rnd.random() < 0.5 and len(data) > 12:
                data = b'RIFF' + data[4:8] + b'WAVE' + data[12:]
            with self.assertRaises(AudioError):
                read_wav(data)


class SourceAndLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.values = tone(2000, 2, 330.0, 44100, 7000)
        self.data = build_wav(self.values, 44100, 2, 16)

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_paths_bytes_bytearray_and_memoryview_agree(self):
        path = self.write('a.wav', self.data)
        expected = read_wav(self.data)
        for source in (path, str(path), bytearray(self.data), memoryview(self.data), memoryview(bytearray(self.data))):
            with self.subTest(type=type(source).__name__):
                self.assertEqual(read_wav(source), expected)
        self.assertEqual(samples_of(expected), self.values)

    def test_missing_directory_and_special_files(self):
        error = expect_error(self, self.dir / 'nope.wav', 'missing')
        self.assertIn('could not be found', str(error))
        expect_error(self, self.dir, 'unreadable')
        expect_error(self, str(self.dir / 'a\x00b.wav'), 'unreadable')
        if hasattr(os, 'mkfifo'):
            fifo = self.dir / 'pipe.wav'
            os.mkfifo(fifo)
            started = time.perf_counter()
            expect_error(self, fifo, 'unreadable')
            self.assertLess(time.perf_counter() - started, 2.0)       # must not block waiting for a writer

    def test_wrong_argument_types(self):
        for bad in (None, 5, 1.5, ['x'], {'a': 1}, object()):
            with self.subTest(bad=bad):
                expect_error(self, bad, 'invalid')
        for limit in (0, -1, 2 ** 31, 1.5, True, None, '100'):
            with self.subTest(limit=limit):
                expect_error(self, self.data, 'invalid', max_bytes=limit)

    def test_size_limit_applies_to_bytes_and_paths(self):
        path = self.write('big.wav', self.data)
        for source in (self.data, path):
            with self.subTest(type=type(source).__name__):
                error = expect_error(self, source, 'too_large', max_bytes=len(self.data) - 1)
                self.assertIn('more than', str(error))
                self.assertEqual(read_wav(source, max_bytes=len(self.data)).frame_count, 2000)

    def test_size_limit_is_checked_before_any_sound_is_read(self):
        path = self.write('big.wav', self.data)
        with mock.patch.object(wav_module.os, 'fdopen', side_effect=AssertionError('the file must not be read')):
            expect_error(self, path, 'too_large', max_bytes=1000)
        with mock.patch.object(wav_module, '_decode', side_effect=AssertionError('nothing may be decoded')):
            raw = bytes(range(256)) * 8                          # 2048 8-bit samples would become 4096 bytes
            file8 = b'RIFF' + struct.pack('<I', 0) + b'WAVE' + chunk(b'fmt ', fmt_payload(1, 1, 8000, 8)) + chunk(b'data', raw)
            expect_error(self, file8, 'too_large', max_bytes=len(file8) + 100)   # the converted audio would not fit

    def test_a_sparse_file_over_the_limit_is_refused_without_reading_it(self):
        path = self.dir / 'huge.wav'
        with open(path, 'wb') as handle:
            handle.write(self.data[:44])
            handle.truncate(150 * 1024 ** 2)
        if os.stat(path).st_blocks * 512 > 50 * 1024 ** 2:
            self.skipTest('this file system does not keep sparse files')
        started = time.perf_counter()
        expect_error(self, path, 'too_large')
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_header_claiming_a_giant_sound_in_a_tiny_file_allocates_nothing(self):
        import tracemalloc
        data = build_wav(self.values, 44100, 2, 16, data_size=0x7FFFFFF0)
        tracemalloc.start()
        try:
            expect_error(self, data, 'truncated')
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2_000_000)

    def test_file_that_shrinks_while_being_read_is_a_clean_error(self):
        big = build_wav(tone(60000, 2, 330.0, 44100, 7000), 44100, 2, 16)     # larger than the read buffer
        path = self.write('shrink.wav', big)
        real_read = wav_module._Source.read

        def shrinking(source, offset, count):
            if offset >= 44:
                with open(path, 'r+b') as handle:
                    handle.truncate(100)
            return real_read(source, offset, count)

        with mock.patch.object(wav_module._Source, 'read', shrinking):
            error = expect_error(self, path, 'truncated')
        self.assertIn('cut short', str(error))


class WriteTests(unittest.TestCase):
    def test_header_matches_the_stations_own_importer(self):
        from myradio.media import open_media
        pcm = audio.to_station_format(Pcm(array('h', tone(24000, 1, 440.0, 24000, 9000)).tobytes(), 24000, 1))
        data = write_wav(pcm)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'x.wav'
            path.write_bytes(data)
            reader = open_media(str(path), pcm.frame_count / 44100)       # the playout engine's strict preflight
            try:
                self.assertEqual(len(reader.readframes(100)), 400)
            finally:
                reader.close()
            with wave.open(str(path), 'rb') as handle:
                self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()),
                                 (2, 2, 44100, pcm.frame_count))
        self.assertEqual(struct.unpack('<4sI4s4sIHHIIHH4sI', data[:44]),
                         (b'RIFF', 36 + len(pcm.frames), b'WAVE', b'fmt ', 16, 1, 2, 44100, 176400, 4, 16, b'data', len(pcm.frames)))
        self.assertEqual(read_wav(data), pcm)

    def test_a_real_station_accepts_the_audio(self):
        from myradio.station import Station
        voice = audio.normalize(audio.to_station_format(Pcm(array('h', tone(24000, 1, 300.0, 24000, 8000)).tobytes(), 24000, 1)))
        with tempfile.TemporaryDirectory() as folder:
            station = Station(folder, start_worker=False)
            try:
                for kind in ('speech', 'ad', 'music'):
                    track = station.import_audio(write_wav(voice), f'Test {kind}', 'Audio tests', kind)
                    self.assertAlmostEqual(track['duration'], voice.seconds, places=3)
                mono = Pcm(array('h', tone(8820, 1, 300.0, 44100, 8000)).tobytes(), 44100, 1)
                self.assertAlmostEqual(station.import_audio(write_wav(mono), 'Mono', '', 'music')['duration'], 0.2, places=3)
                self.assertEqual(len(station.status()['library']), 4)
            finally:
                station.close()

    def test_mono_and_empty_audio_write_valid_files(self):
        mono = Pcm(array('h', [1, -2, 3]).tobytes(), 8000, 1)
        self.assertEqual(read_wav(write_wav(mono)), mono)
        empty = Pcm(b'', 44100, 2)
        header = struct.unpack('<4sI4s4sIHHIIHH4sI', write_wav(empty))
        self.assertEqual(header[-1], 0)
        with self.assertRaises(AudioError):          # an empty file is refused on the way back in
            read_wav(write_wav(empty))
        with self.assertRaises(AudioError):
            write_wav('not audio')

    def test_write_wav_file_is_private_atomic_and_readable(self):
        pcm = Pcm(array('h', tone(500, 2)).tobytes(), 44100, 2)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'out.wav'
            size = write_wav_file(pcm, target)
            self.assertEqual(size, target.stat().st_size)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(read_wav(target), pcm)
            # a failure while replacing leaves the old file untouched and no temporary file behind
            before = target.read_bytes()
            with mock.patch.object(wav_module.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(AudioError) as caught:
                    write_wav_file(Pcm(bytes(400), 44100, 2), target)
            self.assertEqual(caught.exception.code, 'unwritable')
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['out.wav'])
            with self.assertRaises(AudioError):
                write_wav_file(pcm, Path(folder) / 'missing-folder' / 'x.wav')
            with self.assertRaises(AudioError):
                write_wav_file(pcm, None)


class ErrorTests(unittest.TestCase):
    def test_errors_carry_a_code_and_survive_pickling(self):
        import pickle
        error = expect_error(self, build_wav(tone(100), tag=6), 'unsupported')
        self.assertIn('0x0006', error.detail)                         # technical detail for logs, not in the message
        self.assertIn('A-law', error.detail)
        self.assertNotIn('0x0006', str(error))
        again = pickle.loads(pickle.dumps(error))
        self.assertEqual((type(again), str(again), again.code, again.detail), (AudioError, str(error), 'unsupported', error.detail))
        self.assertEqual(AudioError('plain').code, 'invalid')
        self.assertIsInstance(error, ValueError)

    def test_argument_errors_name_what_was_wrong_in_plain_words(self):
        pcm = Pcm(bytes(400), 44100, 2)
        for call, words in ((lambda: audio.fade(pcm, -1, 0), ('fade-in length', 'milliseconds')),
                            (lambda: audio.normalize(pcm, 5.0), ('target loudness', 'LUFS')),
                            (lambda: audio.concat([pcm, pcm], -1), ('crossfade length', 'milliseconds')),
                            (lambda: audio.validate_generated(pcm, 'song', -1, None), ('shortest allowed length', 'seconds')),
                            (lambda: audio.apply_gain(pcm, 99.0), ('volume change', 'decibels'))):
            with self.assertRaises(AudioError) as caught:
                call()
            for word in words:
                self.assertIn(word, str(caught.exception))
            self.assertNotIn('_ms', str(caught.exception))
            self.assertNotIn('_lufs', str(caught.exception))


class PcmTests(unittest.TestCase):
    def test_construction_is_validated(self):
        ok = Pcm(bytes(8), 44100, 2)
        self.assertEqual((ok.frame_count, ok.seconds), (2, 2 / 44100))
        for args in ((bytes(3), 44100, 2), (bytes(4), 44100, 3), (bytes(4), 44100, 0), (bytes(4), 0, 1), (bytes(4), 7999, 1),
                     (bytes(4), 384001, 1), (bytes(4), 44100.0, 1), (bytes(4), True, 1), (bytes(4), 44100, True), ('abcd', 44100, 1),
                     (None, 44100, 1), (bytes(6), 44100, 2)):
            with self.subTest(args=args):
                with self.assertRaises(AudioError):
                    Pcm(*args)

    def test_buffers_are_normalised_to_immutable_bytes(self):
        source = bytearray(b'\x01\x00\x02\x00')
        pcm = Pcm(source, 44100, 1)
        source[0] = 99
        self.assertIs(type(pcm.frames), bytes)
        self.assertEqual(pcm.frames, b'\x01\x00\x02\x00')
        self.assertEqual(Pcm(memoryview(b'\x01\x00'), 44100, 1).frames, b'\x01\x00')

    def test_value_semantics_and_a_short_safe_repr(self):
        a, b = Pcm(bytes(8), 44100, 2), Pcm(bytes(8), 44100, 2)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertNotEqual(a, Pcm(bytes(8), 48000, 2))
        self.assertEqual(Pcm(bytes(8), 44100, 2, ('x',)), a)           # notes do not take part in equality
        with self.assertRaises(Exception):
            a.rate = 1
        big = Pcm(bytes(8_000_000), 44100, 2)
        self.assertLess(len(repr(big)), 100)
        self.assertIn('stereo', repr(big))

    def test_notes_are_bounded_and_with_notes_keeps_the_audio(self):
        pcm = Pcm(bytes(8), 44100, 2)
        many = pcm
        for index in range(40):
            many = many.with_notes(f'note {index}')
        self.assertEqual(len(many.notes), 12)
        self.assertEqual(many.notes[-1], 'note 39')
        self.assertIs(pcm.with_notes(), pcm)
        self.assertEqual(Pcm(bytes(4), 44100, 1, notes=('a', 3)).notes, ('a', '3'))

    def test_module_has_no_forbidden_dependencies(self):
        import ast
        import sys
        package = Path(audio.__file__).parent
        stdlib = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else None
        for path in sorted(package.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'), feature_version=(3, 10))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split('.')[0]]
                for name in names:
                    self.assertNotIn(name, ('audioop', 'numpy', 'scipy', 'soundfile', 'pydub', 'sox'), path.name)
                    if stdlib is not None:
                        self.assertIn(name, stdlib, f'{path.name} imports {name}')


if __name__ == '__main__':
    unittest.main()
