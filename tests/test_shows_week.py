"""The week: limits, overlaps, gaps, the day map, and what is on air when."""
import json
import random
import time
import unittest

from myradio.shows import WEEK_TEMPLATES, Show, ShowError, Week, default_show, find_gaps, find_overlaps
from myradio.shows.conflicts import day_view, overlap_message, report, suggest_fixes, would_overlap_message
from myradio.shows.labels import DAY_MIN, WEEK_MIN
from myradio.shows.model import MAX_SHOWS

try:
    from test_shows_support import EVERY_DAY, at, show
except ImportError:  # run as tests.test_shows_week
    from tests.test_shows_support import EVERY_DAY, at, show

ZONE = 'Europe/London'


def starter():
    return Week('America/New_York', WEEK_TEMPLATES['starter'].build())


class Templates(unittest.TestCase):
    def test_every_ready_made_week_is_valid_and_has_no_overlaps(self):
        for name, template in WEEK_TEMPLATES.items():
            with self.subTest(template=name):
                shows = template.build()
                self.assertEqual(find_overlaps(shows)[0], [])
                self.assertEqual(len(Week('UTC', shows).shows), len(shows))
                data = template.describe()
                json.dumps(data)
                self.assertEqual((data['name'], data['show_count']), (name, len(shows)))
                self.assertTrue(data['title'] and data['description'])
        self.assertEqual(list(WEEK_TEMPLATES), ['starter', 'empty'])

    def test_the_starter_week_matches_the_design_preview(self):
        """The five sample shows in the preview (id, name, hours, style, host, talk, spots, update, color)."""
        expected = [('overnight', 'Overnight Drift', 0, 6, 'ambient-garden', None, 'rare', 15, False, False, '#3b4a63'),
                    ('morning', 'Morning Rise', 6, 10, 'bossa-courtyard', 'dex', 'often', 70, True, True, '#8a4d3a'),
                    ('daytime', 'Daytime Focus', 10, 17, 'chamber-minimalism', 'nova', 'rare', 35, True, False, '#3f6050'),
                    ('golden', 'Golden Hour', 17, 20, 'warm-brush-jazz', 'marlowe', 'some', 50, True, True, '#7a5c26'),
                    ('late', 'Late Night', 20, 24, 'downtempo-trip-hop', 'nova', 'some', 30, False, False, '#454a7a')]
        got = [(s.id, s.name, s.start_min // 60, s.end_min // 60, s.style.palettes[0], s.host_id, s.talk, s.style.energy,
                s.spots_allowed, s.updates_allowed, s.color) for s in WEEK_TEMPLATES['starter'].build()]
        self.assertEqual(got, expected)
        for built in WEEK_TEMPLATES['starter'].build():
            self.assertEqual(built.days, EVERY_DAY)
            self.assertIsNone(built.update_researcher_id)            # no researcher exists yet, so none is named

    def test_the_starter_week_covers_every_hour_and_the_empty_week_covers_none(self):
        self.assertEqual(find_gaps(WEEK_TEMPLATES['starter'].build()), [])
        self.assertEqual(report(Week('UTC', WEEK_TEMPLATES['starter'].build()))['gap_hours'], 0.0)
        self.assertEqual(WEEK_TEMPLATES['empty'].build(), [])


class Limits(unittest.TestCase):
    def test_the_cap_rejects_thousands_of_shows_quickly(self):
        base = show('s0', start='01:00', end='02:00')
        many = [base.replace(id=f's{i}') for i in range(10_000)]
        started = time.monotonic()
        with self.assertRaisesRegex(ShowError, f'up to {MAX_SHOWS} shows'):
            Week('UTC', many)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(Week('UTC', many[:MAX_SHOWS]).shows), MAX_SHOWS)
        with self.assertRaises(ShowError):
            Week('UTC', many[:MAX_SHOWS + 1])

    def test_a_week_validates_what_it_is_given(self):
        a = show('a')
        with self.assertRaisesRegex(ShowError, 'share the id a'):
            Week('UTC', [a, a])
        with self.assertRaisesRegex(ShowError, 'Only ordinary shows'):
            Week('UTC', [default_show()])
        with self.assertRaisesRegex(ShowError, 'Only ordinary shows'):
            Week('UTC', ['not a show'])
        with self.assertRaisesRegex(ShowError, 'not the default show'):
            Week('UTC', [], default=a)
        for zone in ('Mars/Base', '', None, 5, '../../etc/passwd', 'UTC\x00', 'x' * 500, 'Europe/', 'posixrules', ' '):
            with self.subTest(zone=repr(zone)[:20]), self.assertRaises(ShowError):
                Week(zone)

    def test_there_is_always_a_default_show(self):
        week = Week('UTC')
        self.assertTrue(week.default.is_default)
        self.assertEqual(week.all_shows()[0], week.default)
        self.assertEqual(week.get('default'), week.default)
        self.assertIsNone(week.get('missing'))
        louder = default_show(name='Anytime Mix')
        self.assertEqual(week.with_show(louder).default.name, 'Anytime Mix')
        self.assertEqual(week.with_show(louder).shows, ())

    def test_changes_return_new_weeks_and_leave_the_old_one_alone(self):
        week = Week('UTC', [show('a')])
        more = week.with_show(show('b', start='12:00', end='13:00'))
        self.assertEqual([s.id for s in week.shows], ['a'])
        self.assertEqual([s.id for s in more.shows], ['a', 'b'])
        self.assertEqual([s.id for s in more.without('a').shows], ['b'])
        replaced = more.with_show(show('a', name='Renamed'))
        self.assertEqual(replaced.get('a').name, 'Renamed')
        self.assertEqual(len(replaced.shows), 2)
        self.assertEqual(more.with_timezone('Asia/Tokyo').timezone, 'Asia/Tokyo')
        self.assertEqual(more.timezone, 'UTC')

    def test_shows_are_kept_in_a_stable_order(self):
        week = Week('UTC', [show('z', start='12:00', end='13:00'), show('b', start='06:00', end='07:00'),
                            show('a', start='06:00', end='07:00', days='1')])
        self.assertEqual([s.id for s in week.shows], ['a', 'b', 'z'])


class Overlaps(unittest.TestCase):
    def find(self, *shows):
        overlaps, truncated = find_overlaps(shows)
        self.assertFalse(truncated)
        return overlaps

    def test_touching_shows_do_not_overlap(self):
        self.assertEqual(self.find(show('a', end='10:00'), show('b', start='10:00', end='17:00')), [])
        self.assertEqual(self.find(show('late', start='22:00', end='02:00'), show('early', start='02:00', end='06:00')), [])
        self.assertEqual(self.find(show('a', start='20:00', end='24:00'), show('b', start='00:00', end='06:00')), [])

    def test_a_simple_overlap_names_who_started_first_and_when(self):
        morning = show('morning', 'Morning Rise')
        club = show('club', 'Breakfast Club', start='09:00', end='11:00', days='weekdays')
        for order in ((morning, club), (club, morning)):
            (overlap,) = self.find(*order)
            self.assertEqual((overlap.first.id, overlap.second.id), ('morning', 'club'))
            self.assertEqual(overlap.groups(), [((1, 2, 3, 4, 5), 540, 60)])
            self.assertEqual(overlap_message(overlap),
                             'Morning Rise (Every day, 6 am–10 am) and Breakfast Club (Weekdays, 9 am–11 am) '
                             'are both scheduled on weekdays from 9 am to 10 am.')

    def test_ties_go_to_the_smaller_id(self):
        (overlap,) = self.find(show('b'), show('a'))
        self.assertEqual((overlap.first.id, overlap.second.id), ('a', 'b'))

    def test_only_shared_days_overlap(self):
        self.assertEqual(self.find(show('a', days='1,2,3'), show('b', days='4,5')), [])
        (overlap,) = self.find(show('a', days='1,2,3'), show('b', days='3,4'))
        self.assertEqual(overlap.groups(), [((3,), 360, 240)])

    def test_a_show_that_runs_past_midnight_overlaps_the_next_days_early_show(self):
        late = show('late', 'Late', start='22:00', end='02:00')
        early = show('early', 'Earlybird', start='01:00', end='03:00', days='2')
        (overlap,) = self.find(late, early)
        self.assertEqual((overlap.first.id, overlap.second.id), ('late', 'early'))
        self.assertEqual(overlap.groups(), [((2,), 60, 60)])
        self.assertIn('are both scheduled on Tuesday from 1 am to 2 am', overlap_message(overlap))

    def test_the_end_of_the_week_runs_into_monday(self):
        sunday_night = show('sun', start='23:00', end='03:00', days='7')
        monday = show('mon', start='01:00', end='02:00', days='1')
        (overlap,) = self.find(sunday_night, monday)
        self.assertEqual((overlap.first.id, overlap.groups()), ('sun', [((1,), 60, 60)]))
        self.assertEqual(self.find(sunday_night, show('mon3', start='03:00', end='04:00', days='1')), [])

    def test_an_overlap_across_the_end_of_the_week_is_one_overlap(self):
        a = show('a', start='22:00', end='04:00', days='7')
        b = show('b', start='23:00', end='02:00', days='7')
        (overlap,) = self.find(a, b)
        self.assertEqual(len(overlap.pieces), 1)
        self.assertEqual(overlap.groups(), [((7,), 1380, 180)])
        self.assertEqual((overlap.first.id, overlap.second.id), ('a', 'b'))

    def test_all_day_shows(self):
        all_day = show('all', start='00:00', end='24:00')
        self.assertEqual(self.find(all_day), [])                       # a show never overlaps itself
        self.assertEqual(len(self.find(all_day, show('x', start='05:00', end='05:15'))), 1)
        self.assertEqual(len(self.find(all_day, show('y', start='05:00', end='05:15', days='1'))), 1)
        self.assertEqual(len(self.find(all_day, show('twin', start='00:00', end='24:00'))), 1)

    def test_a_long_show_repeating_daily_does_not_overlap_itself(self):
        night = show('night', start='12:00', end='11:00')            # 12:00 to 11:00 the next day: 23 hours, every day
        self.assertEqual(night.length_min, 1380)
        self.assertEqual(self.find(night), [])

    def test_hostile_overlap_data_is_bounded(self):
        everywhere = [show(f'x{i:03d}', start='00:00', end='24:00') for i in range(MAX_SHOWS)]
        started = time.monotonic()
        overlaps, truncated = find_overlaps(everywhere)
        self.assertTrue(truncated)
        self.assertLess(time.monotonic() - started, 2.0)
        week = Week('UTC', everywhere)
        started = time.monotonic()
        data = report(week)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertFalse(data['ok'])
        self.assertTrue(data['overlaps_truncated'])
        self.assertLessEqual(len(data['overlaps']), 50)
        self.assertEqual(len(overlaps), len({(o.first.id, o.second.id) for o in overlaps}))

    def test_messages_for_a_save_that_would_overlap(self):
        morning = show('morning', 'Morning Rise')
        club = show('club', 'Breakfast Club', start='09:00', end='11:00', days='weekdays')
        week = Week(ZONE, [morning, club])
        (overlap,) = find_overlaps(week.shows)[0]
        fixes = suggest_fixes(week, overlap)
        text = would_overlap_message(club, overlap, fixes)
        self.assertEqual(text, 'Breakfast Club would overlap Morning Rise (Every day, 6 am–10 am) on weekdays from 9 am to 10 am. '
                               'Start Breakfast Club at 10 am. Or make Morning Rise end at 9 am.')
        self.assertIn('also overlaps 2 other shows', would_overlap_message(club, overlap, fixes, extra=2))


class Fixes(unittest.TestCase):
    def check_fixes(self, *shows):
        week = Week(ZONE, shows)
        before = find_overlaps(week.shows)[0]
        self.assertTrue(before)
        applied = 0
        for overlap in before:
            fixes = suggest_fixes(week, overlap)
            self.assertTrue(fixes and fixes[-1]['operation'] == 'shows.remove')
            for fix in fixes:
                if fix['operation'] == 'shows.save':
                    change = fix['arguments']['show']
                    changed = Show.parse(change, week.get(change['id']))
                    after = find_overlaps(week.with_show(changed).shows)[0]
                    self.assertLess(len(after), len(before), fix['text'])
                    self.assertFalse(any({o.first.id, o.second.id} == {overlap.first.id, overlap.second.id} for o in after), fix['text'])
                    applied += 1
                else:
                    gone = week.without(fix['arguments']['id'])
                    self.assertLess(len(find_overlaps(gone.shows)[0]), len(before))
        return applied

    def test_suggested_fixes_really_clear_the_overlap(self):
        self.assertGreater(self.check_fixes(show('a', start='06:00', end='10:00'),
                                            show('b', start='09:00', end='11:00', days='weekdays')), 0)
        self.assertGreater(self.check_fixes(show('late', start='22:00', end='02:00'),
                                            show('early', start='01:00', end='03:00', days='2')), 0)
        self.assertGreater(self.check_fixes(show('sun', start='22:00', end='04:00', days='7'),
                                            show('mon', start='02:00', end='08:00', days='1')), 0)
        self.assertGreater(self.check_fixes(show('long', start='06:00', end='12:00', days='6,7'),
                                            show('brunch', start='10:00', end='14:00', days='7')), 0)

    def test_a_show_inside_another_can_only_shorten_the_big_one_or_be_removed(self):
        week = Week(ZONE, [show('big', start='06:00', end='18:00', days='1,2'), show('small', start='09:00', end='10:00', days='1,2')])
        (overlap,) = find_overlaps(week.shows)[0]
        fixes = suggest_fixes(week, overlap)
        self.assertEqual([f['operation'] for f in fixes], ['shows.save', 'shows.remove'])
        self.assertEqual(fixes[0]['arguments'], {'show': {'id': 'big', 'end': '09:00'}})
        self.assertEqual(fixes[1]['arguments'], {'id': 'small'})

    def test_only_fixes_the_station_would_accept_are_offered(self):
        """A fix is a shows.save that must itself pass the no-overlap rule, so it may not leave the changed show touching any other show."""
        rng = random.Random(41)
        offered = 0
        for _ in range(60):
            shows = []
            for n in range(rng.randrange(2, 6)):
                start = rng.randrange(0, 1440, 15)
                end = (start + rng.choice((60, 120, 240, 480))) % 1440 or 1440
                shows.append(Show.parse({'id': f's{n}', 'name': f'Show {n}', 'start': f'{start // 60:02d}:{start % 60:02d}',
                                         'end': f'{end // 60:02d}:{end % 60:02d}', 'days': sorted(rng.sample(range(1, 8), rng.randrange(1, 8)))}))
            week = Week(ZONE, shows)                                 # overlaps are allowed in a Week: this is "damaged" data
            for overlap in find_overlaps(week.shows)[0][:5]:
                for fix in suggest_fixes(week, overlap):
                    if fix['operation'] != 'shows.save':
                        continue
                    change = fix['arguments']['show']
                    changed = Show.parse(change, week.get(change['id']))
                    clashes = [o for o in find_overlaps(week.with_show(changed).shows)[0] if changed.id in (o.first.id, o.second.id)]
                    self.assertEqual(clashes, [], fix['text'])
                    offered += 1
        self.assertGreater(offered, 20)

    def test_a_fix_that_would_still_clash_with_a_third_show_is_not_offered(self):
        week = Week(ZONE, [show('a', 'Morning Rise'), show('c', 'Daytime Focus', start='10:00', end='17:00'),
                           show('b', 'Breakfast Club', start='09:00', end='11:00', days='weekdays')])
        overlaps = find_overlaps(week.shows)[0]
        self.assertEqual(len(overlaps), 2)
        first = [o for o in overlaps if {o.first.id, o.second.id} == {'a', 'b'}][0]
        texts = [f['text'] for f in suggest_fixes(week, first)]
        self.assertNotIn('Start Breakfast Club at 10 am.', texts)        # Breakfast Club would then clash with Daytime Focus
        self.assertEqual(texts[0], 'Make Morning Rise end at 9 am.')
        self.assertEqual(texts[-1], 'Remove Breakfast Club.')

    def test_the_smallest_change_comes_first(self):
        weekend_clash = Week(ZONE, [show('a', 'Morning Rise'), show('b', 'Brunch Club', start='08:00', end='12:00', days='6,7')])
        (overlap,) = find_overlaps(weekend_clash.shows)[0]
        self.assertEqual([f['text'] for f in suggest_fixes(weekend_clash, overlap)],
                         ['Start Brunch Club at 10 am.', 'Stop Morning Rise airing on weekends.', 'Remove Brunch Club.'])
        # Shortening Morning Rise on all seven days to fix a weekend-only clash would lose the most airtime, so it is not offered first.
        weekday_clash = Week(ZONE, [show('a', 'Morning Rise'), show('b', 'Breakfast Club', start='09:00', end='11:00', days='weekdays')])
        (overlap,) = find_overlaps(weekday_clash.shows)[0]
        self.assertEqual([f['text'] for f in suggest_fixes(weekday_clash, overlap)],
                         ['Start Breakfast Club at 10 am.', 'Make Morning Rise end at 9 am.', 'Remove Breakfast Club.'])

    def test_every_fix_text_is_plain_language(self):
        week = Week(ZONE, [show('a', 'Morning Rise', start='06:00', end='10:00'), show('b', 'Breakfast Club', start='09:00', end='11:00')])
        (overlap,) = find_overlaps(week.shows)[0]
        for fix in suggest_fixes(week, overlap):
            self.assertRegex(fix['text'], r'^[A-Z].*\.$')
            self.assertNotRegex(fix['text'], r'shows\.|\{|\}|None')


class Gaps(unittest.TestCase):
    def test_a_full_week_has_no_gaps(self):
        week = starter()
        self.assertEqual(find_gaps(week.shows), [])
        data = report(week)
        self.assertEqual((data['ok'], data['gaps'], data['gap_hours']), (True, [], 0.0))
        self.assertEqual(data['summary'], 'No overlaps, and every hour of the week has a show.')

    def test_an_empty_week_says_the_default_show_plays_all_day(self):
        data = report(Week('UTC'))
        self.assertEqual(data['summary'], 'No shows are scheduled yet, so Station Mix plays all day.')
        self.assertEqual(len(data['gaps']), 1)
        self.assertIn('plays all day, every day', data['gaps'][0]['message'])
        self.assertEqual(data['gaps'][0]['fixes'][0]['arguments'], {'name': 'starter'})
        self.assertEqual(data['gap_hours'], 168.0)

    def test_a_missing_part_of_every_day(self):
        week = Week('UTC', [s for s in starter().shows if s.id != 'daytime'])
        gaps = find_gaps(week.shows)
        self.assertEqual([(g.start, g.length) for g in gaps], [(d * DAY_MIN + 600, 420) for d in range(7)])
        data = report(week)
        (entry,) = data['gaps']
        self.assertEqual((entry['days'], entry['start'], entry['end']), ('1,2,3,4,5,6,7', '10:00', '17:00'))
        self.assertEqual(entry['message'], 'Nothing is scheduled every day from 10 am to 5 pm, so Station Mix (your default show) plays then.')
        self.assertEqual(entry['fixes'][0]['arguments']['show'], {'days': '1,2,3,4,5,6,7', 'start': '10:00', 'end': '17:00'})
        self.assertEqual(data['gap_hours'], 49.0)
        self.assertEqual(data['summary'], 'No overlaps. About 49 hours a week have no show, and Station Mix plays then.')

    def test_weekday_only_shows_leave_nights_and_a_long_weekend_gap(self):
        week = Week('UTC', [show('work', start='09:00', end='17:00', days='weekdays')])
        gaps = find_gaps(week.shows)
        self.assertEqual([(g.start, g.length) for g in gaps],
                         [(1020, 960), (2460, 960), (3900, 960), (5340, 960), (6780, 3840)])   # the last one runs Friday 5 pm to Monday 9 am

    def test_gap_messages_for_weekday_shows(self):
        week = Week('UTC', [show('work', start='09:00', end='17:00', days='weekdays')])
        data = report(week)
        self.assertEqual([g['message'] for g in data['gaps']],
                         ['Nothing is scheduled on Monday to Thursday from 5 pm to 9 am, so Station Mix (your default show) plays then.',
                          'Nothing is scheduled from Friday at 5 pm until Monday at 9 am, so Station Mix (your default show) plays then.'])
        self.assertEqual(data['gap_hours'], 128.0)
        self.assertEqual([g['fixes'] for g in data['gaps']][1], [])           # too long for one show

    def test_a_gap_that_wraps_the_end_of_the_week_is_one_gap(self):
        week = Week('UTC', [show('x', start='06:00', end='10:00', days='3')])      # Wednesday 06:00 to 10:00 only
        gaps = find_gaps(week.shows)
        self.assertEqual([(g.start, g.length) for g in gaps], [(3480, WEEK_MIN - 240)])   # Wednesday 10:00 round to Wednesday 06:00

    def test_shows_and_gaps_always_add_up_to_the_week(self):
        rng = random.Random(5)
        for _ in range(60):
            shows = []
            for n in range(rng.randrange(0, 6)):
                start = rng.randrange(0, 1440)
                end = (start + rng.randrange(15, 600)) % 1440 or 1440
                candidate = Show.parse({'id': f's{n}', 'name': f's{n}', 'start': f'{start // 60}:{start % 60:02d}',
                                        'end': f'{end // 60:02d}:{end % 60:02d}',
                                        'days': sorted(rng.sample(range(1, 8), rng.randrange(1, 8)))})
                if not find_overlaps(shows + [candidate])[0]:
                    shows.append(candidate)
            covered = sum(len(s.day_set) * s.length_min for s in shows)
            self.assertEqual(covered + sum(g.length for g in find_gaps(shows)), WEEK_MIN)


class DayMap(unittest.TestCase):
    def test_the_starter_week_fills_every_day_with_five_shows(self):
        days = day_view(starter())
        self.assertEqual([d['name'] for d in days][:2], ['Monday', 'Tuesday'])
        for day in days:
            self.assertEqual([(b['show_id'], b['start'], b['end']) for b in day['blocks']],
                             [('overnight', '00:00', '06:00'), ('morning', '06:00', '10:00'), ('daytime', '10:00', '17:00'),
                              ('golden', '17:00', '20:00'), ('late', '20:00', '24:00')])
            self.assertFalse(any(b['fallback'] for b in day['blocks']))
            self.assertFalse(any(b['continues_from_previous_day'] or b['continues_to_next_day'] for b in day['blocks']))

    def test_gaps_show_the_default_show_and_midnight_shows_continue(self):
        week = Week(ZONE, [show('late', start='22:00', end='02:00')])
        monday, tuesday = day_view(week)[:2]
        self.assertEqual([(b['show_id'], b['start'], b['end'], b['fallback']) for b in monday['blocks']],
                         [('late', '00:00', '02:00', False), ('default', '02:00', '22:00', True), ('late', '22:00', '24:00', False)])
        self.assertTrue(monday['blocks'][0]['continues_from_previous_day'])    # carried over from Sunday night
        self.assertTrue(monday['blocks'][2]['continues_to_next_day'])
        self.assertFalse(monday['blocks'][1]['continues_from_previous_day'])
        self.assertTrue(tuesday['blocks'][0]['continues_from_previous_day'])

    def test_a_show_on_one_day_only(self):
        week = Week(ZONE, [show('sat', start='20:00', end='04:00', days='6')])
        days = day_view(week)
        self.assertEqual([b['show_id'] for b in days[5]['blocks']], ['default', 'sat'])
        self.assertEqual([(b['show_id'], b['start'], b['end']) for b in days[6]['blocks']], [('sat', '00:00', '04:00'), ('default', '04:00', '24:00')])
        self.assertEqual([b['show_id'] for b in days[0]['blocks']], ['default'])

    def test_every_day_is_covered_exactly_once(self):
        rng = random.Random(9)
        for _ in range(80):
            shows = []
            for n in range(rng.randrange(0, 6)):
                start = rng.randrange(0, 1440)
                end = (start + rng.randrange(15, 900)) % 1440 or 1440
                candidate = Show.parse({'id': f's{n}', 'name': f's{n}', 'start': f'{start // 60}:{start % 60:02d}',
                                        'end': f'{end // 60:02d}:{end % 60:02d}', 'days': sorted(rng.sample(range(1, 8), rng.randrange(1, 8)))})
                if not find_overlaps(shows + [candidate])[0]:
                    shows.append(candidate)
            for day in day_view(Week(ZONE, shows)):
                cursor = 0
                for block in day['blocks']:
                    self.assertEqual(block['start_minute'], cursor)
                    self.assertGreater(block['end_minute'], block['start_minute'])
                    cursor = block['end_minute']
                self.assertEqual(cursor, DAY_MIN)

    def test_an_all_week_show_continues_round_the_circle(self):
        week = Week(ZONE, [show('all', start='00:00', end='24:00')])
        days = day_view(week)
        self.assertEqual(len(days[0]['blocks']), 1)
        self.assertTrue(days[0]['blocks'][0]['continues_from_previous_day'])
        self.assertTrue(days[6]['blocks'][0]['continues_to_next_day'])


class OnAir(unittest.TestCase):
    def setUp(self):
        self.week = Week(ZONE, [show('morning', 'Morning', start='06:00', end='10:00'), show('late', 'Late', start='22:00', end='02:00')])

    def test_now_and_next_change(self):
        now = self.week.airing(at(ZONE, 2025, 1, 15, 7, 30))
        self.assertEqual((now.show.id, now.fallback), ('morning', False))
        self.assertEqual((now.since, now.until), (at(ZONE, 2025, 1, 15, 6), at(ZONE, 2025, 1, 15, 10)))
        self.assertEqual((now.next_show.id, now.next_fallback), ('default', True))
        self.assertEqual(self.week.next_change(at(ZONE, 2025, 1, 15, 7, 30)), (at(ZONE, 2025, 1, 15, 10), self.week.default, True))

    def test_the_boundary_belongs_to_the_show_that_starts(self):
        now = self.week.airing(at(ZONE, 2025, 1, 15, 10))
        self.assertEqual((now.show.id, now.fallback, now.since), ('default', True, at(ZONE, 2025, 1, 15, 10)))
        self.assertEqual(now.until, at(ZONE, 2025, 1, 15, 22))
        self.assertEqual(self.week.airing(at(ZONE, 2025, 1, 15, 6)).show.id, 'morning')
        self.assertEqual(self.week.airing(at(ZONE, 2025, 1, 15, 6) - 1).show.id, 'default')

    def test_a_show_that_runs_past_midnight(self):
        before = self.week.airing(at(ZONE, 2025, 1, 15, 23, 30))
        after = self.week.airing(at(ZONE, 2025, 1, 16, 1))
        for airing in (before, after):
            self.assertEqual(airing.show.id, 'late')
            self.assertEqual((airing.since, airing.until), (at(ZONE, 2025, 1, 15, 22), at(ZONE, 2025, 1, 16, 2)))
            self.assertEqual(airing.next_show.id, 'default')
        self.assertEqual(self.week.airing(at(ZONE, 2025, 1, 16, 2)).show.id, 'default')

    def test_the_week_wraps_from_sunday_into_monday(self):
        week = Week(ZONE, [show('sun', start='23:00', end='01:00', days='7')])
        airing = week.airing(at(ZONE, 2025, 1, 20, 0, 30))                  # Monday 20 January 2025
        self.assertEqual((airing.show.id, airing.since, airing.until), ('sun', at(ZONE, 2025, 1, 19, 23), at(ZONE, 2025, 1, 20, 1)))

    def test_a_long_gap_and_an_empty_week(self):
        monday = Week(ZONE, [show('mon', start='06:00', end='10:00', days='1')])
        airing = monday.airing(at(ZONE, 2025, 1, 16, 12))                    # a Thursday
        self.assertEqual((airing.show.id, airing.fallback), ('default', True))
        self.assertEqual((airing.since, airing.until), (at(ZONE, 2025, 1, 13, 10), at(ZONE, 2025, 1, 20, 6)))
        empty = Week(ZONE).airing(at(ZONE, 2025, 1, 16, 12))
        self.assertEqual((empty.show.id, empty.since, empty.until, empty.next_show), ('default', None, None, None))
        self.assertIsNone(Week(ZONE).next_change(at(ZONE, 2025, 1, 16, 12)))

    def test_times_must_be_sensible(self):
        for bad in (-1, float('nan'), float('inf'), 1e30, 4102444800, 4102444801, True, '10', None):
            with self.subTest(bad=bad), self.assertRaises(ShowError):
                self.week.airing(bad)
        self.assertIsNone(self.week.airing(4102444799).until)                 # the last second of the supported range

    def test_segments_tile_time_exactly(self):
        rng = random.Random(31)
        zones = ('UTC', 'America/New_York', 'Europe/London', 'Asia/Kolkata', 'Australia/Lord_Howe')
        for round_ in range(40):
            zone = rng.choice(zones)
            shows = []
            for n in range(rng.randrange(0, 5)):
                start = rng.randrange(0, 1440)
                end = (start + rng.randrange(15, 800)) % 1440 or 1440
                candidate = Show.parse({'id': f's{n}', 'name': f's{n}', 'start': f'{start // 60}:{start % 60:02d}',
                                        'end': f'{end // 60:02d}:{end % 60:02d}', 'days': sorted(rng.sample(range(1, 8), rng.randrange(1, 8)))})
                if not find_overlaps(shows + [candidate])[0]:
                    shows.append(candidate)
            week = Week(zone, shows)
            low = at(zone, 2024, rng.choice((3, 4, 10, 11)), rng.randrange(1, 28), rng.randrange(24))
            high = low + rng.randrange(1, 10 * 86400)
            segments = week.segments(low, high)
            self.assertEqual((segments[0].start, segments[-1].end), (low, high))
            for left, right in zip(segments, segments[1:]):
                self.assertEqual(left.end, right.start)
                self.assertNotEqual((left.show.id, left.fallback), (right.show.id, right.fallback), 'neighbours of one show are merged')
            for segment in segments:
                self.assertLess(segment.start, segment.end)
                self.assertEqual(segment.fallback, segment.show.is_default)

    def test_damaged_overlapping_data_still_has_one_answer(self):
        a = show('a', start='06:00', end='10:00')
        b = show('b', start='08:00', end='12:00')
        twin = show('c', start='08:00', end='12:00')
        week = Week('UTC', [a, b, twin])
        segments = week.segments(at('UTC', 2025, 1, 15, 5), at('UTC', 2025, 1, 15, 13))
        self.assertEqual([(s.show.id, s.start, s.end) for s in segments],
                         [('default', at('UTC', 2025, 1, 15, 5), at('UTC', 2025, 1, 15, 6)),
                          ('a', at('UTC', 2025, 1, 15, 6), at('UTC', 2025, 1, 15, 8)),
                          ('b', at('UTC', 2025, 1, 15, 8), at('UTC', 2025, 1, 15, 12)),
                          ('default', at('UTC', 2025, 1, 15, 12), at('UTC', 2025, 1, 15, 13))])


if __name__ == '__main__':
    unittest.main()
