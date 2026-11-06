"""Fault injection against the real station: every database statement, commit and file operation inside each operation is made
to fail in turn, and the engine is held to its documented behaviour (see tests/soak_faults.py and docs/lanes/soak.md)."""
import ast
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import soak_faults as F  # noqa: E402
from myradio.station import Station, StationError  # noqa: E402


class FaultMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = F.run_matrix(quick=True)

    def setUp(self):
        # Engines that are broken on purpose leave files open; that is their fault, not a finding.  Scoped, so that the filters of every test that
        # runs afterwards (and "python -W error") are not wiped by resetwarnings.
        guard = warnings.catch_warnings()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)

    def test_every_failure_latches_or_is_refused_as_documented_and_leaves_a_recoverable_directory(self):
        self.assertTrue(self.result.ok, self.result.text())
        self.assertGreater(len(self.result.cases), 250)
        self.assertEqual(self.result.not_reached, 0)

    def test_every_kind_of_call_was_failed_in_every_operation_that_makes_it(self):
        seen = {(c.op, c.kind) for c in self.result.cases}
        for op, (_, _, kinds) in F.OPERATIONS.items():
            for kind in kinds:
                with self.subTest(op=op, kind=kind):
                    self.assertIn((op, kind), seen)

    def test_failures_made_by_the_real_system_are_survived(self):
        names = [name for name, _, _ in self.result.real]
        self.assertGreaterEqual(len(names), 6)
        for name, ok, detail in self.result.real:
            with self.subTest(name=name):
                if ok is None:  # could not be made on this machine: reported as skipped, never counted as survived
                    self.skipTest(detail)
                self.assertTrue(ok, detail)

    def test_departures_from_the_contract_are_recorded_and_still_match(self):
        known = {c.known for c in self.result.cases if c.known and not c.problems}
        self.assertEqual(known, {"F2", "F3", "F12", "F13"})

    def test_a_station_that_does_not_latch_is_caught(self):
        with mock.patch.object(Station, "_fail_safe", lambda self: None):
            result = F.run_matrix(quick=True, ops=("queue.add", "transport.start"))
        self.assertFalse(result.ok)
        self.assertTrue(any("latched=False" in p for c in result.failures for p in c.problems), result.text())

    def test_a_station_that_lets_a_partial_change_reach_the_disk_is_caught(self):
        # An engine that commits the first statement of an operation before the rest of it has run.
        original = F.ProbeDB.execute

        def early_commit(self, sql, *params):
            cursor = original(self, sql, *params)
            if sql.startswith("INSERT INTO queue") and self.probe.kind == "execute":
                self._db.commit()
            return cursor

        with mock.patch.object(F.ProbeDB, "execute", early_commit):
            result = F.run_matrix(quick=True, ops=("transport.stop",))
        self.assertFalse(result.ok)
        self.assertTrue(any("commit boundary" in p for c in result.failures for p in c.problems), result.text())

    def test_a_departure_that_gets_fixed_is_reported_so_the_list_cannot_go_stale(self):
        real = Station._store_pcm

        def fixed(self, *args, **kwargs):
            try:
                return real(self, *args, **kwargs)
            except OSError:
                with self.lock:
                    self._fail_safe()
                raise StationError(self.error) from None

        with mock.patch.object(Station, "_store_pcm", fixed):
            result = F.run_matrix(quick=True, ops=("import",))
        self.assertFalse(result.ok)
        self.assertTrue(any("known departure F3 no longer matches" in p for c in result.failures for p in c.problems), result.text())


class RealFaultsAndScratchSpace(unittest.TestCase):
    """How the matrix reports faults it could not make, how it keeps its scratch space flat, and that its verdicts do not depend on assert."""

    def test_a_real_fault_that_cannot_be_made_here_is_reported_as_skipped_not_survived(self):
        with tempfile.TemporaryDirectory() as root:
            counter = [0]

            def fresh():
                counter[0] += 1
                return os.path.join(root, f"case{counter[0]}")

            with mock.patch.object(os, "geteuid", return_value=0):  # the administrator account ignores file permissions
                real = F.real_faults(fresh)
        status = {name: ok for name, ok, _ in real}
        self.assertIsNone(status["unwritable media folder"])
        self.assertIsNone(status["unreadable media file"])
        self.assertTrue(all(ok is True for name, ok in status.items() if name not in ("unwritable media folder", "unreadable media file")), real)
        result = F.MatrixResult()
        result.real = real
        self.assertEqual(sorted(result.skipped), ["unreadable media file", "unwritable media folder"])
        self.assertIn("SKIP unwritable media folder", result.text())
        self.assertIn("2 of 6 real-system faults could not be run here", result.text())
        self.assertNotIn("ok   unwritable media folder", result.text())

    def test_a_pass_that_still_records_engine_defects_says_so_in_its_headline(self):
        result = F.run_matrix(quick=True, ops=("import",))
        self.assertTrue(result.ok, result.text())
        headline = result.text().splitlines()[0]
        self.assertTrue(headline.startswith("result: PASS with known departures "), headline)
        self.assertIn("F3", headline)
        self.assertIn("F13", headline)

    def test_the_matrix_removes_each_case_directory_when_its_case_ends(self):
        siblings = []
        real_build = F.build

        def watching(directory, op, clock):
            siblings.append(len(os.listdir(os.path.dirname(directory))))  # case directories still on disk when this case starts
            return real_build(directory, op, clock)

        with mock.patch.object(F, "build", watching):
            result = F.run_matrix(quick=True, ops=("queue.add", "transport.start"))
        self.assertTrue(result.ok, result.text())
        self.assertGreater(len(siblings), 20)  # enough cases that piled-up directories could not be missed
        self.assertLessEqual(max(siblings), 1, f"case directories piled up: {max(siblings)} on disk at once")

    def test_no_verdict_in_the_harness_depends_on_an_assert_statement(self):
        # python -O (PYTHONOPTIMIZE) removes assert statements, so a verdict made with one would silently pass.
        for name in ("soak_core.py", "soak_faults.py", "soak_realtime.py", "soak_station.py"):
            tree = ast.parse(Path(HERE, name).read_text())
            lines = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]
            self.assertEqual(lines, [], f"{name} has assert statements on lines {lines}")

    def test_the_real_fault_verdicts_still_fail_a_bad_engine_when_python_strips_asserts(self):
        script = (
            "import sys\n"
            "from unittest import mock\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "if __debug__:\n"
            "    print('this process was not started with -O'); sys.exit(3)\n"
            "import soak_faults as F\n"
            "from myradio.station import Station\n"
            "with mock.patch.object(Station, '_fail_safe', lambda self: None):  # an engine that never latches\n"
            "    result = F.run_matrix(quick=True, ops=('queue.add',))\n"
            "print('FAILED=' + '|'.join(sorted(name for name, ok, _ in result.real if ok is False)))\n")
        done = subprocess.run([sys.executable, "-B", "-O", "-c", script, HERE], capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        failed = done.stdout.split("FAILED=")[1].strip().split("|")
        for name in ("read-only database", "full database", "commit and rollback both fail"):
            self.assertIn(name, failed, done.stdout)


def process_gone(pid):
    """True once ``pid`` has ended (a process that has ended but not yet been collected by its parent counts as ended)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def wait_until(condition, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def read_line(proc, seconds):
    if select.select([proc.stdout], [], [], seconds)[0]:
        return proc.stdout.readline().decode().strip()
    return ""


# A stand-in for the process that runs the crash test: it starts the real crash run, but parks itself for as long as the helper is alive, so
# the test can end it in the middle of a cycle in the ways a ``timeout`` wrapper, an abort or a closing terminal would.
STAND_IN = r"""
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
latest = []
real_popen = subprocess.Popen
class Recording(real_popen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        latest.append(self.pid)
subprocess.Popen = Recording
import soak_faults as F
class Parked:
    def __getattr__(self, name):
        return getattr(time, name)
    def sleep(self, seconds):
        if seconds >= 0.3:  # the period during which the helper is alive and busy
            print("parked", latest[-1], flush=True)
            time.sleep(600)
        else:
            time.sleep(seconds)
F.time = Parked()
F.run_crash(cycles=3, workdir=sys.argv[2])
"""


class CrashHelperCannotBeLeftBehind(unittest.TestCase):
    """The helper process that the crash test starts used to run for ever if the test that started it was killed, writing audio into a temporary
    folder as fast as the disk allowed."""

    def setUp(self):
        self.to_kill = []
        self.addCleanup(self.clean_up)

    def clean_up(self):
        for pid in self.to_kill:  # process groups this test caused to exist, never anyone else's
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def start_stand_in(self, scratch):
        parent = subprocess.Popen([sys.executable, "-B", "-c", STAND_IN, HERE, scratch], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                  start_new_session=True)
        self.to_kill.append(parent.pid)
        self.addCleanup(parent.stdout.close)
        line = read_line(parent, 60)
        self.assertTrue(line.startswith("parked "), f"the stand-in did not reach the point where the helper is running: {line!r}")
        child = int(line.split()[1])
        self.to_kill.append(child)
        return parent, child

    def test_the_helper_ends_by_itself_when_the_process_that_started_it_is_killed(self):
        with tempfile.TemporaryDirectory() as scratch:
            parent, child = self.start_stand_in(scratch)
            self.assertFalse(process_gone(child), "the helper was not running when the test ended its parent")
            os.kill(parent.pid, signal.SIGKILL)  # cannot be caught: no cleanup of any kind runs in the parent
            parent.wait(timeout=10)
            self.assertTrue(wait_until(lambda: process_gone(child), 10), "the helper was still running 10 seconds after its parent was killed (it takes well under a second)")

    def test_the_helper_is_stopped_when_the_process_that_started_it_is_asked_to_end(self):
        with tempfile.TemporaryDirectory() as scratch:
            parent, child = self.start_stand_in(scratch)
            os.kill(parent.pid, signal.SIGTERM)  # what a ``timeout`` wrapper sends
            parent.wait(timeout=10)
            self.assertEqual(parent.returncode, 128 + signal.SIGTERM, "the crash run did not turn the request to end into an orderly exit")
            self.assertTrue(wait_until(lambda: process_gone(child), 10), "the helper was still running 10 seconds after its parent was asked to end (it takes well under a second)")

    def test_the_helper_ends_itself_after_its_time_is_up_even_if_nothing_ever_tells_it_to(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder = os.path.join(scratch, "station")
            os.mkdir(folder)
            Path(folder, F.CRASH_MARKER).write_text("made by the test\n")
            script = "import sys\nsys.path.insert(0, sys.argv[1])\nimport soak_faults as F\nF.crash_child(sys.argv[2], 1, lifetime=2)\n"
            proc = subprocess.Popen([sys.executable, "-B", "-c", script, HERE, folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
            self.to_kill.append(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.fail("the helper was still running 30 seconds after its two second lifetime")
            self.assertEqual(proc.returncode, -signal.SIGALRM)

    def test_the_helper_refuses_a_folder_the_crash_test_did_not_make(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = subprocess.Popen([sys.executable, "-B", str(Path(HERE, "soak_station.py")), "_child", "--dir", folder, "--seed", "1"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True)
            self.to_kill.append(proc.pid)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                self.fail("the helper kept running in a folder that the crash test did not make")
            self.assertEqual(proc.returncode, 2)
            self.assertIn(b"will not change anything", err)
            self.assertEqual(os.listdir(folder), [], "the helper created files in a folder it should have left alone")

    def test_a_helper_that_has_already_ended_is_never_signalled(self):
        # Once poll() has reported an exit status the process number is free to be given to some other process.
        fake = mock.Mock()
        fake.pid, fake.stdout = 424242, None
        fake.poll.return_value = 3
        with mock.patch.object(F.os, "killpg") as killpg:
            F.kill_child_group(fake)
        killpg.assert_not_called()
        fake.wait.assert_called()

    def test_run_crash_does_not_signal_a_helper_that_never_started(self):
        read_end, write_end = os.pipe()
        os.close(write_end)  # nothing will ever be written: the helper "died" before saying it was ready
        fake = mock.Mock()
        fake.pid, fake.stdout = 424243, os.fdopen(read_end, "rb")
        fake.poll.return_value = 1
        with mock.patch.object(F.subprocess, "Popen", return_value=fake), mock.patch.object(F.os, "killpg") as killpg:
            result = F.run_crash(cycles=1)
        self.assertFalse(result.ok)
        self.assertTrue(any("did not start" in p for p in result.problems), result.problems)
        killpg.assert_not_called()

    def test_a_reopened_station_that_cannot_start_a_track_is_a_failure_not_a_pass(self):
        with mock.patch.object(Station, "_next", lambda self: False):  # an engine that never manages to start a track
            result = F.run_crash(cycles=1)
        self.assertFalse(result.ok, result.text())
        self.assertTrue(any("produced silence" in p for p in result.problems), result.problems)


class CrashRecovery(unittest.TestCase):
    def test_a_killed_service_leaves_a_directory_that_reopens(self):
        result = F.run_crash(cycles=1)
        self.assertTrue(result.ok, result.text())
        self.assertTrue(result.cycles[0]["reopened"])


if __name__ == "__main__":
    unittest.main()
