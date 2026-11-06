"""Real sockets: an active-but-slow provider must not monopolize the operator."""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from myradio.chat import Chat
from myradio.station import Station, StationError


ANSWER = json.dumps({'choices': [{'message': {
    'content': 'Propose a rename.',
    'tool_calls': [{'function': {'name': 'station_update',
                                'arguments': '{"name":"Deadline FM"}'}}],
}}]}).encode()


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.behavior = 'success'
        self.cancel = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.received = []
        self.dripped = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.received.append((self.path, self.headers.get('Authorization'),
                                       json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                owner.started.set()
                mode = owner.behavior
                try:
                    if mode == 'success':
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(ANSWER)))
                        self.end_headers()
                        self.wfile.write(ANSWER)
                    elif mode == 'redirect':
                        self.send_response(307)
                        self.send_header('Location', owner.redirect_url)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                    elif mode == 'oversize':
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'x' * (1024 * 1024 + 1))
                    elif mode == 'badchunk':
                        self.send_response(200)
                        self.send_header('Transfer-Encoding', 'chunked')
                        self.end_headers()
                        self.wfile.write(b'not-a-chunk-size\r\n')
                    else:
                        if mode == 'body':
                            self.send_response(200)
                            self.send_header('Content-Length', str(len(ANSWER)))
                            self.end_headers()
                            data = ANSWER
                        else:
                            data = b'HTTP/1.1 200 OK\r\nX-Slow: ' + b'x' * 400
                        for byte in data:
                            if owner.cancel.is_set():
                                break
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            owner.dripped += 1
                            if owner.cancel.wait(.02):
                                break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    owner.finished.set()

        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}/v1'

    def close(self):
        self.cancel.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class AgentDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.station = Station(self.temp.name, start_worker=False)
        self.provider = Provider()
        with patch.dict('os.environ', {}, clear=True):
            self.chat = Chat(self.station)
        self.chat.configure({'url': self.provider.url, 'model': 'deadline-fixture', 'key': 'fixture-secret'})

    def tearDown(self):
        self.provider.close()
        self.station.close()
        self.temp.cleanup()

    def test_slow_headers_and_body_release_operator_without_mutation(self):
        for mode in ('headers', 'body'):
            with self.subTest(mode=mode):
                self.provider.behavior = mode
                self.provider.started.clear()
                self.provider.finished.clear()
                errors = []
                def ask():
                    try:
                        self.chat.message('Rename the station')
                    except StationError as exc:
                        errors.append(str(exc))
                before = time.monotonic()
                # create=True lets this regression run against the old implementation.
                with patch('myradio.chat.MODEL_REQUEST_TIMEOUT', .25, create=True):
                    worker = threading.Thread(target=ask, daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(self.provider.started.wait(1))
                        with self.assertRaisesRegex(StationError, 'still answering'):
                            self.chat.message('Second concurrent request')
                        with self.assertRaisesRegex(StationError, 'busy'):
                            self.chat.configure({'disconnect': True})
                        worker.join(timeout=1)
                        self.assertFalse(worker.is_alive(), 'provider drip-feed held the chat lock beyond the deadline')
                        self.assertLess(time.monotonic() - before, 1)
                        self.assertEqual(len(errors), 1)
                        self.assertIn('No station changes', errors[0])
                        self.assertNotIn('fixture-secret', errors[0])
                        self.assertEqual(self.chat.pending, {})
                        self.assertEqual(self.chat.history, [])
                        self.assertEqual(self.station.status()['station']['name'], 'My radio')
                        self.assertGreater(self.provider.dripped, 2)
                    finally:
                        self.provider.cancel.set()
                        worker.join(timeout=2)
                self.assertTrue(self.provider.finished.wait(1))
                self.provider.cancel.clear()
                self.provider.behavior = 'success'
                # The next request and single-use human approval still work.
                result = self.chat.message('Try again')
                self.assertEqual(self.station.status()['station']['name'], 'My radio')
                self.chat.approve(result['actions'][0]['id'])
                self.assertEqual(self.station.status()['station']['name'], 'Deadline FM')
                self.station.action('station.update', {'name': 'My radio'})
                self.chat.history.clear()
                self.chat.pending['old'] = (time.monotonic() + 120, {})

    def test_redirect_oversize_and_malformed_chunk_are_safe_failures(self):
        other = Provider()
        self.provider.redirect_url = other.url + '/chat/completions'
        try:
            for mode in ('redirect', 'oversize', 'badchunk'):
                with self.subTest(mode=mode):
                    self.provider.behavior = mode
                    self.chat.pending['stale'] = (time.monotonic() + 120, {})
                    with self.assertRaisesRegex(StationError, 'No station changes'):
                        self.chat.message('Rename')
                    self.assertEqual(self.chat.pending, {})
                    self.assertEqual(self.chat.history, [])
                    self.assertFalse(other.received)
                    self.assertEqual(self.station.status()['station']['name'], 'My radio')
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
