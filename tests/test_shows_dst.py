"""Daylight saving: gaps skipped, repeated times taken once, shows that meet still meet.

Two zones (America/New_York and Europe/London), two changes each, plus a zone that shifts by half an hour.
Times in the comments are the real clock times of 2024.
"""
import datetime as dt
import itertools
import unittest
from bisect import bisect_left
from zoneinfo import ZoneInfo

from myradio.scheduling import next_occurrence
from myradio.shows import Week
from myradio.shows.timeline import blocks, check_timestamp, check_zone, expand, grid_next, parse_moment, resolve_local, to_local_iso
from myradio.shows import ShowError

try:
    from test_shows_support import EVERY_DAY, at, iso_utc, show, utc_ts
except ImportError:  # run as tests.test_shows_dst
    from tests.test_shows_support import EVERY_DAY, at, iso_utc, show, utc_ts

NY, LONDON = 'America/New_York', 'Europe/London'


def ordinal(year, month, day):
    return dt.date(year, month, day).toordinal()


def spans(week, zone, low, high):
    return [(iso_utc(o.start), iso_utc(o.end), o.show.id) for o in expand(zone, week.shows, low, high)]


def block_starts(zone, start, end):
    return [(iso_utc(a), b - a) for a, b in blocks(zone, start, end)]


class NewYorkSpring(unittest.TestCase):
    """2024-03-10: at 02:00 clocks jump to 03:00 (07:00 UTC). 02:00 to 02:59 never happens."""

    def test_the_skipped_hour_maps_to_the_moment_the_clocks_jump(self):
        day = ordinal(2024, 3, 10)
        got = {m: iso_utc(resolve_local(NY, day, m)) for m in (0, 60, 119, 120, 150, 179, 180, 181, 360)}
        self.assertEqual(got, {0: '2024-03-10 05:00', 60: '2024-03-10 06:00', 119: '2024-03-10 06:59', 120: '2024-03-10 07:00',
                               150: '2024-03-10 07:00', 179: '2024-03-10 07:00', 180: '2024-03-10 07:00', 181: '2024-03-10 07:01',
                               360: '2024-03-10 10:00'})

    def test_a_show_that_spans_the_gap_lasts_an_hour_less(self):
        week = Week(NY, [show('o', start='00:00', end='06:00')])
        self.assertEqual(spans(week, NY, utc_ts(2024, 3, 9, 12), utc_ts(2024, 3, 11, 12)),
                         [('2024-03-10 05:00', '2024-03-10 10:00', 'o'), ('2024-03-11 04:00', '2024-03-11 10:00', 'o')])
        self.assertEqual(block_starts(NY, utc_ts(2024, 3, 10, 5), utc_ts(2024, 3, 10, 10)),
                         [('2024-03-10 05:00', 3600), ('2024-03-10 06:00', 3600), ('2024-03-10 07:00', 3600),
                          ('2024-03-10 08:00', 3600), ('2024-03-10 09:00', 3600)])
        local = [to_local_iso(NY, a)[11:] for a, _ in blocks(NY, utc_ts(2024, 3, 10, 5), utc_ts(2024, 3, 10, 10))]
        self.assertEqual(local, ['00:00:00-05:00', '01:00:00-05:00', '03:00:00-04:00', '04:00:00-04:00', '05:00:00-04:00'])

    def test_two_shows_that_meet_inside_the_gap_still_meet(self):
        week = Week(NY, [show('a', start='00:00', end='02:30'), show('b', start='02:30', end='06:00')])
        marks = [(s.show.id, iso_utc(s.start), iso_utc(s.end)) for s in week.segments(utc_ts(2024, 3, 10, 5), utc_ts(2024, 3, 10, 12))]
        self.assertEqual(marks, [('a', '2024-03-10 05:00', '2024-03-10 07:00'), ('b', '2024-03-10 07:00', '2024-03-10 10:00'),
                                 ('default', '2024-03-10 10:00', '2024-03-10 12:00')])
        day_before = [(s.show.id, iso_utc(s.start), iso_utc(s.end)) for s in week.segments(utc_ts(2024, 3, 9, 5), utc_ts(2024, 3, 9, 12))]
        self.assertEqual(day_before, [('a', '2024-03-09 05:00', '2024-03-09 07:30'), ('b', '2024-03-09 07:30', '2024-03-09 11:00'),
                                      ('default', '2024-03-09 11:00', '2024-03-09 12:00')])

    def test_a_show_wholly_inside_the_gap_does_not_air_that_day(self):
        week = Week(NY, [show('g', start='02:10', end='02:40')])
        self.assertEqual(spans(week, NY, utc_ts(2024, 3, 9), utc_ts(2024, 3, 11, 12)),
                         [('2024-03-09 07:10', '2024-03-09 07:40', 'g'), ('2024-03-11 06:10', '2024-03-11 06:40', 'g')])

    def test_a_start_inside_the_gap_moves_to_the_jump_and_the_end_is_unchanged(self):
        week = Week(NY, [show('h', start='02:30', end='04:00')])
        self.assertEqual(spans(week, NY, utc_ts(2024, 3, 9), utc_ts(2024, 3, 11, 12)),
                         [('2024-03-09 07:30', '2024-03-09 09:00', 'h'), ('2024-03-10 07:00', '2024-03-10 08:00', 'h'),
                          ('2024-03-11 06:30', '2024-03-11 08:00', 'h')])

    def test_the_station_scheduler_and_shows_agree_except_inside_the_gap(self):
        everyday = EVERY_DAY
        for day in range(1, 32):
            for text, minutes in (('06:00', 360), ('00:00', 0), ('23:59', 1439), ('03:00', 180)):
                mine = resolve_local(NY, ordinal(2024, 3, day), minutes)
                self.assertEqual(next_occurrence(NY, text, everyday, mine - 1), mine, (day, text))
        # 02:30 does not exist on the 10th: a recurring point in time skips that day, a show boundary moves to the jump.
        after = at(NY, 2024, 3, 9, 12)
        self.assertEqual(iso_utc(next_occurrence(NY, '02:30', everyday, after)), '2024-03-11 06:30')
        self.assertEqual(iso_utc(resolve_local(NY, ordinal(2024, 3, 10), 150)), '2024-03-10 07:00')
        for day in (9, 11):
            mine = resolve_local(NY, ordinal(2024, 3, day), 150)
            self.assertEqual(next_occurrence(NY, '02:30', everyday, mine - 1), mine)


class NewYorkAutumn(unittest.TestCase):
    """2024-11-03: at 02:00 clocks go back to 01:00 (06:00 UTC). 01:00 to 01:59 happens twice."""

    def test_a_repeated_time_is_the_first_one(self):
        day = ordinal(2024, 11, 3)
        self.assertEqual(iso_utc(resolve_local(NY, day, 90)), '2024-11-03 05:30')       # 01:30 EDT, not 01:30 EST (06:30)
        self.assertEqual(iso_utc(resolve_local(NY, day, 120)), '2024-11-03 07:00')      # 02:00 EST
        self.assertEqual(iso_utc(resolve_local(NY, day, 60)), '2024-11-03 05:00')

    def test_a_show_that_spans_the_repeat_lasts_an_hour_longer(self):
        week = Week(NY, [show('o', start='00:00', end='06:00')])
        self.assertEqual(spans(week, NY, utc_ts(2024, 11, 3), utc_ts(2024, 11, 3, 23)), [('2024-11-03 04:00', '2024-11-03 11:00', 'o')])
        starts = list(blocks(NY, utc_ts(2024, 11, 3, 4), utc_ts(2024, 11, 3, 11)))
        self.assertEqual([(iso_utc(a), b - a) for a, b in starts], [(f'2024-11-03 {h:02d}:00', 3600) for h in range(4, 11)])
        local = [to_local_iso(NY, a)[11:] for a, _ in starts]
        self.assertEqual(local, ['00:00:00-04:00', '01:00:00-04:00', '01:00:00-05:00', '02:00:00-05:00', '03:00:00-05:00',
                                 '04:00:00-05:00', '05:00:00-05:00'])

    def test_a_boundary_in_the_repeated_hour_is_taken_once_at_its_first_occurrence(self):
        week = Week(NY, [show('f1', start='00:00', end='01:30'), show('f2', start='01:30', end='06:00')])
        marks = [(s.show.id, iso_utc(s.start), iso_utc(s.end)) for s in week.segments(utc_ts(2024, 11, 3, 4), utc_ts(2024, 11, 3, 12))]
        self.assertEqual(marks, [('f1', '2024-11-03 04:00', '2024-11-03 05:30'), ('f2', '2024-11-03 05:30', '2024-11-03 11:00'),
                                 ('default', '2024-11-03 11:00', '2024-11-03 12:00')])

    def test_a_show_that_starts_in_the_repeated_hour_airs_once(self):
        week = Week(NY, [show('k', start='01:30', end='03:00')])
        self.assertEqual(spans(week, NY, utc_ts(2024, 11, 3), utc_ts(2024, 11, 4, 12)),
                         [('2024-11-03 05:30', '2024-11-03 08:00', 'k'), ('2024-11-04 06:30', '2024-11-04 08:00', 'k')])

    def test_the_station_scheduler_also_takes_the_first_occurrence_once(self):
        first = next_occurrence(NY, '01:30', EVERY_DAY, utc_ts(2024, 11, 3, 4))
        self.assertEqual(iso_utc(first), '2024-11-03 05:30')
        self.assertEqual(first, resolve_local(NY, ordinal(2024, 11, 3), 90))
        self.assertEqual(iso_utc(next_occurrence(NY, '01:30', EVERY_DAY, first)), '2024-11-04 06:30')


class LondonSpring(unittest.TestCase):
    """2024-03-31: at 01:00 clocks jump to 02:00 (01:00 UTC). 01:00 to 01:59 never happens."""

    def test_the_gap(self):
        day = ordinal(2024, 3, 31)
        got = {m: iso_utc(resolve_local(LONDON, day, m)) for m in (0, 59, 60, 90, 119, 120, 121)}
        self.assertEqual(got, {0: '2024-03-31 00:00', 59: '2024-03-31 00:59', 60: '2024-03-31 01:00', 90: '2024-03-31 01:00',
                               119: '2024-03-31 01:00', 120: '2024-03-31 01:00', 121: '2024-03-31 01:01'})

    def test_overnight_show_and_neighbours(self):
        week = Week(LONDON, [show('o', start='00:00', end='06:00')])
        self.assertEqual(spans(week, LONDON, utc_ts(2024, 3, 31), utc_ts(2024, 3, 31, 23)), [('2024-03-31 00:00', '2024-03-31 05:00', 'o')])
        local = [to_local_iso(LONDON, a)[11:16] for a, _ in blocks(LONDON, utc_ts(2024, 3, 31), utc_ts(2024, 3, 31, 5))]
        self.assertEqual(local, ['00:00', '02:00', '03:00', '04:00', '05:00'])
        meet = Week(LONDON, [show('a', start='00:00', end='01:30'), show('b', start='01:30', end='06:00')])
        marks = [(s.show.id, iso_utc(s.start), iso_utc(s.end)) for s in meet.segments(utc_ts(2024, 3, 31), utc_ts(2024, 3, 31, 6))]
        self.assertEqual(marks, [('a', '2024-03-31 00:00', '2024-03-31 01:00'), ('b', '2024-03-31 01:00', '2024-03-31 05:00'),
                                 ('default', '2024-03-31 05:00', '2024-03-31 06:00')])

    def test_agreement_with_the_station_scheduler(self):
        for day in range(1, 32):
            mine = resolve_local(LONDON, ordinal(2024, 3, day), 360)
            self.assertEqual(next_occurrence(LONDON, '06:00', EVERY_DAY, mine - 1), mine)
        self.assertEqual(iso_utc(next_occurrence(LONDON, '01:30', EVERY_DAY, utc_ts(2024, 3, 30, 12))), '2024-04-01 00:30')   # the 31st is skipped
        self.assertEqual(iso_utc(resolve_local(LONDON, ordinal(2024, 3, 31), 90)), '2024-03-31 01:00')


class LondonAutumn(unittest.TestCase):
    """2024-10-27: at 02:00 clocks go back to 01:00 (01:00 UTC). 01:00 to 01:59 happens twice."""

    def test_the_repeat(self):
        day = ordinal(2024, 10, 27)
        self.assertEqual(iso_utc(resolve_local(LONDON, day, 90)), '2024-10-27 00:30')      # 01:30 BST, the first one
        self.assertEqual(iso_utc(resolve_local(LONDON, day, 120)), '2024-10-27 02:00')

    def test_overnight_show_lasts_seven_hours_and_each_hour_gets_a_block(self):
        week = Week(LONDON, [show('o', start='00:00', end='06:00')])
        self.assertEqual(spans(week, LONDON, utc_ts(2024, 10, 26, 12), utc_ts(2024, 10, 27, 12)),
                         [('2024-10-26 23:00', '2024-10-27 06:00', 'o')])
        starts = list(blocks(LONDON, utc_ts(2024, 10, 26, 23), utc_ts(2024, 10, 27, 6)))
        self.assertEqual(len(starts), 7)
        self.assertEqual({b - a for a, b in starts}, {3600})
        self.assertEqual([to_local_iso(LONDON, a)[11:] for a, _ in starts],
                         ['00:00:00+01:00', '01:00:00+01:00', '01:00:00+00:00', '02:00:00+00:00', '03:00:00+00:00',
                          '04:00:00+00:00', '05:00:00+00:00'])

    def test_a_show_starting_in_the_repeat_airs_once(self):
        week = Week(LONDON, [show('k', start='01:30', end='03:00')])
        self.assertEqual(spans(week, LONDON, utc_ts(2024, 10, 27), utc_ts(2024, 10, 28, 12)),
                         [('2024-10-27 00:30', '2024-10-27 03:00', 'k'), ('2024-10-28 01:30', '2024-10-28 03:00', 'k')])


class AgainstAnOracle(unittest.TestCase):
    """Every wall-clock minute of the days around every 2024 clock change, in zones of every kind, checked
    against a definition written independently of the code: the first real moment at which the local clock
    reads at least that time (so a skipped time becomes the moment of the jump, and a repeated time is its
    first occurrence)."""

    ZONES = (NY, LONDON, 'Australia/Lord_Howe', 'Pacific/Chatham', 'Australia/Sydney', 'Asia/Kolkata', 'America/Sao_Paulo',
             'Europe/Dublin', 'America/St_Johns', 'UTC')

    @staticmethod
    def offset(zone, moment):
        return int(dt.datetime.fromtimestamp(moment, zone).utcoffset().total_seconds())

    def changes(self, zone):
        z = ZoneInfo(zone)
        found, previous = [], self.offset(z, utc_ts(2024, 1, 1))
        for moment in range(utc_ts(2024, 1, 1), utc_ts(2025, 1, 1), 900):
            current = self.offset(z, moment)
            if current != previous:
                found.append(moment)
                previous = current
        return found

    def oracle(self, zone, day):
        z = ZoneInfo(zone)
        base = (day - dt.date(1970, 1, 1).toordinal()) * 86400
        instants = list(range(base - 16 * 3600, base + 4 * 86400 + 16 * 3600, 60))
        latest = list(itertools.accumulate(((m + self.offset(z, m)) // 60 for m in instants), max))
        return lambda local_minute: instants[bisect_left(latest, local_minute)]

    def test_resolve_local_agrees_with_the_oracle_everywhere_it_matters(self):
        checked = 0
        for zone in self.ZONES:
            days = set()
            for moment in self.changes(zone):
                local_day = dt.datetime.fromtimestamp(moment, ZoneInfo(zone)).date().toordinal()
                days.update((local_day - 1, local_day, local_day + 1))
            if zone == 'UTC':
                days = {ordinal(2024, 6, 1)}
            for day in sorted(days):
                first_at_or_after = self.oracle(zone, day)
                midnight = (day - dt.date(1970, 1, 1).toordinal()) * 1440
                minutes = list(range(0, 1441)) + list(range(1441, 2881, 17))
                got = [resolve_local(zone, day, minute) for minute in minutes]
                for minute, value in zip(minutes, got):
                    expected = first_at_or_after(midnight + minute)
                    self.assertEqual(value, expected, f'{zone} {dt.date.fromordinal(day)} minute {minute}: '
                                                      f'{iso_utc(value)} != {iso_utc(expected)}')
                self.assertEqual(got, sorted(got), f'{zone} {dt.date.fromordinal(day)}: boundaries must never run backwards')
                checked += len(minutes)
        self.assertGreater(checked, 50_000)

    def test_there_really_are_clock_changes_to_test(self):
        self.assertEqual(len(self.changes(NY)), 2)
        self.assertEqual(len(self.changes('Australia/Lord_Howe')), 2)
        self.assertEqual(len(self.changes('Asia/Kolkata')), 0)
        self.assertEqual(len(self.changes('Europe/Dublin')), 2)


class HourGrid(unittest.TestCase):
    @staticmethod
    def brute(zone, moment):
        z = ZoneInfo(zone)
        probe = moment + 1
        while True:
            offset = int(dt.datetime.fromtimestamp(probe, z).utcoffset().total_seconds())
            if (probe + offset) % 3600 == 0:
                return probe
            probe += 1

    def test_the_grid_is_the_set_of_local_tops_of_the_hour(self):
        transitions = {NY: (utc_ts(2024, 3, 10, 7), utc_ts(2024, 11, 3, 6)), LONDON: (utc_ts(2024, 3, 31, 1), utc_ts(2024, 10, 27, 1)),
                       'Asia/Kolkata': (utc_ts(2024, 1, 1),), 'Australia/Lord_Howe': (utc_ts(2024, 4, 6, 15), utc_ts(2024, 10, 5, 15, 30)),
                       'Asia/Kathmandu': (utc_ts(2024, 6, 1),)}
        for zone, instants in transitions.items():
            z = ZoneInfo(zone)
            for instant in instants:
                for delta in (-7201, -3601, -1800, -1, 0, 1, 1799, 1800, 1801, 3599, 3600, 5400):
                    moment = instant + delta
                    with self.subTest(zone=zone, instant=iso_utc(instant), delta=delta):
                        self.assertEqual(grid_next(zone, moment), self.brute(zone, moment))
        for zone in ('America/New_York', 'Australia/Lord_Howe'):         # the offsets really do change where the table says
            z = ZoneInfo(zone)
            for instant in transitions[zone]:
                before = dt.datetime.fromtimestamp(instant - 1, z).utcoffset()
                after = dt.datetime.fromtimestamp(instant, z).utcoffset()
                self.assertNotEqual(before, after)

    def test_blocks_are_cut_the_same_way_whatever_the_window(self):
        reference = list(blocks(NY, utc_ts(2024, 3, 9, 20), utc_ts(2024, 3, 11, 4)))
        for start in range(utc_ts(2024, 3, 9, 20), utc_ts(2024, 3, 10, 12), 1777):
            window = list(blocks(NY, start, utc_ts(2024, 3, 11, 4)))
            self.assertEqual(window[1:], [b for b in reference if b[0] >= window[1][0]])


class Planning(unittest.TestCase):
    def slots(self, items):
        found = {}
        for item in items:
            found.setdefault(item.slot_start, []).append(item)
        return found

    def test_planning_across_the_spring_change(self):
        week = Week(NY, [show('o', start='00:00', end='06:00')])
        items = week.plan(utc_ts(2024, 3, 10, 5), utc_ts(2024, 3, 10, 12), 3)
        slots = self.slots(items)
        self.assertEqual([iso_utc(s) for s in slots], [f'2024-03-10 {h:02d}:00' for h in range(5, 12)])
        for start, group in slots.items():
            self.assertEqual(sum(i.est_seconds for i in group), 3600)
            self.assertEqual(group[0].start, start)
        self.assertEqual([group[0].show_id for group in slots.values()], ['o'] * 5 + ['default'] * 2)
        self.assertEqual([group[0].fallback for group in slots.values()], [False] * 5 + [True] * 2)
        self.assertEqual(len({i.id for i in items}), len(items))

    def test_planning_across_the_autumn_change(self):
        week = Week(NY, [show('o', start='00:00', end='06:00')])
        items = week.plan(utc_ts(2024, 11, 3, 4), utc_ts(2024, 11, 3, 12), 3)
        slots = self.slots(items)
        self.assertEqual([iso_utc(s) for s in slots], [f'2024-11-03 {h:02d}:00' for h in range(4, 12)])
        self.assertEqual([g[0].show_id for g in slots.values()], ['o'] * 7 + ['default'])
        self.assertEqual(len({i.id for i in items}), len(items))
        self.assertEqual(len({i.slot_start for i in items if not i.fallback}), 7)

    def test_a_half_hour_zone_has_blocks_that_tile_and_fill_exactly(self):
        zone = 'Australia/Lord_Howe'
        week = Week(zone, [show('a', start='00:00', end='06:00'), show('b', start='06:00', end='24:00')])
        for start in (utc_ts(2024, 4, 5), utc_ts(2024, 10, 4)):
            items = week.plan(start, start + 5 * 86400, 1)
            slots = self.slots(items)
            ordered = sorted(slots)
            boundaries = {s.start for s in week.segments(start - 2 * 86400, start + 8 * 86400)} | {s.end for s in week.segments(start - 2 * 86400, start + 8 * 86400)}
            for here, there in zip(ordered, ordered[1:]):
                self.assertEqual(here + slots[here][0].slot_seconds, there, 'blocks must tile with no hole')
            for slot_start, group in slots.items():
                seconds = group[0].slot_seconds
                self.assertTrue(0 < seconds <= 5400, seconds)
                if start < slot_start and slot_start + seconds <= start + 5 * 86400:      # blocks cut by the window are partial
                    self.assertEqual(sum(i.est_seconds for i in group), seconds)
                if slot_start not in boundaries:
                    offset = int(dt.datetime.fromtimestamp(slot_start, ZoneInfo(zone)).utcoffset().total_seconds())
                    self.assertEqual((slot_start + offset) % 3600, 0)
            self.assertTrue(any(g[0].slot_seconds == 5400 for g in slots.values()), 'the half-hour change makes one block 90 minutes')

    def test_kolkata_hours_fall_on_the_half_hour_in_utc(self):
        week = Week('Asia/Kolkata')
        items = week.plan(utc_ts(2024, 6, 1, 3), utc_ts(2024, 6, 1, 8), 2)
        self.assertEqual([iso_utc(s) for s in self.slots(items)], ['2024-06-01 02:30', '2024-06-01 03:30', '2024-06-01 04:30',
                                                                      '2024-06-01 05:30', '2024-06-01 06:30', '2024-06-01 07:30'])


class ZonesAndMoments(unittest.TestCase):
    def test_zone_names(self):
        for name in ('UTC', 'Europe/London', 'America/Argentina/Buenos_Aires', 'Etc/GMT+5', ' Asia/Tokyo ', 'Asia/Kolkata'):
            with self.subTest(name=name):
                self.assertEqual(check_zone(name), name.strip())
        for bad in ('', ' ', 'Mars/Olympus', 'Europe', 'Europe/', '/etc/passwd', '../../etc/passwd', 'Europe/../UTC', 'UTC\x00', 'x' * 65,
                    'posixrules', 'localtime', 'Factory', 'zone.tab', 'tzdata.zi', 'Europe/Lon don', 'café', None, 5, [], b'UTC', 'UTC;ls'):
            with self.subTest(bad=repr(bad)[:25]), self.assertRaisesRegex(ShowError, 'not recognized'):
                check_zone(bad)

    def test_moments_at_the_api_edge(self):
        base = utc_ts(2025, 3, 1, 11)
        for given in (base, float(base), str(base), f'{base}.0', '2025-03-01T11:00:00Z', '2025-03-01T11:00:00+00:00',
                      '2025-03-01T06:00:00-05:00', '2025-03-01T16:30:00+05:30'):
            with self.subTest(given=given):
                self.assertEqual(parse_moment(given, NY), base)
        for bad in ('2025-03-01T11:00:00', '2025-03-01', 'tomorrow', '', None, True, [], {}, 'x' * 100, '99999999999999', -5,
                    float('nan'), float('inf'), 4102444801, '0001-01-01T00:00:00+00:00', '2200-01-01T00:00:00+00:00'):
            with self.subTest(bad=repr(bad)[:25]), self.assertRaises(ShowError):
                parse_moment(bad, NY)

    def test_timestamps(self):
        self.assertEqual(check_timestamp(10.9), 10)
        for bad in (-0.5, True, '5', None, float('nan'), 1e300, 4102444801):
            with self.subTest(bad=bad), self.assertRaises(ShowError):
                check_timestamp(bad)


if __name__ == '__main__':
    unittest.main()
