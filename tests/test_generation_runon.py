"""Generation: a Drama take that keeps talking past its script is refused and retried with a new seed."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import test_generation_fakes as fakes
except ImportError:
    from tests import test_generation_fakes as fakes

from myradio.generation.audio_api import load_audio
from myradio.generation.config import Env, parse_config
from myradio.generation.errors import WorkerError
from myradio.generation.payloads import normalize_payload
from myradio.generation.runners import HttpRunner, SubprocessRunner
from myradio.generation.worker_drama import DramaHttpWorker, ran_on
from myradio.generation.workers import KINDS
from myradio.generation.workers_base import GenerationRequest, WorkerContext

ID_TEXT = "You're listening to Late Epoch. Lofi beats to fine-tune models to."


def setUpModule():
    fakes.use_audio()


def tearDownModule():
    fakes.release_audio()


class RanOnTests(unittest.TestCase):
    def test_measured_takes_from_the_toymaker_benchmark(self):
        self.assertTrue(ran_on(ID_TEXT, 46.44))          # the station ID that ran on to the token limit
        self.assertFalse(ran_on(ID_TEXT, 8.59))          # the same ID, a good take
        self.assertFalse(ran_on("x" * 245, 17.37))       # a 245-character lead-in at a normal pace
        self.assertFalse(ran_on("x" * 177, 15.93))

    def test_short_lines_keep_room_for_a_natural_pause(self):
        self.assertFalse(ran_on("Ready.", 3.3))
        self.assertFalse(ran_on("Ready.", 6.0))
        self.assertTrue(ran_on("Ready.", 7.0))

    def test_markers_do_not_count_as_words(self):
        self.assertEqual(ran_on("<|speaker:0|> [laughs] Hi.", 6.3), ran_on("Hi.", 6.3))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        work_root = Path(self.temp.name)
        self.ctx = WorkerContext(load_audio, {}.get, time.time, HttpRunner(), SubprocessRunner(), work_root)
        self.server = fakes.FakeSpeechServer()
        self.addCleanup(self.server.close)
        cfg = parse_config({"workers": [{"id": "voice", "kind": "drama_http", "base_url": self.server.url, "smoke_test": False}]},
                           KINDS, Env())
        self.worker = DramaHttpWorker(cfg.workers["voice"], self.ctx)
        norm = normalize_payload("id", {"text": ID_TEXT})
        self.request = GenerationRequest("job1", "id", norm.data, 99, 1, 600.0, norm.min_s, norm.max_s, threading.Event(),
                                         Path(tempfile.mkdtemp(dir=work_root)))

    def long_take(self, seconds):
        audio = load_audio()
        return audio.read_wav(fakes.tone_wav(seconds=seconds, rate=24000, channels=1))

    def test_a_run_on_take_is_a_retryable_bad_output(self):
        with patch.object(DramaHttpWorker, "_post_speech", return_value=self.long_take(14.0)):
            with self.assertRaises(WorkerError) as caught:
                self.worker.generate(self.request)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("bad_output", True))
        self.assertIn("past the script", caught.exception.message)

    def test_a_take_of_the_right_length_is_kept(self):
        with patch.object(DramaHttpWorker, "_post_speech", return_value=self.long_take(5.0)):
            out = self.worker.generate(self.request)
        self.assertAlmostEqual(out.pcm.seconds, 5.0, places=1)


if __name__ == "__main__":
    unittest.main()
