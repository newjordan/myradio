"""Generation: the Drama speech worker, the YuE2 command line worker and the fixture worker, against fakes."""
import dataclasses
import json
import math
import os
import sys
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
from myradio.generation.errors import Cancelled, ConfigError, WorkerError
from myradio.generation.payloads import normalize_payload
from myradio.generation.runners import HttpRunner, SubprocessRunner
from myradio.generation.worker_fixture import ENV_VAR, FixtureWorker
from myradio.generation.workers import DramaHttpWorker, KINDS, Yue2CliWorker
from myradio.generation.workers_base import GenerationRequest, WorkerContext

FIXTURES_ON = {ENV_VAR: "1"}


def setUpModule():
    fakes.use_audio()


def tearDownModule():
    fakes.release_audio()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.work_root = self.dir / "work"
        self.work_root.mkdir()
        self.notes = self.dir / "notes"
        self.notes.mkdir()
        self.secrets = {}
        self.ctx = WorkerContext(load_audio, self.secrets.get, time.time, HttpRunner(), SubprocessRunner(), self.work_root)
        self.cancel = threading.Event()

    def spec(self, entry, env=None):
        cfg = parse_config({"workers": [entry]}, KINDS, env or Env())
        self.assertEqual(cfg.problems, (), cfg.problems)
        return cfg.workers[entry["id"]]

    def request(self, role, payload, *, seed=1234, attempt=1, deadline_s=600.0):
        norm = normalize_payload(role, payload)
        work = Path(tempfile.mkdtemp(prefix="gen-", dir=self.work_root))
        return GenerationRequest("job1", role, norm.data, seed, attempt, deadline_s, norm.min_s, norm.max_s, self.cancel, work)

    def note(self, name):
        path = self.notes / name
        return path.read_text() if path.exists() else ""

    def assertDead(self, *pids):
        for pid in pids:
            self.assertTrue(fakes.wait_for(lambda pid=pid: not fakes.pid_alive(pid), 5), f"process {pid} is still running")


def with_options(spec, **options):
    """Tests need timeouts shorter than the settings allow; they edit the validated spec directly."""
    return dataclasses.replace(spec, options={**spec.options, **options})


class DramaTests(WorkerCase):
    def setUp(self):
        super().setUp()
        self.server = fakes.FakeSpeechServer()
        self.addCleanup(self.server.close)

    def make(self, entry_extra=None, **options):
        entry = {"id": "voice", "kind": "drama_http", "base_url": self.server.url, **(entry_extra or {})}
        spec = with_options(self.spec(entry), **options) if options else self.spec(entry)
        return DramaHttpWorker(spec, self.ctx)

    def say(self, worker, text="Good evening and welcome to the station.", role="host_break", **payload):
        return worker.generate(self.request(role, {"text": text, **payload}))

    def failure(self, worker, text="Good evening and welcome to the station.", role="host_break", **payload):
        with self.assertRaises(WorkerError) as caught:
            self.say(worker, text, role, **payload)
        return caught.exception

    def test_a_24khz_mono_answer_is_decoded_and_the_request_is_well_formed(self):
        worker = self.make()
        out = self.say(worker, speaker=2, seed=77)
        self.assertEqual((out.pcm.rate, out.pcm.channels), (24000, 1))
        self.assertAlmostEqual(out.pcm.seconds, 1.2, places=2)
        self.assertEqual(out.meta["takes"], 1)
        warm_up, take = (json.loads(r["body"]) for r in self.server.speech_requests())
        self.assertEqual(warm_up["input"], "<|speaker:0|> Ready.")
        self.assertEqual(take["input"], "<|speaker:2|> Good evening and welcome to the station.")
        self.assertEqual((take["model"], take["response_format"], take["seed"]), ("drama-3", "wav", 1234))
        self.assertEqual(take["max_tokens"], math.ceil(45 * 21.5))
        self.assertEqual(self.server.speech_requests()[1]["path"], "/v1/audio/speech")

    def test_other_wav_shapes_are_accepted(self):
        for mode, rate, channels in (("ok44", 44100, 2), ("ok8bit", 22050, 1)):
            with self.subTest(mode):
                self.server.mode = mode
                out = self.say(self.make())
                self.assertEqual((out.pcm.rate, out.pcm.channels), (rate, channels))
                self.assertAlmostEqual(out.pcm.seconds, 1.2, places=2)

    def test_error_answers_become_plain_retryable_or_final_failures(self):
        cases = {"error500": ("worker_failed", True), "busy503": ("worker_unreachable", True), "forbidden": ("worker_failed", False),
                 "redirect": ("worker_failed", False)}
        for mode, (code, retryable) in cases.items():
            with self.subTest(mode):
                self.server.mode = mode
                worker = self.make(smoke_test=False)
                error = self.failure(worker)
                self.assertEqual((error.code, error.retryable), (code, retryable))
                self.assertNotIn("HTTP", error.message)
                self.assertIn("HTTP", error.detail)

    def test_results_that_are_not_audio_are_refused(self):
        for mode in ("wrong_type", "html", "garbage", "truncated", "not_http", "gzip"):
            with self.subTest(mode):
                self.server.mode = mode
                error = self.failure(self.make(smoke_test=False))
                self.assertEqual(error.code, "bad_output", mode)
                for jargon in ("HTTP", "RIFF", "gzip", "header", "Traceback", "chunk"):
                    self.assertNotIn(jargon, error.message)

    def test_a_silent_result_is_refused(self):
        self.server.mode = "silence"
        self.assertEqual(self.failure(self.make(smoke_test=False)).code, "silent_output")

    def test_an_oversize_answer_is_refused_for_good(self):
        self.server.mode = "oversize_header"
        error = self.failure(self.make(smoke_test=False, max_response_bytes=1024 * 1024))
        self.assertEqual((error.code, error.retryable), ("output_too_large", False))

    def test_a_slow_server_is_given_up_on_at_the_deadline(self):
        for mode in ("drip_headers", "drip_body", "hang"):
            with self.subTest(mode):
                self.server.mode = mode
                began = time.monotonic()
                error = self.failure(self.make(smoke_test=False, request_timeout_s=0.8))
                self.assertEqual(error.code, "worker_timeout")
                self.assertLess(time.monotonic() - began, 5.0)

    def test_the_whole_job_has_a_deadline_across_takes(self):
        text = " ".join(f"Sentence number {i} is here to fill the script." for i in range(30))
        self.server.mode = "hang"
        began = time.monotonic()
        error = self.failure(self.make(smoke_test=False, request_timeout_s=0.5, job_timeout_s=1.2, max_take_chars=100), text)
        self.assertEqual(error.code, "worker_timeout")
        self.assertLess(time.monotonic() - began, 6.0)

    def test_cancel_abandons_the_request(self):
        self.server.mode = "hang"
        threading.Timer(0.3, self.cancel.set).start()
        began = time.monotonic()
        with self.assertRaises(Cancelled):
            self.say(self.make(smoke_test=False))
        self.assertLess(time.monotonic() - began, 5.0)

    def test_the_first_request_warms_the_server_up_once(self):
        worker = self.make()
        self.assertFalse(worker.warm)
        self.say(worker)
        self.assertTrue(worker.warm)
        self.assertEqual(len(self.server.speech_requests()), 2)
        self.say(worker)
        self.assertEqual(len(self.server.speech_requests()), 3, "no second warm-up")
        worker.invalidate()
        self.assertFalse(worker.warm)
        self.say(worker)
        self.assertEqual(len(self.server.speech_requests()), 5)

    def test_a_lost_connection_makes_the_worker_cold_again(self):
        worker = self.make(request_timeout_s=3.0)
        self.say(worker)
        self.assertTrue(worker.warm)
        self.server.close()
        error = self.failure(worker)
        self.assertEqual(error.code, "worker_unreachable")
        self.assertFalse(worker.warm)

    def test_long_scripts_are_split_into_takes_and_joined_with_a_crossfade(self):
        text = " ".join(f"This is sentence number {i} of the long bulletin." for i in range(18))
        worker = self.make(max_take_chars=160, crossfade_ms=60)
        out = worker.generate(self.request("update", {"text": text}, seed=4000000000))
        bodies = [json.loads(r["body"]) for r in self.server.speech_requests()][1:]
        n = len(bodies)
        self.assertGreater(n, 4)
        self.assertEqual(out.meta["takes"], n)
        self.assertTrue(all(len(b["input"]) - len("<|speaker:0|> ") <= 160 for b in bodies))
        self.assertEqual([b["seed"] for b in bodies], [(4000000000 + i) % 2 ** 32 for i in range(n)])
        joined = " ".join(b["input"].split(" ", 1)[1] for b in bodies)
        self.assertEqual(joined, text)
        expected = n * 1.2 - (n - 1) * 0.06
        self.assertAlmostEqual(out.pcm.seconds, expected, delta=0.08)

    def test_one_bad_take_fails_the_whole_job(self):
        text = " ".join(f"This is sentence number {i} of the long bulletin." for i in range(12))
        self.server.modes = ["ok24"] + ["ok24", "ok24", "garbage"]
        error = self.failure(self.make(max_take_chars=120), text, role="update")
        self.assertEqual(error.code, "bad_output")

    def test_an_access_key_is_sent_but_never_shown(self):
        self.secrets["drama.token"] = b"s3cr3t-key-123\n"
        worker = self.make({"auth_token_file": "drama.token"})
        self.say(worker)
        self.assertTrue(all(r["headers"].get("authorization") == "Bearer s3cr3t-key-123" for r in self.server.speech_requests()))
        shown = json.dumps([worker.describe(), worker.file_problems(), worker.identity()])
        self.assertNotIn("s3cr3t", shown)
        self.assertTrue(worker.describe()["protects_with_key"])

    def test_a_missing_or_broken_access_key_is_a_clear_final_failure(self):
        worker = self.make({"auth_token_file": "drama.token"})
        self.assertIn("access key", " ".join(worker.file_problems()))
        error = self.failure(worker)
        self.assertEqual((error.code, error.retryable), ("worker_failed", False))
        self.assertEqual(self.server.requests, [])
        self.secrets["drama.token"] = b"bad key with spaces\r\nInjected: header"
        self.assertEqual(self.failure(worker).code, "worker_failed")
        self.assertEqual(self.server.requests, [])

    def test_probe_walks_from_checking_to_warm_without_overstating(self):
        worker = self.make()
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state, result.needs_full, result.evidence), (False, "warming", True, "setup_checked"))
        self.assertEqual(result.version, "fake-audiocpp/0.1")
        self.assertEqual(self.server.speech_requests(), [], "the quick check never makes speech")
        result = worker.probe(True, self.cancel)
        self.assertEqual((result.ok, result.state, result.evidence), (True, "ready", "test_clip_made"))
        self.assertEqual(len(self.server.speech_requests()), 1)
        again = worker.probe(False, self.cancel)
        self.assertEqual((again.ok, again.evidence), (True, "test_clip_made"))
        self.assertEqual(len(self.server.speech_requests()), 1)

    def test_probe_without_a_warm_up_test_is_honest_about_what_it_proved(self):
        worker = self.make(smoke_test=False)
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.evidence, result.state), (True, "setup_checked", "ready"))

    def test_probe_reports_a_dead_or_starting_server_in_plain_words(self):
        worker = self.make()
        self.server.health = "busy503"
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "warming"))
        self.assertIn("not ready yet", result.message)
        self.server.close()
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "unreachable"))
        self.assertIn("not answering", result.message)
        self.assertTrue(result.hint)

    def test_probe_notices_a_refused_access_key(self):
        self.server.health = "forbidden"
        result = self.make().probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "failed"))
        self.assertIn("access key", result.message)
        self.assertNotIn("403", result.message)

    def test_probe_full_failure_keeps_the_worker_not_ready(self):
        self.server.mode = "silence"
        result = self.make().probe(True, self.cancel)
        self.assertFalse(result.ok)
        self.assertIn(result.state, ("failed", "warming"))
        self.assertNotEqual(result.evidence, "test_clip_made")

    def test_a_full_check_makes_a_real_clip_even_when_the_maker_does_not_test_by_itself(self):
        worker = self.make(smoke_test=False)
        quick = worker.probe(False, self.cancel)
        self.assertEqual((quick.ok, quick.evidence, quick.needs_full), (True, "setup_checked", False))
        self.assertEqual(self.server.speech_requests(), [], "a quick check never makes speech")
        full = worker.probe(True, self.cancel)
        self.assertEqual((full.ok, full.evidence), (True, "test_clip_made"))
        self.assertEqual(len(self.server.speech_requests()), 1, "asked for by a person, so a clip is really made")

    def test_a_failed_full_check_asks_to_be_repeated_only_by_a_maker_that_tests_by_itself(self):
        self.server.mode = "error500"
        for smoke, repeat in ((True, True), (False, False)):
            with self.subTest(smoke_test=smoke):
                result = self.make(smoke_test=smoke).probe(True, self.cancel)
                self.assertEqual((result.ok, result.needs_full), (False, repeat))

    def test_missing_model_files_are_reported_before_any_network_use(self):
        worker = self.make({"model_files": [str(self.dir / "missing.gguf")]})
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "missing_files"))
        self.assertIn("missing.gguf", result.message)
        self.assertEqual(self.server.requests, [])

    def test_generation_options_are_passed_on(self):
        worker = self.make({"options": {"temperature": 0.9, "top_p": 0.8}}, smoke_test=False)
        self.say(worker)
        body = json.loads(self.server.speech_requests()[0]["body"])
        self.assertEqual((body["temperature"], body["top_p"]), (0.9, 0.8))


class YueTests(WorkerCase):
    def make(self, script="ok", extra=None, entry=None, **options):
        program = fakes.write_script(self.dir, script)
        command = [sys.executable, program, "--out", "{output_file}", "--tags", "{tags}", "--seed", "{seed}", "--lyrics-file", "{lyrics_file}",
                   "--notes", str(self.notes), "--seconds", "1.5"] + (extra or [])
        raw = {"id": "music", "kind": "yue2_cli", "command": command, **(entry or {})}
        spec = self.spec(raw)
        spec = with_options(spec, **options) if options else spec
        return Yue2CliWorker(spec, self.ctx)

    def song(self, worker, tags="warm tape loop, slow, 70 BPM", **payload):
        return worker.generate(self.request("song", {"tags": tags, "duration_s": 5, **payload}, seed=424242))

    def failure(self, worker, **kw):
        with self.assertRaises(WorkerError) as caught:
            self.song(worker, **kw)
        return caught.exception

    def argv(self):
        return json.loads(self.note("argv.json").strip().splitlines()[-1])

    def pids(self):
        return [int(x) for x in self.note("pid.txt").split()]

    def test_success_substitutes_every_placeholder_into_exactly_one_argument(self):
        worker = self.make()
        nasty = "dreamy $(touch /tmp/never) `id` ; && | > x  \"quoted\" 'single' 100% {not_a_placeholder}"
        out = self.song(worker, tags=nasty, lyrics="[verse]\nline one\nline two\n[chorus]\nsing it", steps=16)
        self.assertEqual((out.pcm.rate, out.pcm.channels), (44100, 2))
        argv = self.argv()
        self.assertEqual(argv[argv.index("--tags") + 1], " ".join(nasty.split()))
        self.assertEqual(argv[argv.index("--seed") + 1], "424242")
        self.assertTrue(argv[argv.index("--out") + 1].endswith("output.wav"))
        self.assertEqual(self.note("lyrics.txt").strip(), "[verse]\nline one\nline two\n[chorus]\nsing it")
        self.assertEqual(os.path.realpath(self.note("cwd.txt").strip()), os.path.realpath(os.path.dirname(argv[argv.index("--out") + 1])))
        self.assertTrue(self.note("tmpdir.txt").strip().startswith(str(self.work_root)))

    def test_an_instrumental_song_passes_the_instrumental_marker(self):
        self.song(self.make())
        self.assertEqual(self.note("lyrics.txt").strip(), "[instrumental]")

    def test_all_documented_placeholders_exist(self):
        placeholders = ["output_file", "work_dir", "tags", "lyrics", "lyrics_file", "seed", "steps", "duration_s", "instrumental", "prompt_id"]
        command = [sys.executable, fakes.write_script(self.dir, "ok"), "--notes", str(self.notes), "--out", "{output_file}"]
        for name in placeholders[1:]:
            command += ["--x-" + name, "{" + name + "}"]
        worker = Yue2CliWorker(self.spec({"id": "music", "kind": "yue2_cli", "command": command}), self.ctx)
        self.song(worker, prompt_id="warm-tape-loop", steps=12)
        argv = self.argv()
        got = {name: argv[argv.index("--x-" + name) + 1] for name in placeholders[1:]}
        self.assertEqual((got["seed"], got["steps"], got["duration_s"], got["instrumental"], got["prompt_id"]), ("424242", "12", "5", "true", "warm-tape-loop"))
        self.assertTrue(got["lyrics_file"].endswith("lyrics.txt"))
        self.assertTrue(os.path.isabs(got["work_dir"]))

    def test_a_nonzero_exit_is_a_plain_failure_with_technical_detail_kept_apart(self):
        error = self.failure(self.make("fail"))
        self.assertEqual((error.code, error.retryable), ("worker_failed", True))
        self.assertNotIn("/secret/path", error.message)
        self.assertNotIn("3", error.message)
        self.assertIn("exit code 3", error.detail)
        self.assertIn("model could not be loaded", error.detail)

    def test_no_output_and_garbage_output_are_failures(self):
        self.assertEqual(self.failure(self.make("nothing")).code, "no_output")
        self.assertEqual(self.failure(self.make("garbage")).code, "bad_output")

    def test_a_hung_program_is_killed_at_the_deadline(self):
        began = time.monotonic()
        error = self.failure(self.make("hang", timeout_s=0.8, kill_grace_s=1.0))
        self.assertEqual(error.code, "worker_timeout")
        self.assertLess(time.monotonic() - began, 10.0)
        self.assertDead(*self.pids())

    def test_a_program_that_ignores_terminate_and_its_child_are_killed(self):
        error = self.failure(self.make("child", timeout_s=0.8, kill_grace_s=0.5))
        self.assertEqual(error.code, "worker_timeout")
        self.assertDead(*self.pids())
        error = self.failure(self.make("ignore_term", timeout_s=0.6, kill_grace_s=0.5))
        self.assertEqual(error.code, "worker_timeout")
        self.assertDead(*self.pids())

    def test_a_flood_of_output_is_stopped(self):
        error = self.failure(self.make("huge", max_output_bytes=2 * 1024 * 1024, kill_grace_s=1.0))
        self.assertEqual((error.code, error.retryable), ("output_too_large", False))

    def test_cancel_stops_the_program(self):
        threading.Timer(0.5, self.cancel.set).start()
        with self.assertRaises(Cancelled):
            self.song(self.make("hang", kill_grace_s=1.0))
        self.assertDead(*self.pids())

    def test_the_plan_step_runs_first_for_instrumental_songs_only(self):
        plan = fakes.write_script(self.dir, "plan")
        entry = {"plan_command": [sys.executable, plan, "--plan", "{plan_file}", "--notes", str(self.notes)],
                 "instrumental_command": [sys.executable, plan, "--use-plan", "{plan_file}", "--out", "{output_file}", "--notes", str(self.notes)]}
        worker = self.make("ok", entry=entry)
        self.song(worker)
        self.assertEqual(self.note("plan_ran.txt").strip(), "yes")
        self.assertIn("with plan=", self.note("main_ran.txt"))
        (self.notes / "plan_ran.txt").unlink()
        (self.notes / "main_ran.txt").unlink()
        self.song(worker, lyrics="[verse]\nsung words here\n[chorus]\nmore")
        self.assertEqual(self.note("plan_ran.txt"), "", "songs with lyrics skip the plan")
        self.assertTrue(self.note("argv.json"), "the ordinary command ran instead")

    def test_a_missing_plan_is_a_failure(self):
        nothing = fakes.write_script(self.dir, "nothing")
        entry = {"plan_command": [sys.executable, nothing, "--plan", "{plan_file}"],
                 "instrumental_command": [sys.executable, nothing, "--use-plan", "{plan_file}", "--out", "{output_file}"]}
        error = self.failure(self.make("ok", entry=entry))
        self.assertEqual(error.code, "no_output")
        self.assertIn("plan", error.message)

    def test_text_that_starts_with_a_dash_never_reaches_a_command(self):
        worker = self.make(entry={"command": [sys.executable, fakes.write_script(self.dir, "ok"), "{tags}", "--out", "{output_file}", "--notes", str(self.notes)]})
        request = self.request("song", {"tags": "ambient pads", "duration_s": 5})
        request = dataclasses.replace(request, payload={**request.payload, "tags": "--out=/etc/passwd"})
        with self.assertRaises(WorkerError) as caught:
            worker.generate(request)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.note("argv.json"), "")

    def test_probe_checks_files_version_and_the_optional_test_song(self):
        missing = self.make(entry={"model_files": [str(self.dir / "weights.gguf")]})
        result = missing.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "missing_files"))
        self.assertIn("weights.gguf", result.message)
        (self.dir / "weights.gguf").write_text("weights")
        version = fakes.write_script(self.dir, "version")
        worker = self.make(entry={"model_files": [str(self.dir / "weights.gguf")], "version_command": [sys.executable, version]})
        result = worker.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state, result.evidence), (True, "ready", "setup_checked"))
        self.assertEqual(result.version, "fake-yue2 1.2.3 (build 77)")
        self.assertRegex(worker.identity()["hash"], r"^[0-9a-f]{64}$")
        self.assertIn("No test song", result.message)
        bad = self.make(entry={"version_command": [sys.executable, fakes.write_script(self.dir, "fail")]})
        result = bad.probe(False, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "failed"))
        self.assertNotIn("/secret/path", result.message)

    def test_the_version_check_runs_once_and_again_only_when_asked(self):
        version = fakes.write_script(self.dir, "version")
        worker = self.make(entry={"version_command": [sys.executable, version, "--notes", str(self.notes)]})
        for _ in range(3):
            self.assertTrue(worker.probe(False, self.cancel).ok)
        self.assertEqual(self.note("version_ran.txt").count("x"), 1, "periodic checks must not wake the program every time")
        worker.refresh()
        worker.probe(False, self.cancel)
        self.assertEqual(self.note("version_ran.txt").count("x"), 2)
        worker.probe(True, self.cancel)
        self.assertEqual(self.note("version_ran.txt").count("x"), 3)

    def test_an_instrumental_command_alone_is_used_for_instrumental_songs_only(self):
        ok = fakes.write_script(self.dir, "ok")
        entry = {"instrumental_command": [sys.executable, ok, "--out", "{output_file}", "--notes", str(self.notes), "--marker", "instrumental"]}
        worker = self.make("ok", entry=entry)
        worker.generate(self.request("song", {"tags": "ambient pads", "duration_s": 5}))
        self.assertIn("--marker", self.argv())
        self.assertNotIn("--tags", self.argv())
        worker.generate(self.request("song", {"tags": "ambient pads", "duration_s": 5, "lyrics": "[verse]\nsung words here\n[chorus]\nmore"}))
        self.assertNotIn("--marker", self.argv())
        self.assertIn("--tags", self.argv())

    def test_probe_with_a_test_song_needs_a_full_check_before_it_counts(self):
        worker = self.make(entry={"smoke_test": True})
        quick = worker.probe(False, self.cancel)
        self.assertEqual((quick.ok, quick.needs_full, quick.state), (False, True, "warming"))
        full = worker.probe(True, self.cancel)
        self.assertEqual((full.ok, full.evidence), (True, "test_clip_made"))
        self.assertTrue(worker.warm)
        worker.invalidate()
        self.assertFalse(worker.warm)
        broken = self.make("fail", entry={"smoke_test": True})
        result = broken.probe(True, self.cancel)
        self.assertEqual((result.ok, result.state), (False, "failed"))
        self.assertNotEqual(result.evidence, "test_clip_made")

    def test_a_full_check_makes_a_real_test_song_even_when_the_maker_does_not_test_by_itself(self):
        worker = self.make()  # smoke_test is off by default
        quick = worker.probe(False, self.cancel)
        self.assertEqual((quick.ok, quick.evidence, quick.needs_full), (True, "setup_checked", False))
        self.assertEqual(self.note("args.txt"), "", "a quick check never starts the program")
        full = worker.probe(True, self.cancel)
        self.assertEqual((full.ok, full.evidence), (True, "test_clip_made"))
        self.assertNotEqual(self.note("args.txt"), "", "asked for by a person, so the program really ran")
        self.assertTrue(worker.warm)

    def test_a_failed_test_song_asks_to_be_repeated_only_by_a_maker_that_tests_by_itself(self):
        for smoke, repeat in ((True, True), (False, False)):
            with self.subTest(smoke_test=smoke):
                result = self.make("fail", entry={"smoke_test": smoke}).probe(True, self.cancel)
                self.assertEqual((result.ok, result.needs_full), (False, repeat))

    def test_describe_shows_the_program_name_only(self):
        shown = json.dumps(self.make(entry={"env": {"API_TOKEN": "topsecret"}}).describe())
        self.assertNotIn("topsecret", shown)
        self.assertNotIn(self.dir.as_posix(), shown)


class FixtureTests(WorkerCase):
    def test_with_no_cap_written_the_tone_always_fits_the_length_the_engine_accepts(self):
        with patch.dict(os.environ, FIXTURES_ON):
            worker = FixtureWorker(self.spec({"id": "tones", "kind": "fixture"}, Env(environ=FIXTURES_ON)), self.ctx)
            self.assertIsNone(worker.options["max_seconds"])
            for label, role, payload in (("a song with no length asked for", "song", {"tags": "ambient pads"}),
                                         ("the longest song", "song", {"tags": "ambient pads", "duration_s": 420}),
                                         ("a short song", "song", {"tags": "ambient pads", "duration_s": 5}),
                                         ("a long host break", "host_break", {"text": "word " * 150}),
                                         ("a station ID", "id", {"text": "Radio Four."}),
                                         ("a long sponsor message", "spot", {"text": "Maple Bakery has fresh bread daily. " * 20})):
                with self.subTest(label):
                    request = self.request(role, payload)
                    seconds = worker.generate(request).pcm.seconds
                    self.assertTrue(request.min_s <= seconds <= request.max_s, f"{seconds:.1f} s is outside {request.min_s:.1f} to {request.max_s:.1f}")

    def test_a_cap_written_in_the_settings_is_kept_even_when_it_makes_results_the_engine_refuses(self):
        with patch.dict(os.environ, FIXTURES_ON):
            worker = FixtureWorker(self.spec({"id": "tones", "kind": "fixture", "max_seconds": 3}, Env(environ=FIXTURES_ON)), self.ctx)
            request = self.request("song", {"tags": "ambient pads"})
            self.assertAlmostEqual(worker.generate(request).pcm.seconds, 3.0, places=2)
            self.assertGreater(request.min_s, 3.0)

    def test_it_refuses_to_exist_unless_switched_on(self):
        entry = {"id": "tones", "kind": "fixture"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_VAR, None)
            with self.assertRaises(WorkerError) as caught:
                FixtureWorker(dataclasses.replace(self.spec(entry, Env(environ=FIXTURES_ON)), id="tones"), self.ctx)
            self.assertEqual((caught.exception.code, caught.exception.retryable), ("worker_blocked", False))
            with self.assertRaises(ConfigError):
                FixtureWorker.parse_options(entry, "w", Env(environ={}))
        for value in ("", "0", "true", "yes", " 1", "1 "):
            with self.subTest(value), patch.dict(os.environ, {ENV_VAR: value}):
                with self.assertRaises(WorkerError):
                    FixtureWorker(self.spec(entry, Env(environ=FIXTURES_ON)), self.ctx)

    def test_it_refuses_to_generate_if_the_switch_is_turned_off_later(self):
        with patch.dict(os.environ, FIXTURES_ON):
            worker = FixtureWorker(self.spec({"id": "tones", "kind": "fixture"}, Env(environ=FIXTURES_ON)), self.ctx)
            request = self.request("host_break", {"text": "Testing one two three four."})
            self.assertTrue(worker.probe(False, self.cancel).ok)
        with patch.dict(os.environ, {ENV_VAR: "0"}):
            with self.assertRaises(WorkerError) as caught:
                worker.generate(request)
            self.assertEqual(caught.exception.code, "worker_blocked")
            self.assertFalse(worker.probe(False, self.cancel).ok)

    def test_output_is_deterministic_valid_and_labelled_a_fixture(self):
        with patch.dict(os.environ, FIXTURES_ON):
            worker = FixtureWorker(self.spec({"id": "tones", "kind": "fixture", "max_seconds": 3}, Env(environ=FIXTURES_ON)), self.ctx)
            self.assertTrue(worker.fixture)
            self.assertTrue(worker.describe()["fixture"])
            self.assertIn("never", worker.describe()["warning"])
            a = worker.generate(self.request("song", {"tags": "ambient pads", "duration_s": 20}, seed=3))
            b = worker.generate(self.request("song", {"tags": "ambient pads", "duration_s": 20}, seed=3))
            c = worker.generate(self.request("song", {"tags": "ambient pads", "duration_s": 20}, seed=4))
            speech = worker.generate(self.request("id", {"text": "Radio Four, the friendly signal."}, seed=5))
        self.assertEqual(a.pcm.frames, b.pcm.frames)
        self.assertNotEqual(a.pcm.frames, c.pcm.frames)
        self.assertEqual((a.pcm.rate, a.pcm.channels), (44100, 2))
        self.assertAlmostEqual(a.pcm.seconds, 3.0, places=2)
        self.assertTrue(a.meta["fixture"])
        self.assertGreater(speech.pcm.seconds, 1.0)
        self.assertEqual(len(a.pcm.frames) % 4, 0)


if __name__ == "__main__":
    unittest.main()
