"""Doctor report: guarded parallel runs, the shape of the report, summary and exit status, plain-text
rendering, and static proof that the package is read-only, offline and never builds an install command.

Hung, slow and misbehaving checks here are small in-process fakes released at the end of each test; the
only real programs started are tiny Python scripts in a temporary directory.
"""
import ast
import collections
import datetime as dt
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from myradio.doctor import checks, report as report_module
from myradio.doctor.checks import CHECKS, STATES, CheckSpec, Settings
from myradio.doctor.report import (EXIT_CODES, REPORT_BUDGET, ReportRunner, exit_status, iso_utc, normalize_check, render_text,
                                   run_report, summarize)
from myradio.doctor.tools import run_tool

from test_doctor_fixtures import (UBUNTU, Gate, canned_spec, doctor_threads, fast_specs, ffmpeg_table, guard_ports, hermetic_tools,
                                  make_program, make_roots, pretend_computer)

Usage = collections.namedtuple("Usage", "total used free")
KEYS = ["id", "title", "status", "detail", "hint", "fix"]
TOP = ["schema", "myradio_version", "generated_at", "duration_ms", "overall", "summary", "counts", "partial", "checks"]
PY = sys.executable


class NormalizeTests(unittest.TestCase):
    def test_a_good_check_keeps_exactly_the_six_keys(self):
        raw = {"id": "x", "title": "X", "status": "warn", "detail": "d", "hint": "h", "fix": "f", "extra": "dropped"}
        self.assertEqual(normalize_check(raw), {"id": "x", "title": "X", "status": "warn", "detail": "d", "hint": "h", "fix": "f"})
        self.assertEqual(list(normalize_check(raw)), KEYS)

    def test_anything_that_is_not_ok_always_gets_a_hint_and_a_detail(self):
        for status in ("warn", "fail", "unknown"):
            item = normalize_check({"id": "x", "title": "X", "status": status})
            self.assertTrue(item["hint"] and item["detail"], status)
        item = normalize_check({"id": "x", "title": "X", "status": "ok"})
        self.assertEqual((item["hint"], item["detail"], item["fix"]), ("", "", ""))

    def test_unusable_checks_are_refused(self):
        for raw in (None, "x", [], 7, {}, {"id": "x"}, {"id": "x", "title": "X"}, {"id": "x", "title": "X", "status": "purple"},
                    {"id": 1, "title": "X", "status": "ok"}, {"id": "x", "title": None, "status": "ok"}, {"id": "", "title": "X", "status": "ok"},
                    {"id": "x", "title": "X", "status": ["ok"]}, {"id": "x", "title": "X", "status": "OK"}):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(normalize_check(raw))

    def test_text_is_cleaned_and_bounded(self):
        item = normalize_check({"id": "a\x1b" + "b" * 300, "title": "T" * 500, "status": "ok", "detail": "d\n\n" + "x" * 900,
                                "hint": "\u202e" + "h" * 900, "fix": "f" * 900})
        self.assertLessEqual(len(item["id"]), 80)
        self.assertLessEqual(len(item["title"]), 120)
        for key in ("detail", "hint", "fix"):
            self.assertLessEqual(len(item[key]), 400)
        self.assertNotIn("\x1b", item["id"])
        self.assertNotIn("\u202e", item["hint"])
        self.assertNotIn("\n", item["detail"])

    def test_values_that_are_not_text_do_not_raise(self):
        item = normalize_check({"id": "x", "title": "X", "status": "ok", "detail": 5, "hint": None, "fix": {"a": 1}})
        self.assertEqual((item["detail"], item["hint"]), ("5", ""))

    def test_the_spec_can_force_the_id_and_title(self):
        item = normalize_check({"id": "wrong", "title": "Wrong", "status": "ok"}, check_id="right", title="Right")
        self.assertEqual((item["id"], item["title"]), ("right", "Right"))


class SummaryAndExitStatusTests(unittest.TestCase):
    def items(self, **counts):
        return [{"status": state} for state, number in counts.items() for _ in range(number)]

    def test_counts_always_list_the_four_states_in_order(self):
        result = summarize(self.items(fail=1))
        self.assertEqual(list(result), ["overall", "summary", "counts"])
        self.assertEqual(list(result["counts"]), list(STATES))
        self.assertEqual(result["counts"], {"ok": 0, "warn": 0, "fail": 1, "unknown": 0})

    def test_the_overall_state_is_the_worst_one(self):
        cases = (({"ok": 3}, "ok"), ({"ok": 2, "unknown": 1}, "unknown"), ({"ok": 2, "unknown": 1, "warn": 1}, "warn"),
                 ({"ok": 1, "warn": 2, "fail": 1, "unknown": 3}, "fail"), ({"fail": 1}, "fail"), ({}, "unknown"))
        for counts, expected in cases:
            with self.subTest(counts=counts):
                self.assertEqual(summarize(self.items(**counts))["overall"], expected)

    def test_the_summary_sentences_are_plain_and_agree_in_number(self):
        self.assertEqual(summarize(self.items(ok=4))["summary"], "Your computer is ready for myradio.")
        self.assertEqual(summarize(self.items(ok=4, warn=1))["summary"], "Your computer can run myradio. 1 thing is worth a look.")
        self.assertEqual(summarize(self.items(warn=3))["summary"], "Your computer can run myradio. 3 things are worth a look.")
        self.assertEqual(summarize(self.items(fail=1))["summary"], "Your computer is not ready yet: 1 thing needs fixing first.")
        self.assertEqual(summarize(self.items(fail=2, warn=1))["summary"],
                         "Your computer is not ready yet: 2 things need fixing first. 1 more is worth a look.")
        self.assertEqual(summarize(self.items(fail=1, warn=2))["summary"],
                         "Your computer is not ready yet: 1 thing needs fixing first. 2 more are worth a look.")
        self.assertEqual(summarize(self.items(ok=2, unknown=1))["summary"], "Nothing is wrong that we could see, but 1 thing could not be checked.")
        self.assertEqual(summarize(self.items(unknown=2))["summary"], "Nothing is wrong that we could see, but 2 things could not be checked.")
        self.assertEqual(summarize([])["summary"], "No checks were run.")

    def test_exit_status_follows_the_worst_state(self):
        def status_for(**counts):
            return exit_status({"checks": self.items(**counts)})

        self.assertEqual(status_for(ok=5), 0)
        self.assertEqual(status_for(ok=5, unknown=1), 1)
        self.assertEqual(status_for(ok=5, warn=1), 1)
        self.assertEqual(status_for(ok=5, warn=1, unknown=2), 1)
        self.assertEqual(status_for(ok=5, fail=1), 2)
        self.assertEqual(status_for(warn=4, unknown=4, fail=1), 2)
        self.assertEqual(EXIT_CODES, {"ok": 0, "unknown": 1, "warn": 1, "fail": 2})
        for broken in ({}, {"checks": None}, {"checks": [{"nope": 1}]}, None):
            self.assertEqual(exit_status(broken), 1)

    def test_the_timestamp_has_an_offset_and_bad_values_do_not_raise(self):
        self.assertEqual(iso_utc(1_700_000_000), "2023-11-14T22:13:20+00:00")
        for bad in (float("nan"), float("inf"), None, "x", 10 ** 30, -10 ** 30):
            self.assertEqual(iso_utc(bad), "1970-01-01T00:00:00+00:00")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        guard_ports(self)  # a real check run here must use port 0, never a fixed port

    def test_the_report_has_a_stable_shape(self):
        runner = ReportRunner(fast_specs("ok", "warn", "ok"), clock=lambda: 1_700_000_000.0)
        self.addCleanup(runner.close)
        report = runner.run()
        self.assertEqual(list(report), TOP)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["generated_at"], "2023-11-14T22:13:20+00:00")
        self.assertIsInstance(report["duration_ms"], int)
        self.assertEqual((report["overall"], report["partial"]), ("warn", False))
        self.assertEqual([c["id"] for c in report["checks"]], ["a", "b", "c"])
        for item in report["checks"]:
            self.assertEqual(list(item), KEYS)
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_a_missing_version_is_an_empty_string_not_a_crash(self):
        with patch.dict(sys.modules, {"myradio": None}):
            self.assertEqual(report_module._version(), "")

    def test_it_reports_the_real_version(self):
        import myradio
        self.assertEqual(run_report(specs=fast_specs("ok"))["myradio_version"], myradio.__version__)

    def test_a_check_that_raises_becomes_unknown_and_the_rest_carry_on(self):
        def boom(settings=None):
            raise RuntimeError("secret internals at /home/someone/.ssh")

        specs = (canned_spec("good"), CheckSpec("bad", "Bad one", boom), canned_spec("also_good"))
        report = run_report(specs=specs)
        self.assertEqual([c["status"] for c in report["checks"]], ["ok", "unknown", "ok"])
        bad = report["checks"][1]
        self.assertEqual((bad["id"], bad["title"]), ("bad", "Bad one"))
        self.assertNotIn("secret", json.dumps(report))
        self.assertTrue(bad["hint"])
        self.assertFalse(report["partial"])

    def test_things_that_are_not_exceptions_cannot_take_the_report_down_either(self):
        def leave(settings=None):
            raise SystemExit(3)

        def interrupt(settings=None):
            raise KeyboardInterrupt()

        report = run_report(specs=(CheckSpec("a", "A", leave), CheckSpec("b", "B", interrupt), canned_spec("c")))
        self.assertEqual([c["status"] for c in report["checks"]], ["unknown", "unknown", "ok"])

    def test_a_check_that_returns_nonsense_is_unknown(self):
        for value in (None, "ok", [], 5, {}, {"detail": "no status"}, {"id": "x", "title": "X", "status": "great"}):
            with self.subTest(value=repr(value)):
                report = run_report(specs=(CheckSpec("odd", "Odd", lambda settings=None, v=value: v),))
                self.assertEqual(report["checks"][0]["status"], "unknown")
                self.assertIn("usable answer", report["checks"][0]["detail"])

    def test_a_bare_status_is_a_usable_answer(self):
        item = run_report(specs=(CheckSpec("bare", "Bare", lambda settings=None: {"status": "ok"}),))["checks"][0]
        self.assertEqual((item["id"], item["title"], item["status"], item["detail"]), ("bare", "Bare", "ok", ""))

    def test_the_spec_decides_the_id_and_title(self):
        spec = CheckSpec("real", "Real title", lambda settings=None: {"id": "liar", "title": "Liar", "status": "ok"})
        item = run_report(specs=(spec,))["checks"][0]
        self.assertEqual((item["id"], item["title"]), ("real", "Real title"))

    def test_a_hung_check_ends_at_the_deadline_not_when_it_finishes(self):
        gate = Gate(self)
        started = time.monotonic()
        runner = ReportRunner((canned_spec("fine"), gate.spec(), canned_spec("also_fine")))
        self.addCleanup(runner.close)
        report = runner.run(budget=0.4)
        took = time.monotonic() - started
        self.assertLess(took, 2.0)
        self.assertTrue(report["partial"])
        self.assertEqual([c["status"] for c in report["checks"]], ["ok", "unknown", "ok"])
        self.assertIn("did not finish in time", report["checks"][1]["detail"])
        self.assertTrue(report["checks"][1]["hint"])
        self.assertLess(report["duration_ms"], 2000)

    def test_a_check_that_is_still_stuck_is_not_started_again(self):
        gate = Gate(self)
        runner = ReportRunner((gate.spec(),))
        self.addCleanup(runner.close)
        first = runner.run(budget=0.2)
        threads_after_first = len(doctor_threads())
        second = runner.run(budget=0.2)
        third = runner.run(budget=0.2)
        self.assertIn("did not finish in time", first["checks"][0]["detail"])
        for later in (second, third):
            self.assertIn("earlier attempt", later["checks"][0]["detail"])
            self.assertTrue(later["partial"])
        self.assertEqual(len(doctor_threads()), threads_after_first, "no new thread for a check that is still stuck")
        gate.event.set()
        for thread in doctor_threads():
            thread.join(3)
        fourth = runner.run(budget=2)
        self.assertEqual((fourth["checks"][0]["status"], fourth["partial"]), ("ok", False), "a freed check runs normally again")

    def test_checks_run_side_by_side(self):
        def napper(index):
            def check(settings=None):
                time.sleep(0.3)
                return {"id": f"n{index}", "title": "N", "status": "ok", "detail": "slept", "hint": "", "fix": ""}
            return CheckSpec(f"n{index}", "N", check)

        started = time.monotonic()
        report = run_report(specs=tuple(napper(i) for i in range(10)), budget=5)
        took = time.monotonic() - started
        self.assertLess(took, 1.5, "ten 0.3 second checks must not take three seconds")
        self.assertEqual(report["counts"]["ok"], 10)

    def test_overlapping_reports_wait_their_turn_and_none_is_wrongly_called_stuck(self):
        def pause(settings=None):
            time.sleep(0.05)
            return {"id": "p", "title": "P", "status": "ok", "detail": "ok", "hint": "", "fix": ""}

        runner = ReportRunner((CheckSpec("p", "P", pause),))
        self.addCleanup(runner.close)
        results, errors = [], []

        def go():
            try:
                results.append(runner.run(budget=5))
            except BaseException as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for report in results:
            self.assertEqual((report["checks"][0]["status"], report["partial"]), ("ok", False))

    def test_close_cancels_a_tool_that_is_still_running_and_leaves_no_thread_behind(self):
        def long_tool(settings=None):
            result = run_tool([PY, "-c", "import signal, time; signal.alarm(20); time.sleep(30)"], timeout=30, cancel=settings.cancel)
            return {"id": "tool", "title": "Tool", "status": "unknown" if result.cancelled else "ok", "detail": "cancelled" if result.cancelled else "finished",
                    "hint": "Run it again.", "fix": ""}

        runner = ReportRunner((CheckSpec("tool", "Tool", long_tool),))
        answer = []
        worker = threading.Thread(target=lambda: answer.append(runner.run(budget=20)))
        worker.start()
        time.sleep(0.4)
        started = time.monotonic()
        runner.close()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(answer[0]["checks"][0]["detail"], "cancelled")
        self.assertEqual(doctor_threads(), [])
        runner.close()  # safe to call twice

    def test_a_closed_runner_answers_without_starting_anything(self):
        started = []
        runner = ReportRunner((CheckSpec("a", "A", lambda settings=None: started.append(1)),))
        runner.close()
        report = runner.run()
        self.assertEqual(started, [])
        self.assertEqual(report["checks"][0]["status"], "unknown")
        self.assertIn("stopped", report["checks"][0]["detail"])

    def test_the_one_shot_helper_leaves_nothing_running(self):
        before = len(threading.enumerate())
        run_report(specs=fast_specs("ok", "ok"))
        self.assertLessEqual(len(threading.enumerate()), before)
        self.assertEqual(doctor_threads(), [])

    def test_a_computer_that_will_not_give_us_a_thread_still_gets_an_answer(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            report = run_report(specs=fast_specs("ok", "ok"))
        self.assertEqual([c["status"] for c in report["checks"]], ["unknown", "unknown"])

    def test_a_malformed_check_list_still_gets_a_plain_answer(self):
        for specs in ((object(),), (None,), ("x",), (CheckSpec("a", "A", None),)):
            with self.subTest(specs=repr(specs)[:30]):
                report = ReportRunner(specs).run()
                self.assertEqual(list(report), TOP)
                self.assertIn(report["checks"][0]["status"], ("unknown",))
                self.assertTrue(report["checks"][0]["hint"])

    def test_nonsense_settings_and_budgets_never_raise(self):
        for settings in (None, "x", 5, object(), Settings(data_dir=object())):
            for budget in (-5, 0, float("nan"), 0.01):
                with self.subTest(settings=repr(settings)[:20], budget=budget):
                    report = ReportRunner(fast_specs("ok")).run(settings, budget=budget)
                    self.assertEqual(list(report), TOP)
        self.assertEqual(REPORT_BUDGET, 6.0)

    def test_the_real_checks_finish_quickly_and_in_order(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            hermetic_tools(stack, temp)
            started = time.monotonic()
            report = run_report(Settings(data_dir=temp, port=0))
        self.assertEqual([c["id"] for c in report["checks"]], [spec.id for spec in CHECKS])
        self.assertLess(time.monotonic() - started, REPORT_BUDGET)
        self.assertFalse(report["partial"], report)


class ExtraChecksTests(unittest.TestCase):
    def run_with(self, extra, budget=2.0):
        return run_report(specs=fast_specs("ok"), extra=extra, budget=budget)

    def test_other_parts_of_myradio_are_appended_after_ours_and_cleaned(self):
        def extra():
            return [{"id": "generation.music", "title": "Music maker", "status": "warn", "detail": "Not set up\x1b[31m", "hint": "Set it up."},
                    {"id": "ads.licence", "title": "Licence", "status": "ok", "detail": "fine"}]

        report = self.run_with(extra)
        self.assertEqual([c["id"] for c in report["checks"]], ["a", "generation.music", "ads.licence"])
        folded = report["checks"][1]
        self.assertEqual(list(folded), KEYS)
        self.assertNotIn("\x1b", folded["detail"])
        self.assertEqual(folded["fix"], "")
        self.assertEqual(report["overall"], "warn")
        self.assertEqual(report["counts"]["warn"], 1)

    def test_bad_duplicate_and_our_own_entries_are_dropped(self):
        def extra():
            return ["nonsense", None, {"id": "a", "title": "Same id as ours", "status": "fail"},
                    {"id": "doctor.ffmpeg", "title": "Ours again", "status": "fail"},
                    {"id": "x.y", "title": "Bad status", "status": "purple"},
                    {"id": "x.ok", "title": "Good", "status": "ok"}, {"id": "x.ok", "title": "Duplicate", "status": "fail"}]

        report = self.run_with(extra)
        self.assertEqual([c["id"] for c in report["checks"]], ["a", "x.ok"])
        self.assertEqual(report["overall"], "ok")

    def test_the_number_of_folded_checks_is_bounded(self):
        report = self.run_with(lambda: [{"id": f"x.{i}", "title": "T", "status": "ok"} for i in range(5000)])
        self.assertEqual(len(report["checks"]), 1 + report_module.MAX_FOREIGN_CHECKS)

    def test_at_most_one_hundred_checks_from_other_parts_are_accepted(self):
        """The limit in docs/lanes/doctor.md, written as a number here so that changing it is noticed."""
        self.assertEqual(report_module.MAX_FOREIGN_CHECKS, 100)
        for given, kept in ((99, 99), (100, 100), (101, 100), (250, 100)):
            with self.subTest(given=given):
                report = self.run_with(lambda n=given: [{"id": f"x.{i}", "title": "T", "status": "ok"} for i in range(n)])
                self.assertEqual(len(report["checks"]) - 1, kept)
                self.assertEqual(report["checks"][-1]["id"], f"x.{kept - 1}", "the first hundred are kept, in order")

    def test_a_failing_or_odd_gatherer_becomes_one_unknown_row(self):
        def explode():
            raise RuntimeError("secret")

        for gatherer in (explode, lambda: "not a list", lambda: None, lambda: 5):
            with self.subTest(gatherer=repr(gatherer)[:30]):
                report = self.run_with(gatherer)
                row = report["checks"][-1]
                self.assertEqual((row["id"], row["status"]), ("extensions", "unknown"))
                self.assertNotIn("secret", json.dumps(report))
                self.assertTrue(row["hint"])

    def test_a_gatherer_that_hangs_ends_at_the_deadline(self):
        gate = Gate(self)

        def hang():
            gate.event.wait(30)
            return []

        started = time.monotonic()
        report = self.run_with(hang, budget=0.3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(report["partial"])
        self.assertIn("did not report back in time", report["checks"][-1]["detail"])

    def test_no_gatherer_means_no_extra_row(self):
        self.assertEqual([c["id"] for c in run_report(specs=fast_specs("ok"))["checks"]], ["a"])


class RenderTextTests(unittest.TestCase):
    def sample(self):
        specs = (canned_spec("good", "ok", "All fine.", title="Good thing"),
                 canned_spec("meh", "warn", "Something is slightly off.", "Do the small thing.", "run: small-thing --now", title="Meh thing"),
                 canned_spec("bad", "fail", "Broken.", "Fix the big thing first.", title="Bad thing"),
                 canned_spec("who", "unknown", "Could not tell.", "Run it again.", title="Who knows"))
        return run_report(specs=specs)

    def test_every_row_has_a_word_for_its_state_and_help_only_when_needed(self):
        text = render_text(self.sample())
        lines = text.splitlines()
        self.assertEqual(lines[0], "myradio doctor: is your computer ready?")
        self.assertIn("It does not install, download or change anything.", " ".join(lines[1:3]))
        for label, title in (("OK", "Good thing"), ("WARN", "Meh thing"), ("FAIL", "Bad thing"), ("UNKNOWN", "Who knows")):
            self.assertTrue(any(line.startswith(f"{label:<8}{title}") for line in lines), f"{label} {title}")
        self.assertIn("What to do: Do the small thing.", text)
        self.assertIn("How to fix it: run: small-thing --now", text)
        self.assertEqual(text.count("What to do:"), 3, "an OK row needs no advice")
        self.assertEqual(text.count("How to fix it:"), 1, "only rows with a fix show one")
        self.assertIn("Your computer is not ready yet: 1 thing needs fixing first. 1 more is worth a look.", text)
        self.assertRegex(text, r"Looked at 4 things in \d\.\d seconds\.")

    def test_lines_are_wrapped_and_plain_ascii_when_the_input_is(self):
        long = canned_spec("long", "warn", "word " * 80, "hint " * 80, "fix " * 80)
        text = render_text(run_report(specs=(long,)))
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 80)
        text.encode("ascii")

    def test_a_command_in_the_fix_is_never_broken_across_lines(self):
        command = "chmod 700 '/very long folder name/" + "a long part " * 8 + "last'"
        fix = "Open a terminal and run: " + command
        self.assertGreater(len(fix), 78)
        text = render_text(run_report(specs=(canned_spec("c", "fail", "Open.", "Close it.", fix),)))
        self.assertIn(" " * 8 + "How to fix it: " + fix, text.splitlines(), "the whole fix is one line, so it can be copied whole")
        self.assertEqual(sum(1 for line in text.splitlines() if "chmod 700" in line), 1)
        advice = canned_spec("a", "warn", "Odd.", "Look.", "Install the full version of the program with your system's package manager, or get it from the web site. " * 3)
        lines = render_text(run_report(specs=(advice,))).splitlines()
        self.assertGreater(len([line for line in lines if "package manager" in line]), 1, "advice without a command is still wrapped")
        self.assertLessEqual(max(len(line) for line in lines), 80)

    def test_a_partial_report_says_so(self):
        gate = Gate(self)
        report = ReportRunner((gate.spec(),)).run(budget=0.2)
        self.assertIn("Some checks did not finish in time.", render_text(report))

    def test_hostile_text_cannot_reach_the_terminal(self):
        spec = canned_spec("evil", "warn", "bad\x1b]0;owned\x07 name \u202e", "hint\x1b[2J", "fix\x00", title="Evil\x1b[31m")
        text = render_text(run_report(specs=(spec,)))
        for char in ("\x1b", "\x07", "\x00", "\u202e"):
            self.assertNotIn(char, text)

    def test_a_malformed_report_still_renders_something(self):
        for bad in ({}, {"checks": []}, {"checks": [{"status": "ok", "title": "T", "detail": ""}], "summary": "s"}):
            with self.subTest(bad=bad):
                self.assertIsInstance(render_text(bad), str)


class FakeZone(dt.tzinfo):
    """A made-up zone: six hours behind UTC, one hour less in March to October."""

    def _summer(self, when):
        return 3 <= when.month <= 10

    def utcoffset(self, when):
        return dt.timedelta(hours=-5 if self._summer(when) else -6)

    def dst(self, when):
        return dt.timedelta(hours=1 if self._summer(when) else 0)

    def tzname(self, when):
        return "FAKE"


class StableJsonTests(unittest.TestCase):
    """A fully simulated Linux computer, so the answers do not depend on the machine running the tests."""

    NOW = dt.datetime(2026, 12, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()

    def setUp(self):
        guard_ports(self)

    def simulated(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        roots = make_roots(Path(temp.name) / "roots", render_nodes=("renderD128",))
        data = Path(temp.name) / "station"
        data.mkdir(mode=0o700)
        pretend_computer(stack, system="Linux", machine="x86_64", release="6.8.0-test", os_release=UBUNTU, programs=("ffmpeg", "ffprobe", "clinfo"),
                         table=ffmpeg_table())
        stack.enter_context(patch.dict("os.environ", {"HOME": "/home/test-user", "TZ": "America/Chicago"}))
        stack.enter_context(patch("myradio.doctor.checks._local_offset", return_value=-21600))
        stack.enter_context(patch("zoneinfo.ZoneInfo", side_effect=lambda name: FakeZone()))
        stack.enter_context(patch("os.sched_getaffinity", return_value=set(range(8)), create=True))
        stack.enter_context(patch("shutil.disk_usage", return_value=Usage(320 * 1024 ** 3, 240 * 1024 ** 3, 80 * 1024 ** 3)))
        specs = tuple(CheckSpec("python", spec.title, lambda s=None: checks.check_python(s, version=(3, 12, 3), implementation="CPython"))
                      if spec.id == "python" else spec for spec in CHECKS)
        return specs, Settings(data_dir=str(data), port=0, now=lambda: self.NOW, **roots)

    def test_two_runs_of_the_same_computer_give_identical_json_apart_from_time(self):
        specs, settings = self.simulated()
        runner = ReportRunner(specs, clock=lambda: 1_700_000_000.0)  # a fixed clock, so a second boundary cannot make two runs differ
        self.addCleanup(runner.close)
        first = runner.run(settings)
        second = runner.run(settings)
        for report in (first, second):
            report.pop("duration_ms")  # how long it took is the one thing that legitimately varies
        self.assertEqual(json.dumps(first, indent=2), json.dumps(second, indent=2))
        self.assertEqual(list(first), [key for key in TOP if key != "duration_ms"])
        self.assertEqual([c["id"] for c in first["checks"]], [spec.id for spec in CHECKS])
        for item in first["checks"]:
            self.assertEqual(list(item), KEYS)

    def test_the_simulated_computer_is_all_ok_and_the_json_is_pure_ascii(self):
        specs, settings = self.simulated()
        report = run_report(settings, specs=specs)
        self.assertEqual({c["id"]: c["status"] for c in report["checks"]}, {spec.id: "ok" for spec in CHECKS}, report["checks"])
        self.assertEqual((report["overall"], report["summary"]), ("ok", "Your computer is ready for myradio."))
        text = json.dumps(report, indent=2)
        text.encode("ascii")
        self.assertEqual(json.loads(text), report)


class HungWorldTests(unittest.TestCase):
    """Whole reports against computers where things never answer."""

    def setUp(self):
        guard_ports(self)

    def test_a_drive_that_never_answers_cannot_stall_the_report_or_pile_up_threads(self):
        gate = Gate(self)
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            hermetic_tools(stack, temp)
            stack.enter_context(patch("shutil.disk_usage", side_effect=lambda path: gate.event.wait(30)))
            runner = ReportRunner()
            self.addCleanup(runner.close)
            settings = Settings(data_dir=temp, port=0, tool_timeout=5)
            started = time.monotonic()
            first = runner.run(settings, budget=1.0)
            self.assertLess(time.monotonic() - started, 3.0)
            rows = {c["id"]: c for c in first["checks"]}
            self.assertEqual(rows["disk-space"]["status"], "unknown")
            self.assertIn("did not finish in time", rows["disk-space"]["detail"])
            self.assertTrue(first["partial"])
            self.assertEqual(rows["python"]["status"], "ok")
            stuck = [t for t in doctor_threads() if t.name == "myradio-doctor-disk-space"]
            self.assertEqual(len(stuck), 1)
            for _ in range(3):
                again = runner.run(settings, budget=1.0)
                self.assertIn("earlier attempt", {c["id"]: c for c in again["checks"]}["disk-space"]["detail"])
            self.assertEqual(len([t for t in doctor_threads() if t.name == "myradio-doctor-disk-space"]), 1, "still one stuck thread, not four")

    def test_every_tool_hanging_with_the_real_time_limits_still_ends_in_a_few_seconds(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            shelf = Path(temp) / "bin"
            shelf.mkdir()
            pids = Path(temp) / "pids"
            pids.mkdir()
            programs = {}
            for name in ("ffmpeg", "ffprobe", "system_profiler", "sysctl"):
                programs[name] = make_program(shelf, name, f"import os, signal, time\nsignal.alarm(30)\nopen({str(pids / (name + '.' + '%d'))!r} % os.getpid(), 'w').write('x')\ntime.sleep(60)\n")
            pretend_computer(stack, system="Darwin", machine="x86_64", mac_version="13.6", programs=(), table=None)
            stack.enter_context(patch("myradio.doctor.checks.run_tool", run_tool))  # the real runner, not the fake table
            stack.enter_context(patch("shutil.which", side_effect=lambda name, *a, **k: programs.get(name)))
            station = Path(temp) / "station"
            station.mkdir(mode=0o700)
            started = time.monotonic()
            report = run_report(Settings(data_dir=str(station), port=0))  # the default time limits
            took = time.monotonic() - started
            rows = {c["id"]: c for c in report["checks"]}
            started_pids = [int(name.split(".")[1]) for name in os.listdir(pids)]
        self.assertLess(took, 6.0, f"took {took:.1f}s")
        self.assertGreaterEqual(took, 2.0, "the hung tools really were waited for")
        for name in ("ffmpeg", "system"):
            self.assertEqual(rows[name]["status"], "unknown", rows[name])
            self.assertTrue(rows[name]["hint"])
        self.assertEqual(rows["accelerator"]["status"], "unknown")
        self.assertEqual(rows["python"]["status"], "ok")
        self.assertFalse(report["partial"], "every check itself finished; only the tools timed out")
        self.assertGreaterEqual(len(started_pids), 3)
        time.sleep(0.3)
        for pid in started_pids:
            with self.assertRaises(ProcessLookupError, msg=f"tool {pid} was left running"):
                os.kill(pid, 0)


# ------------------------------------------------------------ static guarantees

DOCTOR_DIR = Path(report_module.__file__).parent
FORBIDDEN_CALLS = {("os", name) for name in ("system", "popen", "makedirs", "mkdir", "remove", "unlink", "rmdir", "removedirs", "rename", "renames",
                                              "replace", "chmod", "chown", "symlink", "link", "truncate", "write", "ftruncate", "mkfifo", "execv", "execvp",
                                              "spawnv", "fork", "setuid", "setgid", "utime")} | \
                  {("shutil", name) for name in ("rmtree", "copy", "copy2", "copyfile", "copytree", "move", "chown", "make_archive", "unpack_archive")} | \
                  {("socket", name) for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "fromfd")} | \
                  {("tempfile", name) for name in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory", "SpooledTemporaryFile")}
FORBIDDEN_ATTRIBUTES = {"connect", "connect_ex", "listen", "accept", "sendto", "sendall", "write_text", "write_bytes", "touch", "mkdir", "unlink",
                        "rmdir", "chmod", "rename", "symlink_to", "hardlink_to", "urlopen", "urlretrieve", "request"}
FORBIDDEN_IMPORTS = {"urllib", "http", "ftplib", "smtplib", "telnetlib", "xmlrpc", "ctypes", "pickle", "marshal", "shelve", "requests", "asyncio", "multiprocessing"}
INSTALLERS = {"sudo", "su", "doas", "brew", "apt", "apt-get", "pip", "pip3", "dnf", "yum", "pacman", "zypper", "apk", "snap", "chmod", "chown", "rm",
              "curl", "wget", "mv", "cp", "kill", "pkill", "systemctl", "launchctl", "open", "defaults"}


def parsed():
    for path in sorted(DOCTOR_DIR.glob("*.py")):
        yield path, ast.parse(path.read_text(), filename=str(path))


class ReadOnlyAndOfflineTests(unittest.TestCase):
    def setUp(self):
        self.ports = guard_ports(self)  # also fails any test here that binds a fixed port

    def test_the_package_has_the_files_we_expect(self):
        self.assertEqual(sorted(p.name for p in DOCTOR_DIR.glob("*.py")), ["__init__.py", "__main__.py", "checks.py", "extension.py", "report.py", "tools.py"])

    def test_nothing_is_written_deleted_changed_or_sent(self):
        offenders = []
        for path, tree in parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in FORBIDDEN_CALLS:
                        offenders.append((path.name, node.lineno, f"{owner.id}.{node.func.attr}"))
                    if node.func.attr in FORBIDDEN_ATTRIBUTES and not (node.func.attr == "connect" and getattr(owner, "id", "") == "sqlite3"):
                        offenders.append((path.name, node.lineno, node.func.attr))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec", "compile", "__import__"):
                    offenders.append((path.name, node.lineno, node.func.id))
        self.assertEqual(offenders, [])

    def test_no_networking_or_dangerous_modules_are_imported(self):
        offenders = []
        for path, tree in parsed():
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in FORBIDDEN_IMPORTS:
                        offenders.append((path.name, node.lineno, name))
        self.assertEqual(offenders, [])

    def test_the_only_descriptor_work_is_pointing_a_failed_output_at_the_null_device(self):
        """The one thing the package ever opens for writing is the null device, and only after its own output has failed (see __main__.py)."""
        calls = []
        for path, tree in parsed():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "os" and node.func.attr in ("open", "dup", "dup2", "pipe", "fdopen", "close")):
                    calls.append((path.name, node.func.attr, [ast.unparse(argument) for argument in node.args]))
        self.assertEqual(sorted(calls), [("__main__.py", "close", ["null"]), ("__main__.py", "dup2", ["null", "fd"]),
                                         ("__main__.py", "open", ["os.devnull", "os.O_WRONLY"])])

    def test_files_are_only_ever_opened_for_reading(self):
        found = 0
        for path, tree in parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                    found += 1
                    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    if mode is not None:
                        self.assertIsInstance(mode, ast.Constant, f"{path.name}:{node.lineno}")
                        self.assertRegex(mode.value, r"^r?b?$", f"{path.name}:{node.lineno} opens a file for more than reading")
        self.assertGreater(found, 0)

    def test_programs_are_started_in_one_place_and_never_through_a_shell(self):
        users = []
        for path, tree in parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "subprocess" and node.attr in ("Popen", "run", "call", "check_call", "check_output", "getoutput"):
                    users.append(path.name)
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    self.fail(f"{path.name} passes shell=")
                if isinstance(node, ast.Attribute) and node.attr in ("killpg", "kill") and path.name != "tools.py":
                    self.fail(f"{path.name} signals a process")
        self.assertEqual(sorted(set(users)), ["tools.py"])
        self.assertEqual(users.count("tools.py"), 1)

    def test_no_install_or_sudo_command_is_ever_put_in_an_argument_list(self):
        for path, tree in parsed():
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant) and isinstance(node.elts[0].value, str):
                    self.assertNotIn(node.elts[0].value, INSTALLERS, f"{path.name}:{node.lineno}")
        # the only programs the checks ever look for are these, found by name on the search path
        names = set()
        for node in ast.walk(ast.parse((DOCTOR_DIR / "checks.py").read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("which", "_find_tool", "_system_tool") and node.args \
                    and isinstance(node.args[0], ast.Constant):
                names.add(node.args[0].value)
        self.assertEqual(names, {"ffmpeg", "ffprobe", "sysctl", "system_profiler"})

    def test_install_commands_exist_only_as_text_for_the_person(self):
        text = (DOCTOR_DIR / "checks.py").read_text()
        self.assertIn("sudo apt install ffmpeg", text)  # present as instruction text...
        for node in ast.walk(ast.parse(text)):  # ...but never inside a call that starts a program
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("run_tool", "_tool", "_run_together"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        self.assertNotRegex(sub.value, r"sudo|brew|apt|pacman|pip", f"line {node.lineno}")

    def test_the_whole_run_changes_nothing_on_disk_and_creates_no_folder(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as tools, ExitStack() as stack:
            hermetic_tools(stack, tools)
            data = Path(temp) / "station"
            before = sorted(str(p) for p in Path(temp).rglob("*"))
            run_report(Settings(data_dir=str(data), port=0))
            self.assertFalse(data.exists())
            data.mkdir(mode=0o700)
            (data / "station.sqlite3").write_bytes(b"x" * 100)
            stat_before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_mode) for p in Path(temp).rglob("*")}
            run_report(Settings(data_dir=str(data), port=0))
            stat_after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_mode) for p in Path(temp).rglob("*")}
            self.assertEqual(stat_before, stat_after)
            self.assertEqual(before, [])

    def test_a_whole_real_run_binds_nothing_but_port_zero(self):
        """Port 8765 belongs to a running station. Every bind the real checks make while reporting is on port 0."""
        asked = []
        real_bind = socket.socket.bind

        def spy(sock, address, *rest):
            asked.append(address)
            return real_bind(sock, address, *rest)

        with patch("socket.socket.bind", spy), tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as tools, ExitStack() as stack:
            hermetic_tools(stack, tools)
            run_report(Settings(data_dir=temp, port=0))
        self.assertTrue(asked, "the private-connection check really binds")
        self.assertEqual({address[1] for address in asked}, {0})
        self.assertEqual(self.ports.refused, [])

    def test_the_guard_refuses_a_fixed_port_without_binding_it(self):
        with self.assertRaises(PermissionError):
            checks._bind(8765)
        with self.assertRaises(PermissionError):
            checks._bind(9)
        self.assertEqual(self.ports.refused, [("127.0.0.1", 8765), ("127.0.0.1", 9)])
        self.ports.refused.clear()  # expected here
        self.assertIsInstance(checks._bind(0), int)

    def test_it_opens_no_connection_to_anywhere_else(self):
        connections = []

        def spy(self, address):
            connections.append(address)
            raise AssertionError("the doctor tried to connect to something")

        with patch("socket.socket.connect", spy), patch("socket.socket.connect_ex", spy), tempfile.TemporaryDirectory() as temp, \
                tempfile.TemporaryDirectory() as tools, ExitStack() as stack:
            hermetic_tools(stack, tools)
            run_report(Settings(data_dir=temp, port=0))
        self.assertEqual(connections, [])


if __name__ == "__main__":
    unittest.main()
