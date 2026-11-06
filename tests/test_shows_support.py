"""Helpers shared by the shows tests (it holds no tests of its own)."""
import datetime as dt
from zoneinfo import ZoneInfo

from myradio.shows import Show, Week

UTC = dt.timezone.utc
EVERY_DAY = '1,2,3,4,5,6,7'


def show(show_id, name=None, start='06:00', end='10:00', **fields):
    """A valid scheduled show; keyword fields override the defaults."""
    data = {'id': show_id, 'name': name or show_id.replace('-', ' ').title(), 'start': start, 'end': end}
    data.update(fields)
    return Show.parse(data)


def week_of(zone, *shows):
    return Week(zone, shows)


def at(zone, year, month, day, hour=0, minute=0):
    """Epoch seconds of a local wall-clock moment (first occurrence if it happens twice)."""
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone)).astimezone(UTC).timestamp())


def utc_ts(year, month, day, hour=0, minute=0):
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def iso_utc(ts):
    return dt.datetime.fromtimestamp(ts, UTC).strftime('%Y-%m-%d %H:%M')


FIELDS = ('id', 'name', 'days', 'start', 'end', 'style', 'host_id', 'talk', 'spots_allowed', 'updates_allowed',
          'update_researcher_id', 'color', 'clock')
VALID = {'id': 'fuzz', 'name': 'Fuzz Show', 'days': '1,2,3', 'start': '06:00', 'end': '08:00',
         'style': {'palettes': ['neo-soul'], 'tags': [], 'energy': 40, 'adventurousness': 30, 'vocals': 20},
         'host_id': 'dex', 'talk': 'some', 'spots_allowed': True, 'updates_allowed': False, 'update_researcher_id': None,
         'color': '#336699', 'clock': 'classic'}
STRINGS = ('', ' ', 'x', 'morning', '06:00', '24:00', '1,2,3', 'rare', '#abcdef', 'dex', 'a' * 100, '\x00', '\ud800', '<b>',
           '[laughs]', 'weekdays', '../..', '9' * 50, 'default', 'Mon', '2025-01-01T00:00:00Z', 'true', 'none')


def junk(rng, depth=0):
    """A random JSON-like value, including the nasty ones (NaN, huge numbers, lone surrogates, deep nesting)."""
    kind = rng.randrange(9 if depth < 3 else 6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice((True, False))
    if kind == 2:
        return rng.choice((0, 1, -1, 7, 24, 59, 100, 101, 1440, 2 ** 31, 2 ** 70, -2 ** 70))
    if kind == 3:
        return rng.choice((0.0, 1.5, -0.0, 1e308, float('inf'), float('-inf'), float('nan'), 2.0, 70.0))
    if kind == 4:
        return rng.choice(STRINGS)
    if kind == 5:
        return ''.join(chr(rng.randrange(0x20, 0x30000)) for _ in range(rng.randrange(0, 12)))
    if kind == 6:
        return [junk(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
    if kind == 7:
        return {rng.choice(FIELDS + ('palettes', 'energy', 'zzz')): junk(rng, depth + 1) for _ in range(rng.randrange(0, 4))}
    return rng.choice(STRINGS) * rng.randrange(1, 40)


def junk_show(rng):
    """A show payload: sometimes entirely random, usually a valid show with a few fields wrecked."""
    if rng.random() < 0.15:
        return {rng.choice(FIELDS + ('zzz',)): junk(rng) for _ in range(rng.randrange(0, 6))}
    data = dict(VALID)
    for _ in range(rng.randrange(1, 4)):
        data[rng.choice(FIELDS)] = junk(rng)
    if rng.random() < 0.3:
        data.pop(rng.choice(FIELDS), None)
    return data
