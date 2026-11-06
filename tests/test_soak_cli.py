"""What the soak command line prints and refuses.

A result that a person reads (or that is saved as evidence) must not claim more than the run did: a failed run proves nothing, a clause whose
check did not run is not claimed, and a run that checked nothing is not a pass.  These tests hold the printed text and the exit status to that,
and check that a machine without time zone data does not crash the random part of the harness.
"""
import contextlib
import io
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_core as C  # noqa: E402
import soak_faults as F  # noqa: E402
import soak_realtime as R  # noqa: E402
import soak_station as S  # noqa: E402
from myradio.station import FRAMES, Station  # noqa: E402


def drop_a_frame(real):
    return lambda self, now=None: real(self, now)[4:] + bytes(4)


def claims_of(text):
    """The part of a scope statement before 'Does not prove:'."""
    return text.split("Does not prove:")[0]


class ScopeTextIsHonest(unittest.TestCase):
    def test_a_failed_run_does_not_say_what_it_proves(self):
        with mock.patch.object(Station, "tick", drop_a_frame(Station.tick)):
            result = S.run_accelerated(3, 1500, scenarios="smoke", fast_commits=True)
        self.assertFalse(result.ok)
        text = S.scope_text("accelerated", result)
        self.assertNotIn("Proves:", text)
        self.assertIn("This run FAILED", text)
        self.assertIn("Does not prove:", text)

    def test_the_command_line_prints_no_claims_after_a_failed_run_and_exits_with_status_1(self):
        out = io.StringIO()
        with mock.patch.object(Station, "tick", drop_a_frame(Station.tick)), contextlib.redirect_stdout(out):
            code = S.main(["accelerated", "--seed", "3", "--steps", "1500", "--scenarios", "smoke", "--fast-commits"])
        printed = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("FAILED audio-exact", printed)
        self.assertNotIn("Proves:", printed)
        self.assertNotIn("matched an independent statement of the rules", printed)
        self.assertIn("This run FAILED", printed)

    def test_a_passing_run_lists_the_claims_it_made(self):
        result = S.run_accelerated(1, 400, scenarios="none", fast_commits=True)
        self.assertTrue(result.ok, result.text())
        text = S.scope_text("accelerated", result)
        self.assertTrue(text.startswith("Proves: with seed 1"), text)
        self.assertIn("Does not prove:", text)
        self.assertIn("not a soak of days", text)

    def test_memory_is_claimed_only_when_it_was_measured(self):
        result = S.run_accelerated(1, 400, scenarios="none", fast_commits=True)
        samples = list(result.metrics.rss_samples) or [(0.0, 50 * 1048576)]
        result.metrics.rss_samples = samples[:3]  # too few samples: the check cannot run
        self.assertIsNone(result.harness.rss_growth_after_warmup())
        text = S.scope_text("accelerated", result)
        self.assertNotIn("resident memory", claims_of(text))
        self.assertIn("memory growth (the run was too short", text)
        result.metrics.rss_samples = [(float(i), 50 * 1048576 + i) for i in range(8)]  # enough samples, flat
        text = S.scope_text("accelerated", result)
        self.assertIn("resident memory grew", claims_of(text))
        self.assertNotIn("memory growth (the run was too short", text)

    def test_a_run_stopped_by_the_wall_clock_says_how_much_of_it_ran_and_makes_no_repeatability_claim(self):
        result = S.run_accelerated(1, 200000, scenarios="none", fast_commits=True, max_wall=0.3)
        self.assertTrue(result.ok, result.text())
        self.assertTrue(result.truncated)
        text = S.scope_text("accelerated", result)
        self.assertIn("stopped early by the wall-time limit", text.split("Does not prove:")[1])
        self.assertNotIn("same seed and step count give the same run", text)

    def test_realtime_claims_follow_the_checks_that_ran(self):
        ran_everything = {"seconds": 17, "max_lateness": 0.25, "max_drift": 0.1, "longest_stretch": 10.0, "extension": True, "slow_asserted": True, "cap_probed": True,
                          "memory_measured": True, "drift_judged": True}
        result = R.RealtimeResult()
        result.facts = dict(ran_everything)
        text = S.scope_text("realtime", result)
        for claim in ("listeners that stopped reading were dropped", "seventeenth stream was refused", "resident memory stayed under", "misbehaving extension",
                      "more than about 1.0 % would have been caught"):
            self.assertIn(claim, claims_of(text))
        result.facts.update(slow_asserted=False, cap_probed=False, memory_measured=False, extension=False, drift_judged=False)  # a run that was too short to demand them
        text = S.scope_text("realtime", result)
        for claim in ("dropped", "seventeenth", "resident memory", "misbehaving extension", "drifted by less than"):
            self.assertNotIn(claim, claims_of(text), claim)
        self.assertIn("clock drift (no unbroken stretch of the run was long enough to judge it)", text)
        self.assertIn("whether slow listeners are dropped (the run was too short to demand it)", text)
        self.assertIn("the 120 second run, which is scheduled for the hardening workflow", text)
        result.facts["seconds"] = 117
        self.assertNotIn("scheduled for the hardening workflow", S.scope_text("realtime", result))

    def test_a_failed_realtime_run_proves_nothing(self):
        result = R.RealtimeResult()
        result.facts = {"seconds": 17, "max_lateness": 0.25, "max_drift": 0.1, "longest_stretch": 10.0, "slow_asserted": True, "cap_probed": True, "memory_measured": True,
                        "drift_judged": True}
        result.problems.append("the clock ran slow")
        text = S.scope_text("realtime", result)
        self.assertTrue(text.startswith("This run FAILED"), text)
        self.assertNotIn("Proves:", text)

    def test_fault_claims_name_what_could_not_be_run_and_a_failure_claims_nothing(self):
        result = F.MatrixResult()
        case = F.CaseResult("queue.add", "execute", 1, "sqlite-io")
        case.fired = True
        result.cases = [case]
        result.real = [("read-only database", True, "ok"), ("unwritable media folder", None, "not run")]
        text = S.scope_text("faults", result)
        self.assertIn("1 of 2 failures made by the operating system were survived", claims_of(text))
        self.assertIn("1 failure(s) made by the operating system could not be run here: unwritable media folder", text)
        result.real = [("read-only database", False, "latched=False")]
        text = S.scope_text("faults", result)
        self.assertTrue(text.startswith("This run FAILED"), text)
        self.assertNotIn("Proves:", text)

    def test_crash_and_recurrence_claims_come_only_from_a_passing_result(self):
        crash = F.CrashResult()
        crash.cycles = [{"alive": 1.0, "integrity": True, "tracks": 3, "queue": 2, "orphans": 0, "staged": 0, "reopened": True}] * 2
        self.assertIn("after each of 2 forced stops", claims_of(S.scope_text("crash", crash)))
        crash.problems.append("cycle 1: could not reopen")
        self.assertNotIn("Proves:", S.scope_text("crash", crash))
        recurrence = C.RecurrenceResult(1000, [], 10, 4, 12, 0.5)
        self.assertIn("all 1,000 samples", claims_of(S.scope_text("recurrence", recurrence)))
        recurrence = C.RecurrenceResult(1000, [("UTC", "10:00", "1", 0.0, 1.0, 2.0)], 10, 4, 12, 0.5)
        self.assertNotIn("Proves:", S.scope_text("recurrence", recurrence))


class RunsThatCheckNothingAreNotPasses(unittest.TestCase):
    def usage_error(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            S.main(list(argv))
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

    def test_zero_or_negative_steps_are_refused_before_anything_runs(self):
        self.assertIn("--steps must be 1 or more", self.usage_error("accelerated", "--steps", "0", "--scenarios", "none"))
        self.assertIn("--steps must be 1 or more", self.usage_error("accelerated", "--steps", "-5"))

    def test_a_wall_time_limit_of_nothing_is_refused(self):
        self.assertIn("--max-wall must be more than 0", self.usage_error("accelerated", "--max-wall", "0"))

    def test_zero_crash_cycles_are_refused(self):
        self.assertIn("--cycles must be 1 or more", self.usage_error("crash", "--cycles", "0"))

    def test_a_real_time_budget_that_cannot_fit_the_shortest_run_is_refused_not_quietly_lengthened(self):
        message = self.usage_error("realtime", "--seconds", "2")
        self.assertIn("--seconds must be at least 7", message)
        self.assertIn("Nothing was run", message)
        self.assertIn("at least 7", self.usage_error("realtime", "--seconds", "6.5"))

    def test_the_help_text_states_the_real_time_minimum(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            S.main(["realtime", "--help"])
        self.assertIn("at least 7", out.getvalue().replace("\n", " ").replace("  ", " "))

    def test_a_run_that_verified_no_block_is_a_failure(self):
        for steps, wall in ((0, None), (5, 1e-9)):  # no steps at all; a wall-time limit that has run out before the first block
            with self.subTest(steps=steps, max_wall=wall):
                result = S.run_accelerated(5, steps, scenarios="none", fast_commits=True, max_wall=wall)
                self.assertFalse(result.ok)
                self.assertEqual(result.metrics.counters["ticks"], 0)
                self.assertEqual([f["invariant"] for f in result.failures], ["ticks-verified"])
                self.assertIn("proves nothing", result.text())
                self.assertTrue(result.text().startswith("result: FAIL"))


class WallTimeLimit(unittest.TestCase):
    def test_the_limit_is_honoured_between_the_directed_scenarios_not_only_between_daylight_saving_zones(self):
        # A limit that has run out before the first scenario ends: the rest of the directed scenarios (the daylight saving ones come last) must not start.
        result = S.run_accelerated(1, 120000, scenarios="full", fast_commits=True, max_wall=0.01)
        self.assertTrue(result.ok, result.text())
        self.assertTrue(result.truncated)
        self.assertEqual(result.metrics.checks["dst-gap-skipped"], 0)
        self.assertLess(result.metrics.counters["ticks"], 3000)

    def test_a_scenario_is_never_cut_in_half_so_a_stopped_run_never_fails_for_that_reason(self):
        for wall in (0.02, 0.1, 0.25, 0.5, 0.9):
            with self.subTest(max_wall=wall):
                result = S.run_accelerated(1, 120000, scenarios="full", fast_commits=True, max_wall=wall)
                self.assertTrue(result.ok, result.text())


class _Roll(random.Random):
    """A random generator that always rolls into the daylight saving branch of the random part of the run."""

    def random(self):
        return 0.94


class NoTimeZoneData(unittest.TestCase):
    def test_the_random_part_does_not_crash_when_the_machine_has_no_time_zones(self):
        with S.session(seed=7, fast_commits=True) as h:
            for _ in range(4):
                h.op_import(kind="music", frames=FRAMES * 3)
            h.op_start()
            h.zones = []
            h.rng = _Roll(1)
            h.max_steps = h.steps + 3
            h.drive()  # used to fail with "Cannot choose from an empty sequence"
            self.assertEqual(h.failures, [])
            self.assertGreater(h.metrics.counters["ticks"], 0)

    def test_a_whole_run_without_time_zones_passes(self):
        # Seed 6 with 6,000 steps reached that branch before the fix.
        with mock.patch.object(S, "available_zones", lambda names=None: []):
            result = S.run_accelerated(6, 6000, scenarios="smoke", fast_commits=True)
        self.assertTrue(result.ok, result.text())
        self.assertEqual(result.metrics.counters["dst_gap_scenarios"] + result.metrics.counters["programs_queued"], 0)


class ZonesThatNeverChangeTheirClocks(unittest.TestCase):
    """A machine whose time zone data lacks every zone that changes its clocks: the short run must still check recurring programs, and the checks that
    need a daylight saving change must simply not be part of what it claims (the in-suite smoke test relaxes exactly these)."""

    def test_programs_are_still_checked_and_no_daylight_saving_claim_is_made(self):
        with mock.patch.object(S, "available_zones", lambda names=None: ["UTC", "Asia/Kolkata", "Asia/Kathmandu"]):
            result = S.run_accelerated(20260511, 15000, scenarios="smoke", fast_commits=True)
        self.assertTrue(result.ok, result.text())
        c, checks = result.metrics.counters, result.metrics.checks
        self.assertGreater(c["programs_queued"], 0)
        self.assertGreater(c["programs_missed"], 0)
        self.assertGreater(checks["program-off-air"], 0)
        self.assertGreater(checks["program-next-due"], 0)
        self.assertEqual(checks["dst-gap-skipped"] + checks["dst-fold-once"], 0)
        self.assertEqual(c["dst_gap_scenarios"] + c["dst_fold_scenarios"], 0)


class CommandLineHousekeeping(unittest.TestCase):
    def test_the_crash_helper_is_not_advertised_in_the_help(self):
        # test_soak_faults shows that it refuses a folder the crash test did not make; here: the visible help does not list it
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            S.main(["--help"])
        self.assertNotIn("_child", out.getvalue())
        self.assertIn("accelerated", out.getvalue())


if __name__ == "__main__":
    unittest.main()
