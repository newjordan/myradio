"""Queue arrangement: moving entries, adding at a position, and keeping that order through playout, stop, schedules,
an upgrade from a station made before positions existed, and a backup and restore."""
import datetime as dt
import io
import random
import sqlite3
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from myradio.backup import create_backup, restore_backup
from myradio.chat import tools
from myradio.station import FRAMES, Station, StationError


def audio(sample, frames=FRAMES):
    output = io.BytesIO()
    with wave.open(output, 'wb') as f:
        f.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<h', sample) * frames)
    return output.getvalue()


def block(sample):
    return struct.pack('<hh', sample, sample) * FRAMES


class QueueOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.station = Station(self.temp.name, start_worker=False)
        self.tracks = {name: self.station.import_wav(audio(sample), name, 'Artist', 'music')
                       for name, sample in (('A', 11), ('B', 22), ('C', 33), ('D', 44))}

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def queue(self, station=None):
        return [item['title'] for item in (station or self.station).status()['queue']]

    def ids(self):
        return {item['title']: item['id'] for item in self.station.status()['queue']}

    def add(self, *names, **args):
        for name in names:
            self.station.action('queue.add', {'track_id': self.tracks[name]['id'], **args})

    def move(self, name, position):
        self.station.action('queue.move', {'queue_id': self.ids()[name], 'position': position})

    def test_moving_an_entry_changes_only_the_order_never_the_ids(self):
        self.add('A', 'B', 'C', 'D')
        before = self.ids()
        self.move('D', 0)
        self.assertEqual(self.queue(), ['D', 'A', 'B', 'C'])
        self.move('A', 2)
        self.assertEqual(self.queue(), ['D', 'B', 'A', 'C'])
        self.move('D', 99)                                       # past the end means last
        self.assertEqual(self.queue(), ['B', 'A', 'C', 'D'])
        self.station.action('queue.move', {'queue_id': str(self.ids()['C']), 'position': 0})   # the UI may send the id as text
        self.assertEqual(self.queue(), ['C', 'B', 'A', 'D'])
        self.assertEqual(self.ids(), before)

    def test_adding_at_a_position(self):
        self.add('A', 'B')
        self.add('C', position=1)
        self.assertEqual(self.queue(), ['A', 'C', 'B'])
        self.add('D', position=0)
        self.assertEqual(self.queue(), ['D', 'A', 'C', 'B'])
        self.add('A', position=500)
        self.assertEqual(self.queue(), ['D', 'A', 'C', 'B', 'A'])
        self.add('B')                                            # without a position it appends, as before
        self.assertEqual(self.queue()[-1], 'B')

    def test_the_station_plays_the_arranged_order(self):
        self.add('A', 'B', 'C')
        self.move('C', 0)
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), block(33))
        self.assertEqual(self.queue(), ['A', 'B'])

    def test_stop_puts_the_playing_track_back_in_front_of_an_arranged_queue(self):
        self.add('A', 'B', 'C')
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), block(11))         # A is playing
        self.move('C', 0)
        self.station.action('transport.stop')
        self.assertEqual(self.queue(), ['A', 'C', 'B'])

    def test_a_due_schedule_goes_in_front_of_an_arranged_queue(self):
        self.add('A', 'B', 'C')
        self.move('C', 0)
        base = dt.datetime(2026, 1, 5, 9, tzinfo=dt.timezone.utc).timestamp()
        with patch('myradio.station.time.time', return_value=base):
            self.station.action('schedule.add', {'track_id': self.tracks['D']['id'], 'at': '2026-01-05T09:00:10+00:00'})
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(now=base + 20), block(44))
        self.assertEqual(self.queue(), ['C', 'A', 'B'])

    def test_bad_arguments_are_refused_and_change_nothing(self):
        self.add('A', 'B')
        qid = self.ids()['A']
        for args in ({'queue_id': qid, 'position': -1}, {'queue_id': qid, 'position': True}, {'queue_id': qid, 'position': '1'},
                     {'queue_id': qid, 'position': 1.0}, {'queue_id': qid, 'position': 10 ** 7}, {'queue_id': qid},
                     {'position': 0}, {'queue_id': qid, 'position': 0, 'extra': 1}):
            with self.subTest(args=args), self.assertRaises(StationError):
                self.station.action('queue.move', args)
        with self.assertRaisesRegex(StationError, 'not found'):
            self.station.action('queue.move', {'queue_id': 987654, 'position': 0})
        with self.assertRaises(StationError):
            self.station.action('queue.add', {'track_id': self.tracks['C']['id'], 'position': -3})
        self.assertEqual(self.queue(), ['A', 'B'])

    def test_the_catalog_says_position_is_optional_for_adding_and_required_for_moving(self):
        catalog = {item['name']: item for item in self.station.operations()}
        self.assertEqual(catalog['queue.add']['required'], ['track_id'])
        self.assertEqual(catalog['queue.add']['arguments']['position'], {'type': 'integer'})
        self.assertEqual(catalog['queue.move']['required'], ['queue_id', 'position'])
        functions = {t['function']['name']: t['function']['parameters'] for t in tools()}
        self.assertEqual(functions['queue_add']['required'], ['track_id'])
        self.assertEqual(functions['queue_move']['required'], ['queue_id', 'position'])

    def test_rows_written_without_a_position_still_play_in_id_order(self):
        # Older tools and test fixtures write the table directly; the station must not let a missing position jump the queue.
        self.add('A', 'B')
        self.move('B', 0)
        self.station.db.executemany('INSERT INTO queue(track_id) VALUES (?)', [(self.tracks['C']['id'],), (self.tracks['D']['id'],)])
        self.station.db.execute('INSERT INTO queue(id,track_id) VALUES ((SELECT MIN(id)-1 FROM queue),?)', (self.tracks['D']['id'],))
        self.station.db.commit()
        self.assertEqual(self.queue(), ['D', 'B', 'A', 'C', 'D'])
        self.assertIsNone(self.station.db.execute('SELECT 1 FROM queue WHERE pos IS NULL').fetchone())

    def test_without_moves_play_order_and_id_order_agree(self):
        # Front inserts (stop) and appends in any mix keep the two orders identical; tools that read ``ORDER BY id`` stay right.
        rnd = random.Random(4)
        self.station.action('transport.start')
        for _ in range(60):
            step = rnd.random()
            if step < 0.45:
                self.add(rnd.choice('ABCD'))
            elif step < 0.65:
                self.station.action('transport.stop')
                self.station.action('transport.start')
            else:
                self.station.tick()
            by_pos = [r[0] for r in self.station.db.execute('SELECT id FROM queue ORDER BY pos,id')]
            by_id = [r[0] for r in self.station.db.execute('SELECT id FROM queue ORDER BY id')]
            self.assertEqual(by_pos, by_id)


class UpgradeAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_station_from_before_positions_keeps_its_queue_order(self):
        directory = self.root / 'old'
        station = Station(directory, start_worker=False)
        names = [station.import_wav(audio(s), n, 'Artist', 'music')['id'] for n, s in (('A', 11), ('B', 22), ('C', 33))]
        station.close()
        with sqlite3.connect(directory / 'station.sqlite3') as db:      # rebuild the queue table the way older versions made it
            db.executescript('DROP INDEX IF EXISTS queue_order; DROP TABLE queue;'
                             'CREATE TABLE queue(id INTEGER PRIMARY KEY AUTOINCREMENT, track_id TEXT NOT NULL REFERENCES tracks(id));')
            db.executemany('INSERT INTO queue(id,track_id) VALUES (?,?)', [(-1, names[2]), (5, names[0]), (9, names[1])])
        for _ in range(2):                                                # opening twice changes nothing more
            station = Station(directory, start_worker=False)
            try:
                self.assertEqual([q['title'] for q in station.status()['queue']], ['C', 'A', 'B'])
                self.assertEqual([q['id'] for q in station.status()['queue']], [-1, 5, 9])
            finally:
                station.close()

    def test_a_backup_keeps_the_arranged_order(self):
        source, archive, target = self.root / 'source', self.root / 'station.myradio.zip', self.root / 'restored'
        station = Station(source, start_worker=False)
        try:
            ids = {n: station.import_wav(audio(s, FRAMES * 3), n, 'Artist', 'music')['id'] for n, s in (('A', 11), ('B', 22), ('C', 33))}
            for name in 'ABC':
                station.action('queue.add', {'track_id': ids[name]})
            entry = next(q['id'] for q in station.status()['queue'] if q['title'] == 'C')
            station.action('queue.move', {'queue_id': entry, 'position': 0})
        finally:
            station.close()
        create_backup(source, archive)
        restore_backup(archive, target)
        restored = Station(target, start_worker=False)
        try:
            self.assertEqual([q['title'] for q in restored.status()['queue']], ['C', 'A', 'B'])
        finally:
            restored.close()


if __name__ == '__main__':
    unittest.main()
