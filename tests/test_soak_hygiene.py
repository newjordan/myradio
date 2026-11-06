"""The soak tests must leave the test process as they found it.

The whole suite runs in one process, so a test that wipes the warning filters, or leaves threads or descriptors behind, changes what every test after it
sees.  (The mutant and fault tests used to call warnings.resetwarnings, which emptied the filter list: under ``python -W error`` every later test then
stopped turning warnings into errors.)
"""
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_soak_checker as checker  # noqa: E402
import test_soak_faults as faults  # noqa: E402


class WarningFiltersArePutBack(unittest.TestCase):
    def cases(self):
        return [("mutant tests", checker.MutantTests("test_the_unmodified_engine_passes_the_same_drive")),
                ("directed scenario mutants", checker.DirectedScenarioMutants("test_a_queue_that_accepts_one_entry_too_many")),
                ("fault matrix tests", faults.FaultMatrix("test_a_station_that_does_not_latch_is_caught"))]

    def test_the_filter_list_is_the_same_after_the_set_up_and_clean_up_of_each(self):
        for label, case in self.cases():
            with self.subTest(label), warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)  # a distinctive filter that wiping the list would remove
                before = list(warnings.filters)
                case.setUp()
                case.doCleanups()
                self.assertEqual(list(warnings.filters), before)

    def test_warnings_are_still_errors_afterwards_as_they_are_under_python_dash_W_error(self):
        for label, case in self.cases():
            with self.subTest(label), warnings.catch_warnings():
                warnings.simplefilter("error")
                case.setUp()
                case.doCleanups()
                with self.assertRaises(DeprecationWarning):
                    warnings.warn("an old way of doing it", DeprecationWarning)


if __name__ == "__main__":
    unittest.main()
