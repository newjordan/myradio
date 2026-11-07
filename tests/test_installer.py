"""`myradio setup`: the settings it writes load cleanly, and downloads resume, verify and refuse bad files."""
import hashlib
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from myradio import installer
from myradio.generation.config import Env, parse_config
from myradio.generation.workers import KINDS


class Quiet(installer.Reporter):
    def __init__(self):
        super().__init__(as_json=True)
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""
    drop_after = None   # bytes to send before cutting the connection, once
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.calls.append(self.headers.get("Range"))
        if self.path == "/missing":
            self.send_response(404); self.end_headers(); return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        body = cls.payload[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if cls.drop_after is not None:
            n, cls.drop_after = cls.drop_after, None
            self.wfile.write(body[:n]); self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(body)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        RangeHandler.payload = os.urandom(3_000_000)
        RangeHandler.drop_after = None
        RangeHandler.calls = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def art(self, path="/file", sha=None):
        sha = sha or hashlib.sha256(RangeHandler.payload).hexdigest()
        return installer.Artifact("test file", self.url + path, sha, len(RangeHandler.payload), "models/test.bin")

    def test_written_settings_load_without_problems(self):
        model_dir = self.root / "models"
        model_dir.mkdir()
        for name in ("yue2-3b-q8_0.gguf", "yue2-vae-f16.gguf"):
            (model_dir / name).write_bytes(b"x")
        binary = self.root / "audiocpp_cli"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        for kind in ("linux-cuda", "spark", "mac-metal"):
            machine = installer.Machine(kind, "test")
            env = {"LD_LIBRARY_PATH": "/opt/libs"} if kind == "linux-cuda" else {}
            cfg = installer.worker_config(machine, binary, model_dir, env, "Test FM")
            loaded = parse_config(json.loads(json.dumps(cfg)), KINDS, Env())
            self.assertEqual(loaded.problems, (), f"{kind}: {loaded.problems}")
            self.assertIn("music", loaded.workers)
            self.assertIn("song", loaded.workers["music"].roles)
            backend = loaded.workers["music"]
            self.assertTrue(any("metal" if kind == "mac-metal" else "cuda" in str(a) for a in cfg["workers"][0]["command"]))

    def test_download_verifies_and_is_kept(self):
        rep = Quiet()
        path = installer.fetch(self.art(), self.root, rep)
        self.assertEqual(path.read_bytes(), RangeHandler.payload)
        calls = len(RangeHandler.calls)
        installer.fetch(self.art(), self.root, rep)          # second run: nothing downloaded again
        self.assertEqual(len(RangeHandler.calls), calls)

    def test_download_resumes_after_a_dropped_connection(self):
        RangeHandler.drop_after = 1_000_000
        rep = Quiet()
        path = installer.fetch(self.art(), self.root, rep, retries=3)
        self.assertEqual(path.read_bytes(), RangeHandler.payload)
        self.assertTrue(any(r and r.startswith("bytes=") for r in RangeHandler.calls), RangeHandler.calls)

    def test_bad_checksum_is_refused_and_removed(self):
        with self.assertRaises(installer.SetupError):
            installer.fetch(self.art(sha="0" * 64), self.root, Quiet())
        self.assertFalse((self.root / "models/test.bin").exists())
        self.assertFalse((self.root / "models/test.bin.part").exists())

    def test_missing_file_says_not_published(self):
        with self.assertRaises(installer.SetupError) as ctx:
            installer.fetch(self.art("/missing"), self.root, Quiet())
        self.assertIn("not published", str(ctx.exception))

    def test_existing_settings_are_backed_up(self):
        rep = Quiet()
        data = self.root / "data"
        first = installer.write_config(data, {"artist": "A"}, rep, force=False)
        installer.write_config(data, {"artist": "B"}, rep, force=False)
        backups = list(first.parent.glob("config.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), {"artist": "A"})
        self.assertEqual(json.loads(first.read_text()), {"artist": "B"})

    def test_setup_subcommand_dispatches(self):
        from myradio import server
        rc = server.main(["setup", "--check", "--json"])
        self.assertIn(rc, (0, 1))


if __name__ == "__main__":
    unittest.main()
