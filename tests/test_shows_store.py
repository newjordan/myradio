"""Persistence: the shows file, its migrations, damaged files, limits and backups."""
import gc
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from myradio.extensions import Context, ExtensionError, migrate
from myradio.shows import MAX_SHOWS, default_show
from myradio.shows.store import DB_FILE, MIGRATIONS, Store, open_database, set_aside

try:
    from test_shows_support import show
except ImportError:  # run as tests.test_shows_store
    from tests.test_shows_support import show

NOW = 1_735_689_600.0     # 2025-01-01 00:00:00 UTC


def make_ctx(root, clock=lambda: NOW):
    state = Path(root) / 'ext' / 'shows'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Context('shows', Path(root), state, station=None, clock=clock)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = make_ctx(self.temp.name)
        self.stores = []

    def open(self, timezone='America/New_York', source='configured'):
        conn, note = open_database(self.ctx, NOW)
        store = Store(conn)
        self.stores.append(store)
        self.addCleanup(self._close, store)
        store.bootstrap(lambda: (timezone, source), NOW)
        return store, note

    @staticmethod
    def _close(store):
        try:
            store.close()
        except sqlite3.Error:
            pass

    @property
    def path(self):
        return self.ctx.state_dir / DB_FILE


class FreshFile(StoreCase):
    def test_a_new_file_has_the_default_show_a_seed_and_a_first_revision(self):
        store, note = self.open()
        self.assertIsNone(note)
        loaded = store.load()
        self.assertEqual((loaded.week.timezone, loaded.revision, loaded.zone_source, loaded.skipped, loaded.problems),
                         ('America/New_York', 1, 'configured', 0, ()))
        self.assertEqual(loaded.week.shows, ())
        self.assertEqual(loaded.week.default, default_show())
        self.assertTrue(0 < loaded.seed < 2 ** 62)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(store.conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(store.conn.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))

    def test_bootstrapping_again_changes_nothing_and_does_not_ask_for_a_zone(self):
        store, _ = self.open()
        before = store.load()

        def refuse():
            raise AssertionError('the time zone is only worked out for a brand-new file')

        store.bootstrap(refuse, NOW + 5)
        after = store.load()
        self.assertEqual((before.seed, before.revision, before.week.timezone, before.zone_source),
                         (after.seed, after.revision, after.week.timezone, after.zone_source))

    def test_each_station_gets_its_own_seed(self):
        store, _ = self.open()
        with tempfile.TemporaryDirectory() as other:
            conn, _ = open_database(make_ctx(other), NOW)
            second = Store(conn)
            try:
                second.bootstrap(lambda: ('UTC', 'configured'), NOW)
                self.assertNotEqual(store.load().seed, second.load().seed)
            finally:
                second.close()

    def test_nothing_secret_is_stored(self):
        store, _ = self.open()
        store.save_show(show('a'), NOW)
        store.close()
        raw = self.path.read_bytes()
        for word in (b'token', b'password', b'secret', b'launch'):
            self.assertNotIn(word, raw.lower())


class Migrations(StoreCase):
    def test_migrating_twice_is_harmless_and_keeps_data(self):
        store, _ = self.open()
        store.save_show(show('keep'), NOW)
        self.assertEqual(migrate(store.conn, MIGRATIONS), len(MIGRATIONS))
        self.assertEqual(migrate(store.conn, MIGRATIONS), len(MIGRATIONS))
        self.assertEqual([s.id for s in store.load().week.shows], ['keep'])

    def test_the_first_step_can_be_replayed_after_a_crash_before_the_version_was_written(self):
        store, _ = self.open()
        store.save_show(show('keep'), NOW)
        store.conn.executescript(MIGRATIONS[0])                  # what happens if the version number was never saved
        store.conn.execute('PRAGMA user_version=0')
        store.conn.commit()
        self.assertEqual(migrate(store.conn, MIGRATIONS), len(MIGRATIONS))
        self.assertEqual([s.id for s in store.load().week.shows], ['keep'])

    def test_reopening_gives_an_identical_schema(self):
        store, _ = self.open()
        first = store.conn.execute('SELECT type, name, sql FROM sqlite_master ORDER BY name').fetchall()
        store.close()
        conn, _ = open_database(self.ctx, NOW)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute('SELECT type, name, sql FROM sqlite_master ORDER BY name').fetchall(), first)

    def test_a_file_from_a_newer_myradio_is_refused_and_left_alone(self):
        store, _ = self.open()
        store.save_show(show('keep'), NOW)
        store.conn.execute(f'PRAGMA user_version={len(MIGRATIONS) + 1}')
        store.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ExtensionError, 'newer myradio'):
            open_database(self.ctx, NOW)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.ctx.state_dir.iterdir() if 'damaged' in p.name], [])


class Changes(StoreCase):
    def test_changes_survive_closing_and_reopening(self):
        store, _ = self.open()
        a, b, c = show('a', start='06:00', end='07:00'), show('b', start='08:00', end='09:00'), show('c', start='10:00', end='11:00')
        self.assertEqual([store.save_show(a, NOW), store.save_show(b, NOW + 1), store.save_show(c, NOW + 2)], [2, 3, 4])
        self.assertEqual(store.delete_show('b'), 5)
        self.assertEqual(store.save_show(c.replace(name='Changed'), NOW + 3), 6)
        self.assertEqual(store.save_show(default_show(name='Anytime Mix', host_id='dex'), NOW + 4), 7)
        self.assertEqual(store.set_timezone('Asia/Tokyo', 'chosen'), 8)
        seed = store.load().seed
        store.close()

        again, note = self.open('UTC', 'fallback')                  # the arguments are ignored: the file already exists
        self.assertIsNone(note)
        loaded = again.load()
        self.assertEqual([s.id for s in loaded.week.shows], ['a', 'c'])
        self.assertEqual(loaded.week.get('c').name, 'Changed')
        self.assertEqual((loaded.week.default.name, loaded.week.default.host_id), ('Anytime Mix', 'dex'))
        self.assertEqual((loaded.week.timezone, loaded.zone_source, loaded.revision, loaded.seed), ('Asia/Tokyo', 'chosen', 8, seed))
        self.assertEqual(loaded.week.get('a'), a)

    def test_deleting_something_that_is_not_there_still_succeeds_and_never_removes_the_default(self):
        store, _ = self.open()
        self.assertEqual(store.delete_show('nope'), 2)
        self.assertEqual(store.delete_show('default'), 3)
        self.assertEqual(store.load().week.default, default_show())

    def test_replacing_the_week_swaps_all_shows_at_once_in_order(self):
        store, _ = self.open()
        store.save_show(show('old', start='01:00', end='02:00'), NOW)
        new = [show(f'n{i}', start=f'{i + 2:02d}:00', end=f'{i + 3:02d}:00') for i in range(5)]
        store.replace_shows(new, NOW + 10)
        self.assertEqual([s.id for s in store.load().week.shows], [f'n{i}' for i in range(5)])
        store.replace_shows([], NOW + 20)
        self.assertEqual(store.load().week.shows, ())

    def test_a_failed_replace_changes_nothing(self):
        store, _ = self.open()
        store.save_show(show('keep', start='01:00', end='02:00'), NOW)
        before = store.load()
        calls = []
        real = Store._upsert

        def flaky(self, shown, now):
            calls.append(shown.id)
            if len(calls) == 3:
                raise sqlite3.OperationalError('disk full')
            return real(self, shown, now)

        with mock.patch.object(Store, '_upsert', flaky), self.assertRaises(sqlite3.OperationalError):
            store.replace_shows([show(f'n{i}', start=f'{i + 2:02d}:00', end=f'{i + 3:02d}:00') for i in range(4)], NOW + 1)
        after = store.load()
        self.assertEqual(([s.id for s in after.week.shows], after.revision), (['keep'], before.revision))

    def test_updating_keeps_the_creation_order_and_the_unique_default(self):
        store, _ = self.open()
        store.save_show(show('first', start='01:00', end='02:00'), NOW)
        store.save_show(show('second', start='03:00', end='04:00'), NOW + 5)
        store.save_show(show('first', name='Edited', start='01:00', end='02:00'), NOW + 10)
        rows = store.conn.execute("SELECT id, created, updated FROM shows WHERE is_default = 0 ORDER BY created").fetchall()
        self.assertEqual([(r['id'], r['created'], r['updated']) for r in rows], [('first', NOW, NOW + 10), ('second', NOW + 5, NOW + 5)])
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO shows(id,name,days,start_min,length_min,style,talk,spots_allowed,updates_allowed,color,clock,"
                               "is_default,created,updated) VALUES ('second-default','x','1',0,1440,'{}','some',0,0,'#000000','classic',1,0,0)")

    def test_a_plain_copy_of_the_main_file_is_a_complete_backup(self):
        store, _ = self.open()
        store.save_show(show('only-in-the-log-without-a-checkpoint'), NOW)
        store.set_timezone('Europe/London', 'chosen')
        with tempfile.TemporaryDirectory() as other:
            copy = Path(other) / DB_FILE
            shutil.copyfile(self.path, copy)               # the main file only: no -wal, no -shm
            conn = sqlite3.connect(copy)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT id FROM shows WHERE is_default = 0").fetchall(), [('only-in-the-log-without-a-checkpoint',)])
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='timezone'").fetchone(), ('Europe/London',))


class Loading(StoreCase):
    def raw_insert(self, store, rows):
        store.conn.executemany(
            "INSERT INTO shows(id,name,days,start_min,length_min,style,host_id,talk,spots_allowed,updates_allowed,update_researcher_id,"
            "color,clock,is_default,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        store.conn.commit()

    @staticmethod
    def row(show_id, **changes):
        base = dict(id=show_id, name='Row', days='1,2,3', start_min=60, length_min=60, style=json.dumps(
            {'palettes': [], 'tags': [], 'energy': 50, 'adventurousness': 40, 'vocals': 30}), host_id=None, talk='some',
            spots_allowed=0, updates_allowed=0, update_researcher_id=None, color='#3b4a63', clock='classic', is_default=0,
            created=NOW, updated=NOW)
        base.update(changes)
        return tuple(base.values())

    def test_unreadable_rows_are_skipped_and_reported_in_plain_words(self):
        store, _ = self.open()
        self.raw_insert(store, [self.row('good'), self.row('bad-days', days='9'), self.row('bad-style', style='{nope'),
                                self.row('bad-talk', talk='LOTS'), self.row('bad-length', length_min=3), self.row('x' * 80),
                                self.row('bad-name', name='  ')])
        loaded = store.load()
        self.assertEqual([s.id for s in loaded.week.shows], ['good'])
        self.assertEqual(loaded.skipped, 6)
        self.assertEqual(loaded.problems, ('6 saved shows could not be read and were left out. Add them again if you need them.',))

    def test_ten_thousand_rows_are_not_loaded(self):
        store, _ = self.open()
        self.raw_insert(store, [self.row(f'r{i:05d}', created=NOW + i) for i in range(10_000)])
        started = time.monotonic()
        loaded = store.load()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(loaded.week.shows), MAX_SHOWS)
        self.assertEqual(loaded.skipped, 10_000 - MAX_SHOWS)
        self.assertEqual(loaded.problems, (f'{10_000 - MAX_SHOWS} saved shows were left out because a week holds at most {MAX_SHOWS} shows.',))
        self.assertEqual(loaded.week.get('r00000').id, 'r00000')            # the oldest are kept

    def test_an_unreadable_default_row_falls_back_to_the_built_in_one(self):
        store, _ = self.open()
        store.conn.execute("UPDATE shows SET style='garbage' WHERE is_default = 1")
        store.conn.commit()
        loaded = store.load()
        self.assertEqual(loaded.week.default, default_show())
        self.assertEqual(loaded.problems, ('The saved default show could not be read, so the built-in one is used.',))

    def test_a_missing_default_row_is_recreated_on_the_next_start(self):
        store, _ = self.open()
        store.conn.execute("DELETE FROM shows WHERE is_default = 1")
        store.conn.commit()
        self.assertEqual(store.load().week.default, default_show())
        store.bootstrap(lambda: ('UTC', 'configured'), NOW)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM shows WHERE is_default = 1").fetchone()[0], 1)

    def test_a_saved_time_zone_that_is_no_longer_available_falls_back_to_utc(self):
        store, _ = self.open()
        store.conn.execute("UPDATE settings SET value='Mars/Base' WHERE key='timezone'")
        store.conn.commit()
        loaded = store.load()
        self.assertEqual(loaded.week.timezone, 'UTC')
        self.assertIn('Mars/Base', loaded.problems[0])
        self.assertIn('choose a time zone', loaded.problems[0])
        for junk in ('', 'x' * 500, '../../etc/passwd'):
            store.conn.execute("UPDATE settings SET value=? WHERE key='timezone'", (junk,))
            store.conn.commit()
            self.assertEqual(store.load().week.timezone, 'UTC')

    def test_missing_settings_are_replaced_with_safe_values(self):
        store, _ = self.open()
        store.conn.execute("DELETE FROM settings")
        store.conn.commit()
        loaded = store.load()
        self.assertEqual((loaded.revision, loaded.week.timezone, loaded.zone_source), (1, 'UTC', 'configured'))
        self.assertTrue(loaded.seed > 0)


class DamagedFiles(StoreCase):
    def test_a_file_that_is_not_a_database_is_set_aside_not_deleted(self):
        self.path.write_bytes(b'this is definitely not sqlite ' * 100)
        (self.ctx.state_dir / (DB_FILE + '-wal')).write_bytes(b'stale log')
        store, note = self.open()
        self.assertRegex(note, r'^Your saved shows could not be read, so a new empty week was started\. The old file was kept as '
                               r'shows\.sqlite3\.damaged-20250101T000000Z; restore it from a backup if you need those shows\.$')
        kept = self.ctx.state_dir / 'shows.sqlite3.damaged-20250101T000000Z'
        self.assertEqual(kept.read_bytes(), b'this is definitely not sqlite ' * 100)
        self.assertEqual((self.ctx.state_dir / 'shows.sqlite3.damaged-20250101T000000Z-wal').read_bytes(), b'stale log')
        self.assertEqual(store.load().week.shows, ())
        store.save_show(show('fresh'), NOW)
        self.assertEqual([s.id for s in store.load().week.shows], ['fresh'])

    def test_real_damage_inside_a_file_that_still_looks_like_a_database(self):
        # Context.open_db (part of the extension seam) can drop a half-opened connection when its own setup
        # statements fail on a damaged file; Python then reports it as an unclosed database. It is harmless
        # and not ours to fix, so this test does not let it clutter the output.
        for damage in ('scribbled', 'truncated', 'zeroed'):
            with self.subTest(damage=damage), warnings.catch_warnings():
                warnings.simplefilter('ignore', ResourceWarning)
                with tempfile.TemporaryDirectory() as other:
                    ctx = make_ctx(other)
                    conn, _ = open_database(ctx, NOW)
                    store = Store(conn)
                    store.bootstrap(lambda: ('UTC', 'configured'), NOW)
                    for i in range(30):
                        store.save_show(show(f's{i}', start=f'{i % 20:02d}:00', end=f'{i % 20 + 1:02d}:00', days=str(i % 7 + 1)), NOW + i)
                    store.close()
                    path = ctx.state_dir / DB_FILE
                    data = bytearray(path.read_bytes())
                    if damage == 'scribbled':
                        for i in range(100, len(data), 7):
                            data[i] = 0xFF
                    elif damage == 'truncated':
                        data = data[:5000]
                    else:
                        data[100:] = bytes(len(data) - 100)
                    path.write_bytes(bytes(data))
                    conn, note = open_database(ctx, NOW)
                    gc.collect()
                    fresh = Store(conn)
                    try:
                        fresh.bootstrap(lambda: ('UTC', 'configured'), NOW)
                        self.assertIn('could not be read, so a new empty week was started', note)
                        self.assertEqual(fresh.load().week.shows, ())
                        self.assertTrue(any(p.name.startswith('shows.sqlite3.damaged-') for p in ctx.state_dir.iterdir()))
                    finally:
                        fresh.close()

    def test_an_absurdly_large_file_or_log_is_set_aside_without_being_opened(self):
        for which in ('main', 'log'):
            with self.subTest(which=which), tempfile.TemporaryDirectory() as other:
                ctx = make_ctx(other)
                if which == 'log':                                           # a healthy main file beside a huge log
                    conn, _ = open_database(ctx, NOW)
                    healthy = Store(conn)
                    healthy.bootstrap(lambda: ('UTC', 'configured'), NOW)
                    healthy.close()
                target = ctx.state_dir / (DB_FILE if which == 'main' else DB_FILE + '-wal')
                with open(target, 'wb') as handle:
                    handle.write(b'SQLite format 3\x00')
                    handle.truncate(70 * 1024 * 1024)                       # sparse: takes no real space
                started = time.monotonic()
                conn, note = open_database(ctx, NOW)
                conn.close()
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertIn('could not be read, so a new empty week was started', note)
                kept = [p for p in ctx.state_dir.iterdir() if 'damaged' in p.name]
                self.assertTrue(any(p.stat().st_size > 60 * 1024 * 1024 for p in kept))
                self.assertTrue(all(p.stat().st_size < 1_000_000 for p in ctx.state_dir.iterdir() if 'damaged' not in p.name))

    def test_a_second_damage_the_same_second_gets_its_own_name(self):
        self.path.write_bytes(b'junk one' * 50)
        store, _ = self.open()
        store.close()
        self.path.write_bytes(b'junk two' * 50)
        _, note = self.open()
        self.assertIn('damaged-20250101T000000Z-2', note)
        self.assertEqual((self.ctx.state_dir / 'shows.sqlite3.damaged-20250101T000000Z').read_bytes(), b'junk one' * 50)
        self.assertEqual((self.ctx.state_dir / 'shows.sqlite3.damaged-20250101T000000Z-2').read_bytes(), b'junk two' * 50)

    def test_a_failed_integrity_check_counts_as_damage(self):
        class Wrapped:
            def __init__(self, real):
                self.real = real

            def execute(self, sql, *args):
                if sql == 'PRAGMA quick_check':
                    return self.real.execute("SELECT 'row 1 missing from index'")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        real_open = self.ctx.open_db
        calls = []

        def open_db(name):
            conn = real_open(name)
            calls.append(conn)
            return Wrapped(conn) if len(calls) == 1 else conn

        self.ctx.open_db = open_db
        _, note = self.open()
        self.assertIn('could not be read', note)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any('damaged' in p.name for p in self.ctx.state_dir.iterdir()))

    def test_a_busy_or_unwritable_file_is_not_treated_as_damage(self):
        for message in ('database is locked', 'unable to open database file', 'attempt to write a readonly database', 'disk I/O error'):
            with self.subTest(message=message):
                self.ctx.open_db = mock.Mock(side_effect=sqlite3.OperationalError(message))
                with self.assertRaises(ExtensionError) as caught:
                    open_database(self.ctx, NOW)
                text = str(caught.exception)
                self.assertIn('could not be opened right now', text)
                self.assertNotIn(message, text)
        self.assertEqual([p.name for p in self.ctx.state_dir.iterdir() if 'damaged' in p.name], [])

    def test_set_aside_moves_all_three_files_and_picks_unused_names(self):
        for suffix in ('', '-wal', '-shm'):
            (self.ctx.state_dir / (DB_FILE + suffix)).write_bytes(suffix.encode() or b'main')
        name = set_aside(self.ctx.state_dir, NOW)
        self.assertEqual(name, 'shows.sqlite3.damaged-20250101T000000Z')
        self.assertEqual(sorted(p.name for p in self.ctx.state_dir.iterdir()),
                         [name, name + '-shm', name + '-wal'])
        (self.ctx.state_dir / DB_FILE).write_bytes(b'again')
        self.assertEqual(set_aside(self.ctx.state_dir, NOW), name + '-2')

    def test_a_symbolic_link_is_refused_outright(self):
        outside = Path(self.temp.name) / 'elsewhere.sqlite3'
        outside.write_bytes(b'')
        os.symlink(outside, self.path)
        with self.assertRaisesRegex(ExtensionError, 'symbolic link'):
            open_database(self.ctx, NOW)


if __name__ == '__main__':
    unittest.main()
