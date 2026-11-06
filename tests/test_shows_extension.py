"""The shows extension through the real station, the real HTTP service and the extension seam's own rules."""
import http.client
import json
import os
import random
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from myradio import server as server_module
from myradio.extensions import ExtensionError, ExtensionHost, Request, StationFacade, builtin_extensions
from myradio.server import Server
from myradio.shows import MAX_SHOWS, Week, find_overlaps, hour_plan
from myradio.shows.extension import ShowsExtension, build, detect_system_timezone
from myradio.shows.store import DB_FILE, Store
from myradio.station import Station, StationError

try:
    from test_shows_support import at, junk_show, show, utc_ts
except ImportError:  # run as tests.test_shows_extension
    from tests.test_shows_support import at, junk_show, show, utc_ts

NY = 'America/New_York'
TOKEN = 't' * 40
JARGON = re.compile(r'Traceback|sqlite|SQL|Exception|Error:|<class|0x[0-9a-f]{4}|\bNone\b|\bNaN\b|\bnull\b|PCM|GGUF|SSRF|stack|errno', re.I)


class StationCase(unittest.TestCase):
    """A real Station (no playout worker) with the shows extension registered."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.boot()

    def boot(self, **options):
        options.setdefault('timezone', NY)
        self.ext = ShowsExtension(**options)
        self.station = Station(self.temp.name, start_worker=False, extensions=[self.ext])
        self.addCleanup(self.station.close)

    def reboot(self, **options):
        self.station.close()
        self.boot(**options)

    def act(self, op, **args):
        return self.station.action('shows.' + op, args or None)

    def set_clock(self, ts):
        self.ext._ctx.clock = lambda: ts

    def listed(self):
        return {s['id']: s for s in self.act('list')['shows']}

    def state(self):
        """What a refused or failed change must leave exactly as it was."""
        listing = self.act('list')
        return listing['shows'], listing['default_show'], listing['revision'], listing['timezone']

    def fresh_station(self, **options):
        """A second station in its own folder, for first-run behavior."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        ext = ShowsExtension(**options)
        station = Station(folder.name, start_worker=False, extensions=[ext])
        self.addCleanup(station.close)
        return station, ext

    def route(self, path, query=None):
        host = self.station.extensions
        match = host.find_route('GET', '/api/x/shows' + path)
        self.assertIsNotNone(match, path)
        return host.dispatch_route(match, Request('GET', path, query or {}, None, b''))

    def refused(self, op, **args):
        with self.assertRaises(StationError) as caught:
            self.act(op, **args)
        message = str(caught.exception)
        self.assertNotRegex(message, JARGON, message)
        return message


class Seam(StationCase):
    def test_the_extension_runs_and_describes_its_operations(self):
        self.assertEqual(self.station.status()['extensions']['shows']['state'], 'running')
        catalog = {c['name']: c for c in self.station.operations()}
        self.assertEqual(sorted(n for n in catalog if n.startswith('shows.')),
                         ['shows.apply_template', 'shows.list', 'shows.plan', 'shows.remove', 'shows.save', 'shows.set_timezone'])
        self.assertEqual(catalog['shows.save']['required'], ['show'])
        self.assertEqual(catalog['shows.remove']['required'], ['id'])
        self.assertEqual(catalog['shows.apply_template']['required'], ['name'])
        self.assertEqual(catalog['shows.apply_template']['arguments']['name']['enum'], ['starter', 'empty'])
        self.assertEqual(catalog['shows.plan']['required'], [])
        self.assertEqual(catalog['shows.plan']['arguments']['hours']['maximum'], 168)
        for entry in catalog.values():
            if entry['name'].startswith('shows.'):
                self.assertTrue(0 < len(entry['description']) <= 400)

    def test_it_is_a_built_in_extension(self):
        found = [e for e in builtin_extensions() if getattr(e, 'name', '') == 'shows']
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], ShowsExtension)
        self.assertIsInstance(build(), ShowsExtension)

    def test_it_consumes_no_events_so_it_never_gets_a_mailbox_thread(self):
        self.assertIsNone(self.station.extensions._slots['shows'].mailbox)
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn('myradio-ext-shows', names)

    def test_the_file_lives_in_the_extensions_own_folder_and_is_listed_for_backup(self):
        path = Path(self.temp.name) / 'ext' / 'shows' / DB_FILE
        self.assertTrue(path.is_file())
        self.assertEqual(self.station.extensions.backup_files()['shows'], [path])
        self.assertEqual(self.ext.persistent_files(), [DB_FILE])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        conn = sqlite3.connect(Path(self.temp.name) / 'station.sqlite3')
        self.addCleanup(conn.close)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn('shows', names, 'shows must not put tables in the station database')

    def test_stopping_is_clean_and_idempotent(self):
        self.ext.stop()
        self.ext.stop()
        with self.assertRaisesRegex(ExtensionError, 'not running'):
            self.ext.plan(0, 3600)
        self.station.close()
        self.assertEqual(self.station.extensions.status()['shows']['state'], 'stopped')

    def test_it_creates_no_threads(self):
        before = {t.ident for t in threading.enumerate()}
        with tempfile.TemporaryDirectory() as other:
            station = Station(other, start_worker=False, extensions=[ShowsExtension(timezone='UTC')])
            station.action('shows.apply_template', {'name': 'starter'})
            station.action('shows.plan', {'hours': 2})
            station.status()
            station.close()
        self.assertEqual({t.ident for t in threading.enumerate()} - before, set())


class Status(StationCase):
    def test_a_new_station_plays_the_default_show_all_day(self):
        status = self.station.status()['extensions']['shows']['status']
        self.assertEqual(status['ready'], True)
        self.assertEqual((status['timezone'], status['timezone_source'], status['revision']), (NY, 'configured', 1))
        self.assertEqual(status['counts'], {'shows': 0, 'overlaps': 0, 'gap_hours': 168.0})
        self.assertEqual(status['current'], {'show_id': 'default', 'name': 'Station Mix', 'host_id': None, 'fallback': True,
                                             'since': None, 'until': None})
        self.assertIsNone(status['next_change'])
        self.assertEqual((status['conflicts'], status['notes']), ([], []))

    def test_status_follows_the_clock(self):
        self.act('apply_template', name='starter')
        cases = [((2025, 1, 15, 7, 0, 0), 'morning', (2025, 1, 15, 6), (2025, 1, 15, 10), 'daytime'),
                 ((2025, 1, 15, 5, 59, 59), 'overnight', (2025, 1, 15, 0), (2025, 1, 15, 6), 'morning'),
                 ((2025, 1, 15, 10, 0, 0), 'daytime', (2025, 1, 15, 10), (2025, 1, 15, 17), 'golden'),
                 ((2025, 1, 15, 23, 59, 59), 'late', (2025, 1, 15, 20), (2025, 1, 16, 0), 'overnight'),
                 ((2025, 1, 16, 0, 0, 0), 'overnight', (2025, 1, 16, 0), (2025, 1, 16, 6), 'morning')]
        for (y, mo, d, h, mi, s), show_id, since, until, following in cases:
            self.set_clock(at(NY, y, mo, d, h, mi) + s)
            status = self.ext.status()
            with self.subTest(at=f'{h:02d}:{mi:02d}:{s:02d}'):
                self.assertEqual(status['current']['show_id'], show_id)
                self.assertEqual((status['current']['since'], status['current']['until']), (at(NY, *since), at(NY, *until)))
                self.assertEqual(status['next_change']['show_id'], following)
                self.assertEqual(status['next_change']['at'], at(NY, *until))
                self.assertFalse(status['current']['fallback'])
        self.set_clock(at(NY, 2025, 1, 15, 7))
        self.assertEqual(self.ext.status()['next_change']['at_iso'], '2025-01-15T10:00:00-05:00')

    def test_times_at_the_edges_are_epoch_seconds_as_floats(self):
        self.act('apply_template', name='starter')
        self.set_clock(at(NY, 2025, 1, 15, 7))
        status = self.ext.status()
        for value in (status['current']['since'], status['current']['until'], status['next_change']['at']):
            self.assertIsInstance(value, float)
        plan = self.act('plan', hours=1)
        for value in (plan['from'], plan['to'], plan['items'][0]['start'], plan['items'][0]['slot_start']):
            self.assertIsInstance(value, float)
        now = self.route('/week').body['now']
        for value in (now['at'], now['since'], now['until']):
            self.assertIsInstance(value, float)
        quiet = self.fresh_station(timezone='UTC')[1].status()
        self.assertEqual((quiet['current']['since'], quiet['current']['until']), (None, None))

    def test_status_is_a_cached_snapshot_not_a_recalculation(self):
        self.act('apply_template', name='starter')
        self.set_clock(at(NY, 2025, 1, 15, 7))
        from myradio.shows.week import Week as WeekClass
        with mock.patch.object(WeekClass, 'airing', autospec=True, side_effect=WeekClass.airing) as airing:
            for step in range(200):
                self.set_clock(at(NY, 2025, 1, 15, 7) + step)
                self.ext.status()
            self.assertEqual(airing.call_count, 1)
            self.set_clock(at(NY, 2025, 1, 15, 10))
            self.ext.status()
            self.assertEqual(airing.call_count, 2)

    def test_status_never_calls_the_station_and_never_waits_for_a_write(self):
        self.act('apply_template', name='starter')
        done = threading.Event()
        with mock.patch.object(Station, 'status', side_effect=AssertionError('status() must not call station.status()')), \
                self.ext._write_lock:                                         # a slow change is in progress
            worker = threading.Thread(target=lambda: (self.ext.status(), done.set()), daemon=True)
            worker.start()
            self.assertTrue(done.wait(2), 'status() must be lock-free')
        worker.join(2)

    def test_status_stays_small_and_serialisable(self):
        self.act('apply_template', name='starter')
        self.assertLess(len(json.dumps(self.ext.status())), 1500)
        shows = [show(f'x{i:03d}', start='00:00', end='24:00') for i in range(MAX_SHOWS)]
        with self.ext._write_lock:
            self.ext._commit(lambda store: store.replace_shows(shows, 1.0), Week(NY, shows))
        data = self.ext.status()
        self.assertLess(len(json.dumps(data)), 5000)
        self.assertEqual(len(data['conflicts']), 3)
        self.assertGreater(data['counts']['overlaps'], 0)

    def test_status_notes_and_conflicts_are_plain_language(self):
        clash = [show('a', 'Morning Rise'), show('b', 'Breakfast Club', start='09:00', end='11:00')]
        with self.ext._write_lock:
            self.ext._commit(lambda store: store.replace_shows(clash, 1.0), Week(NY, clash))
        status = self.ext.status()
        self.assertEqual(status['counts']['overlaps'], 1)
        self.assertEqual(status['conflicts'], ['Morning Rise (Every day, 6 am–10 am) and Breakfast Club (Every day, 9 am–11 am) '
                                               'are both scheduled every day from 9 am to 10 am.'])
        for text in status['conflicts']:
            self.assertNotRegex(text, JARGON)


class Operations(StationCase):
    def test_save_creates_updates_and_merges(self):
        made = self.act('save', show={'name': 'Breakfast Club', 'days': 'weekdays', 'start': '9:00', 'end': '11:00',
                                      'host_id': 'dex', 'talk': 'often', 'style': {'palettes': ['neo-soul'], 'energy': 80}})
        self.assertEqual((made['ok'], made['operation'], made['created'], made['revision']), (True, 'shows.save', True, 2))
        self.assertEqual(made['summary'], 'Saved Breakfast Club (Weekdays, 9 am–11 am).')
        self.assertEqual(made['show']['id'], 'breakfast-club')
        self.assertEqual(made['notes'], [])
        again = self.act('save', show={'id': 'breakfast-club', 'talk': 'rare', 'spots_allowed': True})
        self.assertEqual((again['created'], again['revision']), (False, 3))
        shown = again['show']
        self.assertEqual((shown['talk'], shown['spots_allowed'], shown['host_id'], shown['start'], shown['end']), ('rare', True, 'dex', '09:00', '11:00'))
        self.assertEqual((shown['style']['palettes'], shown['style']['energy']), (['neo-soul'], 80))
        moved = self.act('save', show={'id': 'breakfast-club', 'name': 'Late Breakfast', 'start': '09:30'})
        self.assertEqual((moved['show']['name'], moved['show']['start'], moved['show']['end']), ('Late Breakfast', '09:30', '11:00'))
        self.assertEqual(len(self.act('list')['shows']), 1)

    def test_a_new_show_without_an_id_gets_a_readable_unique_one(self):
        ids = [self.act('save', show={'name': 'Jazz', 'days': str(day), 'start': 20, 'end': 22})['show']['id'] for day in (1, 2, 3)]
        self.assertEqual(ids, ['jazz', 'jazz-2', 'jazz-3'])

    def test_notes_mention_midnight_and_a_missing_host(self):
        made = self.act('save', show={'name': 'Night Owls', 'start': '22:00', 'end': '02:00'})
        self.assertEqual(made['notes'], ['This show runs past midnight, so it carries on into the next day.',
                                         'No host is chosen, so this show has no talk breaks.'])
        self.assertEqual(made['show']['end'], '02:00')
        self.assertTrue(made['show']['crosses_midnight'])

    def test_a_show_that_would_overlap_is_refused_and_nothing_changes(self):
        self.act('save', show={'id': 'morning', 'name': 'Morning Rise', 'start': '06:00', 'end': '10:00'})
        before = self.state()
        message = self.refused('save', show={'name': 'Breakfast Club', 'days': 'weekdays', 'start': '09:00', 'end': '11:00'})
        self.assertEqual(message, 'Breakfast Club would overlap Morning Rise (Every day, 6 am–10 am) on weekdays from 9 am to 10 am. '
                                  'Start Breakfast Club at 10 am. Or make Morning Rise end at 9 am.')
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.listed()), 1)
        self.act('save', show={'id': 'morning', 'end': '09:00'})                 # follow the advice, then it fits
        self.assertEqual(self.act('save', show={'name': 'Breakfast Club', 'days': 'weekdays', 'start': '09:00', 'end': '11:00'})['created'], True)

    def test_editing_a_show_into_a_clash_is_refused_too(self):
        self.act('save', show={'id': 'a', 'name': 'A', 'start': '06:00', 'end': '08:00'})
        self.act('save', show={'id': 'b', 'name': 'B', 'start': '08:00', 'end': '10:00'})
        self.assertIn('would overlap', self.refused('save', show={'id': 'a', 'end': '09:00'}))
        self.assertIn('would overlap', self.refused('save', show={'id': 'b', 'days': '1,2', 'start': '07:00'}))
        self.assertEqual(self.listed()['a']['end'], '08:00')

    def test_a_clash_with_several_shows_says_so(self):
        for i, (start, end) in enumerate((('06:00', '08:00'), ('08:00', '10:00'), ('10:00', '12:00'))):
            self.act('save', show={'id': f's{i}', 'name': f'S{i}', 'start': start, 'end': end})
        message = self.refused('save', show={'id': 'wide', 'name': 'Wide', 'start': '07:00', 'end': '11:00'})
        self.assertIn('Wide would overlap S0', message)
        self.assertIn('It also overlaps 2 other shows.', message)

    def test_the_default_show_can_be_changed_but_not_scheduled_or_removed(self):
        changed = self.act('save', show={'id': 'default', 'name': 'Anytime Mix', 'host_id': 'nova', 'style': {'energy': 20}})
        self.assertEqual((changed['show']['name'], changed['show']['host_id'], changed['show']['is_default']), ('Anytime Mix', 'nova', True))
        self.assertEqual(self.act('list')['default_show']['style']['energy'], 20)
        self.assertEqual(self.act('list')['shows'], [])
        self.assertIn('no days, start or end', self.refused('save', show={'id': 'default', 'start': '06:00', 'end': '07:00'}))
        self.assertIn("The default show can't be removed", self.refused('remove', id='default'))
        self.assertEqual(self.act('list')['shows'], [])

    def test_remove(self):
        self.act('apply_template', name='starter')
        removed = self.act('remove', id='daytime')
        self.assertEqual((removed['removed'], removed['name']), ('daytime', 'Daytime Focus'))
        self.assertEqual(removed['summary'], 'Removed Daytime Focus. Its hours now play Station Mix, your default show, until you add another show.')
        self.assertNotIn('daytime', self.listed())
        self.assertEqual(self.ext.status()['counts']['gap_hours'], 49.0)
        self.assertIn('no show with the id daytime', self.refused('remove', id='daytime'))
        self.assertIn('no show with the id nothing-here', self.refused('remove', id=' NOTHING-HERE '))
        self.assertIn('lowercase letters', self.refused('remove', id='bad id!'))

    def test_templates_are_offered_never_applied_and_guard_existing_shows(self):
        self.assertEqual(self.act('list')['shows'], [])                             # nothing is applied on its own
        done = self.act('apply_template', name='starter')
        self.assertEqual((done['shows'], done['summary']), (['overnight', 'morning', 'daytime', 'golden', 'late'],
                                                           "Applied 'A day on the air': 5 shows."))
        message = self.refused('apply_template', name='starter')
        self.assertEqual(message, "Your week already has 5 shows, and applying 'A day on the air' would replace them. "
                                  "Ask again with replace set to true to confirm.")
        self.act('save', show={'id': 'morning', 'name': 'My Morning'})
        self.act('apply_template', name='starter', replace=True)
        self.assertEqual(self.listed()['morning']['name'], 'Morning Rise')
        cleared = self.act('apply_template', name='empty', replace=True)
        self.assertEqual((cleared['shows'], self.act('list')['shows']), ([], []))
        self.assertEqual(cleared['summary'], 'Cleared the schedule. Your default show now plays all day.')
        self.act('apply_template', name='empty')                                   # nothing to replace, so no confirmation is needed
        with self.assertRaisesRegex(StationError, 'one of: starter, empty'):
            self.act('apply_template', name='nonsense')

    def test_applying_a_template_keeps_the_default_show(self):
        self.act('save', show={'id': 'default', 'name': 'My Default'})
        self.act('apply_template', name='starter')
        self.assertEqual(self.act('list')['default_show']['name'], 'My Default')

    def test_set_timezone(self):
        done = self.act('set_timezone', timezone=' Europe/London ')
        self.assertEqual((done['timezone'], done['revision']), ('Europe/London', 2))
        self.assertEqual(done['summary'], 'Shows now follow Europe/London time. Each show keeps its clock times, so 6 am stays 6 am.')
        status = self.ext.status()
        self.assertEqual((status['timezone'], status['timezone_source']), ('Europe/London', 'chosen'))
        for bad in ('', 'Mars/Base', '../../etc/passwd', 'UTC\x00', 'x' * 64, 'Europe/', 'posixrules', 'café'):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(StationError) as caught:
                    self.act('set_timezone', timezone=bad)
                self.assertNotRegex(str(caught.exception), JARGON)
        self.assertEqual(self.ext.status()['timezone'], 'Europe/London')

    def test_a_zone_change_moves_the_shows_in_real_time_but_not_on_the_clock(self):
        self.act('apply_template', name='starter')
        self.set_clock(utc_ts(2025, 1, 15, 11, 0))                                    # 06:00 in New York
        self.assertEqual(self.ext.status()['current']['show_id'], 'morning')
        self.act('set_timezone', timezone='Europe/London')
        self.assertEqual(self.ext.status()['current']['show_id'], 'daytime')           # 11:00 in London
        self.assertEqual(self.listed()['morning']['start'], '06:00')

    def test_list_returns_what_save_accepts(self):
        self.act('apply_template', name='starter')
        listing = self.act('list')
        self.assertEqual(listing['timezone'], NY)
        self.assertEqual([s['id'] for s in listing['shows']], ['overnight', 'morning', 'daytime', 'golden', 'late'])
        for entry in listing['shows'] + [listing['default_show']]:
            echoed = self.act('save', show=entry)
            self.assertEqual(echoed['show'], entry)
        self.assertEqual(self.act('list')['shows'], listing['shows'])
        self.assertTrue(listing['conflicts']['ok'])
        self.assertNotIn('days', listing)                                             # the day map is for the screen, not for agents

    def test_plan_operation(self):
        self.act('apply_template', name='starter')
        start = at(NY, 2025, 1, 15, 6)
        plan = self.act('plan', hours=2, from_ts=start, seed=5)
        self.assertEqual((plan['from'], plan['to'], plan['seed'], plan['timezone']), (start, start + 7200, 5, NY))
        self.assertEqual(plan['from_iso'], '2025-01-15T06:00:00-05:00')
        self.assertTrue(all(i['show_id'] == 'morning' for i in plan['items']))
        expected = [i.to_dict(NY) for i in self.ext.week().plan(start, start + 7200, 5)]
        self.assertEqual(plan['items'], expected)
        self.assertEqual(self.act('plan', hours=2, from_ts=start, seed=5)['items'], expected)
        default_seed = self.act('plan', hours=1, from_ts=start)
        self.assertEqual(default_seed['seed'], self.act('list')['plan_seed'])
        self.set_clock(start + 30)
        self.assertEqual(self.act('plan')['from'], start + 30)                        # now, rounded down to the second
        self.assertEqual(len(self.act('plan')['items']) > 0, True)

    def test_typed_arguments_are_checked_before_anything_runs(self):
        calls = [('save', {}), ('save', {'show': 'text'}), ('save', {'show': []}), ('save', {'show': {'name': 'x'}, 'extra': 1}),
                 ('save', {'show': {'name': 'x' * 1_000_000}}), ('remove', {}), ('remove', {'id': ''}), ('remove', {'id': 5}),
                 ('remove', {'id': 'x' * 64}), ('apply_template', {'name': 'nope'}), ('apply_template', {'name': 'starter', 'replace': 'yes'}),
                 ('set_timezone', {'timezone': 5}), ('set_timezone', {}), ('plan', {'hours': 0}), ('plan', {'hours': 169}),
                 ('plan', {'hours': '3'}), ('plan', {'hours': True}), ('plan', {'hours': 2.5}), ('plan', {'from_ts': -1}),
                 ('plan', {'from_ts': 'now'}), ('plan', {'seed': -1}), ('plan', {'seed': 1.5}), ('list', {'verbose': True})]
        for op, args in calls:
            with self.subTest(op=op, args=str(args)[:50]):
                with self.assertRaises(StationError) as caught:
                    self.act(op, **args)
                self.assertNotRegex(str(caught.exception), JARGON)
        self.assertEqual(self.act('list')['revision'], 1)

    def test_the_plan_operation_refuses_out_of_range_values_in_plain_words(self):
        for args in ({'seed': 2 ** 63}, {'from_ts': 4102444801}, {'from_ts': 1e300}):
            with self.subTest(args=args):
                self.assertNotRegex(self.refused('plan', **args), JARGON)

    def test_hostile_shows_are_refused_in_plain_words(self):
        bad = [{'name': 'x' * 41, 'start': 1, 'end': 3}, {'name': '<script>alert(1)</script>', 'start': 1, 'end': 3},
               {'name': '[laughs] <|speaker:1|>', 'start': 1, 'end': 3}, {'name': 'ok', 'start': 'soon', 'end': 3},
               {'name': 'ok', 'start': 1, 'end': 1}, {'name': 'ok', 'start': 1, 'end': 3, 'days': 'x'},
               {'name': 'ok', 'start': 1, 'end': 3, 'style': {'energy': 1000}}, {'name': 'ok', 'start': 1, 'end': 3, 'style': {'palettes': ['x'] * 40}},
               {'name': 'ok', 'start': 1, 'end': 3, 'color': 'javascript:alert(1)'}, {'name': 'ok', 'start': 1, 'end': 3, 'host_id': '../../x'},
               {'name': 'ok', 'start': 1, 'end': 3, 'update_researcher_id': 'a b'}, {'name': 'ok', 'start': 1, 'end': 3, 'talk': 'constantly'},
               {'name': 'ok', 'start': 1, 'end': 3, 'clock': 'warp'}, {'name': 'ok', 'start': 1, 'end': 3, 'command': 'rm -rf /'},
               {'name': 'ok', 'start': 1, 'end': 3, 'spots_allowed': 'yes'}, {'id': 'default', 'name': 'x', 'start': 1, 'end': 3},
               {'id': 'A B', 'name': 'x', 'start': 1, 'end': 3}, {'name': None, 'start': 1, 'end': 3}, {'name': 'ok', 'start': 25, 'end': 3},
               {'name': 'ok', 'start': 1, 'end': 3, 'days': [0, 8]}, {'name': 'ok', 'start': 1, 'end': 3, 'style': 'loud'}]
        for show_data in bad:
            with self.subTest(show=str(show_data)[:60]):
                self.assertTrue(self.refused('save', show=show_data))
        self.assertEqual(self.act('list')['shows'], [])
        self.assertEqual(self.act('list')['revision'], 1)

    def test_the_cap_applies_to_the_operation_too(self):
        shows = []
        for i in range(MAX_SHOWS):                                                   # 200 non-overlapping shows: 29 per day in 45 minute slots
            start = (i // 7) * 45
            shows.append(show(f's{i:03d}', start=f'{start // 60:02d}:{start % 60:02d}', end=f'{(start + 45) // 60:02d}:{(start + 45) % 60:02d}',
                              days=str(i % 7 + 1)))
        self.assertEqual(find_overlaps(shows)[0], [])
        with self.ext._write_lock:
            self.ext._commit(lambda store: store.replace_shows(shows, 1.0), Week(NY, shows))
        self.assertEqual(len(self.act('list')['shows']), MAX_SHOWS)
        message = self.refused('save', show={'name': 'One too many', 'start': '23:30', 'end': '23:50', 'days': '1'})
        self.assertIn('200 shows', message)
        self.assertIn('Remove one before adding another', message)
        self.refused('apply_template', name='starter')
        self.act('remove', id='s000')
        self.assertTrue(self.act('save', show={'name': 'Fits Now', 'start': '00:00', 'end': '00:45', 'days': '1'})['created'])
        updated = self.act('save', show={'id': 's001', 'name': 'Renamed'})             # editing at the cap is fine
        self.assertEqual(updated['created'], False)

    def test_every_refusal_reads_like_plain_language(self):
        messages = []
        self.act('save', show={'id': 'morning', 'name': 'Morning', 'start': '06:00', 'end': '10:00'})
        for op, args in (('save', {'show': {'name': 'x' * 50, 'start': 1, 'end': 3}}), ('save', {'show': {'name': 'Yoga', 'start': '6am', 'end': 3}}),
                         ('save', {'show': {'name': 'Yoga', 'start': 7, 'end': 9}}), ('remove', {'id': 'nope'}), ('remove', {'id': 'default'}),
                         ('set_timezone', {'timezone': 'Nowhere/Land'}), ('apply_template', {'name': 'starter'}), ('plan', {'seed': 2 ** 70})):
            with self.assertRaises(StationError) as caught:
                self.act(op, **args)
            messages.append(str(caught.exception))
        for message in messages:
            self.assertRegex(message, r'^[A-Z0-9].{15,}[.?]$', message)
            self.assertNotRegex(message, JARGON, message)
            self.assertLess(len(message), 400)

    def test_a_failed_write_changes_nothing_and_the_next_one_works(self):
        self.act('save', show={'id': 'a', 'name': 'A', 'start': '06:00', 'end': '07:00'})
        before = self.state()
        with mock.patch.object(Store, 'save_show', side_effect=sqlite3.OperationalError('disk I/O error')):
            message = self.refused('save', show={'id': 'b', 'name': 'B', 'start': '08:00', 'end': '09:00'})
        self.assertEqual(message, 'Your change could not be saved, so nothing was changed. Check that the disk has free space and try again.')
        self.assertEqual(self.state(), before)
        with mock.patch.object(Store, 'delete_show', side_effect=sqlite3.OperationalError('x')):
            self.refused('remove', id='a')
        with mock.patch.object(Store, 'replace_shows', side_effect=sqlite3.OperationalError('x')):
            self.refused('apply_template', name='starter', replace=True)
        with mock.patch.object(Store, 'set_timezone', side_effect=sqlite3.OperationalError('x')):
            self.refused('set_timezone', timezone='Asia/Tokyo')
        self.assertEqual(self.state(), before)
        self.assertTrue(self.act('save', show={'id': 'b', 'name': 'B', 'start': '08:00', 'end': '09:00'})['created'])


class Persistence(StationCase):
    def test_everything_survives_a_restart(self):
        self.act('apply_template', name='starter', replace=True)
        self.act('save', show={'id': 'daytime', 'days': '1,2,3,4,5,6'})
        self.act('save', show={'name': 'Sunday Brunch', 'days': '7', 'start': '10:00', 'end': '12:00', 'host_id': 'nova', 'color': '#123456'})
        self.act('save', show={'id': 'default', 'name': 'Anytime Mix', 'style': {'tags': ['late-night']}})
        self.act('set_timezone', timezone='Pacific/Auckland')
        before = self.act('list')
        plan = self.act('plan', hours=48, from_ts=1_736_000_000)
        self.reboot(timezone='America/Chicago')                                       # an argument for a brand-new file only
        after = self.act('list')
        self.assertEqual(after['shows'], before['shows'])
        self.assertEqual((after['timezone'], after['plan_seed'], after['revision'], after['default_show']),
                         (before['timezone'], before['plan_seed'], before['revision'], before['default_show']))
        self.assertEqual(self.act('plan', hours=48, from_ts=1_736_000_000)['items'], plan['items'])
        self.assertEqual(self.ext.status()['timezone_source'], 'chosen')

    def test_names_in_any_script_survive_a_restart_and_two_shows_may_share_a_name(self):
        names = ['Caf\u00e9 9\u20135 & Co.', "Mike's Show", '\u6df1\u591c\u30e9\u30b8\u30aa', 'Late Night', 'Late Night']
        for index, name in enumerate(names):
            self.act('save', show={'name': name, 'start': f'{index * 2 + 1:02d}:00', 'end': f'{index * 2 + 2:02d}:00'})
        before = self.listed()
        self.assertEqual(sorted(s['name'] for s in before.values()), sorted(names))
        self.assertEqual(len(before), 5)
        self.reboot()
        self.assertEqual(self.listed(), before)
        self.assertEqual(sorted(before)[:2], ['cafe-9-5-co', 'late-night'])

    def test_a_copied_main_file_restores_the_whole_week_on_another_station(self):
        self.act('apply_template', name='starter')
        self.act('save', show={'id': 'daytime', 'days': '1,2,3,4,5,6'})                  # Sundays are free for brunch
        self.act('save', show={'name': 'Sunday Brunch', 'days': '7', 'start': '10:00', 'end': '12:00'})
        before = self.act('list')
        source = self.station.extensions.backup_files()['shows'][0]
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / 'ext' / 'shows'
            target.mkdir(parents=True, mode=0o700)
            shutil.copyfile(source, target / DB_FILE)                                  # the main file alone, as a backup would carry it
            restored = Station(other, start_worker=False, extensions=[ShowsExtension(timezone='UTC')])
            try:
                again = restored.action('shows.list')
                self.assertEqual((again['shows'], again['timezone'], again['plan_seed']), (before['shows'], before['timezone'], before['plan_seed']))
            finally:
                restored.close()

    def test_a_damaged_file_is_set_aside_and_the_station_still_starts(self):
        self.act('apply_template', name='starter')
        path = Path(self.temp.name) / 'ext' / 'shows' / DB_FILE
        self.station.close()
        path.write_bytes(b'not a database at all' * 40)
        self.boot()
        self.assertEqual(self.station.status()['extensions']['shows']['state'], 'running')
        self.assertEqual(self.act('list')['shows'], [])
        notes = self.ext.status()['notes']
        self.assertEqual(len(notes), 1)
        self.assertIn('could not be read', notes[0])
        checks = {c['id']: c for c in self.station.extensions.checks()}
        self.assertEqual(checks['shows.data']['status'], 'warn')
        self.assertTrue(any('damaged' in p.name for p in path.parent.iterdir()))
        self.act('apply_template', name='starter')                                    # and it can be used again right away

    def test_a_newer_file_stops_the_extension_with_a_plain_message_and_keeps_the_data(self):
        self.act('apply_template', name='starter')
        path = Path(self.temp.name) / 'ext' / 'shows' / DB_FILE
        self.station.close()
        conn = sqlite3.connect(path)
        conn.execute('PRAGMA user_version=99')
        conn.commit()
        conn.close()
        before = path.read_bytes()
        self.boot()
        entry = self.station.status()['extensions']['shows']
        self.assertEqual(entry['state'], 'failed')
        self.assertIn('newer myradio', entry['error'])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(StationError):
            self.act('list')
        self.assertEqual({c['id']: c['status'] for c in self.station.extensions.checks()}['shows.start'], 'fail')


class Routes(StationCase):
    def setUp(self):
        super().setUp()
        self.act('apply_template', name='starter')
        self.now = at(NY, 2025, 1, 15, 7, 30)
        self.set_clock(self.now)

    def ok(self, path, query=None):
        response = self.route(path, query)
        self.assertEqual(response.status, 200, response.body)
        json.dumps(response.body)
        return response.body

    def bad(self, path, query, status=400):
        response = self.route(path, query)
        self.assertEqual(response.status, status, response.body)
        message = response.body['error']
        self.assertNotRegex(message, JARGON, message)
        return message

    def test_week(self):
        week = self.ok('/week')
        self.assertEqual((week['timezone'], week['timezone_source'], week['revision']), (NY, 'configured', 2))
        self.assertEqual([s['id'] for s in week['shows']], ['overnight', 'morning', 'daytime', 'golden', 'late'])
        self.assertEqual(week['default_show']['id'], 'default')
        self.assertEqual(len(week['days']), 7)
        self.assertEqual([b['show_id'] for b in week['days'][0]['blocks']], ['overnight', 'morning', 'daytime', 'golden', 'late'])
        self.assertEqual((week['now']['show_id'], week['now']['name'], week['now']['next']['show_id']), ('morning', 'Morning Rise', 'daytime'))
        self.assertEqual(week['now']['until_iso'], '2025-01-15T10:00:00-05:00')
        self.assertEqual([t['name'] for t in week['templates']], ['starter', 'empty'])
        self.assertEqual(week['templates'][0]['show_count'], 5)
        self.assertEqual([t['name'] for t in week['clock_templates']], ['classic', 'music-heavy', 'talky'])
        self.assertEqual(week['limits'], {'max_shows': 200, 'max_name_length': 40, 'min_minutes': 15, 'talk_levels': ['rare', 'some', 'often'],
                                          'max_plan_hours': 168})
        self.assertTrue(week['conflicts']['ok'])
        self.assertEqual(week['notes'], [])
        self.assertIsInstance(week['plan_seed'], int)

    def test_week_before_any_show_is_added(self):
        self.act('apply_template', name='empty', replace=True)
        week = self.ok('/week')
        self.assertEqual(week['shows'], [])
        self.assertEqual([b['show_id'] for b in week['days'][0]['blocks']], ['default'])
        self.assertEqual(week['conflicts']['gaps'][0]['fixes'][0]['arguments'], {'name': 'starter'})
        self.assertEqual(week['now']['show_id'], 'default')

    def test_plan(self):
        body = self.ok('/plan', {'hours': ['3'], 'from': [str(self.now)]})
        self.assertEqual((body['hours'], body['from'], body['to'], body['count']), (3, self.now, self.now + 3 * 3600, len(body['items'])))
        self.assertTrue(all(self.now <= i['start'] < self.now + 3 * 3600 for i in body['items']))
        self.assertEqual(body['seed'], self.act('list')['plan_seed'])
        self.assertEqual(body['items'], self.ok('/plan', {'hours': ['3'], 'from': ['2025-01-15T07:30:00-05:00']})['items'])
        self.assertEqual(body['items'], self.ok('/plan', {'hours': ['3'], 'from': ['2025-01-15T12:30:00Z']})['items'])
        self.assertEqual(self.ok('/plan')['hours'], 24)                                # default length, from now
        self.assertEqual(self.ok('/plan')['from'], self.now)
        self.assertEqual(self.ok('/plan', {'hours': ['1'], 'seed': ['7']})['seed'], 7)
        self.assertNotEqual(self.ok('/plan', {'hours': ['1'], 'seed': ['7']})['items'], self.ok('/plan', {'hours': ['1'], 'seed': ['8']})['items'])
        self.assertEqual(len(self.ok('/plan', {'hours': ['168']})['items']) > 2000, True)

    def test_plan_refuses_unbounded_or_confusing_requests(self):
        for hours in ('0', '-1', '169', '1000000', 'x', '1.5', '1e3', '9' * 50, '\u0663', '+5', '0x10'):
            with self.subTest(hours=hours):
                self.assertIn('hours must be a whole number from 1 to 168', self.bad('/plan', {'hours': [hours]}))
        self.assertEqual(self.ok('/plan', {'hours': [' ']})['hours'], 24)           # blank means "not given"
        for seed in ('-1', 'x', str(2 ** 63), '1.5', '\u0663'):
            with self.subTest(seed=seed):
                self.assertIn('seed must be a whole number', self.bad('/plan', {'seed': [seed]}))
        for text in ('tomorrow', '2025-01-15T07:30:00', '2025-01-15', '-5', '2025-13-45T00:00:00+00:00'):
            with self.subTest(start=text):
                self.assertIn('time zone offset', self.bad('/plan', {'from': [text]}))
        self.assertIn('outside the dates', self.bad('/plan', {'from': ['99999999999']}))
        self.assertIn('too long', self.bad('/plan', {'from': ['x' * 200]}))

    def test_clock(self):
        body = self.ok('/clock', {'show_id': ['morning']})
        self.assertEqual((body['show_id'], body['show_name'], body['template'], body['total_seconds'], body['seed']),
                         ('morning', 'Morning Rise', 'classic', 3600, None))
        self.assertEqual([r['role'] for r in body['summary']], ['id', 'song', 'host_break', 'spot', 'update'])
        self.assertEqual([r['count'] for r in body['summary']], [1, 14, 3, 1, 1])
        self.assertEqual(sum(r['est_seconds'] for r in body['items']), 3600)
        self.assertEqual(body['items'][0], {'role': 'id', 'kind': 'speech', 'est_seconds': 10, 'offset_seconds': 0, 'length': '0:10'})
        self.assertEqual(body['music_seconds'], 3320)
        self.assertEqual(body['item_count'], len(body['items']))
        self.assertEqual([t['name'] for t in body['templates']], ['classic', 'music-heavy', 'talky'])
        talky = self.ok('/clock', {'show_id': ['morning'], 'template': ['talky']})
        self.assertEqual((talky['template'], talky['template_title'], sum(r['est_seconds'] for r in talky['items'])), ('talky', 'More talk', 3600))
        self.assertLess(talky['music_seconds'], body['music_seconds'])
        seeded = self.ok('/clock', {'show_id': ['morning'], 'seed': ['9']})
        self.assertEqual(seeded['seed'], 9)
        self.assertEqual(sum(r['est_seconds'] for r in seeded['items']), 3600)
        self.assertEqual(seeded, self.ok('/clock', {'show_id': ['morning'], 'seed': ['9']}))
        self.assertTrue(any('no host' in n for n in self.ok('/clock', {'show_id': ['overnight']})['notes']))
        self.assertEqual(self.ok('/clock', {'show_id': ['default']})['show_name'], 'Station Mix')

    def test_clock_what_if_settings_preview_changes_without_saving_anything(self):
        before = self.state()
        plain = self.ok('/clock', {'show_id': ['overnight']})
        self.assertEqual((plain['overrides'], [r['role'] for r in plain['summary']]), ({}, ['id', 'song']))
        busy = self.ok('/clock', {'show_id': ['overnight'], 'talk': ['often'], 'host_id': ['dex'], 'spots_allowed': ['true'],
                                  'updates_allowed': ['1'], 'template': ['talky']})
        self.assertEqual(busy['overrides'], {'talk': 'often', 'host_id': 'dex', 'spots_allowed': True, 'updates_allowed': True, 'template': 'talky'})
        expected = hour_plan(show('o', host_id='dex', talk='often', spots_allowed=True, updates_allowed=True, clock='talky'))
        self.assertEqual([(r['role'], r['est_seconds']) for r in busy['items']], [(i.role, i.est_seconds) for i in expected])
        quiet = self.ok('/clock', {'show_id': ['morning'], 'host_id': ['none'], 'spots_allowed': ['off'], 'updates_allowed': ['no']})
        self.assertEqual([r['role'] for r in quiet['summary']], ['id', 'song'])
        self.assertTrue(any('no host' in n for n in quiet['notes']))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ok('/clock', {'show_id': ['overnight']}), plain)
        for query, text in (({'talk': ['loud']}, 'rare, some or often'), ({'host_id': ['Bad Id!']}, 'lowercase letters'),
                            ({'spots_allowed': ['maybe']}, 'true or false'), ({'updates_allowed': ['2']}, 'true or false')):
            with self.subTest(query=query):
                self.assertIn(text, self.bad('/clock', dict(query, show_id=['morning'])))

    def test_clock_errors(self):
        self.assertIn('Choose a show to preview', self.bad('/clock', {}))
        self.assertIn('There is no show with that id', self.bad('/clock', {'show_id': ['nope']}, 404))
        self.assertIn('lowercase letters', self.bad('/clock', {'show_id': ['Bad Id!']}))
        self.assertIn('Choose an hour style', self.bad('/clock', {'show_id': ['morning'], 'template': ['fast']}))
        self.assertIn('seed', self.bad('/clock', {'show_id': ['morning'], 'seed': ['-3']}))
        self.assertIn('too long', self.bad('/clock', {'show_id': ['x' * 500]}))

    def test_conflicts(self):
        clean = self.ok('/conflicts')
        self.assertEqual((clean['ok'], clean['overlaps'], clean['gaps'], clean['overlap_count']), (True, [], [], 0))
        clash = [s for s in self.ext.week().shows] + [show('club', 'Breakfast Club', start='09:00', end='11:00', days='weekdays')]
        with self.ext._write_lock:
            self.ext._commit(lambda store: store.replace_shows(clash, 1.0), Week(NY, clash))
        body = self.ok('/conflicts')
        self.assertFalse(body['ok'])
        self.assertEqual(body['overlap_count'], 2)
        first = body['overlaps'][0]
        self.assertEqual((first['kind'], first['severity'], first['show_ids'], first['days'], first['start'], first['end']),
                         ('overlap', 'error', ['morning', 'club'], '1,2,3,4,5', '09:00', '10:00'))
        self.assertTrue(first['message'].startswith('Morning Rise (Every day, 6 am–10 am) and Breakfast Club'))
        self.assertEqual([f['operation'] for f in first['fixes']], ['shows.save', 'shows.save', 'shows.remove'])
        self.assertEqual(self.ext.status()['counts']['overlaps'], 2)
        checks = {c['id']: c for c in self.station.extensions.checks()}
        self.assertEqual(checks['shows.schedule']['status'], 'warn')
        self.assertIn('Breakfast Club', checks['shows.schedule']['detail'])
        # The suggested fix really works when it is sent back through the operations.
        fix = first['fixes'][0]
        self.station.action(fix['operation'], fix['arguments'])
        self.assertEqual(self.ok('/conflicts')['overlap_count'], 1)


class Checks(StationCase):
    def check(self, name):
        return {c['id']: c for c in self.station.extensions.checks()}[f'shows.{name}']

    def test_an_ordinary_week(self):
        self.act('apply_template', name='starter')
        self.assertEqual((self.check('timezone')['status'], self.check('timezone')['detail']), ('ok', 'Shows follow America/New_York time.'))
        self.assertEqual((self.check('schedule')['status'], self.check('schedule')['detail']), ('ok', '5 shows cover every hour of the week.'))
        self.assertNotIn('shows.data', {c['id'] for c in self.station.extensions.checks()})

    def test_no_shows_yet_is_fine_and_says_what_to_do(self):
        check = self.check('schedule')
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(check['detail'], 'No shows are scheduled, so Station Mix plays all day.')
        self.assertIn('ready-made week', check['hint'])

    def test_gaps_are_reported_without_alarm(self):
        self.act('apply_template', name='starter')
        self.act('remove', id='daytime')
        check = self.check('schedule')
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(check['detail'], '4 shows. About 49 hours a week use Station Mix, your default show.')

    def test_an_unknown_time_zone_is_flagged(self):
        station, ext = self.fresh_station(detector=lambda: None)
        check = {c['id']: c for c in station.extensions.checks()}['shows.timezone']
        self.assertEqual(check['status'], 'warn')
        self.assertIn('could not tell which time zone', check['detail'])
        self.assertIn('Choose your time zone', check['hint'])
        self.assertEqual(ext.status()['timezone'], 'UTC')
        station.action('shows.set_timezone', {'timezone': 'Europe/Paris'})
        self.assertEqual({c['id']: c for c in station.extensions.checks()}['shows.timezone']['status'], 'ok')

    def test_every_check_is_well_formed(self):
        self.act('apply_template', name='starter')
        for check in self.station.extensions.checks():
            self.assertIn(check['status'], ('ok', 'warn', 'fail', 'unknown'))
            self.assertNotRegex(check['detail'] + check['hint'], JARGON)


class FirstRun(StationCase):
    def test_the_time_zone_comes_from_the_computer_when_it_can(self):
        _, ext = self.fresh_station(detector=lambda: 'Asia/Tokyo')
        status = ext.status()
        self.assertEqual((status['timezone'], status['timezone_source']), ('Asia/Tokyo', 'detected'))

    def test_a_detector_that_fails_or_lies_falls_back_to_utc(self):
        def explode():
            raise RuntimeError('no idea')

        for detector in (explode, lambda: 'Mars/Base', lambda: '../../x', lambda: '', lambda: None):
            with self.subTest(detector=detector):
                _, ext = self.fresh_station(detector=detector)
                status = ext.status()
                self.assertEqual((status['timezone'], status['timezone_source']), ('UTC', 'fallback'))

    def test_detection_only_runs_for_a_brand_new_file(self):
        calls = []
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for round_ in range(3):
            station = Station(folder.name, start_worker=False, extensions=[ShowsExtension(detector=lambda: calls.append(1) or 'Asia/Tokyo')])
            try:
                self.assertEqual(station.action('shows.list')['timezone'], 'Asia/Tokyo')
            finally:
                station.close()
        self.assertEqual(calls, [1])

    def test_a_chosen_zone_is_not_overridden_by_detection(self):
        _, ext = self.fresh_station(timezone='Europe/Madrid', detector=lambda: 'Asia/Tokyo')
        self.assertEqual((ext.status()['timezone'], ext.status()['timezone_source']), ('Europe/Madrid', 'configured'))

    def test_an_invalid_configured_zone_stops_the_extension_with_a_plain_message(self):
        with tempfile.TemporaryDirectory() as other:
            station = Station(other, start_worker=False, extensions=[ShowsExtension(timezone='Mars/Base')])
            try:
                entry = station.status()['extensions']['shows']
                self.assertEqual(entry['state'], 'failed')
                self.assertIn('not recognized', entry['error'])
            finally:
                station.close()

    def test_detection_reads_the_environment_and_the_standard_files(self):
        with tempfile.TemporaryDirectory() as folder:
            link, plain = Path(folder) / 'localtime', Path(folder) / 'timezone'
            missing = {'localtime': str(link), 'timezone_file': str(plain)}
            self.assertIsNone(detect_system_timezone({}, **missing))
            self.assertEqual(detect_system_timezone({'TZ': ':Europe/Paris'}, **missing), 'Europe/Paris')
            self.assertEqual(detect_system_timezone({'TZ': 'Asia/Tokyo'}, **missing), 'Asia/Tokyo')
            os.symlink('/usr/share/zoneinfo/Africa/Cairo', link)                      # Linux layout
            self.assertEqual(detect_system_timezone({}, **missing), 'Africa/Cairo')
            link.unlink()
            os.symlink('/var/db/timezone/zoneinfo/America/Chicago', link)             # macOS layout
            self.assertEqual(detect_system_timezone({}, **missing), 'America/Chicago')
            link.unlink()
            os.symlink('../usr/share/zoneinfo.default/Australia/Sydney', link)
            self.assertEqual(detect_system_timezone({}, **missing), 'Australia/Sydney')
            link.unlink()
            plain.write_text('Europe/Berlin\n')                                        # Debian layout
            self.assertEqual(detect_system_timezone({}, **missing), 'Europe/Berlin')
            self.assertEqual(detect_system_timezone({'TZ': 'Asia/Tokyo'}, **missing), 'Asia/Tokyo')    # the variable wins

    def test_detection_ignores_junk(self):
        with tempfile.TemporaryDirectory() as folder:
            link, plain = Path(folder) / 'localtime', Path(folder) / 'timezone'
            where = {'localtime': str(link), 'timezone_file': str(plain)}
            for junk in ('../../etc/passwd', 'UTC\x00', 'A' * 500, 'Mars/Base', 'EST5EDT,M3.2.0,M11.1.0', '/etc/passwd', ' '):
                self.assertIsNone(detect_system_timezone({'TZ': junk}, **where), junk)
            os.symlink('/etc/passwd', link)
            plain.write_bytes(b'\xff\xfe binary \x00 junk' * 20)
            self.assertIsNone(detect_system_timezone({'TZ': 'zone.tab'}, **where))
            link.unlink()
            os.symlink('/usr/share/zoneinfo/../../../etc/shadow', link)
            self.assertIsNone(detect_system_timezone({}, **where))


class Concurrency(StationCase):
    def test_parallel_changes_and_reads_stay_consistent(self):
        errors = []

        def writer(day):
            for slot in range(4):
                try:
                    self.act('save', show={'id': f'd{day}-{slot}', 'name': f'Day {day} slot {slot}', 'days': str(day),
                                           'start': f'{slot * 5 + 1:02d}:00', 'end': f'{slot * 5 + 3:02d}:00'})
                except Exception as exc:
                    errors.append(exc)

        def reader():
            for _ in range(25):
                try:
                    self.ext.status()
                    self.act('list')
                    self.act('plan', hours=2)
                    self.route('/week')
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(day,)) for day in range(1, 7)] + [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse([t for t in threads if t.is_alive()])
        self.assertEqual(errors, [])
        shows = self.listed()
        self.assertEqual(len(shows), 24)
        self.assertEqual(self.act('list')['revision'], 1 + 24)
        saved = list(self.ext.week().shows)
        self.station.close()
        conn = sqlite3.connect(Path(self.temp.name) / 'ext' / 'shows' / DB_FILE)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shows WHERE is_default = 0").fetchone()[0], 24)
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0], '25')
        finally:
            conn.close()
        self.assertEqual(len(saved), 24)

    def test_racing_overlapping_saves_never_leave_an_overlap(self):
        outcomes = []

        def attempt(name):
            try:
                self.act('save', show={'id': name, 'name': name, 'start': '06:00', 'end': '10:00'})
                outcomes.append('saved')
            except StationError as exc:
                outcomes.append('refused')

        threads = [threading.Thread(target=attempt, args=(f'racer-{i}',)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes), ['refused'] * 7 + ['saved'])
        self.assertEqual(find_overlaps(self.ext.week().shows)[0], [])


class QuietStation:
    """What the extension host needs from a station when no real one is wanted."""

    def status(self):
        raise AssertionError('the shows extension must never ask the station for its status')

    def import_audio(self, *args, **kwargs):
        raise AssertionError('the shows extension never imports audio')

    def action(self, operation, arguments=None):
        raise AssertionError('the shows extension never drives the station')


class RandomOperations(unittest.TestCase):
    """A long random run of operations. After every one, the saved file must describe exactly what the
    running extension believes, no overlap may exist, and a refused operation must change nothing."""

    ZONES = ('UTC', NY, 'Europe/London', 'Asia/Kolkata', 'Australia/Lord_Howe', 'Mars/Base', '', '../etc')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = ExtensionHost(self.temp.name, StationFacade(QuietStation()))
        self.ext = ShowsExtension(timezone='UTC')
        self.host.register(self.ext)
        self.host.start()
        self.addCleanup(self.host.stop)
        self.path = Path(self.temp.name) / 'ext' / 'shows' / DB_FILE

    def snapshot(self):
        week = self.ext.week()
        return week.shows, week.default, week.timezone, self.ext._snap.revision

    def saved(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            loaded = Store(conn).load()
        finally:
            conn.close()
        return loaded.week.shows, loaded.week.default, loaded.week.timezone, loaded.revision

    def random_show(self, rng, known):
        data = {}
        if known and rng.random() < 0.5:
            data['id'] = rng.choice(known)
        else:
            data['name'] = f'Show {rng.randrange(1000)}'
        if 'id' not in data or rng.random() < 0.5:
            start = rng.randrange(0, 1440, rng.choice((1, 15, 60)))
            length = rng.choice((15, 30, 45, 60, 90, 120, 180, 480, 600))
            end = (start + length) % 1440 or 1440
            if rng.random() < 0.9:
                data.update(start=f'{start // 60:02d}:{start % 60:02d}', end=f'{end // 60:02d}:{end % 60:02d}')
            data['days'] = sorted(rng.sample(range(1, 8), rng.randrange(1, 8)))
        for field, choices in (('talk', ('rare', 'some', 'often')), ('clock', ('classic', 'music-heavy', 'talky')),
                               ('host_id', (None, 'dex', 'nova')), ('spots_allowed', (True, False)), ('updates_allowed', (True, False))):
            if rng.random() < 0.3:
                data[field] = rng.choice(choices)
        if rng.random() < 0.2:
            data['style'] = {'energy': rng.randrange(0, 101), 'palettes': rng.sample(['neo-soul', 'dub-techno', 'tango-nuevo'], rng.randrange(0, 3))}
        if rng.random() < 0.1:
            data[rng.choice(('start', 'end', 'days', 'talk', 'energy', 'color'))] = rng.choice(('junk', 99, -1, None, [], 'x' * 80))
        return data

    def test_hundreds_of_random_operations(self):
        rng = random.Random(8675309)
        outcomes = {'ok': 0, 'refused': 0}
        for step in range(250):
            known = [s.id for s in self.ext.week().shows]
            roll = rng.random()
            if roll < 0.55:
                name, args = 'shows.save', {'show': self.random_show(rng, known)}
            elif roll < 0.75:
                name, args = 'shows.remove', {'id': rng.choice(known + ['nope', 'default'])}
            elif roll < 0.85:
                name, args = 'shows.apply_template', {'name': rng.choice(('starter', 'empty')), 'replace': rng.random() < 0.7}
            elif roll < 0.92:
                name, args = 'shows.set_timezone', {'timezone': rng.choice(self.ZONES)}
            else:
                name, args = 'shows.plan', {'hours': rng.randrange(1, 30), 'seed': rng.randrange(1000)}
            before = self.snapshot()
            try:
                result = self.host.call(name, args)
            except ExtensionError as exc:
                outcomes['refused'] += 1
                self.assertEqual(self.snapshot(), before, f'step {step}: {name} {args} was refused but changed something')
                self.assertNotRegex(str(exc), JARGON)
            else:
                outcomes['ok'] += 1
                json.dumps(result)
            after = self.snapshot()
            self.assertEqual(find_overlaps(after[0])[0], [], f'step {step}: {name} {args} left an overlap')
            self.assertEqual(self.saved(), after, f'step {step}: the file and the running extension disagree after {name}')
            self.assertLessEqual(len(after[0]), MAX_SHOWS)
            json.dumps(self.ext.status())
            if name in ('shows.plan',) or before == after:
                self.assertEqual(before[3], after[3])
        self.assertGreater(outcomes['ok'], 60)
        self.assertGreater(outcomes['refused'], 20)
        self.assertGreater(len(self.ext.week().shows) + outcomes['ok'], 0)


class HostileOperations(StationCase):
    def test_random_payloads_through_the_real_operation_never_hit_the_generic_failure_path(self):
        """A failure the code did not expect would reach people as 'The shows operation failed'. Anything wrong with
        the input must be a specific, plain refusal instead."""
        rng = random.Random(99)
        self.act('save', show={'id': 'keeper', 'name': 'Keeper', 'start': '12:00', 'end': '13:00'})
        refused = saved = 0
        for _ in range(400):
            before = self.state()
            try:
                self.act('save', show=junk_show(rng))
            except StationError as exc:
                refused += 1
                self.assertNotIn('operation failed', str(exc))
                self.assertNotRegex(str(exc), JARGON)
                self.assertEqual(self.state(), before)
            else:
                saved += 1
            self.assertEqual(find_overlaps(self.ext.week().shows)[0], [])
        self.assertGreater(refused, 150)
        self.assertEqual(self.ext._snap.revision, 2 + saved)
        self.assertEqual(self.station.extensions._slots['shows'].errors, 0, 'the host counts every unexpected exception')


class HttpIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        web = Path(self.temp.name) / 'web'
        (web / 'panels').mkdir(parents=True)
        patcher = mock.patch.object(server_module, 'WEB', web)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ext = ShowsExtension(timezone=NY)
        self.station = Station(Path(self.temp.name) / 'data', start_worker=False, extensions=[self.ext])
        with mock.patch.dict('os.environ', {}, clear=True):
            self.server = Server(('127.0.0.1', 0), self.station, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.station.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.station.action('shows.apply_template', {'name': 'starter'})

    def call(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        sent = dict(headers or {})
        if token:
            sent['X-Myradio-Token'] = TOKEN
        if body is not None:
            sent['Content-Type'] = 'application/json'
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def test_the_routes_over_http(self):
        status, week = self.call('GET', '/api/x/shows/week')
        self.assertEqual((status, [s['id'] for s in week['shows']]), (200, ['overnight', 'morning', 'daytime', 'golden', 'late']))
        status, plan = self.call('GET', '/api/x/shows/plan?hours=2&from=2025-01-15T06:00:00-05:00&seed=3')
        self.assertEqual((status, plan['hours'], plan['seed'], plan['from']), (200, 2, 3, at(NY, 2025, 1, 15, 6)))
        status, clock = self.call('GET', '/api/x/shows/clock?show_id=morning&template=music-heavy')
        self.assertEqual((status, clock['template'], clock['total_seconds']), (200, 'music-heavy', 3600))
        status, conflicts = self.call('GET', '/api/x/shows/conflicts')
        self.assertEqual((status, conflicts['ok']), (200, True))

    def test_errors_over_http_are_plain_and_bounded(self):
        for path, expected in (('/api/x/shows/plan?hours=0', 400), ('/api/x/shows/plan?hours=169', 400), ('/api/x/shows/plan?hours=abc', 400),
                               ('/api/x/shows/plan?from=yesterday', 400), ('/api/x/shows/clock', 400), ('/api/x/shows/clock?show_id=nope', 404),
                               ('/api/x/shows/missing', 404)):
            with self.subTest(path=path):
                status, body = self.call('GET', path)
                self.assertEqual(status, expected)
                self.assertNotRegex(json.dumps(body), JARGON)

    def test_authentication_and_methods_are_enforced_by_the_host(self):
        self.assertEqual(self.call('GET', '/api/x/shows/week', token=False)[0], 401)
        self.assertEqual(self.call('GET', '/api/x/shows/week', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.call('GET', '/api/x/shows/week', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.call('POST', '/api/x/shows/week', {})[0], 404)
        self.assertEqual(self.call('POST', '/api/x/shows/save', {'show': {}})[0], 404)            # changes go through operations, not routes

    def test_operations_over_http(self):
        self.station.action('shows.apply_template', {'name': 'empty', 'replace': True})
        status, body = self.call('POST', '/api/action', {'operation': 'shows.save', 'arguments': {'show': {
            'name': 'Sunday Brunch', 'days': '7', 'start': '10:00', 'end': '12:00'}}})
        self.assertEqual((status, body['ok'], body['show']['id']), (200, True, 'sunday-brunch'))
        status, body = self.call('POST', '/api/action', {'operation': 'shows.save', 'arguments': {'show': {
            'name': 'Clash', 'days': '7', 'start': '11:00', 'end': '13:00'}}})
        self.assertEqual(status, 400)
        self.assertIn('would overlap', body['error'])
        status, catalog = self.call('GET', '/api/operations')
        self.assertIn('shows.plan', {c['name'] for c in catalog})


if __name__ == '__main__':
    unittest.main()
