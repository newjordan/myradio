"""Clock templates and hour_plan: the example hour, totals, shuffling and scaling."""
import hashlib
import itertools
import json
import math
import random
import unittest
from unittest import mock

from myradio.shows import CLOCK_TEMPLATES, default_show, hour_plan, summarize
from myradio.shows.clocks import (HOUR, MAX_SEED, SHORT_BLOCK, SONG_FLOOR, TINY_BLOCK, TALK_LEVELS, _Stream, clock_notes,
                                  seed_key, template_for)

HOURS = 3600
TEMPLATE_NAMES = ('classic', 'music-heavy', 'talky')


def a_show(talk='some', host='dex', spots=False, updates=False, clock='classic'):
    return default_show(talk=talk, host_id=host, spots_allowed=spots, updates_allowed=updates, clock=clock)


def preview_hour_plan(talk, host, spots, news):
    """A line-for-line port of hourPlan() from the design preview (panels.js), the reference for the recipe."""
    count, each = {'rare': (1, 20), 'some': (2, 30), 'often': (3, 40)}[talk]
    extra = [('host_break', each)] * (count if host else 0)
    if spots:
        extra.append(('spot', 60))
    if news:
        extra.append(('update', 90))
    used = 10 + sum(s for _, s in extra)
    songs = max(1, math.floor((3600 - used) / 225))
    length = (3600 - used) / songs
    gap = max(1, math.floor(songs / (len(extra) + 1)))
    sequence, made = [('id', 10)], 0
    for i in range(len(extra) + 1):
        take = songs - made if i == len(extra) else min(gap, songs - made)
        for _ in range(take):
            sequence.append(('song', length))
            made += 1
        if i < len(extra):
            sequence.append(extra[i])
    return sequence


def combos():
    return itertools.product(TEMPLATE_NAMES, TALK_LEVELS, (None, 'dex'), (False, True), (False, True))


def digest(items):
    return hashlib.sha256(json.dumps([(i.role, i.est_seconds, i.ordinal) for i in items]).encode()).hexdigest()


class Recipe(unittest.TestCase):
    def test_the_recipe_matches_the_design_preview_for_every_combination(self):
        for talk, host, spots, news in itertools.product(TALK_LEVELS, (None, 'dex'), (False, True), (False, True)):
            with self.subTest(talk=talk, host=host, spots=spots, news=news):
                mine = [(i.role, i.est_seconds) for i in hour_plan(a_show(talk, host, spots, news))]
                theirs = preview_hour_plan(talk, host, spots, news)
                self.assertEqual([r for r, _ in mine], [r for r, _ in theirs])
                for (_, got), (role, want) in zip(mine, theirs):
                    if role == 'song':
                        self.assertLess(abs(got - want), 1.0)      # the preview uses fractions, this uses whole seconds
                    else:
                        self.assertEqual(got, want)
                self.assertEqual(sum(s for _, s in mine), 3600)

    def test_an_example_hour_of_a_busy_show(self):
        items = hour_plan(a_show('often', 'dex', True, True))
        self.assertEqual([i.role for i in items],
                         ['id'] + ['song'] * 2 + ['host_break'] + ['song'] * 2 + ['host_break'] + ['song'] * 2 + ['host_break']
                         + ['song'] * 2 + ['spot'] + ['song'] * 2 + ['update'] + ['song'] * 4)
        self.assertEqual([i.est_seconds for i in items if i.role != 'song'], [10, 40, 40, 40, 60, 90])
        songs = [i.est_seconds for i in items if i.role == 'song']
        self.assertEqual((len(songs), sum(songs), min(songs), max(songs)), (14, 3320, 237, 238))
        self.assertEqual([i.ordinal for i in items if i.role == 'host_break'], [0, 1, 2])

    def test_a_quiet_show_has_an_id_and_songs_only(self):
        items = hour_plan(a_show('rare', None))
        self.assertEqual([i.role for i in items], ['id'] + ['song'] * 15)
        self.assertEqual(sum(i.est_seconds for i in items), 3600)

    def test_no_host_means_no_talk_breaks_whatever_the_talk_level(self):
        for talk in TALK_LEVELS:
            roles = [i.role for i in hour_plan(a_show(talk, None, True, True))]
            self.assertNotIn('host_break', roles)
            self.assertEqual((roles.count('spot'), roles.count('update')), (1, 1))

    def test_the_legend_summary(self):
        rows = summarize(hour_plan(a_show('often', 'dex', True, True)))
        self.assertEqual([r['role'] for r in rows], ['id', 'song', 'host_break', 'spot', 'update'])
        self.assertEqual([r['count'] for r in rows], [1, 14, 3, 1, 1])
        self.assertEqual([r['label'] for r in rows], ['Station ID', 'Songs', 'Host breaks', 'Spots', 'Update'])
        self.assertEqual(sum(r['seconds'] for r in rows), 3600)
        self.assertEqual(rows[1]['length'], '55:20')

    def test_notes_explain_the_hour_in_plain_words(self):
        notes = clock_notes(a_show('often', None, False, True))
        self.assertTrue(any('no host' in n for n in notes))
        self.assertTrue(any('Spots are switched off' in n for n in notes))
        self.assertTrue(any('music keeps playing' in n for n in notes))
        self.assertEqual(clock_notes(a_show('some', 'dex', True, False)), [])


class Totals(unittest.TestCase):
    SEEDS = (None, 0, 1, 7, 99, 12345, 2 ** 63 - 1, 'a', 'seed text', '1|morning|1700000000')

    def test_every_template_talk_level_and_option_sums_to_the_hour(self):
        for template, talk, host, spots, updates in combos():
            show = a_show(talk, host, spots, updates, template)
            spec = CLOCK_TEMPLATES[template]
            for seed in self.SEEDS:
                with self.subTest(template=template, talk=talk, host=host, spots=spots, updates=updates, seed=seed):
                    items = hour_plan(show, seed)
                    self.assertEqual(sum(i.est_seconds for i in items), HOURS)
                    roles = [i.role for i in items]
                    self.assertEqual(roles[0], 'id')
                    self.assertEqual((roles.count('id'), roles.count('spot'), roles.count('update')),
                                     (1, int(spots), int(updates)))
                    self.assertEqual(roles.count('host_break'), spec.breaks[talk][0] if host else 0)
                    self.assertEqual(roles[-1], 'song')
                    self.assertTrue(all(type(i.est_seconds) is int and i.est_seconds > 0 for i in items))
                    songs = [i.est_seconds for i in items if i.role == 'song']
                    self.assertGreaterEqual(sum(songs) / len(songs), SONG_FLOOR)
                    for here, there in zip(roles, roles[1:]):
                        if here != 'song':
                            self.assertEqual(there, 'song', 'breaks must be separated by a song')
                    for role in set(roles):
                        self.assertEqual([i.ordinal for i in items if i.role == role], list(range(roles.count(role))))
                    fixed = {'id': spec.id_seconds, 'spot': spec.spot_seconds, 'update': spec.update_seconds,
                             'host_break': spec.breaks[talk][1]}
                    for item in items:
                        if item.role != 'song':
                            self.assertEqual(item.est_seconds, fixed[item.role])

    def test_the_templates_trade_music_for_talk_in_the_expected_direction(self):
        for talk, host, spots, updates in itertools.product(TALK_LEVELS, (None, 'dex'), (False, True), (False, True)):
            music = {}
            for template in TEMPLATE_NAMES:
                items = hour_plan(a_show(talk, host, spots, updates, template))
                music[template] = sum(i.est_seconds for i in items if i.role == 'song')
            with self.subTest(talk=talk, host=host, spots=spots, updates=updates):
                self.assertGreaterEqual(music['music-heavy'], music['classic'])
                self.assertGreaterEqual(music['classic'], music['talky'])
                if host or spots or updates:
                    self.assertGreater(music['music-heavy'], music['talky'])

    def test_the_template_argument_overrides_the_show_setting(self):
        show = a_show('some', 'dex', True, True, 'classic')
        self.assertEqual(hour_plan(show, None, template='talky'), hour_plan(a_show('some', 'dex', True, True, 'talky')))
        self.assertEqual(template_for('talky').name, 'talky')

    def test_templates_describe_themselves(self):
        for name, spec in CLOCK_TEMPLATES.items():
            data = spec.describe()
            json.dumps(data)
            self.assertEqual(data['name'], name)
            self.assertEqual(set(data['host_breaks']), set(TALK_LEVELS))
        self.assertEqual(list(CLOCK_TEMPLATES), list(TEMPLATE_NAMES))


class Shuffling(unittest.TestCase):
    def test_the_same_seed_always_gives_the_same_hour(self):
        show = a_show('often', 'dex', True, True)
        for seed in (0, 1, 'x', 2 ** 40):
            self.assertEqual(hour_plan(show, seed), hour_plan(show, seed))
            self.assertEqual(hour_plan(show, seed), hour_plan(a_show('often', 'dex', True, True), seed))

    def test_the_seed_changes_the_arrangement_but_not_the_ingredients(self):
        show = a_show('often', 'dex', True, True)
        recipe = hour_plan(show)
        arrangements = set()
        for seed in range(40):
            items = hour_plan(show, seed)
            arrangements.add(tuple((i.role, i.est_seconds) for i in items))
            self.assertEqual(sorted(i.role for i in items if i.role != 'song'),
                             sorted(i.role for i in recipe if i.role != 'song'))
            self.assertEqual(len([i for i in items if i.role == 'song']), 14)
        self.assertGreater(len(arrangements), 35, 'hours should not repeat')
        self.assertNotIn(tuple((i.role, i.est_seconds) for i in recipe), arrangements)

    def test_the_seed_is_text_so_an_integer_and_its_digits_are_the_same_seed(self):
        show = a_show('some', 'dex', True, False)
        self.assertEqual(hour_plan(show, 5), hour_plan(show, '5'))
        self.assertNotEqual(hour_plan(show, 5), hour_plan(show, 6))

    def test_results_are_pinned_so_they_cannot_drift_between_versions_or_machines(self):
        show = a_show('often', 'dex', True, True)
        items = hour_plan(show, 'golden-seed')
        self.assertEqual([(i.role, i.est_seconds) for i in items][:8],
                         [('id', 10), ('song', 271), ('song', 195), ('song', 242), ('update', 90), ('song', 247),
                          ('host_break', 40), ('song', 226)])
        self.assertEqual(digest(items), '50c0bb339c1f7bc4f40744753e89006ec389d6031ba88607d454c09f3ea889a7')
        self.assertEqual(digest(hour_plan(show, 12345, template='talky')),
                         '413204f684184a0deb91452e37fe6ac8457c999131bfdbe78f6521ae4c537b13')
        stream = _Stream('abc')
        self.assertEqual([stream.below(1000) for _ in range(6)], [947, 607, 747, 5, 341, 964])

    def test_nothing_in_the_hour_plan_reads_the_clock_or_python_randomness(self):
        show = a_show('often', 'dex', True, True)
        expected = hour_plan(show, 3)
        boom = AssertionError('hour_plan must be a pure function')
        with mock.patch('time.time', side_effect=boom), mock.patch('time.monotonic', side_effect=boom), \
                mock.patch('random.random', side_effect=boom), mock.patch('os.urandom', side_effect=boom):
            self.assertEqual(hour_plan(show, 3), expected)
            hour_plan(show)

    def test_the_stream_is_roughly_uniform(self):
        stream = _Stream('uniform')
        counts = [0] * 10
        for _ in range(5000):
            counts[stream.below(10)] += 1
        self.assertTrue(all(400 < c < 600 for c in counts), counts)


class Scaling(unittest.TestCase):
    """Blocks other than a whole hour (a show that starts or ends part-way through an hour)."""

    def test_every_block_length_is_filled_exactly(self):
        for seconds in list(range(1, 400)) + list(range(400, 4 * HOUR + 1, 61)) + [900, 1800, 2700, 3599, 3600, 3601, 4 * HOUR]:
            for template, talk, host, spots, updates in itertools.islice(combos(), 0, None, 7):
                show = a_show(talk, host, spots, updates, template)
                for seed in (None, 11):
                    items = hour_plan(show, seed, seconds=seconds)
                    with self.subTest(seconds=seconds, template=template, talk=talk, host=host, seed=seed):
                        if seconds < TINY_BLOCK:
                            self.assertEqual(items, [])
                        elif seconds < SHORT_BLOCK:
                            self.assertEqual([(i.role, i.est_seconds) for i in items], [('song', seconds)])
                        else:
                            self.assertEqual(sum(i.est_seconds for i in items), seconds)
                            self.assertEqual(items[0].role, 'id')
                            self.assertEqual(items[-1].role, 'song')
                            roles = [i.role for i in items]
                            for here, there in zip(roles, roles[1:]):
                                if here != 'song':
                                    self.assertEqual(there, 'song')

    def test_a_half_hour_gets_half_the_breaks_rounded_up(self):
        items = hour_plan(a_show('often', 'dex', True, True), None, seconds=1800)
        roles = [i.role for i in items]
        self.assertEqual((roles.count('host_break'), roles.count('spot'), roles.count('update'), roles.count('id')), (2, 1, 1, 1))
        quarter = [i.role for i in hour_plan(a_show('often', 'dex', True, True), None, seconds=900)]
        self.assertEqual((quarter.count('host_break'), quarter.count('spot'), quarter.count('update')), (1, 0, 0))

    def test_blocks_a_little_over_an_hour_keep_the_hours_ingredients(self):
        show = a_show('often', 'dex', True, True)
        base = [i.role for i in hour_plan(show)]
        for seconds in (3600, 3700, 3899):
            roles = [i.role for i in hour_plan(show, None, seconds=seconds)]
            self.assertEqual(sorted(set(roles)), sorted(set(base)))
            for role in ('id', 'host_break', 'spot', 'update'):
                self.assertEqual(roles.count(role), base.count(role), (seconds, role))

    def test_bad_arguments_are_refused(self):
        show = a_show()
        for seconds in (0, -5, 4 * HOUR + 1, True, 3600.0, '3600', None):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                hour_plan(show, seconds=seconds)
        for template in ('nope', '', ['classic'], 5):
            with self.subTest(template=template), self.assertRaises(ValueError):
                hour_plan(show, template=template)
        for seed in (True, 1.5, -1, MAX_SEED + 1, '', 'x' * 201, [], {}, b'x'):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                hour_plan(show, seed)
        self.assertEqual(seed_key(MAX_SEED), str(MAX_SEED))
        self.assertEqual(seed_key('x' * 200), 'x' * 200)


class PropertyChecks(unittest.TestCase):
    def test_random_shows_and_block_lengths(self):
        rng = random.Random(77)
        for _ in range(400):
            show = a_show(rng.choice(TALK_LEVELS), rng.choice((None, 'h')), rng.random() < .5, rng.random() < .5,
                          rng.choice(TEMPLATE_NAMES))
            seconds = rng.choice((HOURS, rng.randrange(60, 4 * HOUR)))
            seed = rng.choice((None, rng.randrange(2 ** 63)))
            items = hour_plan(show, seed, seconds=seconds)
            if seconds >= TINY_BLOCK:
                self.assertEqual(sum(i.est_seconds for i in items), seconds)
            self.assertEqual(items, hour_plan(show, seed, seconds=seconds))


if __name__ == '__main__':
    unittest.main()
