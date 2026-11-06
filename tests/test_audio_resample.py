"""Sample-rate conversion and the fixed-point lane engine underneath it."""
import math
import random
import time
import unittest
from array import array
from unittest import mock

from myradio.audio import AudioError, Pcm, resample, synth, to_station_format
from myradio.audio import rates as resample_module
from myradio.audio.lanes import gain_plan, run_plan
from myradio.audio.rates import MAX_PHASES, build_plan

try:
    from test_audio_support import amplitude_at, db, samples_of, zero_crossings
except ImportError:  # run as tests.test_audio_resample from the repository root
    from tests.test_audio_support import amplitude_at, db, samples_of, zero_crossings


def naive(values, channels, plan, n_out):
    """What the lane engine promises, computed one sample at a time with plain Python integers."""
    L, M, bits = plan.L, plan.M, plan.qbits
    frames = len(values) // channels
    out = []
    for m in range(n_out):
        j, r = divmod(m, L)
        for c in range(channels):
            acc = 0
            for e, h in plan.entries[r]:
                i = M * j + e
                if 0 <= i < frames:
                    acc += h * values[i * channels + c]
            out.append(max(-32768, min(32767, (acc + (1 << (bits - 1))) >> bits)))
    return out


def count_overs(values, channels, plan, n_out):
    """How many output samples the plain integer loop would have had to clamp."""
    L, M, bits = plan.L, plan.M, plan.qbits
    frames = len(values) // channels
    total = 0
    for m in range(n_out):
        j, r = divmod(m, L)
        for c in range(channels):
            acc = 0
            for e, h in plan.entries[r]:
                i = M * j + e
                if 0 <= i < frames:
                    acc += h * values[i * channels + c]
            y = (acc + (1 << (bits - 1))) >> bits
            total += y > 32767 or y < -32768
    return total


def expected_length(frames, rate_in, rate_out):
    return (frames * rate_out + rate_in // 2) // rate_in


class LaneEngineTests(unittest.TestCase):
    CASES = (
        (48000, 44100, 'good', 2, 1200), (24000, 44100, 'good', 1, 900), (22050, 44100, 'good', 2, 600),
        (44100, 22050, 'good', 1, 700), (96000, 44100, 'good', 2, 1500), (8000, 44100, 'good', 1, 120),
        (44100, 48000, 'fast', 2, 700), (16000, 44100, 'good', 1, 300), (12000, 44100, 'fast', 1, 300),
        (44101, 44100, 'good', 1, 500), (44100, 8000, 'good', 1, 1500),
    )

    def signal(self, kind, count, rnd):
        if kind == 'noise':
            return [rnd.randint(-32768, 32767) for _ in range(count)]
        if kind == 'square':                                            # forces heavy clamping
            return [32767 if (i // 5) % 2 == 0 else -32768 for i in range(count)]
        return [rnd.randint(-3, 3) for _ in range(count)]

    def test_bit_exact_against_a_plain_integer_loop(self):
        rnd = random.Random(11)
        for rate_in, rate_out, quality, channels, frames in self.CASES:
            for kind in ('noise', 'square', 'tiny'):
                with self.subTest(rate_in=rate_in, rate_out=rate_out, quality=quality, channels=channels, kind=kind):
                    values = self.signal(kind, frames * channels, rnd)
                    pcm = Pcm(array('h', values).tobytes(), rate_in, channels)
                    out = resample(pcm, rate_out, quality)
                    plan = build_plan(*resample_module._ratio(rate_in, rate_out)[:2], quality)
                    self.assertEqual(samples_of(out), naive(values, channels, plan, expected_length(frames, rate_in, rate_out)))

    def test_segment_boundaries_do_not_matter(self):
        rnd = random.Random(12)
        values = [rnd.randint(-32768, 32767) for _ in range(2 * 1500)]
        pcm = Pcm(array('h', values).tobytes(), 48000, 2)
        plan = build_plan(160, 147, 'good')
        n_out = expected_length(1500, 48000, 44100)
        reference = naive(values, 2, plan, n_out)
        for lanes in (1, 2, 3, 7, 10, 37, 4096):
            with self.subTest(lanes=lanes):
                got = array('h')
                got.frombytes(bytes(run_plan(pcm.frames, 2, plan, n_out, count=lanes)))
                self.assertEqual(list(got), reference)

    def test_cut_off_samples_are_counted_exactly(self):
        rnd = random.Random(14)
        for rate_in, rate_out, channels, frames in ((48000, 44100, 2, 1500), (24000, 44100, 1, 900), (44100, 22050, 1, 700)):
            values = [rnd.choice((32767, -32768, 0, 12345)) for _ in range(frames * channels)]
            pcm = Pcm(array('h', values).tobytes(), rate_in, channels)
            plan = build_plan(*resample_module._ratio(rate_in, rate_out)[:2], 'good')
            n_out = expected_length(frames, rate_in, rate_out)
            wanted = count_overs(values, channels, plan, n_out)
            self.assertGreater(wanted, 0)
            for lanes in (7, 37, 4096):                           # partial last segments must not be miscounted
                overs = [0]
                run_plan(pcm.frames, channels, plan, n_out, count=lanes, overs=overs)
                self.assertEqual(overs[0], wanted, f'{rate_in}->{rate_out} lanes={lanes}')
            note = resample(pcm, rate_out).notes[-1]
            self.assertIn(f'{wanted:,} sample(s)', note)

    def test_gain_plan_is_bit_exact_including_saturation(self):
        rnd = random.Random(13)
        values = [rnd.randint(-32768, 32767) for _ in range(3000)] + [0, 1, -1, 32767, -32768]
        pcm = Pcm(array('h', values).tobytes(), 44100, 1)
        for fixed in (0, 1, 12345, 1 << 29, 1 << 30, (1 << 30) + 7, 5 << 30, 100 << 30):
            with self.subTest(fixed=fixed):
                got = array('h')
                got.frombytes(bytes(run_plan(pcm.frames, 1, gain_plan(fixed), len(values), count=499)))
                want = [max(-32768, min(32767, (v * fixed + (1 << 29)) >> 30)) for v in values]
                self.assertEqual(list(got), want)
        with self.assertRaises(AudioError):
            gain_plan(129 << 30)

    def test_every_filter_phase_has_exactly_unit_gain_at_zero_frequency(self):
        for ratio in ((160, 147, 'good'), (80, 147, 'good'), (1, 2, 'good'), (320, 147, 'good'), (160, 147, 'fast')):
            plan = build_plan(*ratio)
            for row in plan.entries:
                self.assertEqual(sum(h for _, h in row), 1 << 22)

    def test_filter_sizes(self):
        self.assertEqual(build_plan(160, 147, 'good').taps, 70)       # 48 kHz to 44.1 kHz
        self.assertEqual(build_plan(80, 147, 'good').taps, 64)        # 24 kHz to 44.1 kHz
        self.assertEqual(build_plan(320, 147, 'good').taps, 140)      # 96 kHz to 44.1 kHz
        self.assertEqual(build_plan(160, 147, 'fast').taps, 2)
        self.assertEqual(build_plan(160, 147, 'good').L, 147)


class LengthAndIdentityTests(unittest.TestCase):
    def test_output_length_is_the_rounded_ratio(self):
        for rate_in in (8000, 16000, 22050, 24000, 32000, 44100, 48000, 96000):
            for rate_out in (8000, 44100, 48000):
                for frames in (1, 7, 999):
                    with self.subTest(rate_in=rate_in, rate_out=rate_out, frames=frames):
                        pcm = Pcm(bytes(2 * frames), rate_in, 1)
                        out = resample(pcm, rate_out)
                        self.assertEqual(out.frame_count, expected_length(frames, rate_in, rate_out))
                        self.assertEqual((out.rate, out.channels), (rate_out, 1))

    def test_whole_seconds_convert_to_whole_seconds(self):
        for rate_in in (8000, 22050, 24000, 48000, 96000):
            pcm = Pcm(bytes(2 * rate_in), rate_in, 1)
            self.assertEqual(resample(pcm, 44100).frame_count, 44100)
            self.assertEqual(resample(pcm, 44100, 'fast').frame_count, 44100)

    def test_same_rate_returns_the_same_object(self):
        pcm = synth.sine(440.0, 0.1, 48000, 2)
        self.assertIs(resample(pcm, 48000), pcm)

    def test_empty_audio(self):
        out = resample(Pcm(b'', 24000, 1), 44100)
        self.assertEqual((out.frame_count, out.rate, out.channels), (0, 44100, 1))
        station = to_station_format(Pcm(b'', 16000, 1))
        self.assertEqual((station.frame_count, station.rate, station.channels), (0, 44100, 2))

    def test_tiny_audio_is_fine(self):
        for frames in (1, 2, 3):
            out = resample(Pcm(array('h', [1000] * frames).tobytes(), 48000, 1), 44100)
            self.assertEqual(out.frame_count, expected_length(frames, 48000, 44100))
        self.assertEqual(resample(Pcm(array('h', [1000]).tobytes(), 96000, 1), 8000).frame_count, 0)


class FrequencyTests(unittest.TestCase):
    def check_pass(self, rate_in, rate_out, freqs, tolerance_db=0.02):
        for freq in freqs:
            with self.subTest(rate_in=rate_in, rate_out=rate_out, freq=freq):
                pcm = synth.sine(float(freq), 0.5, rate_in, 1, -10.0)
                gain = db(amplitude_at(resample(pcm, rate_out), freq) / amplitude_at(pcm, freq))
                self.assertAlmostEqual(gain, 0.0, delta=tolerance_db)

    def check_reject(self, rate_in, rate_out, tone, alias, floor_db):
        pcm = synth.sine(float(tone), 0.5, rate_in, 1, -10.0)
        out = resample(pcm, rate_out)
        rejection = db(max(amplitude_at(out, alias), 1e-9) / amplitude_at(pcm, tone))
        self.assertLess(rejection, floor_db, f'{tone} Hz leaves {rejection:.1f} dB at {alias} Hz')

    def test_a_tone_keeps_its_pitch(self):
        for rate_in, rate_out, freq in ((48000, 44100, 1000), (24000, 44100, 1000), (22050, 44100, 1000), (16000, 44100, 1000),
                                        (8000, 44100, 1000), (96000, 44100, 1000), (44100, 48000, 1000), (44100, 22050, 4000)):
            with self.subTest(rate_in=rate_in, rate_out=rate_out):
                out = resample(synth.sine(float(freq), 0.5, rate_in, 1, -12.0), rate_out)
                values = samples_of(out)
                inner = values[int(0.05 * rate_out):int(0.45 * rate_out)]          # 0.4 s without the edges
                self.assertAlmostEqual(zero_crossings(inner), 2 * freq * 0.4, delta=1)

    def test_converted_sines_match_the_ideal_sine_at_the_new_rate(self):
        amplitude = round(32768 * 10 ** (-10 / 20))
        for rate_in, rate_out, freq in ((48000, 44100, 1000), (24000, 44100, 1000), (22050, 44100, 1000), (96000, 44100, 3000),
                                        (44100, 48000, 5000), (8000, 44100, 1000), (44100, 8000, 800)):
            pcm = synth.sine(float(freq), 0.5, rate_in, 1, -10.0)
            first, last = int(0.1 * rate_out), int(0.4 * rate_out)
            for quality in ('good', 'fast'):
                with self.subTest(rate_in=rate_in, rate_out=rate_out, quality=quality):
                    values = samples_of(resample(pcm, rate_out, quality))
                    worst = max(abs(values[i] - amplitude * math.sin(2 * math.pi * freq * i / rate_out)) for i in range(first, last))
                    if quality == 'good':
                        self.assertLessEqual(worst, 2.0)        # rounding only: gain, pitch and time alignment are exact
                    else:                                       # linear interpolation error bound: A * (2*pi*f/rate)^2 / 8
                        self.assertLessEqual(worst, amplitude * (2 * math.pi * freq / min(rate_in, rate_out)) ** 2 / 8 * 1.1 + 2)

    def test_there_is_no_time_shift(self):
        impulse = Pcm(array('h', [0] * 999 + [20000] + [0] * 1000).tobytes(), 48000, 1)
        values = samples_of(resample(impulse, 44100))
        self.assertIn(max(range(len(values)), key=values.__getitem__), (917, 918))          # 999 * 44100 / 48000 = 917.8
        values = samples_of(resample(Pcm(array('h', [0] * 399 + [20000] + [0] * 400).tobytes(), 16000, 1), 44100))
        self.assertEqual(max(range(len(values)), key=values.__getitem__), round(399 * 44100 / 16000))

    def test_passband_is_flat_when_downsampling_to_44_1(self):
        self.check_pass(48000, 44100, (1000, 5000, 10000, 15000, 17000))
        self.check_pass(96000, 44100, (1000, 8000, 15000, 17000))

    def test_passband_is_flat_when_upsampling(self):
        self.check_pass(24000, 44100, (1000, 5000, 8000))
        self.check_pass(22050, 44100, (1000, 5000, 8000))
        self.check_pass(44100, 48000, (1000, 10000, 17000))

    def test_frequencies_above_the_new_limit_do_not_fold_back(self):
        self.check_reject(48000, 44100, 23000, 21100, -80.0)        # would alias to 21.1 kHz
        self.check_reject(48000, 44100, 22400, 21700, -75.0)
        self.check_reject(96000, 44100, 30000, 14100, -80.0)
        self.check_reject(44100, 16000, 12000, 4000, -80.0)
        self.check_reject(44100, 22050, 15000, 7050, -80.0)

    def test_upsampling_leaves_no_images(self):
        self.check_reject(24000, 44100, 5000, 19000, -85.0)
        self.check_reject(22050, 44100, 10000, 12050, -80.0)
        self.check_reject(16000, 44100, 5000, 11000, -85.0)
        self.check_reject(8000, 44100, 2000, 6000, -85.0)

    def test_the_fast_mode_is_linear_interpolation_and_does_not_filter(self):
        ramp = Pcm(array('h', [i * 7 for i in range(2000)]).tobytes(), 32000, 1)
        out = samples_of(resample(ramp, 44100, 'fast'))
        for m in range(1, len(out) - 5):
            self.assertLessEqual(abs(out[m] - 7 * m * 32000 / 44100), 1.0)
        pcm = synth.sine(23000.0, 0.5, 48000, 1, -10.0)
        leak = db(amplitude_at(resample(pcm, 44100, 'fast'), 21100) / amplitude_at(pcm, 23000))
        self.assertGreater(leak, -20.0)                      # documented: no anti-aliasing in fast mode
        inner = samples_of(resample(synth.sine(1000.0, 0.5, 48000, 1, -12.0), 44100, 'fast'))[2205:19845]
        self.assertAlmostEqual(zero_crossings(inner), 800, delta=1)

    def test_channels_never_leak_into_each_other(self):
        left = synth.sine(1000.0, 0.3, 48000, 1, -10.0)
        values = [v for sample in samples_of(left) for v in (sample, 0)]
        pcm = Pcm(array('h', values).tobytes(), 48000, 2)
        for quality in ('good', 'fast'):
            with self.subTest(quality=quality):
                out = samples_of(resample(pcm, 44100, quality))
                self.assertEqual(set(out[1::2]), {0})
                self.assertGreater(max(out[0::2]), 8000)
        different = [v for a, b in zip(samples_of(synth.sine(1000.0, 0.5, 48000, 1, -10.0)), samples_of(synth.sine(3000.0, 0.5, 48000, 1, -10.0))) for v in (a, b)]
        out = resample(Pcm(array('h', different).tobytes(), 48000, 2), 44100)
        self.assertGreater(amplitude_at(out, 1000, channel=0) / amplitude_at(out, 3000, channel=0), 1000)
        self.assertGreater(amplitude_at(out, 3000, channel=1) / amplitude_at(out, 1000, channel=1), 1000)


class OverloadTests(unittest.TestCase):
    def test_gentle_audio_converts_without_any_note(self):
        for rate_in in (48000, 24000, 96000, 22050):
            self.assertEqual(resample(synth.sine(1000.0, 0.3, rate_in, 2, -6.0), 44100).notes, ())
            self.assertEqual(resample(synth.noise(0.3, rate_in, 1, -20.0, seed=1), 44100).notes, ())

    def test_full_scale_square_waves_saturate_instead_of_wrapping(self):
        for rate_in, rate_out in ((48000, 44100), (44100, 48000), (22050, 44100)):
            with self.subTest(rate_in=rate_in):
                out = samples_of(resample(synth.square(500.0, 0.2, rate_in, 1, 0.0), rate_out))
                self.assertEqual((max(out), min(out)), (32767, -32768))
                half = rate_out / 1000.0                                   # samples in half a cycle of 500 Hz
                for h in range(3, 8):                                      # the middle of each half cycle, away from its edges
                    first, last = int((h + 0.25) * half), int((h + 0.75) * half)
                    centre = out[first:last]
                    if h % 2 == 0:
                        self.assertTrue(all(v > 30000 for v in centre), f'half cycle {h}: {centre}')
                    else:
                        self.assertTrue(all(v < -30000 for v in centre), f'half cycle {h}: {centre}')


class RatioAndLimitTests(unittest.TestCase):
    def test_ordinary_rates_are_converted_exactly(self):
        for rate in (8000, 11025, 12000, 16000, 22050, 24000, 32000, 48000, 88200, 96000, 176400, 192000, 384000):
            M, L, error = resample_module._ratio(rate, 44100)
            self.assertEqual((M * 44100, error), (L * rate, 0.0), rate)
            self.assertLessEqual(L, MAX_PHASES)

    def test_unusual_ratios_are_approximated_and_say_so(self):
        pcm = synth.sine(1000.0, 0.5, 44101, 1, -12.0)
        out = resample(pcm, 44100)
        self.assertEqual(out.frame_count, expected_length(pcm.frame_count, 44101, 44100))
        self.assertEqual(len(out.notes), 1)
        self.assertIn('parts per million', out.notes[0])
        inner = samples_of(out)[2205:19845]
        self.assertAlmostEqual(zero_crossings(inner), 800, delta=1)
        for rate in range(8000, 384001, 7919):
            M, L, error = resample_module._ratio(rate, 44100)
            self.assertLessEqual(L, MAX_PHASES)
            self.assertLess(error, 6e-4)
        self.assertEqual(resample(synth.sine(1000.0, 0.2, 44056, 1, -12.0), 44100).notes, ())   # close enough not to mention

    def test_bad_arguments(self):
        pcm = synth.sine(440.0, 0.1, 48000, 2)
        for rate in (0, 7999, 384001, -44100, 44100.0, True, '44100', None):
            with self.assertRaises(AudioError, msg=repr(rate)):
                resample(pcm, rate)
        for quality in ('best', '', None, 'GOOD', 3):
            with self.assertRaises(AudioError, msg=repr(quality)):
                resample(pcm, 44100, quality)
        for bad in (None, b'abc', 'x'):
            with self.assertRaises(AudioError):
                resample(bad, 44100)
            with self.assertRaises(AudioError):
                to_station_format(bad)

    def test_results_too_big_for_the_station_are_refused_before_any_work(self):
        started = time.perf_counter()
        long_voice = Pcm(bytes(30 * 1024 * 1024), 8000, 1)           # about 33 minutes
        for call in (lambda: to_station_format(long_voice), lambda: resample(long_voice, 384000), lambda: resample(long_voice, 44100)):
            with self.assertRaises(AudioError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'too_large')
            self.assertIn('shorter', str(caught.exception))
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_the_work_guard_stops_conversions_that_would_take_minutes(self):
        pcm = synth.sine(440.0, 0.5, 96000, 2)
        with mock.patch.object(resample_module, 'MAX_TAP_WORK', 1000):
            with self.assertRaises(AudioError) as caught:
                resample(pcm, 44100)
        self.assertEqual(caught.exception.code, 'too_large')

    def test_filter_tables_are_built_once_per_ratio(self):
        pcm = synth.sine(440.0, 0.2, 32000, 1)
        resample(pcm, 44100)
        before = build_plan.cache_info()
        resample(pcm, 44100)
        resample(pcm, 44100)
        after = build_plan.cache_info()
        self.assertEqual(after.misses, before.misses)
        self.assertEqual(after.hits, before.hits + 2)


class ThreadSafetyTests(unittest.TestCase):
    def test_threads_sharing_audio_and_caches_get_identical_results(self):
        import threading
        sources = [synth.sine(300.0 + 50 * i, 0.4, rate, 2 if i % 2 else 1, -14.0) for i, rate in enumerate((24000, 48000, 22050, 32000, 16000, 96000))]
        expected = [(to_station_format(p), audio_loudness(p)) for p in sources]
        results = [None] * 12
        errors = []

        def work(index):
            try:
                p = sources[index % len(sources)]
                results[index] = (to_station_format(p), audio_loudness(p))
            except BaseException as error:        # reported below, on the main thread
                errors.append(error)

        resample_module.build_plan.cache_clear()          # force the threads to race to build the same tables
        threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        for index, result in enumerate(results):
            self.assertEqual(result, expected[index % len(sources)])


def audio_loudness(pcm):
    from myradio.audio import loudness_lufs
    return loudness_lufs(Pcm(pcm.frames, pcm.rate, pcm.channels))      # a fresh object, so each thread measures it itself


class StationFormatTests(unittest.TestCase):
    def test_mono_becomes_the_same_sound_in_both_speakers(self):
        mono = Pcm(array('h', [1, -2, 300, -400, 5]).tobytes(), 44100, 1)
        out = to_station_format(mono)
        self.assertEqual((out.rate, out.channels), (44100, 2))
        self.assertEqual(samples_of(out), [1, 1, -2, -2, 300, 300, -400, -400, 5, 5])

    def test_station_format_audio_is_returned_unchanged(self):
        pcm = synth.sine(440.0, 0.1)
        self.assertIs(to_station_format(pcm), pcm)

    def test_voice_at_24_khz_and_songs_at_48_khz(self):
        voice = to_station_format(synth.sine(300.0, 1.0, 24000, 1, -18.0))
        self.assertEqual((voice.rate, voice.channels, voice.frame_count), (44100, 2, 44100))
        values = samples_of(voice)
        self.assertEqual(values[0::2], values[1::2])
        song = to_station_format(synth.sine(300.0, 1.0, 48000, 2, -18.0))
        self.assertEqual((song.rate, song.channels, song.frame_count), (44100, 2, 44100))

    def test_notes_survive_conversion(self):
        pcm = synth.sine(300.0, 0.1, 24000, 1).with_notes('remember me')
        self.assertEqual(to_station_format(pcm).notes, ('remember me',))

    def test_conversion_speed_does_not_blow_up(self):
        pcm = synth.sine(300.0, 20.0, 24000, 1, -18.0)
        started = time.perf_counter()
        to_station_format(pcm)
        self.assertLess(time.perf_counter() - started, 20.0)       # about 0.4 s when the machine is idle


if __name__ == '__main__':
    unittest.main()
