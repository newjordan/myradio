"""A few seconds of the real-time mode: the real clock thread, a real server on 127.0.0.1 port 0 and clients using it at once.

The long run (python3 tests/soak_station.py realtime) holds the clock to 250 ms of jitter and 100 ms of drift; this short one only has to prove that
the mode starts, serves every kind of client, drops a listener that stops reading, and stops cleanly, so its jitter limits are loose enough that ordinary
machine load does not fail it.  (The block count and the listener rate floors still apply, so a machine that is badly overloaded can still fail it.)
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_realtime as R  # noqa: E402
from myradio.station import FRAMES, RATE, Station  # noqa: E402

BLOCK = R.BLOCK_SECONDS


class RealtimeMicroRun(unittest.TestCase):
    def test_starts_serves_every_kind_of_client_and_stops_cleanly(self):
        result = R.run_realtime(4.0, seed=3, pollers=2, fast=2, slow=1, max_lateness=1.0, max_gap=1.0, p99_gap=0.5, max_drift=0.5, stalling_extension=True)
        self.assertTrue(result.ok, result.text())
        rows = dict(result.rows)
        self.assertIn("removed by the service", rows["listener inside the process that never reads"])
        self.assertIn("refused with a plain 400", rows["sixteen-listener limit"])
        self.assertEqual(rows["client errors"], 0)
        self.assertTrue(result.facts["drift_judged"], "the short run could not judge drift at all")

    def test_a_budget_shorter_than_the_clients_need_is_refused_not_quietly_lengthened(self):
        with self.assertRaises(ValueError) as caught:
            R.run_realtime(2.0)
        self.assertIn("at least 4 seconds", str(caught.exception))
        with self.assertRaises(ValueError):
            R.run_realtime(6.0, overhead=3.0)  # 3 seconds left for the clients


class SlowStation(Station):
    """An engine whose clock thread paces itself at 5 % over a block: every block is a little late and the lateness adds up."""

    def _run(self):
        deadline = time.monotonic()
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                with self.lock:
                    self._fail_safe()
            deadline += FRAMES / RATE * 1.05
            if deadline < time.monotonic() - 0.5:
                deadline = time.monotonic()
            self.stop_event.wait(max(0, deadline - time.monotonic()))


class ClockDriftIsCaught(unittest.TestCase):
    def test_a_clock_that_runs_five_percent_slow_fails_even_though_every_block_stays_close_to_the_median_schedule(self):
        with mock.patch.object(R, "Station", SlowStation):
            # Loose jitter limits, exactly as the micro run above; only the drift limit (default) is in force.
            result = R.run_realtime(4.0, seed=3, pollers=1, fast=1, slow=0, max_lateness=1.0, max_gap=1.0, p99_gap=0.5, stalling_extension=False, stream_cap=False)
        self.assertFalse(result.ok, result.text())
        self.assertTrue(any("ran slow" in problem for problem in result.problems), result.problems)
        self.assertGreater(result.facts["longest_stretch"], 3.0)

    def test_the_healthy_clock_drifts_by_almost_nothing(self):
        result = R.run_realtime(4.0, seed=3, pollers=1, fast=1, slow=0, max_lateness=1.0, max_gap=1.0, p99_gap=0.5, stalling_extension=False, stream_cap=False)
        self.assertTrue(result.ok, result.text())
        self.assertFalse(any("drift" in note for note in result.notes), result.notes)


def train(count, spacing=BLOCK, start=1000.0, jitter=()):
    """Block times as the clock thread would hand them out: one every ``spacing`` seconds, each moved by the matching entry of ``jitter``."""
    return [start + i * spacing + (jitter[i % len(jitter)] if jitter else 0.0) for i in range(count)]


class ClockJudgement(unittest.TestCase):
    """The arithmetic that turns block times into jitter and drift, on trains of block times made by hand."""

    def test_a_perfect_clock_has_no_jitter_and_no_drift(self):
        timing = R.judge_clock(train(170))
        self.assertAlmostEqual(timing.worst_lateness, 0.0, places=9)
        drift, seconds = timing.worst_drift
        self.assertAlmostEqual(drift, 0.0, places=9)
        self.assertAlmostEqual(seconds, 169 * BLOCK, places=6)

    def test_ordinary_jitter_is_not_drift(self):
        timing = R.judge_clock(train(170, jitter=(0.012, -0.008, 0.03, 0.0, -0.02, 0.005)))
        self.assertGreater(timing.worst_lateness, 0.02)
        self.assertLess(abs(timing.worst_drift[0]), 0.01)

    def test_a_steady_two_percent_error_is_drift_whose_jitter_is_only_half_as_large(self):
        timing = R.judge_clock(train(170, spacing=BLOCK * 1.02))
        drift, seconds = timing.worst_drift
        self.assertAlmostEqual(drift, 0.02 * 169 * BLOCK, places=6)  # 338 ms over 17 s: positive, the clock ran slow
        self.assertAlmostEqual(timing.worst_lateness, drift / 2, delta=0.01)  # what the old measure saw: half of it, under the 250 ms limit
        self.assertLess(timing.worst_lateness, R.MAX_LATENESS)
        self.assertGreater(abs(drift), R.MAX_DRIFT)

    def test_a_clock_that_runs_fast_has_negative_drift(self):
        drift, _ = R.judge_clock(train(170, spacing=BLOCK * 0.98)).worst_drift
        self.assertLess(drift, -R.MAX_DRIFT)

    def test_a_stall_splits_the_run_into_stretches_that_are_judged_apart(self):
        before = train(60)
        after = train(80, start=before[-1] + 1.2 + 0.5)  # the engine restarts its schedule after a long stall
        timing = R.judge_clock(before + after, hold=(before[-1] + 0.1, before[-1] + 1.3))
        self.assertEqual(len(timing.drifts), 2)
        self.assertLess(abs(timing.worst_drift[0]), 1e-6)
        self.assertGreater(max(timing.across), 1.2)
        # Judged as one run, the restart of the schedule would look like an enormous drift
        self.assertGreater(abs(R.judge_clock(before + after).worst_drift[0]), 1.0)

    def test_a_stretch_too_short_to_judge_is_not_judged(self):
        timing = R.judge_clock(train(R.MIN_BLOCKS_FOR_DRIFT - 1, spacing=BLOCK * 1.5))
        self.assertEqual(timing.drifts, [])
        self.assertEqual(timing.worst_drift, (0.0, 0.0))


class RealtimeCleansUpAfterItself(unittest.TestCase):
    def leftovers(self, before):
        return sorted(t.name for t in threading.enumerate() if t.name not in before and t.is_alive() and t.name.startswith(("myradio-", "soak-")))

    def test_a_failure_while_starting_the_server_stops_the_clock_thread(self):
        before = {t.name for t in threading.enumerate()}

        def refuse(*args, **kwargs):
            raise OSError("the port is not available")

        with mock.patch.object(R, "Server", refuse), self.assertRaises(OSError):
            R.run_realtime(4.0)
        deadline = time.monotonic() + 5
        while self.leftovers(before) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.leftovers(before), [], "the clock thread kept running after the run failed")

    def test_a_failure_in_the_middle_of_the_run_stops_the_clients_the_server_and_the_clock(self):
        before = {t.name for t in threading.enumerate()}

        def broken(run):
            raise RuntimeError("a fault in the harness")

        with mock.patch.object(R, "probe_stream_cap", broken), self.assertRaises(RuntimeError):
            R.run_realtime(5.0, seed=2, pollers=1, fast=1, slow=1, stalling_extension=False, max_lateness=1.0, max_gap=1.0, p99_gap=0.5, max_drift=0.5)
        deadline = time.monotonic() + 8
        while self.leftovers(before) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.leftovers(before), [], "threads of the failed run were still alive")


if __name__ == "__main__":
    unittest.main()
