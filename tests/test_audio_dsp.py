"""The public contract, measurements, the quality gate, fades, trimming, joining and level changes."""
import dataclasses
import inspect
import json
import math
import random
import unittest
from array import array
from unittest import mock

import myradio.audio as audio
from myradio.audio import (Analysis, AudioError, Pcm, Problem, analyze, apply_gain, concat, fade, normalize,
                           normalize_with_report, synth, trim_silence, validate_generated)

try:
    from test_audio_support import db, pcm_from_samples, samples_of, tone
except ImportError:  # run as tests.<module> from the repository root
    from tests.test_audio_support import db, pcm_from_samples, samples_of, tone


def stereo(values, rate=44100):
    return pcm_from_samples(values, rate, 2)


def burst(seconds, level=-6.0, rate=44100, channels=2):
    """Loud, edge-to-edge sound whose very first and last samples are well above any silence threshold."""
    return synth.square(1000.0, seconds, rate, channels, level)


def fade_out(pcm, seconds=0.2):
    return fade(pcm, 0, seconds * 1000)


class ContractTests(unittest.TestCase):
    """The names, argument orders and shapes the generation lane relies on must not drift."""

    def params(self, function):
        return [(p.name, p.default) for p in inspect.signature(function).parameters.values()]

    def test_function_signatures(self):
        names = lambda function: [name for name, _ in self.params(function)]
        self.assertEqual(self.params(audio.read_wav), [('source', inspect.Parameter.empty), ('max_bytes', 104857600)])
        self.assertEqual(names(audio.write_wav), ['pcm'])
        self.assertEqual(names(audio.to_station_format), ['pcm'])
        self.assertEqual(self.params(audio.resample), [('pcm', inspect.Parameter.empty), ('rate', inspect.Parameter.empty), ('quality', 'good')])
        self.assertEqual(names(audio.analyze), ['pcm'])
        self.assertEqual(names(audio.validate_generated), ['pcm', 'role', 'min_s', 'max_s'])
        self.assertEqual(self.params(audio.normalize), [('pcm', inspect.Parameter.empty), ('target_lufs', -16.0), ('ceiling_dbfs', -1.0)])
        self.assertEqual(names(audio.fade), ['pcm', 'in_ms', 'out_ms'])
        self.assertEqual(self.params(audio.trim_silence), [('pcm', inspect.Parameter.empty), ('threshold_dbfs', -50.0), ('keep_ms', 60)])
        self.assertEqual(self.params(audio.concat), [('parts', inspect.Parameter.empty), ('crossfade_ms', 0)])

    def test_types_and_fields(self):
        self.assertTrue(issubclass(AudioError, ValueError))
        self.assertTrue(dataclasses.is_dataclass(Pcm))
        self.assertEqual([f.name for f in dataclasses.fields(Pcm)][:3], ['frames', 'rate', 'channels'])
        self.assertEqual([f.name for f in dataclasses.fields(Analysis)],
                         ['duration_s', 'peak_dbfs', 'rms_dbfs', 'loudness_lufs', 'silence_ratio', 'clipped_ratio',
                          'dc_offset', 'leading_silence_s', 'trailing_silence_s'])
        self.assertEqual([f.name for f in dataclasses.fields(Problem)], ['code', 'message'])
        self.assertEqual(set(audio.PROBLEM_CODES), {'silent', 'too_short', 'too_long', 'clipped', 'dc_offset', 'truncated_tail'})
        self.assertEqual(set(audio.ROLES), {'song', 'host_break', 'id', 'spot', 'update'})
        self.assertEqual((audio.STATION_RATE, audio.STATION_CHANNELS), (44100, 2))
        for name in audio.__all__:
            self.assertTrue(hasattr(audio, name), name)

    def test_everything_returns_new_immutable_audio_and_never_changes_its_input(self):
        pcm = synth.sine(440.0, 1.0, 44100, 2, -12.0)
        snapshot = pcm.frames
        for result in (fade(pcm, 10, 10), normalize(pcm), trim_silence(pcm), concat([pcm, pcm], 100), apply_gain(pcm, -3.0),
                       audio.resample(pcm, 48000), audio.to_station_format(pcm)):
            self.assertIsInstance(result, Pcm)
        self.assertEqual(pcm.frames, snapshot)


class AnalyzeTests(unittest.TestCase):
    def test_sine_wave_measurements(self):
        pcm = synth.sine(1000.0, 2.0, 44100, 2, -20.0)
        info = analyze(pcm)
        self.assertAlmostEqual(info.duration_s, 2.0, places=6)
        self.assertAlmostEqual(info.peak_dbfs, -20.0, delta=0.01)
        self.assertAlmostEqual(info.rms_dbfs, -23.01, delta=0.01)
        self.assertAlmostEqual(info.loudness_lufs, -20.0, delta=0.05)
        self.assertEqual((info.silence_ratio, info.clipped_ratio), (0.0, 0.0))
        self.assertLess(info.dc_offset, 1e-4)
        self.assertLess(info.leading_silence_s, 0.001)
        self.assertLess(info.trailing_silence_s, 0.001)

    def test_digital_silence_gives_finite_floor_values(self):
        info = analyze(synth.silence(1.5, 44100, 2))
        self.assertEqual((info.peak_dbfs, info.rms_dbfs, info.loudness_lufs), (-120.0, -120.0, -120.0))
        self.assertEqual((info.silence_ratio, info.clipped_ratio, info.dc_offset), (1.0, 0.0, 0.0))
        self.assertAlmostEqual(info.leading_silence_s, 1.5)
        self.assertAlmostEqual(info.trailing_silence_s, 1.5)
        text = json.dumps(info.as_dict(), allow_nan=False)     # no inf or NaN anywhere
        self.assertIn('loudness_lufs', text)

    def test_empty_audio(self):
        info = analyze(Pcm(b'', 44100, 2))
        self.assertEqual(info.duration_s, 0.0)
        self.assertEqual(info.peak_dbfs, -120.0)
        json.dumps(info.as_dict(), allow_nan=False)

    def test_clipping_dc_and_silent_edges(self):
        clipped = analyze(synth.square(1000.0, 1.0, 44100, 2, 0.0))
        self.assertGreater(clipped.clipped_ratio, 0.99)
        self.assertAlmostEqual(clipped.peak_dbfs, 0.0, delta=0.01)
        dc = analyze(synth.constant(3000, 1.0, 44100, 1))
        self.assertAlmostEqual(dc.dc_offset, 3000 / 32768, places=6)
        mixed = audio.concat([synth.silence(0.5), burst(1.0), synth.silence(0.25)])
        info = analyze(mixed)
        self.assertAlmostEqual(info.leading_silence_s, 0.5, places=6)
        self.assertAlmostEqual(info.trailing_silence_s, 0.25, places=6)
        self.assertAlmostEqual(info.silence_ratio, 0.75 / 1.75, places=3)

    def test_a_few_full_scale_samples_are_not_counted_as_clipping(self):
        values = tone(44100, 1, 440.0, 44100, 12000)
        values[1000] = 32767
        values[2000] = -32768
        info = analyze(Pcm(array('h', values).tobytes(), 44100, 1))
        self.assertAlmostEqual(info.clipped_ratio, 2 / 44100, places=9)
        self.assertAlmostEqual(info.peak_dbfs, 0.0, delta=0.01)

    def test_channels_are_measured_separately_for_dc(self):
        left = [4000] * 22050
        right = [-1000] * 22050
        both = [v for pair in zip(left, right) for v in pair]
        info = analyze(stereo(both))
        self.assertAlmostEqual(info.dc_offset, 4000 / 32768, places=6)

    def test_results_are_remembered_on_the_object(self):
        pcm = synth.sine(500.0, 1.0)
        self.assertEqual(analyze(pcm), analyze(pcm))
        self.assertIn('scan', pcm._memo)
        self.assertIn('lufs', pcm._memo)

    def test_a_quality_check_does_not_spoil_a_later_full_analysis(self):
        pcm = synth.sine(500.0, 1.0, 44100, 2, -18.0)
        validate_generated(pcm, 'song', None, None)             # the cheap scan runs first ...
        again = analyze(pcm)                                    # ... and the full analysis still includes the RMS level
        fresh = analyze(synth.sine(500.0, 1.0, 44100, 2, -18.0))
        self.assertEqual(again, fresh)
        self.assertAlmostEqual(again.rms_dbfs, -21.0, delta=0.05)

    def test_mono_and_stereo_agree(self):
        mono = synth.sine(700.0, 1.0, 44100, 1, -18.0)
        two = synth.sine(700.0, 1.0, 44100, 2, -18.0)
        a, b = analyze(mono), analyze(two)
        for field in ('peak_dbfs', 'rms_dbfs', 'loudness_lufs', 'clipped_ratio', 'dc_offset'):
            self.assertAlmostEqual(getattr(a, field), getattr(b, field), places=6, msg=field)

    def test_invalid_input(self):
        for bad in (None, b'abc', 'x', [1, 2]):
            with self.assertRaises(AudioError):
                analyze(bad)


class ValidateGeneratedTests(unittest.TestCase):
    def codes(self, pcm, role='song', low=1.0, high=30.0):
        return [p.code for p in validate_generated(pcm, role, low, high)]

    def clean(self, seconds=3.0, **kwargs):
        return fade(synth.sine(440.0, seconds, 44100, 2, -18.0), 20, 200)

    def test_a_clean_clip_has_no_problems(self):
        self.assertEqual(validate_generated(self.clean(), 'song', 1.0, 30.0), [])
        self.assertEqual(validate_generated(self.clean(), 'id', None, None), [])

    def test_silent(self):
        self.assertEqual(self.codes(synth.silence(3.0)), ['silent'])
        self.assertEqual(self.codes(synth.noise(3.0, 44100, 2, -80.0)), ['silent'])      # a faint hiss is still nothing
        self.assertEqual(self.codes(Pcm(b'', 44100, 2), low=None, high=None), ['silent'])
        mostly_quiet = audio.concat([synth.silence(10.0), fade(synth.sine(440.0, 0.1, 44100, 2, -10.0), 5, 50)])
        self.assertEqual(self.codes(mostly_quiet, low=None, high=None), ['silent'])
        problem = validate_generated(synth.silence(3.0), 'host_break', None, None)[0]
        self.assertIn('host break', problem.message)

    def test_length_limits(self):
        pcm = self.clean(3.0)
        self.assertEqual(self.codes(pcm, low=5.0, high=None), ['too_short'])
        self.assertEqual(self.codes(pcm, low=None, high=2.0), ['too_long'])
        self.assertEqual(self.codes(pcm, low=3.0, high=3.0), [])
        self.assertEqual(self.codes(Pcm(b'', 44100, 2), low=1.0, high=None), ['silent', 'too_short'])
        message = validate_generated(pcm, 'song', 60, 120)[0].message
        self.assertIn('3.0 seconds', message)
        self.assertIn('1 minute', message)

    def test_clipped(self):
        loud = fade(synth.square(300.0, 3.0, 44100, 2, 0.0), 10, 300)
        self.assertIn('clipped', self.codes(loud))
        peaky = list(tone(44100 * 3, 1, 220.0, 44100, 12000))
        for i in range(0, len(peaky), 20000):          # a handful of rail samples is ordinary peak-normalised audio
            peaky[i] = 32767
        self.assertEqual(self.codes(fade(Pcm(array('h', peaky).tobytes(), 44100, 1), 10, 300)), [])
        heavy = [32767 if i % 50 < 2 else v for i, v in enumerate(peaky)]    # 4% of samples stuck at the top
        self.assertIn('clipped', self.codes(fade(Pcm(array('h', heavy).tobytes(), 44100, 1), 10, 300)))

    def test_dc_offset(self):
        shifted = [v + 4000 for v in tone(44100 * 3, 1, 330.0, 44100, 6000)]
        self.assertIn('dc_offset', self.codes(fade(Pcm(array('h', shifted).tobytes(), 44100, 1), 10, 300)))
        slight = [v + 200 for v in tone(44100 * 3, 1, 330.0, 44100, 6000)]
        self.assertNotIn('dc_offset', self.codes(fade(Pcm(array('h', slight).tobytes(), 44100, 1), 10, 300)))

    def test_truncated_tail(self):
        cut = synth.sine(440.0, 3.0, 44100, 2, -18.0)       # stops mid-wave at a normal level
        self.assertEqual(self.codes(cut), ['truncated_tail'])
        self.assertEqual(self.codes(fade(cut, 0, 100)), [])
        with_pause = audio.concat([cut, synth.silence(0.1)])
        self.assertEqual(self.codes(with_pause), [])
        quiet_end = audio.concat([fade(cut, 0, 3000)])
        self.assertEqual(self.codes(quiet_end), [])

    def test_several_problems_are_reported_together_in_a_fixed_order(self):
        both = synth.constant(20000, 3.0, 44100, 2)
        self.assertEqual(self.codes(both, low=5.0, high=None), ['too_short', 'dc_offset', 'truncated_tail'])
        codes = [p.code for p in validate_generated(Pcm(array('h', [32767] * 3000).tobytes(), 44100, 1), 'update', 5, 6)]
        self.assertEqual(codes, ['too_short', 'clipped', 'dc_offset', 'truncated_tail'])

    def test_messages_are_plain_language(self):
        jargon = ('PCM', 'dBFS', 'LUFS', 'DC', 'sample rate', 'Traceback', 'AudioError', 'clipping')
        samples = [synth.silence(2.0), synth.square(300.0, 2.0, 44100, 2, 0.0), synth.constant(20000, 2.0), synth.sine(440.0, 2.0)]
        for pcm in samples:
            for role in audio.ROLES:
                for problem in validate_generated(pcm, role, 60.0, None) + validate_generated(pcm, role, None, 0.5):
                    self.assertIsInstance(problem, Problem)
                    self.assertIn(problem.code, audio.PROBLEM_CODES)
                    self.assertTrue(problem.message.endswith('.'))
                    self.assertEqual(str(problem), problem.message)
                    for word in jargon:
                        self.assertNotIn(word, problem.message)

    def test_role_wording(self):
        words = {'song': 'song', 'host_break': 'host break', 'id': 'station ID', 'spot': 'sponsor message', 'update': 'news update'}
        for role, word in words.items():
            self.assertIn(word, validate_generated(synth.silence(1.0), role, None, None)[0].message)

    def test_bad_arguments(self):
        pcm = self.clean()
        for role in ('', 'Song', 'jingle', None, 3, ['song'], {'song': 1}, b'song'):
            with self.assertRaises(AudioError):
                validate_generated(pcm, role, 1, 2)
        for args in ((5, 1), (-1, 2), (1, float('nan')), (True, 2), ('1', 2), (1, float('inf'))):
            with self.assertRaises(AudioError, msg=str(args)):
                validate_generated(pcm, 'song', *args)
        with self.assertRaises(AudioError):
            validate_generated(b'x', 'song', 1, 2)


class FadeTests(unittest.TestCase):
    def test_shape_and_exact_length(self):
        pcm = synth.constant(10000, 1.0, 44100, 2)
        out = fade(pcm, 100, 200)
        head, tail = 4410, 8820
        self.assertEqual(out.frame_count, pcm.frame_count)
        values = samples_of(out)
        left = values[0::2]
        self.assertEqual(values[0::2], values[1::2])
        self.assertEqual(left[0], 0)
        self.assertEqual(left[-1], 0)
        self.assertEqual(left[head], 10000)                       # the frame after the fade-in is untouched
        self.assertEqual(left[-tail - 1], 10000)                  # the frame before the fade-out is untouched
        for k in (1, 100, 2205, head - 1):
            self.assertEqual(left[k], round(10000 * k / head))
        for j in (1, 100, 4410, tail - 1):                        # j frames before the end
            self.assertEqual(left[-1 - j], round(10000 * j / tail))
        self.assertEqual(left[:head], sorted(left[:head]))
        self.assertEqual(left[-tail:], sorted(left[-tail:], reverse=True))
        self.assertEqual(out.frames[head * 4:(pcm.frame_count - tail) * 4], pcm.frames[head * 4:(pcm.frame_count - tail) * 4])

    def test_mono_and_negative_samples(self):
        pcm = synth.constant(-8000, 0.5, 8000, 1)
        out = samples_of(fade(pcm, 250, 0))
        self.assertEqual(len(out), 4000)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[500], -2000)
        self.assertEqual(out[1000], -4000)
        self.assertEqual(out[2000], -8000)
        self.assertEqual(out[-1], -8000)

    def test_nothing_to_do_returns_the_same_audio(self):
        pcm = synth.sine(440.0, 0.5)
        self.assertIs(fade(pcm, 0, 0), pcm)
        self.assertIs(fade(pcm), pcm)
        self.assertIs(fade(pcm, 0.001, 0), pcm)             # rounds to zero frames

    def test_fades_longer_than_the_audio_are_shortened_to_fit(self):
        pcm = synth.constant(10000, 0.1, 44100, 1)           # 4410 frames
        out = samples_of(fade(pcm, 1000, 3000))
        self.assertEqual(len(out), 4410)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[-1], 0)
        self.assertGreater(max(out), 5000)
        self.assertEqual(max(out), out[out.index(max(out))])
        total = samples_of(fade(pcm, 10_000, 0))              # the whole clip fades in
        self.assertEqual(len(total), 4410)
        self.assertEqual(total[0], 0)
        self.assertLess(total[-1], 10000)

    def test_stereo_channels_fade_together_and_independently_of_content(self):
        values = [v for _ in range(2000) for v in (10000, -5000)]
        out = samples_of(fade(stereo(values), 20, 0))
        ramp = int(0.02 * 44100)
        self.assertEqual(out[2 * 100], round(10000 * 100 / ramp))
        self.assertEqual(out[2 * 100 + 1], round(-5000 * 100 / ramp))

    def test_bad_arguments(self):
        pcm = synth.sine(440.0, 0.2)
        for args in ((-1, 0), (0, -5), (float('nan'), 0), (0, float('inf')), (True, 0), ('5', 0), (10 ** 9, 0)):
            with self.assertRaises(AudioError, msg=str(args)):
                fade(pcm, *args)
        with self.assertRaises(AudioError):
            fade('x', 1, 1)

    def test_empty_audio(self):
        empty = Pcm(b'', 44100, 2)
        self.assertIs(fade(empty, 10, 10), empty)


class TrimSilenceTests(unittest.TestCase):
    def padded(self, before=0.5, after=0.25, level=-6.0):
        return audio.concat([synth.silence(before), burst(1.0, level), synth.silence(after)])

    def test_cut_exactly_to_the_sound(self):
        out = trim_silence(self.padded(), keep_ms=0)
        self.assertEqual(out.frame_count, 44100)
        self.assertEqual(out.frames, burst(1.0).frames)

    def test_default_keeps_sixty_milliseconds_each_side(self):
        out = trim_silence(self.padded())
        margin = round(0.06 * 44100)
        self.assertEqual(out.frame_count, 44100 + 2 * margin)
        values = samples_of(out)
        self.assertEqual(set(values[:2 * margin]), {0})
        self.assertEqual(set(values[-2 * margin:]), {0})

    def test_keeps_only_what_exists_and_returns_the_same_object_when_nothing_changes(self):
        pcm = self.padded(0.02, 0.01)
        self.assertIs(trim_silence(pcm, keep_ms=500), pcm)
        out = trim_silence(pcm, keep_ms=15)
        self.assertEqual(out.frame_count, round(0.015 * 44100) + 44100 + round(0.01 * 44100))
        sound = burst(1.0)
        self.assertIs(trim_silence(sound), sound)

    def test_threshold_decides_what_counts_as_silence(self):
        quiet = synth.square(1000.0, 0.5, 44100, 2, -60.0)
        pcm = audio.concat([quiet, burst(0.5), quiet])
        self.assertEqual(trim_silence(pcm, -50.0, 0).frame_count, 22050)
        self.assertEqual(trim_silence(pcm, -70.0, 0).frame_count, pcm.frame_count)

    def test_silence_inside_the_audio_is_untouched(self):
        pcm = audio.concat([synth.silence(0.3), burst(0.2), synth.silence(0.5), burst(0.2), synth.silence(0.3)])
        out = trim_silence(pcm, keep_ms=0)
        self.assertEqual(out.frame_count, round(0.9 * 44100))
        self.assertEqual(out.frames, audio.concat([burst(0.2), synth.silence(0.5), burst(0.2)]).frames)

    def test_a_single_loud_sample_is_found(self):
        values = [0] * 20000
        values[12345] = 700
        out = trim_silence(Pcm(array('h', values).tobytes(), 44100, 1), -50.0, 0)
        self.assertEqual(out.frame_count, 1)
        self.assertEqual(samples_of(out), [700])

    def test_either_channel_keeps_a_frame(self):
        values = [0, 0] * 5000
        values[2 * 3000 + 1] = 5000                          # only the right channel is loud
        values[2 * 3500] = 5000                              # only the left channel is loud
        out = trim_silence(stereo(values), -50.0, 0)
        self.assertEqual(out.frame_count, 501)

    def test_completely_silent_audio_becomes_empty_with_a_note(self):
        out = trim_silence(synth.silence(2.0))
        self.assertEqual(out.frame_count, 0)
        self.assertEqual((out.rate, out.channels), (44100, 2))
        self.assertTrue(any('silent' in note for note in out.notes))

    def test_mono_and_bad_arguments(self):
        mono = audio.concat([synth.silence(0.5, 8000, 1), burst(0.5, rate=8000, channels=1), synth.silence(0.5, 8000, 1)])
        self.assertEqual(trim_silence(mono, keep_ms=0).frame_count, 4000)
        for args in ((float('nan'), 0), (1.0, 0), (-200.0, 0), (-50.0, -1), (-50.0, float('inf')), ('x', 0), (-50.0, 10 ** 9)):
            with self.assertRaises(AudioError, msg=str(args)):
                trim_silence(mono, *args)
        empty = Pcm(b'', 44100, 2)
        self.assertIs(trim_silence(empty), empty)


class ConcatTests(unittest.TestCase):
    def test_plain_joining_is_exact(self):
        a, b, c = synth.sine(220.0, 0.3), synth.sine(330.0, 0.5), synth.sine(440.0, 0.2)
        joined = concat([a, b, c])
        self.assertEqual(joined.frames, a.frames + b.frames + c.frames)
        self.assertEqual(joined.frame_count, a.frame_count + b.frame_count + c.frame_count)
        self.assertIs(concat([a]), a)
        self.assertIs(concat((a,), 500), a)

    def test_crossfade_length_is_sample_exact(self):
        parts = [synth.sine(220.0, 1.0), synth.sine(330.0, 2.0), synth.sine(440.0, 1.5)]
        for ms in (1, 10, 250, 499.5):
            with self.subTest(ms=ms):
                overlap = round(ms * 44.1)
                out = concat(parts, ms)
                self.assertEqual(out.frame_count, sum(p.frame_count for p in parts) - 2 * overlap)
        self.assertEqual(concat(parts, 0.0001).frames, concat(parts).frames)        # rounds to no overlap

    def test_untouched_stretches_are_byte_identical(self):
        a, b = synth.sine(220.0, 1.0), synth.sine(330.0, 1.0)
        overlap = 4410
        out = concat([a, b], 100)
        self.assertEqual(out.frames[:(a.frame_count - overlap) * 4], a.frames[:(a.frame_count - overlap) * 4])
        self.assertEqual(out.frames[-(b.frame_count - overlap) * 4:], b.frames[overlap * 4:])

    def test_short_parts_only_overlap_by_half_their_length(self):
        parts = [synth.sine(220.0, 1.0), synth.sine(330.0, 2.0), synth.sine(440.0, 0.5), synth.sine(550.0, 1.0)]
        wanted = round(600 * 44.1)
        counts = [p.frame_count for p in parts]
        overlaps = [min(wanted, counts[i] // 2, counts[i + 1] // 2) for i in range(3)]
        self.assertLess(overlaps[1], wanted)                    # the 0.5 s part limits two of the joins
        out = concat(parts, 600)
        self.assertEqual(out.frame_count, sum(counts) - sum(overlaps))

    def test_equal_power_midpoint_and_no_level_dip(self):
        same = synth.constant(1000, 2.0, 44100, 1)
        out = samples_of(concat([same, same], 1000))
        self.assertEqual(len(out), 2 * 88200 - 44100)
        self.assertAlmostEqual(out[88200 - 22050], 1000 * math.sqrt(2.0), delta=3)      # in step: +3 dB at the middle
        self.assertEqual(out[0], 1000)
        self.assertEqual(out[-1], 1000)
        a, b = synth.noise(4.0, 44100, 1, -20.0, seed=1), synth.noise(4.0, 44100, 1, -20.0, seed=2)
        joined = samples_of(concat([a, b], 2000))
        centre = joined[len(joined) // 2 - 4410:len(joined) // 2 + 4410]
        edge = joined[:4410]
        rms = lambda xs: math.sqrt(sum(x * x for x in xs) / len(xs))
        self.assertLess(abs(db(rms(centre) / rms(edge))), 1.0)                   # a linear fade would dip 3 dB here

    def test_crossfade_values_follow_the_documented_curve(self):
        rnd = random.Random(5)
        for channels in (1, 2):
            a = Pcm(array('h', [rnd.randint(-32768, 32767) for _ in range(900 * channels)]).tobytes(), 8000, channels)
            b = Pcm(array('h', [rnd.randint(-32768, 32767) for _ in range(900 * channels)]).tobytes(), 8000, channels)
            overlap = 400                                              # 50 ms at 8 kHz
            out = samples_of(concat([a, b], 50))
            xa, xb = samples_of(a), samples_of(b)
            start = (900 - overlap) * channels
            for i in range(overlap):
                angle = (i + 0.5) / overlap * math.pi / 2
                for c in range(channels):
                    want = max(-32768.0, min(32767.0, xa[start + i * channels + c] * math.cos(angle) + xb[i * channels + c] * math.sin(angle)))
                    self.assertEqual(out[start + i * channels + c], round(want))

    def test_loud_crossfade_saturates_instead_of_wrapping(self):
        loud = synth.constant(30000, 1.0, 44100, 1)
        out = samples_of(concat([loud, loud], 500))
        self.assertEqual(max(out), 32767)
        self.assertGreater(min(out), 29000)

    def test_stereo_channels_are_mixed_separately(self):
        left_only = pcm_from_samples([8000, 0] * 22050, 44100, 2)
        right_only = pcm_from_samples([0, 8000] * 22050, 44100, 2)
        out = samples_of(concat([left_only, right_only], 400))
        n = len(out) // 2
        mid = n // 2
        self.assertAlmostEqual(out[2 * mid], 8000 * math.cos(math.pi / 4), delta=100)
        self.assertAlmostEqual(out[2 * mid + 1], 8000 * math.sin(math.pi / 4), delta=100)

    def test_notes_are_merged_without_repeats(self):
        a = synth.sine(220.0, 0.2).with_notes('first', 'shared')
        b = synth.sine(330.0, 0.2).with_notes('shared', 'second')
        self.assertEqual(concat([a, b]).notes, ('first', 'shared', 'second'))
        self.assertEqual(concat([a, b], 50).notes, ('first', 'shared', 'second'))

    def test_bad_input(self):
        a = synth.sine(220.0, 0.2)
        for parts in ([], (), None, a, 'ab', [a, 'x'], [a, None], [a, a.frames]):
            with self.assertRaises(AudioError, msg=repr(parts)[:40]):
                concat(parts)
        with self.assertRaises(AudioError) as caught:
            concat([a, synth.sine(220.0, 0.2, 48000, 2)])
        self.assertIn('station format', str(caught.exception))
        with self.assertRaises(AudioError):
            concat([a, synth.sine(220.0, 0.2, 44100, 1)])
        for ms in (-1, float('nan'), float('inf'), True, '5', 10 ** 9):
            with self.assertRaises(AudioError, msg=str(ms)):
                concat([a, a], ms)

    def test_total_size_is_capped(self):
        big = Pcm(bytes(60 * 1024 * 1024), 44100, 2)
        with self.assertRaises(AudioError) as caught:
            concat([big, big])
        self.assertEqual(caught.exception.code, 'too_large')

    def test_empty_parts_are_harmless(self):
        a = synth.sine(220.0, 0.2)
        empty = Pcm(b'', 44100, 2)
        self.assertEqual(concat([empty, a, empty]).frames, a.frames)
        self.assertEqual(concat([empty, a, empty], 50).frames, a.frames)


class NormalizeTests(unittest.TestCase):
    def test_reaches_the_target_loudness(self):
        for level, target in ((-30.0, -16.0), (-6.0, -23.0), (-45.0, -14.0)):
            with self.subTest(level=level, target=target):
                pcm = synth.sine(997.0, 2.0, 44100, 2, level)
                out, report = normalize_with_report(pcm, target, -1.0)
                self.assertEqual(out.frame_count, pcm.frame_count)
                self.assertAlmostEqual(analyze(out).loudness_lufs, target, delta=0.02)
                self.assertAlmostEqual(report.gain_db, target - analyze(pcm).loudness_lufs, delta=0.01)
                self.assertAlmostEqual(report.expected_lufs, analyze(out).loudness_lufs, delta=0.02)
                self.assertIsNone(report.limited_by)
                self.assertTrue(report.applied)
                self.assertEqual(out.notes, ())
                self.assertEqual(normalize(pcm, target, -1.0), out)

    def test_default_target_is_minus_16_lufs_with_a_minus_1_dbfs_ceiling(self):
        out = normalize(synth.noise(3.0, 44100, 2, -30.0, seed=4))
        self.assertAlmostEqual(analyze(out).loudness_lufs, -16.0, delta=0.05)
        self.assertLessEqual(analyze(out).peak_dbfs, -1.0)

    def test_the_ceiling_not_the_target_can_limit_the_gain_and_it_is_reported(self):
        values = [int(10 * math.sin(i / 7.0)) for i in range(44100 * 3)]       # a faint hum
        for i in range(0, len(values), 4410):
            values[i] = 20000                                                  # with loud clicks
        pcm = Pcm(array('h', values).tobytes(), 44100, 1)
        out, report = normalize_with_report(pcm, -16.0, -1.0)
        self.assertEqual(report.limited_by, 'ceiling')
        self.assertLess(analyze(out).loudness_lufs, -30.0)                      # nowhere near the target
        ceiling_sample = math.floor(32768 * 10 ** (-1.0 / 20))
        self.assertEqual(max(abs(v) for v in samples_of(out)), ceiling_sample)  # exactly at the ceiling, never over
        self.assertLessEqual(report.peak_after_dbfs, -1.0)
        self.assertGreater(report.gain_db, 0.0)
        self.assertTrue(any('peak ceiling' in note for note in out.notes))
        self.assertIn('instead of', report.message)
        self.assertIn(f'{report.gain_db:+.1f} dB', report.message)
        self.assertAlmostEqual(report.expected_lufs, analyze(out).loudness_lufs, delta=0.05)

    def test_never_clips_even_with_the_ceiling_at_full_scale(self):
        values = [int(900 * math.sin(i / 5.0)) for i in range(44100 * 2)]
        values[5000], values[6000] = 32767, -32768
        pcm = Pcm(array('h', values).tobytes(), 44100, 1)
        out = samples_of(normalize(pcm, -10.0, 0.0))
        self.assertTrue(all(-32768 <= v <= 32767 for v in out))
        for before, after in zip(values, out):                                   # nothing wrapped around
            if abs(before) > 50:
                self.assertEqual(before > 0, after > 0)

    def test_loud_audio_is_turned_down_without_changing_its_shape(self):
        pcm = synth.noise(3.0, 44100, 2, -3.0, seed=5)
        out, report = normalize_with_report(pcm, -16.0, -1.0)
        self.assertLess(report.gain_db, -5.0)
        self.assertLess(analyze(out).peak_dbfs, analyze(pcm).peak_dbfs - 5.0)
        ratio = [b / a for a, b in zip(samples_of(pcm)[:2000], samples_of(out)[:2000]) if abs(a) > 3000]
        self.assertLess(max(ratio) - min(ratio), 0.002)                          # one constant gain

    def test_silent_and_unmeasurably_quiet_audio_is_left_alone(self):
        for pcm in (synth.silence(1.0), synth.sine(997.0, 3.0, 44100, 2, -90.0), Pcm(b'', 44100, 2)):
            out, report = normalize_with_report(pcm)
            self.assertFalse(report.applied)
            self.assertEqual(out.frames, pcm.frames)
        out = normalize(synth.silence(1.0))
        self.assertTrue(any('too quiet' in note for note in out.notes))

    def test_quiet_audio_hits_the_boost_cap(self):
        pcm = synth.sine(997.0, 3.0, 44100, 2, -58.0)
        out, report = normalize_with_report(pcm, -16.0, -1.0)
        self.assertEqual(report.limited_by, 'boost_cap')
        self.assertAlmostEqual(report.gain_db, audio.MAX_BOOST_DB, delta=0.01)
        self.assertAlmostEqual(analyze(out).loudness_lufs, -18.0, delta=0.05)

    def test_normalising_twice_changes_nothing_more(self):
        once = normalize(synth.noise(3.0, 44100, 2, -26.0, seed=6))
        _, report = normalize_with_report(once)
        self.assertLess(abs(report.gain_db), 0.01)

    def test_mono_is_treated_as_playing_from_both_speakers(self):
        mono = synth.sine(500.0, 2.0, 44100, 1, -24.0)
        two = synth.sine(500.0, 2.0, 44100, 2, -24.0)
        _, a = normalize_with_report(mono)
        _, b = normalize_with_report(two)
        self.assertAlmostEqual(a.gain_db, b.gain_db, places=6)

    def test_bad_arguments(self):
        pcm = synth.sine(500.0, 1.0)
        for args in ((1.0, -1.0), (-80.0, -1.0), (float('nan'), -1.0), (-16.0, 1.0), (-16.0, -40.0), (-16.0, float('inf')), (True, -1.0), ('-16', -1.0)):
            with self.assertRaises(AudioError, msg=str(args)):
                normalize(pcm, *args)
        with self.assertRaises(AudioError):
            normalize(None)


class GainTests(unittest.TestCase):
    def test_gain_is_exact_for_whole_number_ratios(self):
        values = [0, 1, -1, 1000, -1000, 12345, -12345, 16383, -16384]
        pcm = Pcm(array('h', values).tobytes(), 44100, 1)
        twice = samples_of(apply_gain(pcm, 20 * math.log10(2.0)))
        self.assertEqual(twice, [2 * v for v in values])
        half = samples_of(apply_gain(pcm, -20 * math.log10(2.0)))
        for before, after in zip(values, half):
            self.assertLessEqual(abs(after - before / 2), 0.5001)

    def test_boost_saturates_and_never_wraps(self):
        values = [0, 5000, -5000, 20000, -20000, 32767, -32768]
        out = samples_of(apply_gain(Pcm(array('h', values).tobytes(), 44100, 1), 20.0))
        self.assertEqual(out, [0, 32767, -32768, 32767, -32768, 32767, -32768])

    def test_cut_off_samples_are_reported(self):
        values = [0, 5000, -5000, 20000, -20000, 32767, -32768, 100]
        out = apply_gain(Pcm(array('h', values).tobytes(), 44100, 1), 20.0)
        self.assertEqual(len(out.notes), 1)
        self.assertIn('6 sample(s)', out.notes[0])
        self.assertEqual(apply_gain(Pcm(array('h', values).tobytes(), 44100, 1), -6.0).notes, ())
        for pcm in (synth.noise(1.0, 44100, 2, -1.0, seed=3), synth.square(300.0, 1.0, 44100, 2, 0.0)):
            self.assertEqual(normalize(pcm, -10.0, 0.0).notes, ())      # normalising never cuts anything off

    def test_zero_and_extremes(self):
        pcm = synth.sine(440.0, 0.2)
        self.assertIs(apply_gain(pcm, 0.0), pcm)
        self.assertEqual(set(apply_gain(pcm, -120.0).frames), {0})
        empty = Pcm(b'', 44100, 2)
        self.assertIs(apply_gain(empty, 6.0), empty)
        for bad in (41.0, -121.0, float('nan'), float('inf'), None, True, '3'):
            with self.assertRaises(AudioError, msg=str(bad)):
                apply_gain(pcm, bad)

    def test_chunk_boundaries_do_not_matter(self):
        from myradio.audio import edit
        rnd = random.Random(21)
        values = [rnd.randint(-32768, 32767) for _ in range(5000)]
        pcm = Pcm(array('h', values).tobytes(), 44100, 1)
        whole = apply_gain(pcm, -4.5)
        with mock.patch.object(edit, '_GAIN_CHUNK', 777):
            self.assertEqual(apply_gain(pcm, -4.5), whole)
        fixed = int(math.floor(10 ** (-4.5 / 20) * (1 << 30)))
        self.assertEqual(samples_of(whole), [max(-32768, min(32767, (v * fixed + (1 << 29)) >> 30)) for v in values])

    def test_stereo_gain_keeps_channels_apart(self):
        values = [v for _ in range(3000) for v in (1000, -2000)]
        out = samples_of(apply_gain(stereo(values), 6.0))
        self.assertEqual(set(out[0::2]), {1995})          # 1000 * 1.99526 = 1995.26
        self.assertEqual(set(out[1::2]), {-3991})         # -2000 * 1.99526 = -3990.52, nearest whole number


class RobustnessTests(unittest.TestCase):
    def test_running_out_of_memory_becomes_a_plain_error(self):
        from myradio.audio import analysis, edit, rates, wav
        pcm = synth.sine(440.0, 0.2)
        for module, name, call in ((wav, '_locate', lambda: audio.read_wav(audio.write_wav(pcm))),
                                   (rates, 'build_plan', lambda: audio.resample(pcm, 48000)),
                                   (edit, '_scale_samples', lambda: apply_gain(pcm, 3.0)),
                                   (analysis, '_scan', lambda: analyze(pcm))):
            with self.subTest(name=name):
                with mock.patch.object(module, name, side_effect=MemoryError()):
                    with self.assertRaises(AudioError) as caught:
                        call()
                self.assertEqual(caught.exception.code, 'too_large')
                self.assertIn('not enough free memory', str(caught.exception))

    def test_audio_and_results_survive_pickle_copy_and_json(self):
        import copy
        import pickle
        pcm = synth.sine(440.0, 0.3, 48000, 2, -12.0).with_notes('note')
        analyze(pcm)                                           # fills the cache that travels with the object
        for clone in (pickle.loads(pickle.dumps(pcm)), copy.deepcopy(pcm), copy.copy(pcm)):
            self.assertEqual(clone, pcm)
            self.assertEqual(clone.notes, ('note',))
            self.assertEqual(analyze(clone), analyze(pcm))
        _, report = normalize_with_report(pcm)
        self.assertEqual(set(json.loads(json.dumps(report.as_dict(), allow_nan=False))), {f.name for f in dataclasses.fields(report)})
        self.assertEqual(pickle.loads(pickle.dumps(analyze(pcm))), analyze(pcm))
        self.assertEqual(pickle.loads(pickle.dumps(Problem('silent', 'x'))), Problem('silent', 'x'))


class TinyAudioTests(unittest.TestCase):
    def test_nothing_misbehaves_on_zero_to_a_few_frames(self):
        for channels in (1, 2):
            for rate in (8000, 44100, 48000):
                for frames in (0, 1, 2, 3, 5):
                    pcm = Pcm(array('h', [1234, -5678, 32767, -32768, 0, 9, -9, 400, 55, -1][:frames * channels] + [7] * max(0, frames * channels - 10)).tobytes(), rate, channels)
                    with self.subTest(channels=channels, rate=rate, frames=frames):
                        analyze(pcm)
                        loudness = audio.loudness_lufs(pcm)
                        self.assertTrue(math.isfinite(loudness))
                        for role in audio.ROLES:
                            validate_generated(pcm, role, 1.0, 5.0)
                        normalize(pcm)
                        fade(pcm, 10, 10)
                        trim_silence(pcm)
                        concat([pcm, pcm], 5)
                        apply_gain(pcm, 3.0)
                        audio.resample(pcm, 44100)
                        audio.resample(pcm, 8000, 'fast')
                        station = audio.to_station_format(pcm)
                        self.assertEqual((station.rate, station.channels), (44100, 2))
                        audio.write_wav(pcm)


class SelfCheckTests(unittest.TestCase):
    def test_reports_ok_quickly(self):
        result = audio.selfcheck()
        self.assertEqual(set(result), {'ok', 'message', 'seconds'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['message'], 'The audio tools are working.')
        self.assertLess(result['seconds'], 5.0)
        json.dumps(result, allow_nan=False)

    def test_reports_a_plain_failure_instead_of_raising(self):
        with mock.patch('myradio.audio.check.to_station_format', side_effect=AudioError('boom')):
            result = audio.selfcheck()
        self.assertFalse(result['ok'])
        self.assertIn('not working correctly', result['message'])
        self.assertNotIn('Traceback', result['message'])
        with mock.patch('myradio.audio.check.to_station_format', side_effect=RuntimeError('not an audio error')):
            result = audio.selfcheck()
        self.assertFalse(result['ok'])
        self.assertIn('RuntimeError', result['message'])
        with mock.patch('myradio.audio.check.read_wav', return_value=synth.silence(0.1)):
            result = audio.selfcheck()
        self.assertFalse(result['ok'])
        self.assertIn('saving and reloading', result['message'])


class SynthTests(unittest.TestCase):
    def test_signals_have_the_requested_properties(self):
        pcm = synth.sine(440.0, 0.5, 48000, 2, -20.0)
        self.assertEqual((pcm.rate, pcm.channels, pcm.frame_count), (48000, 2, 24000))
        values = samples_of(pcm)
        self.assertEqual(values[0::2], values[1::2])
        self.assertAlmostEqual(max(values), 3277, delta=2)
        self.assertEqual(synth.noise(0.1, seed=3), synth.noise(0.1, seed=3))
        self.assertNotEqual(synth.noise(0.1, seed=3), synth.noise(0.1, seed=4))
        stereo_noise = samples_of(synth.noise(0.1, 44100, 2, -20.0, seed=3))
        self.assertNotEqual(stereo_noise[0::2], stereo_noise[1::2])
        self.assertEqual(set(synth.constant(7, 0.01, 44100, 1).frames), {7, 0})
        self.assertEqual(synth.silence(0.5, 8000, 1).frame_count, 4000)

    def test_bad_arguments(self):
        for call in (lambda: synth.sine(-1.0, 1.0), lambda: synth.sine(30000.0, 1.0, 44100), lambda: synth.sine(440.0, -1.0),
                     lambda: synth.sine(440.0, 1.0, 44100, 3), lambda: synth.sine(440.0, 1.0, 44100.5), lambda: synth.constant(40000, 1.0),
                     lambda: synth.sine(440.0, 1.0, 44100, 2, 6.0), lambda: synth.silence(10 ** 6)):
            with self.assertRaises(AudioError):
                call()


if __name__ == '__main__':
    unittest.main()
