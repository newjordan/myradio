"""Clock templates and the hour plan.

``hour_plan`` says what one block of airtime holds: a station ID, host breaks that follow the show's
talk level, an optional spot break and update, and songs for the rest. Its estimated seconds always
add up to the block.

With no seed it returns the plain recipe, which is the example hour shown in the design preview
(one ID, 1/2/3 host breaks of 20/30/40 seconds for rare/some/often talk, a 60 second spot break, a
90 second update, songs of about 225 seconds). With a seed the same ingredients are shuffled a
little (the order of breaks, how many songs sit between them, and song lengths) so that no two
hours sound alike, and the result is a pure function of the seed: it is the same after a restart,
on another machine and on any supported Python version. Nothing here reads the clock or uses
Python's per-process hash randomization.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Optional, Union

from .labels import ROLE_LABELS, ROLE_ORDER, mmss

HOUR = 3600
MAX_SEED = 2 ** 63 - 1
MAX_BLOCK = 4 * HOUR       # no block is ever planned longer than this
TINY_BLOCK = 60            # shorter than this: nothing is planned (the previous item simply runs over)
SHORT_BLOCK = 300          # shorter than this: one filler song, no station ID or breaks
SONG_FLOOR = 150           # the average planned song is never shorter than this
SONG_LOW, SONG_HIGH = 150, 420   # bounds for the seeded song-length variation
TALK_LEVELS = ("rare", "some", "often")


@dataclass(frozen=True)
class ClockTemplate:
    name: str
    title: str
    description: str
    breaks: Mapping[str, tuple]   # talk level -> (host breaks per hour, seconds per break)
    id_seconds: int
    spot_seconds: int
    update_seconds: int
    song_seconds: int             # average song length, used to decide how many songs fit

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "host_breaks": {level: {"per_hour": n, "seconds_each": s} for level, (n, s) in self.breaks.items()}}


TEMPLATES = {
    "classic": ClockTemplate(
        "classic", "Classic",
        "A station ID, a few short host breaks and songs for the rest of the hour.",
        {"rare": (1, 20), "some": (2, 30), "often": (3, 40)}, 10, 60, 90, 225),
    "music-heavy": ClockTemplate(
        "music-heavy", "More music",
        "Fewer and shorter breaks. Songs fill most of the hour.",
        {"rare": (1, 15), "some": (1, 20), "often": (2, 25)}, 10, 30, 60, 240),
    "talky": ClockTemplate(
        "talky", "More talk",
        "More and longer host breaks, and a fuller update. Songs fill what is left.",
        {"rare": (2, 30), "some": (3, 45), "often": (5, 60)}, 10, 60, 120, 210),
}
DEFAULT_TEMPLATE = "classic"


@dataclass(frozen=True)
class ClockItem:
    role: str          # one of: id, song, host_break, spot, update
    est_seconds: int   # whole seconds
    ordinal: int       # 0-based position among items of the same role in this block


class _Stream:
    """A tiny deterministic pseudo-random stream (SHA-256 in counter mode)."""

    def __init__(self, key: str):
        self._key = key.encode("utf-8")
        self._count = 0

    def below(self, n: int) -> int:
        digest = hashlib.sha256(self._key + b"|" + str(self._count).encode("ascii")).digest()
        self._count += 1
        return int.from_bytes(digest[:8], "big") % n

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]


def seed_key(seed: Union[int, str]) -> str:
    """The text a seed is reduced to. Integers must be 0..2**63-1; text is limited to 200 characters."""
    if isinstance(seed, bool) or not isinstance(seed, (int, str)):
        raise ValueError("The seed must be a whole number or text.")
    if isinstance(seed, int) and not 0 <= seed <= MAX_SEED:
        raise ValueError(f"The seed must be a whole number from 0 to {MAX_SEED}.")
    if isinstance(seed, str) and not 0 < len(seed) <= 200:
        raise ValueError("A text seed must be 1 to 200 characters.")
    return str(seed)


def template_for(name: str) -> ClockTemplate:
    try:
        return TEMPLATES[name]
    except (KeyError, TypeError):
        raise ValueError("Choose an hour style: " + ", ".join(TEMPLATES) + ".") from None


def _scaled(per_hour: int, seconds: int) -> int:
    """per_hour * seconds / 3600 rounded half up, in whole-number arithmetic."""
    return (2 * per_hour * seconds + HOUR) // (2 * HOUR)


def hour_plan(show, seed: Union[None, int, str] = None, *, template: Optional[str] = None,
              seconds: int = HOUR) -> list:
    """Ordered ``ClockItem`` list whose ``est_seconds`` add up to ``seconds`` (an hour by default).

    ``show`` needs ``talk``, ``host_id``, ``spots_allowed``, ``updates_allowed`` and ``clock``.
    ``seed`` None gives the recipe; any other seed gives the shuffled variant for that seed.
    Blocks other than a full hour are scaled: the number of breaks is proportional to the length
    (rounded half up), a block under five minutes holds a single filler song, and a block under one
    minute holds nothing.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= MAX_BLOCK:
        raise ValueError("A block must be between 1 second and 4 hours long.")
    spec = template_for(getattr(show, "clock", DEFAULT_TEMPLATE) if template is None else template)
    stream = None if seed is None else _Stream(seed_key(seed))
    if seconds < TINY_BLOCK:
        return []
    if seconds < SHORT_BLOCK:
        return [ClockItem("song", seconds, 0)]

    count, each = spec.breaks[show.talk]
    extras: list = []
    if show.host_id:
        extras += [("host_break", each)] * _scaled(count, seconds)
    if show.spots_allowed:
        extras += [("spot", spec.spot_seconds)] * _scaled(1, seconds)
    if show.updates_allowed:
        extras += [("update", spec.update_seconds)] * _scaled(1, seconds)

    # Drop the least important breaks (update, then spot, then the last host break) until songs fit:
    # there must be a song between breaks and the songs must average at least SONG_FLOOR seconds.
    while True:
        available = seconds - spec.id_seconds - sum(s for _, s in extras)
        songs = max(1, available // spec.song_seconds)
        if (available >= SONG_FLOOR and songs >= len(extras) + 1) or not extras:
            break
        extras.pop()

    base, spare = divmod(available, songs)
    lengths = [base + (1 if i < spare else 0) for i in range(songs)]

    # Songs between breaks, evenly spread; the last stretch takes what is left (as in the preview).
    gap = max(1, songs // (len(extras) + 1))
    sizes, made = [], 0
    for i in range(len(extras) + 1):
        take = songs - made if i == len(extras) else min(gap, songs - made)
        sizes.append(take)
        made += take

    if stream is not None:
        stream.shuffle(extras)
        for _ in range(len(extras)):                 # nudge breaks one song earlier or later
            i = stream.below(len(extras))
            forward = stream.below(2) == 0
            donor, taker = (i, i + 1) if forward else (i + 1, i)
            if sizes[donor] >= 2:
                sizes[donor] -= 1
                sizes[taker] += 1
        for _ in range(songs):                       # vary song lengths without changing the total
            i, j = stream.below(songs), stream.below(songs)
            shift = 1 + stream.below(20)
            if i != j and lengths[i] - shift >= SONG_LOW and lengths[j] + shift <= SONG_HIGH:
                lengths[i] -= shift
                lengths[j] += shift

    sequence = [("id", spec.id_seconds)]
    cursor = 0
    for i, size in enumerate(sizes):
        for _ in range(size):
            sequence.append(("song", lengths[cursor]))
            cursor += 1
        if i < len(extras):
            sequence.append(extras[i])

    seen: dict = {}
    items = []
    for role, secs in sequence:
        ordinal = seen.get(role, 0)
        seen[role] = ordinal + 1
        items.append(ClockItem(role, secs, ordinal))
    return items


def summarize(items) -> list:
    """Legend rows (role, label, count, seconds) in a fixed order, skipping roles that are absent."""
    totals: dict = {}
    for item in items:
        count, secs = totals.get(item.role, (0, 0))
        totals[item.role] = (count + 1, secs + item.est_seconds)
    return [{"role": role, "label": ROLE_LABELS[role], "count": totals[role][0], "seconds": totals[role][1],
             "length": mmss(totals[role][1])} for role in ROLE_ORDER if role in totals]


def clock_notes(show, template: Optional[str] = None) -> list:
    """Short plain-language remarks about why an example hour looks the way it does."""
    notes = []
    if not show.host_id:
        notes.append("This show has no host, so it has no talk breaks. Choose a host to add them.")
    if not show.spots_allowed:
        notes.append("Spots are switched off for this show.")
    if show.updates_allowed:
        notes.append("The update plays only when a researcher has something new. Otherwise music keeps playing.")
    return notes
