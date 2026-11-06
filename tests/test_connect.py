"""Connect your agent: the studio's copy-paste setup data, agent activity tracking, the MCP adapter driving the queue,
and the connect command that saves a stream destination without the password ever being printed."""
import contextlib
import io
import json
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from unittest.mock import patch

from myradio import connect
from myradio.mcp import Client, dispatch
from myradio.server import Server, load_token
from myradio.station import FRAMES, Station


def audio(sample):
    output = io.BytesIO()
    with wave.open(output, 'wb') as f:
        f.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<h', sample) * FRAMES)
    return output.getvalue()


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.station = Station(self.temp.name, start_worker=False)
        self.token = load_token(self.temp.name)
        self.server = Server(('127.0.0.1', 0), self.station, self.token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.station.close(); self.thread.join()
        self.temp.cleanup()

    def get(self, path, headers=None):
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', None, {'X-Myradio-Token': self.token, **(headers or {})})
        with self.http.open(request, timeout=5) as response:
            return json.loads(response.read())

    def post(self, path, body, headers=None):
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', json.dumps(body).encode(),
                                         {'X-Myradio-Token': self.token, 'Content-Type': 'application/json', **(headers or {})})
        with self.http.open(request, timeout=5) as response:
            return json.loads(response.read())

    def cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', env or {}, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = connect.main(['--data-dir', self.temp.name, '--port', str(self.port), *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_connect_info_needs_the_launch_code_and_never_contains_it(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.http.open(f'http://127.0.0.1:{self.port}/api/connect', timeout=5)
        self.assertEqual(caught.exception.code, 401)
        info = self.get('/api/connect')
        self.assertEqual(info['port'], self.port)
        self.assertEqual(info['data_dir'], str(self.station.directory))
        self.assertTrue(info['python'])
        self.assertTrue((__import__('pathlib').Path(info['package_dir']) / 'myradio' / 'mcp.py').is_file())   # the copied config's cwd finds the adapter
        self.assertEqual(info['station'], 'My radio')
        self.assertNotIn(self.token, json.dumps(info))

    def test_only_agent_requests_are_recorded_as_agent_activity(self):
        self.assertEqual(self.get('/api/status')['agent_link'], {'seen_at': None, 'last': None})   # the browser is not an agent
        self.get('/api/status', {'X-Myradio-Client': 'mcp'})
        link = self.get('/api/status')['agent_link']
        self.assertIsNotNone(link['seen_at'])
        self.assertEqual(link['last'], 'status')
        self.get('/api/status', {'X-Myradio-Client': 'browser'})
        self.assertEqual(self.get('/api/status')['agent_link']['last'], 'status')
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/action', {'operation': 'not a real <operation>', 'arguments': {}}, {'X-Myradio-Client': 'mcp'})
        self.assertEqual(self.get('/api/status')['agent_link']['last'], 'request')       # odd names are never echoed into the studio

    def test_the_mcp_adapter_can_dj_the_queue_and_the_studio_sees_it(self):
        ids = [self.station.import_wav(audio(s), name, 'Artist', 'music')['id'] for name, s in (('A', 11), ('B', 22), ('C', 33))]
        for track_id in ids:
            self.station.action('queue.add', {'track_id': track_id})
        client = Client(self.port, self.token)                         # what the copied MCP config runs
        names = [tool['name'] for tool in dispatch(client, 'tools/list', {})['tools']]
        self.assertIn('queue_move', names)
        last = self.station.status()['queue'][-1]
        result = dispatch(client, 'tools/call', {'name': 'queue_move', 'arguments': {'queue_id': str(last['id']), 'position': 0}})
        self.assertFalse(result.get('isError'), result)
        self.assertEqual([q['title'] for q in self.station.status()['queue']], ['C', 'A', 'B'])
        self.assertEqual(self.get('/api/status')['agent_link']['last'], 'queue.move')

    def test_the_connect_command_saves_a_stream_without_printing_the_password(self):
        secret = 'not-printed-source-password'
        code, out, err = self.cli('stream', '--url', 'http://127.0.0.1:9/live.mp3', env={'MYRADIO_SOURCE_PASSWORD': secret})
        self.assertEqual(code, 0, err)
        self.assertNotIn(secret, out + err)
        self.assertEqual(self.station.broadcast.settings_summary(), {'url': 'http://127.0.0.1:9/live.mp3', 'username': 'source'})
        self.assertEqual(self.get('/api/status')['agent_link']['last'], 'broadcast')
        code, out, err = self.cli('status')
        self.assertEqual(code, 0)
        self.assertIn('http://127.0.0.1:9/live.mp3', out)
        self.assertNotIn(secret, out)
        self.assertEqual(self.station.status()['broadcast']['state'], 'disconnected')     # saving never goes live

    def test_the_connect_command_refuses_a_missing_or_embedded_password(self):
        code, _, err = self.cli('stream', '--url', 'http://127.0.0.1:9/live.mp3', env={'MYRADIO_SOURCE_PASSWORD': ''})
        self.assertEqual(code, 2)
        self.assertIn('never by typing it into a command', err)
        code, _, err = self.cli('stream', '--url', 'http://source:oops@127.0.0.1:9/live.mp3', env={'MYRADIO_SOURCE_PASSWORD': 'x'})
        self.assertEqual(code, 2)
        self.assertNotIn('oops', err)
        code, _, err = self.cli('stream', '--url', 'http://radio.example.org/live.mp3', env={'MYRADIO_SOURCE_PASSWORD': 'x'})
        self.assertEqual(code, 1)                                          # the station's own rules still apply (remote needs HTTPS)
        self.assertEqual(self.station.broadcast.settings_summary(), {})


if __name__ == '__main__':
    unittest.main()
