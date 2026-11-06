"""A fast, deterministic slice of the accelerated soak, run with the normal suite.

The same harness that runs for minutes under ``python3 tests/soak_station.py accelerated`` runs here for a few thousand
simulated steps with a fixed seed.  It plays real audio blocks through the real station on a simulated clock and checks every
block, every queue change, every scheduled event, restart and quarantine against an independent statement of the rules.
It also has to prove it did something: the run must have started tracks, admitted events across daylight saving changes,
quarantined and recovered damaged files, and restarted the service.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_core as C  # noqa: E402
import soak_station as S  # noqa: E402
from myradio.station import Station  # noqa: E402

SEED = 20260511
STEPS = 15000
# The long runs allow 64 MiB of growth after warm-up.  This run is short, so a leak shows only if the limit is far lower: a clean run grows by
# 0.4 to 3 MiB here, so 24 MiB leaves a wide margin and still fails an engine that keeps 4 KB for every block it plays.
MEMORY_LIMIT_MB = 24.0
MEBIBYTE = 1024 * 1024
ZONES = C.available_zones()
DST_POOL = [zone for zone in C.DST_ZONES if zone in ZONES][:2]  # the zones the daylight saving scenarios use at this level


@unittest.skipUnless(ZONES, "this machine has no time zone database, so recurring programs cannot be tested here")
class AcceleratedSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # fast_commits: no fsync per commit.  The checks read committed data through a second connection, so this changes the
        # speed and nothing they can observe (the long runs keep the engine's own setting by default).
        cls.first = S.run_accelerated(SEED, STEPS, scenarios="smoke", fast_commits=True, rss_growth_mb=MEMORY_LIMIT_MB)
        cls.second = S.run_accelerated(SEED, STEPS, scenarios="smoke", fast_commits=True, rss_growth_mb=MEMORY_LIMIT_MB)

    def test_every_invariant_holds(self):
        self.assertTrue(self.first.ok, self.first.text())
        self.assertFalse(self.first.truncated)

    def test_a_fixed_seed_gives_the_same_run_every_time(self):
        self.assertEqual(self.first.digest, self.second.digest)
        self.assertEqual(self.first.metrics.counters, self.second.metrics.counters)
        self.assertEqual(self.first.metrics.ops, self.second.metrics.ops)

    def test_another_seed_is_another_run(self):
        a = S.run_accelerated(1, 1500, scenarios="none", fast_commits=True)
        b = S.run_accelerated(2, 1500, scenarios="none", fast_commits=True)
        self.assertTrue(a.ok and b.ok, a.text() + b.text())
        self.assertNotEqual(a.digest, b.digest)

    def test_the_run_exercised_what_it_claims_to_check(self):
        c = self.first.metrics.counters
        self.assertGreaterEqual(c["ticks"], STEPS * 0.9)
        self.assertGreater(c["tracks_started"], 300)
        self.assertGreater(c["tracks_finished"], 300)
        self.assertGreater(c["accepted"], 60)
        self.assertGreater(c["rejected_as_expected"], 3)
        self.assertGreater(c["restarts"], 2)
        self.assertGreater(c["quarantined"], 5)
        if not c["midplay_not_possible"]:  # a file system with huge blocks cannot make the cut; the scenario says so instead of failing
            self.assertGreaterEqual(c["playback_failures"], 1)  # a file cut short while it was playing
        self.assertGreater(c["schedules_queued"], 0)
        self.assertGreater(c["schedules_missed"], 0)
        # Recurring programs: the daylight saving scenarios add many; without them the directed off-air scenario still queues and misses some.
        self.assertGreater(c["programs_queued"], 3 if len(DST_POOL) == 2 else 0)
        self.assertGreater(c["programs_missed"], 0)
        self.assertGreater(c["hops"], 10)
        self.assertGreater(c["audio_frames_verified"], 100 * C.FRAMES)
        self.assertGreater(c["deep_checks"], 100)
        self.assertGreater(c["sweeps"], 30)

    def test_daylight_saving_gaps_and_repeats_were_played_through(self):
        if "America/New_York" not in ZONES or "Europe/London" not in ZONES:
            self.skipTest("this machine has no time zone database for the zones the scenarios use")
        c = self.first.metrics.counters
        self.assertGreaterEqual(c["dst_gap_scenarios"], 2)
        self.assertGreaterEqual(c["dst_fold_scenarios"], 2)

    def test_the_checks_that_matter_were_evaluated_many_times(self):
        checks = self.first.metrics.checks
        names = ["audio-exact", "queue-law", "queue-order", "program-admission", "program-next-due", "schedule-admission", "media-verdict", "stale-boundary",
                 "restart-queue", "persisted-state", "status-json", "file-descriptors", "threads", "elapsed-bounds", "history-limit", "queue-limit",
                 "schedule-pruning", "clock-back-no-refire", "autofill-rotation", "silence-is-justified", "waiting-is-silent", "waiting-ends",
                 "queue-fills-to-limit", "program-off-air", "ticks-verified", "memory-growth"]
        if not self.first.metrics.counters["midplay_not_possible"]:
            names.append("midplay-failure")
        if len(DST_POOL) == 2:  # the daylight saving checks need two zones that change their clocks
            names += ["dst-gap-skipped", "dst-fold-once"]
        for name in names:
            with self.subTest(check=name):
                self.assertGreater(checks[name], 0, f"{name} never ran")
        self.assertEqual(sum(self.first.metrics.failed.values()), 0)

    def test_resources_stayed_flat(self):
        m = self.first.metrics
        self.assertLessEqual(m.thread_max, m.thread_min + 0)
        self.assertLessEqual(m.fd_max - m.fd_min, 1)  # the one descriptor of the file being played

    def test_memory_growth_was_measured_and_stayed_small(self):
        # The memory check needs enough samples; a run this short used to take too few, so the check never ran and a large leak passed.
        growth = self.first.harness.rss_growth_after_warmup()
        self.assertIsNotNone(growth, f"only {len(self.first.metrics.rss_samples)} memory samples were taken")
        self.assertGreater(self.first.metrics.checks["memory-growth"], 0)
        self.assertLess(growth, MEMORY_LIMIT_MB * MEBIBYTE)

    def test_an_engine_that_keeps_a_little_of_every_block_fails_the_short_run(self):
        real = Station.tick

        def leaky(self, now=None):
            self.__dict__.setdefault("_kept", []).append(bytes(16384))  # about 90 MB over this run
            return real(self, now)

        with mock.patch.object(Station, "tick", leaky):
            result = S.run_accelerated(SEED, 6000, scenarios="smoke", fast_commits=True, rss_growth_mb=MEMORY_LIMIT_MB)
        self.assertFalse(result.ok)
        self.assertEqual({f["invariant"] for f in result.failures}, {"memory-growth"}, result.text())


if __name__ == "__main__":
    unittest.main()
