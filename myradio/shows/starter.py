"""Ready-made weeks. They are offered, never applied on their own: a person (or their agent) has to ask.

The starter week mirrors the sample shows in the design preview. The preview's example hosts
(nova, dex, marlowe) are referenced by id only; a station that has no host with one of those ids
simply has no host on that show until one is chosen.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Show

EVERY_DAY = "1,2,3,4,5,6,7"


@dataclass(frozen=True)
class WeekTemplate:
    name: str
    title: str
    description: str
    shows: tuple          # dicts understood by ``Show.parse``

    def build(self) -> list:
        return [Show.parse(entry) for entry in self.shows]

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "show_count": len(self.shows), "shows": [e["name"] for e in self.shows]}


def _show(show_id, name, start, end, palette, energy, vocals, host, talk, spots, updates, color):
    return {"id": show_id, "name": name, "days": EVERY_DAY, "start": start, "end": end,
            "style": {"palettes": [palette], "tags": [], "energy": energy, "adventurousness": 35, "vocals": vocals},
            "host_id": host, "talk": talk, "spots_allowed": spots, "updates_allowed": updates, "color": color,
            "clock": "classic"}


WEEK_TEMPLATES = {
    "starter": WeekTemplate(
        "starter", "A day on the air",
        "Five shows that follow the day, every day: Overnight Drift, Morning Rise, Daytime Focus, Golden Hour "
        "and Late Night. Change anything afterwards.",
        (
            _show("overnight", "Overnight Drift", "00:00", "06:00", "ambient-garden", 15, 0, None, "rare", False, False, "#3b4a63"),
            _show("morning", "Morning Rise", "06:00", "10:00", "bossa-courtyard", 70, 40, "dex", "often", True, True, "#8a4d3a"),
            _show("daytime", "Daytime Focus", "10:00", "17:00", "chamber-minimalism", 35, 5, "nova", "rare", True, False, "#3f6050"),
            _show("golden", "Golden Hour", "17:00", "20:00", "warm-brush-jazz", 50, 30, "marlowe", "some", True, True, "#7a5c26"),
            _show("late", "Late Night", "20:00", "24:00", "downtempo-trip-hop", 30, 20, "nova", "some", False, False, "#454a7a"),
        )),
    "empty": WeekTemplate(
        "empty", "No scheduled shows",
        "Clears the schedule. Your default show then plays all day, every day, until you add shows.",
        ()),
}
TEMPLATE_NAMES = tuple(WEEK_TEMPLATES)
