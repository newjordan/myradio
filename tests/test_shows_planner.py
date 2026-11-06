"""The planner: plan items, determinism, stability and bounds."""
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from myradio.shows import ROLES, WEEK_TEMPLATES, PlanItem, Show, ShowError, Week, find_overlaps
from myradio.shows.planner import MAX_PLAN_SECONDS, ROLE_KIND

try:
    from test_shows_support import at, iso_utc, show, utc_ts
except ImportError:  # run as tests.test_shows_planner
    from tests.test_shows_support import at, iso_utc, show, utc_ts

NY = 'America/New_York'
ROOT = Path(__file__).resolve().parent.parent
ITEM_KEYS = {'id', 'start', 'role', 'kind', 'show_id', 'host_id', 'est_seconds', 'constraints', 'slot_start', 'slot_seconds',
             'position', 'ordinal', 'fallback', 'signature', 'label'}


def starter(zone=NY):
    return Week(zone, WEEK_TEMPLATES['starter'].build())


def digest(items, zone=NY):
    return hashlib.sha256(json.dumps([i.to_dict(zone) for i in items], sort_keys=True).encode()).hexdigest()


def random_week(rng, zone):
    shows = []
    for n in range(rng.randrange(0, 6)):
        start = rng.randrange(0, 1440)
        end = (start + rng.randrange(15, 800)) % 1440 or 1440
        candidate = Show.parse({'id': f's{n}', 'name': f's{n}', 'start': f'{start // 60}:{start % 60:02d}',
                                'end': f'{end // 60:02d}:{end % 60:02d}', 'days': sorted(rng.sample(range(1, 8), rng.randrange(1, 8))),
                                'host_id': rng.choice([None, 'host-a']), 'talk': rng.choice(['rare', 'some', 'often']),
                                'spots_allowed': rng.random() < .5, 'updates_allowed': rng.random() < .5,
                                'clock': rng.choice(['classic', 'music-heavy', 'talky'])})
        if not find_overlaps(shows + [candidate])[0]:
            shows.append(candidate)
    return Week(zone, shows)


class Schema(unittest.TestCase):
    def setUp(self):
        self.week = starter()
        self.start = at(NY, 2025, 1, 15, 5)                      # a Wednesday, 05:00 local: the end of Overnight Drift, then Morning Rise
        self.items = self.week.plan(self.start, self.start + 4 * 3600, 99)

    def test_every_item_has_exactly_the_documented_fields(self):
        self.assertGreater(len(self.items), 40)
        for item in self.items:
            self.assertIsInstance(item, PlanItem)
            data = item.to_dict(NY)
            self.assertEqual(set(data), ITEM_KEYS | {'start_iso', 'slot_start_iso'})
            self.assertEqual(set(item.to_dict()), ITEM_KEYS)
            self.assertRegex(data['id'], r'^[0-9a-f]{32}$')
            self.assertRegex(data['signature'], r'^[0-9a-f]{16}$')
            self.assertIn(data['role'], ROLES)
            self.assertEqual(data['kind'], ROLE_KIND[data['role']])
            self.assertIs(type(data['est_seconds']), int)
            self.assertGreater(data['est_seconds'], 0)
            self.assertIsInstance(data['start'], float)
            self.assertIsInstance(data['slot_start'], float)
            self.assertIs(type(data['position']), int)
            self.assertIs(type(data['ordinal']), int)
            self.assertIs(type(data['fallback']), bool)
            self.assertRegex(data['start_iso'], r'^2025-01-15T\d\d:\d\d:\d\d-05:00$')
        json.dumps([i.to_dict(NY) for i in self.items])

    def test_each_item_has_a_label_for_a_coming_up_list(self):
        labels = {i.role: i.to_dict()['label'] for i in self.items}
        self.assertEqual(labels, {'id': 'Station ID', 'song': 'Song', 'host_break': 'Host break', 'spot': 'Spot', 'update': 'Update'})

    def test_roles_map_to_the_shared_vocabulary(self):
        self.assertEqual(ROLES, ('song', 'host_break', 'id', 'spot', 'update'))
        self.assertEqual(ROLE_KIND, {'song': 'music', 'host_break': 'speech', 'id': 'speech', 'update': 'speech', 'spot': 'ad'})
        self.assertEqual({i.role for i in self.items}, set(ROLES))

    def test_hosts_voice_speech_and_songs_have_no_host(self):
        for item in self.items:
            if item.role == 'song':
                self.assertIsNone(item.host_id)
            elif item.show_id == 'morning':
                self.assertEqual(item.host_id, 'dex')
            elif item.show_id == 'overnight':
                self.assertIsNone(item.host_id)

    def test_constraints_for_each_role(self):
        by_role = {}
        for item in self.items:
            by_role.setdefault((item.show_id, item.role), item)
        song = by_role[('morning', 'song')].constraints
        self.assertEqual(song, {'palettes': ['bossa-courtyard'], 'tags': [], 'energy': 70, 'adventurousness': 35, 'vocals': 40})
        host = by_role[('morning', 'host_break')]
        self.assertEqual(host.constraints, {'talk': 'often', 'max_seconds': 40, 'optional': True})
        self.assertEqual(by_role[('morning', 'id')].constraints, {'max_seconds': 10, 'optional': False})
        self.assertEqual(by_role[('morning', 'spot')].constraints, {'max_seconds': 60, 'optional': True})
        self.assertEqual(by_role[('morning', 'update')].constraints,
                         {'researcher_id': None, 'max_seconds': 90, 'optional': True, 'if_nothing_new': 'play_music'})
        for item in self.items:
            if item.role != 'song':
                self.assertEqual(item.constraints['max_seconds'], item.est_seconds)

    def test_a_chosen_researcher_travels_with_the_update(self):
        week = Week(NY, [show('m', updates_allowed=True, update_researcher_id='astronomy')])
        items = week.plan(at(NY, 2025, 1, 15, 6), at(NY, 2025, 1, 15, 7), 1)
        (update,) = [i for i in items if i.role == 'update']
        self.assertEqual(update.constraints['researcher_id'], 'astronomy')

    def test_items_are_in_time_order_and_each_block_adds_up(self):
        previous = self.start - 1
        for item in self.items:
            self.assertGreaterEqual(item.start, previous)
            self.assertGreaterEqual(item.start, self.start)
            self.assertLess(item.start, self.start + 4 * 3600)
            previous = item.start
        blocks = {}
        for item in self.items:
            blocks.setdefault(item.slot_start, []).append(item)
        self.assertEqual([iso_utc(s) for s in blocks], ['2025-01-15 10:00', '2025-01-15 11:00', '2025-01-15 12:00', '2025-01-15 13:00'])
        for slot_start, group in blocks.items():
            self.assertEqual(sum(i.est_seconds for i in group), 3600)
            cursor = slot_start
            for place, item in enumerate(group):
                self.assertEqual((item.start, item.position, item.slot_seconds), (cursor, place, 3600))
                cursor += item.est_seconds
            self.assertEqual(group[0].role, 'id')
        self.assertEqual({i.show_id for i in self.items if i.slot_start < at(NY, 2025, 1, 15, 6)}, {'overnight'})
        self.assertEqual({i.show_id for i in self.items if i.slot_start >= at(NY, 2025, 1, 15, 6)}, {'morning'})

    def test_the_first_item_of_a_window_that_opens_mid_block_is_the_next_one(self):
        items = self.week.plan(self.start + 100, self.start + 1100, 99)
        self.assertTrue(items)
        self.assertTrue(all(self.start + 100 <= i.start < self.start + 1100 for i in items))
        self.assertEqual(items[0].slot_start, float(self.start))              # it belongs to the block that began earlier
        self.assertGreater(items[0].position, 0)

    def test_signatures_follow_the_shows_rules_but_ids_follow_the_slot(self):
        other = Week(NY, [s.replace(host_id='nova') if s.id == 'morning' else s for s in self.week.shows])
        changed = other.plan(self.start, self.start + 4 * 3600, 99)
        self.assertEqual([i.id for i in changed], [i.id for i in self.items])
        for old, new in zip(self.items, changed):
            if old.show_id == 'morning' and old.role != 'song':
                self.assertNotEqual(old.signature, new.signature)
                self.assertEqual(new.host_id, 'nova')
            else:
                self.assertEqual(old.signature, new.signature)


class Determinism(unittest.TestCase):
    def setUp(self):
        self.start = at(NY, 2025, 1, 15, 5)

    def test_the_same_seed_gives_the_same_plan(self):
        first = starter().plan(self.start, self.start + 48 * 3600, 7)
        second = starter().plan(self.start, self.start + 48 * 3600, 7)
        self.assertEqual(first, second)
        self.assertEqual(digest(first), digest(second))

    def test_other_seeds_give_other_plans(self):
        a = starter().plan(self.start, self.start + 6 * 3600, 1)
        b = starter().plan(self.start, self.start + 6 * 3600, 2)
        self.assertEqual(len({i.id for i in a} & {i.id for i in b}), 0)
        self.assertNotEqual([(i.role, i.est_seconds) for i in a], [(i.role, i.est_seconds) for i in b])

    def test_the_plan_never_reads_the_clock_or_randomness(self):
        week = starter()
        expected = week.plan(self.start, self.start + 24 * 3600, 5)
        boom = AssertionError('planning must not depend on when it is called')
        with mock.patch('time.time', side_effect=boom), mock.patch('time.monotonic', side_effect=boom), \
                mock.patch('time.perf_counter', side_effect=boom), mock.patch('random.random', side_effect=boom), \
                mock.patch('os.urandom', side_effect=boom):
            self.assertEqual(week.plan(self.start, self.start + 24 * 3600, 5), expected)
        for fake_now in (0, 1.0e9, 4.0e9):
            with mock.patch('time.time', return_value=fake_now):
                self.assertEqual(week.plan(self.start, self.start + 24 * 3600, 5), expected)

    def test_a_fresh_process_with_another_hash_seed_gives_byte_identical_output(self):
        code = ("import json, sys\nsys.path.insert(0, %r)\nfrom myradio.shows import Week, WEEK_TEMPLATES\n"
                "week = Week('America/New_York', WEEK_TEMPLATES['starter'].build())\n"
                "items = week.plan(1730000000, 1730000000 + 36 * 3600, 42)\n"
                "print(json.dumps([i.to_dict('America/New_York') for i in items], sort_keys=True))\n") % str(ROOT)
        base = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL') if k in os.environ}
        outputs = []
        for hash_seed in ('0', '1', '12345'):
            done = subprocess.run([sys.executable, '-c', code], env=dict(base, PYTHONHASHSEED=hash_seed), cwd=str(ROOT),
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr[-500:])
            outputs.append(done.stdout.strip())
        self.assertEqual(len(set(outputs)), 1)
        here = json.dumps([i.to_dict(NY) for i in starter().plan(1730000000, 1730000000 + 36 * 3600, 42)], sort_keys=True)
        self.assertEqual(outputs[0], here)

    def test_overlapping_windows_describe_the_same_items_with_the_same_ids(self):
        week = starter()
        rng = random.Random(3)
        low = self.start
        whole = week.plan(low, low + 30 * 3600, 11)
        for _ in range(25):
            a = low + rng.randrange(0, 10 * 3600)
            b = a + rng.randrange(1, 12 * 3600)
            c = b + rng.randrange(1, 8 * 3600)
            self.assertEqual(week.plan(a, b, 11) + week.plan(b, c, 11), week.plan(a, c, 11), (a, b, c))
            self.assertEqual(week.plan(a, c, 11), [i for i in whole if a <= i.start < c] if c <= low + 30 * 3600 else week.plan(a, c, 11))

    def test_windows_that_start_at_odd_moments_still_agree_on_ids(self):
        week = starter()
        reference = {i.id: i for i in week.plan(self.start, self.start + 12 * 3600, 21)}
        for offset in (0, 1, 59, 600, 3599, 3600, 3601, 7777):
            for item in week.plan(self.start + offset, self.start + 12 * 3600 - 5, 21):
                self.assertEqual(item, reference[item.id])

    def test_editing_one_show_leaves_every_other_slot_untouched(self):
        week = starter()
        edited = Week(NY, [s.replace(talk='rare', host_id='nova') if s.id == 'morning' else s for s in week.shows])
        low = at(NY, 2025, 1, 15, 0)
        before, after = week.plan(low, low + 24 * 3600, 4), edited.plan(low, low + 24 * 3600, 4)
        outside = lambda items: [i for i in items if i.show_id != 'morning']
        self.assertEqual(outside(before), outside(after))
        morning = lambda items: [i for i in items if i.show_id == 'morning']
        self.assertNotEqual(morning(before), morning(after))
        self.assertEqual([i.id for i in before if i.show_id == 'morning' and i.role == 'id'],
                         [i.id for i in after if i.show_id == 'morning' and i.role == 'id'])

    def test_changing_the_zone_changes_when_things_happen_but_not_how_an_hour_is_built(self):
        a = Week('UTC', WEEK_TEMPLATES['starter'].build()).plan(utc_ts(2025, 1, 15, 6), utc_ts(2025, 1, 15, 7), 5)
        self.assertEqual({i.show_id for i in a}, {'morning'})

    def test_results_are_pinned_so_they_cannot_drift_between_versions_or_machines(self):
        items = starter().plan(at(NY, 2025, 1, 15, 5), at(NY, 2025, 1, 15, 5) + 36 * 3600, 42)
        self.assertEqual(len(items), 656)
        first = items[0]
        self.assertEqual((iso_utc(first.start), first.role, first.show_id, first.est_seconds, first.id, first.signature),
                         ('2025-01-15 10:00', 'id', 'overnight', 10, '53fbd52599965695a5af5633a8c5890f', 'c9af1a55893490b8'))
        self.assertEqual([(i.role, i.est_seconds) for i in items[1:5]], [('song', 251), ('song', 240), ('song', 259), ('song', 215)])
        self.assertEqual(digest(items), 'd8856b0eafee498194212d1f0243a27fda0c5076cf4c6752fd52e0ccda0ccfb4')


class Bounds(unittest.TestCase):
    def setUp(self):
        self.week = starter()
        self.start = at(NY, 2025, 1, 15, 5)

    def test_a_week_is_the_most_in_one_call(self):
        started = time.monotonic()
        items = self.week.plan(self.start, self.start + MAX_PLAN_SECONDS, 3)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(MAX_PLAN_SECONDS, 168 * 3600)
        self.assertTrue(2500 < len(items) < 6000, len(items))
        with self.assertRaisesRegex(ShowError, 'at most a week'):
            self.week.plan(self.start, self.start + MAX_PLAN_SECONDS + 1, 3)

    def test_bad_windows_are_refused(self):
        for low, high in ((100, 100), (200, 100), (-5, 100), (0, 4102444801), (float('nan'), 100), (100, float('nan')), (100, float('inf')),
                          (True, 100), ('1', 100), (None, 100), (100, None), (1e300, 1e301), ([], 100)):
            with self.subTest(low=low, high=high), self.assertRaises(ShowError):
                self.week.plan(low, high, 1)

    def test_bad_seeds_are_refused(self):
        for seed in (-1, 2 ** 63, 1.5, True, '1', None, [], 1e30):
            with self.subTest(seed=seed), self.assertRaisesRegex(ShowError, 'seed'):
                self.week.plan(self.start, self.start + 3600, seed)
        self.assertTrue(self.week.plan(self.start, self.start + 3600, 2 ** 63 - 1))
        self.assertTrue(self.week.plan(self.start, self.start + 3600, 0))

    def test_planning_near_the_edges_of_time(self):
        self.assertTrue(self.week.plan(0, 3600, 1))
        late = 4102444800 - 7200
        self.assertTrue(self.week.plan(late, late + 3600, 1))


class FillAndFallback(unittest.TestCase):
    def test_unscheduled_time_plays_the_default_show(self):
        week = Week('UTC', [show('m', start='06:00', end='10:00')])
        items = week.plan(utc_ts(2025, 1, 15, 9), utc_ts(2025, 1, 15, 12), 5)
        by_show = {}
        for item in items:
            by_show.setdefault((item.show_id, item.fallback), []).append(item)
        self.assertEqual(set(by_show), {('m', False), ('default', True)})
        self.assertEqual({i.slot_start for i in by_show[('default', True)]}, {float(utc_ts(2025, 1, 15, 10)), float(utc_ts(2025, 1, 15, 11))})

    def test_a_week_with_no_shows_still_has_a_plan(self):
        items = Week('UTC').plan(utc_ts(2025, 1, 15), utc_ts(2025, 1, 15, 3), 5)
        self.assertEqual({(i.show_id, i.fallback) for i in items}, {('default', True)})
        self.assertEqual(len({i.slot_start for i in items}), 3)

    def test_a_short_gap_gets_one_filler_song(self):
        week = Week('UTC', [show('a', start='06:00', end='10:00'), show('b', start='10:02', end='12:00')])
        items = week.plan(utc_ts(2025, 1, 15, 10), utc_ts(2025, 1, 15, 10, 2), 5)
        self.assertEqual([(i.role, i.est_seconds, i.show_id, i.fallback, i.slot_seconds) for i in items], [('song', 120, 'default', True, 120)])

    def test_a_show_that_starts_part_way_through_an_hour_has_a_shorter_first_block(self):
        week = Week('UTC', [show('half', start='21:30', end='23:00', host_id='dex', spots_allowed=True)])
        items = week.plan(utc_ts(2025, 1, 15, 21, 30), utc_ts(2025, 1, 15, 23), 5)
        blocks = {}
        for item in items:
            blocks.setdefault(item.slot_start, []).append(item)
        self.assertEqual([(iso_utc(s), g[0].slot_seconds, sum(i.est_seconds for i in g)) for s, g in blocks.items()],
                         [('2025-01-15 21:30', 1800, 1800), ('2025-01-15 22:00', 3600, 3600)])
        for group in blocks.values():
            self.assertEqual(group[0].role, 'id')

    def test_damaged_overlapping_data_still_plans_deterministically(self):
        week = Week('UTC', [show('a', start='06:00', end='10:00'), show('b', start='08:00', end='12:00'), show('c', start='08:00', end='12:00')])
        first = week.plan(utc_ts(2025, 1, 15, 5), utc_ts(2025, 1, 15, 13), 8)
        self.assertEqual(first, week.plan(utc_ts(2025, 1, 15, 5), utc_ts(2025, 1, 15, 13), 8))
        self.assertEqual([(iso_utc(g), s) for g, s in sorted({(i.slot_start, i.show_id) for i in first})],
                         [('2025-01-15 05:00', 'default'), ('2025-01-15 06:00', 'a'), ('2025-01-15 07:00', 'a'), ('2025-01-15 08:00', 'b'),
                          ('2025-01-15 09:00', 'b'), ('2025-01-15 10:00', 'b'), ('2025-01-15 11:00', 'b'), ('2025-01-15 12:00', 'default')])


class Properties(unittest.TestCase):
    def test_random_weeks_zones_and_windows(self):
        rng = random.Random(2024)
        zones = ('UTC', NY, 'Europe/London', 'Asia/Kolkata', 'Australia/Lord_Howe', 'Pacific/Auckland')
        for round_ in range(30):
            zone = rng.choice(zones)
            week = random_week(rng, zone)
            low = at(zone, 2024, rng.choice((3, 4, 9, 10, 11)), rng.randrange(1, 28), rng.randrange(24), rng.choice((0, 7, 30)))
            high = low + rng.randrange(3600, 60 * 3600)
            seed = rng.randrange(2 ** 63)
            items = week.plan(low, high, seed)
            self.assertEqual(items, week.plan(low, high, seed))
            self.assertEqual(len({i.id for i in items}), len(items))
            starts = [i.start for i in items]
            self.assertEqual(starts, sorted(starts))
            self.assertTrue(all(low <= s < high for s in starts))
            blocks = {}
            for item in items:
                blocks.setdefault((item.slot_start, item.slot_seconds), []).append(item)
            ordered = sorted(blocks)
            for (s1, n1), (s2, _) in zip(ordered, ordered[1:]):
                self.assertLessEqual(s1 + n1, s2, 'blocks never overlap')
                self.assertLess(s2 - (s1 + n1), 1, 'blocks tile time with no hole')
            for (slot_start, seconds), group in blocks.items():
                complete = slot_start >= low and slot_start + seconds <= high
                if complete and seconds >= 60:
                    self.assertEqual(sum(i.est_seconds for i in group), seconds, (zone, iso_utc(slot_start)))
            cut = low + rng.randrange(1, high - low)
            self.assertEqual(week.plan(low, cut, seed) + week.plan(cut, high, seed), items)


if __name__ == '__main__':
    unittest.main()
