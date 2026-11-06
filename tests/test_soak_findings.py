"""Defects the soak harness found in the engine, as executable repros (reports/soak-findings.md).

Each test states the behaviour the product should have and is marked as an expected failure while the defect exists.  When an
engine fix lands the test starts passing, unittest reports an unexpected success, and the marker is removed in the same change.
Findings that are design decisions, observations or log noise rather than defects (F3, F6, F12, F14) have no test here; they are covered, with their current
behaviour, by the fault matrix.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_core as C  # noqa: E402
import soak_faults as F  # noqa: E402


class ReprosCleanUpAfterThemselves(unittest.TestCase):
    """A repro that has to break something to show a defect must not leave the breakage behind in the process that ran it (the unit suite
    runs every test in one process)."""

    def test_the_descriptor_repro_leaves_no_descriptor_open(self):
        counted, listed = C.fd_count(), C.open_descriptors()
        F.f11_directory_descriptor_leak()  # leaks five descriptors on purpose while the defect exists
        self.assertEqual(C.fd_count(), counted)
        self.assertEqual(C.open_descriptors() - listed, set())

    def test_closing_what_a_repro_leaked_never_touches_anyone_else_s_descriptors(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder, open(__file__, "rb") as bystander:
            number = bystander.fileno()
            self.assertEqual(C.close_descriptors_of(folder, {number}), 0)
            bystander.read(1)  # still open and usable


class EngineDefects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = F.run_findings()
        cls.found = {f.ident: f for f in cls.result.items}

    def test_every_repro_ran_without_error(self):
        self.assertEqual(self.result.errors, [])
        self.assertEqual(sorted(self.found, key=lambda i: int(i[1:])), [f"F{n}" for n in range(1, 15)])
        for f in self.result.items:
            self.assertIsInstance(f.present, bool)
            self.assertTrue(f.evidence)

    def fixed(self, ident):
        finding = self.found[ident]
        self.assertFalse(finding.present, f"{ident} {finding.title}: {finding.evidence}")

    @unittest.expectedFailure
    def test_f1_the_queue_never_holds_more_than_1000_entries(self):
        self.fixed("F1")

    @unittest.expectedFailure
    def test_f2_a_failed_delete_leaves_no_audio_file_behind(self):
        self.fixed("F2")

    @unittest.expectedFailure
    def test_f4_cancelled_schedules_are_kept_within_the_limit(self):
        self.fixed("F4")

    @unittest.expectedFailure
    def test_f5_the_soonest_pending_schedules_are_visible_and_can_be_cancelled(self):
        self.fixed("F5")

    @unittest.expectedFailure
    def test_f7_an_out_of_range_queue_id_is_refused_in_plain_words(self):
        self.fixed("F7")

    @unittest.expectedFailure
    def test_f8_a_title_with_a_lone_surrogate_is_refused_in_plain_words(self):
        self.fixed("F8")

    @unittest.expectedFailure
    def test_f9_a_station_name_cannot_make_status_unreadable(self):
        self.fixed("F9")

    @unittest.expectedFailure
    def test_f10_one_unresolvable_time_zone_does_not_stop_the_station(self):
        self.fixed("F10")

    @unittest.expectedFailure
    def test_f11_opening_a_directory_as_media_does_not_leak_a_descriptor(self):
        self.fixed("F11")

    @unittest.expectedFailure
    def test_f13_an_import_that_was_saved_keeps_its_audio_file(self):
        self.fixed("F13")


if __name__ == "__main__":
    unittest.main()
