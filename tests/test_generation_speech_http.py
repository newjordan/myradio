"""Generation: the general audio.cpp voice worker (speech_http), e.g. Jessica on Qwen3-TTS CustomVoice."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    import test_generation_fakes as fakes
except ImportError:
    from tests import test_generation_fakes as fakes

from myradio.generation.audio_api import load_audio
from myradio.generation.config import Env, parse_config
from myradio.generation.payloads import normalize_payload
from myradio.generation.runners import HttpRunner, SubprocessRunner
from myradio.generation.workers import KINDS, SpeechHttpWorker
from myradio.generation.workers_base import GenerationRequest, WorkerContext

JESSICA = {"model": "qwen3-customvoice", "voice": "vivian",
           "request_options": {"instruct": "A bright, warm young American woman reading a short radio news desk."}}


def setUpModule():
    fakes.use_audio()


def tearDownModule():
    fakes.release_audio()


class SpeechHttpTests(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work_root = Path(self.temp.name)
        self.ctx = WorkerContext(load_audio, {}.get, time.time, HttpRunner(), SubprocessRunner(), self.work_root)
        self.server = fakes.FakeSpeechServer()
        self.addCleanup(self.server.close)

    def parse(self, **extra):
        entry = {"id": "news", "kind": "speech_http", "base_url": self.server.url, **extra}
        return parse_config({"workers": [entry]}, KINDS, Env())

    def make(self, **extra):
        cfg = self.parse(**extra)
        self.assertEqual(cfg.problems, (), cfg.problems)
        return SpeechHttpWorker(cfg.workers["news"], self.ctx)

    def say(self, worker, text, role="update"):
        norm = normalize_payload(role, {"text": text, "allow_tags": True})
        request = GenerationRequest("job1", role, norm.data, 77, 1, 600.0, norm.min_s, norm.max_s, threading.Event(),
                                    Path(tempfile.mkdtemp(dir=self.work_root)))
        return worker.generate(request)

    def test_jessica_settings_reach_the_server_without_drama_markers(self):
        worker = self.make(smoke_test=False, **JESSICA)
        out = self.say(worker, "<|speaker:0|>Jessica with the news on the hour.")
        self.assertGreater(out.pcm.seconds, 0.5)
        body = json.loads(self.server.speech_requests()[-1]["body"])
        self.assertEqual((body["model"], body["voice"], body["input"]), ("qwen3-customvoice", "vivian", "Jessica with the news on the hour."))
        self.assertEqual(body["options"], JESSICA["request_options"])
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["response_format"], "wav")

    def test_the_warm_up_line_has_no_markers(self):
        worker = self.make(**JESSICA)
        self.say(worker, "Jessica with the news on the hour.")
        warm_up = json.loads(self.server.speech_requests()[0]["body"])
        self.assertEqual(warm_up["input"], "Ready.")

    def test_markers_kept_when_asked_and_tokens_from_the_rate(self):
        worker = self.make(smoke_test=False, model="drama3", speaker_markers=True, tokens_per_second=21.5)
        self.say(worker, "<|speaker:2|>Hello there, night owls.", role="host_break")
        body = json.loads(self.server.speech_requests()[-1]["body"])
        self.assertTrue(body["input"].startswith("<|speaker:2|>"))
        self.assertEqual(body["max_tokens"], int(45 * 21.5) + 1)

    def test_cloning_voices_send_the_reference(self):
        worker = self.make(smoke_test=False, model="chatterbox", voice="/srv/voices/juno.wav",
                           reference_text="Oh, we are live!", request_options={"exaggeration": 0.9})
        self.say(worker, "Up next, a brand new song.", role="host_break")
        body = json.loads(self.server.speech_requests()[-1]["body"])
        self.assertEqual((body["voice"], body["reference_text"], body["options"]), ("/srv/voices/juno.wav", "Oh, we are live!", {"exaggeration": 0.9}))

    def test_each_speaker_slot_gets_its_own_voice(self):
        worker = self.make(smoke_test=False, model="zonos3", voice="Savannah_en-American_F_1",
                           speaker_voices={"3": "Thaddeus_en-announcervoice_M_1", "7": "Wyatt_en-American_M_1"})
        sent = []
        for text in ("<|speaker:3|>Here is the news.", "<|speaker:7|>Back to the music.", "<|speaker:5|>Thanks for listening."):
            self.say(worker, text, role="host_break")
            sent.append(json.loads(self.server.speech_requests()[-1]["body"]))
        self.assertEqual([b["voice"] for b in sent],
                         ["Thaddeus_en-announcervoice_M_1", "Wyatt_en-American_M_1", "Savannah_en-American_F_1"])
        self.assertEqual(sent[0]["input"], "Here is the news.")

    def test_fade_out_is_sent_only_when_set(self):
        worker = self.make(smoke_test=False, model="zonos3", fade_out_ms=120)
        self.say(worker, "Back to the music.", role="host_break")
        self.assertEqual(json.loads(self.server.speech_requests()[-1]["body"])["fade_out_ms"], 120)
        plain = self.make(smoke_test=False, model="zonos3")
        self.say(plain, "Back to the music.", role="host_break")
        self.assertNotIn("fade_out_ms", json.loads(self.server.speech_requests()[-1]["body"]))
        self.assertTrue(self.parse(model="zonos3", fade_out_ms=5000).problems)

    def test_bad_speaker_voices_are_refused(self):
        for bad in ({"32": "x"}, {"a": "x"}, {"1": ""}, {"1": 5}, ["x"]):
            with self.subTest(bad=bad):
                self.assertTrue(self.parse(model="zonos3", speaker_voices=bad).problems)

    def test_the_model_must_be_named(self):
        self.assertTrue(self.parse().problems)

    def test_bad_request_options_are_refused(self):
        for bad in ({"Bad Key": 1}, {"instruct": ["a", "list"]}, {"x": float("nan")}):
            with self.subTest(bad=bad):
                self.assertTrue(self.parse(model="qwen3-customvoice", request_options=bad).problems)

    def test_it_can_serve_the_news_role(self):
        cfg = self.parse(roles=["update", "id"], **JESSICA)
        self.assertEqual(cfg.problems, ())
        self.assertEqual(set(cfg.workers["news"].roles), {"update", "id"})


if __name__ == "__main__":
    unittest.main()
