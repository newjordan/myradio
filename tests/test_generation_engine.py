"""Generation: the engine behind the extension (scheduling, buffer, retries, recovery, honest status), against fake workers."""
import json
import os
import sqlite3
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

from myradio.extensions import ExtensionError
from myradio.generation.errors import WorkerError
from myradio.generation.store import JobStore
from myradio.generation.workers_base import ProbeResult

wait_for = fakes.wait_for


def setUpModule():
    fakes.use_audio()


def tearDownModule():
    fakes.release_audio()


class EngineCase(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        fakes.reset_controls()
        self.harnesses = []
        self.addCleanup(self.close_all)
        self.threads_before = {t.name for t in threading.enumerate()}

    def close_all(self):
        for harness in reversed(self.harnesses):
            harness.close()
        self.harnesses.clear()

    def harness(self, workers=None, *, no_config=False, config=None, **kw):
        if no_config:
            settings = None
        else:
            config = dict(config or {})
            # generous marks, so tests that queue many jobs are not held back by the default buffer
            marks = {role: {"low": 40, "high": 90} for role in ("host_break", "id", "spot", "update")}
            marks.update(config.pop("buffer", {}))
            settings = {"workers": workers if workers is not None else [fakes.worker_cfg("w1")], "buffer": marks, **config}
        harness = fakes.Harness(settings, **kw)
        self.harnesses.append(harness)
        return harness

    def control(self, wid="w1"):
        return fakes.CONTROLS.setdefault(wid, fakes.Control())

    def done(self, harness, job, timeout=20):
        got = harness.wait_state(job["id"], "succeeded", "failed", "cancelled", "expired", "interrupted", timeout=timeout)
        return got

    def imports(self, harness):
        return list(harness.station.imports)


class HonestStatusTests(EngineCase):
    def test_nothing_set_up_means_nothing_is_claimed(self):
        h = self.harness(no_config=True)
        status = h.status()
        self.assertEqual(status["capabilities"], {"music_generation": False, "speech_generation": False,
                                                  "roles": {r: False for r in ("song", "host_break", "id", "spot", "update")}})
        self.assertEqual((status["configured"], status["workers"], status["config"]["present"]), (False, [], False))
        with self.assertRaisesRegex(ExtensionError, "No voice maker is set up"):
            h.call("enqueue", {"role": "host_break", "payload": {"text": "Hello there everyone."}})
        with self.assertRaisesRegex(ExtensionError, "No music maker is set up"):
            h.call("enqueue", {"role": "song", "payload": {"tags": "ambient pads"}})
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["generation.config"]["status"], "warn")
        self.assertIn("No music or voice maker", checks["generation.config"]["detail"])
        json.dumps(h.host.status())

    def test_a_failed_check_means_the_capability_is_false(self):
        self.control().probe = ProbeResult(False, "unreachable", "The voice maker is not answering.", "Start it, then choose Check again.")
        h = self.harness()
        self.assertTrue(wait_for(lambda: h.status()["workers"] and h.status()["workers"][0]["state"] == "unreachable"))
        status = h.status()
        self.assertFalse(status["capabilities"]["speech_generation"])
        self.assertFalse(status["capabilities"]["music_generation"])
        worker = status["workers"][0]
        self.assertEqual((worker["ready"], worker["message"], worker["hint"]), (False, "The voice maker is not answering.", "Start it, then choose Check again."))
        self.control().probe = ProbeResult(True, "ready", "Ready.", evidence="setup_checked")
        reply = h.route("POST", "/probe", body={})
        self.assertEqual(reply.status, 200)
        self.assertTrue(h.status()["capabilities"]["speech_generation"])
        self.assertTrue(h.status()["capabilities"]["music_generation"])
        self.assertEqual(h.status()["workers"][0]["evidence"], "setup_checked")

    def test_capabilities_follow_which_roles_the_ready_worker_covers(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["id", "spot"])])
        self.assertTrue(wait_for(lambda: all(w["ready"] for w in h.status()["workers"]) and len(h.status()["workers"]) == 2))
        roles = h.status()["capabilities"]["roles"]
        self.assertEqual(roles, {"song": True, "host_break": False, "id": True, "spot": True, "update": False})
        self.assertTrue(h.status()["capabilities"]["music_generation"])
        self.assertTrue(h.status()["capabilities"]["speech_generation"])

    def test_test_tone_makers_never_count_as_real_generation(self):
        h = self.harness([{"id": "tones", "kind": "controlled_fixture", "resource": "gpu0"}])
        self.assertTrue(wait_for(lambda: h.status()["workers"] and h.status()["workers"][0]["state"] == "ready"))
        status = h.status()
        self.assertFalse(status["capabilities"]["music_generation"])
        self.assertFalse(status["capabilities"]["speech_generation"])
        self.assertTrue(status["fixture_active"])
        self.assertTrue(status["workers"][0]["fixture"])
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["generation.fixture"]["status"], "warn")

    def test_repeated_failures_make_a_worker_degraded_until_real_work_succeeds(self):
        control = self.control()
        control.errors = [WorkerError("worker_failed", "The maker stopped with a problem.", retryable=False)] * 3
        h = self.harness()
        for n in range(3):
            self.done(h, h.enqueue("host_break", {"text": f"Script number {n} that will fail for the test."}))
        self.assertTrue(wait_for(lambda: h.status()["workers"][0]["state"] == "degraded"))
        status = h.status()
        self.assertFalse(status["capabilities"]["speech_generation"])
        self.assertEqual(status["workers"][0]["recent_failures"], 3)
        self.assertIn("last few attempts failed", status["workers"][0]["message"])
        ok = self.done(h, h.enqueue("host_break", {"text": "A script that works after the trouble."}))
        self.assertEqual(ok["state"], "succeeded")
        self.assertTrue(wait_for(lambda: h.status()["capabilities"]["speech_generation"]))
        worker = h.status()["workers"][0]
        self.assertEqual((worker["state"], worker["evidence"], worker["recent_failures"]), ("ready", "real_job_completed", 0))

    def test_disk_trouble_does_not_blame_the_worker(self):
        control = self.control()
        h = self.harness(config={"retry": {"max_attempts": 1}})
        self.done(h, h.enqueue())
        calls = len(control.calls)
        with patch("myradio.generation.engine.free_bytes", return_value=1024):
            for n in range(4):
                failed = self.done(h, h.enqueue("host_break", {"text": f"Script {n} stopped by a full disk."}))
                self.assertEqual((failed["state"], failed["error_code"]), ("failed", "low_disk"))
        self.assertEqual(h.status()["workers"][0]["recent_failures"], 0)
        self.assertTrue(h.status()["capabilities"]["speech_generation"])
        self.assertEqual(len(control.calls), calls, "the worker was never called while the disk was full")


    def test_the_headline_and_next_step_follow_the_real_state(self):
        h = self.harness(no_config=True)
        self.assertEqual(h.status()["headline"], "No music or voice maker is set up.")
        self.assertIn("Your own audio works without them", h.status()["next_step"])
        h2 = self.harness()
        self.assertTrue(wait_for(lambda: h2.status()["headline"] == "The makers are ready."))
        h2.call("pause")
        self.assertEqual(h2.status()["headline"], "Making audio is paused.")
        self.assertIn("Resume", h2.status()["next_step"])
        h2.call("resume")
        self.control().probe = ProbeResult(False, "unreachable", "The voice maker is not answering.", "Start it, then choose Check again.")
        h2.route("POST", "/probe")
        self.assertEqual(h2.status()["next_step"], "Start it, then choose Check again.")
        self.assertIn("not answering", h2.status()["headline"])
        h3 = self.harness([{"id": "tones", "kind": "controlled_fixture", "resource": "gpu0"}])
        self.assertTrue(wait_for(lambda: h3.status()["headline"] == "Only test tones are set up."))

    def test_every_failed_job_says_what_to_do_next_in_plain_words(self):
        from myradio.generation.errors import MESSAGES
        from myradio.generation.views import job_hint
        for code in MESSAGES:
            hint = job_hint({"state": "failed", "error_code": code})
            self.assertTrue(hint, code)
            self.assertNotRegex(hint, r"Traceback|PCM|GGUF|SSRF|errno|HTTP")
        self.assertEqual(job_hint({"state": "queued", "attempts": 1}), "It will try again by itself.")
        self.assertEqual(job_hint({"state": "queued", "attempts": 0}), "")
        self.assertEqual(job_hint({"state": "succeeded"}), "")
        h = self.harness(config={"retry": {"max_attempts": 1}})
        self.control().errors = [WorkerError("worker_unreachable", "The maker did not answer. Check that it is running, then try again.")]
        failed = self.done(h, h.enqueue())
        self.assertEqual(failed["hint"], "Check that the maker is running, then choose Try again.")


class RunningJobsTests(EngineCase):
    def test_every_kind_of_job_lands_in_the_library_with_the_right_kind_and_provenance(self):
        h = self.harness([fakes.worker_cfg("w1")], config={"artist": "Test FM"})
        expected = {"song": "music", "host_break": "speech", "id": "speech", "spot": "ad", "update": "speech"}
        jobs = {role: h.enqueue(role, seed=None) if False else h.enqueue(role) for role in expected}
        for role, job in jobs.items():
            done = self.done(h, job)
            self.assertEqual(done["state"], "succeeded", (role, done.get("error")))
            self.assertIsNotNone(done["track_id"])
        by_track = {t["id"]: t for t in h.station.imports}
        for role, job in jobs.items():
            done = h.job(job["id"])
            track = by_track[done["track_id"]]
            self.assertEqual((track["kind"], track["artist"]), (expected[role], "Test FM"), role)
            self.assertEqual(track["format"], (44100, 2, 2), "converted to the station format")
            self.assertGreater(track["duration"], 2.0)
            prov = done["provenance"]
            self.assertEqual((prov["role"], prov["kind"], prov["job_id"], prov["fixture"], prov["status"]), (role, expected[role], job["id"], False, "ready"))
            self.assertEqual((prov["worker"]["id"], prov["worker"]["kind"], prov["attempts"]), ("w1", "controlled", 1))
            self.assertEqual(prov["seed"], done["seed"])
            self.assertRegex(prov["audio_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("worker_s", prov["timings"])
            self.assertIn("validate", prov["timings"]["finish_steps"])
            self.assertIsInstance(prov["validation"], list)
            self.assertEqual(prov["params"]["tags" if role == "song" else "text"], done["payload"]["tags" if role == "song" else "text"])
            self.assertAlmostEqual(prov["duration_s"], track["duration"], places=2)

    def test_the_library_title_comes_from_the_job_and_the_track_is_findable_from_its_job(self):
        h = self.harness()
        job = h.enqueue("song", {"title": "Rain on the Roof", "prompt_id": "rainy-roof-loop"})
        done = self.done(h, job)
        self.assertEqual(h.station.imports[0]["title"], "Rain on the Roof")
        self.assertEqual(done["provenance"]["prompt_id"], "rainy-roof-loop")
        found = h.route("GET", "/jobs", {"track_id": [done["track_id"]]})
        self.assertEqual(found.body["job"]["id"], job["id"])

    def test_one_resource_group_with_one_slot_runs_one_job_at_a_time(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["host_break", "id"])])
        for wid in ("music", "voice"):
            self.control(wid).delay = 0.05
        jobs = []
        for n in range(4):
            jobs.append(h.enqueue("song", {"title": f"Song {n}"}))
            jobs.append(h.enqueue("host_break", {"text": f"Script number {n} for the serial test."}))
        for job in jobs:
            self.assertEqual(self.done(h, job)["state"], "succeeded")
        self.assertEqual(fakes.GLOBAL.max_active, 1)
        self.assertEqual(len(fakes.GLOBAL.order), 8)
        threads = {c["thread"] for wid in ("music", "voice") for c in self.control(wid).calls}
        self.assertEqual(len(threads), 1, "a single slot thread serves the whole group")

    def test_separate_resource_groups_work_in_parallel(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"], resource="gpu0"), fakes.worker_cfg("voice", roles=["host_break"], resource="gpu1")],
                         config={"resources": {"gpu0": {"max_concurrent": 1}, "gpu1": {"max_concurrent": 1}}})
        for wid in ("music", "voice"):
            self.control(wid).gate.clear()
        a, b = h.enqueue("song"), h.enqueue("host_break")
        self.assertTrue(wait_for(lambda: self.control("music").entered.is_set() and self.control("voice").entered.is_set()), "both ran at the same time")
        self.assertEqual(fakes.GLOBAL.active, 2)
        self.assertEqual(h.status()["jobs"]["running"], 2)
        for wid in ("music", "voice"):
            self.control(wid).gate.set()
        self.assertEqual((self.done(h, a)["state"], self.done(h, b)["state"]), ("succeeded", "succeeded"))

    def test_a_group_can_allow_more_than_one_job_at_once(self):
        h = self.harness(config={"resources": {"gpu0": {"max_concurrent": 2}}})
        control = self.control()
        control.gate.clear()
        jobs = [h.enqueue("host_break", {"text": f"Script number {n} for the two slot test."}) for n in range(3)]
        self.assertTrue(wait_for(lambda: control.active == 2))
        time.sleep(0.2)
        self.assertEqual(control.active, 2, "a third job waits for a free slot")
        control.gate.set()
        for job in jobs:
            self.assertEqual(self.done(h, job)["state"], "succeeded")
        self.assertEqual(control.max_active, 2)

    def test_higher_priority_goes_first_and_equal_priority_is_first_come_first_served(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        blocker = h.enqueue("host_break", {"text": "This one occupies the slot while the others queue up."})
        self.assertTrue(wait_for(control.entered.is_set))
        low = h.enqueue("host_break", {"text": "Low priority script waiting its turn."}, priority=10)
        high = h.enqueue("host_break", {"text": "High priority script waiting its turn."}, priority=70)
        mid1 = h.enqueue("host_break", {"text": "Middle script number one waiting."}, priority=40)
        mid2 = h.enqueue("host_break", {"text": "Middle script number two waiting."}, priority=40)
        control.gate.set()
        for job in (blocker, low, high, mid1, mid2):
            self.done(h, job)
        order = [job_id for _, job_id in fakes.GLOBAL.order]
        self.assertEqual(order, [blocker["id"], high["id"], mid1["id"], mid2["id"], low["id"]])

    def test_the_loaded_worker_keeps_the_machine_until_another_has_waited_too_long(self):
        for overdue in (False, True):
            with self.subTest(overdue=overdue):
                fakes.reset_controls()
                clock = fakes.FakeClock()
                h = self.harness([fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["host_break"])], clock=clock,
                                 config={"resources": {"gpu0": {"max_concurrent": 1, "switch_after_s": 120}}})
                for wid in ("music", "voice"):
                    self.control(wid).gate.clear()
                a1 = h.enqueue("song", {"title": "A1"})
                self.assertTrue(wait_for(self.control("music").entered.is_set))
                b1 = h.enqueue("host_break", {"text": "The voice job that arrives while music is loaded."})
                a2 = h.enqueue("song", {"title": "A2"})
                a3 = h.enqueue("song", {"title": "A3"})
                if overdue:
                    clock.advance(200)
                for wid in ("music", "voice"):
                    self.control(wid).gate.set()
                h.engine.poke()
                for job in (a1, b1, a2, a3):
                    self.assertEqual(self.done(h, job)["state"], "succeeded")
                order = [job_id for _, job_id in fakes.GLOBAL.order]
                expected = [a1["id"], b1["id"], a2["id"], a3["id"]] if overdue else [a1["id"], a2["id"], a3["id"], b1["id"]]
                self.assertEqual(order, expected)
                h.close()

    def test_jobs_follow_the_current_worker_for_their_kind(self):
        h = self.harness()
        job = h.enqueue("host_break")
        self.assertEqual(self.done(h, job)["worker"], "w1")
        self.assertEqual(h.job(job["id"])["resource"], "gpu0")

    def test_a_failed_or_invalid_take_never_reaches_the_library(self):
        clock = fakes.FakeClock()
        control = self.control()
        control.silent = True
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 2, "base_s": 1, "cap_s": 2}})
        job = h.enqueue("host_break")
        self.assertTrue(wait_for(lambda: h.job(job["id"])["attempts"] == 1 and h.job(job["id"])["state"] == "queued"))
        clock.advance(5)
        h.engine.poke()
        done = self.done(h, job)
        self.assertEqual((done["state"], done["error_code"], done["attempts"]), ("failed", "silent_output", 2))
        self.assertIn("thrown away", done["error"])
        self.assertNotIn("silent:", done["error"])
        self.assertEqual(self.imports(h), [])
        self.assertEqual(len(control.calls), 2, "retried within limits, then reported")
        self.assertTrue(wait_for(lambda: h.status()["recent"] and h.status()["recent"][0]["error"] == done["error"]))
        self.assertEqual(h.status()["buffer"]["host_break"]["ready"], 0)

    def test_too_short_results_are_refused_and_nothing_is_imported(self):
        control = self.control()
        control.seconds = 0.3
        h = self.harness(config={"retry": {"max_attempts": 1}})
        done = self.done(h, h.enqueue("song", {"duration_s": 60}))
        self.assertEqual((done["state"], done["error_code"]), ("failed", "audio_too_short"))
        self.assertEqual(self.imports(h), [])

    def test_a_library_that_refuses_the_audio_is_a_retryable_plain_failure(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 2, "base_s": 1, "cap_s": 2}})
        h.station.fail_next = [ValueError("The library is full.")]
        job = h.enqueue()
        self.assertTrue(wait_for(lambda: h.job(job["id"])["attempts"] >= 1 and h.job(job["id"])["error_code"] == "import_failed"))
        first = h.job(job["id"])
        self.assertIn("could not add it to the library", first["error"])
        self.assertIn("The library is full", first["detail"])
        clock.advance(5)
        h.engine.poke()
        done = self.done(h, job)
        self.assertEqual((done["state"], done["attempts"]), ("succeeded", 2))
        self.assertEqual(len(self.imports(h)), 1)

    def test_a_worker_that_raises_something_unexpected_never_breaks_the_engine(self):
        control = self.control()
        control.errors = [RuntimeError("secret internal /home/me/key")]
        h = self.harness(config={"retry": {"max_attempts": 1}})
        with self.assertLogs("myradio.generation", level="ERROR"):
            done = self.done(h, h.enqueue())
        self.assertEqual((done["state"], done["error_code"]), ("failed", "internal"))
        self.assertNotIn("secret", done["error"])
        self.assertEqual(self.done(h, h.enqueue("host_break", {"text": "The next script still works fine."}))["state"], "succeeded")

    def test_the_finished_audio_is_converted_checked_and_leveled_before_import(self):
        h = self.harness()
        job = h.enqueue("host_break")
        self.done(h, job)
        track = h.station.imports[0]
        self.assertEqual(track["format"], (44100, 2, 2))
        # every imported file is a real, complete 44.1 kHz 16-bit stereo WAV whose length matches its duration
        self.assertAlmostEqual(track["bytes"], 44 + int(round(track["duration"] * 44100)) * 4, delta=8)


class BufferTests(EngineCase):
    def test_it_tops_up_below_the_low_mark_and_stops_at_the_high_mark(self):
        h = self.harness(config={"buffer": {"host_break": {"low": 2, "high": 3}}})
        jobs = [h.enqueue("host_break", {"text": f"Script number {n} for the buffer test."}) for n in range(6)]
        self.assertTrue(wait_for(lambda: len(self.imports(h)) == 3))
        time.sleep(0.4)
        self.assertEqual(len(self.imports(h)), 3, "stops at the high mark")
        view = h.route("GET", "/buffer").body["roles"]["host_break"]
        self.assertEqual((view["ready"], view["filling"], view["queued"], view["wanted"]), (3, False, 3, 0))
        counts = h.status()["jobs"]
        self.assertEqual((counts["succeeded"], counts["queued"]), (3, 3))
        # between the marks nothing changes: one item airs, two are still ready (not below low)
        first = h.station.imports[0]["id"]
        h.emit("track_started", first)
        self.assertTrue(wait_for(lambda: h.route("GET", "/buffer").body["roles"]["host_break"]["ready"] == 2))
        time.sleep(0.3)
        self.assertEqual(len(self.imports(h)), 3, "hysteresis: still holding between the marks")
        # a second one airs: now below the low mark, so it fills up to the high mark again
        h.emit("track_started", h.station.imports[1]["id"])
        self.assertTrue(wait_for(lambda: len(self.imports(h)) == 5))
        time.sleep(0.3)
        self.assertEqual(len(self.imports(h)), 5, "filled back to the high mark (1 + 2 = 3 ready) and stopped")
        self.assertEqual(h.route("GET", "/buffer").body["roles"]["host_break"]["ready"], 3)
        self.assertEqual(h.status()["jobs"]["queued"], 1)

    def test_an_urgent_job_ignores_the_marks(self):
        h = self.harness(config={"buffer": {"host_break": {"low": 1, "high": 1}}})
        self.done(h, h.enqueue("host_break"))
        held = h.enqueue("host_break", {"text": "This script is held back by the buffer."})
        time.sleep(0.4)
        self.assertEqual(h.job(held["id"])["state"], "queued")
        urgent = h.enqueue("host_break", {"text": "This one is needed right now on air."}, priority=90)
        self.assertEqual(self.done(h, urgent)["state"], "succeeded")
        self.assertEqual(h.job(held["id"])["state"], "queued")

    def test_the_marks_are_set_by_operation_persist_and_are_bounded(self):
        h = self.harness()
        saved = h.call("set_buffer", {"role": "song", "low": 100, "high": 300})["buffer"]
        self.assertEqual((saved["low"], saved["high"], saved["unit"]), (100, 300, "seconds"))
        self.assertEqual(h.route("GET", "/buffer").body["overrides"], {"song": [100, 300]})
        for args in ({"role": "song", "low": 400, "high": 300}, {"role": "song", "low": -1, "high": 3}, {"role": "song", "low": 1, "high": 0},
                     {"role": "song", "low": 8000, "high": 9000}, {"role": "id", "low": 1, "high": 500}, {"role": "nope", "low": 1, "high": 2},
                     {"role": "id", "low": 1.5, "high": 2}, {"role": "id", "low": 1}):
            with self.subTest(args=args), self.assertRaises(ExtensionError):
                h.call("set_buffer", args)
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)
        self.harnesses.append(again)
        self.assertEqual(again.route("GET", "/buffer").body["roles"]["song"]["low"], 100)

    def test_the_view_tells_producers_how_much_more_to_send(self):
        h = self.harness(config={"buffer": {"song": {"low": 100, "high": 400}}})
        control = self.control()
        control.gate.clear()
        h.enqueue("song", {"duration_s": 60})
        h.enqueue("song", {"duration_s": 60, "title": "Two"})
        self.assertTrue(wait_for(control.entered.is_set))
        view = h.route("GET", "/buffer").body["roles"]["song"]
        self.assertEqual((view["unit"], view["ready"], view["low"], view["high"], view["running"], view["queued"], view["pending"]), ("seconds", 0, 100, 400, 1, 1, 120))
        self.assertEqual((view["filling"], view["wanted"]), (True, 280))
        self.assertEqual(h.status()["buffer"]["song"]["wanted"], 280)
        control.gate.set()

    def test_deleted_audio_stops_counting_as_ready(self):
        h = self.harness(config={"buffer": {"host_break": {"low": 1, "high": 5}}})
        self.done(h, h.enqueue("host_break"))
        self.assertEqual(h.route("GET", "/buffer").body["roles"]["host_break"]["ready"], 1)
        h.station.delete_track(h.station.imports[0]["id"])
        h.engine._last_reconcile = 0
        h.engine._reconcile()
        self.assertEqual(h.route("GET", "/buffer").body["roles"]["host_break"]["ready"], 0)
        self.assertGreaterEqual(h.station.status_calls, 1)

    def test_airing_is_noticed_from_station_events_only_once(self):
        h = self.harness(config={"buffer": {"host_break": {"low": 1, "high": 5}}})
        self.done(h, h.enqueue("host_break"))
        track = h.station.imports[0]["id"]
        h.emit("track_started", track)
        h.emit("track_finished", track)
        h.host.emit({"type": "transport", "state": "started"})
        h.host.emit({"type": "track_started", "track": {"id": "not-ours"}})
        h.host.emit({"type": "track_started", "track": None})
        h.host.emit({"type": "track_started"})
        self.assertTrue(wait_for(lambda: h.route("GET", "/buffer").body["roles"]["host_break"]["ready"] == 0))
        self.assertEqual(h.engine.store.provenance(track)["status"], "aired")
        self.assertTrue(wait_for(lambda: h.status()["station_running"] is True))


class QueueRulesTests(EngineCase):
    def test_enqueue_is_safe_to_repeat_with_the_same_key(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        first = h.call("enqueue", {"role": "host_break", "payload": {"text": "Slot seven needs a station ID."}, "idempotency_key": "slot:7"})
        again = h.call("enqueue", {"role": "host_break", "payload": {"text": "Slot seven needs a station ID."}, "idempotency_key": "slot:7"})
        self.assertEqual((first["created"], again["created"], again["deduplicated"]), (True, False, True))
        self.assertEqual(first["job"]["id"], again["job"]["id"])
        with self.assertRaisesRegex(ExtensionError, "different request"):
            h.call("enqueue", {"role": "host_break", "payload": {"text": "A different script with the same key."}, "idempotency_key": "slot:7"})
        control.gate.set()
        done = self.done(h, first["job"])
        self.assertEqual(done["state"], "succeeded")
        after = h.call("enqueue", {"role": "host_break", "payload": {"text": "Slot seven needs a station ID."}, "idempotency_key": "slot:7"})
        self.assertFalse(after["created"], "the same key never makes the same audio twice")
        self.assertEqual(len(self.imports(h)), 1)
        for bad in ("has space", "x" * 121, "semi;colon"):
            with self.subTest(key=bad), self.assertRaises(ExtensionError):
                h.call("enqueue", {"role": "host_break", "payload": {"text": "Hello there everyone."}, "idempotency_key": bad})

    def test_a_waiting_job_expires_at_its_deadline_and_never_runs(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock)
        control = self.control()
        control.gate.clear()
        blocker = h.enqueue("host_break", {"text": "Occupies the slot so the next one has to wait."})
        self.assertTrue(wait_for(control.entered.is_set))
        short = h.enqueue("host_break", {"text": "News that is only useful for ten seconds."}, ttl_s=10)
        keeps = h.enqueue("host_break", {"text": "A script with no deadline at all."})
        clock.advance(11)
        h.engine.poke()
        self.assertTrue(wait_for(lambda: h.job(short["id"])["state"] == "expired"))
        expired = h.job(short["id"])
        self.assertEqual(expired["error_code"], "expired")
        self.assertIn("not made in time", expired["error"])
        control.gate.set()
        self.done(h, blocker)
        self.done(h, keeps)
        self.assertNotIn(short["id"], [c["job"] for c in control.calls])
        retried = h.call("retry", {"job_id": short["id"]})["job"]
        self.assertEqual(self.done(h, retried)["state"], "succeeded")

    def test_a_result_that_arrives_after_its_deadline_is_thrown_away(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock)
        control = self.control()
        control.gate.clear()
        job = h.enqueue("update", ttl_s=60)
        self.assertTrue(wait_for(control.entered.is_set))
        clock.advance(120)
        control.gate.set()
        done = self.done(h, job)
        self.assertEqual((done["state"], done["error_code"]), ("expired", "expired"))
        self.assertIn("too late", done["error"])
        self.assertEqual(self.imports(h), [], "a stale bulletin is never added to the library")

    def test_cancel_a_waiting_job(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        blocker = h.enqueue("host_break", {"text": "Occupies the slot while the other one waits."})
        self.assertTrue(wait_for(control.entered.is_set))
        waiting = h.enqueue("host_break", {"text": "This one is cancelled before it starts."})
        reply = h.call("cancel", {"job_id": waiting["id"]})
        self.assertEqual((reply["job"]["state"], reply["settled"]), ("cancelled", True))
        control.gate.set()
        self.done(h, blocker)
        self.assertEqual([c["job"] for c in control.calls], [blocker["id"]])
        with self.assertRaisesRegex(ExtensionError, "already finished"):
            h.call("cancel", {"job_id": waiting["id"]})
        with self.assertRaisesRegex(ExtensionError, "not found"):
            h.call("cancel", {"job_id": "0" * 32})

    def test_cancel_a_running_job_stops_the_worker_and_imports_nothing(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        running = h.enqueue("host_break")
        self.assertTrue(wait_for(control.entered.is_set))
        began = time.monotonic()
        reply = h.call("cancel", {"job_id": running["id"]})
        self.assertLess(time.monotonic() - began, 8.0)
        self.assertEqual((reply["job"]["state"], reply["settled"]), ("cancelled", True))
        self.assertEqual(reply["job"]["error_code"], "cancelled")
        self.assertEqual(control.active, 0)
        self.assertEqual(self.imports(h), [])
        control.gate.set()
        self.assertEqual(self.done(h, h.enqueue("host_break", {"text": "The next job after a cancel works fine."}))["state"], "succeeded")

    def test_pause_stops_new_work_and_resume_continues(self):
        h = self.harness()
        control = self.control()
        result = h.call("pause")
        self.assertEqual((result["paused"], result["running"]), (True, 0))
        self.assertIn("free", result["message"])
        job = h.enqueue()
        time.sleep(0.4)
        self.assertEqual(h.job(job["id"])["state"], "queued")
        self.assertEqual(control.calls, [])
        self.assertTrue(h.status()["paused"])
        self.assertTrue(h.route("GET", "/buffer").body["paused"])
        h.call("resume")
        self.assertEqual(self.done(h, job)["state"], "succeeded")
        self.assertFalse(h.status()["paused"])

    def test_pause_can_release_the_machine_at_once_without_losing_the_job(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        job = h.enqueue()
        self.assertTrue(wait_for(control.entered.is_set))
        result = h.call("pause", {"cancel_running": True})
        self.assertEqual(result["stopping_now"], 1)
        self.assertTrue(wait_for(lambda: control.active == 0))
        self.assertTrue(wait_for(lambda: h.job(job["id"])["state"] == "queued"))
        back = h.job(job["id"])
        self.assertEqual((back["attempts"], back["state"]), (0, "queued"))
        self.assertEqual(self.imports(h), [])
        time.sleep(0.3)
        self.assertEqual(h.job(job["id"])["state"], "queued", "still paused")
        control.gate.set()
        h.call("resume")
        self.assertEqual(self.done(h, job)["state"], "succeeded")

    def test_pause_survives_a_restart(self):
        h = self.harness()
        h.call("pause")
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)
        self.harnesses.append(again)
        self.assertTrue(again.status()["paused"])
        job = again.enqueue()
        time.sleep(0.3)
        self.assertEqual(again.job(job["id"])["state"], "queued")
        again.call("resume")
        self.assertEqual(self.done(again, job)["state"], "succeeded")

    def test_failed_attempts_are_retried_with_growing_delays_then_reported(self):
        clock = fakes.FakeClock()
        control = self.control()
        control.errors = [WorkerError("worker_timeout", "The maker took too long and was stopped.")] * 3
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 3, "base_s": 10, "cap_s": 25}})
        job = h.enqueue()
        self.assertTrue(wait_for(lambda: h.job(job["id"])["attempts"] == 1 and h.job(job["id"])["state"] == "queued"))
        first = h.job(job["id"])
        self.assertEqual((first["error_code"], first["state_label"]), ("worker_timeout", "Waiting to try again"))
        self.assertIsNotNone(first["next_try"])
        time.sleep(0.2)
        self.assertEqual(len(control.calls), 1, "waits out the delay")
        clock.advance(9)
        h.engine.poke()
        time.sleep(0.2)
        self.assertEqual(len(control.calls), 1)
        clock.advance(2)
        h.engine.poke()
        self.assertTrue(wait_for(lambda: len(control.calls) == 2))
        self.assertTrue(wait_for(lambda: h.job(job["id"])["attempts"] == 2 and h.job(job["id"])["state"] == "queued"))
        clock.advance(19)
        h.engine.poke()
        time.sleep(0.2)
        self.assertEqual(len(control.calls), 2, "the second delay is twice as long (20 s)")
        clock.advance(2)
        h.engine.poke()
        final = self.done(h, job)
        self.assertEqual((final["state"], final["attempts"], final["error_code"]), ("failed", 3, "worker_timeout"))
        self.assertEqual([a["outcome"] for a in h.engine.store.get(job["id"])["attempt_log"]], ["failed"] * 3)
        again = h.call("retry", {"job_id": job["id"]})["job"]
        self.assertEqual((again["state"], again["attempts"]), ("queued", 0))
        self.assertEqual(self.done(h, again)["state"], "succeeded")
        with self.assertRaisesRegex(ExtensionError, "already succeeded"):
            h.call("retry", {"job_id": job["id"]})

    def test_the_delay_is_capped_and_a_final_error_is_not_retried(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 6, "base_s": 10, "cap_s": 25}})
        self.assertEqual([h.engine._backoff(n) for n in range(1, 6)], [10, 20, 25, 25, 25])
        control = self.control()
        control.errors = [WorkerError("worker_failed", "Refused.", retryable=False)]
        done = self.done(h, h.enqueue())
        self.assertEqual((done["state"], done["attempts"]), ("failed", 1))

    def test_unpinned_seeds_change_between_attempts_and_pinned_seeds_do_not(self):
        clock = fakes.FakeClock()
        control = self.control()
        control.errors = [WorkerError("bad_output", "Not usable.")] * 2
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 3, "base_s": 10, "cap_s": 10}})
        free = h.enqueue("host_break", {"text": "A script whose seed may vary between tries."})
        for _ in range(2):
            self.assertTrue(wait_for(lambda: h.job(free["id"])["state"] == "queued" and h.job(free["id"])["attempts"] >= 1))
            clock.advance(11)
            h.engine.poke()
            time.sleep(0.15)
        self.assertEqual(self.done(h, free)["state"], "succeeded")
        seeds = [c["seed"] for c in control.calls]
        self.assertEqual(len(seeds), 3)
        self.assertEqual(len(set(seeds)), 3)
        self.assertEqual(seeds[0], free["seed"])
        control.calls.clear()
        control.errors = [WorkerError("bad_output", "Not usable.")]
        pinned = h.enqueue("host_break", {"text": "A script whose seed is pinned by the producer.", "seed": 99})
        self.assertTrue(wait_for(lambda: h.job(pinned["id"])["state"] == "queued" and h.job(pinned["id"])["attempts"] == 1))
        clock.advance(11)
        h.engine.poke()
        self.assertEqual(self.done(h, pinned)["state"], "succeeded")
        self.assertEqual([c["seed"] for c in control.calls], [99, 99])

    def test_many_callers_sending_the_same_request_at_once_make_one_job(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        results, barrier = [], threading.Barrier(8)

        def send():
            barrier.wait(10)
            results.append(h.call("enqueue", {"role": "host_break", "payload": {"text": "Everyone asks for the same station ID."},
                                              "idempotency_key": "same"}))

        threads = [threading.Thread(target=send) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for r in results if r["created"]), 1)
        self.assertEqual(len({r["job"]["id"] for r in results}), 1)
        control.gate.set()
        self.assertEqual(self.done(h, results[0]["job"])["state"], "succeeded")
        self.assertEqual(len(self.imports(h)), 1)

    def test_a_typo_in_a_makers_settings_does_not_throw_away_its_waiting_jobs(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["host_break"])])
        h.call("pause")
        song = h.enqueue("song", {"title": "Waiting for a fix"})
        root = h.root
        h.stop()
        broken = {"workers": [dict(fakes.worker_cfg("music", roles=["song"]), resource="nowhere"), fakes.worker_cfg("voice", roles=["host_break"])]}
        again = fakes.Harness(broken, root=root)
        self.harnesses.append(again)
        self.assertEqual(again.job(song["id"])["state"], "queued", "a settings problem is not a reason to give up on waiting work")
        self.assertEqual({w["id"]: w["state"] for w in again.status()["workers"]}["music"], "misconfigured")
        with self.assertRaisesRegex(ExtensionError, "problem in its settings"):
            again.enqueue("song", {"title": "Refused for now"})
        (again.state_dir / "config.json").write_text(json.dumps({"workers": [fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["host_break"])]}))
        reply = again.route("POST", "/probe", body={})
        self.assertEqual((reply.body["reloaded"], reply.body["settings_changed"]), (True, True))
        again.call("resume")
        self.assertEqual(self.done(again, song)["state"], "succeeded")

    def test_an_unreadable_settings_file_never_replaces_the_working_settings_or_wipes_the_queue(self):
        h = self.harness()
        self.assertTrue(wait_for(lambda: h.status()["capabilities"]["speech_generation"]))
        h.call("pause")
        job = h.enqueue()
        (h.state_dir / "config.json").write_text("{this is not valid")
        reply = h.route("POST", "/probe", body={})
        self.assertEqual((reply.body["reloaded"], reply.body["settings_changed"]), (False, True))
        self.assertIn("cannot be read", reply.body["note"])
        self.assertEqual(h.job(job["id"])["state"], "queued")
        self.assertTrue(h.status()["capabilities"]["speech_generation"])
        self.assertTrue(wait_for(lambda: h.status()["config"]["changed_since_start"]), "the status notices that the file no longer matches")
        root = h.root
        h.stop()
        again = fakes.Harness(None, root=root)  # a restart with the file still broken
        self.harnesses.append(again)
        self.assertEqual(again.job(job["id"])["state"], "queued", "still waiting, not failed")
        self.assertFalse(again.status()["capabilities"]["speech_generation"])

    def test_a_job_whose_maker_was_removed_cannot_be_retried_into_a_dead_end(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["host_break"])])
        self.control("music").errors = [WorkerError("worker_failed", "Refused.", retryable=False)]
        failed = self.done(h, h.enqueue("song", {"title": "Doomed"}))
        self.assertEqual(failed["state"], "failed")
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("voice", roles=["host_break"])]}, root=root)
        self.harnesses.append(again)
        with self.assertRaisesRegex(ExtensionError, "No music maker is set up"):
            again.call("retry", {"job_id": failed["id"]})

    def test_a_worker_that_is_busy_is_not_interrupted_by_checks(self):
        h = self.harness()
        control = self.control()
        self.assertTrue(wait_for(lambda: control.probes >= 1))
        control.gate.clear()
        job = h.enqueue()
        self.assertTrue(wait_for(control.entered.is_set))
        before = control.probes
        h.route("POST", "/probe", body={})
        h.engine._cheap_probes()
        self.assertEqual(control.probes, before, "a maker that is busy making something is not asked to answer a check")
        self.assertEqual(h.status()["workers"][0]["state"], "ready")
        control.gate.set()
        self.done(h, job)
        h.route("POST", "/probe", body={})
        self.assertGreater(control.probes, before)

    def test_a_small_queue_limit_is_enforced(self):
        h = self.harness(config={"limits": {"max_queued": 10}})
        control = self.control()
        control.gate.clear()
        for n in range(10):
            h.enqueue("host_break", {"text": f"Script number {n} for the queue limit test."})
        with self.assertRaisesRegex(ExtensionError, "queue is full"):
            h.enqueue("host_break", {"text": "One more than the queue can hold."})
        control.gate.set()


class RecoveryTests(EngineCase):
    def test_a_job_running_when_the_process_died_is_put_back_in_line_and_made(self):
        h = self.harness()
        root = h.root
        h.stop()
        # Simulate a crash: a job was left "running" in the database file.
        path = root / "ext" / "generation" / "generation.sqlite3"
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        store = JobStore(conn, time.time)
        from myradio.generation.payloads import normalize_payload
        norm = normalize_payload("host_break", {"text": "This was being made when the power went out."})
        job, _ = store.enqueue(norm, priority=50, ttl_s=None, key="crash", resource="gpu0", worker_id="w1", max_attempts=3, seed=5, max_live=500)
        store.start(job["id"], time.time(), worker_id="w1", resource="gpu0", seed_used=5)
        conn.close()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)
        self.harnesses.append(again)
        self.assertEqual(again.status()["recovered"], {"requeued": 1, "interrupted": 0})
        done = self.done(again, {"id": job["id"]})
        self.assertEqual(done["state"], "succeeded")
        got = again.engine.store.get(job["id"])
        self.assertEqual((got["interruptions"], got["attempts"]), (1, 1))
        self.assertEqual(got["attempt_log"][0]["outcome"], "interrupted")

    def test_a_job_that_keeps_getting_interrupted_waits_for_a_person(self):
        h = self.harness()
        root = h.root
        h.stop()
        path = root / "ext" / "generation" / "generation.sqlite3"
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        store = JobStore(conn, time.time)
        from myradio.generation.payloads import normalize_payload
        job, _ = store.enqueue(normalize_payload("host_break", {"text": "This one is interrupted again and again."}), priority=50, ttl_s=None,
                               key=None, resource="gpu0", worker_id="w1", max_attempts=3, seed=5, max_live=500)
        conn.execute("UPDATE jobs SET state='running', interruptions=2 WHERE id=?", (job["id"],))
        conn.commit()
        conn.close()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)
        self.harnesses.append(again)
        self.assertEqual(again.status()["recovered"], {"requeued": 0, "interrupted": 1})
        parked = again.job(job["id"])
        self.assertEqual((parked["state"], parked["state_label"]), ("interrupted", "Interrupted, needs a retry"))
        time.sleep(0.3)
        self.assertEqual(again.job(job["id"])["state"], "interrupted")
        self.assertEqual(self.done(again, again.call("retry", {"job_id": job["id"]})["job"])["state"], "succeeded")

    def test_a_clean_stop_puts_running_work_back_without_calling_it_an_interruption(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        job = h.enqueue()
        self.assertTrue(wait_for(control.entered.is_set))
        root = h.root
        h.stop()
        self.assertEqual(control.active, 0, "the worker was told to stop")
        conn = sqlite3.connect(root / "ext" / "generation" / "generation.sqlite3", isolation_level=None)
        row = conn.execute("SELECT state, attempts, interruptions FROM jobs WHERE id=?", (job["id"],)).fetchone()
        conn.close()
        self.assertEqual(row, ("queued", 0, 0))
        control.gate.set()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)
        self.harnesses.append(again)
        self.assertEqual(again.status()["recovered"], {"requeued": 0, "interrupted": 0})
        self.assertEqual(self.done(again, job)["state"], "succeeded")

    def test_waiting_jobs_for_a_kind_that_lost_its_maker_are_failed_not_stranded(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"]), fakes.worker_cfg("voice", roles=["host_break"])])
        for wid in ("music", "voice"):
            self.control(wid).gate.clear()
        h.enqueue("host_break")
        self.assertTrue(wait_for(self.control("voice").entered.is_set))
        song = h.enqueue("song")
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("voice", roles=["host_break"])]}, root=root)
        self.harnesses.append(again)
        stranded = again.job(song["id"])
        self.assertEqual((stranded["state"], stranded["error_code"]), ("failed", "worker_missing"))
        for wid in ("music", "voice"):
            self.control(wid).gate.set()


class SafetyAndBoundsTests(EngineCase):
    def test_fixture_output_is_labelled_everywhere_and_the_flag_cannot_be_lost(self):
        h = self.harness([{"id": "tones", "kind": "controlled_fixture", "resource": "gpu0"}])
        job = h.enqueue("song", {"title": "Sunny Morning"})
        done = self.done(h, job)
        track = h.station.imports[0]
        self.assertTrue(track["title"].startswith("[FIXTURE] "))
        self.assertIn("FIXTURE", track["artist"])
        self.assertIn("not real music", track["artist"])
        self.assertTrue(done["fixture"])
        self.assertTrue(done["provenance"]["fixture"])
        raw = sqlite3.connect(h.state_dir / "generation.sqlite3", isolation_level=None)  # autocommit: no lock is left behind
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE provenance SET fixture=0")
        self.assertTrue(h.engine.store.provenance(done["track_id"])["fixture"])
        self.assertTrue(wait_for(lambda: h.status()["recent"] and h.status()["recent"][0]["fixture"]),
                        "the status is rebuilt by the engine's own thread a moment after the job row changes")

    def test_status_is_prebuilt_lock_free_and_never_calls_the_station(self):
        h = self.harness()
        self.done(h, h.enqueue())
        calls_before = h.station.status_calls
        # Hold each of the engine's locks in turn (one at a time, so this test cannot deadlock with the engine) and
        # ask for the status: it must come back at once because it takes none of them.
        for name, lock in (("store", h.engine.store._lock), ("engine", h.engine._cond), ("snapshot", h.engine._snap_lock)):
            release, held = threading.Event(), threading.Event()

            def hold(lock=lock, release=release, held=held):
                with lock:
                    held.set()
                    release.wait(10)

            thread = threading.Thread(target=hold, daemon=True)
            thread.start()
            self.assertTrue(held.wait(10), name)
            began = time.monotonic()
            for _ in range(200):
                h.ext.status()
            elapsed = time.monotonic() - began
            release.set()
            thread.join(10)
            self.assertLess(elapsed, 2.0, f"status() must not wait for the {name} lock")
        self.assertEqual(h.station.status_calls, calls_before)
        json.dumps(h.ext.status())

    def test_a_snapshot_that_stopped_being_refreshed_says_so(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock)
        h.engine._publish()
        self.assertNotIn("stale", h.ext.status())
        self.assertNotIn("generated_at", h.ext.status(), "the public shape carries ISO times only")
        h.engine._snapshot_at = clock() - 120
        self.assertTrue(h.ext.status()["stale"])
        h.engine._publish()
        self.assertNotIn("stale", h.ext.status())

    def test_status_stays_small_however_many_jobs_there_are(self):
        h = self.harness(config={"limits": {"max_queued": 2000}})
        control = self.control()
        control.gate.clear()
        for n in range(300):
            h.enqueue("host_break", {"text": f"Script number {n} that waits in a long queue."})
        h.engine._publish()
        size = len(json.dumps(h.ext.status()))
        self.assertLess(size, 12000)
        self.assertEqual(h.status()["jobs"]["queued"] + h.status()["jobs"]["running"], 300)
        control.gate.set()

    def test_a_stalled_worker_never_slows_the_station_clock(self):
        h = self.harness()
        control = self.control()
        control.gate.clear()
        h.enqueue()
        self.assertTrue(wait_for(control.entered.is_set))
        began = time.monotonic()
        for _ in range(50):
            h.host.status()
            h.host.emit({"type": "track_started", "track": {"id": "x"}})
        self.assertLess(time.monotonic() - began, 3.0)
        control.gate.set()

    def test_working_folders_are_private_and_removed(self):
        h = self.harness()
        self.done(h, h.enqueue())
        work = h.state_dir / "work"
        self.assertEqual(oct(work.stat().st_mode & 0o777), "0o700")
        self.assertTrue(wait_for(lambda: [p for p in work.iterdir()] == []))
        (work / "gen-leftover").mkdir()
        (work / "gen-leftover" / "f").write_text("x")
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)
        self.harnesses.append(again)
        self.assertEqual(list((root / "ext" / "generation" / "work").iterdir()), [], "leftovers from a crash are cleaned at start")

    def test_the_audio_tools_missing_is_a_plain_final_failure_and_a_failed_check(self):
        h = self.harness(config={"retry": {"max_attempts": 3}})
        with patch("myradio.generation.audio_api.importlib.import_module", side_effect=ImportError("gone")):
            done = self.done(h, h.enqueue())
            h.engine._audio_checked = 0
            h.engine._check_audio()
        self.assertEqual((done["state"], done["error_code"], done["attempts"]), ("failed", "audio_tools_missing", 1))
        self.assertIn("audio tools are missing", done["error"])
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["generation.audio"]["status"], "fail")
        h.engine._publish()
        self.assertFalse(h.status()["capabilities"]["speech_generation"], "nothing can be finished without the audio tools, so nothing is claimed")
        self.assertFalse(h.status()["audio_tools"]["available"])
        h.engine._audio_checked = 0
        h.engine._check_audio()
        h.engine._publish()
        self.assertEqual({c["id"]: c for c in h.host.checks()}["generation.audio"]["status"], "ok")
        self.assertTrue(h.status()["capabilities"]["speech_generation"])

    def test_low_disk_space_stops_a_job_before_the_worker_runs(self):
        h = self.harness(config={"limits": {"min_free_disk_mb": 500}, "retry": {"max_attempts": 1}})
        control = self.control()
        with patch("myradio.generation.engine.free_bytes", return_value=10 * 1024 * 1024):
            done = self.done(h, h.enqueue())
            checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual((done["state"], done["error_code"]), ("failed", "low_disk"))
        self.assertIn("Free up some space", done["error"])
        self.assertEqual(control.calls, [])
        self.assertEqual(checks["generation.disk"]["status"], "fail")

    def test_settings_changes_are_noticed_and_picked_up_by_check_again(self):
        h = self.harness([fakes.worker_cfg("w1", roles=["host_break"])])
        self.assertFalse(h.status()["config"]["changed_since_start"])
        (h.state_dir / "config.json").write_text(json.dumps({"workers": [fakes.worker_cfg("w1", roles=["host_break"]), fakes.worker_cfg("w2", roles=["song"])]}))
        h.engine._check_config_file()
        h.engine._publish()
        self.assertTrue(h.status()["config"]["changed_since_start"])
        self.assertEqual(len(h.status()["workers"]), 1)
        reply = h.route("POST", "/probe", body={})
        self.assertEqual(reply.status, 200)
        self.assertEqual((reply.body["reloaded"], reply.body["settings_changed"]), (True, True))
        self.assertEqual(sorted(w["id"] for w in reply.body["workers"]), ["w1", "w2"])
        self.assertFalse(h.status()["config"]["changed_since_start"])
        again = h.route("POST", "/probe", body={})
        self.assertEqual((again.body["reloaded"], again.body["settings_changed"]), (False, False))
        self.assertEqual(self.done(h, h.enqueue("song"))["worker"], "w2")

    def test_a_settings_change_waits_while_something_is_being_made(self):
        h = self.harness([fakes.worker_cfg("w1", roles=["host_break"])])
        control = self.control()
        control.gate.clear()
        job = h.enqueue()
        self.assertTrue(wait_for(control.entered.is_set))
        (h.state_dir / "config.json").write_text(json.dumps({"workers": [fakes.worker_cfg("w1", roles=["host_break"]), fakes.worker_cfg("w2", roles=["song"])]}))
        reply = h.route("POST", "/probe", body={})
        self.assertEqual((reply.body["reloaded"], reply.body["settings_changed"]), (False, True))
        self.assertIn("something is being made", reply.body["note"])
        self.assertEqual(len(reply.body["workers"]), 1)
        control.gate.set()
        self.assertEqual(self.done(h, job)["state"], "succeeded")

    def test_an_unreadable_settings_file_is_reported_and_nothing_is_claimed(self):
        h = self.harness(no_config=True)
        (h.state_dir / "config.json").write_text("{not json")
        reply = h.route("POST", "/probe", body={})
        self.assertEqual(reply.status, 200)
        h2 = fakes.Harness(None)
        self.harnesses.append(h2)
        (h2.state_dir / "config.json").write_text("{not json")
        h2.stop()
        h3 = fakes.Harness(None, root=h2.root)
        self.harnesses.append(h3)
        status = h3.status()
        self.assertFalse(status["capabilities"]["speech_generation"])
        self.assertIn("could not be used", " ".join(status["config"]["problems"]))

    def test_a_damaged_history_file_is_reported_plainly_and_the_station_keeps_running(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / "ext" / "generation"
        state.mkdir(parents=True, mode=0o700)
        (state / "generation.sqlite3").write_bytes(b"this is not a database file at all" * 100)
        h = fakes.Harness(None, root=temp.name)
        self.harnesses.append(h)
        entry = h.host.status()["generation"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("history file could not be opened", entry["error"])
        self.assertEqual([c["status"] for c in h.host.checks() if c["id"] == "generation.start"], ["fail"])
        with self.assertRaises(ExtensionError):
            h.call("pause")
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("myradio-gen")], [])

    def test_a_history_file_that_cannot_be_prepared_does_not_leave_its_connection_open(self):
        from myradio.extensions import Context
        opened, real = [], Context.open_db

        def spy(ctx, filename):
            opened.append(real(ctx, filename))
            return opened[-1]

        with patch.object(Context, "open_db", spy), patch("myradio.generation.extension.JobStore", side_effect=sqlite3.DatabaseError("cannot be prepared")):
            h = fakes.Harness(None)
            self.harnesses.append(h)
        entry = h.host.status()["generation"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("history file could not be opened", entry["error"])
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_a_settings_file_that_is_a_link_is_not_followed(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        outside = Path(temp.name) / "outside.json"
        outside.write_text(json.dumps({"workers": [fakes.worker_cfg("w1")]}))
        state = Path(temp.name) / "ext" / "generation"
        state.mkdir(parents=True, mode=0o700)
        os.symlink(outside, state / "config.json")
        h = fakes.Harness(None, root=temp.name)
        self.harnesses.append(h)
        status = h.status()
        self.assertEqual(status["workers"], [])
        self.assertIn("could not be used", " ".join(status["config"]["problems"]))

    def test_deleting_the_audio_lets_the_same_key_make_it_again(self):
        h = self.harness(config={"buffer": {"host_break": {"low": 1, "high": 5}}})
        args = {"role": "host_break", "payload": {"text": "Slot nine needs its station ID again."}, "idempotency_key": "slot:9"}
        first = h.call("enqueue", args)["job"]
        self.done(h, first)
        self.assertFalse(h.call("enqueue", args)["created"])
        h.station.delete_track(h.station.imports[0]["id"])
        h.engine._last_reconcile = 0
        h.engine._reconcile()
        again = h.call("enqueue", args)
        self.assertTrue(again["created"])
        self.assertNotEqual(again["job"]["id"], first["id"])
        self.assertEqual(self.done(h, again["job"])["state"], "succeeded")
        self.assertEqual(len(self.imports(h)), 2)

    def test_probe_is_rate_limited(self):
        h = self.harness(probe_min_interval=60)
        self.assertEqual(h.route("POST", "/probe", body={}).status, 200)
        limited = h.route("POST", "/probe", body={})
        self.assertEqual(limited.status, 429)
        self.assertIn("Try again in", limited.body["error"])
        self.assertGreater(limited.body["retry_after_s"], 0)

    def test_after_stop_no_threads_remain(self):
        h = self.harness([fakes.worker_cfg("music", roles=["song"], resource="gpu0"), fakes.worker_cfg("voice", roles=["host_break"], resource="gpu1")],
                         config={"resources": {"gpu0": {"max_concurrent": 2}, "gpu1": {"max_concurrent": 1}}})
        self.assertGreaterEqual(len([t for t in threading.enumerate() if t.name.startswith("myradio-gen")]), 4)
        for n in range(3):
            self.done(h, h.enqueue("host_break", {"text": f"Script {n} to exercise the threads."}))
        h.close()
        leftovers = [t.name for t in threading.enumerate() if t.name.startswith(("myradio-gen", "myradio-ext-generation"))]
        self.assertEqual(leftovers, [])
        self.assertEqual(h.ext._engine.alive_threads(), [])


class FullCheckTests(EngineCase):
    """A test clip (the voice maker's warm-up, the music maker's test song) that fails is tried again slowly, never in a tight
    loop, and never stands in front of real work. Before the fix a failing test clip was repeated thousands of times a second
    and the other makers in the same group never got a turn."""

    def server(self, mode):
        server = fakes.FakeSpeechServer(mode)
        self.addCleanup(server.close)
        return server

    @staticmethod
    def voice(server, **extra):
        return {"id": "voice", "kind": "drama_http", "base_url": server.url, "request_timeout_s": 5, "warmup_timeout_s": 10, **extra}

    @staticmethod
    def worker(h, wid):
        return next(w for w in h.status()["workers"] if w["id"] == wid)

    def test_a_voice_server_that_answers_but_cannot_speak_is_not_hammered_and_other_makers_keep_working(self):
        server = self.server("error500")
        h = self.harness([self.voice(server), fakes.worker_cfg("music", roles=["song"])])
        song = h.enqueue("song", {"title": "Must not wait for the voice maker"})
        self.assertEqual(self.done(h, song)["state"], "succeeded")
        time.sleep(1.0)
        self.assertLessEqual(len(server.speech_requests()), 3, "one warm-up and then a wait, not a new request after every answer")
        self.assertLessEqual(len([r for r in server.requests if r["method"] == "GET"]), 4)
        self.assertNotEqual(self.worker(h, "voice")["state"], "ready")
        self.assertFalse(h.status()["capabilities"]["speech_generation"])

    def test_a_voice_job_still_gets_its_turn_while_the_warm_up_keeps_failing(self):
        server = self.server("error500")
        h = self.harness([self.voice(server)], config={"retry": {"max_attempts": 1}})
        job = h.enqueue("host_break")
        done = self.done(h, job)
        self.assertEqual((done["state"], done["attempts"]), ("failed", 1), "the job was attempted, and failed by the normal rules")
        self.assertLessEqual(len(server.speech_requests()), 3)

    def test_a_test_song_that_fails_is_not_started_again_and_again(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        notes = Path(temp.name)
        script = fakes.write_script(temp.name, "fail_noted")
        music = {"id": "music", "kind": "yue2_cli", "smoke_test": True, "timeout_s": 60,
                 "command": [sys.executable, script, "--out", "{output_file}", "--notes", str(notes)]}
        h = self.harness([music, fakes.worker_cfg("voice", roles=["host_break"])])
        job = h.enqueue("host_break", {"text": "A spoken link that must not wait for the music maker's test."})
        self.assertEqual(self.done(h, job)["state"], "succeeded")
        runs_file = notes / "runs.txt"
        self.assertTrue(wait_for(lambda: runs_file.exists()), "the test song was started")
        time.sleep(1.0)
        runs = runs_file.read_text().count("run")
        self.assertTrue(1 <= runs <= 2, f"the test song program was started {runs} times")
        self.assertFalse(h.status()["capabilities"]["music_generation"])

    def test_a_music_program_that_ignores_the_asked_length_passes_its_test_song(self):
        # The documented command has no {duration_s}, so the program makes a song of its own natural length. The test song only has
        # to prove that the program runs: before the fix anything over 40 s failed the maker although real jobs of that length passed.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        music = {"id": "music", "kind": "yue2_cli", "smoke_test": True, "timeout_s": 120,
                 "command": [sys.executable, fakes.write_script(temp.name, "ok"), "--out", "{output_file}", "--seconds", "45"]}
        h = self.harness([music])
        self.assertTrue(wait_for(lambda: self.worker(h, "music")["evidence"] == "test_clip_made", timeout=90),
                        f"the 45 s test song was accepted: {self.worker(h, 'music')}")
        music_view = self.worker(h, "music")
        self.assertEqual((music_view["state"], music_view["ready"]), ("ready", True))
        self.assertTrue(h.status()["capabilities"]["music_generation"])

    def test_automatic_test_songs_stop_after_three_failures_and_only_a_person_starts_another(self):
        # Each test song is a whole model run on the shared card; before the fix a maker that could not pass was started again
        # every five minutes for ever, and every "Check again" or quick check re-armed it.
        clock = fakes.FakeClock()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runs_file = Path(temp.name) / "runs.txt"
        runs = lambda: runs_file.read_text().count("run") if runs_file.exists() else 0
        music = {"id": "music", "kind": "yue2_cli", "smoke_test": True, "timeout_s": 60,
                 "command": [sys.executable, fakes.write_script(temp.name, "fail_noted"), "--out", "{output_file}", "--notes", temp.name]}
        h = self.harness([music], clock=clock)
        self.assertTrue(wait_for(lambda: runs() == 1), "the first test song is made by itself")
        for expected in (2, 3):
            self.assertTrue(wait_for(lambda: not h.engine.wstate["music"].probing))
            clock.advance(301)  # past any wait that follows a failed test
            h.engine.poke()
            self.assertTrue(wait_for(lambda: runs() == expected), f"test song {expected} is made by itself after the wait")
        self.assertTrue(wait_for(lambda: not h.engine.wstate["music"].probing))
        for _ in range(4):  # waits, quick checks and "Check again" no longer start one
            clock.advance(301)
            h.engine.poke()
            h.route("POST", "/probe", body={})
            time.sleep(0.3)
        self.assertEqual(runs(), 3, "three failed test songs in a row, then the engine stops making them by itself")
        self.assertIn("stopped making them by itself", self.worker(h, "music")["hint"])
        self.assertIn("Run a full test", self.worker(h, "music")["hint"])
        self.assertFalse(h.status()["capabilities"]["music_generation"])
        self.assertTrue(h.route("POST", "/probe", body={"full": True}).body["full_check_queued"])
        self.assertTrue(wait_for(lambda: runs() == 4), "a person's Run a full test still makes one")
        self.assertTrue(wait_for(lambda: not h.engine.wstate["music"].probing))
        clock.advance(301)
        h.engine.poke()
        time.sleep(0.5)
        self.assertEqual(runs(), 4, "and failing again does not restart the automatic ones")

    def test_the_wait_after_failed_tests_doubles_up_to_five_minutes_and_never_more(self):
        from myradio.generation.engine import full_retry_delay
        self.assertEqual([full_retry_delay(n) for n in range(1, 8)], [15, 30, 60, 120, 240, 300, 300])
        for failures in (0, 50, 10 ** 6):
            self.assertLessEqual(full_retry_delay(failures), 300.0, f"{failures} failures")

    def test_the_wait_after_a_failed_test_doubles_and_a_working_server_then_passes(self):
        clock = fakes.FakeClock()
        server = self.server("error500")
        h = self.harness([self.voice(server)], clock=clock)
        posts = lambda: len(server.speech_requests())
        self.assertTrue(wait_for(lambda: posts() == 1))
        expected = 1
        for wait in (15, 30, 60):  # the wait after the 1st, 2nd and 3rd failure
            clock.advance(wait - 1)
            h.engine.poke()
            time.sleep(0.3)
            self.assertEqual(posts(), expected, f"still waiting out {wait} seconds")
            clock.advance(2)
            h.engine.poke()
            expected += 1
            self.assertTrue(wait_for(lambda: posts() == expected), f"tried again after {wait} seconds")
        server.mode = "ok24"
        clock.advance(121)
        h.engine.poke()
        self.assertTrue(wait_for(lambda: self.worker(h, "voice")["state"] == "ready"))
        voice = self.worker(h, "voice")
        self.assertEqual((voice["evidence"], voice["ready"]), ("test_clip_made", True))
        self.assertTrue(h.status()["capabilities"]["speech_generation"])
        total = posts()
        time.sleep(0.4)
        self.assertEqual(posts(), total, "once it works, no more test clips are made")

    def test_a_person_asking_for_a_full_test_is_not_held_back_by_the_wait(self):
        clock = fakes.FakeClock()
        server = self.server("error500")
        h = self.harness([self.voice(server)], clock=clock)
        self.assertTrue(wait_for(lambda: len(server.speech_requests()) == 1 and self.worker(h, "voice")["state"] == "failed"))
        server.mode = "ok24"  # fixed; the engine's own next try is 15 seconds away and the clock does not move
        reply = h.route("POST", "/probe", body={"full": True})
        self.assertTrue(reply.body["full_check_queued"])
        self.assertIn("test clip", reply.body["full_check_message"])
        self.assertTrue(wait_for(lambda: self.worker(h, "voice")["state"] == "ready"))

    def test_the_answer_to_a_full_test_says_so_when_nothing_could_be_started(self):
        self.control().probe = ProbeResult(False, "unreachable", "The voice maker is not answering.", "Start it, then choose Check again.")
        h = self.harness()
        body = h.route("POST", "/probe", body={"full": True}).body
        self.assertFalse(body["full_check_queued"])
        self.assertIn("No test clip was started", body["full_check_message"])
        self.assertNotIn("full_check_message", h.route("POST", "/probe", body={}).body)

    def test_a_full_test_makes_a_real_clip_even_when_the_maker_is_not_set_up_to_test_by_itself(self):
        server = self.server("ok24")
        h = self.harness([self.voice(server, smoke_test=False)])
        self.assertTrue(wait_for(lambda: self.worker(h, "voice")["state"] == "ready"))
        self.assertEqual((self.worker(h, "voice")["evidence"], len(server.speech_requests())), ("setup_checked", 0), "nothing is made by itself")
        self.assertFalse(h.status()["proven"]["speech_generation"])
        self.assertTrue(h.route("POST", "/probe", body={"full": True}).body["full_check_queued"])
        self.assertTrue(wait_for(lambda: len(server.speech_requests()) == 1), "asked for by a person, so a clip is made")
        self.assertTrue(wait_for(lambda: self.worker(h, "voice")["evidence"] == "test_clip_made"))
        self.assertTrue(h.status()["proven"]["speech_generation"])


class HonestReadinessTests(EngineCase):
    """What a maker's card and the status claim, and how a maker that failed gets back: a "degraded" maker (three failed attempts in a row,
    or a failed test clip) has to be able to come back by following the screen's advice; "ready" must not be taken for "has made something"."""

    def degraded(self):
        self.control().errors = [WorkerError("worker_failed", "The maker stopped with a problem.", retryable=False)] * 3
        h = self.harness()
        for n in range(3):
            self.done(h, h.enqueue("host_break", {"text": f"Script number {n} that will fail for the test."}))
        self.assertTrue(wait_for(lambda: h.status()["workers"][0]["state"] == "degraded"))
        return h

    def test_the_hint_names_the_button_that_works(self):
        h = self.degraded()
        self.assertIn("Run a full test", h.status()["workers"][0]["hint"])
        self.assertFalse(h.status()["capabilities"]["speech_generation"])

    def test_choosing_check_again_proves_a_degraded_maker_and_brings_it_back(self):
        h = self.degraded()
        h.route("POST", "/probe", body={})
        self.assertTrue(wait_for(lambda: h.status()["workers"][0]["state"] == "ready"), "the engine made its own test clip and it passed")
        self.assertEqual(h.status()["workers"][0]["recent_failures"], 0)
        self.assertTrue(h.status()["capabilities"]["speech_generation"])

    def test_choosing_run_a_full_test_brings_it_back_too(self):
        h = self.degraded()
        self.assertTrue(h.route("POST", "/probe", body={"full": True}).body["full_check_queued"])
        self.assertTrue(wait_for(lambda: h.status()["workers"][0]["state"] == "ready"))
        self.assertTrue(h.status()["capabilities"]["speech_generation"])

    def test_a_music_program_that_exists_but_is_broken_is_trusted_only_as_far_as_it_was_tested(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runs = lambda: (Path(temp.name) / "runs.txt").read_text().count("run") if (Path(temp.name) / "runs.txt").exists() else 0
        music = {"id": "music", "kind": "yue2_cli", "timeout_s": 60,
                 "command": [sys.executable, fakes.write_script(temp.name, "fail_noted"), "--out", "{output_file}", "--notes", temp.name]}
        h = self.harness([music])
        self.assertTrue(wait_for(lambda: h.status()["workers"] and h.status()["workers"][0]["state"] == "ready"))
        status = h.status()
        self.assertEqual(status["workers"][0]["evidence"], "setup_checked")
        self.assertIn("nothing made yet", status["workers"][0]["evidence_label"])
        self.assertTrue(status["capabilities"]["music_generation"], "its files and program look right, which is all that was checked")
        self.assertFalse(status["proven"]["music_generation"], "but nothing has been made, so nothing is proven")
        self.assertEqual(runs(), 0)
        self.assertTrue(h.route("POST", "/probe", body={"full": True}).body["full_check_queued"])
        self.assertTrue(wait_for(lambda: runs() == 1), "a full test really runs the program, whatever smoke_test says")
        self.assertTrue(wait_for(lambda: not h.status()["capabilities"]["music_generation"]))
        h.route("POST", "/probe", body={})  # a cheap check afterwards cannot overrule a test that failed
        time.sleep(0.5)
        worker = h.status()["workers"][0]
        self.assertEqual(worker["state"], "degraded")
        self.assertIn("test", worker["message"])
        self.assertFalse(h.status()["capabilities"]["music_generation"])
        self.assertEqual(runs(), 1, "the next test waits; the program is not started again at once")

    def test_a_maker_counts_as_set_up_after_its_check_but_as_proven_only_after_it_made_something(self):
        h = self.harness()
        self.assertTrue(wait_for(lambda: h.status()["capabilities"]["speech_generation"]))
        status = h.status()
        self.assertFalse(status["proven"]["speech_generation"])
        self.assertEqual(status["workers"][0]["evidence_label"], "Set up and checked; nothing made yet")
        self.assertEqual((status["headline"], status["next_step"]), ("The makers are ready.", "Nothing has been made yet to prove it. Choose Run a full test to be sure."))
        voice = {c["id"]: c for c in h.host.checks()}["generation.voice"]
        self.assertEqual((voice["status"], voice["hint"]), ("ok", "Nothing has been made yet to prove it. Choose Run a full test to be sure."))
        self.done(h, h.enqueue())
        self.assertTrue(wait_for(lambda: h.status()["proven"]["speech_generation"]))
        self.assertTrue(wait_for(lambda: h.status()["next_step"] == "" and {c["id"]: c for c in h.host.checks()}["generation.voice"]["hint"] == ""),
                        "once something has really been made there is nothing left to prove")
        self.assertEqual(h.status()["workers"][0]["evidence"], "real_job_completed")
        self.assertEqual(h.status()["proven"]["roles"]["host_break"], True)
        self.assertEqual(h.status()["proven"]["roles"]["song"], True, "the controlled maker covers every kind")

    def test_a_test_tone_maker_never_claims_to_have_made_real_work(self):
        h = self.harness([{"id": "tones", "kind": "controlled_fixture", "resource": "gpu0"}])
        self.done(h, h.enqueue())
        self.assertTrue(wait_for(lambda: h.status()["workers"][0]["evidence"] == "test_tones"))
        worker = h.status()["workers"][0]
        self.assertEqual(worker["evidence_label"], "Test tones only; nothing real has been made")
        self.assertTrue(worker["fixture"])
        h.route("POST", "/probe", body={})
        self.assertEqual(h.status()["workers"][0]["evidence"], "test_tones", "a later check does not raise it")
        self.assertFalse(h.status()["proven"]["speech_generation"])
        self.assertFalse(h.status()["capabilities"]["speech_generation"])

    def test_a_status_that_is_out_of_date_claims_nothing(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock)
        self.assertTrue(wait_for(lambda: h.status()["capabilities"]["speech_generation"]))
        status = {}
        for _ in range(50):  # the engine's own thread refreshes the snapshot about once a second, so look again if it got in first
            h.engine._snapshot_at = clock() - 120
            status = h.ext.status()
            if status.get("stale"):
                break
        self.assertTrue(status.get("stale"))
        self.assertEqual(status["capabilities"], {"music_generation": False, "speech_generation": False, "roles": {r: False for r in fakes.ROLES}})
        self.assertFalse(status["proven"]["speech_generation"])
        self.assertIn("out of date", status["headline"])
        self.assertEqual([w["ready"] for w in status["workers"]], [False], "no maker card says ready either")
        h.engine._publish()
        self.assertTrue(wait_for(lambda: "stale" not in h.ext.status() and h.ext.status()["capabilities"]["speech_generation"]))


class SchedulingFixTests(EngineCase):
    def test_a_long_line_of_jobs_the_buffer_will_not_run_cannot_hide_one_it_will(self):
        h = self.harness([fakes.worker_cfg("voice", roles=["host_break"]), fakes.worker_cfg("music", roles=["song"])],
                         config={"buffer": {"host_break": {"low": 1, "high": 1}}, "limits": {"max_queued": 600}})
        self.done(h, h.enqueue("host_break"))  # the voice buffer is now at its high mark
        for n in range(310):  # more than the 300 the scheduler looks at; every one is held back, and they all outrank the song
            h.enqueue("host_break", {"text": f"Held back script number {n} waiting for room in the buffer."}, priority=60)
        song = h.enqueue("song", {"title": "Needs to run: nothing is ready for music"}, priority=50)
        self.assertEqual(self.done(h, song, timeout=40)["state"], "succeeded")
        self.assertEqual(h.status()["jobs"]["queued"], 310, "the held-back jobs are still waiting, untouched")

    def test_a_job_at_exactly_the_urgent_priority_ignores_the_marks(self):
        h = self.harness(config={"buffer": {"host_break": {"low": 1, "high": 1}}})
        self.done(h, h.enqueue("host_break"))
        just_under = h.enqueue("host_break", {"text": "One notch under urgent, so it is held back."}, priority=79)
        exactly = h.enqueue("host_break", {"text": "Exactly urgent, so the marks do not apply."}, priority=80)
        self.assertEqual(self.done(h, exactly)["state"], "succeeded")
        time.sleep(0.3)
        self.assertEqual(h.job(just_under["id"])["state"], "queued")

    def test_leftover_test_tones_do_not_hold_back_a_real_maker(self):
        marks = {"host_break": {"low": 1, "high": 1}}
        h = self.harness([{"id": "tones", "kind": "controlled_fixture", "resource": "gpu0", "roles": ["host_break"]}], config={"buffer": marks})
        self.done(h, h.enqueue("host_break"))
        self.assertEqual(h.route("GET", "/buffer").body["roles"]["host_break"]["ready"], 1, "while the maker is the test maker, its tones count")
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1", roles=["host_break"])], "buffer": marks}, root=root)
        self.harnesses.append(again)
        view = again.route("GET", "/buffer").body["roles"]["host_break"]
        self.assertEqual((view["ready"], view["filling"]), (0, True), "the old test tones are not audio a real maker can count on")
        self.assertEqual(self.done(again, again.enqueue("host_break", {"text": "A real script for the real maker."}))["state"], "succeeded")

    def test_the_test_tone_flag_of_a_job_survives_taking_the_test_maker_out_of_the_settings(self):
        h = self.harness([{"id": "tones", "kind": "controlled_fixture", "resource": "gpu0"}])
        done = self.done(h, h.enqueue("song", {"title": "Sunny Morning"}))
        self.assertTrue(done["fixture"])
        root = h.root
        h.stop()
        again = fakes.Harness({"workers": [fakes.worker_cfg("w1")]}, root=root)  # the test maker is gone from the settings
        self.harnesses.append(again)
        view = again.job(done["id"])
        self.assertTrue(view["fixture"], "the job still says it was made by a test maker")
        self.assertTrue(view["provenance"]["fixture"])
        self.assertTrue(again.route("GET", "/jobs").body["jobs"][0]["fixture"])
        self.assertTrue(again.status()["recent"][0]["fixture"])

    def test_a_long_time_limit_is_not_cut_to_an_hour(self):
        h = self.harness()
        self.done(h, h.enqueue())
        self.assertGreaterEqual(self.control().calls[0]["deadline_s"], 7200 + 3600, "a music program may be given 2 hours plus a 1 hour planning step")

    def test_a_failure_while_a_cancel_arrives_is_a_cancel_not_a_retry(self):
        h = self.harness([{"id": "w1", "kind": "fails_when_stopped", "resource": "gpu0"}], kinds={"fails_when_stopped": FailsWhenStopped})
        job = h.enqueue()
        self.assertTrue(wait_for(self.control().entered.is_set))
        reply = h.call("cancel", {"job_id": job["id"]})
        self.assertEqual((reply["job"]["state"], reply["settled"]), ("cancelled", True))
        self.assertEqual(h.job(job["id"])["error_code"], "cancelled")
        self.assertEqual(h.status()["jobs"]["queued"], 0, "it was not put back in line")

    def test_a_failure_while_work_is_being_paused_puts_the_job_back_without_using_an_attempt(self):
        h = self.harness([{"id": "w1", "kind": "fails_when_stopped", "resource": "gpu0"}], kinds={"fails_when_stopped": FailsWhenStopped})
        job = h.enqueue()
        self.assertTrue(wait_for(self.control().entered.is_set))
        h.call("pause", {"cancel_running": True})
        self.assertTrue(wait_for(lambda: h.job(job["id"])["state"] == "queued"))
        self.assertEqual(h.job(job["id"])["attempts"], 0)

    def test_a_job_the_database_calls_running_but_nothing_is_running_goes_back_in_line(self):
        h = self.harness()
        h.call("pause")
        job = h.enqueue()
        h.engine.store.start(job["id"], time.time(), worker_id="w1", resource="gpu0", seed_used=1)  # the database says running; no thread is
        h.engine._reap_orphans()
        back = h.job(job["id"])
        self.assertEqual((back["state"], back["attempts"]), ("queued", 0))
        self.assertIn("unexpected problem", back["note"])
        h.call("resume")
        self.assertEqual(self.done(h, job)["state"], "succeeded")

    def test_a_maker_switched_off_stays_visible_and_its_waiting_jobs_stay_in_line(self):
        h = self.harness([fakes.worker_cfg("w1", roles=["host_break"])])
        h.call("pause")
        job = h.enqueue()
        root = h.root
        h.stop()
        off = {"workers": [dict(fakes.worker_cfg("w1", roles=["host_break"]), enabled=False)]}
        again = fakes.Harness(off, root=root)
        self.harnesses.append(again)
        self.assertEqual(again.job(job["id"])["state"], "queued", "switched off is not gone: the job keeps waiting")
        status = again.status()
        self.assertEqual([(w["id"], w["state"], w["state_label"], w["ready"]) for w in status["workers"]], [("w1", "blocked", "Switched off", False)])
        self.assertIn("switched off", status["workers"][0]["message"])
        self.assertIn("switched off", status["headline"])
        self.assertFalse(status["capabilities"]["speech_generation"])
        self.assertEqual(status["config"]["problems"], [], "switching a maker off is not a problem")
        with self.assertRaisesRegex(ExtensionError, "switched off"):
            again.enqueue()
        checks = {c["id"]: c for c in again.host.checks()}
        self.assertEqual(checks["generation.voice"]["status"], "warn")
        self.assertIn("switched off", checks["generation.voice"]["detail"])
        (again.state_dir / "config.json").write_text(json.dumps({"workers": [fakes.worker_cfg("w1", roles=["host_break"])]}))
        self.assertTrue(again.route("POST", "/probe", body={}).body["reloaded"])
        again.call("resume")
        self.assertEqual(self.done(again, job)["state"], "succeeded")

    def test_marks_set_by_operation_give_way_when_the_settings_file_changes_them(self):
        marks = lambda low, high: {"workers": [fakes.worker_cfg("w1")], "buffer": {"host_break": {"low": low, "high": high}}}
        first = fakes.Harness(marks(2, 4))
        self.harnesses.append(first)
        first.call("set_buffer", {"role": "host_break", "low": 3, "high": 6})
        view = first.route("GET", "/buffer").body["roles"]["host_break"]
        self.assertEqual((view["low"], view["high"], view["marks_from"]), (3, 6, "operation"))
        root = first.root
        first.stop()
        same = fakes.Harness(marks(2, 4), root=root)
        self.harnesses.append(same)
        view = same.route("GET", "/buffer").body["roles"]["host_break"]
        self.assertEqual((view["low"], view["high"], view["marks_from"]), (3, 6, "operation"), "the file did not change, so the override stands")
        same.stop()
        edited = fakes.Harness(marks(5, 8), root=root)
        self.harnesses.append(edited)
        body = edited.route("GET", "/buffer").body
        view = body["roles"]["host_break"]
        self.assertEqual((view["low"], view["high"], view["marks_from"]), (5, 8, "settings"), "the person edited the file, so the file wins")
        self.assertEqual(body["overrides"], {})


class FailsWhenStopped(fakes.ControlledWorker):
    """Waits to be told to stop, then fails with an ordinary error instead of reporting that it was cancelled (a program that
    was killed and exited with an error is exactly this)."""
    kind = "fails_when_stopped"

    def generate(self, request):
        self.control().entered.set()
        while not request.cancel.wait(0.02):
            pass
        raise WorkerError("worker_failed", "The maker stopped with a problem.", detail="it was killed")


class FinishingTests(EngineCase):
    """What the finished file really sounds like. Levels use the audio module's own measuring, edges and fades are measured with
    the standard library only; neither relies on what the engine says it did."""

    def audio(self):
        from myradio.generation.audio_api import load_audio
        return load_audio()

    def made(self, h, role="song", payload=None, **kw):
        done = self.done(h, h.enqueue(role, payload, **kw))
        return done, h.station.wavs.get(done["track_id"])

    def lufs(self, wav):
        audio = self.audio()
        return audio.analyze(audio.read_wav(wav)).loudness_lufs

    def test_the_peak_ceiling_holds_for_the_converted_samples_that_go_on_air(self):
        # A 48 kHz take with a short burst of + + - - (a 12 kHz tone whose every sample sits at 0.707 of its true peak) over a quiet bed:
        # normalising is held back by the ceiling, and converting to 44.1 kHz afterwards brings the true peak out, about 3 dB over
        # the ceiling (to the rail for a -1 dBFS ceiling). Converting first makes the ceiling a promise about the broadcast samples.
        import array
        import math
        from myradio.generation import audio_api
        from myradio.generation.config import FinishSpec
        audio = self.audio()
        rate, data = 48000, array.array("h")
        for i in range(rate * 3):
            v = 20000 if (rate <= i < rate + 480 and i % 4 < 2) else -20000 if rate <= i < rate + 480 else int(3000 * math.sin(2 * math.pi * 1000 * i / rate))
            data.extend((v, v))
        take = audio.Pcm(data.tobytes(), rate, 2)

        def peak_db(pcm):
            samples = array.array("h")
            samples.frombytes(pcm.frames)
            if sys.byteorder == "big":
                samples.byteswap()
            return 20 * math.log10(max(abs(x) for x in samples) / 32768)

        for ceiling in (-1.0, -6.0):
            with self.subTest(ceiling=ceiling):
                spec = FinishSpec(target_lufs=-16.0, ceiling_dbfs=ceiling, strict_tail=False)
                old_order = audio.to_station_format(audio.normalize(audio.fade(audio.trim_silence(take, threshold_dbfs=-50.0, keep_ms=60), 15, 250),
                                                                    target_lufs=-16.0, ceiling_dbfs=ceiling))
                self.assertGreater(peak_db(old_order), ceiling + 0.9, "the take is hostile: the old order breaks the ceiling")
                finished = audio_api.finish(audio, take, role="song", min_s=1.0, max_s=10.0, spec=spec)
                aired = audio.read_wav(finished.wav)
                self.assertEqual((aired.rate, aired.channels), (44100, 2))
                self.assertLessEqual(peak_db(aired), ceiling + 0.01, "the broadcast samples stay at or under the ceiling")
                self.assertEqual(finished.levels.get("limited_by"), "ceiling")
                self.assertEqual(list(finished.steps_s), ["validate", "convert", "trim", "fade", "normalize", "write"])

    def test_quiet_and_hot_takes_both_end_up_at_the_station_level(self):
        for label, amplitude in (("quiet", 0.03), ("hot", 0.9)):
            with self.subTest(label):
                fakes.reset_controls()
                h = self.harness()
                self.control().amplitude, self.control().seconds = amplitude, 4.0
                done, wav = self.made(h)
                self.assertEqual(done["state"], "succeeded", done.get("error"))
                self.assertAlmostEqual(self.lufs(wav), -16.0, delta=1.0)
                self.assertLess(fakes.measure(wav)["peak"], 10 ** (-1.0 / 20) + 0.001, "never above the -1 dBFS ceiling")
                h.close()

    def test_the_target_level_comes_from_the_settings(self):
        h = self.harness(config={"finishing": {"target_lufs": -22.0}})
        self.control().seconds = 4.0
        done, wav = self.made(h)
        self.assertEqual(done["state"], "succeeded", done.get("error"))
        self.assertAlmostEqual(self.lufs(wav), -22.0, delta=1.0)

    def test_silence_in_front_of_the_sound_is_cut_to_a_short_pause(self):
        h = self.harness()
        self.control().lead_s, self.control().seconds = 2.0, 4.0
        done, wav = self.made(h)
        self.assertEqual(done["state"], "succeeded", done.get("error"))
        facts = fakes.measure(wav)
        self.assertLess(facts["lead_s"], 0.2, "two seconds of silence in front were trimmed")
        self.assertGreater(facts["seconds"], 3.9, "the sound itself was kept")

    def test_both_ends_are_faded_so_they_never_click(self):
        h = self.harness(config={"finishing": {"strict_tail": False}})  # let a take that starts and stops abruptly through to see the fades
        self.control().fade_ms, self.control().seconds = 0, 4.0
        done, wav = self.made(h)
        self.assertEqual(done["state"], "succeeded", done.get("error"))
        facts = fakes.measure(wav)
        self.assertLess(facts["head_peak"], 0.25 * facts["peak"], "the start was faded in")
        self.assertLess(facts["tail_peak"], 0.10 * facts["peak"], "the end was faded out")

    def test_a_take_that_stops_abruptly_is_refused_unless_the_settings_allow_it(self):
        h = self.harness(config={"retry": {"max_attempts": 1}})
        self.control().fade_ms, self.control().seconds = 0, 4.0
        failed = self.done(h, h.enqueue("song"))
        self.assertEqual((failed["state"], failed["error_code"]), ("failed", "audio_cut_off"))
        self.assertEqual(self.imports(h), [])
        lenient = self.harness(config={"finishing": {"strict_tail": False}})
        done, wav = self.made(lenient)
        self.assertEqual(done["state"], "succeeded")
        notes = {n["code"]: n for n in done["provenance"]["validation"]}
        self.assertFalse(notes["truncated_tail"]["fatal"], "kept as a note in the provenance")

    def test_a_distorted_take_is_noted_and_refused_only_when_the_settings_say_so(self):
        h = self.harness()
        self.control().amplitude, self.control().seconds = 1.6, 4.0
        done, wav = self.made(h)
        self.assertEqual(done["state"], "succeeded", done.get("error"))
        self.assertEqual([n["fatal"] for n in done["provenance"]["validation"] if n["code"] == "clipped"], [False])
        strict = self.harness(config={"finishing": {"strict_clipping": True}, "retry": {"max_attempts": 1}})
        failed = self.done(strict, strict.enqueue("song"))
        self.assertEqual((failed["state"], failed["error_code"]), ("failed", "audio_unusable"))
        self.assertEqual(self.imports(strict), [])

    def test_a_spoken_result_far_longer_than_asked_for_is_thrown_away(self):
        h = self.harness(config={"retry": {"max_attempts": 1}})
        self.control().seconds = 6.0
        failed = self.done(h, h.enqueue("id", {"max_seconds": 1}))  # up to 4 seconds are accepted for a 1 second request
        self.assertEqual((failed["state"], failed["error_code"]), ("failed", "audio_too_long"))
        self.assertEqual(self.imports(h), [])


class AfterTheImportTests(EngineCase):
    """Once the audio is in the library nothing may make it a second time."""

    def test_a_failed_save_after_the_audio_was_imported_never_makes_it_again(self):
        h = self.harness()
        real, calls = h.engine.store.finish_success, []

        def flaky(*args, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kw)

        with self.assertLogs("myradio.generation", level="WARNING"), patch.object(h.engine.store, "finish_success", flaky):
            done = self.done(h, h.enqueue())
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(len(self.imports(h)), 1, "one job, one track")
        self.assertEqual(len(self.control().calls), 1, "and the audio was made once")
        self.assertEqual(done["provenance"]["track_id"], h.station.imports[0]["id"])

    def test_a_job_whose_audio_is_in_the_library_is_written_down_later_not_made_again(self):
        h = self.harness()
        real, calls = h.engine.store.finish_success, []

        def broken_for_a_while(*args, **kw):
            calls.append(1)
            if len(calls) <= 3:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kw)

        with self.assertLogs("myradio.generation", level="WARNING"), patch("myradio.generation.engine.SUCCESS_WRITE_TRIES", 2), \
                patch.object(h.engine.store, "finish_success", broken_for_a_while):
            job = h.enqueue()
            self.assertTrue(wait_for(lambda: len(calls) >= 4, 30), "the engine keeps trying to write it down")
            done = self.done(h, job)
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(len(self.imports(h)), 1)
        self.assertEqual(len(self.control().calls), 1)

    def test_a_value_a_maker_returns_that_json_cannot_hold_does_not_cost_a_second_copy(self):
        h = self.harness([{"id": "odd", "kind": "odd_meta", "resource": "gpu0"}], kinds={"odd_meta": OddMetaWorker})
        done = self.done(h, h.enqueue())
        self.assertEqual((done["state"], len(self.imports(h))), ("succeeded", 1))
        self.assertIn("object", done["provenance"]["timings"]["worker_meta"]["thing"], "kept as text instead of failing the save")

    def test_a_maker_result_that_refers_to_itself_is_still_written_down_without_a_second_copy(self):
        h = self.harness([{"id": "odd", "kind": "odd_meta", "resource": "gpu0"}], kinds={"odd_meta": OddMetaWorker})
        OddMetaWorker.circular = True
        self.addCleanup(setattr, OddMetaWorker, "circular", False)
        done = self.done(h, h.enqueue())
        self.assertEqual((done["state"], len(self.imports(h))), ("succeeded", 1))
        self.assertEqual(done["provenance"]["timings"]["worker_meta"], {}, "the part JSON cannot hold was left out, the rest was kept")
        self.assertGreater(done["provenance"]["duration_s"], 1.0)

    def test_a_library_that_refuses_the_audio_does_not_make_the_audio_again_for_each_retry(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 3, "base_s": 1, "cap_s": 2}})
        h.station.fail_next = [ValueError("The library is busy."), ValueError("The library is busy.")]
        job = h.enqueue()
        for expected in (1, 2):
            self.assertTrue(wait_for(lambda: h.job(job["id"])["attempts"] == expected and h.job(job["id"])["state"] == "queued"))
            clock.advance(5)
            h.engine.poke()
        done = self.done(h, job)
        self.assertEqual((done["state"], done["attempts"]), ("succeeded", 3))
        self.assertEqual(len(self.control().calls), 1, "only the import was repeated; a song would have cost minutes of the card each time")
        self.assertEqual(len(self.imports(h)), 1)
        self.assertGreater(done["provenance"]["duration_s"], 1.0)

    def test_settings_are_not_swapped_while_a_finished_job_is_still_being_written_down(self):
        h = self.harness([fakes.worker_cfg("w1", roles=["host_break"])])
        real, broken = h.engine.store.finish_success, {"on": True}

        def maybe_broken(*args, **kw):
            if broken["on"]:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kw)

        with self.assertLogs("myradio.generation", level="WARNING"), patch("myradio.generation.engine.SUCCESS_WRITE_TRIES", 1), \
                patch.object(h.engine.store, "finish_success", maybe_broken):
            job = h.enqueue()
            self.assertTrue(wait_for(lambda: h.engine._imported), "the audio is in the library but the job is not written down")
            (h.state_dir / "config.json").write_text(json.dumps({"workers": [fakes.worker_cfg("w1", roles=["host_break"]), fakes.worker_cfg("w2", roles=["song"])]}))
            reply = h.route("POST", "/probe", body={})
            self.assertEqual((reply.body["reloaded"], reply.body["settings_changed"]), (False, True))
            self.assertIn("something is being made", reply.body["note"])
            broken["on"] = False
            done = self.done(h, job)
        self.assertEqual((done["state"], len(self.imports(h))), ("succeeded", 1))

    def test_a_held_back_result_is_let_go_when_the_job_is_cancelled(self):
        clock = fakes.FakeClock()
        h = self.harness(clock=clock, config={"retry": {"max_attempts": 3, "base_s": 1, "cap_s": 2}})
        h.station.fail_next = [ValueError("The library is busy.")]
        job = h.enqueue()
        self.assertTrue(wait_for(lambda: h.job(job["id"])["state"] == "queued" and h.job(job["id"])["attempts"] == 1))
        self.assertIn(job["id"], h.engine._kept)
        h.call("cancel", {"job_id": job["id"]})
        self.assertNotIn(job["id"], h.engine._kept)


class OddMetaWorker(fakes.ControlledWorker):
    kind = "odd_meta"
    circular = False

    def generate(self, request):
        output = super().generate(request)
        output.meta = {"thing": object()}  # not something JSON can hold
        if self.circular:
            output.meta["me"] = output.meta  # nor can it hold a value that contains itself
        return output


if __name__ == "__main__":
    unittest.main()
