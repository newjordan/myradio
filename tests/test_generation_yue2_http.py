"""Generation: the YuE2 worker that talks to a resident audio.cpp server (yue2_http), against a fake server."""
import base64
import http.server
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
from myradio.generation.errors import WorkerError
from myradio.generation.payloads import normalize_payload
from myradio.generation.runners import HttpRunner, SubprocessRunner
from myradio.generation.workers import KINDS, Yue2HttpWorker
from myradio.generation.workers_base import GenerationRequest, WorkerContext

LYRICS = "[verse]\nDesk lamp glowing, tea gone cold\n\n[chorus]\nEasy now, a little at a time"


def setUpModule():
    fakes.use_audio()


def tearDownModule():
    fakes.release_audio()


class FakeAudioCpp:
    """GET /v1/models and POST /v1/tasks/run like audiocpp_server, with switchable answers."""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.loaded = True
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, body, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                models = {"object": "list", "data": [{"id": "yue2", "family": "yue2", "task": "gen", "loaded": outer.loaded},
                                                     {"id": "drama3", "family": "fish_drama3", "task": "tts", "loaded": True}]}
                self._send(200, json.dumps(models).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                outer.requests.append({"path": self.path, "body": body})
                if outer.mode == "error500":
                    return self._send(500, b'{"error": "model failed"}')
                if outer.mode == "busy503":
                    return self._send(503, b'{"error": "busy"}')
                if outer.mode == "no_audio":
                    return self._send(200, b'{"timing": {}}')
                if outer.mode == "not_wav":
                    return self._send(200, json.dumps({"audio": base64.b64encode(b"ID3 not a wav file at all").decode()}).encode())
                wav = fakes.tone_wav(seconds=2.0, rate=48000, channels=2)
                answer = {"audio": base64.b64encode(wav).decode(), "sample_rate": 48000, "channels": 2,
                          "timing": {"wall_ms": 1500, "audio_duration_ms": 2000, "rtf": 0.75}}
                self._send(200, json.dumps(answer).encode())

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Yue2HttpTests(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work_root = Path(self.temp.name) / "work"
        self.work_root.mkdir()
        self.ctx = WorkerContext(load_audio, {}.get, time.time, HttpRunner(), SubprocessRunner(), self.work_root)
        self.cancel = threading.Event()
        self.server = FakeAudioCpp()
        self.addCleanup(self.server.close)

    def make(self, **extra):
        entry = {"id": "music", "kind": "yue2_http", "base_url": self.server.url, **extra}
        cfg = parse_config({"workers": [entry]}, KINDS, Env())
        self.assertEqual(cfg.problems, (), cfg.problems)
        return Yue2HttpWorker(cfg.workers["music"], self.ctx)

    def request(self, payload, seed=4321):
        norm = normalize_payload("song", payload)
        work = Path(tempfile.mkdtemp(prefix="gen-", dir=self.work_root))
        return GenerationRequest("job1", "song", norm.data, seed, 1, 600.0, norm.min_s, norm.max_s, self.cancel, work)

    def test_a_song_is_one_task_request_and_the_answer_is_decoded(self):
        worker = self.make(request_options={"cot": "full", "guidance_scale": 1.0, "semantic_max_tokens": 4500})
        out = worker.generate(self.request({"tags": "lofi hip hop, mellow", "lyrics": LYRICS, "duration_s": 150, "steps": 8}))
        self.assertEqual((out.pcm.rate, out.pcm.channels), (48000, 2))
        self.assertAlmostEqual(out.pcm.seconds, 2.0, places=2)
        self.assertEqual((out.meta["server_wall_s"], out.meta["server_audio_s"], out.meta["resident"]), (1.5, 2.0, True))
        sent = self.server.requests[0]
        self.assertEqual(sent["path"], "/v1/tasks/run")
        self.assertEqual(sent["body"]["model"], "yue2")
        request = sent["body"]["request"]
        self.assertEqual((request["lyrics"], request["seed"]), (LYRICS, 4321))
        self.assertEqual(request["options"], {"cot": "full", "guidance_scale": "1", "semantic_max_tokens": "4500",
                                              "style": "lofi hip hop, mellow", "num_inference_steps": "8"})

    def test_the_token_budget_follows_the_song_length_unless_fixed(self):
        worker = self.make()
        worker.generate(self.request({"tags": "lofi", "lyrics": LYRICS, "duration_s": 120, "steps": 4}))
        options = self.server.requests[0]["body"]["request"]["options"]
        self.assertEqual((options["semantic_max_tokens"], options["num_inference_steps"], options["cot"]), ("3000", "4", "full"))

    def test_an_instrumental_is_sent_as_instrumental(self):
        self.make().generate(self.request({"tags": "ambient pads", "duration_s": 30}))
        self.assertEqual(self.server.requests[0]["body"]["request"]["lyrics"], "[instrumental]")

    def test_song_fields_cannot_be_fixed_in_the_settings(self):
        for key in ("style", "lyrics", "num_inference_steps"):
            with self.subTest(key):
                cfg = parse_config({"workers": [{"id": "music", "kind": "yue2_http", "base_url": self.server.url,
                                                 "request_options": {key: "x"}}]}, KINDS, Env())
                self.assertTrue(cfg.problems)
                self.assertIn(key, cfg.problems[0])

    def test_a_remote_server_needs_allow_remote(self):
        cfg = parse_config({"workers": [{"id": "music", "kind": "yue2_http", "base_url": "http://192.168.1.20:8090"}]}, KINDS, Env())
        self.assertTrue(cfg.problems)

    def test_bad_answers_become_plain_failures(self):
        worker = self.make()
        cases = {"error500": ("worker_failed", True), "busy503": ("worker_unreachable", True),
                 "no_audio": ("bad_output", True), "not_wav": ("bad_output", True)}
        for mode, (code, retryable) in cases.items():
            with self.subTest(mode):
                self.server.mode = mode
                with self.assertRaises(WorkerError) as caught:
                    worker.generate(self.request({"tags": "lofi", "lyrics": LYRICS, "duration_s": 60}))
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retryable)

    def test_the_probe_checks_that_the_model_is_on_the_server(self):
        result = self.make().probe(False, self.cancel)
        self.assertTrue(result.ok, result.message)
        self.assertIn("loaded", result.message)
        missing = self.make(model="yue2-other").probe(False, self.cancel)
        self.assertFalse(missing.ok)
        self.assertIn("yue2-other", missing.message)

    def test_a_server_that_is_down_is_unreachable(self):
        worker = self.make()
        self.server.close()
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "unreachable"))

    def test_a_full_probe_makes_a_short_test_song(self):
        result = self.make().probe(True, self.cancel)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.evidence, "test_clip_made")
        self.assertEqual(self.server.requests[0]["body"]["request"]["lyrics"], "[instrumental]")


if __name__ == "__main__":
    unittest.main()
