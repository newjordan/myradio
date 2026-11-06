"""The research extension through the real host, station and HTTP server: operations, routes, validation and safety."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from myradio.extensions import ExtensionError, ExtensionHost, StationFacade, builtin_extensions
from myradio.research.extension import RANGES, ResearchExtension, build, read_settings
from myradio.research.errors import FetchError
from myradio.research.fetcher import MAX_BACKOFF_WAIT, Fetcher, RequestBudget
from myradio.research.service import PersistentBudget
from myradio.server import Server
from myradio.station import Station, StationError
from test_research_support import (FakeClock, FakeStation, FixtureServer, Harness, item, loopback_fetcher, research_threads, researcher, response, rss,
                                   wait_until)

TOKEN = 'r' * 40


class HostCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = FixtureServer()
        self.addCleanup(self.server.close)
        self.server.route('/feed.xml', lambda req, conn: conn.send(response(200, rss([item('Meteor showers tonight', 'Meteor showers peak tonight.', 2)]))))
        self.url = self.server.base + '/feed.xml'
        self.harnesses = []
        self.addCleanup(self.close_all)
        self.h = self.start()

    def start(self, **kw):
        harness = Harness(self.temp.name, **kw)
        self.harnesses.append(harness)
        return harness

    def close_all(self):
        for harness in reversed(self.harnesses):
            harness.close()
        self.assertTrue(wait_until(lambda: not research_threads(), 3), research_threads())

    def save(self, **fields):
        return self.h.call('save', {'researcher': researcher(self.url, **fields)})


class OperationTests(HostCase):
    def test_the_catalog_lists_exactly_the_six_operations_with_typed_arguments(self):
        catalog = {entry['name']: entry for entry in self.h.host.catalog()}
        self.assertEqual(sorted(catalog), ['research.pause', 'research.remove', 'research.resume', 'research.review', 'research.run_now', 'research.save'])
        self.assertEqual(catalog['research.save']['required'], ['researcher'])
        self.assertEqual(catalog['research.save']['arguments']['researcher']['type'], 'object')
        self.assertEqual(catalog['research.review']['arguments']['decision']['enum'], ['approved', 'rejected'])
        self.assertEqual(sorted(catalog['research.review']['required']), ['brief_id', 'decision'])
        for name in ('pause', 'resume', 'remove', 'run_now'):
            self.assertEqual(catalog[f'research.{name}']['required'], ['researcher_id'])
            self.assertEqual(catalog[f'research.{name}']['arguments']['researcher_id']['maxLength'], 32)
        for entry in catalog.values():
            self.assertLessEqual(len(entry['description']), 400)
        self.assertIn('{url, name?, kind?}', catalog['research.save']['arguments']['researcher']['description'])

    def test_arguments_are_validated_by_the_host_before_any_handler_runs(self):
        rid = self.save()['researcher']['id']
        bad_calls = [
            ('save', {}), ('save', {'researcher': 'not an object'}), ('save', {'researcher': []}), ('save', {'researcher': {}, 'extra': 1}),
            ('save', {'researcher': {'name': 'x' * 70000}}),
            ('remove', {}), ('remove', {'researcher_id': 5}), ('remove', {'researcher_id': 'x' * 33}), ('remove', {'researcher_id': ''}),
            ('pause', {'researcher_id': rid, 'extra': True}), ('resume', {'id': rid}),
            ('run_now', {'researcher_id': None}), ('review', {'brief_id': 'a' * 32}), ('review', {'decision': 'approved'}),
            ('review', {'brief_id': 'a' * 32, 'decision': 'approve'}), ('review', {'brief_id': 'a' * 32, 'decision': 'approved', 'also': 1}),
        ]
        for name, args in bad_calls:
            with self.subTest(name=name, args=str(args)[:50]), self.assertRaises(ExtensionError):
                self.h.call(name, args)
        self.assertEqual(len(self.h.service.researchers()), 1)

    def test_operation_results_are_small_plain_structures(self):
        saved = self.save()
        self.assertEqual(set(saved), {'ok', 'operation', 'researcher', 'created', 'notes'})
        rid = saved['researcher']['id']
        self.assertTrue(saved['created'])
        self.assertEqual(self.h.call('pause', {'researcher_id': rid})['paused'], True)
        resumed = self.h.call('resume', {'researcher_id': rid})
        self.assertEqual(set(resumed), {'ok', 'operation', 'researcher_id', 'paused', 'state', 'next_run_at'})
        queued = self.h.call('run_now', {'researcher_id': rid})
        self.assertEqual(set(queued), {'ok', 'operation', 'researcher_id', 'state', 'message'})
        self.h.service.tick()
        brief_id = self.h.service.briefs(researcher=rid)[0]['id']
        reviewed = self.h.call('review', {'brief_id': brief_id, 'decision': 'approved'})
        self.assertEqual({k: reviewed[k] for k in ('brief_id', 'researcher_id', 'review', 'changed')}, {'brief_id': brief_id, 'researcher_id': rid, 'review': 'approved', 'changed': True})
        self.assertEqual(self.h.call('remove', {'researcher_id': rid})['removed'], rid)

    def test_an_id_that_is_not_an_id_is_refused_in_plain_words(self):
        for op in ('remove', 'pause', 'resume', 'run_now'):
            with self.subTest(op=op), self.assertRaisesRegex(ExtensionError, 'does not look right'):
                self.h.call(op, {'researcher_id': 'not-a-real-id'})
        with self.assertRaisesRegex(ExtensionError, 'does not look right'):
            self.h.call('review', {'brief_id': '../../etc/passwd', 'decision': 'approved'})

    def test_no_operation_can_carry_a_command_an_endpoint_or_a_credential(self):
        """Operations accept ids, a decision and a researcher description only. There is nowhere to put a command."""
        for entry in self.h.host.catalog():
            for key, spec in entry['arguments'].items():
                self.assertNotIn(key, ('command', 'cmd', 'url', 'endpoint', 'path', 'token', 'password', 'script', 'argv', 'shell', 'proxy'))
        for sneaky in ({'command': 'rm -rf /'}, {'endpoint': 'http://10.0.0.1/'}, {'loopback_fixture': True}, {'allow_http': True}, {'proxy': 'http://x'},
                       {'headers': {'Cookie': 'a=b'}}, {'user_agent': 'x'}, {'timeout': 9999}, {'max_bytes': 10 ** 9}):
            with self.subTest(extra=list(sneaky)[0]):
                with self.assertRaisesRegex(ExtensionError, 'is not recognised. Check the spelling, or leave it out'):
                    self.h.call('save', {'researcher': researcher(self.url, **sneaky)})
                with self.assertRaises(ExtensionError):
                    self.h.call('run_now', {'researcher_id': '0' * 32, **sneaky})
        self.assertEqual(self.h.service.researchers(), [])

    def test_a_source_may_not_carry_hidden_options_either(self):
        for source in ({'url': self.url, 'command': 'x'}, {'url': self.url, 'headers': {}}, {'url': self.url, 'allow_http': True}, {'url': self.url, 'proxy': 'x'}):
            with self.subTest(source=list(source)[1]), self.assertRaisesRegex(ExtensionError, 'Each source needs an address'):
                self.h.call('save', {'researcher': researcher(self.url, sources=[source])})


class ValidationTests(HostCase):
    """Validation runs with the production fetcher (no fixture mode), so every address rule applies as it would for a user."""

    def setUp(self):
        super().setUp()
        self.h.close()
        self.harnesses.clear()
        self.h = self.start(fetcher=Fetcher(host_min_interval=0, budget=RequestBudget(10)))
        self.url = 'https://example.org/feed.xml'

    def refused(self, fragment, **fields):
        with self.assertRaises(ExtensionError) as caught:
            self.save(**fields)
        message = str(caught.exception)
        self.assertIn(fragment, message)
        for jargon in ('Traceback', 'Exception', 'KeyError', 'TypeError', 'ValueError', 'None', 'json', 'dict', 'schema'):
            self.assertNotIn(jargon, message)
        self.assertEqual(self.h.service.researchers(), [])

    def test_names_interests_and_sources_are_required_in_plain_words(self):
        self.refused('The name is needed.', name='   ')
        self.refused('at most 80 characters', name='x' * 81)
        self.refused('Add at least one item to Interests.', interests=[])
        self.refused('Add at least one item to Interests.', interests='  ,  ;  ')
        self.refused('at most 20 items', interests=[f'topic {n}' for n in range(21)])
        self.refused('at most 80 characters', interests=['x' * 81])
        self.refused('must be text', interests=[5])
        self.refused('must be a list', interests={'a': 1})
        self.refused('Add at least one website or feed address', sources=[])
        self.refused('Add at least one website or feed address', sources=None)
        self.refused('at most 20 sources', sources=[f'https://s{n}.example/feed' for n in range(21)])
        self.refused('Each source needs a web address', sources=[{'name': 'No address'}])

    def test_addresses_must_be_secure_public_and_well_formed(self):
        self.refused('Only secure web addresses', sources=['http://example.org/feed'])
        self.refused('does not look like a web address', sources=['javascript:alert(1)'])
        self.refused('standard secure web port', sources=['https://example.org:8443/feed'])
        self.refused('private network', sources=['https://localhost/feed'])
        self.refused('private network', sources=['https://printer.local/feed'])
        self.refused('private network', sources=['https://nas/feed'])
        self.refused('private or internal address', sources=['https://192.168.1.10/feed'])
        self.refused('private or internal address', sources=['https://[::1]/feed'])
        self.refused('private or internal address', sources=['https://169.254.169.254/latest/meta-data'])
        self.refused('does not look like a web address', sources=['https://user:secret@example.org/feed'])
        self.refused('does not look like a web address', sources=['https://127.1/feed'])

    def test_source_names_and_kinds(self):
        self.refused('kind must be reported, commentary or announcement', sources=[{'url': self.url, 'kind': 'gospel'}])
        self.refused('at most 80 characters', sources=[{'url': self.url, 'name': 'n' * 81}])
        saved = self.save(sources=[{'url': self.url, 'name': ' My   club ', 'kind': 'reported'}, {'url': self.url + '?utm_source=x#frag'}])
        self.assertEqual(saved['researcher']['sources'], [{'url': self.url, 'name': 'My club', 'kind': 'reported'}])      # the duplicate collapsed
        self.assertEqual(self.save(sources=[{'url': self.url, 'kind': ''}])['researcher']['sources'][0]['kind'], None)

    def test_schedules(self):
        self.refused('between 1 and 168 hours', cadence={'type': 'every_hours', 'hours': 0})
        self.refused('between 1 and 168 hours', cadence={'type': 'every_hours', 'hours': 169})
        self.refused('whole number of hours', cadence={'type': 'every_hours', 'hours': 'six'})
        self.refused('whole number of hours', cadence={'type': 'every_hours', 'hours': True})
        self.refused('whole number of hours', cadence={'type': 'every_hours', 'hours': 1.5})
        self.refused('only needs the number of hours', cadence={'type': 'every_hours', 'hours': 6, 'time': '08:00'})
        self.refused('24-hour local time', cadence=daily_cadence(time='8:00'))
        self.refused('24-hour local time', cadence=daily_cadence(time='25:00'))
        self.refused('24-hour local time', cadence=daily_cadence(time=None))
        self.refused('Unknown timezone', cadence=daily_cadence(timezone='Mars/Olympus_Mons'))
        self.refused('IANA timezone', cadence=daily_cadence(timezone=None))
        self.refused('ISO days', cadence=daily_cadence(weekdays='0'))
        self.refused('ISO days', cadence=daily_cadence(weekdays='8,1'))
        self.refused('ISO days', cadence=daily_cadence(weekdays='mon'))
        self.refused('time, a time zone', cadence={**daily_cadence(), 'extra': 1})
        self.refused('either', cadence={'type': 'weekly'})
        self.refused('how often', cadence='daily')
        saved = self.save(cadence=daily_cadence(weekdays=[5, 1, 3]))['researcher']
        self.assertEqual(saved['cadence'], daily_cadence(weekdays='1,3,5'))
        self.assertEqual(self.save(cadence=None)['researcher']['cadence'], {'type': 'every_hours', 'hours': 24})

    def test_numbers_and_choices(self):
        self.refused('between 1 and 720 hours', freshness_hours=0)
        self.refused('between 1 and 720 hours', freshness_hours=721)
        self.refused('whole number of hours', freshness_hours='24')
        self.refused('between 15 and 600 seconds', segment_seconds=14)
        self.refused('between 15 and 600 seconds', segment_seconds=601)
        self.refused('short name', anchor='Marlowe!')
        self.refused('short name', anchor='-x')
        self.refused('short name', anchor='a' * 64)
        self.refused("'before_air'", review='sometimes')
        self.refused('on or off', tone_filter='yes')
        self.refused('Paused is either', paused='no')
        self.refused('The setting \u201cmood\u201d is not recognised. Check the spelling, or leave it out.', mood='cheerful')
        self.assertEqual(self.save(anchor='marlowe', review='auto', freshness_hours=720, segment_seconds=600)['researcher']['anchor'], 'marlowe')

    def test_an_address_with_a_password_or_key_in_it_is_flagged_without_repeating_it(self):
        """A private feed address often carries its key in the address. It is kept as typed (it has to be, to be fetched), so the person
        is told where it ends up: on screen, and in backups. The warning never repeats the value."""
        result = self.save(sources=['https://example.org/private.atom?token=SECRET-TOKEN-123&user=me', 'https://example.net/feed.xml?page=2'])
        self.assertEqual(len(result['notes']), 1)
        self.assertIn('example.org', result['notes'][0])
        self.assertIn('looks like a password or key', result['notes'][0])
        self.assertIn('backups', result['notes'][0])
        self.assertNotIn('SECRET-TOKEN-123', json.dumps(result['notes']))
        self.assertEqual(result['researcher']['sources'][0]['url'], 'https://example.org/private.atom?token=SECRET-TOKEN-123&user=me')     # as typed
        for harmless in ('https://example.org/feed.xml?page=2&keyword=planets', 'https://example.org/feed.xml?key=', 'https://example.org/feed.xml'):
            with self.subTest(address=harmless):
                self.assertEqual(self.save(sources=[harmless])['notes'], [])
        for secret in ('api_key=abc', 'accessToken=abc', 'X-Amz-Signature=abc', 'password=abc', 'auth=abc', 'sig=abc'):
            with self.subTest(address=secret):
                self.assertEqual(len(self.save(sources=['https://example.org/feed.xml?' + secret])['notes']), 1)

    def test_a_source_and_an_exclusion_that_contradict_each_other_are_refused(self):
        self.refused('also asked to skip', exclude_sources=['example.org'])
        self.refused('also asked to skip', exclude_sources=['example.org/feed.xml'])
        self.refused('also asked to skip', exclude_sources=['WWW.Example.ORG'])
        self.assertTrue(self.save(exclude_sources=['other.example', 'Some Name'])['researcher'])

    def test_lists_may_be_given_as_text_and_are_tidied(self):
        saved = self.save(interests='meteor showers, Meteor Showers;  planets\nbackyard   astronomy', exclude_topics='politics, politics',
                          questions='What is on tonight? Where?; Anything new?', tone_words='gotcha, GOTCHA', tone_allow='crisis')['researcher']
        self.assertEqual(saved['interests'], ['meteor showers', 'planets', 'backyard astronomy'])
        self.assertEqual(saved['exclude_topics'], ['politics'])
        self.assertEqual(saved['questions'], ['What is on tonight? Where?', 'Anything new?'])
        self.assertEqual((saved['tone_words'], saved['tone_allow']), (['gotcha'], ['crisis']))

    def test_text_is_stripped_of_invisible_characters_but_not_of_meaning(self):
        saved = self.save(name='Stargazing\u202e\u200b club', purpose='  For\tthe   club \x00 members ', interests=['\u200bplanets\U000e0041'])['researcher']
        self.assertEqual((saved['name'], saved['purpose'], saved['interests']), ('Stargazing club', 'For the club members', ['planets']))

    def test_a_screen_can_send_back_what_it_received(self):
        first = self.save()['researcher']
        echoed = dict(first)
        echoed['name'] = 'Renamed from the screen'
        again = self.h.call('save', {'researcher': echoed})
        self.assertFalse(again['created'])
        self.assertEqual((again['researcher']['id'], again['researcher']['name']), (first['id'], 'Renamed from the screen'))
        self.assertEqual(len(self.h.service.researchers()), 1)

    def test_editing_an_unknown_researcher_is_refused(self):
        with self.assertRaisesRegex(ExtensionError, 'no longer exists'):
            self.save(id='0' * 32)
        with self.assertRaisesRegex(ExtensionError, 'does not exist'):
            self.save(id='nonsense')

    def test_helpful_notes_come_back_with_a_save(self):
        saved = self.save(cadence={'type': 'every_hours', 'hours': 12}, freshness_hours=2)
        self.assertEqual(len(saved['notes']), 1)
        self.assertIn('some stories may be missed', saved['notes'][0])
        self.assertEqual(self.save(name='Fine')['notes'], [])

    def test_the_first_save_keeps_every_field_the_caller_chose(self):
        fields = dict(name='Full', purpose='Everything set', interests=['a b', 'c'], questions=['Why?'], exclude_topics=['x'], exclude_sources=['y.example'],
                      cadence=daily_cadence(time='07:30', weekdays='1,2,3,4,5'), freshness_hours=48, segment_seconds=90, anchor='dex', review='auto',
                      tone_filter=False, tone_words=['gotcha'], tone_allow=['crisis'], paused=True)
        saved = self.save(**fields)['researcher']
        for key, value in fields.items():
            self.assertEqual(saved[key], value, key)
        self.assertEqual(saved['cadence_text'], 'Weekdays at 07:30 (America/New_York)')
        self.assertEqual(saved['state'], 'paused')


def daily_cadence(time='08:00', timezone='America/New_York', weekdays='1,2,3,4,5,6,7'):
    return {'type': 'daily', 'time': time, 'timezone': timezone, 'weekdays': weekdays}


class SettingsTests(unittest.TestCase):
    def test_numbers_are_clamped_and_unknown_keys_are_noted(self):
        settings, notes = read_settings({'daily_request_cap': 10 ** 9, 'host_min_interval_seconds': 0, 'request_timeout_seconds': 'fast', 'contact': ' me@x.org ',
                                         'loopback_fixture': True})
        self.assertEqual((settings['daily_request_cap'], settings['host_min_interval_seconds'], settings['request_timeout_seconds'], settings['contact']),
                         (5000, 1.0, 20.0, 'me@x.org'))
        self.assertEqual(len(notes), 4)
        self.assertEqual(read_settings({}), ({'daily_request_cap': 500, 'host_min_interval_seconds': 3.0, 'request_timeout_seconds': 20.0, 'contact': ''}, []))

    def test_the_pause_between_requests_to_one_website_cannot_be_longer_than_the_station_will_wait(self):
        """A longer setting would mean that a second source on the same website could never be read: the fetcher refuses waits over 30 seconds."""
        self.assertLessEqual(RANGES['host_min_interval_seconds'][1], MAX_BACKOFF_WAIT)
        settings, notes = read_settings({'host_min_interval_seconds': 120})
        self.assertEqual(settings['host_min_interval_seconds'], MAX_BACKOFF_WAIT)
        self.assertEqual(len(notes), 1)
        self.assertIn('30', notes[0])
        self.assertEqual(read_settings({'host_min_interval_seconds': 30})[1], [])

    def test_the_stations_own_extension_keeps_the_daily_request_count_across_a_restart(self):
        """Built exactly as the station builds it, nothing injected: the allowance must live in the database, not in memory."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        clock = FakeClock()

        def start():
            host = ExtensionHost(temp.name, StationFacade(FakeStation()), clock=clock)
            extension = build()
            host.register(extension)
            host.start()
            self.addCleanup(host.stop)
            return host, extension.service.fetcher.budget

        host, budget = start()
        self.assertIsInstance(budget, PersistentBudget)
        for _ in range(7):
            budget.take()
        self.assertEqual(budget.used(), 7)
        host.stop()
        host, budget = start()
        self.assertIsInstance(budget, PersistentBudget)
        self.assertEqual(budget.used(), 7)                                                  # the restart did not hand out a fresh allowance
        self.assertEqual(host.status()['research']['status']['totals']['requests_today'], 7)
        budget.take()
        self.assertEqual(budget.used(), 8)
        clock.advance(86400)
        self.assertEqual(budget.used(), 0)                                                  # and a new day still starts again

    def test_config_cannot_switch_off_a_safety_rule(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = FixtureServer()
        self.addCleanup(server.close)
        server.serve('/feed.xml', rss([]))
        state = Path(temp.name) / 'ext' / 'research'
        state.mkdir(parents=True)
        (state / 'config.json').write_text(json.dumps({'loopback_fixture': True, 'allow_http': True, 'allow_private': True, 'daily_request_cap': 99999999,
                                                       'host_min_interval_seconds': 0, 'request_timeout_seconds': 99999, 'contact': 'x\r\nInjected: 1',
                                                       'proxy': 'http://evil.example', 'max_bytes': 10 ** 9}))
        host = ExtensionHost(temp.name, StationFacade(FakeStation()))
        extension = build()                                              # exactly what the station builds
        host.register(extension)
        host.start()
        self.addCleanup(host.stop)
        fetcher = extension.service.fetcher
        self.assertFalse(fetcher.fixture)
        self.assertEqual((fetcher.budget.limit, fetcher.host_min_interval, fetcher.deadline_seconds), (5000, 1.0, 60.0))
        self.assertNotIn('Injected', fetcher.user_agent)
        with self.assertRaises(FetchError) as caught:
            fetcher.fetch(server.base + '/feed.xml')
        self.assertEqual(caught.exception.kind, 'not_https')
        self.assertEqual(server.hits(), [])
        with self.assertRaisesRegex(ExtensionError, 'Only secure web addresses'):
            host.call('research.save', {'researcher': researcher(server.base + '/feed.xml')})
        notes = {c['id']: c for c in host.checks()}['research.settings']
        self.assertEqual(notes['status'], 'warn')
        for key in ('loopback_fixture', 'allow_http', 'proxy'):
            self.assertIn(key, notes['detail'])

    def test_a_broken_config_file_does_not_stop_the_extension(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / 'ext' / 'research'
        state.mkdir(parents=True)
        (state / 'config.json').write_text('{not json')
        host = ExtensionHost(temp.name, StationFacade(FakeStation()))
        host.register(build())
        host.start()
        self.addCleanup(host.stop)
        self.assertEqual(host.status()['research']['state'], 'running')
        self.assertIn('config.json was not used', {c['id']: c for c in host.checks()}['research.settings']['detail'])

    def test_build_returns_a_fresh_extension_with_nothing_injected(self):
        first, second = build(), build()
        self.assertIsNot(first, second)
        self.assertEqual((first.name, first._fetcher, first._options), ('research', None, {}))
        self.assertTrue(first._autostart)

    def test_the_station_finds_the_research_extension_among_its_built_ins(self):
        found = [e for e in builtin_extensions() if getattr(e, 'name', '') == 'research']
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], ResearchExtension)

    def test_the_doctor_is_told_whether_websites_can_be_verified(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        host = ExtensionHost(temp.name, StationFacade(FakeStation()))
        host.register(build())
        host.start()
        self.addCleanup(host.stop)
        certificates = {c['id']: c for c in host.checks()}['research.certificates']
        self.assertIn(certificates['status'], ('ok', 'warn'))
        with patch('ssl.get_default_verify_paths') as paths, patch.dict('os.environ', {}, clear=True):
            paths.return_value.cafile, paths.return_value.capath = None, None
            missing = {c['id']: c for c in host.checks()}['research.certificates']
        self.assertEqual(missing['status'], 'warn')
        self.assertIn('Install Certificates', missing['hint'])


class RouteTests(HostCase):
    def route(self, method, path, query=None, body=None):
        from myradio.extensions import Request
        match = self.h.host.find_route(method, '/api/x/research' + path)
        self.assertIsNotNone(match, path)
        return self.h.host.dispatch_route(match, Request(method, path, query or {}, body, b'' if body is None else json.dumps(body).encode()))

    def test_the_routes_exist_and_nothing_else_does(self):
        for method, path in (('GET', '/researchers'), ('GET', '/briefs'), ('GET', '/sources'), ('GET', '/tone'), ('POST', '/sources/check')):
            self.assertIsNotNone(self.h.host.find_route(method, '/api/x/research' + path), path)
        for method, path in (('POST', '/researchers'), ('GET', '/sources/check'), ('DELETE', '/researchers'), ('GET', '/secrets'), ('GET', '/../x')):
            self.assertIsNone(self.h.host.find_route(method, '/api/x/research' + path), path)

    def test_researchers_route_lists_everything_a_screen_needs(self):
        empty = self.route('GET', '/researchers')
        self.assertEqual((empty.status, empty.body['researchers']), (200, []))
        self.assertEqual(empty.body['limits']['researchers'], 50)
        rid = self.save()['researcher']['id']
        self.h.run(rid)
        body = self.route('GET', '/researchers').body
        entry = body['researchers'][0]
        for key in ('id', 'name', 'purpose', 'interests', 'questions', 'sources', 'exclude_topics', 'exclude_sources', 'cadence', 'cadence_text',
                    'freshness_hours', 'segment_seconds', 'anchor', 'review', 'tone_filter', 'tone_words', 'tone_allow', 'paused', 'state', 'next_run_at',
                    'last_run', 'pending_review', 'created_at'):
            self.assertIn(key, entry)
        self.assertEqual((entry['last_run']['outcome'], entry['pending_review'], entry['state']), ('ready', 1, 'idle'))
        json.dumps(body)

    def test_briefs_route_filters_and_validates_its_query(self):
        rid = self.save()['researcher']['id']
        self.h.run(rid)
        other = self.h.service.save(researcher(self.url, name='Other', interests=['gardening']))['researcher']['id']
        self.h.run(other)
        listed = self.route('GET', '/briefs', {'researcher': [rid]}).body['briefs']
        self.assertEqual([b['researcher_id'] for b in listed], [rid])
        self.assertEqual(len(self.route('GET', '/briefs').body['briefs']), 2)
        self.assertEqual(len(self.route('GET', '/briefs', {'outcome': ['ready']}).body['briefs']), 1)
        self.assertEqual(len(self.route('GET', '/briefs', {'review': ['pending']}).body['briefs']), 1)
        self.assertEqual(len(self.route('GET', '/briefs', {'limit': ['1']}).body['briefs']), 1)
        self.assertEqual(self.route('GET', '/briefs', {'airable': ['1']}).body['briefs'], [])
        self.h.service.review(listed[0]['id'], 'approved')
        self.assertEqual(len(self.route('GET', '/briefs', {'airable': ['1']}).body['briefs']), 1)
        self.assertEqual(self.route('GET', '/briefs', {'id': [listed[0]['id']]}).body['briefs'][0]['id'], listed[0]['id'])
        for query in ({'researcher': ['nope']}, {'review': ['maybe']}, {'outcome': ['great']}, {'limit': ['0']}, {'limit': ['51']}, {'limit': ['ten']}, {'id': ['x']}):
            with self.subTest(query=query):
                reply = self.route('GET', '/briefs', query)
                self.assertEqual(reply.status, 400)
                self.assertTrue(reply.body['error'].endswith('.'))

    def test_the_airable_filter_is_decided_before_the_limit_so_a_quiet_station_does_not_hide_a_ready_update(self):
        """With 24 newer 'nothing new' briefs in front of it, an update that is ready to air must still be found."""
        ready = self.save(name='Has an update', review='auto')['researcher']['id']
        first = self.h.run(ready)
        quiet = [self.save(name=f'Quiet {n}', interests=['gardening'])['researcher']['id'] for n in range(3)]
        for _ in range(8):
            for rid in quiet:
                self.h.clock.advance(60)
                self.h.run(rid)
        self.assertEqual(len(self.route('GET', '/briefs').body['briefs']), 20)             # the plain listing is cut at its default limit
        self.assertEqual(self.h.service.next_airable()['id'], first['id'])
        found = self.route('GET', '/briefs', {'airable': ['1']}).body['briefs']
        self.assertEqual([b['id'] for b in found], [first['id']])
        self.assertEqual([b['id'] for b in self.route('GET', '/briefs', {'airable': ['true'], 'limit': ['1']}).body['briefs']], [first['id']])
        self.assertEqual(self.route('GET', '/briefs', {'airable': ['1'], 'researcher': [quiet[0]]}).body['briefs'], [])
        later = self.save(name='Another update', review='auto', interests=['meteor'])['researcher']['id']
        second = self.h.run(later)
        self.assertEqual(second['outcome'], 'ready')
        listed = [b['id'] for b in self.route('GET', '/briefs', {'airable': ['1']}).body['briefs']]
        self.assertEqual(listed, [second['id'], first['id']])                               # newest first, like every other listing
        self.h.service.claim_airable(ready)
        self.assertEqual([b['id'] for b in self.route('GET', '/briefs', {'airable': ['1']}).body['briefs']], [second['id']])

    def test_the_brief_document_has_the_documented_shape(self):
        rid = self.save(anchor='marlowe', segment_seconds=90)['researcher']['id']
        self.h.run(rid)
        brief = self.route('GET', '/briefs', {'researcher': [rid]}).body['briefs'][0]
        self.assertEqual(set(brief), {'id', 'researcher_id', 'researcher_name', 'outcome', 'message', 'review', 'trigger', 'created_ts', 'created_at',
                                      'reviewed_ts', 'reviewed_at', 'aired_ts', 'aired_at', 'airable', 'freshness_hours', 'segment_seconds', 'target_words',
                                      'anchor', 'fresh_until_ts', 'fresh_until_at', 'items', 'sources', 'summary', 'warnings'})
        self.assertEqual((brief['anchor'], brief['segment_seconds'], brief['target_words'], brief['freshness_hours']), ('marlowe', 90, 225, 24))
        self.assertEqual(set(brief['summary']), {'considered', 'kept', 'skipped', 'explanation'})
        self.assertEqual(set(brief['sources'][0]), {'url', 'name', 'status', 'message', 'entries', 'format', 'checked_at'})

    def test_sources_route(self):
        self.assertEqual(self.route('GET', '/sources').body, {'sources': []})
        self.save()
        sources = self.route('GET', '/sources').body['sources']
        self.assertEqual((sources[0]['status'], sources[0]['url'], sources[0]['host']), ('unchecked', self.url, '127.0.0.1'))

    def test_tone_route_shows_the_whole_word_list_and_how_it_is_used(self):
        from myradio.research.selection import TONE_LEXICON
        body = self.route('GET', '/tone').body
        self.assertEqual(len(body['words']), len(TONE_LEXICON))
        self.assertEqual({w['list'] for w in body['words']}, {'outrage', 'fear', 'click-bait'})
        self.assertTrue(all(set(w) == {'phrase', 'weight', 'list'} and w['weight'] > 0 for w in body['words']))
        self.assertIn('not an understanding of the story', body['explanation'])
        self.assertNotIn('sentiment', body['explanation'])
        self.assertEqual((body['penalty_cap'], body['minimum_relevance']), (6.0, 1.5))
        json.dumps(body)

    def test_check_this_address_route(self):
        found = self.route('POST', '/sources/check', body={'url': self.url})
        self.assertEqual((found.status, found.body['ok'], found.body['kind'], found.body['posts']), (200, True, 'feed', 1))
        for body in ({}, {'url': ''}, {'url': 5}, None):
            with self.subTest(body=body):
                self.assertEqual(self.route('POST', '/sources/check', body=body).status, 400)
        refused = self.route('POST', '/sources/check', body={'url': 'https://10.0.0.5/feed'})
        self.assertEqual(refused.status, 400)
        self.assertIn('private or internal', refused.body['error'])

    def test_routes_report_a_stopped_service_plainly(self):
        self.h.service.store.close()
        self.assertEqual(self.route('GET', '/researchers').status, 400)


class StationIntegrationTests(unittest.TestCase):
    """The same extension inside a real Station, over real HTTP."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.feed = FixtureServer()
        self.addCleanup(self.feed.close)
        # The station under test uses the real clock, so the feed is dated relative to the real "now".
        self.feed.route('/feed.xml', lambda req, conn: conn.send(response(200, rss([{'title': 'Meteor showers tonight', 'link': 'https://sky.example/m',
                                                                                   'ts': time.time() - 1800, 'desc': 'Meteor showers peak tonight.'}]))))
        self.url = self.feed.base + '/feed.xml'
        self.ext = ResearchExtension(fetcher=loopback_fetcher(), autostart=True, tick_seconds=0.05, run_now_wait=5.0)
        self.station = Station(Path(self.temp.name) / 'data', start_worker=False, extensions=[self.ext])
        with patch.dict('os.environ', {}, clear=True):
            self.web = Server(('127.0.0.1', 0), self.station, TOKEN)
        threading.Thread(target=self.web.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(lambda: wait_until(lambda: not research_threads(), 3))
        self.addCleanup(self.station.close)
        self.addCleanup(self.web.server_close)
        self.addCleanup(self.web.shutdown)
        self.port = self.web.server_address[1]

    def call(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        sent = dict(headers or {})
        if token:
            sent['X-Myradio-Token'] = TOKEN
        if body is not None:
            sent['Content-Type'] = 'application/json'
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=sent)
        reply = conn.getresponse()
        data = reply.read()
        conn.close()
        try:
            return reply.status, json.loads(data)
        except ValueError:
            return reply.status, data

    def test_an_agent_or_the_screen_can_create_run_and_review_a_researcher(self):
        status, saved = self.call('POST', '/api/action', {'operation': 'research.save', 'arguments': {'researcher': researcher(self.url)}})
        self.assertEqual(status, 200)
        rid = saved['researcher']['id']
        status, ran = self.call('POST', '/api/action', {'operation': 'research.run_now', 'arguments': {'researcher_id': rid}})
        self.assertEqual((status, ran['state'], ran['outcome']), (200, 'done', 'ready'))
        status, briefs = self.call('GET', f'/api/x/research/briefs?researcher={rid}')
        self.assertEqual((status, len(briefs['briefs']), briefs['briefs'][0]['review']), (200, 1, 'pending'))
        status, reviewed = self.call('POST', '/api/action', {'operation': 'research.review', 'arguments': {'brief_id': briefs['briefs'][0]['id'], 'decision': 'approved'}})
        self.assertEqual((status, reviewed['review']), (200, 'approved'))
        self.assertEqual(self.call('GET', f'/api/x/research/briefs?researcher={rid}&airable=1')[1]['briefs'][0]['airable'], True)
        status, researchers = self.call('GET', '/api/x/research/researchers')
        self.assertEqual((status, researchers['researchers'][0]['last_run']['outcome']), (200, 'ready'))
        status, everything = self.call('GET', '/api/status')
        self.assertEqual(everything['extensions']['research']['state'], 'running')
        self.assertEqual(everything['extensions']['research']['status']['totals']['researchers'], 1)
        json.dumps(everything)

    def test_bad_requests_get_plain_messages_and_do_not_leak_internals(self):
        status, body = self.call('POST', '/api/action', {'operation': 'research.save', 'arguments': {'researcher': {'name': 'No interests', 'sources': [self.url]}}})
        self.assertEqual(status, 400)
        self.assertIn('Add at least one item to Interests', json.dumps(body))
        status, body = self.call('POST', '/api/action', {'operation': 'research.save', 'arguments': {'researcher': {'loopback_fixture': True}}})
        self.assertEqual(status, 400)
        self.assertIn('is not recognised', json.dumps(body))
        status, body = self.call('POST', '/api/action', {'operation': 'research.remove', 'arguments': {'researcher_id': '0' * 32}})
        self.assertEqual(status, 400)
        self.assertIn('does not exist', json.dumps(body))

    def test_every_route_needs_the_private_token_and_a_local_origin(self):
        for method, path in (('GET', '/api/x/research/researchers'), ('GET', '/api/x/research/briefs'), ('GET', '/api/x/research/sources')):
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, token=False)[0], 401)
                self.assertEqual(self.call(method, path, headers={'Origin': 'https://evil.example'})[0], 403)
                self.assertEqual(self.call(method, path, headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.call('POST', '/api/x/research/sources/check', {'url': self.url}, token=False)[0], 401)
        self.assertEqual(self.call('POST', '/api/action', {'operation': 'research.save', 'arguments': {}}, token=False)[0], 401)
        self.assertEqual(self.call('POST', '/api/x/research/sources/check', {'url': self.url}, headers={'Origin': 'https://evil.example'})[0], 403)

    def test_a_station_that_stopped_after_a_storage_fault_refuses_changes_but_still_answers_reads(self):
        with self.station.lock:
            self.station._fail_safe()
        with self.assertRaises(StationError):
            self.station.action('research.save', {'researcher': researcher(self.url)})
        self.assertEqual(self.call('GET', '/api/x/research/researchers')[0], 200)

    def test_the_operations_show_up_in_the_station_catalog_for_chat_and_agents(self):
        names = [o['name'] for o in self.station.operations()]
        for name in ('research.save', 'research.remove', 'research.pause', 'research.resume', 'research.run_now', 'research.review'):
            self.assertIn(name, names)
        self.assertEqual(self.station.status()['extensions']['research']['state'], 'running')

    def test_closing_the_station_stops_the_scheduler_thread_and_leaves_one_file(self):
        self.assertEqual([t.name for t in research_threads()], ['myradio-research'])
        self.station.close()
        self.assertTrue(wait_until(lambda: not research_threads(), 3), research_threads())
        files = sorted(p.name for p in (Path(self.temp.name) / 'data' / 'ext' / 'research').iterdir())
        self.assertEqual(files, ['research.sqlite3'])
        self.station.close()


if __name__ == '__main__':
    unittest.main()
