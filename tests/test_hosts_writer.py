"""The optional writing assistant: settings, the chat-completions call, and the extension wiring."""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_hosts_support import FULL, HostsCase  # noqa: E402

from myradio.writer import WriterError, answer_text, make_complete, parse_writer  # noqa: E402

GOOD = ("Up next, Paper Lantern Weather by Marble Hours. It feels slow and glowing to me, so let it play "
        "for a little while.")


class FakeChat:
    """A loopback OpenAI-style chat server that records what it was sent."""

    def __init__(self, reply=GOOD, status=200, think=False):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                text = ("<think>planning the link</think>" + reply) if think else reply
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SettingsTests(unittest.TestCase):
    def test_a_loopback_server_and_model_are_enough(self):
        settings, notes = parse_writer({"base_url": "http://127.0.0.1:8001", "model": "treebeard"})
        self.assertEqual(settings["url"], "http://127.0.0.1:8001/v1/chat/completions")
        self.assertEqual((settings["model"], settings["timeout_s"], settings["max_tokens"]), ("treebeard", 20.0, 400))
        self.assertTrue(settings["disable_thinking"])
        self.assertEqual(notes, [])

    def test_a_remote_server_needs_allow_remote(self):
        with self.assertRaises(WriterError):
            parse_writer({"base_url": "http://192.168.1.20:8001", "model": "m"})
        settings, _ = parse_writer({"base_url": "https://writer.example", "model": "m", "allow_remote": True})
        self.assertEqual(settings["url"], "https://writer.example/v1/chat/completions")

    def test_bad_settings_are_refused_with_a_plain_reason(self):
        bad = [5, {"model": "m"}, {"base_url": "ftp://127.0.0.1", "model": "m"},
               {"base_url": "http://user:pw@127.0.0.1:8001", "model": "m"},
               {"base_url": "http://127.0.0.1:8001/v1", "model": "m"},
               {"base_url": "http://127.0.0.1:8001", "model": "has space"},
               {"base_url": "http://127.0.0.1:8001", "model": "m", "timeout_s": 0},
               {"base_url": "http://127.0.0.1:8001", "model": "m", "max_tokens": 10.5},
               {"base_url": "http://127.0.0.1:8001", "model": "m", "path": "/../secret"},
               {"base_url": "http://127.0.0.1:8001", "model": "m", "disable_thinking": "yes"}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(WriterError) as caught:
                parse_writer(raw)
            self.assertTrue(str(caught.exception).startswith(("writer.", "The writer")))

    def test_unknown_settings_are_noted_not_fatal(self):
        _, notes = parse_writer({"base_url": "http://localhost:8001", "model": "m", "colour": "blue"})
        self.assertEqual(len(notes), 1)
        self.assertIn("colour", notes[0])


class AnswerTests(unittest.TestCase):
    def test_reasoning_blocks_are_removed(self):
        raw = json.dumps({"choices": [{"message": {"content": "<think>\nhmm\n</think>\n\nHello there."}}]}).encode()
        self.assertEqual(answer_text(raw), "Hello there.")

    def test_a_response_that_is_not_a_chat_completion_is_an_error(self):
        for raw in (b"not json", b"{}", json.dumps({"choices": []}).encode(),
                    json.dumps({"choices": [{"message": {"content": None}}]}).encode()):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                answer_text(raw)


class CallTests(unittest.TestCase):
    def setUp(self):
        self.chat = FakeChat(think=True)
        self.addCleanup(self.chat.close)

    def test_the_request_is_a_plain_chat_completion_and_the_reply_is_cleaned(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        key = Path(temp.name) / "key"
        key.write_text("sk-local-test\n")
        settings, _ = parse_writer({"base_url": self.chat.base, "model": "treebeard", "api_key_file": str(key),
                                    "max_tokens": 300, "temperature": 0.7})
        text = make_complete(settings)("system words", "user words")
        self.assertEqual(text, GOOD)
        sent = self.chat.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-local-test")
        body = sent["body"]
        self.assertEqual((body["model"], body["max_tokens"], body["temperature"], body["stream"]), ("treebeard", 300, 0.7, False))
        self.assertEqual(body["messages"], [{"role": "system", "content": "system words"},
                                            {"role": "user", "content": "user words"}])
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_an_unreachable_or_failing_server_raises_so_the_templates_take_over(self):
        settings, _ = parse_writer({"base_url": "http://127.0.0.1:9", "model": "m", "timeout_s": 2})
        with self.assertRaises(ValueError):
            make_complete(settings)("s", "u")
        failing = FakeChat(status=500)
        self.addCleanup(failing.close)
        settings, _ = parse_writer({"base_url": failing.base, "model": "m", "timeout_s": 5})
        with self.assertRaises(ValueError):
            make_complete(settings)("s", "u")


class ExtensionTests(HostsCase):
    def write_config(self, config):
        (Path(self.temp.name) / "ext" / "hosts" / "config.json").write_text(json.dumps(config))
        self.reboot()

    def test_a_configured_writer_writes_the_script(self):
        chat = FakeChat()
        self.addCleanup(chat.close)
        self.write_config({"writer": {"base_url": chat.base, "model": "treebeard", "timeout_s": 10}})
        result = self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL,
                                                  "seed": 3, "target_seconds": 10, "use_ai": True})
        self.assertEqual(result["source"], "llm", result.get("warnings"))
        self.assertIn("Paper Lantern Weather", result["text"])
        self.assertEqual(len(chat.requests), 1)

    def test_without_use_ai_the_writer_is_not_asked(self):
        chat = FakeChat()
        self.addCleanup(chat.close)
        self.write_config({"writer": {"base_url": chat.base, "model": "treebeard"}})
        result = self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 3})
        self.assertEqual(result["source"], "template")
        self.assertEqual(chat.requests, [])

    def test_a_down_writer_falls_back_to_the_templates(self):
        self.write_config({"writer": {"base_url": "http://127.0.0.1:9", "model": "treebeard", "timeout_s": 2}})
        result = self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL,
                                                  "seed": 3, "target_seconds": 10, "use_ai": True})
        self.assertEqual(result["source"], "template")
        self.assertTrue(result["text"])

    def test_bad_writer_settings_are_reported_and_never_stop_hosts(self):
        self.write_config({"writer": {"base_url": "http://10.0.0.5:8001", "model": "treebeard"}})
        self.assertEqual(self.host.status()["hosts"]["state"], "running")
        warnings = [c for c in self.host.checks() if c["id"].startswith("hosts.config")]
        self.assertTrue(any("writing assistant" in c["detail"] for c in warnings), warnings)


if __name__ == "__main__":
    unittest.main()
