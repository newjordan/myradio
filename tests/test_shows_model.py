"""The show model: validation, normalization, labels and hostile input."""
import ast
import json
import random
import sys
import unittest
from pathlib import Path

from myradio.shows import DEFAULT_SHOW_ID, Show, ShowError, Style, default_show
from myradio.shows import labels
from myradio.shows.model import (AUTO_COLORS, MAX_NAME, clean_days, clean_name, parse_minute, slugify)

try:
    from test_shows_support import junk_show
except ImportError:  # run as tests.test_shows_model
    from tests.test_shows_support import junk_show


def parse(**fields):
    data = {'name': 'Morning Rise', 'start': '06:00', 'end': '10:00'}
    data.update(fields)
    return Show.parse(data)


class DefaultsAndShape(unittest.TestCase):
    def test_a_minimal_show_gets_sensible_defaults(self):
        show = parse(days='weekdays', start=6)
        self.assertEqual((show.id, show.name, show.days), ('morning-rise', 'Morning Rise', '1,2,3,4,5'))
        self.assertEqual((show.start_min, show.length_min, show.end_min), (360, 240, 600))
        self.assertEqual((show.host_id, show.talk, show.clock), (None, 'some', 'classic'))
        self.assertEqual((show.spots_allowed, show.updates_allowed, show.update_researcher_id), (False, False, None))
        self.assertEqual(show.color, AUTO_COLORS[0])
        self.assertFalse(show.crosses_midnight)
        self.assertEqual(show.when(), 'Weekdays, 6 am–10 am')
        self.assertEqual(show.style, Style((), (), 50, 40, 30))

    def test_to_dict_is_json_and_round_trips_through_parse(self):
        show = parse(host_id='dex', talk='often', spots_allowed=True, updates_allowed=True,
                     update_researcher_id='astronomy', style={'palettes': ['bossa-courtyard'], 'tags': ['sunlit'],
                                                              'energy': 70, 'adventurousness': 20, 'vocals': 40})
        data = json.loads(json.dumps(show.to_dict()))
        self.assertEqual(Show.parse(data), show)
        self.assertEqual(Show.parse(data, show), show)
        self.assertEqual(data['start'], '06:00')
        self.assertEqual(data['end'], '10:00')
        self.assertEqual((data['start_minute'], data['duration_minutes'], data['crosses_midnight']), (360, 240, False))

    def test_random_shows_round_trip(self):
        rng = random.Random(20240601)
        for n in range(150):
            start = rng.randrange(0, 1440)
            length = rng.randrange(15, 1441) if start == 0 else rng.randrange(15, 1440)
            end_min = (start + length) % 1440 or 1440
            days = sorted(rng.sample(range(1, 8), rng.randrange(1, 8)))
            hosted = rng.random() < 0.5
            updates = rng.random() < 0.5
            show = Show.parse({
                'id': f'show-{n}', 'name': f'Show {n}', 'days': days,
                'start': f'{start // 60}:{start % 60:02d}', 'end': f'{end_min // 60:02d}:{end_min % 60:02d}',
                'host_id': 'host-a' if hosted else None, 'talk': rng.choice(['rare', 'some', 'often']),
                'spots_allowed': rng.random() < 0.5, 'updates_allowed': updates,
                'update_researcher_id': 'res-1' if updates and rng.random() < 0.5 else None,
                'clock': rng.choice(['classic', 'music-heavy', 'talky']),
                'style': {'energy': rng.randrange(101), 'vocals': rng.randrange(101), 'adventurousness': rng.randrange(101)}})
            self.assertEqual(show.length_min, length, (start, end_min))
            self.assertEqual(Show.parse(show.to_dict(), show), show)
            self.assertEqual(Show.parse(json.loads(json.dumps(show.to_dict()))), show)

    def test_read_only_fields_are_ignored_but_unknown_fields_are_refused(self):
        show = parse()
        data = dict(show.to_dict(), when='nonsense', duration_minutes=5, start_minute=99, crosses_midnight=True, is_default=True)
        self.assertEqual(Show.parse(data, show), show)
        for key in ('shade', 'host', 'Name', 'days ', 7):
            with self.subTest(key=key), self.assertRaisesRegex(ShowError, 'Unknown show setting'):
                Show.parse({**show.to_dict(), key: 'x'}, show)

    def test_fields_left_out_keep_the_old_value_and_none_clears(self):
        show = parse(host_id='dex', talk='often', style={'energy': 80, 'palettes': ['neo-soul']})
        changed = Show.parse({'talk': 'rare'}, show)
        self.assertEqual((changed.host_id, changed.talk, changed.style), ('dex', 'rare', show.style))
        self.assertIsNone(Show.parse({'host_id': None}, show).host_id)
        self.assertIsNone(Show.parse({'host_id': ''}, show).host_id)
        merged = Show.parse({'style': {'vocals': 5}}, show)
        self.assertEqual((merged.style.energy, merged.style.palettes, merged.style.vocals), (80, ('neo-soul',), 5))
        self.assertEqual(Show.parse({'style': {'palettes': []}}, show).style.palettes, ())

    def test_ids_are_slugs_generated_from_names_and_kept_unique(self):
        self.assertEqual(slugify('Morning Rise'), 'morning-rise')
        self.assertEqual(slugify('  Café 9–5 & Co.  '), 'cafe-9-5-co')
        self.assertEqual(slugify('深夜'), 'show')
        self.assertEqual(slugify('Default'), 'show')
        self.assertEqual(slugify('A' * 100), 'a' * 40)
        self.assertEqual(slugify('Rise', {'rise', 'rise-2'}), 'rise-3')
        self.assertEqual(Show.parse({'name': 'Rise', 'start': 1, 'end': 3}, taken_ids={'rise'}).id, 'rise-2')
        self.assertEqual(parse(id='  Morning-Rise ').id, 'morning-rise')
        self.assertEqual(parse(id='a\n').id, 'a')                      # surrounding whitespace is ignored
        for bad in ('', ' ', '-x', 'a b', 'a_b', 'x' * 64, 'café', 'a/b', '../x', 'a\nb', None, 5, ['x']):
            with self.subTest(bad=bad), self.assertRaises(ShowError):
                parse(id=bad)
        self.assertEqual(parse(id='x' * 63).id, 'x' * 63)

    def test_the_id_default_belongs_to_the_default_show(self):
        with self.assertRaisesRegex(ShowError, 'kept for the default show'):
            parse(id='default')
        with self.assertRaisesRegex(ShowError, 'cannot be changed'):
            Show.parse({'id': 'other'}, parse())

    def test_new_shows_need_a_name_and_both_times(self):
        for data in ({}, {'name': 'X'}, {'name': 'X', 'start': '06:00'}, {'name': 'X', 'end': '07:00'},
                     {'start': '06:00', 'end': '07:00'}):
            with self.subTest(data=data), self.assertRaises(ShowError):
                Show.parse(data)

    def test_new_shows_take_unused_colours_in_order(self):
        taken = []
        for _ in range(len(AUTO_COLORS) + 2):
            color = Show.parse({'name': 'x', 'start': 1, 'end': 2}, taken_colors=taken).color
            taken.append(color)
        self.assertEqual(taken[:len(AUTO_COLORS)], list(AUTO_COLORS))
        self.assertIn(taken[-1], AUTO_COLORS)


class Names(unittest.TestCase):
    def test_names_are_tidied(self):
        self.assertEqual(clean_name('  Morning \n\t Rise  '), 'Morning Rise')
        self.assertEqual(clean_name('Café 9–5 & Co.'), 'Café 9–5 & Co.')
        self.assertEqual(clean_name('深夜ラジオ'), '深夜ラジオ')
        self.assertEqual(clean_name("Mike's Show"), "Mike's Show")
        self.assertEqual(clean_name('x' * MAX_NAME), 'x' * MAX_NAME)

    def test_names_that_could_confuse_people_or_speech_tags_are_refused(self):
        for bad in ('', '   ', '\n', 'x' * (MAX_NAME + 1), 'Show\x00', 'Show\x1b[0m', '<b>Hi</b>', 'Hi [laughs]', '<|speaker:1|>',
                    'a|b', 'a{b}', 'a\\b', 'Rise‮', 'zero​width', 'lone\ud800surrogate', None, 5, ['x'], b'x', {}):
            with self.subTest(name=repr(bad)[:30]), self.assertRaises(ShowError):
                clean_name(bad)

    def test_a_huge_name_is_refused_quickly(self):
        import time
        started = time.monotonic()
        with self.assertRaises(ShowError):
            clean_name('x' * 5_000_000)
        with self.assertRaises(ShowError):
            clean_name(' '.join(['word'] * 200_000))
        self.assertLess(time.monotonic() - started, 1.0)


class Days(unittest.TestCase):
    def test_days_are_normalised(self):
        for given, expected in (('1,2,3', '1,2,3'), (' 3 , 1 ,2', '1,2,3'), ('5,5,1', '1,5'), ([7, 1], '1,7'), ((2,), '2'),
                                (['2', ' 1 '], '1,2'), ('weekdays', '1,2,3,4,5'), ('Weekends', '6,7'), ('daily', '1,2,3,4,5,6,7'),
                                ('every day', '1,2,3,4,5,6,7'), ('everyday', '1,2,3,4,5,6,7')):
            with self.subTest(given=given):
                self.assertEqual(clean_days(given), expected)

    def test_bad_days_explain_what_to_send(self):
        for bad in ('', ' ', '0', '8', '1,,2', '1,', ',1', 'mon', '1-5', '1 2', '-1', '1.5', 'x' * 100_000, [], [0], [8], [1.5],
                    [True], ['x'], ['0'], ['12'], [None], None, 3, 1.0, {}, set([1]), list(range(1, 8)) * 3):
            with self.subTest(bad=repr(bad)[:30]):
                with self.assertRaisesRegex(ShowError, 'Monday is 1'):
                    clean_days(bad)


class Times(unittest.TestCase):
    def test_accepted_times(self):
        for given, kind, minutes in (('06:00', 'start', 360), ('6:30', 'start', 390), (6, 'start', 360), ('00:00', 'start', 0),
                                     ('23:59', 'start', 1439), (23, 'start', 1380), ('10:00', 'end', 600), (24, 'end', 1440),
                                     ('24:00', 'end', 1440), ('0:15', 'end', 15), (1, 'end', 60), (0, 'end', 0), (' 7:05 ', 'end', 425)):
            with self.subTest(given=given, kind=kind):
                self.assertEqual(parse_minute(given, kind), minutes)

    def test_refused_times(self):
        for kind, bad in [('start', x) for x in ('24:00', 24, '25:00', '6:60', '06:5', '6', '06:00:00', '6am', '', ' ', '-1:00', -1, 6.5,
                                                 True, None, [], {}, 'x' * 100_000, '٠٦:٠٠')] + \
                         [('end', x) for x in ('24:01', 25, '6:60', 'noon', 7.5, False, None, [], '24:30')]:
            with self.subTest(kind=kind, bad=repr(bad)[:20]), self.assertRaises(ShowError):
                parse_minute(bad, kind)

    def test_end_before_start_means_the_show_runs_past_midnight(self):
        night = parse(start='22:00', end='02:00')
        self.assertEqual((night.length_min, night.crosses_midnight, night.end_min), (240, True, 120))
        self.assertEqual(night.to_dict()['end'], '02:00')
        self.assertEqual(night.when(), 'Every day, 10 pm–2 am')
        until_midnight = parse(start='20:00', end='24:00')
        self.assertEqual((until_midnight.length_min, until_midnight.crosses_midnight), (240, False))
        self.assertEqual(until_midnight.to_dict()['end'], '24:00')
        self.assertEqual(parse(start='20:00', end='00:00').length_min, 240)   # 00:00 also means midnight when it is not the start
        self.assertEqual(parse(start=20, end=24).to_dict()['end'], '24:00')
        all_day = parse(start='00:00', end='24:00')
        self.assertEqual((all_day.length_min, all_day.crosses_midnight), (1440, False))
        long_night = parse(start='00:01', end='00:00')
        self.assertEqual(long_night.length_min, 1439)

    def test_zero_and_tiny_shows_are_refused_with_advice(self):
        for start, end in (('06:00', '06:00'), ('00:00', '00:00'), ('23:00', '23:00')):
            with self.subTest(start=start), self.assertRaisesRegex(ShowError, 'needs to end after it starts'):
                parse(start=start, end=end)
        for start, end in (('06:00', '06:14'), ('23:50', '24:00'), ('23:55', '00:05')):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(ShowError, 'at least 15 minutes'):
                parse(start=start, end=end)
        self.assertEqual(parse(start='06:00', end='06:15').length_min, 15)

    def test_changing_only_one_end_keeps_the_other_clock_time(self):
        show = parse(start='06:00', end='10:00')
        later = Show.parse({'start': '07:00'}, show)
        self.assertEqual((later.start_min, later.end_min, later.length_min), (420, 600, 180))
        longer = Show.parse({'end': '11:30'}, show)
        self.assertEqual((longer.start_min, longer.end_min, longer.length_min), (360, 690, 330))
        night = parse(start='22:00', end='02:00')
        self.assertEqual(Show.parse({'start': '23:00'}, night).length_min, 180)


class StyleAndRules(unittest.TestCase):
    def test_style_values(self):
        show = parse(style={'palettes': ['Warm-Brush-Jazz', 'warm-brush-jazz', 'neo-soul'], 'tags': ['late-night'],
                            'energy': 70.0, 'adventurousness': 0, 'vocals': 100})
        self.assertEqual(show.style.palettes, ('warm-brush-jazz', 'neo-soul'))
        self.assertEqual((show.style.energy, show.style.adventurousness, show.style.vocals), (70, 0, 100))
        self.assertIsInstance(show.style.energy, int)

    def test_bad_style_values(self):
        for style in ('jazz', [], 5, {'energy': -1}, {'energy': 101}, {'energy': 70.5}, {'energy': True}, {'energy': '70'},
                      {'energy': None}, {'energy': float('nan')}, {'energy': float('inf')}, {'vocals': 1e30},
                      {'palettes': 'jazz'}, {'palettes': [5]}, {'palettes': ['Bad Name']}, {'palettes': ['x'] * 33},
                      {'palettes': [['a']]}, {'tags': ['a/b']}, {'tags': ['']}, {'mood': 'calm'}, {'energy': 5, 'extra': 1}):
            with self.subTest(style=repr(style)[:40]), self.assertRaises(ShowError):
                parse(style=style)
        self.assertEqual(len(parse(style={'palettes': [f'p{i}' for i in range(32)]}).style.palettes), 32)

    def test_talk_clock_colour_and_booleans(self):
        self.assertEqual(parse(talk=' OFTEN ').talk, 'often')
        self.assertEqual(parse(clock='Music-Heavy').clock, 'music-heavy')
        self.assertEqual(parse(color='#ABCDEF').color, '#abcdef')
        for field, bad in (('talk', 'lots'), ('talk', None), ('talk', 1), ('clock', 'fast'), ('clock', None), ('color', 'red'),
                           ('color', '#abc'), ('color', '#12345g'), ('color', 5), ('spots_allowed', 1), ('spots_allowed', 'yes'),
                           ('updates_allowed', None), ('host_id', 'Not A Slug!'), ('host_id', 5), ('update_researcher_id', ['x'])):
            with self.subTest(field=field, bad=bad), self.assertRaises(ShowError):
                parse(**{field: bad})

    def test_hosts_and_researchers_are_opaque_slugs(self):
        self.assertEqual(parse(host_id='  DEX ').host_id, 'dex')
        self.assertIsNone(parse(host_id='   ').host_id)
        self.assertIsNone(parse(update_researcher_id='').update_researcher_id)

    def test_updates_and_researchers_stay_consistent(self):
        self.assertTrue(parse(update_researcher_id='astronomy').updates_allowed)          # choosing one switches updates on
        self.assertTrue(parse(updates_allowed=True).updates_allowed)
        with self.assertRaisesRegex(ShowError, 'switched off'):
            parse(updates_allowed=False, update_researcher_id='astronomy')
        base = parse(update_researcher_id='astronomy')
        off = Show.parse({'updates_allowed': False}, base)                                 # turning updates off drops the researcher
        self.assertEqual((off.updates_allowed, off.update_researcher_id), (False, None))
        self.assertEqual(Show.parse({'update_researcher_id': None}, base).updates_allowed, True)
        self.assertEqual(Show.parse({'talk': 'rare'}, base).update_researcher_id, 'astronomy')


class DefaultShow(unittest.TestCase):
    def test_the_default_show(self):
        default = default_show()
        self.assertEqual((default.id, default.is_default, default.name), (DEFAULT_SHOW_ID, True, 'Station Mix'))
        self.assertEqual((default.days, default.start_min, default.length_min), ('1,2,3,4,5,6,7', 0, 1440))
        self.assertEqual(default.to_dict()['end'], '24:00')
        self.assertEqual(default.when(), 'Whenever no other show is scheduled')

    def test_the_default_show_can_change_its_sound_but_not_its_schedule(self):
        default = default_show()
        changed = Show.parse({'id': 'default', 'name': 'Anytime Mix', 'host_id': 'dex', 'talk': 'often'}, default)
        self.assertEqual((changed.name, changed.host_id, changed.is_default), ('Anytime Mix', 'dex', True))
        self.assertEqual(Show.parse(default.to_dict(), default), default)                  # an echoed copy is accepted
        for field, value in (('days', '1,2'), ('start', '06:00'), ('end', '10:00'), ('start', 'junk')):
            with self.subTest(field=field), self.assertRaisesRegex(ShowError, 'no days, start or end'):
                Show.parse({field: value}, default)
        self.assertEqual(Show.parse({'days': 'daily', 'start': 0, 'end': 24}, default), default)


class DirectConstruction(unittest.TestCase):
    """Anyone may build a Show directly: it must refuse everything that is not canonical."""

    def good(self, **changes):
        return parse().replace(**changes)

    def test_non_canonical_or_wrong_typed_fields_are_refused_with_show_error(self):
        for field, bad in (('id', 'Bad Id'), ('id', 5), ('id', 'default'), ('name', ' padded '), ('name', 'a  b'), ('name', 5),
                           ('days', '3,1'), ('days', [1, 2]), ('days', ''), ('days', None), ('start_min', 1440), ('start_min', -1),
                           ('start_min', 1.5), ('start_min', True), ('start_min', '5'), ('length_min', 14), ('length_min', 1441),
                           ('length_min', None), ('style', None), ('style', {}), ('host_id', ''), ('host_id', 'A'), ('host_id', 4),
                           ('talk', 'Rare'), ('spots_allowed', 1), ('updates_allowed', 'no'), ('update_researcher_id', 'x y'),
                           ('color', '#ABCDEF'), ('color', 'blue'), ('clock', 'slow'), ('is_default', True), ('is_default', 1)):
            with self.subTest(field=field, bad=bad), self.assertRaises(ShowError):
                self.good(**{field: bad})

    def test_style_refuses_garbage_without_type_errors(self):
        for kwargs in ({'palettes': 5}, {'palettes': ['A']}, {'palettes': ('a', 'a')}, {'tags': ['x'] * 40}, {'energy': 101}, {'energy': 70.0},
                       {'adventurousness': 'x'}, {'vocals': -3}, {'vocals': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ShowError):
                Style(**kwargs)

    def test_a_researcher_needs_updates_switched_on(self):
        with self.assertRaises(ShowError):
            self.good(updates_allowed=False, update_researcher_id='x')

    def test_a_default_show_must_have_no_schedule(self):
        with self.assertRaises(ShowError):
            default_show(days='1,2')
        with self.assertRaises(ShowError):
            default_show(start_min=60)
        with self.assertRaises(ShowError):
            default_show(length_min=60)


class Hostile(unittest.TestCase):
    """Whatever arrives, the only failure is a ShowError (never a crash or a different exception)."""

    VALUES = (None, True, False, 0, -1, 10 ** 30, 1.5, float('nan'), float('inf'), '', ' ', 'x', 'x' * 70000, '\x00', '\ud800', [], [[]],
              [None], {}, {'a': {'b': {}}}, object(), b'bytes', (1, 2), {1, 2}, ['x'] * 5000)

    def test_every_field_fails_cleanly(self):
        base = parse()
        for field in ('id', 'name', 'days', 'start', 'end', 'style', 'host_id', 'talk', 'spots_allowed', 'updates_allowed',
                      'update_researcher_id', 'color', 'clock'):
            for value in self.VALUES:
                with self.subTest(field=field, value=repr(value)[:25]):
                    try:
                        for known in (None, base):
                            data = {'name': 'Hostile', 'start': '06:00', 'end': '10:00', 'id': 'hostile'}
                            data[field] = value
                            if known is not None:
                                data['id'] = base.id
                            Show.parse(data, known)
                    except ShowError:
                        pass

    def test_non_dictionary_payloads(self):
        for payload in (None, [], 'show', 5, [('name', 'x')], object()):
            with self.subTest(payload=repr(payload)[:20]), self.assertRaisesRegex(ShowError, 'as an object'):
                Show.parse(payload)
        with self.assertRaises(ShowError):
            Show.parse({'name': 'x', 'start': 1, 'end': 3, 5: 'numeric key'})


class Fuzz(unittest.TestCase):
    """Thousands of random payloads: the only thing allowed to happen is a clean ShowError or a valid show."""

    def test_random_payloads_never_raise_anything_but_show_error(self):
        rng = random.Random(1234567)
        base = parse(id='fuzz', host_id='dex')
        accepted = refused = 0
        for _ in range(6000):
            data = junk_show(rng)
            for known in (None, base):
                try:
                    show = Show.parse(data, known)
                except ShowError as exc:
                    refused += 1
                    self.assertNotRegex(str(exc), r'Traceback|<class|0x[0-9a-f]{6}|object at')
                else:
                    accepted += 1
                    self.assertEqual(Show.parse(show.to_dict(), show), show)       # whatever is accepted is canonical
                    json.dumps(show.to_dict())
        self.assertGreater(accepted, 100)
        self.assertGreater(refused, 3000)


class Packaging(unittest.TestCase):
    def test_runtime_code_uses_only_the_standard_library(self):
        folder = Path(__file__).resolve().parent.parent / 'myradio' / 'shows'
        files = sorted(folder.glob('*.py'))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module or '']
                else:
                    continue
                for module in modules:
                    top = module.split('.')[0]
                    self.assertIn(top, sys.stdlib_module_names, f'{path.name} imports {module}, which is not in the standard library')
                    self.assertNotEqual(top, 'audioop', 'audioop does not exist on Python 3.13')

    def test_the_package_is_found_by_the_installer_and_loaded_by_the_station(self):
        self.assertTrue((Path(__file__).resolve().parent.parent / 'myradio' / 'shows' / '__init__.py').is_file())
        from myradio.shows import extension
        self.assertEqual(extension.build().name, 'shows')


class Labels(unittest.TestCase):
    def test_time_labels(self):
        self.assertEqual([labels.time_label(m) for m in (0, 60, 360, 390, 720, 780, 1380, 1439, 1440, 1500)],
                         ['12 am', '1 am', '6 am', '6:30 am', '12 pm', '1 pm', '11 pm', '11:59 pm', '12 am', '1 am'])
        self.assertEqual(labels.range_label(360, 600), '6 am–10 am')
        self.assertEqual(labels.clock_string(1440), '24:00')
        self.assertEqual(labels.clock_string(0), '00:00')
        self.assertEqual(labels.clock_string(75), '01:15')
        self.assertEqual(labels.end_string(0, 1440), '24:00')
        self.assertEqual(labels.end_string(1320, 240), '02:00')
        self.assertEqual(labels.end_string(1200, 240), '24:00')

    def test_day_labels(self):
        cases = {(1, 2, 3, 4, 5, 6, 7): 'every day', (1, 2, 3, 4, 5): 'weekdays', (6, 7): 'weekends', (2,): 'Tuesday',
                 (1, 2): 'Monday and Tuesday', (1, 3, 5): 'Monday, Wednesday and Friday',
                 (1, 2, 3, 5): 'Monday to Wednesday and Friday', (1, 6, 7): 'Monday, Saturday and Sunday',
                 (3, 4, 5, 6): 'Wednesday to Saturday', (): 'no days'}
        for days, expected in cases.items():
            with self.subTest(days=days):
                self.assertEqual(labels.days_label(days), expected)

    def test_durations(self):
        self.assertEqual([labels.duration_label(m) for m in (1, 15, 60, 61, 120, 150)],
                         ['1 minute', '15 minutes', '1 hour', '1 hour 1 minute', '2 hours', '2 hours 30 minutes'])
        self.assertEqual(labels.mmss(225), '3:45')


if __name__ == '__main__':
    unittest.main()
