"""The doctor command line: python -m myradio.doctor [--json] [--data-dir DIR] [--port PORT].

Most tests swap in canned checks so the exit status and output can be asserted exactly; a few run the real
checks (one in this process, one as a real child process) against a throwaway folder and port 0. The last class
starts real child processes with real pipes to show what happens when the report cannot be delivered.
"""
import errno
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import myradio.doctor.__main__ as cli
from myradio.doctor.checks import DEFAULT_PORT
from myradio.doctor.report import EXIT_CODES, ReportRunner

from contextlib import ExitStack

from test_doctor_fixtures import canned_spec, fast_specs, guard_ports, hermetic_tools, make_fake_tools, wait_for_files

TOP = ["schema", "myradio_version", "generated_at", "duration_ms", "overall", "summary", "counts", "partial", "checks"]
REPO = Path(__file__).resolve().parent.parent
# The one sentence said when the report cannot be delivered. Written out here so that a change of wording is noticed.
NOT_WRITTEN = ("The report could not be written because the output is closed or the disk is full. "
               "Run the command again in a normal terminal window, or free up some space.")


class Recorder:
    """Replaces the runner the command line builds, so a test can choose the checks and see the settings."""

    def __init__(self, specs):
        self.specs, self.settings, self.closed = specs, [], 0
        outer = self

        class Runner(ReportRunner):
            def run(self, settings=None, **kwargs):
                outer.settings.append(settings)
                return super().run(settings, **kwargs)

            def close(self, wait=1.0):
                outer.closed += 1
                return super().close(wait)

        self.factory = lambda: Runner(specs)


class CliCase(unittest.TestCase):
    def run_cli(self, argv, specs=None, stdout=None):
        specs = specs if specs is not None else fast_specs("ok")
        recorder = Recorder(specs)
        out, err = stdout or io.StringIO(), io.StringIO()
        with patch.object(cli, "ReportRunner", recorder.factory), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        self.recorder = recorder
        return code, out.getvalue() if hasattr(out, "getvalue") else "", err.getvalue()


class ExitStatusTests(CliCase):
    def test_the_exit_status_follows_the_worst_state(self):
        cases = ((("ok", "ok"), 0), (("ok", "unknown"), 1), (("ok", "warn"), 1), (("warn", "unknown"), 1), (("ok", "fail"), 2),
                 (("fail", "warn", "unknown", "ok"), 2), (("unknown",), 1))
        for statuses, expected in cases:
            for flag in ([], ["--json"]):
                with self.subTest(statuses=statuses, flag=flag):
                    code, out, err = self.run_cli(flag, fast_specs(*statuses))
                    self.assertEqual((code, err), (expected, ""))
                    self.assertTrue(out.endswith("\n"))

    def test_the_documented_codes(self):
        self.assertEqual(EXIT_CODES, {"ok": 0, "unknown": 1, "warn": 1, "fail": 2})
        self.assertEqual(cli.EXIT_USAGE, 64)

    def test_the_runner_is_always_closed(self):
        self.run_cli([], fast_specs("ok"))
        self.assertEqual(self.recorder.closed, 1)


class OutputTests(CliCase):
    def test_the_default_output_is_a_readable_report(self):
        code, out, _ = self.run_cli([], (canned_spec("good", "ok", "All fine.", title="Good"), canned_spec("bad", "warn", "Slightly off.", "Fix it.", title="Bad")))
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("myradio doctor: is your computer ready?\n"))
        self.assertIn("OK      Good", out)
        self.assertIn("WARN    Bad", out)
        self.assertIn("What to do: Fix it.", out)
        self.assertIn("Your computer can run myradio. 1 thing is worth a look.", out)
        self.assertNotRegex(out, r"^\s*\{", "text mode must not print JSON")

    def test_json_mode_prints_only_json_with_a_stable_shape(self):
        code, out, err = self.run_cli(["--json"], fast_specs("ok", "fail"))
        report = json.loads(out)
        self.assertEqual((code, err), (2, ""))
        self.assertEqual(list(report), TOP)
        self.assertEqual(report["overall"], "fail")
        self.assertEqual([c["id"] for c in report["checks"]], ["a", "b"])
        self.assertEqual(out, json.dumps(report, indent=2) + "\n")

    def test_json_is_ascii_even_when_the_text_is_not(self):
        code, out, _ = self.run_cli(["--json"], (canned_spec("odd", "ok", "Caf\u00e9 \u65e5\u672c"),))
        out.encode("ascii")
        self.assertEqual(json.loads(out)["checks"][0]["detail"], "Caf\u00e9 \u65e5\u672c")

    def test_text_that_the_terminal_cannot_show_is_escaped_instead_of_crashing(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", write_through=True)
        code, out, _ = self.run_cli([], (canned_spec("odd", "ok", "Caf\u00e9"),), stdout=stream)
        self.assertEqual(code, 0)
        self.assertIn(b"Caf\\xe9", stream.buffer.getvalue())

    def test_a_reader_that_goes_away_does_not_make_it_fail(self):
        class Closed(io.StringIO):
            def write(self, text):
                raise BrokenPipeError()

        code, _, _ = self.run_cli([], fast_specs("ok", "fail"), stdout=Closed())
        self.assertEqual(code, 2)

    def test_hostile_text_never_reaches_the_terminal(self):
        spec = canned_spec("evil", "warn", "x\x1b]0;owned\x07 \u202e", "y\x1b[2J", "z\x00")
        code, out, _ = self.run_cli([], (spec,))
        for char in ("\x1b", "\x07", "\x00", "\u202e"):
            self.assertNotIn(char, out)


class Refusing(io.StringIO):
    """An output that refuses what it is given, in the way a real one would."""

    def __init__(self, error, *, on="write"):
        super().__init__()
        self.error, self.on = error, on

    def write(self, text):
        if self.on == "write":
            raise self.error
        return super().write(text)

    def flush(self):
        if self.on == "flush":
            raise self.error


class OutputThatFailsTests(CliCase):
    """The exit status describes the computer, not the printing, whatever happens to the output."""

    def run_with(self, argv, out, err, specs=None):
        recorder = Recorder(specs if specs is not None else fast_specs("ok", "fail"))
        with patch.object(cli, "ReportRunner", recorder.factory), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code

    def test_a_reader_that_went_away_is_silent_and_keeps_the_status(self):
        for flag in ([], ["--json"]):
            for on in ("write", "flush"):
                with self.subTest(flag=flag, on=on):
                    err = io.StringIO()
                    self.assertEqual(self.run_with(flag, Refusing(BrokenPipeError(errno.EPIPE, "Broken pipe"), on=on), err), 2)
                    self.assertEqual(err.getvalue(), "")

    def test_any_other_trouble_with_the_output_gives_one_plain_sentence_and_keeps_the_status(self):
        troubles = {"a full disk": OSError(errno.ENOSPC, "No space left on device"), "an input/output error": OSError(errno.EIO, "Input/output error"),
                    "a closed file": ValueError("I/O operation on closed file"), "a closed descriptor": OSError(errno.EBADF, "Bad file descriptor")}
        for name, error in troubles.items():
            for on in ("write", "flush"):
                with self.subTest(trouble=name, on=on):
                    err = io.StringIO()
                    self.assertEqual(self.run_with([], Refusing(error, on=on), err), 2)
                    self.assertEqual(err.getvalue(), NOT_WRITTEN + "\n")
                    self.assertNotIn("Traceback", err.getvalue())

    def test_an_output_that_is_closed_altogether_is_handled_too(self):
        err = io.StringIO()
        self.assertEqual(self.run_with(["--json"], None, err), 2)
        self.assertEqual(err.getvalue(), NOT_WRITTEN + "\n")
        self.assertEqual(self.run_with([], None, io.StringIO(), fast_specs("ok")), 0)

    def test_nothing_raises_when_the_error_output_is_broken_as_well(self):
        full = OSError(errno.ENOSPC, "No space left on device")
        self.assertEqual(self.run_with([], None, None), 2)
        self.assertEqual(self.run_with([], Refusing(full), Refusing(full)), 2)
        self.assertEqual(self.run_with([], Refusing(BrokenPipeError()), Refusing(BrokenPipeError()), fast_specs("ok", "warn")), 1)

    def test_being_interrupted_still_answers_130_when_the_error_output_is_broken(self):
        class Interrupted(ReportRunner):
            def run(self, settings=None, **kwargs):
                raise KeyboardInterrupt()

        for err in (None, Refusing(BrokenPipeError()), Refusing(OSError(errno.ENOSPC, "full"))):
            with self.subTest(err=type(err).__name__), patch.object(cli, "ReportRunner", lambda: Interrupted(fast_specs("ok"))), \
                    redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(cli.main([]), 130)

    def test_help_and_usage_mistakes_keep_their_status_when_the_output_is_broken(self):
        self.assertEqual(self.run_with(["--help"], Refusing(BrokenPipeError()), io.StringIO()), 0)
        self.assertEqual(self.run_with(["--help"], None, io.StringIO()), 0)
        self.assertEqual(self.run_with(["--port", "nope"], io.StringIO(), Refusing(BrokenPipeError())), 64)
        self.assertEqual(self.run_with(["--port", "nope"], io.StringIO(), None), 64)

    def test_text_that_the_output_cannot_show_is_still_escaped_not_reported_as_a_failure(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", write_through=True)
        err = io.StringIO()
        self.assertEqual(self.run_with([], stream, err, (canned_spec("odd", "ok", "Caf\u00e9"),)), 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn(b"Caf\\xe9", stream.buffer.getvalue())


class ArgumentTests(CliCase):
    def test_defaults_and_the_choices_are_passed_to_the_checks(self):
        self.run_cli([])
        self.assertEqual((self.recorder.settings[0].data_dir, self.recorder.settings[0].port, self.recorder.settings[0].in_service), (None, DEFAULT_PORT, False))
        self.run_cli(["--data-dir", "/some/where", "--port", "9100"])
        self.assertEqual((self.recorder.settings[0].data_dir, self.recorder.settings[0].port), ("/some/where", 9100))
        self.run_cli(["--data-dir", ""])
        self.assertIsNone(self.recorder.settings[0].data_dir)
        self.run_cli(["--port", "0"])
        self.assertEqual(self.recorder.settings[0].port, 0)

    def test_the_port_range_is_exactly_0_to_65535(self):
        for value in ("0", "1", "8765", "65535"):
            with self.subTest(accepted=value):
                code, out, err = self.run_cli(["--port", value])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.recorder.settings[0].port, int(value))
        for value in ("65536", "65537", "99999", "-1", "-65535"):
            with self.subTest(refused=value):
                code, out, err = self.run_cli(["--port", value])
                self.assertEqual((code, out), (64, ""))
                self.assertIn("the port must be a whole number from 0 to 65535", err)
                self.assertEqual(self.recorder.settings, [])

    def test_usage_mistakes_exit_64_with_a_plain_message_and_run_nothing(self):
        for argv in (["--port", "abc"], ["--port", "-1"], ["--port", "70000"], ["--port", ""], ["--port"], ["--nonsense"], ["--json=1"], ["extra"],
                     ["--data-dir"], ["--port", "1.5"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv)
                self.assertEqual((code, out), (64, ""))
                self.assertTrue(err)
                self.assertEqual(self.recorder.settings, [])
        _, _, err = self.run_cli(["--port", "abc"])
        self.assertIn("the port must be a whole number from 0 to 65535", err)
        self.assertNotIn("Traceback", err)

    def test_help_explains_the_exit_status_and_exits_cleanly(self):
        code, out, err = self.run_cli(["--help"])
        self.assertEqual((code, err), (0, ""))
        for text in ("--json", "--data-dir", "--port", "Exit status", "never run", "nothing is installed"):
            self.assertIn(text, out)

    def test_a_hostile_folder_name_cannot_inject_anything_into_the_output(self):
        specs = (canned_spec("echo", "ok", "folder was checked"),)
        code, out, _ = self.run_cli(["--data-dir", "x\x1b[31m\nname\u202e"], specs)
        self.assertEqual(code, 0)
        for char in ("\x1b", "\u202e"):
            self.assertNotIn(char, out)

    def test_the_command_can_be_interrupted(self):
        class Interrupted(ReportRunner):
            def run(self, settings=None, **kwargs):
                raise KeyboardInterrupt()

        err = io.StringIO()
        with patch.object(cli, "ReportRunner", lambda: Interrupted(fast_specs("ok"))), redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main([])
        self.assertEqual(code, 130)
        self.assertIn("Stopped", err.getvalue())

    def test_the_sentence_for_an_undeliverable_report_is_plain(self):
        self.assertEqual(cli.NOT_WRITTEN, NOT_WRITTEN)
        for jargon in ("stdout", "pipe", "errno", "Traceback", "exception"):
            self.assertNotIn(jargon, NOT_WRITTEN)

    def test_main_is_the_console_script_entry_point(self):
        self.assertTrue(callable(cli.main))
        self.assertEqual(cli.main.__module__, "myradio.doctor.__main__")


class RealRunTests(unittest.TestCase):
    def test_the_real_checks_in_this_process(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            hermetic_tools(stack, temp)
            station = Path(temp) / "station"
            station.mkdir(mode=0o700)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--json", "--data-dir", str(station), "--port", "0"])
            report = json.loads(out.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(code, EXIT_CODES[report["overall"]])
        self.assertEqual(len(report["checks"]), 14)
        self.assertFalse(report["partial"])

    def test_it_runs_as_a_real_command_and_the_status_matches_the_report(self):
        with tempfile.TemporaryDirectory() as temp:
            station = Path(temp) / "station"
            station.mkdir(mode=0o700)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO), PATH=make_fake_tools(temp))  # the only FFmpeg it can find is the fake
            done = subprocess.run([sys.executable, "-m", "myradio.doctor", "--json", "--data-dir", str(station), "--port", "0"],
                                  capture_output=True, text=True, timeout=60, cwd=temp, env=env)
            report = json.loads(done.stdout)
            self.assertEqual(done.returncode, EXIT_CODES[report["overall"]])
            self.assertEqual(done.stderr, "")
            plain = subprocess.run([sys.executable, "-m", "myradio.doctor", "--data-dir", str(station), "--port", "0"],
                                   capture_output=True, text=True, timeout=60, cwd=temp, env=env)
            self.assertEqual(plain.returncode, done.returncode)
            self.assertIn("myradio doctor: is your computer ready?", plain.stdout)
            helped = subprocess.run([sys.executable, "-m", "myradio.doctor", "--help"], capture_output=True, text=True, timeout=60, cwd=temp, env=env)
            self.assertEqual(helped.returncode, 0)
            wrong = subprocess.run([sys.executable, "-m", "myradio.doctor", "--port", "nope"], capture_output=True, text=True, timeout=60, cwd=temp, env=env)
            self.assertEqual(wrong.returncode, 64)
            self.assertEqual(wrong.stdout, "")
            self.assertEqual(sorted(os.listdir(station)), [], "the doctor must not write into the station folder")


# A small program that runs the real command line with canned checks, but only after the test says "go". That lets a test close its end
# of a pipe BEFORE the report is printed, which no in-process test can do (Python tries to send its output a second time as it exits,
# and when that fails it replaces the exit status with 120).
CHILD = """
import os, sys, time
import myradio.doctor.__main__ as cli
from myradio.doctor.checks import CheckSpec
from myradio.doctor.report import ReportRunner

status, ready, go = sys.argv[1:4]

def check(settings=None):
    return {"id": "x", "title": "X", "status": status, "detail": "Something was found.", "hint": "" if status == "ok" else "Do the thing.", "fix": ""}

cli.ReportRunner = lambda: ReportRunner((CheckSpec("x", "X", check),))
open(ready, "w").close()
end = time.monotonic() + 30
while not os.path.exists(go) and time.monotonic() < end:
    time.sleep(0.01)
sys.exit(cli.main(sys.argv[4:]))
"""


class RealOutputTests(unittest.TestCase):
    def setUp(self):
        guard_ports(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO))
        self.count = 0

    def start(self, status, argv=(), *, wrap=(), **popen):
        """Start the child. Returns (process, ready file, go file); the child waits for the go file before it prints."""
        self.count += 1
        ready, go = self.dir / f"ready{self.count}", self.dir / f"go{self.count}"
        command = [*wrap, sys.executable, "-c", CHILD, status, str(ready), str(go), *argv]
        process = subprocess.Popen(command, env=self.env, cwd=self.dir, start_new_session=True, **popen)
        self.addCleanup(self.stop, process)
        return process, ready, go

    @staticmethod
    def stop(process):
        if process.poll() is None:  # still running: it is ours (not yet collected, so its number cannot belong to anything else)
            try:
                os.killpg(process.pid, signal.SIGKILL)  # nothing started here may outlive its test
            except OSError:
                pass
            process.wait(10)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def finish(self, process):
        error = process.stderr.read().decode("utf-8", "replace")
        return process.wait(30), error

    def test_a_reader_that_goes_away_before_the_report_is_printed_does_not_change_the_status(self):
        """Exactly the case of `myradio-doctor --json | consumer-that-has-already-exited`: it used to end with status 120 and an error message."""
        for status, expected in (("ok", 0), ("warn", 1), ("fail", 2)):
            for argv in ([], ["--json"], ["--help"]):
                with self.subTest(status=status, argv=argv):
                    reader, writer = os.pipe()
                    process, ready, go = self.start(status, argv, stdout=writer, stderr=subprocess.PIPE)
                    os.close(writer)  # the test keeps no write end
                    self.assertTrue(wait_for_files([ready], 30), "the child should be ready")
                    os.close(reader)  # the reader goes away; nothing can receive the report now
                    go.touch()
                    code, error = self.finish(process)
                    self.assertEqual((code, error), (0 if argv == ["--help"] else expected, ""))

    def test_the_real_command_survives_a_reader_that_is_already_gone(self):
        shelf = make_fake_tools(self.dir)
        station = self.dir / "station"
        station.mkdir(mode=0o700)
        for flag in ([], ["--json"]):
            with self.subTest(flag=flag):
                process = subprocess.Popen([sys.executable, "-m", "myradio.doctor", *flag, "--data-dir", str(station), "--port", "0"],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.dir, start_new_session=True,
                                           env=dict(self.env, PATH=shelf))
                self.addCleanup(self.stop, process)
                process.stdout.close()
                code, error = self.finish(process)
                self.assertIn(code, (0, 1, 2))
                self.assertEqual(error, "")

    @unittest.skipUnless(os.path.exists("/dev/full"), "this computer has no /dev/full")
    def test_a_full_disk_gives_one_plain_sentence_and_the_status_the_report_earned(self):
        for status, expected in (("ok", 0), ("warn", 1), ("fail", 2)):
            for argv in ([], ["--json"]):
                with self.subTest(status=status, argv=argv):
                    with open("/dev/full", "wb") as full:  # every write to it fails with "No space left on device"
                        process, ready, go = self.start(status, argv, stdout=full, stderr=subprocess.PIPE)
                    go.touch()
                    code, error = self.finish(process)
                    self.assertEqual(code, expected)
                    self.assertEqual(error, NOT_WRITTEN + "\n", "one plain sentence: no traceback and no message about ignored errors")

    def test_an_output_that_was_closed_before_the_start_gives_one_plain_sentence(self):
        for status, expected in (("ok", 0), ("fail", 2)):
            with self.subTest(status=status):
                # `exec "$@" >&-` starts the program with its standard output closed, as `command >&-` does
                process, ready, go = self.start(status, wrap=["/bin/sh", "-c", 'exec "$@" >&-', "sh"], stderr=subprocess.PIPE)
                go.touch()
                code, error = self.finish(process)
                self.assertEqual(code, expected, error)
                self.assertEqual(error, NOT_WRITTEN + "\n")

    def test_a_reader_that_leaves_after_the_first_line_is_fine(self):
        """`| head -1`: the whole (short) report is delivered before the reader leaves, so this has always worked.
        It is here to keep that ordinary case working; it is not the failing case of the tests above."""
        process, ready, go = self.start("fail", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        go.touch()
        first = process.stdout.readline()
        process.stdout.close()
        code, error = self.finish(process)
        self.assertEqual(first, b"myradio doctor: is your computer ready?\n")
        self.assertEqual((code, error), (2, ""))


if __name__ == "__main__":
    unittest.main()
