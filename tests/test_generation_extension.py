"""Generation: operations, routes and doctor checks through the extension host, and end to end in a real Station."""
import http.client
import json
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

from myradio import server as server_module
from myradio.extensions import ExtensionError, builtin_extensions
from myradio.generation.audio_api import load_audio
from myradio.generation.engine import Engine
from myradio.generation.extension import GenerationExtension, build
from myradio.generation.worker_fixture import ENV_VAR
from myradio.server import Server
from myradio.station import Station, StationError

wait_for = fakes.wait_for
TOKEN = "g" * 40
ROLES = ("song", "host_break", "id", "spot", "update")


def setUpModule():
    fakes.use_audio()


def tearDownModule():
    fakes.release_audio()


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        fakes.reset_controls()
        self.h = fakes.Harness({"workers": [fakes.worker_cfg("w1")], "buffer": {r: {"low": 40, "high": 90} for r in ROLES[1:]}})
        self.addCleanup(self.h.close)

    def test_it_loads_as_a_built_in_extension(self):
        self.assertEqual(build().name, "generation")
        self.assertIn("generation", [e.name for e in builtin_extensions()])

    def test_the_operations_are_typed_namespaced_and_carry_no_configuration(self):
        catalog = {c["name"]: c for c in self.h.host.catalog()}
        self.assertEqual(set(catalog), {f"generation.{n}" for n in ("enqueue", "cancel", "retry", "pause", "resume", "set_buffer", "job")})
        enqueue = catalog["generation.enqueue"]
        self.assertEqual(sorted(enqueue["required"]), ["payload", "role"])
        self.assertEqual(enqueue["arguments"]["role"]["enum"], list(ROLES))
        self.assertEqual((enqueue["arguments"]["priority"]["minimum"], enqueue["arguments"]["priority"]["maximum"]), (0, 100))
        self.assertEqual(enqueue["arguments"]["payload"]["type"], "object")
        names = {arg for op in catalog.values() for arg in op["arguments"]}
        for forbidden in ("command", "argv", "url", "base_url", "host", "port", "path", "env", "token", "key", "worker", "resource", "config", "timeout_s"):
            self.assertNotIn(forbidden, names)
        for op in catalog.values():
            self.assertTrue(0 < len(op["description"]) <= 400)

    def test_the_host_rejects_bad_arguments_before_anything_runs(self):
        text = {"text": "Hello there everyone, welcome."}
        bad = [{}, {"role": "host_break"}, {"role": "nope", "payload": text}, {"role": "host_break", "payload": "text"}, {"role": "host_break", "payload": text, "priority": 101},
               {"role": "host_break", "payload": text, "priority": -1}, {"role": "host_break", "payload": text, "priority": 5.5},
               {"role": "host_break", "payload": text, "ttl_s": 1}, {"role": "host_break", "payload": text, "ttl_s": 10 ** 9},
               {"role": "host_break", "payload": text, "command": ["rm", "-rf", "/"]}, {"role": "host_break", "payload": text, "base_url": "http://evil"},
               {"role": "host_break", "payload": text, "idempotency_key": 5}, {"role": "host_break", "payload": {"text": "x" * 70000}}]
        for args in bad:
            with self.subTest(args=str(args)[:70]), self.assertRaises(ExtensionError):
                self.h.call("enqueue", args)
        self.assertEqual(self.h.status()["jobs"]["queued"] + self.h.status()["jobs"]["running"] + self.h.status()["jobs"]["succeeded"], 0)
        for op, args in (("cancel", {}), ("retry", {"job_id": 5}), ("pause", {"cancel_running": "yes"}), ("job", {"job_id": "x" * 100}), ("resume", {"extra": 1})):
            with self.subTest(op=op), self.assertRaises(ExtensionError):
                self.h.call(op, args)

    def test_a_payload_cannot_smuggle_in_a_command_or_an_address(self):
        for payload in ({"text": "Hello there everyone, welcome.", "command": ["/bin/sh"]}, {"text": "Hello there everyone.", "base_url": "http://evil.example"},
                        {"tags": "ambient pads", "argv": ["x"]}):
            role = "song" if "tags" in payload else "host_break"
            with self.subTest(payload=payload), self.assertRaisesRegex(ExtensionError, "Unknown field"):
                self.h.call("enqueue", {"role": role, "payload": payload})

    def test_operation_answers_are_plain_json(self):
        reply = self.h.call("enqueue", {"role": "host_break", "payload": {"text": "Hello there everyone, welcome."}, "priority": 60, "ttl_s": 3600})
        self.assertEqual((reply["ok"], reply["operation"], reply["created"], reply["message"]), (True, "generation.enqueue", True, "Queued."))
        job = reply["job"]
        self.assertEqual((job["role"], job["priority"], job["state_label"] in ("Waiting", "Being made now", "Done")), ("host_break", 60, True))
        self.assertTrue(job["expires"].endswith("+00:00"))
        self.assertTrue(job["created"].endswith("+00:00"))
        self.assertRegex(job["id"], r"^[0-9a-f]{32}$")
        json.dumps(reply)
        done = self.h.wait_state(job["id"], "succeeded")
        self.assertEqual(done["payload"]["speaker"], 0)
        self.assertEqual([a["outcome"] for a in done["attempt_log"]], ["ok"])

    def test_routes_list_filter_page_and_look_up_jobs(self):
        ids = []
        for n in range(5):
            ids.append(self.h.enqueue("host_break", {"text": f"Script number {n} for the listing test."})["id"])
        self.h.enqueue("song", {"title": "Listed Song"}) if False else None
        for job_id in ids:
            self.h.wait_state(job_id, "succeeded")
        listing = self.h.route("GET", "/jobs")
        self.assertEqual(listing.status, 200)
        body = listing.body
        self.assertEqual(len(body["jobs"]), 5)
        self.assertEqual(body["jobs"][0]["id"], ids[-1], "newest first")
        self.assertEqual(body["counts"]["succeeded"], 5)
        self.assertIsNone(body["next_before"])
        self.assertNotIn("payload", body["jobs"][0], "the list stays small; ask for one job to see everything")
        page = self.h.route("GET", "/jobs", {"limit": ["2"]}).body
        self.assertEqual((len(page["jobs"]), page["next_before"] is not None), (2, True))
        older = self.h.route("GET", "/jobs", {"limit": ["10"], "before": [str(page["next_before"])]}).body["jobs"]
        self.assertEqual([j["id"] for j in older], ids[2::-1])
        self.assertEqual(len(self.h.route("GET", "/jobs", {"state": ["succeeded"], "role": ["host_break"]}).body["jobs"]), 5)
        self.assertEqual(self.h.route("GET", "/jobs", {"state": ["failed"]}).body["jobs"], [])
        one = self.h.route("GET", "/jobs", {"id": [ids[0]]}).body["job"]
        self.assertEqual((one["id"], one["state"], one["provenance"]["role"]), (ids[0], "succeeded", "host_break"))
        self.assertIn("params", one["provenance"])
        for query, status in (({"state": ["bogus"]}, 400), ({"role": ["bogus"]}, 400), ({"limit": ["many"]}, 400), ({"before": ["x"]}, 400),
                              ({"id": ["0" * 32]}, 404), ({"track_id": ["nope"]}, 404)):
            with self.subTest(query=query):
                self.assertEqual(self.h.route("GET", "/jobs", query).status, status)

    def test_workers_and_buffer_routes(self):
        self.assertTrue(wait_for(lambda: self.h.route("GET", "/workers").body["workers"][0]["state"] == "ready"))
        workers = self.h.route("GET", "/workers").body
        w = workers["workers"][0]
        self.assertEqual((w["id"], w["kind"], w["resource"], w["ready"]), ("w1", "controlled", "gpu0", True))
        self.assertEqual(sorted(w["roles"]), sorted(ROLES))
        self.assertTrue(workers["capabilities"]["music_generation"])
        self.assertEqual(set(workers["proven"]), {"music_generation", "speech_generation", "roles"})
        for key in ("state_label", "message", "hint", "evidence", "evidence_label", "last_checked"):
            self.assertIn(key, w)
        buffer = self.h.route("GET", "/buffer").body
        self.assertEqual(set(buffer["roles"]), set(ROLES))
        self.assertEqual(buffer["roles"]["song"]["unit"], "seconds")
        self.assertEqual(buffer["roles"]["id"]["unit"], "items")
        self.assertEqual((buffer["urgent_priority"], buffer["paused"]), (80, False))

    def test_probe_validates_its_body(self):
        for body in ({"full": "yes"}, {"surprise": 1}, {"full": 1}, [1, 2], "full", 5):
            with self.subTest(body=body):
                self.assertEqual(self.h.route("POST", "/probe", body=body).status, 400)
        reply = self.h.route("POST", "/probe", body={"full": True})
        self.assertEqual(reply.status, 200)
        self.assertTrue(reply.body["full_check_queued"])
        self.assertEqual(self.h.route("POST", "/probe").status, 200, "an empty body is fine")

    def test_the_answer_to_a_full_test_says_what_was_started_and_what_has_been_proven(self):
        body = self.h.route("POST", "/probe", body={"full": True}).body
        self.assertTrue(body["full_check_queued"])
        self.assertIn("short test clip", body["full_check_message"])
        self.assertEqual(set(body["proven"]), {"music_generation", "speech_generation", "roles"})
        self.assertNotIn("full_check_message", self.h.route("POST", "/probe", body={}).body)

    def test_a_full_test_asked_for_while_paused_says_it_waits_for_the_resume(self):
        self.h.call("pause")
        body = self.h.route("POST", "/probe", body={"full": True}).body
        self.assertTrue(body["full_check_queued"])
        self.assertIn("resumed", body["full_check_message"])

    def test_a_list_position_that_cannot_exist_is_a_plain_refusal_not_a_server_error(self):
        for before in ("99999999999999999999", "-5", str(2 ** 63)):
            with self.subTest(before=before):
                reply = self.h.route("GET", "/jobs", {"before": [before]})
                self.assertEqual(reply.status, 400)
                self.assertIn("before", reply.body["error"])
        self.assertEqual(self.h.route("GET", "/jobs", {"limit": ["99999999999999999999"]}).status, 200, "a huge limit is simply capped")

    def test_enormous_numbers_in_a_job_are_refused_in_plain_words(self):
        with self.assertRaisesRegex(ExtensionError, "seed must be a whole number"):
            self.h.call("enqueue", {"role": "host_break", "payload": {"text": "Hello there everyone, welcome.", "seed": 10 ** 400}})

    def test_a_settings_file_with_an_enormous_number_still_starts_and_names_the_problem(self):
        h = fakes.Harness({"workers": [fakes.worker_cfg("w1")], "limits": {"max_queued": 10 ** 400}})
        self.addCleanup(h.close)
        self.assertEqual(h.host.status()["generation"]["state"], "running")
        self.assertIn("max_queued", " ".join(h.status()["config"]["problems"]))

    def two_makers(self):
        return {"workers": [fakes.worker_cfg("w1"), fakes.worker_cfg("w2", roles=["song"])]}

    def test_new_settings_that_will_not_load_leave_the_old_ones_running(self):
        (self.h.state_dir / "config.json").write_text(json.dumps(self.two_makers()))
        with patch("myradio.generation.extension.Engine", side_effect=RuntimeError("no memory")), self.assertLogs("myradio.generation", level="ERROR"):
            reply = self.h.route("POST", "/probe", body={})
        self.assertEqual(reply.status, 200)
        self.assertEqual((reply.body["reloaded"], reply.body["settings_changed"]), (False, True))
        self.assertIn("could not be loaded", reply.body["note"])
        self.assertEqual([w["id"] for w in self.h.status()["workers"]], ["w1"], "the old settings never stopped")
        self.assertEqual(self.h.wait_state(self.h.enqueue("host_break")["id"], "succeeded")["state"], "succeeded")

    def test_new_settings_that_will_not_start_bring_the_old_ones_back(self):
        (self.h.state_dir / "config.json").write_text(json.dumps(self.two_makers()))
        original, calls = Engine.start, []

        def start(engine):
            calls.append(1)
            if len(calls) == 1:  # the new settings' engine
                raise RuntimeError("no thread available")
            return original(engine)

        with patch.object(Engine, "start", start), self.assertLogs("myradio.generation", level="ERROR"):
            reply = self.h.route("POST", "/probe", body={})
        self.assertEqual(reply.status, 200)
        self.assertFalse(reply.body["reloaded"])
        self.assertIn("in use again", reply.body["note"])
        self.assertEqual([w["id"] for w in reply.body["workers"]], ["w1"])
        self.assertEqual(self.h.wait_state(self.h.enqueue("host_break")["id"], "succeeded")["state"], "succeeded", "enqueued work really runs")

    def test_when_nothing_can_be_started_the_service_says_it_is_not_running_instead_of_pretending(self):
        (self.h.state_dir / "config.json").write_text(json.dumps(self.two_makers()))
        with patch.object(Engine, "start", side_effect=RuntimeError("no thread available")), self.assertLogs("myradio.generation", level="ERROR"):
            reply = self.h.route("POST", "/probe", body={})
        self.assertEqual(reply.status, 503)
        self.assertIn("Restart myradio", reply.body["error"])
        self.assertEqual(self.h.ext.status(), {"enabled": False})
        with self.assertRaisesRegex(ExtensionError, "not running"):
            self.h.call("pause")

    def test_the_files_to_back_up_are_the_history_and_the_settings_but_never_secrets(self):
        (self.h.state_dir / "voice.token").write_text("super-secret")
        (self.h.state_dir / "work").mkdir(exist_ok=True)
        self.h.wait_state(self.h.enqueue()["id"], "succeeded")
        files = sorted(p.name for p in self.h.host.backup_files()["generation"])
        self.assertIn("generation.sqlite3", files)
        self.assertIn("config.json", files)
        self.assertNotIn("voice.token", files)
        self.assertEqual([f for f in files if f.endswith(".tmp") or f == "work"], [])

    def test_secrets_never_appear_in_anything_the_extension_shows(self):
        secret = "S3CRET-TOKEN-VALUE"
        server = fakes.FakeSpeechServer()
        self.addCleanup(server.close)
        (self.h.state_dir / "voice.token").write_text(secret)
        cfg = {"workers": [{"id": "voice", "kind": "drama_http", "base_url": server.url, "auth_token_file": "voice.token"}],
               "buffer": {r: {"low": 40, "high": 90} for r in ROLES[1:]}}
        (self.h.state_dir / "config.json").write_text(json.dumps(cfg))
        self.h.route("POST", "/probe", body={})
        job = self.h.enqueue("host_break")
        self.h.wait_state(job["id"], "succeeded", "failed")
        shown = json.dumps([self.h.host.status(), self.h.route("GET", "/jobs").body, self.h.route("GET", "/workers").body,
                            self.h.route("GET", "/jobs", {"id": [job["id"]]}).body, self.h.host.checks()])
        self.assertNotIn(secret, shown)
        self.assertEqual(server.speech_requests()[-1]["headers"]["authorization"], f"Bearer {secret}")

    def test_the_doctor_checks_say_what_is_wrong_in_plain_words(self):
        self.assertTrue(wait_for(lambda: self.h.status()["workers"][0]["state"] == "ready"))
        checks = {c["id"]: c for c in self.h.host.checks()}
        self.assertEqual(checks["generation.config"]["status"], "ok")
        self.assertEqual(checks["generation.music"]["status"], "ok")
        self.assertEqual(checks["generation.voice"]["status"], "ok")
        self.assertEqual(checks["generation.disk"]["status"], "ok")
        self.assertEqual(checks["generation.audio"]["status"], "ok")
        for check in checks.values():
            self.assertIn(check["status"], ("ok", "warn", "fail", "unknown"))
            self.assertNotRegex(check["detail"] + check["hint"], r"Traceback|PCM|GGUF|SSRF|errno")
        fakes.CONTROLS["w1"].probe = fakes.ProbeResult(False, "missing_files", "Some files the maker needs were not found: weights.gguf.", "Check the paths in your settings.")
        self.h.route("POST", "/probe")
        checks = {c["id"]: c for c in self.h.host.checks()}
        self.assertEqual(checks["generation.music"]["status"], "fail")
        self.assertIn("weights.gguf", checks["generation.music"]["detail"])
        self.assertEqual(checks["generation.music"]["hint"], "Check the paths in your settings.")

    def test_problems_in_the_settings_are_listed_without_stopping_the_good_part(self):
        h = fakes.Harness({"workers": [fakes.worker_cfg("w1", roles=["host_break"]), {"id": "broken", "kind": "yue2_cli", "command": "run it please"}],
                           "workrs": 1})
        self.addCleanup(h.close)
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["generation.config"]["status"], "warn")
        self.assertIn("workrs", checks["generation.config"]["detail"])
        self.assertEqual(checks["generation.music"]["status"], "fail")
        self.assertIn("list of separate arguments", checks["generation.music"]["detail"])
        statuses = {w["id"]: w for w in h.status()["workers"]}
        self.assertEqual(statuses["broken"]["state"], "misconfigured")
        self.assertEqual(statuses["w1"]["state"] in ("ready", "checking"), True)
        with self.assertRaisesRegex(ExtensionError, "problem in its settings"):
            h.call("enqueue", {"role": "song", "payload": {"tags": "ambient pads"}})
        self.assertTrue(h.enqueue("host_break")["id"])


class StationCase(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        fakes.reset_controls()
        self.real_audio = fakes.use_audio()
        self.addCleanup(fakes.release_audio)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.data = self.dir / "data"
        self.speech = fakes.FakeSpeechServer("ok24_long")
        self.addCleanup(self.speech.close)
        self.notes = self.dir / "notes"
        self.notes.mkdir()
        env = patch.dict(os.environ, {ENV_VAR: "1"})
        env.start()
        self.addCleanup(env.stop)

    def station(self, config):
        state = self.data / "ext" / "generation"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        (state / "config.json").write_text(json.dumps(config))
        station = Station(self.data, start_worker=False, extensions=[GenerationExtension(probe_min_interval=0)])
        self.addCleanup(station.close)
        return station

    def full_config(self, **extra):
        script = fakes.write_script(self.dir, "ok")
        return {"artist": "E2E FM",
                "workers": [{"id": "voice", "kind": "drama_http", "base_url": self.speech.url, "request_timeout_s": 20},
                            {"id": "music", "kind": "yue2_cli", "timeout_s": 60,
                             "command": [sys.executable, script, "--out", "{output_file}", "--tags", "{tags}", "--seconds", "2.5", "--notes", str(self.notes)]}],
                "buffer": {r: {"low": 40, "high": 90} for r in ROLES[1:]}, **extra}

    def job(self, station, job_id):
        return station.action("generation.job", {"job_id": job_id})["job"]

    def wait_job(self, station, job_id, timeout=20):
        wait_for(lambda: self.job(station, job_id)["state"] in ("succeeded", "failed", "cancelled", "expired", "interrupted"), timeout)
        return self.job(station, job_id)

    def extension_status(self, station):
        return station.status()["extensions"]["generation"]


class EndToEndTests(StationCase):
    def test_real_workers_behind_fakes_make_audio_that_lands_in_the_library_with_the_right_kind(self):
        station = self.station(self.full_config())
        ext = lambda: self.extension_status(station)
        self.assertEqual(ext()["state"], "running")
        self.assertTrue(wait_for(lambda: ext()["status"]["workers"] and all(w["state"] in ("ready", "warming") for w in ext()["status"]["workers"])))
        jobs = {
            "song": station.action("generation.enqueue", {"role": "song", "payload": {"tags": "warm tape loop, 70 BPM", "duration_s": 5, "title": "Tape Loop"}})["job"],
            "host_break": station.action("generation.enqueue", {"role": "host_break", "payload": {"text": "Good evening, you are listening to the station."}})["job"],
            "id": station.action("generation.enqueue", {"role": "id", "payload": {"text": "Radio Four, the friendly signal."}})["job"],
            "spot": station.action("generation.enqueue", {"role": "spot", "payload": {"text": "This message comes from Maple Bakery, fresh bread daily."}})["job"],
            "update": station.action("generation.enqueue", {"role": "update", "payload": {"text": "Here is a short update about the garden club meeting."}})["job"],
        }
        done = {role: self.wait_job(station, job["id"]) for role, job in jobs.items()}
        for role, job in done.items():
            self.assertEqual(job["state"], "succeeded", (role, job["error"], job.get("detail")))
        library = {t["id"]: t for t in station.status()["library"]}
        expected = {"song": "music", "host_break": "speech", "id": "speech", "spot": "ad", "update": "speech"}
        for role, job in done.items():
            track = library[job["track_id"]]
            self.assertEqual((track["kind"], track["artist"]), (expected[role], "E2E FM"), role)
            self.assertGreater(track["duration"], 1.0)
            self.assertTrue(track["available"])
        self.assertEqual(library[done["song"]["track_id"]]["title"], "Tape Loop")
        self.assertEqual(done["song"]["provenance"]["worker"]["kind"], "yue2_cli")
        self.assertEqual(done["spot"]["provenance"]["worker"]["kind"], "drama_http")
        self.assertRegex(done["song"]["provenance"]["worker"]["hash"] or "0" * 64, r"^[0-9a-f]{64}$")
        status = ext()["status"]
        self.assertTrue(status["capabilities"]["music_generation"])
        self.assertTrue(status["capabilities"]["speech_generation"])
        self.assertEqual(status["capabilities"]["roles"], {r: True for r in ROLES})
        # The core station still reports its own (fixed) capability flags; the real ones live under the extension.
        json.dumps(station.status())
        self.assertEqual(len(self.speech.speech_requests()), 1 + 4, "one warm-up and one take for each of the four speech jobs")

    def test_a_clean_station_close_stops_everything_and_leaves_no_process_behind(self):
        hang = fakes.write_script(self.dir, "hang")
        config = {"workers": [{"id": "music", "kind": "yue2_cli", "timeout_s": 300, "kill_grace_s": 1,
                               "command": [sys.executable, hang, "--out", "{output_file}", "--notes", str(self.notes)]}]}
        station = self.station(config)
        job = station.action("generation.enqueue", {"role": "song", "payload": {"tags": "ambient pads", "duration_s": 5}})["job"]
        self.assertTrue(wait_for(lambda: (self.notes / "pid.txt").exists()))
        pid = int((self.notes / "pid.txt").read_text().split()[0])
        self.assertTrue(fakes.pid_alive(pid))
        began = time.monotonic()
        station.close()
        self.assertLess(time.monotonic() - began, 10)
        self.assertTrue(wait_for(lambda: not fakes.pid_alive(pid), 5), "the music program was stopped with the station")
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith(("myradio-gen", "myradio-ext-generation"))], [])
        import sqlite3
        conn = sqlite3.connect(self.data / "ext" / "generation" / "generation.sqlite3")
        row = conn.execute("SELECT state, interruptions FROM jobs WHERE id=?", (job["id"],)).fetchone()
        conn.close()
        self.assertEqual(row, ("queued", 0))

    def test_test_tones_import_as_labelled_fixtures_and_never_count_as_real(self):
        station = self.station({"workers": [{"id": "tones", "kind": "fixture", "max_seconds": 3}], "buffer": {r: {"low": 40, "high": 90} for r in ROLES[1:]}})
        song = station.action("generation.enqueue", {"role": "song", "payload": {"tags": "ambient pads", "duration_s": 5}})["job"]
        talk = station.action("generation.enqueue", {"role": "host_break", "payload": {"text": "Testing one two three four five six."}})["job"]
        for job in (song, talk):
            self.assertEqual(self.wait_job(station, job["id"])["state"], "succeeded")
        library = station.status()["library"]
        self.assertEqual(len(library), 2)
        for track in library:
            self.assertTrue(track["title"].startswith("[FIXTURE] "), track["title"])
            self.assertEqual(track["artist"], "FIXTURE - not real music")
        self.assertEqual(sorted(t["kind"] for t in library), ["music", "speech"])
        caps = self.extension_status(station)["status"]
        self.assertFalse(caps["capabilities"]["music_generation"])
        self.assertTrue(caps["fixture_active"])
        self.assertTrue(self.job(station, song["id"])["provenance"]["fixture"])

    def test_unsafe_test_settings_are_refused_when_the_switch_is_off(self):
        with patch.dict(os.environ, {ENV_VAR: "0"}):
            station = self.station({"workers": [{"id": "tones", "kind": "fixture"}]})
            status = self.extension_status(station)["status"]
            self.assertEqual(status["workers"][0]["state"], "misconfigured")
            self.assertIn("switched off", status["workers"][0]["message"])
            with self.assertRaisesRegex(StationError, "problem in its settings|No music maker|No voice maker"):
                station.action("generation.enqueue", {"role": "song", "payload": {"tags": "ambient pads"}})

    def test_a_voice_maker_that_is_down_is_reported_plainly_and_nothing_is_claimed(self):
        config = self.full_config(retry={"max_attempts": 1})
        self.speech.close()
        station = self.station(config)
        status = lambda: self.extension_status(station)["status"]
        voice = lambda: next(w for w in status()["workers"] if w["id"] == "voice")
        self.assertTrue(wait_for(lambda: voice()["state"] == "unreachable"))
        self.assertFalse(status()["capabilities"]["speech_generation"])
        self.assertIn("not answering", voice()["message"])
        self.assertEqual(voice()["hint"], "Start the voice maker, then choose Check again.")
        self.assertTrue(status()["capabilities"]["music_generation"] or status()["workers"][1]["state"] in ("ready", "checking"))
        job = station.action("generation.enqueue", {"role": "host_break", "payload": {"text": "Nobody is listening for this script."}})["job"]
        done = self.wait_job(station, job["id"])
        self.assertEqual((done["state"], done["error_code"]), ("failed", "worker_unreachable"))
        self.assertIn("did not answer", done["error"])
        self.assertEqual(done["hint"], "Check that the maker is running, then choose Try again.")
        self.assertEqual(station.status()["library"], [])
        checks = {c["id"]: c for c in station.extensions.checks()}
        self.assertEqual(checks["generation.voice"]["status"], "fail")

    def test_closing_the_station_abandons_a_hung_speech_request_quickly(self):
        self.speech.mode = "hang"
        station = self.station(self.full_config())
        job = station.action("generation.enqueue", {"role": "host_break", "payload": {"text": "This request never gets an answer from the server."}})["job"]
        self.assertTrue(wait_for(lambda: self.speech.speech_requests(), 10))
        began = time.monotonic()
        station.close()
        self.assertLess(time.monotonic() - began, 8)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith(("myradio-gen", "myradio-ext-generation"))], [])
        import sqlite3
        conn = sqlite3.connect(self.data / "ext" / "generation" / "generation.sqlite3", isolation_level=None)
        row = conn.execute("SELECT state, interruptions FROM jobs WHERE id=?", (job["id"],)).fetchone()
        conn.close()
        self.assertEqual(row, ("queued", 0))
        self.speech.stop_event.set()

    def test_a_test_tone_maker_stops_working_the_moment_the_switch_is_turned_off(self):
        station = self.station({"workers": [{"id": "tones", "kind": "fixture", "max_seconds": 3}], "retry": {"max_attempts": 3}})
        first = station.action("generation.enqueue", {"role": "song", "payload": {"tags": "ambient pads", "duration_s": 5}})["job"]
        self.assertEqual(self.wait_job(station, first["id"])["state"], "succeeded")
        with patch.dict(os.environ, {ENV_VAR: "0"}):
            second = station.action("generation.enqueue", {"role": "song", "payload": {"tags": "ambient pads again", "duration_s": 5}})["job"]
            done = self.wait_job(station, second["id"])
        self.assertEqual((done["state"], done["error_code"], done["attempts"]), ("failed", "worker_blocked", 1))
        self.assertIn("only for tests", done["error"])
        self.assertEqual(len(station.status()["library"]), 1)

    def test_a_worker_that_hangs_never_delays_the_station_clock(self):
        self.speech.mode = "hang"
        station = self.station(self.full_config())
        track = None
        import io, wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            out.writeframes(bytes(44100 * 4 * 3))
        track = station.import_audio(buf.getvalue(), "Own song", "me", "music")
        station.action("transport.start")
        station.action("generation.enqueue", {"role": "host_break", "payload": {"text": "This request never gets an answer from the server."}})
        self.assertTrue(wait_for(lambda: self.speech.speech_requests(), 10))
        worst = 0.0
        for _ in range(60):
            began = time.monotonic()
            station.tick()
            station.status()
            worst = max(worst, time.monotonic() - began)
        self.assertLess(worst, 1.0, "a hung speech server must not slow the clock")
        self.assertFalse(station.faulted)
        self.speech.stop_event.set()

    def test_making_audio_while_playing_does_not_stall_the_clock(self):
        station = self.station(self.full_config())
        import io, wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            out.writeframes(bytes(44100 * 4 * 30))
        station.import_audio(buf.getvalue(), "Own song", "me", "music")
        station.action("transport.start")
        jobs = [station.action("generation.enqueue", {"role": "host_break", "payload": {"text": f"Script number {n} made while the station plays."}})["job"] for n in range(4)]
        worst, ticks = 0.0, 0
        while any(self.job(station, j["id"])["state"] not in ("succeeded", "failed") for j in jobs):
            began = time.monotonic()
            station.tick()
            worst = max(worst, time.monotonic() - began)
            ticks += 1
            time.sleep(0.01)
        self.assertGreater(ticks, 5)
        self.assertLess(worst, 1.0)
        self.assertTrue(all(self.job(station, j["id"])["state"] == "succeeded" for j in jobs))
        self.assertEqual(len([t for t in station.status()["library"] if t["kind"] == "speech"]), 4)

    def test_generated_tracks_are_noticed_when_they_start_playing(self):
        station = self.station(self.full_config(buffer={"host_break": {"low": 1, "high": 2}}))
        job = station.action("generation.enqueue", {"role": "host_break", "payload": {"text": "Good evening, you are listening to the station."}})["job"]
        done = self.wait_job(station, job["id"])
        status = lambda: self.extension_status(station)["status"]["buffer"]["host_break"]
        self.assertTrue(wait_for(lambda: status()["ready"] == 1))
        station.action("queue.add", {"track_id": done["track_id"]})
        station.action("transport.start")
        station.tick()
        self.assertTrue(wait_for(lambda: status()["ready"] == 0), "the track left the ready pool when it began to play")


class FinishingCostTests(unittest.TestCase):
    """Finishing runs inside the station process (the audio tools are pure Python). A ticking clock must stay on time while minutes of
    audio are finished on a slot. The result is measured, not assumed: gaps of 100 to 130 ms were seen on a 100 ms ticker."""

    def setUp(self):
        fakes.arm_watchdog(self)
        fakes.reset_controls()
        self.real_audio = fakes.use_audio()
        self.addCleanup(fakes.release_audio)

    def finish_while_ticking(self, h, seconds):
        """Finish ``seconds`` of audio on a slot while a 100 ms ticker runs. Returns (the finished job, the longest time between two ticks)."""
        gaps, stop = [], threading.Event()

        def tick():
            last = time.monotonic()
            while not stop.is_set():
                time.sleep(0.1)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = threading.Thread(target=tick, daemon=True)
        ticker.start()
        try:
            job = h.enqueue("song", {"duration_s": seconds, "title": "Long"})
            done = h.wait_state(job["id"], "succeeded", "failed", timeout=180)
        finally:
            stop.set()
            ticker.join(5)
        self.assertGreater(len(gaps), 5)
        return done, max(gaps)

    def test_finishing_minutes_of_audio_does_not_stall_a_ticking_clock(self):
        audio = load_audio()
        seconds = 20 if getattr(audio, "IS_STAND_IN", False) else 120  # the stand-in does the same work far more slowly
        one = audio.read_wav(fakes.tone_wav(1.0, fade_ms=100))
        h = fakes.Harness({"workers": [fakes.worker_cfg("w1", roles=["song"])]})
        self.addCleanup(h.close)
        fakes.CONTROLS.setdefault("w1", fakes.Control()).prebuilt = audio.Pcm(frames=one.frames * seconds, rate=one.rate, channels=one.channels)
        worst = []
        for _ in range(2):  # one late tick can be the operating system on a busy computer; two runs in a row are the audio tools
            done, longest = self.finish_while_ticking(h, seconds)
            self.assertEqual(done["state"], "succeeded", done.get("error"))
            self.assertGreater(done["provenance"]["timings"]["finish_s"], 0.3, "finishing took real time, so the measurement means something")
            worst.append(longest)
            if longest < 0.6:
                break
        self.assertLess(worst[-1], 0.6, f"the clock was late by {worst[-1] - 0.1:.2f} s while {seconds} s of audio was finished (runs: {worst})")


class HttpTests(StationCase):
    def setUp(self):
        super().setUp()
        self.web = self.dir / "web"
        (self.web / "panels").mkdir(parents=True)
        patcher = patch.object(server_module, "WEB", self.web)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.station_obj = self.station(self.full_config())
        with patch.dict("os.environ", {}, clear=True):
            self.server = Server(("127.0.0.1", 0), self.station_obj, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-Myradio-Token": TOKEN} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def test_the_screen_and_agent_surfaces_are_reachable_only_with_the_token(self):
        for method, path in (("GET", "/api/x/generation/jobs"), ("GET", "/api/x/generation/workers"), ("GET", "/api/x/generation/buffer"), ("POST", "/api/x/generation/probe")):
            self.assertEqual(self.call(method, path, token=False)[0], 401, path)
        status, catalog = self.call("GET", "/api/operations")
        self.assertEqual(status, 200)
        self.assertIn("generation.enqueue", {c["name"] for c in catalog})
        status, reply = self.call("POST", "/api/action", {"operation": "generation.enqueue",
                                                          "arguments": {"role": "id", "payload": {"text": "Radio Four, the friendly signal."}}})
        self.assertEqual((status, reply["created"]), (200, True))
        job_id = reply["job"]["id"]
        self.assertTrue(wait_for(lambda: self.call("GET", f"/api/x/generation/jobs?id={job_id}")[1]["job"]["state"] == "succeeded", 20))
        status, listing = self.call("GET", "/api/x/generation/jobs?state=succeeded&limit=5")
        self.assertEqual((status, [j["id"] for j in listing["jobs"]]), (200, [job_id]))
        self.assertEqual(self.call("GET", "/api/x/generation/jobs?state=nonsense")[0], 400)
        self.assertEqual(self.call("GET", "/api/x/generation/jobs?id=" + "0" * 32)[0], 404)
        status, workers = self.call("GET", "/api/x/generation/workers")
        self.assertEqual(status, 200)
        self.assertEqual({w["id"] for w in workers["workers"]}, {"voice", "music"})
        self.assertEqual(self.call("POST", "/api/x/generation/probe", {"full": "yes"})[0], 400)
        status, probe = self.call("POST", "/api/x/generation/probe", {})
        self.assertEqual(status, 200)
        self.assertIn("workers", probe)
        status, plain = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("generation", plain["extensions"])


if __name__ == "__main__":
    unittest.main()
