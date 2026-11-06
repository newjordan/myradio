"""The doctor's bounded tool runner and text clean-up, tested against real (misbehaving) programs.

Nothing here touches the network, a GPU or any real FFmpeg. The "programs" are small Python scripts
written into a temporary directory.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from myradio.doctor.tools import MAX_TOOL_OUTPUT, ToolResult, clean_text, display_path, run_tool

from test_doctor_fixtures import HEARTBEAT, alive_after, make_program, wait_for_files

PY = sys.executable


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_quick_tool_returns_its_output_and_exit_code(self):
        result = run_tool([PY, "-c", "print('hello')"], timeout=10)
        self.assertEqual((result.ran, result.exit_code, result.output.strip(), result.ok), (True, 0, "hello", True))
        self.assertFalse(result.timed_out or result.truncated or result.cancelled)

    def test_error_output_is_merged_and_exit_codes_are_kept(self):
        result = run_tool([PY, "-c", "import sys; sys.stderr.write('oops'); sys.exit(3)"], timeout=10)
        self.assertEqual((result.ran, result.exit_code, result.output, result.ok), (True, 3, "oops", False))

    def test_a_program_that_is_not_there_or_not_allowed_never_raises(self):
        self.assertEqual(run_tool([str(self.dir / "nothing-here")], timeout=5).reason, "missing")
        plain = self.dir / "plain.txt"
        plain.write_text("not a program")
        self.assertEqual(run_tool([str(plain)], timeout=5).reason, "denied")
        self.assertEqual(run_tool([str(self.dir)], timeout=5).reason, "denied")
        for result in (run_tool([str(self.dir / "x")], timeout=5), run_tool([str(plain)], timeout=5)):
            self.assertFalse(result.ran or result.ok)

    def test_strange_arguments_are_refused_not_run(self):
        for bad in ([], "echo hi", None, 7, [None], [1], [""], ["a\0b"], [b"ls"], ["ok", ""]):
            with self.subTest(args=repr(bad)):
                result = run_tool(bad, timeout=1)
                self.assertEqual((result.ran, result.reason), (False, "invalid"))
        for bad_timeout in (float("nan"), float("inf"), "3", None, True):
            with self.subTest(timeout=repr(bad_timeout)):
                self.assertEqual(run_tool([PY, "-c", "print(1)"], timeout=bad_timeout).reason, "invalid")
        for bad_cap in (0, -5, 1.5, True, None):
            with self.subTest(max_bytes=repr(bad_cap)):
                self.assertEqual(run_tool([PY, "-c", "print(1)"], timeout=1, max_bytes=bad_cap).reason, "invalid")

    def test_no_time_left_means_nothing_is_started(self):
        marker = self.dir / "started"
        program = make_program(self.dir, "mark", f"open({str(marker)!r}, 'w').write('x')")
        for timeout in (0, -1):
            result = run_tool([program], timeout=timeout)
            self.assertEqual((result.ran, result.timed_out), (False, True))
        time.sleep(0.2)
        self.assertFalse(marker.exists())

    def test_arguments_are_never_interpreted_by_a_shell(self):
        trick = f"$(echo pwned); `id` | cat > {self.dir}/nowhere; echo 'x' && true"
        result = run_tool([PY, "-c", "import sys; print(sys.argv[1])", trick], timeout=10)
        self.assertEqual(result.output.strip(), trick)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_standard_input_is_closed(self):
        result = run_tool([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=10)
        self.assertEqual(result.output.strip(), "''")

    def test_binary_output_becomes_text_safely(self):
        result = run_tool([PY, "-c", "import sys; sys.stdout.buffer.write(bytes(range(256)))"], timeout=10)
        self.assertTrue(result.ran)
        self.assertIsInstance(result.output, str)
        self.assertIn("\ufffd", result.output)  # undecodable bytes are replaced, not fatal
        self.assertTrue(128 <= len(result.output) <= 256)

    def test_a_hung_tool_is_stopped_at_the_deadline(self):
        beat = self.dir / "hang.beat"
        program = make_program(self.dir, "hang", f"import sys\nsys.argv = ['x', {str(beat)!r}]\n" + HEARTBEAT)
        started = time.monotonic()
        result = run_tool([program], timeout=0.8)
        took = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertGreaterEqual(took, 0.75)
        self.assertLess(took, 3.0)
        self.assertFalse(alive_after(beat), "the tool itself must be stopped")

    def test_everything_a_stopped_tool_started_is_stopped_with_it(self):
        outer, inner = self.dir / "outer.beat", self.dir / "inner.beat"
        (self.dir / "heartbeat.py").write_text(HEARTBEAT)
        source = f'''
import pathlib, signal, subprocess, sys, time
signal.alarm(20)
subprocess.Popen([sys.executable, {str(self.dir / "heartbeat.py")!r}, {str(inner)!r}])
beat = pathlib.Path({str(outer)!r})
n = 0
while True:
    n += 1
    beat.write_text(str(n))
    time.sleep(0.03)
'''
        program = make_program(self.dir, "family", source)
        cancel = threading.Event()
        # stop it only once both programs are really running, however slowly this computer starts them
        watcher = threading.Thread(target=lambda: (wait_for_files([outer, inner], 15), cancel.set()), daemon=True)
        watcher.start()
        result = run_tool([program], timeout=30, cancel=cancel)
        watcher.join(5)
        self.assertTrue(result.cancelled)
        self.assertTrue(outer.exists() and inner.exists(), "both programs should have been running")
        self.assertFalse(alive_after(outer), "the tool itself must be stopped")
        self.assertFalse(alive_after(inner), "whatever the tool started must be stopped too")

    def test_a_tool_that_leaves_something_holding_its_output_does_not_hold_us_up(self):
        beat = self.dir / "leftover.beat"
        (self.dir / "heartbeat.py").write_text(HEARTBEAT)
        source = f'''
import subprocess, sys
subprocess.Popen([sys.executable, {str(self.dir / "heartbeat.py")!r}, {str(beat)!r}])
print("parent done", flush=True)
'''
        program = make_program(self.dir, "leaver", source)
        started = time.monotonic()
        result = run_tool([program], timeout=8)
        took = time.monotonic() - started
        self.assertLess(took, 3.0, "must not wait for the leftover process to close the pipe")
        self.assertEqual((result.ran, result.exit_code, result.timed_out), (True, 0, False))
        self.assertIn("parent done", result.output)
        self.assertFalse(alive_after(beat), "the leftover process must be cleaned up")

    def test_flooding_output_is_cut_off_and_the_tool_is_stopped(self):
        beat = self.dir / "flood.beat"
        source = f'''
import pathlib, signal, sys
signal.alarm(20)
beat = pathlib.Path({str(beat)!r})
n = 0
while True:
    n += 1
    beat.write_text(str(n))
    sys.stdout.write("x" * 65536)
    sys.stdout.flush()
'''
        program = make_program(self.dir, "flood", source)
        started = time.monotonic()
        result = run_tool([program], timeout=10, max_bytes=50000)
        took = time.monotonic() - started
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), 50000)
        self.assertFalse(result.ok)
        self.assertLess(took, 4.0)
        self.assertFalse(alive_after(beat), "a flooding tool must be stopped")

    def test_exactly_the_cap_is_not_truncation(self):
        result = run_tool([PY, "-c", "import sys; sys.stdout.write('y' * 1000)"], timeout=10, max_bytes=1000)
        self.assertEqual((len(result.output), result.truncated, result.ok), (1000, False, True))
        result = run_tool([PY, "-c", "import sys; sys.stdout.write('y' * 1001)"], timeout=10, max_bytes=1000)
        self.assertEqual((len(result.output), result.truncated), (1000, True))

    def test_default_cap_is_bounded(self):
        self.assertLessEqual(MAX_TOOL_OUTPUT, 1024 * 1024)

    def test_cancel_stops_a_running_tool_quickly(self):
        beat = self.dir / "cancel.beat"
        program = make_program(self.dir, "beater", f"import sys\nsys.argv = ['x', {str(beat)!r}]\n" + HEARTBEAT)
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.monotonic()
        result = run_tool([program], timeout=30, cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(alive_after(beat))

    def test_an_already_cancelled_call_starts_nothing(self):
        cancel = threading.Event()
        cancel.set()
        marker = self.dir / "started"
        program = make_program(self.dir, "mark", f"open({str(marker)!r}, 'w').write('x')")
        result = run_tool([program], timeout=5, cancel=cancel)
        self.assertEqual((result.ran, result.cancelled), (False, True))
        time.sleep(0.2)
        self.assertFalse(marker.exists())

    def test_nothing_is_left_running_after_many_calls(self):
        before = threading.active_count()
        for _ in range(20):
            run_tool([PY, "-c", "print(1)"], timeout=10)
        run_tool([PY, "-c", "import time; time.sleep(30)"], timeout=0.2)
        self.assertLessEqual(threading.active_count(), before)

    def test_every_start_failure_mode_is_reported_not_raised(self):
        for error, reason in ((OSError("too many open files"), "failed"), (ValueError("embedded null"), "invalid"),
                              (PermissionError(), "denied"), (FileNotFoundError(), "missing")):
            with self.subTest(error=type(error).__name__), patch("subprocess.Popen", side_effect=error):
                result = run_tool([PY, "-c", "pass"], timeout=1)
                self.assertEqual((result.ran, result.reason), (False, reason))

    def test_when_the_group_cannot_be_signalled_the_tool_itself_is_stopped(self):
        started = time.monotonic()
        with patch("os.killpg", side_effect=PermissionError("not allowed")):
            result = run_tool([PY, "-c", "import signal, time; signal.alarm(20); time.sleep(30)"], timeout=0.4)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(result.exit_code, -9)

    def test_reading_or_watching_that_goes_wrong_ends_the_call_without_raising(self):
        real_read = os.read

        def failing_read(fd, count):
            if count == 65536:  # the doctor's own read of the tool's output (not the one Popen makes internally)
                raise OSError("input/output error")
            return real_read(fd, count)

        with patch("os.read", failing_read):
            result = run_tool([PY, "-c", "print('hello')"], timeout=5)
        self.assertEqual((result.ran, result.output), (True, ""))
        with patch("selectors.DefaultSelector", side_effect=OSError("no selector")):
            result = run_tool([PY, "-c", "import signal, time; signal.alarm(20); time.sleep(30)"], timeout=5)
        self.assertEqual((result.ran, result.timed_out), (True, True))

    def test_a_tool_that_closes_its_output_but_will_not_finish_is_stopped(self):
        source = "import os, signal, time\nsignal.alarm(20)\nos.close(1)\nos.close(2)\ntime.sleep(30)\n"
        started = time.monotonic()
        result = run_tool([make_program(self.dir, "quiet", source)], timeout=0.6)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_result_flags_for_a_result_that_did_not_finish_cleanly(self):
        self.assertFalse(ToolResult(ran=True, exit_code=0, timed_out=True).ok)
        self.assertFalse(ToolResult(ran=True, exit_code=0, truncated=True).ok)
        self.assertFalse(ToolResult(ran=True, exit_code=0, cancelled=True).ok)
        self.assertTrue(ToolResult(ran=True, exit_code=0).ok)
        self.assertFalse(ToolResult().ok)


class CleanTextTests(unittest.TestCase):
    def test_control_and_direction_characters_cannot_reach_a_terminal(self):
        dirty = "a\x1b[31mred\x1b[0m\u202eevil\u200b\x00\x07\ud800 end"
        cleaned = clean_text(dirty, 200)
        for char in cleaned:
            self.assertNotIn(char, "\x1b\u202e\u200b\x00\x07\ud800")
        self.assertTrue(cleaned.startswith("a?[31mred") and cleaned.endswith(" end"))

    def test_white_space_collapses_unless_asked_to_keep_spacing(self):
        self.assertEqual(clean_text("  a \t b\n\n c  "), "a b c")
        self.assertEqual(clean_text("a  b", collapse=False), "a  b")
        self.assertEqual(clean_text("a\nb", collapse=False), "a?b")

    def test_long_text_is_cut_to_the_limit_with_dots(self):
        out = clean_text("x" * 1000, 50)
        self.assertEqual((len(out), out[-3:]), (50, "..."))
        self.assertEqual(clean_text("short", 50), "short")
        self.assertLessEqual(len(clean_text("y" * 100, 2)), 2 + 3)

    def test_odd_values_do_not_raise(self):
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(12), "12")
        self.assertIsInstance(clean_text(object()), str)

        class Bad:
            def __str__(self):
                raise RuntimeError("no")

        self.assertEqual(clean_text(Bad()), "")

    def test_huge_input_is_handled_quickly(self):
        started = time.monotonic()
        self.assertLessEqual(len(clean_text("z" * 5_000_000, 400)), 400)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_ordinary_unicode_is_kept(self):
        self.assertEqual(clean_text("Caf\u00e9 \u2013 \u65e5\u672c\u8a9e \U0001f3b5"), "Caf\u00e9 \u2013 \u65e5\u672c\u8a9e \U0001f3b5")


class DisplayPathTests(unittest.TestCase):
    def test_a_computer_that_cannot_say_where_home_is_changes_nothing(self):
        with patch("os.path.expanduser", side_effect=RuntimeError("no home")):
            self.assertEqual(display_path("/home/sam/x"), "/home/sam/x")

    def test_home_is_written_as_a_tilde(self):
        with patch.dict(os.environ, {"HOME": "/home/sam"}):
            self.assertEqual(display_path("/home/sam/.myradio"), "~/.myradio")
            self.assertEqual(display_path("/home/sam"), "~")
            self.assertEqual(display_path("/home/samuel/x"), "/home/samuel/x")
            self.assertEqual(display_path("/var/lib/x"), "/var/lib/x")

    def test_a_missing_or_root_home_changes_nothing(self):
        with patch.dict(os.environ, {"HOME": "/"}):
            self.assertEqual(display_path("/usr/bin"), "/usr/bin")


if __name__ == "__main__":
    unittest.main()
