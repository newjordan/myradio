"""Unattended playout fault injection; no encoder, network or model required."""
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from myradio.station import Station, StationError, FRAMES, SILENCE
from myradio.chat import tools
from myradio.mcp import dispatch
from myradio.backup import create_backup, restore_backup


def wav(sample=1234, frames=FRAMES * 3, rate=44100, channels=2):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<h', sample) * frames * channels)
    return out.getvalue()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'station'
        self.station = Station(self.directory, start_worker=False)

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def track(self, name='Music', sample=1234, **kw):
        return self.station.import_wav(wav(sample, **kw), name)

    def media(self, track):
        return self.directory / 'media' / (track['id'] + '.wav')

    def test_missing_music_is_quarantined_and_next_track_plays_same_block(self):
        bad = self.track('Missing')
        good = self.track('Good', 4321)
        self.media(bad).unlink()
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), struct.pack('<hh', 4321, 4321) * FRAMES)
        state = self.station.status()
        self.assertTrue(state['running'])
        self.assertEqual(state['current']['id'], good['id'])
        self.assertEqual(state['health']['state'], 'degraded')
        self.assertEqual(state['health']['unavailable_tracks'], 1)
        failed = next(t for t in state['library'] if t['id'] == bad['id'])
        self.assertFalse(failed['available'])
        self.assertTrue(failed['playback_error'])
        self.assertNotIn(str(self.directory), failed['playback_error'])
        for _ in range(20): self.station.tick()
        self.assertEqual(sum(h['event'] == 'media unavailable' for h in self.station.status()['history']), 1)

    def test_all_unavailable_is_bounded_persistent_and_never_auto_retries(self):
        bad = self.track()
        self.media(bad).write_bytes(b'broken')
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), SILENCE)
        for _ in range(50): self.assertEqual(self.station.tick(), SILENCE)
        self.assertEqual(self.station.status()['health']['state'], 'waiting')
        self.assertTrue(self.station.running)  # explicit waiting, not an implicit external reconnect
        self.station.close()
        self.station = Station(self.directory, start_worker=False)
        self.assertFalse(self.station.running)
        self.assertFalse(self.station.status()['library'][0]['available'])
        self.media(bad).write_bytes(wav())
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), SILENCE)  # repair requires explicit retry
        self.station.action('library.retry', {'track_id':bad['id']})
        self.assertEqual(self.station.tick(), struct.pack('<hh', 1234, 1234) * FRAMES)
        self.assertEqual(self.station.status()['health']['state'], 'playing')

    def test_invalid_format_and_truncated_data_never_reach_listeners(self):
        for data in (wav(rate=48000), wav(channels=1), wav()[:-10], b'RIFFbad'):
            with self.subTest(length=len(data)):
                bad = self.track('Damaged')
                self.media(bad).write_bytes(data)
                self.station.action('queue.add', {'track_id':bad['id']})
                self.station.action('station.update', {'auto_fill':False})
                self.station.action('transport.start')
                self.assertEqual(self.station.tick(), SILENCE)
                self.assertEqual(self.station.status()['health']['unavailable_tracks'], 1)
                self.station.action('library.delete', {'track_id':bad['id']})

    def test_mid_track_read_failure_preserves_clock_and_uses_ready_music(self):
        first = self.track('First')
        second = self.track('Second', 3210)
        self.station.action('transport.start')
        self.station.tick()
        with patch.object(self.station.reader, 'readframes', side_effect=OSError('private/path secret')):
            block = self.station.tick()
        self.assertEqual(block, struct.pack('<hh', 3210, 3210) * FRAMES)
        self.assertEqual(self.station.status()['current']['id'], second['id'])
        self.assertFalse(next(t for t in self.station.status()['library'] if t['id'] == first['id'])['available'])

    def test_queue_of_bad_entries_has_per_tick_work_budget(self):
        bad = self.track()
        self.media(bad).unlink()
        good = self.track('Good', 99)
        for _ in range(40): self.station.action('queue.add', {'track_id':bad['id']})
        self.station.action('queue.add', {'track_id':good['id']})
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), SILENCE)
        self.assertEqual(len(self.station.status()['queue']), 25)  # sixteen candidates per block
        self.assertEqual(self.station.tick(), SILENCE)
        self.assertEqual(self.station.tick(), struct.pack('<hh', 99, 99) * FRAMES)
        self.assertTrue(self.station.running)

    def test_retry_validates_before_clearing_and_is_shared_with_agents(self):
        bad = self.track()
        self.media(bad).unlink()
        self.station.action('transport.start'); self.station.tick()
        with self.assertRaises(StationError):
            self.station.action('library.retry', {'track_id':bad['id']})
        self.assertFalse(self.station.status()['library'][0]['available'])
        self.media(bad).write_bytes(wav())
        self.assertIn('library_retry', [t['function']['name'] for t in tools()])
        station = self.station
        class Client:
            def request(self, path, body):
                return station.action(body['operation'], body['arguments'])
        result = dispatch(Client(), 'tools/call', {'name':'library_retry', 'arguments':{'track_id':bad['id']}})
        self.assertFalse(result['isError'])
        self.assertTrue(self.station.status()['library'][0]['available'])

    def test_symlinks_and_nonregular_media_are_rejected_without_blocking(self):
        bad = self.track()
        self.media(bad).unlink()
        self.media(bad).symlink_to('/dev/zero')
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), SILENCE)
        self.assertEqual(self.station.status()['health']['unavailable_tracks'], 1)
        self.media(bad).unlink()
        import os
        os.mkfifo(self.media(bad))
        with self.assertRaises(StationError):
            self.station.action('library.retry', {'track_id':bad['id']})

    def test_cancelled_schedules_are_portable_and_health_is_rechecked_on_restore(self):
        import datetime as dt
        track = self.track()
        self.station.action('schedule.add', {'track_id':track['id'], 'at':(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()})
        self.station.action('schedule.cancel', {'schedule_id':self.station.status()['schedules'][0]['id']})
        self.station.close()
        archive = Path(self.temp.name) / 'station.zip'
        create_backup(self.directory, archive)
        restored = Path(self.temp.name) / 'restored'
        restore_backup(archive, restored)
        self.station = Station(restored, start_worker=False)
        self.assertEqual(self.station.status()['schedules'][0]['status'], 'cancelled')
        self.station.action('transport.start')
        self.assertNotEqual(self.station.tick(), SILENCE)

    def test_real_worker_and_http_stream_survive_missing_audio_and_retry(self):
        import json
        import threading
        import time
        import urllib.request
        from myradio.chat import Chat
        from myradio.server import Server
        bad = self.track('Bad')
        good = self.track('Ready', 321)
        self.media(bad).unlink()
        self.station.close()
        self.station = Station(self.directory)  # real 100 ms worker, no manual ticks
        server = Server(('127.0.0.1', 0), self.station, 'test-private-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        def request(path, body=None):
            return http.open(urllib.request.Request(base+path,
                None if body is None else json.dumps(body).encode(),
                {'X-Myradio-Token':'test-private-token', 'Content-Type':'application/json'}), timeout=3)
        try:
            with request('/api/action', {'operation':'transport.start'}) as r:
                self.assertEqual(r.status, 200)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.station.status()['health']['unavailable_tracks'] != 1:
                time.sleep(.02)
            with request('/api/status') as r:
                state = json.load(r)
            self.assertEqual(state['health']['state'], 'degraded')
            self.assertEqual(state['current']['id'], good['id'])
            with request('/api/chat', {'message':'status'}) as r:
                self.assertIn('Unavailable audio', json.load(r)['reply'])
            with request('/stream.wav') as stream:
                header = stream.read(44)
                self.assertEqual(header[:4], b'RIFF')
                self.assertEqual(stream.read(len(SILENCE)), struct.pack('<hh',321,321) * FRAMES)
            self.media(bad).write_bytes(wav())
            with request('/api/action', {'operation':'library.retry', 'arguments':{'track_id':bad['id']}}) as r:
                self.assertTrue(json.load(r)['ok'])
            self.assertEqual(self.station.status()['health']['unavailable_tracks'], 0)
            self.assertTrue(self.station.worker.is_alive())
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)
