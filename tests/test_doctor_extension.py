"""The doctor as a station extension, wired into the real Station and, once, the real HTTP server.

The checks themselves are canned in most tests (fast and exact); one test runs the real checks through the
real route. The only socket opened is the test HTTP server on 127.0.0.1 with port 0.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from myradio.doctor.checks import CHECKS, CheckSpec
from myradio.doctor.extension import CACHE_SECONDS, REFRESH_INTERVAL, DoctorExtension, build
from myradio.doctor.report import ReportRunner
from myradio.doctor.tools import run_tool
from myradio.extensions import Extension, ExtensionError, Request, StationFacade, builtin_extensions
from myradio.server import Server
from myradio.station import Station, StationError

from contextlib import ExitStack

from test_doctor_fixtures import (HEARTBEAT, Gate, alive_after, canned_spec, doctor_threads, fast_specs, guard_ports, hermetic_tools, make_program,
                                  wait_for_files)

TOKEN = "d" * 40
URL = "/api/x/doctor/report"
KEYS = ["id", "title", "status", "detail", "hint", "fix"]


class Ticker:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CountingRunner(ReportRunner):
    def __init__(self, specs, **kwargs):
        super().__init__(specs, **kwargs)
        self.runs, self.settings, self.extra_seen, self.closes = 0, [], [], 0

    def run(self, settings=None, **kwargs):
        self.runs += 1
        self.settings.append(settings)
        self.extra_seen.append(kwargs.get("extra"))
        return super().run(settings, **kwargs)

    def close(self, wait=1.0):
        self.closes += 1
        return super().close(wait)


class Neighbour(Extension):
    """Another extension that contributes checks."""

    name = "neighbour"

    def __init__(self, items=None):
        self.items = items if items is not None else [
            {"id": "disk", "title": "Neighbour disk", "status": "warn", "detail": "Low on space.", "hint": "Free some."},
            {"id": "x"}, "junk", {"id": "bad", "title": "Bad", "status": "purple"}]

    def checks(self):
        return self.items


class Hanging(Extension):
    name = "hanging"

    def __init__(self, gate):
        self.gate = gate

    def checks(self):
        self.gate.event.wait(30)
        return []


class Broken(Extension):
    name = "broken"

    def start(self, ctx):
        raise RuntimeError("boom")


class StationCase(unittest.TestCase):
    def setUp(self):
        guard_ports(self)  # nothing here may bind a fixed port: the station's own port, 8765, is not ours to touch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ticker = Ticker()
        self.runner = CountingRunner(fast_specs("ok", "warn"))
        self.doctor = DoctorExtension(monotonic=self.ticker, runner=self.runner, budget=2.0)

    def boot(self, *others, doctor=None):
        self.station = Station(Path(self.temp.name) / "data", start_worker=False, extensions=[doctor or self.doctor, *others])
        self.addCleanup(self.station.close)
        return self.station

    def get(self, query=None, method="GET"):
        match = self.station.extensions.find_route(method, URL)
        self.assertIsNotNone(match, "the doctor route must be registered")
        return self.station.extensions.dispatch_route(match, Request(method, URL, query or {}, None, b""))

    def report(self, query=None):
        reply = self.get(query)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.body


class RegistrationTests(StationCase):
    def test_it_registers_cleanly_and_offers_one_route_one_operation_and_nothing_else(self):
        self.boot()
        self.assertEqual(self.station.status()["extensions"]["doctor"]["state"], "running")
        self.assertIsNone(self.station.status()["extensions"]["doctor"]["error"])
        self.assertIsNotNone(self.station.extensions.find_route("GET", URL))
        self.assertIsNone(self.station.extensions.find_route("POST", URL))
        self.assertIsNone(self.station.extensions.find_route("GET", "/api/x/doctor/other"))
        self.assertEqual([p for p in self.station.extensions.panels() if p["extension"] == "doctor"], [])
        self.assertEqual(self.doctor.persistent_files(), ())
        self.assertEqual(self.station.extensions.backup_files()["doctor"], [])

    def test_it_listens_to_no_events_so_it_has_no_mailbox_thread(self):
        self.assertIs(type(self.doctor).on_event, Extension.on_event)
        self.boot()
        self.assertNotIn("events_dropped", self.station.status()["extensions"]["doctor"])
        self.assertEqual([t for t in threading.enumerate() if t.name == "myradio-ext-doctor"], [])

    def test_the_operation_is_described_for_the_operator_and_agents(self):
        self.boot()
        entry = {o["name"]: o for o in self.station.operations()}["doctor.report"]
        self.assertEqual(entry["required"], [])
        self.assertEqual(list(entry["arguments"]), ["refresh"])
        self.assertEqual(entry["arguments"]["refresh"]["type"], "boolean")
        self.assertIn("Read-only", entry["description"])
        self.assertLessEqual(len(entry["description"]), 400)

    def test_the_built_in_loader_finds_it(self):
        found = {e.name: e for e in builtin_extensions()}
        self.assertIsInstance(found["doctor"], DoctorExtension)
        self.assertIsInstance(build(), DoctorExtension)
        self.assertEqual(DoctorExtension.name, "doctor")

    def test_its_own_checks_list_is_empty_so_nothing_is_counted_twice(self):
        self.boot(Neighbour())
        self.assertEqual(self.doctor.checks(), [], "an empty list, as the contract says (not an empty tuple)")
        self.assertEqual(list(self.doctor.checks()), [])
        ids = [c["id"] for c in self.station.extensions.checks()]
        self.assertEqual(ids, ["neighbour.disk"])
        self.assertFalse([i for i in ids if i.startswith("doctor.")])
        self.assertEqual([c["id"] for c in StationFacade(self.station).extension_checks()], ["neighbour.disk"])

    def test_nothing_is_checked_while_the_station_is_still_starting(self):
        with patch.object(StationFacade, "extension_checks", autospec=True, return_value=[]) as gather:
            self.boot(Neighbour())
            self.assertEqual(gather.call_count, 0, "asking now would report the other extensions as not running yet")
            self.assertEqual(self.runner.runs, 0)
            self.report()
            self.assertEqual(gather.call_count, 1)

    def test_the_real_runner_is_built_when_none_is_given(self):
        ext = DoctorExtension()
        self.boot(doctor=ext)
        self.assertIsInstance(ext._runner, ReportRunner)


class ReportTests(StationCase):
    def test_the_route_returns_the_report_with_our_checks_first_and_other_extensions_after(self):
        self.boot(Neighbour(), Broken())
        body = self.report()
        self.assertEqual([c["id"] for c in body["checks"]], ["a", "b", "neighbour.disk", "broken.start"])
        self.assertEqual(body["overall"], "fail")
        self.assertEqual(body["counts"], {"ok": 1, "warn": 2, "fail": 1, "unknown": 0})
        for item in body["checks"]:
            self.assertEqual(list(item), KEYS)
        self.assertEqual(body["checks"][3]["status"], "fail")
        self.assertEqual((body["cached"], body["age_seconds"]), (False, 0.0))
        json.dumps(body)

    def test_the_station_folder_comes_from_the_station_never_from_the_request(self):
        self.boot()
        self.report({"data_dir": ["/etc"], "port": ["22"], "path": ["/"], "refresh": ["1"]})
        settings = self.runner.settings[-1]
        self.assertEqual(settings.data_dir, str(self.station.directory))
        self.assertTrue(settings.in_service)
        self.assertEqual(settings.port, 8765)

    def test_the_answer_is_kept_for_thirty_seconds(self):
        self.boot()
        first = self.report()
        self.ticker.now += CACHE_SECONDS - 0.1
        second = self.report()
        self.assertEqual((first["cached"], second["cached"], self.runner.runs), (False, True, 1))
        self.assertEqual(second["age_seconds"], 29.9)
        self.assertEqual(second["generated_at"], first["generated_at"])
        self.ticker.now += 0.1
        third = self.report()
        self.assertEqual((third["cached"], third["age_seconds"], self.runner.runs), (False, 0.0, 2))

    def test_asking_for_a_fresh_answer_is_allowed_only_every_five_seconds(self):
        self.boot()
        self.report()
        self.ticker.now += REFRESH_INTERVAL - 0.1
        self.assertTrue(self.report({"refresh": ["1"]})["cached"])
        self.assertEqual(self.runner.runs, 1)
        self.ticker.now += 0.1
        again = self.report({"refresh": ["1"]})
        self.assertEqual((again["cached"], self.runner.runs), (False, 2))
        self.ticker.now += 1
        self.assertTrue(self.report({"refresh": ["1"]})["cached"], "a refresh right after a refresh is served from the cache")
        self.assertEqual(self.runner.runs, 2)

    def test_only_clear_yes_values_count_as_a_refresh(self):
        self.boot()

        def runs_after_asking(query):
            self.ticker.now += CACHE_SECONDS + 1  # let the stored answer expire, then make a fresh one
            self.report()
            self.ticker.now += REFRESH_INTERVAL  # old enough that a refresh would be allowed
            before = self.runner.runs
            self.report(query)
            return self.runner.runs - before

        for value in ("1", "true", "TRUE", "yes", "on", " 1 "):
            with self.subTest(value=value):
                self.assertEqual(runs_after_asking({"refresh": [value]}), 1)
        for value in ("0", "false", "", "banana", "2", "no"):
            with self.subTest(value=value):
                self.assertEqual(runs_after_asking({"refresh": [value]}), 0)
        for odd in ({"refresh": []}, {"refresh": None}, {"other": ["1"]}, {}):
            with self.subTest(query=odd):
                self.assertEqual(runs_after_asking(odd), 0)

    def test_a_request_with_a_strange_query_is_just_a_cached_request(self):
        self.boot()
        self.report()
        for query in (None, "refresh=1", 5, object()):
            with self.subTest(query=repr(query)[:20]):
                reply = self.doctor._route_report(self.doctor._ctx, Request("GET", URL, query, None, b""))
                self.assertTrue(reply["cached"])
        self.assertEqual(self.runner.runs, 1)

    def test_a_clock_that_goes_backwards_does_not_freeze_the_answer(self):
        self.boot()
        self.report()
        self.ticker.now -= 500
        self.assertFalse(self.report()["cached"])
        self.assertEqual(self.runner.runs, 2)

    def test_many_requests_at_once_run_the_checks_once(self):
        gate = Gate(self)
        slow = CountingRunner((canned_spec("a"),))
        doctor = DoctorExtension(monotonic=self.ticker, runner=slow, budget=5)
        self.boot(Hanging(gate), doctor=doctor)
        answers, errors = [], []

        def ask():
            try:
                answers.append(self.report())
            except BaseException as exc:  # pragma: no cover - reported by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        self.assertEqual(slow.runs, 1, "the other seven must wait for the one report, not start their own")
        gate.event.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(answers), 8)
        self.assertEqual(slow.runs, 1)
        self.assertEqual(sum(1 for a in answers if not a["cached"]), 1)
        self.assertEqual(len({a["generated_at"] for a in answers}), 1)

    def test_a_hung_neighbour_cannot_hold_the_report_hostage(self):
        gate = Gate(self)
        doctor = DoctorExtension(monotonic=self.ticker, runner=CountingRunner(fast_specs("ok")), budget=0.4)
        self.boot(Hanging(gate), doctor=doctor)
        started = time.monotonic()
        body = self.report()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(body["partial"])
        row = body["checks"][-1]
        self.assertEqual((row["id"], row["status"]), ("extensions", "unknown"))
        self.assertTrue(row["hint"])

    def test_a_gatherer_that_fails_gives_one_unknown_row(self):
        self.boot()
        with patch.object(StationFacade, "extension_checks", side_effect=RuntimeError("secret")):
            body = self.report({"refresh": ["1"]})
        self.assertEqual(body["checks"][-1]["id"], "extensions")
        self.assertNotIn("secret", json.dumps(body))

    def test_a_host_that_cannot_list_other_checks_still_gives_ours(self):
        self.boot()
        self.doctor._ctx = type("Ctx", (), {"directory": self.station.directory, "station": object(), "clock": time.time})()
        body = self.doctor.report()
        self.assertEqual([c["id"] for c in body["checks"]], ["a", "b"])
        self.assertIsNone(self.runner.extra_seen[-1])

    def test_a_runner_that_blows_up_gives_a_plain_error_and_the_next_request_still_works(self):
        class Explodes(ReportRunner):
            def run(self, settings=None, **kwargs):
                raise RuntimeError("secret internals")

        doctor = DoctorExtension(monotonic=self.ticker, runner=Explodes(fast_specs("ok")))
        self.boot(doctor=doctor)
        reply = self.get()
        self.assertEqual(reply.status, 400)
        self.assertEqual(list(reply.body), ["error"])
        self.assertNotIn("secret", json.dumps(reply.body))
        self.assertIn("Try again", reply.body["error"])
        with self.assertRaises(StationError):
            self.station.action("doctor.report", {})

    def test_it_never_exposes_what_is_in_the_station_folder(self):
        self.boot()
        (self.station.directory / "control.token").write_text("secret-launch-code-123")
        (self.station.directory / "agent.json").write_text('{"api_key": "sk-secret"}')
        text = json.dumps(self.report({"refresh": ["1"]}))
        self.assertNotIn("secret-launch-code", text)
        self.assertNotIn("sk-secret", text)

    def test_the_real_checks_run_inside_the_station_and_skip_the_port_test(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        hermetic_tools(stack, self.temp.name)  # the only FFmpeg it can find is a harmless fake
        doctor = DoctorExtension(budget=20)
        self.boot(doctor=doctor)
        body = self.report()
        self.assertEqual([c["id"] for c in body["checks"]], [spec.id for spec in CHECKS])
        port = {c["id"]: c for c in body["checks"]}["port"]
        self.assertEqual(port["status"], "ok")
        self.assertIn("skipped", port["detail"])
        folder = {c["id"]: c for c in body["checks"]}["data-folder"]
        self.assertEqual(folder["status"], "ok", folder)
        self.assertFalse(body["partial"])


class StatusTests(StationCase):
    def test_status_says_nothing_has_been_checked_until_the_first_report(self):
        self.boot()
        self.assertEqual(self.doctor.status(), {"checked": False})
        self.assertEqual(self.station.status()["extensions"]["doctor"]["status"], {"checked": False})
        self.assertEqual(self.runner.runs, 0, "status must never run a check")

    def test_status_afterwards_is_a_small_cached_summary(self):
        self.boot(Neighbour())
        body = self.report()
        status = self.station.status()["extensions"]["doctor"]["status"]
        self.assertEqual(list(status), ["checked", "overall", "summary", "counts", "checked_at", "attention"])
        self.assertEqual((status["checked"], status["overall"], status["checked_at"]), (True, "warn", body["generated_at"]))
        self.assertEqual(status["attention"], ["b", "neighbour.disk"])
        self.assertEqual(status["counts"], {"ok": 1, "warn": 2, "fail": 0, "unknown": 0})
        self.assertLess(len(json.dumps(status)), 1000)
        self.assertEqual(self.runner.runs, 1)

    def test_attention_lists_what_failed_or_warns_and_not_what_could_not_be_checked(self):
        runner = CountingRunner(fast_specs("ok", "unknown", "fail", "warn", "unknown"))
        doctor = DoctorExtension(monotonic=self.ticker, runner=runner)
        self.boot(doctor=doctor)
        self.report()
        status = doctor.status()
        self.assertEqual(status["attention"], ["c", "d"], "a row that is only 'could not be checked' is not something to fix")
        self.assertEqual(status["counts"], {"ok": 1, "warn": 1, "fail": 1, "unknown": 2})
        self.assertEqual(status["overall"], "fail")

    def test_status_is_a_copy_and_the_attention_list_is_bounded(self):
        runner = CountingRunner(fast_specs(*["warn"] * 40))
        doctor = DoctorExtension(monotonic=self.ticker, runner=runner)
        self.boot(doctor=doctor)
        self.report()
        status = doctor.status()
        self.assertEqual(len(status["attention"]), 12)
        status["counts"]["warn"] = 999
        status["attention"].clear()
        self.assertEqual(doctor.status()["counts"]["warn"], 40)

    def test_reports_are_copies_too(self):
        self.boot()
        first = self.doctor.report()
        first["checks"].clear()
        first["counts"]["ok"] = 99
        second = self.doctor.report()
        self.assertEqual(len(second["checks"]), 2)
        self.assertEqual(second["counts"]["ok"], 1)

    def test_status_never_waits_for_a_report_that_is_running_and_never_calls_the_station(self):
        gate = Gate(self)
        runner = CountingRunner(fast_specs("ok"))
        doctor = DoctorExtension(monotonic=self.ticker, runner=runner, budget=5)  # the instance the station runs, not a spare one
        self.boot(Hanging(gate), doctor=doctor)
        self.assertIsNotNone(doctor._ctx, "the instance under test must be the started one")
        worker = threading.Thread(target=lambda: self.get(), daemon=True)  # a report that holds the compute lock while the neighbour hangs
        worker.start()
        end = time.monotonic() + 10
        while runner.runs == 0 and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(runner.runs, 1)
        self.assertTrue(doctor._compute_lock.locked(), "the report is in progress and holds the lock")
        answers = []

        def poll():
            for _ in range(200):
                answers.append(doctor.status())

        with patch.object(Station, "status", side_effect=AssertionError("status() must not call the station")) as station_status:
            caller = threading.Thread(target=poll, daemon=True)
            started = time.monotonic()
            caller.start()
            caller.join(3)
            self.assertFalse(caller.is_alive(), "status() must not wait for the report that is running")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(answers), 200)
        self.assertEqual(station_status.call_count, 0)
        self.assertEqual(answers[0], {"checked": False}, "nothing has finished yet")
        gate.event.set()
        worker.join(10)
        self.assertEqual(doctor.status()["checked"], True)

    def test_the_host_accepts_the_status_shape(self):
        self.boot()
        self.report()
        entry = self.station.extensions.status()["doctor"]
        self.assertEqual((entry["state"], entry["error"]), ("running", None))
        json.dumps(entry)


class OperationTests(StationCase):
    def test_the_operation_returns_the_same_report(self):
        self.boot(Neighbour())
        result = self.station.action("doctor.report", {})
        self.assertEqual((result["ok"], result["operation"]), (True, "doctor.report"))
        self.assertEqual([c["id"] for c in result["checks"]], ["a", "b", "neighbour.disk"])
        self.assertEqual(result["overall"], "warn")
        self.assertEqual(self.station.action("doctor.report")["cached"], True)

    def test_refresh_is_typed_and_rate_limited_like_the_route(self):
        self.boot()
        self.station.action("doctor.report", {})
        self.assertEqual(self.station.action("doctor.report", {"refresh": True})["cached"], True)
        self.ticker.now += REFRESH_INTERVAL
        self.assertEqual(self.station.action("doctor.report", {"refresh": True})["cached"], False)
        self.assertEqual(self.runner.runs, 2)

    def test_arguments_that_could_point_it_somewhere_are_refused(self):
        self.boot()
        for bad in ({"refresh": "yes"}, {"refresh": 1}, {"path": "/etc"}, {"port": 22}, {"data_dir": "/"}, {"command": "ls"}, {"refresh": True, "x": 1}):
            with self.subTest(args=bad), self.assertRaises(StationError):
                self.station.action("doctor.report", bad)
        self.assertEqual(self.runner.runs, 0)

    def test_it_is_read_only_for_the_station(self):
        self.boot()
        before = (self.station.status()["running"], list(self.station.status()["queue"]), self.station.status()["station"]["name"])
        self.station.action("doctor.report", {})
        after = (self.station.status()["running"], list(self.station.status()["queue"]), self.station.status()["station"]["name"])
        self.assertEqual(before, after)

    def test_before_it_is_started_it_refuses_politely(self):
        ext = DoctorExtension(runner=CountingRunner(fast_specs("ok")))
        with self.assertRaisesRegex(ExtensionError, "not running"):
            ext.report()


class ShutdownTests(StationCase):
    def test_stopping_the_station_stops_the_doctors_runner_exactly_once(self):
        self.boot(Neighbour())
        self.report()
        self.assertEqual(self.runner.closes, 0, "the runner keeps working while the station runs")
        self.station.close()
        self.assertEqual(self.runner.closes, 1, "stopping the station must stop the doctor's runner")
        self.assertEqual(doctor_threads(), [])
        self.assertIsNone(self.station.extensions.find_route("GET", URL))
        self.assertEqual(self.station.extensions.status()["doctor"]["state"], "stopped")
        self.station.close()  # closing twice is fine
        self.assertEqual(self.runner.closes, 1)

    def test_stopping_the_station_stops_the_doctor_and_leaves_no_thread(self):
        self.boot(Neighbour())
        self.report()
        self.station.close()
        self.assertEqual(doctor_threads(), [])
        self.assertIsNone(self.station.extensions.find_route("GET", URL))
        self.assertEqual(self.station.extensions.status()["doctor"]["state"], "stopped")
        self.station.close()  # closing twice is fine

    def test_stopping_cancels_a_report_that_is_still_running(self):
        """A real program is still running when the station stops: stopping must cut it short, not wait for it or leave it behind."""
        beat = Path(self.temp.name) / "tool.beat"
        program = make_program(self.temp.name, "slowtool", f"import sys\nsys.argv = ['x', {str(beat)!r}]\n" + HEARTBEAT)

        def slow_tool(settings=None):
            result = run_tool([program], timeout=15, cancel=settings.cancel)
            return {"id": "tool", "title": "Tool", "status": "unknown" if result.cancelled else "ok",
                    "detail": "cancelled" if result.cancelled else "finished by itself", "hint": "Run it again.", "fix": ""}

        runner = CountingRunner((CheckSpec("tool", "Tool", slow_tool),))
        self.boot(doctor=DoctorExtension(monotonic=self.ticker, runner=runner, budget=12))
        answers = []
        worker = threading.Thread(target=lambda: answers.append(self.get()), daemon=True)
        worker.start()
        self.assertTrue(wait_for_files([beat], 20), "the program should be running before the station stops")
        started = time.monotonic()
        self.station.close()
        worker.join(8)
        self.assertFalse(worker.is_alive(), "the report must come back soon after the station stops, not wait for the program")
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(answers[0].status, 200)
        row = answers[0].body["checks"][0]
        self.assertEqual((row["status"], row["detail"]), ("unknown", "cancelled"), "the program was cut short by the stop, it did not finish by itself")
        self.assertEqual(runner.closes, 1)
        self.assertFalse(alive_after(beat), "the program must be stopped, not left running")
        self.assertEqual(doctor_threads(), [])

    def test_a_report_that_finishes_as_the_station_stops_leaves_no_thread(self):
        """The earlier version of this scenario (it was called "stopping cancels a report", but the neighbour was let go first, so nothing was cancelled)."""
        gate = Gate(self)
        self.boot(Hanging(gate), doctor=DoctorExtension(monotonic=self.ticker, runner=CountingRunner(fast_specs("ok")), budget=20))
        worker = threading.Thread(target=lambda: self.get())
        worker.start()
        time.sleep(0.2)
        started = time.monotonic()
        gate.event.set()
        self.station.close()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(doctor_threads(), [])

    def test_a_report_waiting_on_a_neighbour_is_not_held_up_by_the_stop_itself(self):
        """Stopping returns at once even when a neighbour's check never answers; that report simply ends at its own deadline."""
        gate = Gate(self)
        self.boot(Hanging(gate), doctor=DoctorExtension(monotonic=self.ticker, runner=CountingRunner(fast_specs("ok")), budget=1.0))
        worker = threading.Thread(target=lambda: self.get(), daemon=True)
        worker.start()
        time.sleep(0.2)
        started = time.monotonic()
        self.station.close()
        self.assertLess(time.monotonic() - started, 4.0, "stopping must not wait for a neighbour that never answers")
        worker.join(10)
        self.assertFalse(worker.is_alive())
        gate.event.set()


class HttpTests(unittest.TestCase):
    """Through the real server: authentication, origin checks and the JSON the browser will see."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = CountingRunner(fast_specs("ok", "warn"))
        self.doctor = DoctorExtension(runner=self.runner, budget=5)
        self.station = Station(Path(self.temp.name) / "data", start_worker=False, extensions=[self.doctor, Neighbour()])
        with patch.dict("os.environ", {}, clear=True):
            self.server = Server(("127.0.0.1", 0), self.station, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.station.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if token:
            sent["X-Myradio-Token"] = TOKEN
        if body is not None:
            sent["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def test_the_browser_gets_the_report_as_json(self):
        status, body = self.call("GET", URL)
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in body["checks"]], ["a", "b", "neighbour.disk"])
        self.assertEqual((body["schema"], body["overall"], body["cached"]), (1, "warn", False))
        status, again = self.call("GET", URL)
        self.assertEqual((status, again["cached"], self.runner.runs), (200, True, 1))
        status, fresh = self.call("GET", URL + "?refresh=1")
        self.assertEqual((status, fresh["cached"]), (200, True), "a refresh within five seconds is served from the cache")

    def test_it_needs_the_unlock_code_and_a_local_origin(self):
        self.assertEqual(self.call("GET", URL, token=False)[0], 401)
        self.assertEqual(self.call("GET", URL, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", URL, headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.runner.runs, 0, "refused requests must not run any check")

    def test_only_get_is_accepted_and_odd_requests_are_refused_plainly(self):
        self.assertEqual(self.call("POST", URL, {})[0], 404)
        self.assertEqual(self.call("GET", URL + "/extra")[0], 404)
        self.assertEqual(self.call("GET", "/api/x/doctor/")[0], 404)
        self.assertEqual(self.call("GET", URL + "?refresh=%00&x=" + "a" * 3000)[0], 200)

    def test_the_operation_is_in_the_catalog_the_agents_read(self):
        status, catalog = self.call("GET", "/api/operations")
        self.assertEqual(status, 200)
        self.assertIn("doctor.report", {c["name"] for c in catalog})
        status, body = self.call("POST", "/api/action", {"operation": "doctor.report", "arguments": {}})
        self.assertEqual((status, body["ok"], body["overall"]), (200, True, "warn"))
        self.assertEqual(self.call("POST", "/api/action", {"operation": "doctor.report", "arguments": {"path": "/etc"}})[0], 400)


if __name__ == "__main__":
    unittest.main()
