"""Cadence, daylight saving, the scheduler thread, run_now, pause and resume, shutdown, status and doctor checks."""
import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from myradio.extensions import ExtensionError
from myradio.research.model import jitter_seconds, nominal_next, plausible_next
from myradio.research.service import RETRY_LIMIT, MAX_JOBS_PER_TICK
from test_research_support import (NOW, FixtureServer, Harness, item, research_threads, researcher, response, rss, wait_until)

HOUR = 3600.0
NY = ZoneInfo('America/New_York')


def local(year, month, day, hour=0, minute=0, zone=NY, fold=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=zone, fold=fold).timestamp()


def daily(time_text, zone='America/New_York', weekdays='1,2,3,4,5,6,7'):
    return {'type': 'daily', 'time': time_text, 'timezone': zone, 'weekdays': weekdays}


class CadenceTests(unittest.TestCase):
    def test_every_n_hours_counts_from_the_last_start_and_runs_at_once_when_overdue_or_new(self):
        cadence = {'type': 'every_hours', 'hours': 6}
        self.assertEqual(nominal_next(cadence, now=1000.0), 1000.0)                                    # never ran
        self.assertEqual(nominal_next(cadence, now=1000.0, last_start=1000.0 - 2 * HOUR), 1000.0 + 4 * HOUR)
        self.assertEqual(nominal_next(cadence, now=1000.0 + 20 * HOUR, last_start=1000.0), 1000.0 + 20 * HOUR)    # overdue: now

    def test_daily_runs_at_the_local_wall_clock_time_in_the_chosen_zone(self):
        cadence = daily('08:00')
        self.assertEqual(nominal_next(cadence, now=local(2025, 11, 5, 7, 0)), local(2025, 11, 5, 8, 0))
        self.assertEqual(nominal_next(cadence, now=local(2025, 11, 5, 8, 0)), local(2025, 11, 6, 8, 0))       # strictly after now
        kolkata = ZoneInfo('Asia/Kolkata')
        self.assertEqual(nominal_next(daily('08:00', 'Asia/Kolkata'), now=local(2025, 11, 5, 7, 0, kolkata)), local(2025, 11, 5, 8, 0, kolkata))
        self.assertEqual(nominal_next(daily('08:00', 'UTC'), now=NOW), NOW + 8 * HOUR)

    def test_a_time_that_does_not_exist_on_the_spring_forward_day_is_skipped(self):
        cadence = daily('02:30')
        before = local(2025, 3, 8, 12, 0)
        self.assertEqual(nominal_next(cadence, now=before), local(2025, 3, 10, 2, 30))              # 2025-03-09 02:30 never happened in New York
        self.assertEqual(dt.datetime.fromtimestamp(local(2025, 3, 10, 2, 30), dt.timezone.utc).hour, 6)         # EDT: 06:30 UTC

    def test_a_repeated_time_on_the_fall_back_day_runs_once_at_the_first_occurrence(self):
        cadence = daily('01:30')
        first = nominal_next(cadence, now=local(2025, 11, 1, 12, 0))
        self.assertEqual(first, local(2025, 11, 2, 1, 30, fold=0))
        self.assertEqual(dt.datetime.fromtimestamp(first, dt.timezone.utc).hour, 5)                 # the EDT occurrence, 05:30 UTC
        second = nominal_next(cadence, now=first)
        self.assertEqual(second, local(2025, 11, 3, 1, 30))                                         # not the repeated 01:30 EST an hour later
        self.assertGreater(second - first, 24 * HOUR)

    def test_a_daily_time_keeps_its_wall_clock_hour_across_the_change(self):
        cadence = daily('09:00')
        run = local(2025, 3, 8, 9, 0)
        following = nominal_next(cadence, now=run)
        self.assertEqual(following, local(2025, 3, 9, 9, 0))
        self.assertEqual(following - run, 23 * HOUR)                                                 # a 23-hour day, still 09:00 local

    def test_weekday_selection(self):
        fridays = daily('17:00', weekdays='5')
        saturday = local(2025, 11, 8, 12, 0)
        self.assertEqual(nominal_next(fridays, now=saturday), local(2025, 11, 14, 17, 0))
        weekdays = daily('12:00', weekdays='1,2,3,4,5')
        self.assertEqual(nominal_next(weekdays, now=local(2025, 11, 7, 13, 0)), local(2025, 11, 10, 12, 0))     # Friday afternoon to Monday

    def test_jitter_is_small_stable_and_per_researcher(self):
        values = {jitter_seconds(f'researcher-{n}:100', 60.0) for n in range(50)}
        self.assertTrue(all(0.0 <= v < 60.0 for v in values))
        self.assertGreater(len(values), 40)
        self.assertEqual(jitter_seconds('x:1', 60.0), jitter_seconds('x:1', 60.0))
        self.assertEqual(jitter_seconds('x:1', 0.0), 0.0)

    def test_plausible_next_catches_a_clock_that_was_set_back(self):
        every = {'type': 'every_hours', 'hours': 6}
        self.assertTrue(plausible_next(every, NOW + 5 * HOUR, NOW))
        self.assertFalse(plausible_next(every, NOW + 10 * 86400, NOW))
        self.assertFalse(plausible_next(every, None, NOW))
        self.assertTrue(plausible_next(daily('08:00'), NOW + 7 * 86400, NOW))
        self.assertFalse(plausible_next(daily('08:00'), NOW + 30 * 86400, NOW))


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = FixtureServer()
        self.addCleanup(self.server.close)
        self.harnesses = []
        self.addCleanup(self.close_all)
        self.feed_items = [item('Meteor showers peak tonight', 'Meteor showers will light up the sky tonight.', 2)]
        self.server.route('/feed.xml', lambda req, conn: conn.send(response(200, rss(self.feed_items))))
        self.url = self.server.base + '/feed.xml'
        self.h = self.start()

    def start(self, **kw):
        harness = Harness(self.temp.name, **kw)
        self.harnesses.append(harness)
        return harness

    def close_all(self):
        for harness in reversed(self.harnesses):
            harness.close()
        self.assertTrue(wait_until(lambda: not research_threads(), 3), research_threads())

    def make(self, **kw):
        return self.h.save(**researcher(kw.pop('url', self.url), **kw))

    def row(self, rid):
        return self.h.service.store.get(rid)


class ScheduleTests(SchedulerCase):
    def test_a_new_every_n_hours_researcher_runs_within_a_minute_and_then_waits_n_hours(self):
        rid = self.make()['id']
        due = self.row(rid)['next_run_ts']
        self.assertTrue(NOW <= due < NOW + 60)
        self.assertEqual(self.h.service.tick(), 0)                                  # not due yet (jitter)
        self.h.clock.advance(61)
        self.assertEqual(self.h.service.tick(), 1)
        after = self.row(rid)
        self.assertEqual(after['last_outcome'], 'ready')
        started = NOW + 61
        self.assertTrue(started + 6 * HOUR <= after['next_run_ts'] < started + 6 * HOUR + 60)
        self.assertEqual(self.h.service.tick(), 0)

    def test_a_manual_run_does_not_move_the_schedule(self):
        rid = self.make()['id']
        before = self.row(rid)['next_run_ts']
        self.h.run(rid)
        self.assertEqual(self.row(rid)['next_run_ts'], before)
        self.assertEqual(self.h.service.briefs(researcher=rid)[0]['trigger'], 'manual')

    def test_a_daily_researcher_is_scheduled_for_its_next_wall_clock_time(self):
        self.h.clock.now = local(2025, 3, 8, 12, 0)
        rid = self.make(cadence=daily('02:30'))['id']
        due = self.row(rid)['next_run_ts']
        self.assertTrue(local(2025, 3, 10, 2, 30) <= due < local(2025, 3, 10, 2, 30) + 60)         # the 9th has no 02:30
        self.assertEqual(self.h.service.researcher(rid)['cadence_text'], 'Every day at 02:30 (America/New_York)')

    def test_a_daily_researcher_runs_when_its_time_comes_and_then_waits_for_the_next_day(self):
        self.h.clock.now = local(2025, 11, 5, 7, 0)
        rid = self.make(cadence=daily('08:00', weekdays='1,2,3,4,5'))['id']
        self.assertEqual(self.h.service.tick(), 0)
        self.h.clock.now = local(2025, 11, 5, 8, 2)
        self.assertEqual(self.h.service.tick(), 1)
        nxt = self.row(rid)['next_run_ts']
        self.assertTrue(local(2025, 11, 6, 8, 0) <= nxt < local(2025, 11, 6, 8, 0) + 60)

    def test_editing_the_cadence_reschedules_and_other_edits_do_not(self):
        rid = self.make()['id']
        due = self.row(rid)['next_run_ts']
        self.h.clock.advance(5)
        self.h.save(**researcher(self.url, id=rid, interests=['planets']))
        self.assertEqual(self.row(rid)['next_run_ts'], due)
        self.h.save(**researcher(self.url, id=rid, cadence=daily('09:00', 'UTC')))
        self.assertGreater(self.row(rid)['next_run_ts'], due)
        self.assertEqual(self.h.service.researcher(rid)['cadence']['type'], 'daily')

    def test_paused_researchers_never_run_on_their_own_and_resume_schedules_them_again(self):
        rid = self.make()['id']
        paused = self.h.call('pause', {'researcher_id': rid})
        self.assertEqual((paused['paused'], paused['state']), (True, 'paused'))
        self.assertIsNone(self.row(rid)['next_run_ts'])
        self.h.clock.advance(100 * HOUR)
        self.assertEqual(self.h.service.tick(), 0)
        self.assertEqual(self.h.service.briefs(researcher=rid), [])
        resumed = self.h.call('resume', {'researcher_id': rid})
        self.assertEqual((resumed['paused'], resumed['state']), (False, 'idle'))
        self.assertTrue(self.h.clock() <= self.row(rid)['next_run_ts'] < self.h.clock() + 60)
        self.h.clock.advance(61)
        self.assertEqual(self.h.service.tick(), 1)

    def test_a_paused_researcher_can_still_be_run_by_hand_and_stays_paused(self):
        rid = self.make()['id']
        self.h.call('pause', {'researcher_id': rid})
        brief = self.h.run(rid)
        self.assertEqual(brief['outcome'], 'ready')
        self.assertTrue(self.row(rid)['paused'])
        self.assertIsNone(self.row(rid)['next_run_ts'])

    def test_saving_a_paused_researcher_keeps_it_paused_unless_told_otherwise(self):
        rid = self.make()['id']
        self.h.call('pause', {'researcher_id': rid})
        self.h.save(**researcher(self.url, id=rid, name='Renamed'))
        self.assertTrue(self.row(rid)['paused'])
        self.assertTrue(self.make(name='Draft', paused=True)['paused'])
        self.assertFalse(self.h.save(**researcher(self.url, id=rid, paused=False))['paused'])

    def test_what_is_due_runs_in_due_order_a_few_at_a_time(self):
        ids = [self.make(name=f'R{n}')['id'] for n in range(12)]
        self.h.clock.advance(120)
        self.assertEqual(self.h.service.tick(), MAX_JOBS_PER_TICK)
        self.assertEqual(self.h.service.tick(), 2)
        self.assertEqual(self.h.service.tick(), 0)
        ran = sorted(ids, key=lambda rid: self.row(rid)['last_run_ts'] or 0)
        self.assertEqual(len(ran), 12)

    def test_overdue_researchers_catch_up_once_after_downtime(self):
        rid = self.make()['id']
        self.h.close()
        self.harnesses.clear()
        self.h = self.start(clock=self.h.clock)
        self.h.clock.advance(5 * 86400)                       # the station was off for five days
        self.assertEqual(self.h.service.tick(), 1)
        self.assertEqual(self.h.service.tick(), 0)            # one catch-up run, not five
        self.assertEqual(len(self.h.service.briefs(researcher=rid)), 1)
        self.assertGreater(self.row(rid)['next_run_ts'], self.h.clock())

    def test_an_impossible_due_time_from_a_clock_that_was_set_back_is_repaired_on_start(self):
        rid = self.make()['id']
        self.h.service.store.set_next_run(rid, NOW + 400 * 86400)
        self.h.close()
        self.harnesses.clear()
        self.h = self.start(clock=self.h.clock)
        self.assertLess(self.row(rid)['next_run_ts'], NOW + 7 * HOUR)

    def test_failures_are_retried_soon_twice_and_then_back_on_the_normal_schedule(self):
        self.server.route('/feed.xml', lambda req, conn: conn.send(response(500, b'oops', content_type='text/plain')))
        rid = self.make(cadence={'type': 'every_hours', 'hours': 24})['id']
        retries = []
        for attempt in range(RETRY_LIMIT + 1):
            self.h.clock.advance(self.row(rid)['next_run_ts'] - self.h.clock() + 1)
            self.assertEqual(self.h.service.tick(), 1)
            retries.append(self.row(rid)['next_run_ts'] - self.h.clock())
        self.assertTrue(all(20 * 60 <= wait < 20 * 60 + 61 for wait in retries[:RETRY_LIMIT]), retries)
        self.assertGreater(retries[RETRY_LIMIT], 23 * HOUR)
        self.assertEqual(self.row(rid)['consecutive_errors'], RETRY_LIMIT + 1)
        self.server.route('/feed.xml', lambda req, conn: conn.send(response(200, rss(self.feed_items))))
        self.h.run(rid)
        self.assertEqual(self.row(rid)['consecutive_errors'], 0)

    def test_the_wake_up_interval_is_about_thirty_seconds_with_a_little_spread(self):
        service = self.h.service
        self.assertEqual(service.tick_seconds, 30.0)
        delays = [service._next_delay() for _ in range(300)]
        self.assertTrue(all(27.0 <= d <= 33.0 for d in delays))
        self.assertGreater(max(delays) - min(delays), 3.0)


class RunNowTests(SchedulerCase):
    def test_run_now_queues_and_the_scheduler_carries_it_out(self):
        rid = self.make()['id']
        queued = self.h.service.run_now(rid)
        self.assertEqual(queued['state'], 'queued')
        self.assertEqual(self.h.service.researcher(rid)['state'], 'queued')
        self.assertEqual(self.h.host.status()['research']['status']['researchers'][0]['state'], 'queued')
        again = self.h.service.run_now(rid)                                  # asking twice does not run it twice
        self.assertEqual(again['state'], 'queued')
        self.assertEqual(self.h.service.tick(), 1)
        self.assertEqual(self.h.service.tick(), 0)
        self.assertEqual(len(self.h.service.briefs(researcher=rid)), 1)
        self.assertEqual(self.h.service.researcher(rid)['state'], 'idle')

    def test_run_now_reports_the_outcome_when_the_run_is_quick(self):
        h = self.start(autostart=True, tick_seconds=0.05, run_now_wait=5.0)
        rid = h.save(**researcher(self.url))['id']
        result = h.call('run_now', {'researcher_id': rid})
        self.assertEqual((result['state'], result['outcome'], result['items']), ('done', 'ready', 1))
        self.assertEqual(result['message'], '1 update ready. Waiting for your review.')
        self.assertEqual(result['researcher_id'], rid)
        self.assertEqual(len(result['brief_id']), 32)

    def test_run_now_says_it_is_running_when_it_takes_a_while(self):
        entered, release = threading.Event(), threading.Event()

        def slow(req, conn):
            entered.set()
            release.wait(5)
            conn.send(response(200, rss(self.feed_items)))

        self.server.route('/feed.xml', slow)
        rid = self.make()['id']
        self.h.service.run_now(rid)
        worker = threading.Thread(target=self.h.service.tick)
        worker.start()
        self.assertTrue(entered.wait(3))
        running = self.h.service.run_now(rid)
        self.assertEqual(running['state'], 'running')
        self.assertIn('running now', running['message'])
        self.assertEqual(self.h.host.status()['research']['status']['researchers'][0]['state'], 'running')
        release.set()
        worker.join(5)
        self.assertEqual(self.h.service.researcher(rid)['state'], 'idle')

    def test_unknown_researchers_and_a_stopped_service_are_plain_errors(self):
        for rid in ('0' * 32, 'f' * 32):
            with self.subTest(rid=rid), self.assertRaisesRegex(ExtensionError, 'does not exist'):
                self.h.call('run_now', {'researcher_id': rid})
        for bad in ('', 'x', '../..', 'A' * 32, 5):
            with self.subTest(rid=bad), self.assertRaises(ExtensionError):
                self.h.call('run_now', {'researcher_id': bad})
        rid = self.make()['id']
        self.h.service.stop()
        with self.assertRaisesRegex(ExtensionError, 'shutting down'):
            self.h.service.run_now(rid)

    def test_two_researchers_never_fetch_at_the_same_time(self):
        live, peak, lock = [0], [0], threading.Lock()

        def slow(req, conn):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            conn.sleep(0.15)
            with lock:
                live[0] -= 1
            conn.send(response(200, rss(self.feed_items)))

        self.server.route('/feed.xml', slow)
        ids = [self.make(name=f'R{n}')['id'] for n in range(3)]
        h = self.h
        threads = [threading.Thread(target=h.service.tick) for _ in range(3)]
        h.clock.advance(120)
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(peak[0], 1)
        self.assertEqual(sorted(self.row(i)['last_outcome'] for i in ids), ['ready'] * 3)
        self.assertEqual(len(self.server.hits('/feed.xml')), 3)                 # three researchers, three runs, none repeated


class SchedulerThreadTests(SchedulerCase):
    def test_the_thread_runs_what_comes_due_and_stops_cleanly(self):
        h = self.start(autostart=True, tick_seconds=0.05)
        self.assertTrue(h.service.running)
        rid = h.save(**researcher(self.url))['id']
        self.assertEqual(h.service.briefs(researcher=rid), [])
        h.clock.advance(61)
        self.assertTrue(wait_until(lambda: h.service.briefs(researcher=rid), 5))
        self.assertEqual(h.service.briefs(researcher=rid)[0]['trigger'], 'scheduled')
        self.assertEqual([t.name for t in research_threads()], ['myradio-research'])
        h.close()
        self.assertFalse(h.service.running)
        self.assertEqual(research_threads(), [])
        h.close()                                                                 # stopping twice is harmless
        h.service.stop()

    def test_nothing_runs_when_nothing_is_due_and_the_thread_just_waits(self):
        h = self.start(autostart=True, tick_seconds=0.02)
        rid = h.save(**researcher(self.url, cadence={'type': 'every_hours', 'hours': 24}))['id']
        time.sleep(0.2)
        self.assertEqual(h.service.briefs(researcher=rid), [])
        self.assertEqual(len(self.server.hits()), 0)
        self.assertIsNotNone(h.service.status()['scheduler']['last_tick_at'])

    def test_stopping_while_a_slow_fetch_is_in_flight_returns_promptly_and_records_nothing(self):
        entered = threading.Event()

        def hang(req, conn):
            entered.set()
            conn.sleep(30)

        self.server.route('/feed.xml', hang)
        h = self.start(autostart=True, tick_seconds=0.05, run_now_wait=0.0)
        rid = h.save(**researcher(self.url))['id']
        h.service.run_now(rid)
        self.assertTrue(entered.wait(5))
        began = time.monotonic()
        h.close()
        self.assertLess(time.monotonic() - began, 4)
        self.assertEqual(research_threads(), [])

    def test_a_crash_in_one_pass_does_not_end_the_thread(self):
        h = self.start(autostart=True, tick_seconds=0.02)
        rid = h.save(**researcher(self.url))['id']
        calls = []
        original = h.service.store.next_due

        def flaky(now):
            calls.append(now)
            if len(calls) == 2:
                raise RuntimeError('boom')
            return original(now)

        with mock.patch.object(h.service.store, 'next_due', side_effect=flaky):
            h.clock.advance(61)
            self.assertTrue(wait_until(lambda: h.service.briefs(researcher=rid), 5))
        self.assertTrue(wait_until(lambda: h.service.status()['scheduler']['problems'] == 1, 5))     # the hiccup is visible, not silent
        self.assertTrue(h.service.running)


class StatusAndChecksTests(SchedulerCase):
    def test_status_never_touches_the_database_or_waits_on_anything(self):
        rid = self.make()['id']
        self.h.run(rid)
        service = self.h.service
        original = service.store

        class Exploding:
            def __getattr__(self, name):
                raise AssertionError(f'status() touched the store: {name}')

        service.store = Exploding()
        try:
            with service._lock, service._snap_lock, service._job_lock:                       # even with every lock held elsewhere
                snapshot = service.status()
                hosted = self.h.host.status()['research']
        finally:
            service.store = original
        self.assertEqual(snapshot['totals']['researchers'], 1)
        self.assertEqual(hosted['state'], 'running')

    def test_status_shape_and_content(self):
        rid = self.make()['id']
        self.h.run(rid)
        status = self.h.host.status()['research']['status']
        self.assertEqual(set(status), {'enabled', 'scheduler', 'researchers', 'totals'})
        entry = status['researchers'][0]
        self.assertEqual((entry['id'], entry['name'], entry['paused'], entry['state']), (rid, 'Backyard astronomy', False, 'idle'))
        self.assertEqual(entry['last_run']['outcome'], 'ready')
        self.assertEqual(entry['last_run']['at'], '2025-11-05T00:00:00+00:00')
        self.assertTrue(entry['next_run_at'].endswith('+00:00'))
        self.assertEqual((entry['pending_review'], status['totals']['pending_review']), (1, 1))
        self.assertEqual(status['totals']['daily_cap'], 500)
        json.dumps(status)
        self.assertLess(len(json.dumps(status)), 1500)

    def test_status_for_fifty_researchers_with_the_longest_names_and_messages_still_fits(self):
        """Names of 80 emoji take twelve bytes each once written as JSON; the whole research status used to be replaced by
        {'truncated': true} when fifty of them were listed. Names in the status are shortened (the full name stays on GET /researchers)."""
        names = []
        for n in range(50):
            name = chr(0x1F600 + n % 40) * 79 + str(n % 10)
            names.append(name)
            rid = self.h.service.save(researcher(self.url, name=name))['researcher']['id']
            self.h.service.store.note_failure(rid, 'x' * 400, NOW, None)
        self.h.service._refresh()
        status = self.h.host.status()['research']['status']
        self.assertNotIn('truncated', status)
        self.assertEqual(len(status['researchers']), 50)
        self.assertLess(len(json.dumps(self.h.service.status())), 62000)
        self.assertTrue(all(len(entry['name']) <= 40 for entry in status['researchers']))
        self.assertEqual(sorted(r['name'] for r in self.h.service.researchers()), sorted(names))          # nothing is lost where it matters

    def test_status_for_fifty_researchers_stays_inside_the_hosts_size_limit(self):
        for n in range(50):
            self.h.service.save(researcher(self.url, name=f'Researcher number {n} with a fairly long name', interests=['a' * 80] * 5))
        self.assertLess(len(json.dumps(self.h.service.status())), 60000)
        self.assertNotIn('truncated', self.h.host.status()['research']['status'])

    def test_checks_start_unknown_then_follow_what_really_happened(self):
        ids = lambda checks: {c['id']: c for c in checks}
        first = ids(self.h.service.checks())
        self.assertEqual(first['network']['status'], 'unknown')
        self.assertEqual(first['clock']['status'], 'ok')
        self.assertEqual(first['allowance']['status'], 'ok')
        rid = self.make()['id']
        self.h.run(rid)
        self.assertEqual(ids(self.h.service.checks())['network']['status'], 'ok')
        self.assertIn('just now', ids(self.h.service.checks())['network']['detail'])
        self.server.close()
        self.h.save(**researcher(self.url, id=rid, name='Still here'))
        for expected in ('warn', 'warn', 'fail'):
            self.h.run(rid)
            self.assertEqual(ids(self.h.service.checks())['network']['status'], expected)
        detail = ids(self.h.service.checks())['network']
        self.assertIn('online', detail['hint'])
        self.assertNotIn('Traceback', detail['detail'])

    def test_a_few_dead_addresses_among_working_ones_are_not_a_problem_with_the_internet(self):
        """Websites that cannot be reached count against 'Reaching the web' only when the run reached no website at all, in whatever
        order the sources are listed. A retired address next to a source that answered says something about that address."""
        dead = ['http://127.0.0.1:1/a.xml', 'http://127.0.0.1:1/b.xml', 'http://127.0.0.1:1/c.xml']
        verdict = lambda: {c['id']: c for c in self.h.service.checks()}['network']
        for position, sources in (('after', [self.url] + dead), ('before', dead + [self.url]), ('between', dead[:1] + [self.url] + dead[1:])):
            with self.subTest(dead=position):
                rid = self.make(name=f'Mixed {position}', sources=sources)['id']
                brief = self.h.run(rid)
                self.assertEqual(sorted(s['status'] for s in brief['sources']), ['error', 'error', 'error', 'ok'])
                check = verdict()
                self.assertEqual(check['status'], 'ok', check)
                self.assertIn('A website was reached', check['detail'])
                self.assertEqual(self.h.service.observations.snapshot()['failures'], 0)

    def test_a_run_that_reaches_nothing_still_counts_every_attempt_and_the_verdict_still_reaches_fail(self):
        verdict = lambda: {c['id']: c for c in self.h.service.checks()}['network']
        dead = ['http://127.0.0.1:1/a.xml', 'http://127.0.0.1:1/b.xml', 'http://127.0.0.1:1/c.xml']
        rid = self.make(sources=dead)['id']
        brief = self.h.run(rid)
        self.assertEqual(brief['outcome'], 'error')
        check = verdict()
        self.assertEqual(check['status'], 'fail')
        self.assertIn('The last 3 attempts could not reach any website', check['detail'])
        self.assertIn('online', check['hint'])
        self.h.save(**researcher(self.url, id=rid))                                    # a website answers again: back to fine at once
        self.h.run(rid)
        self.assertEqual(verdict()['status'], 'ok')

    def test_trying_an_address_in_the_check_box_never_changes_the_verdict_about_the_internet(self):
        before = {c['id']: c for c in self.h.service.checks()}['network']
        self.assertEqual(before['status'], 'unknown')
        for address in ('http://127.0.0.1:1/typo.xml', 'http://127.0.0.1:1/typo2.xml', 'http://127.0.0.1:1/typo3.xml'):
            with self.assertRaises(ExtensionError):
                self.h.service.check_source(address)
        self.assertEqual({c['id']: c for c in self.h.service.checks()}['network'], before)
        self.assertEqual(self.h.service.observations.snapshot()['failures'], 0)

    def test_clock_checks(self):
        status = lambda: {c['id']: c for c in self.h.service.checks()}['clock']
        self.h.clock.now = 1_000_000.0
        self.assertEqual(status()['status'], 'fail')
        self.h.clock.now = NOW
        self.assertEqual(status()['status'], 'ok')
        rid = self.make()['id']
        self.h.run(rid)                                           # the clock moves backwards after a research run
        self.h.clock.now = NOW - 3600
        self.assertEqual(status()['status'], 'warn')
        self.assertIn('backwards', status()['detail'])
        self.h.clock.now = NOW
        service = self.h.service
        for _ in range(3):
            service.observations.reached(NOW, NOW + 3600)         # websites say it is an hour later than this computer thinks
        self.assertEqual(status()['status'], 'warn')
        self.assertIn('60 minutes', status()['detail'])

    def test_allowance_check_warns_before_the_cap_and_fails_at_it(self):
        service = self.h.service
        for used, expected in ((0, 'ok'), (399, 'ok'), (400, 'warn'), (499, 'warn'), (500, 'fail')):
            with mock.patch.object(service.store, 'requests_on', return_value=used):
                check = {c['id']: c for c in service.checks()}['allowance']
                self.assertEqual((used, check['status']), (used, expected))
        self.assertIn('500', check['detail'])

    def test_every_check_is_in_the_shape_the_doctor_expects_and_in_plain_language(self):
        checks = self.h.host.checks()
        self.assertTrue(checks)
        for check in checks:
            self.assertTrue(check['id'].startswith('research.'))
            self.assertIn(check['status'], ('ok', 'warn', 'fail', 'unknown'))
            self.assertGreater(len(check['title']), 3)
            for jargon in ('SSRF', 'TLS', 'DNS', 'SNI', 'PCM', 'GGUF', 'traceback', 'Exception'):
                self.assertNotIn(jargon, check['detail'] + check['hint'])


if __name__ == '__main__':
    unittest.main()
