"""The kinds of thing a host says, and how each maps onto the shared station vocabulary."""
from __future__ import annotations

import dataclasses
from typing import Dict, FrozenSet, Tuple

# Shared vocabulary: generation job roles and library track kinds.
ROLE_SONG, ROLE_HOST_BREAK, ROLE_ID, ROLE_SPOT, ROLE_UPDATE = "song", "host_break", "id", "spot", "update"
KIND_MUSIC, KIND_SPEECH, KIND_AD = "music", "speech", "ad"

_LEXICON = ("audience", "sign_off", "vibe", "place", "aside")
LEXICON_KEYS: Tuple[str, ...] = _LEXICON
# Slot names a template may use. A leading capital asks for the first letter in capitals.
_BASE_SLOTS = ("station", "tagline", "host", "greeting", "daypart", "time", "weekday") + _LEXICON
_TRACKS = ("now_title", "now_artist", "next_title", "next_artist")


def _slots(*extra: str) -> FrozenSet[str]:
    names = set(_BASE_SLOTS) | set(extra)
    return frozenset(names | {n[0].upper() + n[1:] for n in names})


@dataclasses.dataclass(frozen=True)
class Segment:
    id: str
    role: str
    library_kind: str
    label: str
    summary: str
    requires: Tuple[str, ...]   # facts the caller must supply; without them nothing honest can be said
    low: float                  # default length in seconds for the briefest host
    high: float                 # default length in seconds for the chattiest host
    min_s: float                # shortest and longest length a caller may ask for
    max_s: float
    slots: FrozenSet[str]
    extendable: bool = False    # may be stretched with extra lines when a long length is asked for
    key: Tuple[str, ...] = ()   # values a script should use whenever the caller supplied them
    ext_at: str = "end"         # where extra lines go: after the script ("end") or ahead of its closing line


SEGMENTS: Dict[str, Segment] = {s.id: s for s in (
    Segment("station_id", ROLE_ID, KIND_SPEECH, "Station ID", "Says the station's name.",
            ("station",), 2.5, 5, 2, 7, _slots()),
    Segment("song_intro", ROLE_HOST_BREAK, KIND_SPEECH, "Song intro", "Introduces the song coming up next.",
            (), 4, 18, 3, 30, _slots("note", *_TRACKS), True, ("next_title",)),
    Segment("song_back_announce", ROLE_HOST_BREAK, KIND_SPEECH, "Back announcement",
            "Says what song just played.", (), 4, 16, 3, 25, _slots("note", *_TRACKS), True, ("now_title",)),
    Segment("time_check", ROLE_HOST_BREAK, KIND_SPEECH, "Time check", "Tells the local time.",
            ("local_time",), 2.5, 5, 2, 7, _slots()),
    Segment("sign_on", ROLE_HOST_BREAK, KIND_SPEECH, "Sign on", "Opens the broadcast day.",
            (), 6, 22, 4, 30, _slots("note", "next_title", "next_artist"), True),
    Segment("sign_off", ROLE_HOST_BREAK, KIND_SPEECH, "Sign off", "Closes the broadcast day.",
            (), 6, 22, 4, 30, _slots("note", "now_title", "now_artist"), True, ext_at="before_last"),
    Segment("spot_wrapper", ROLE_SPOT, KIND_AD, "Sponsor message",
            "Reads a sponsor's own words, always saying that it is sponsored.",
            ("sponsor", "sponsor_copy"), 5, 14, 4, 40, _slots("sponsor", "sponsor_copy")),
    Segment("update_intro", ROLE_UPDATE, KIND_SPEECH, "Update opening", "Introduces a sourced update.",
            (), 3.5, 7, 3, 10, _slots("topic", "count", "items", "here_are"), False, ("topic",)),
    Segment("update_outro", ROLE_UPDATE, KIND_SPEECH, "Update closing", "Closes a sourced update.",
            (), 3, 6, 3, 9, _slots("topic", "count", "items", "sources", "those", "were", "item_noun")),
    Segment("filler", ROLE_HOST_BREAK, KIND_SPEECH, "Short chat", "A short, friendly moment between songs.",
            (), 5, 20, 3, 30, _slots("note", *_TRACKS), True),
)}

SEGMENT_IDS: Tuple[str, ...] = tuple(SEGMENTS)

# A whole sourced update (opening, items read as found, closing). It is built by briefs.py, not from a template.
UPDATE = Segment("update", ROLE_UPDATE, KIND_SPEECH, "Update", "A sourced update read from a research brief.",
                 (), 20, 75, 8, 180, frozenset())
