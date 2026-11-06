"""Real SQLite limits and deterministic transaction/cleanup fault injection."""
import io
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

from myradio.station import Station, StationError, FRAMES, SILENCE


class FailingDB:
    def __init__(self, db, *, sql=None, commit=False, rollback=False, reads=False):
        self.db, self.sql = db, sql
        self.fail_commit, self.fail_rollback, self.reads = commit, rollback, reads

    def execute(self, sql, *args):
        if self.sql and self.sql in sql:
            self.sql = None  # failure leaves transaction pending until rollback
            raise sqlite3.OperationalError('injected write failure')
        if self.reads and sql.startswith('SELECT'):
            raise sqlite3.OperationalError('injected read failure')
        return self.db.execute(sql, *args)

    def commit(self):
        if self.fail_commit:
            self.fail_commit = False
            raise sqlite3.OperationalError('injected commit failure')
        return self.db.commit()

    def rollback(self):
        if self.fail_rollback:
            raise sqlite3.OperationalError('injected rollback failure')
        return self.db.rollback()

    def __getattr__(self, name):
        return getattr(self.db, name)


class StorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'station'
        self.s = Station(self.directory, start_worker=False)
        out = io.BytesIO()
        with wave.open(out, 'wb') as f:
            f.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
            f.writeframes(struct.pack('<hh', 1234, 1234) * FRAMES * 5)
        self.wav = out.getvalue()
        self.track = self.s.import_wav(self.wav, 'Music')
        self.db = self.s.db

    def tearDown(self):
        self.s.db = self.db
        self.s.close()
        self.temp.cleanup()

    def assert_fault(self):
        self.assertFalse(self.s.running)
        self.assertTrue(self.s.faulted)
        self.assertIsNone(self.s.reader)
        state = self.s.status()
        self.assertEqual(state['health']['state'], 'fault')
        self.assertIn('restart', state['error'])
        for op, args in [('transport.start', {}), ('queue.add', {'track_id': self.track['id']})]:
            with self.assertRaisesRegex(StationError, 'restart'):
                self.s.action(op, args)
        self.assertEqual(self.s.tick(), SILENCE)

    def test_readonly_during_finish_keeps_worker_alive_and_monitor_responsive(self):
        self.s.action('transport.start')
        self.s.tick()
        reader = self.s.reader
        listener = self.s.subscribe()
        self.db.execute('PRAGMA query_only=ON')
        with patch.object(self.s.broadcast, 'stop', wraps=self.s.broadcast.stop) as stop:
            self.s.worker = threading.Thread(target=self.s._run, daemon=True)
            self.s.worker.start()
            deadline = time.monotonic() + 3
            while not self.s.faulted and time.monotonic() < deadline:
                time.sleep(.02)
            self.assert_fault()
            self.assertTrue(self.s.worker.is_alive())
            self.assertTrue(reader.source.closed)
            self.assertTrue(stop.called)
            # Drain pre-fault PCM; subsequent clock blocks must be silence.
            while not listener.empty():
                listener.get_nowait()
            self.assertEqual(listener.get(timeout=1), SILENCE)
        self.db.execute('PRAGMA query_only=OFF')
        self.assert_fault()  # removing the restriction cannot silently resume

    def test_real_sqlite_full_rolls_back_partial_playout(self):
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        self.db.execute(f'PRAGMA max_page_count={pages}')
        self.db.execute('CREATE TEMP TRIGGER fill_history BEFORE INSERT ON main.history BEGIN INSERT INTO history(title,at,event) VALUES (zeroblob(1048576),"test","full"); END')
        self.s.action('queue.add', {'track_id': self.track['id']})
        self.s.action('transport.start')
        with self.assertRaises(StationError):
            self.s.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 1)
        self.assertFalse(self.db.in_transaction)
        self.assert_fault()

    def test_failed_action_commit_cannot_be_committed_by_later_tick(self):
        self.s.db = FailingDB(self.db, commit=True)
        with self.assertRaises(StationError):
            self.s.action('queue.add', {'track_id': self.track['id']})
        self.s.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 0)
        self.assertFalse(self.db.in_transaction)
        self.assert_fault()

    def test_schedule_queue_insert_rolls_back_when_status_write_fails(self):
        self.db.execute('INSERT INTO schedules VALUES (?,?,?,?,?)', ('event', self.track['id'], 'test', 100, 'pending'))
        self.db.commit()
        self.s.action('transport.start')
        self.s.db = FailingDB(self.db, sql='UPDATE schedules SET status=?')
        with self.assertRaises(StationError):
            self.s.tick(100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT status FROM schedules').fetchone()[0], 'pending')
        self.assert_fault()

    def test_rollback_and_broadcast_cleanup_failure_do_not_kill_clock(self):
        self.s.action('transport.start')
        self.s.tick()
        reader = self.s.reader
        self.s.db = FailingDB(self.db, commit=True, rollback=True)
        with patch.object(self.s.broadcast, 'stop', side_effect=OSError('cleanup failure')):
            with self.assertRaises(StationError):
                self.s.action('queue.add', {'track_id': self.track['id']})
        self.assertTrue(reader.source.closed)
        self.assert_fault()
        # No future tick may commit the uncertain transaction. Restart closes it.
        self.s.db = self.db
        self.s.close()
        self.s = Station(self.directory, start_worker=False)
        self.db = self.s.db
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 1)  # recovered current only
        self.assertFalse(self.s.running)

    def test_failed_import_commit_leaves_neither_row_nor_audio(self):
        self.s.db = FailingDB(self.db, commit=True)
        with self.assertRaises(StationError):
            self.s.import_wav(self.wav, 'Failed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tracks').fetchone()[0], 1)
        self.assertEqual(len(list(self.s.media.iterdir())), 1)
        self.assert_fault()

    def test_read_failure_returns_explicitly_stale_fault_snapshot(self):
        state = self.s.status()
        self.s.db = FailingDB(self.db, reads=True)
        fault = self.s.status()
        self.assertEqual(fault['library'], state['library'])
        self.assertTrue(fault['health']['stale'])
        self.assertEqual(fault['health']['state'], 'fault')
        fault['library'].clear()
        self.assertEqual(len(self.s.status()['library']), 1)
        self.assert_fault()

    def test_read_failure_without_cache_is_still_reportable(self):
        self.s.db = FailingDB(self.db, reads=True)
        self.assertEqual(self.s.status()['library'], [])
        self.assert_fault()

    def test_emergency_broadcast_stop_is_allowed_when_faulted(self):
        self.s._fail_safe()
        with patch.object(self.s.broadcast, 'stop', return_value={'state': 'stopped'}) as stop:
            self.assertEqual(self.s.action('broadcast.stop'), {'state': 'stopped'})
            stop.assert_called_once()
        self.assert_fault()


if __name__ == '__main__':
    unittest.main()
