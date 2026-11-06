"""Generation: the subprocess runner and the HTTP runner against misbehaving fakes."""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import test_generation_fakes as fakes
except ImportError:
    from tests import test_generation_fakes as fakes

from myradio.generation.errors import Cancelled, WorkerError
from myradio.generation.runners import HttpRunner, SubprocessRunner, build_env, tree_bytes


class ProcessCase(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.work = self.dir / "work"
        self.notes = self.dir / "notes"
        self.work.mkdir()
        self.notes.mkdir()
        self.runner = SubprocessRunner()

    def script(self, name):
        return fakes.write_script(self.dir, name)

    def run_script(self, name, *extra, deadline_s=20.0, **kw):
        argv = [sys.executable, self.script(name), "--out", str(self.work / "out.wav"), "--notes", str(self.notes), *extra]
        env = build_env(("PATH", "HOME"), {}, self.work)
        return self.runner.run(argv, cwd=self.work, env=env, deadline_s=deadline_s, **kw)

    def note(self, name):
        path = self.notes / name
        return path.read_text() if path.exists() else ""

    def pids(self):
        return [int(x) for x in self.note("pid.txt").split()]

    def assertDead(self, *pids):
        for pid in pids:
            self.assertTrue(fakes.wait_for(lambda pid=pid: not fakes.pid_alive(pid), 5), f"process {pid} is still running")


class SubprocessTests(ProcessCase):
    def test_success_captures_output_and_runs_in_the_private_directory(self):
        with patch.dict(os.environ, {"MYRADIO_TEST_SECRET": "hunter2"}):
            result = self.run_script("ok", "--seconds", "0.5")
        self.assertEqual((result.returncode, result.reason, result.killed, result.leaked), (0, "exit", False, False))
        self.assertIn("rendering", result.stderr)
        self.assertTrue((self.work / "out.wav").stat().st_size > 1000)
        self.assertEqual(os.path.realpath(self.note("cwd.txt").strip()), os.path.realpath(self.work))
        env = eval(self.note("env.txt"))
        self.assertNotIn("MYRADIO_TEST_SECRET", env)
        self.assertIn("TMPDIR", env)
        self.assertLess(len(env), 12, "only the passthrough list and the private temp variables are visible")

    def test_arguments_are_never_interpreted_by_a_shell(self):
        marker = self.dir / "pwned"
        nasty = f"$(touch {marker}); `touch {marker}` && touch {marker} | cat > {marker}; \"quote' \\n"
        result = self.run_script("ok", "--tags", nasty, "--seconds", "0.3")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertIn(nasty, self.note("args.txt").strip())

    def test_standard_input_is_closed(self):
        result = self.runner.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], cwd=self.work, env=build_env(("PATH",), {}, self.work),
                                 deadline_s=10)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "''"))

    def test_nonzero_exit_is_reported_with_the_tail_of_the_error_output(self):
        result = self.run_script("fail")
        self.assertEqual((result.returncode, result.reason), (3, "exit"))
        self.assertIn("model could not be loaded", result.stderr)

    def test_a_program_that_writes_nothing_is_not_the_runners_concern(self):
        result = self.run_script("nothing")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.work / "out.wav").exists())

    def test_a_hanging_program_is_stopped_at_the_deadline(self):
        began = time.monotonic()
        result = self.run_script("hang", deadline_s=0.8, grace_s=2.0)
        self.assertEqual(result.reason, "timeout")
        self.assertLess(time.monotonic() - began, 8.0)
        self.assertFalse(result.killed, "SIGTERM was enough")
        self.assertDead(*self.pids())

    def test_a_program_that_ignores_terminate_is_killed(self):
        began = time.monotonic()
        result = self.run_script("ignore_term", deadline_s=0.5, grace_s=0.5)
        self.assertEqual((result.reason, result.killed, result.leaked), ("timeout", True, False))
        self.assertLess(time.monotonic() - began, 8.0)
        self.assertEqual(result.returncode, -9)
        self.assertDead(*self.pids())

    def test_child_processes_die_with_the_program(self):
        result = self.run_script("child", deadline_s=0.6, grace_s=0.5)
        self.assertEqual(result.reason, "timeout")
        leader, child = self.pids()
        self.assertTrue(result.killed, "the child ignores SIGTERM, so the group needed SIGKILL")
        self.assertDead(leader, child)

    def test_a_child_left_behind_after_a_normal_exit_is_stopped(self):
        began = time.monotonic()
        result = self.run_script("leave_child", deadline_s=20, grace_s=1.0)
        self.assertEqual((result.returncode, result.reason), (0, "exit"))
        self.assertLess(time.monotonic() - began, 10.0, "must not wait for the leftover child to finish its own sleep")
        self.assertDead(*self.pids())

    def test_cancel_stops_the_program_quickly(self):
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        began = time.monotonic()
        result = self.run_script("hang", deadline_s=30, cancel=cancel, grace_s=2.0)
        self.assertEqual(result.reason, "cancelled")
        self.assertLess(time.monotonic() - began, 6.0)
        self.assertDead(*self.pids())

    def test_a_program_that_writes_far_too_much_is_stopped_and_bounded(self):
        began = time.monotonic()
        result = self.run_script("huge", deadline_s=30, max_dir_bytes=5 * 1024 * 1024, grace_s=1.0)
        self.assertEqual(result.reason, "too_large")
        self.assertLess(time.monotonic() - began, 15.0)
        self.assertLess(tree_bytes(self.work, 10 ** 12), 120 * 1024 * 1024)

    def test_captured_output_is_a_bounded_ring_that_keeps_the_end(self):
        runner = SubprocessRunner(ring_bytes=1000)
        code = "import sys; sys.stderr.write('x' * 3000000 + 'THE-END'); sys.stdout.write('o' * 5000 + 'OUT-END')"
        result = runner.run([sys.executable, "-c", code], cwd=self.work, env=build_env(("PATH",), {}, self.work), deadline_s=30)
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stderr), 1000)
        self.assertTrue(result.stderr.endswith("THE-END"))
        self.assertTrue(result.stdout.endswith("OUT-END") and len(result.stdout) <= 1000)

    def test_a_program_that_cannot_start_is_a_plain_error(self):
        for argv in (["/nonexistent/program"], [str(self.dir)], [str(self.dir / "notexec")]):
            (self.dir / "notexec").write_text("x")
            with self.subTest(argv=argv), self.assertRaises(WorkerError) as caught:
                self.runner.run(argv, cwd=self.work, env={}, deadline_s=5)
            self.assertEqual(caught.exception.code, "worker_failed")
            self.assertNotIn("/nonexistent", caught.exception.message)

    def test_bad_arguments_are_refused_before_anything_starts(self):
        for argv in ("echo hi", [], [sys.executable, "a\x00b"], [sys.executable, 5], None):
            with self.subTest(argv=repr(argv)), self.assertRaises(WorkerError):
                self.runner.run(argv, cwd=self.work, env={}, deadline_s=5)

    def test_build_env_keeps_only_what_was_asked_for(self):
        with patch.dict(os.environ, {"KEEP_ME": "1", "DROP_ME": "2", "SECRET_TOKEN": "s3cret"}, clear=False):
            env = build_env(["KEEP_ME", "NOT_SET"], {"FIXED": "yes"}, self.work)
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertEqual(env["FIXED"], "yes")
        self.assertNotIn("DROP_ME", env)
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("NOT_SET", env)
        self.assertEqual({env["TMPDIR"], env["TEMP"], env["TMP"]}, {str(self.work)})

    def test_many_runs_leave_no_threads_or_processes_behind(self):
        before = {t.name for t in threading.enumerate()}
        for _ in range(5):
            self.run_script("ok", "--seconds", "0.3")
        self.assertEqual({t.name for t in threading.enumerate()} - before, set())


class HttpCase(unittest.TestCase):
    def setUp(self):
        fakes.arm_watchdog(self)
        self.server = fakes.FakeSpeechServer()
        self.addCleanup(self.server.close)
        self.runner = HttpRunner()

    def post(self, mode, deadline_s=5.0, limit=20 * 1024 * 1024, **kw):
        self.server.mode = mode
        return self.runner.request("POST", "127.0.0.1", self.server.port, "/v1/audio/speech", body=b'{"input": "hi"}',
                                   headers={"Content-Type": "application/json"}, deadline_s=deadline_s, max_response_bytes=limit, **kw)

    def failure(self, mode, **kw):
        with self.assertRaises(WorkerError) as caught:
            self.post(mode, **kw)
        return caught.exception


class HttpRunnerTests(HttpCase):
    def test_a_normal_answer(self):
        response = self.post("ok44")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.body[:4], b"RIFF")
        request = self.server.speech_requests()[0]
        self.assertEqual((request["method"], request["path"], request["body"]), ("POST", "/v1/audio/speech", b'{"input": "hi"}'))
        self.assertEqual(request["headers"]["connection"], "close")
        self.assertEqual(request["headers"]["accept-encoding"], "identity")
        self.assertTrue(request["headers"]["user-agent"].startswith("myradio-generation"))

    def test_a_get_request(self):
        response = self.runner.request("GET", "127.0.0.1", self.server.port, "/v1/models", deadline_s=5, max_response_bytes=10000)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["server"], "fake-audiocpp/0.1")

    def test_chunked_unframed_and_interim_answers_are_read(self):
        sizes = {mode: len(self.post(mode).body) for mode in ("ok44", "chunked", "no_length_ok", "interim_100")}
        self.assertEqual(sizes["chunked"], len(fakes.tone_wav(1.2)))
        self.assertGreater(sizes["no_length_ok"], 1000)
        self.assertEqual(sizes["interim_100"], len(fakes.tone_wav(1.0)))

    def test_an_error_status_is_returned_not_raised(self):
        for mode, status in (("error500", 500), ("busy503", 503), ("forbidden", 403)):
            with self.subTest(mode):
                self.assertEqual(self.post(mode).status, status)

    def test_redirects_are_not_followed(self):
        other = fakes.FakeSpeechServer()
        self.addCleanup(other.close)
        self.server.mode = "redirect"
        response = self.post("redirect")
        self.assertEqual(response.status, 302)
        self.assertEqual(other.requests, [])
        self.assertIn("location", response.headers)

    def test_the_deadline_is_absolute_even_when_the_server_drips_bytes(self):
        for mode in ("drip_headers", "drip_body", "hang"):
            with self.subTest(mode):
                began = time.monotonic()
                error = self.failure(mode, deadline_s=0.8)
                self.assertEqual(error.code, "worker_timeout")
                self.assertLess(time.monotonic() - began, 4.0)
                self.assertTrue(error.message and "Traceback" not in error.message)

    def test_truncated_and_malformed_answers_are_plain_errors(self):
        for mode in ("truncated", "not_http", "header_flood", "dup_length", "te_and_cl", "gzip", "bad_chunk"):
            with self.subTest(mode):
                error = self.failure(mode)
                self.assertEqual(error.code, "bad_output", mode)
                self.assertTrue(error.retryable)

    def test_an_oversize_answer_is_refused_without_reading_it_all(self):
        began = time.monotonic()
        error = self.failure("oversize_header", limit=1024 * 1024)
        self.assertEqual((error.code, error.retryable), ("output_too_large", False))
        self.assertLess(time.monotonic() - began, 5.0)
        began = time.monotonic()
        error = self.failure("oversize_stream", limit=2 * 1024 * 1024)
        self.assertEqual(error.code, "output_too_large")
        self.assertLess(time.monotonic() - began, 6.0)

    def test_a_closed_port_is_unreachable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(WorkerError) as caught:
            self.runner.request("GET", "127.0.0.1", port, "/", deadline_s=3, max_response_bytes=1000)
        self.assertEqual(caught.exception.code, "worker_unreachable")
        self.assertIn("Check that it is running", caught.exception.message)

    def test_only_this_computer_is_allowed_unless_the_worker_opted_in(self):
        for host in ("192.0.2.1", "10.1.2.3", "::ffff:8.8.8.8", "2001:db8::1"):
            began = time.monotonic()
            with self.subTest(host), self.assertRaises(WorkerError) as caught:
                self.runner.request("GET", host, 80, "/", deadline_s=3, max_response_bytes=1000)
            self.assertEqual((caught.exception.code, caught.exception.retryable), ("worker_blocked", False))
            self.assertLess(time.monotonic() - began, 2.0, "no connection attempt is made")
        self.assertEqual(self.runner.request("GET", "127.0.0.1", self.server.port, "/", deadline_s=3, max_response_bytes=10000, allow_remote=True).status, 200)

    def test_localhost_by_name_works(self):
        self.assertEqual(self.runner.request("GET", "localhost", self.server.port, "/", deadline_s=5, max_response_bytes=10000).status, 200)

    def test_an_ipv6_loopback_address_works(self):
        try:
            probe = socket.socket(socket.AF_INET6)
            probe.bind(("::1", 0))
            probe.close()
        except OSError:
            self.skipTest("this computer has no IPv6 loopback address")

        class V6(fakes._Server):
            address_family = socket.AF_INET6

        server = V6(("::1", 0), fakes._Handler)
        server.requests, server.stop_event, server.mode_for = [], threading.Event(), (lambda method, path: "ok44")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            response = self.runner.request("POST", "::1", server.server_address[1], "/v1/audio/speech", body=b"{}", deadline_s=5, max_response_bytes=10 ** 7)
            self.assertEqual((response.status, response.body[:4]), (200, b"RIFF"))
            self.assertEqual(server.requests[0]["headers"]["host"], f"[::1]:{server.server_address[1]}")
        finally:
            server.stop_event.set()
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_cancel_abandons_a_waiting_request_quickly(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        began = time.monotonic()
        with self.assertRaises(Cancelled):
            self.post("hang", deadline_s=30, cancel=cancel)
        self.assertLess(time.monotonic() - began, 5.0)

    def test_bad_requests_are_refused_locally(self):
        for kwargs in ({"method": "DELETE", "path": "/"}, {"method": "GET", "path": "no-slash"}, {"method": "GET", "path": "/a b"},
                       {"method": "GET", "path": "/", "headers": {"X-A": "a\r\nInjected: 1"}}, {"method": "GET", "path": "/", "headers": {"Bad Name": "x"}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WorkerError):
                self.runner.request(kwargs.pop("method"), "127.0.0.1", self.server.port, kwargs.pop("path"), deadline_s=2, max_response_bytes=1000, **kwargs)
        self.assertEqual(self.server.requests, [])

    def test_no_threads_are_left_behind(self):
        before = {t.name for t in threading.enumerate() if t.name.startswith("myradio-gen")}
        for mode in ("ok44", "error500", "truncated"):
            try:
                self.post(mode)
            except WorkerError:
                pass
        self.assertEqual({t.name for t in threading.enumerate() if t.name.startswith("myradio-gen")}, before)


if __name__ == "__main__":
    unittest.main()
