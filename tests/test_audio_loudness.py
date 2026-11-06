"""Integrated loudness (K-weighted, gated), checked against known and independently measured values.

Reference values written as ``FFMPEG`` below were captured once from FFmpeg's ebur128 filter on exactly these
synthetic signals; the tests themselves do not need FFmpeg.
"""
import json
import math
import unittest
from unittest import mock

from myradio.audio import Pcm, analyze, concat, loudness_lufs, synth
from myradio.audio import loudness as loudness_module
from myradio.audio.loudness import k_coefficients, k_weighting_gain_db

# BS.1770-4 printed coefficients for 48 kHz
STANDARD_48K = (1.53512485958697, -2.69169618940638, 1.19839281085285, -1.69065929318241, 0.73248077421585,
                -1.99004745483398, 0.99007225036621)

# (frequency, level dBFS peak, seconds, FFmpeg ebur128 integrated loudness) for 48 kHz sines played from both speakers
# (the tests use the mono version, which this module measures as both speakers, so the numbers are directly comparable)
FFMPEG = (
    (997.0, -20.0, 6.0, -20.0040),
    (100.0, -18.0, 6.0, -19.8310),
    (40.0, -18.0, 6.0, -24.2610),
    (5000.0, -24.0, 6.0, -20.6800),
    (15000.0, -24.0, 6.0, -20.6500),
)


def analytic(rate, freq, level_dbfs, channels_with_signal=2):
    peak = round(32768 * 10 ** (level_dbfs / 20)) / 32768           # the synthesiser rounds the amplitude to a whole sample
    return -0.691 + 10 * math.log10(channels_with_signal * peak ** 2 / 2) + k_weighting_gain_db(rate, freq)


class FilterTests(unittest.TestCase):
    def test_coefficients_match_the_standard_at_48_khz(self):
        for ours, printed in zip(k_coefficients(48000), STANDARD_48K):
            self.assertAlmostEqual(ours, printed, places=9)

    def test_frequency_response_shape(self):
        self.assertAlmostEqual(k_weighting_gain_db(48000, 997.0), 0.691, delta=0.002)
        self.assertAlmostEqual(k_weighting_gain_db(48000, 10000.0), 4.0, delta=0.15)     # the +4 dB shelf
        self.assertLess(k_weighting_gain_db(48000, 20.0), -10.0)                          # the 38 Hz high-pass
        self.assertAlmostEqual(k_weighting_gain_db(48000, 300.0), 0.0, delta=0.2)

    def test_coefficients_exist_for_every_supported_rate(self):
        for rate in (8000, 44100, 192000, 384000):
            self.assertEqual(len(k_coefficients(rate)), 7)
            self.assertTrue(all(math.isfinite(c) for c in k_coefficients(rate)))


class ToneTests(unittest.TestCase):
    def test_997_hz_at_minus_20_dbfs_reads_minus_20_lufs(self):
        for rate in (44100, 48000):
            for channels in (2, 1):
                with self.subTest(rate=rate, channels=channels):
                    pcm = synth.sine(997.0, 2.0, rate, channels, -20.0)
                    self.assertAlmostEqual(loudness_lufs(pcm), -20.0, delta=0.01)

    def test_other_sample_rates_stay_within_a_twentieth_of_a_unit(self):
        for rate in (8000, 16000, 22050, 32000, 96000):
            with self.subTest(rate=rate):
                self.assertAlmostEqual(loudness_lufs(synth.sine(997.0, 2.0, rate, 2, -20.0)), -20.0, delta=0.05)

    def test_matches_the_filter_response_at_other_frequencies(self):
        for rate in (44100, 48000):
            for freq in (50.0, 100.0, 400.0, 3000.0, 8000.0, 15000.0):
                with self.subTest(rate=rate, freq=freq):
                    pcm = synth.sine(freq, 2.0, rate, 1, -20.0)
                    self.assertAlmostEqual(loudness_lufs(pcm), analytic(rate, freq, -20.0), delta=0.01)

    def test_agrees_with_ffmpeg_ebur128(self):
        for freq, level, seconds, reference in FFMPEG:
            with self.subTest(freq=freq):
                self.assertAlmostEqual(loudness_lufs(synth.sine(freq, seconds, 48000, 1, level)), reference, delta=0.02)

    def test_level_is_linear_in_amplitude(self):
        quiet = loudness_lufs(synth.sine(997.0, 2.0, 44100, 2, -26.0))
        loud = loudness_lufs(synth.sine(997.0, 2.0, 44100, 2, -14.0))
        self.assertAlmostEqual(loud - quiet, 12.0, delta=0.01)

    def test_one_speaker_is_three_db_quieter_than_two(self):
        both = synth.sine(997.0, 2.0, 44100, 2, -20.0)
        only_left = Pcm(bytes(b for i in range(0, len(both.frames), 4) for b in both.frames[i:i + 2] + b'\x00\x00'), 44100, 2)
        self.assertAlmostEqual(loudness_lufs(both) - loudness_lufs(only_left), 3.0103, delta=0.01)

    def test_mono_counts_as_both_speakers(self):
        mono = synth.sine(500.0, 2.0, 44100, 1, -24.0)
        duplicated = Pcm(b''.join(mono.frames[i:i + 2] * 2 for i in range(0, len(mono.frames), 2)), 44100, 2)
        self.assertAlmostEqual(loudness_lufs(mono), loudness_lufs(duplicated), places=9)

    def test_white_noise_matches_the_integrated_response(self):
        for rate in (44100, 48000):
            with self.subTest(rate=rate):
                pcm = synth.noise(4.0, rate, 2, -14.0, seed=11)
                peak = round(32768 * 10 ** (-14.0 / 20))
                variance = ((2 * peak + 1) ** 2 - 1) / 12 / 32768 ** 2
                steps = 4000
                mean = sum(10 ** (k_weighting_gain_db(rate, (i + 0.5) * rate / 2 / steps) / 10) for i in range(steps)) / steps
                expected = -0.691 + 10 * math.log10(2 * variance * mean)
                self.assertAlmostEqual(loudness_lufs(pcm), expected, delta=0.04)

    def test_short_clips_are_measured_as_one_block(self):
        for ms in (10, 50, 100, 300, 399, 400, 401, 500):
            with self.subTest(ms=ms):
                self.assertAlmostEqual(loudness_lufs(synth.sine(997.0, ms / 1000, 44100, 2, -20.0)), -20.0, delta=0.05)


class GatingTests(unittest.TestCase):
    """Run at 16 kHz mono so the long programme-like signals stay cheap."""

    rate = 16000

    def tone(self, seconds, level):
        return synth.sine(997.0, seconds, self.rate, 1, level)

    def test_quiet_passages_below_the_relative_gate_are_ignored(self):
        loud = self.tone(8.0, -14.0)
        mixed = concat([loud, self.tone(8.0, -40.0), loud])
        self.assertAlmostEqual(loudness_lufs(mixed), loudness_lufs(loud), delta=0.1)

    def test_ffmpeg_gating_references(self):
        def sines(*parts):
            return concat([synth.sine(997.0, seconds, 48000, 1, level) for seconds, level in parts])
        # a quiet passage 26 LU down is dropped by the relative gate (FFmpeg: -14.1380)
        self.assertAlmostEqual(loudness_lufs(sines((5, -14.0), (5, -40.0), (5, -14.0))), -14.1380, delta=0.02)
        # a passage below -70 LUFS is dropped by the absolute gate (FFmpeg: -20.1140)
        self.assertAlmostEqual(loudness_lufs(sines((6, -20.0), (4, -90.0))), -20.1140, delta=0.02)
        # passages 6 LU apart are all kept and averaged as power (FFmpeg: -22.9540)
        self.assertAlmostEqual(loudness_lufs(sines((4, -26.0), (4, -20.0), (4, -26.0))), -22.9540, delta=0.02)

    def test_the_absolute_gate_ignores_near_silence(self):
        sound = self.tone(20.0, -20.0)
        mixed = concat([sound, self.tone(10.0, -85.0)])
        self.assertAlmostEqual(loudness_lufs(mixed), loudness_lufs(sound), delta=0.1)
        self.assertEqual(loudness_lufs(self.tone(5.0, -90.0)), -120.0)

    def test_passages_within_ten_units_are_averaged_as_power(self):
        first, second = self.tone(10.0, -26.0), self.tone(10.0, -20.0)
        joined = concat([first, second, first])
        a, b = loudness_lufs(first), loudness_lufs(second)
        expected = 10 * math.log10((2 * 10 ** (a / 10) + 10 ** (b / 10)) / 3)
        self.assertAlmostEqual(loudness_lufs(joined), expected, delta=0.1)

    def test_two_equal_halves_read_like_either_half(self):
        half = self.tone(5.0, -23.0)
        self.assertAlmostEqual(loudness_lufs(concat([half, half])), loudness_lufs(half), delta=0.01)


class RobustnessTests(unittest.TestCase):
    def test_silence_and_empty_audio_read_the_floor(self):
        for pcm in (synth.silence(2.0), Pcm(b'', 44100, 2), synth.silence(0.05)):
            self.assertEqual(loudness_lufs(pcm), -120.0)

    def test_results_are_always_finite_and_json_safe(self):
        for pcm in (synth.silence(1.0), synth.square(300.0, 1.0, 44100, 2, 0.0), synth.constant(32767, 1.0), synth.noise(1.0, seed=2),
                    synth.sine(21000.0, 1.0, 44100, 2, -3.0)):
            value = loudness_lufs(pcm)
            self.assertTrue(math.isfinite(value))
            json.dumps(analyze(pcm).as_dict(), allow_nan=False)

    def test_a_full_scale_square_wave_is_loud(self):
        value = loudness_lufs(synth.square(1000.0, 1.0, 44100, 2, 0.0))
        self.assertGreater(value, -1.0)
        self.assertLess(value, 4.0)

    def test_the_measurement_is_remembered_per_audio_object(self):
        pcm = synth.sine(500.0, 1.0)
        with mock.patch.object(loudness_module, '_step_energies', wraps=loudness_module._step_energies) as spy:
            first = loudness_lufs(pcm)
            second = loudness_lufs(pcm)
            analyze(pcm)
        self.assertEqual(first, second)
        self.assertEqual(spy.call_count, 1)

    def test_bad_input(self):
        for bad in (None, b'abc', 'x'):
            with self.assertRaises(ValueError):
                loudness_lufs(bad)


if __name__ == '__main__':
    unittest.main()
