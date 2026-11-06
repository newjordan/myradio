"""Shared fixtures for the hosts tests (this file holds no tests of its own)."""
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from myradio.extensions import ExtensionHost, StationFacade  # noqa: E402
from myradio.hosts import HostsService  # noqa: E402
from myradio.hosts.extension import build  # noqa: E402
from myradio.hosts.templates import TemplateLibrary, load_voice  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "myradio", "hosts", "data")

FULL = {
    "station": "Porchlight FM", "tagline": "Late-night sounds for stargazers and makers",
    "now_title": "Low Orbit Lullaby", "now_artist": "The Gentle Machines",
    "next_title": "Paper Lantern Weather", "next_artist": "Marble Hours",
    "local_time": "21:07", "weekday": "Tuesday", "notes": ["Request lines are open until midnight"],
    "sponsor": "Corner Bike Co.", "sponsor_copy": "Fixing bikes since 1998. Mention the station for a free tune-up.",
    "topic": "Backyard astronomy", "item_count": 3, "sources": ["Example Astronomy Club newsletter", "Example Sky Notes"],
}
THIN = {"station": "Radio", "local_time": "00:00", "sponsor": "Acme", "sponsor_copy": "Buy things."}
FACT_SETS = {
    "full": FULL,
    "no_artist": {k: v for k, v in FULL.items() if "artist" not in k},
    "thin": THIN,
    "morning_single": dict(FULL, local_time="06:45", item_count=1, weekday=3),
    "noon": dict(FULL, local_time="12:00", daypart="afternoon"),
}
BRIEF = {
    "id": "brief-1", "topic": "Backyard astronomy", "generated_at": "2026-11-05T07:42:00+00:00",
    "items": [
        {"id": "i1", "text": "A bright meteor was reported over the Pacific Northwest on Tuesday night. Observers called it "
                             "unusually slow. More details are expected.", "source": "Example Astronomy Club newsletter",
         "url": "https://example.org/meteor", "published_at": "2026-11-05T06:10:00+00:00"},
        {"id": "i2", "text": "Saturn's rings are close to edge-on this season, which makes them look thin.",
         "source": "Example Sky Notes", "kind": "commentary", "published_at": 1762282800},
        {"id": "i3", "text": "A beginner's guide to cleaning a telescope mirror.", "source": "Example Backyard Optics",
         "kind": "uncertain"},
    ],
}

_LIBRARY = None


def library():
    """The built-in script library, parsed once for the whole test run."""
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = TemplateLibrary.builtin()
    return _LIBRARY


def voice_data(name):
    """The shipped script style ``name`` as a dictionary, ready to be changed for a test."""
    with open(os.path.join(DATA, f"voice-{name}.json"), encoding="utf-8") as handle:
        return json.load(handle)


def library_with(voice, data):
    """The built-in library with one script style replaced by ``data`` (a test fixture, never shipped)."""
    return TemplateLibrary({**library().voices, voice: load_voice(data, f"{voice}-test.json")})


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def research_item(rank, headline, extract, source, kind="reported", url="https://example.org/a", truncated=False):
    """An item shaped like the research lane's (docs/lanes/research.md), built by hand: a fixture, not real feed data."""
    ts = 1762340000.0 + rank
    return {"id": f"{rank:016x}", "rank": rank, "headline": headline, "headline_speech": headline, "extract": extract,
            "extract_speech": extract, "extract_truncated": truncated, "source_name": source, "source_name_speech": source,
            "source_url": "https://example.org/feed.xml", "url": url, "published_ts": ts, "published_at": _iso(ts),
            "date_precision": "time", "date_basis": "published", "fetched_ts": ts + 60, "fetched_at": _iso(ts + 60),
            "kind": kind, "kind_reason": "fixture", "score": 1.0, "matched": ["sky"], "why": ["matched sky"],
            "tone": {"penalty": 0, "reasons": []}, "categories": []}


def research_brief(items, outcome="ready", name="Sky watcher"):
    """A research-lane Brief around ``items``. Tests check it with ``myradio.research.schema.check_brief`` first."""
    created = 1762343600.0
    ready = outcome == "ready"
    fresh = min(i["published_ts"] for i in items) + 24 * 3600 if items else None
    return {"id": "a" * 32, "researcher_id": "b" * 32, "researcher_name": name, "outcome": outcome, "message": "A fixture brief.",
            "review": "approved" if ready else None, "trigger": "manual", "created_ts": created, "created_at": _iso(created),
            "reviewed_ts": None, "reviewed_at": None, "aired_ts": None, "aired_at": None, "airable": ready,
            "freshness_hours": 24, "segment_seconds": 60, "target_words": 150, "anchor": None,
            "fresh_until_ts": fresh if ready else None, "fresh_until_at": _iso(fresh) if ready and fresh else None,
            "items": items if ready else [],
            "sources": [{"url": "https://example.org/feed.xml", "name": "Example", "status": "ok", "message": "Read.",
                         "entries": 3, "format": "rss", "checked_at": _iso(created)}],
            "summary": {"considered": 5, "kept": len(items) if ready else 0, "skipped": {}, "explanation": "A fixture."},
            "warnings": []}


def new_service(**kwargs):
    """A service with no database and a fresh memory, sharing the parsed library."""
    return HostsService(library=library(), **kwargs)


@contextlib.contextmanager
def no_network():
    """Any attempt to open a connection or resolve a name fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("the hosts code tried to use the network")

    with mock.patch("socket.socket.connect", refuse), mock.patch("socket.create_connection", refuse), \
            mock.patch("socket.getaddrinfo", refuse), mock.patch("urllib.request.urlopen", refuse):
        yield


class FakeStation:
    """Stands in for the station behind the extension seam (no audio, no clock, no network)."""

    def __init__(self):
        self.calls = 0
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {"station": {"name": "Porchlight FM", "tagline": "Late-night sounds for stargazers"},
                "current": {"title": "Low Orbit Lullaby", "artist": "The Gentle Machines", "kind": "music"},
                "queue": [{"title": "Spoken break", "artist": "", "kind": "speech"},
                          {"title": "Paper Lantern Weather", "artist": "Marble Hours", "kind": "music"}]}

    def import_audio(self, data, title, artist="", kind="music"):
        return {"id": "x", "title": title}

    def action(self, operation, arguments=None):
        return {"ok": True, "operation": operation}


class HostsCase(unittest.TestCase):
    """A running hosts extension inside a real ExtensionHost (temporary data directory)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.station = FakeStation()
        self.boot()

    def boot(self):
        self.host = ExtensionHost(self.temp.name, StationFacade(self.station))
        self.ext = build()
        self.host.register(self.ext)
        self.host.start()
        self.addCleanup(self.host.stop)

    def reboot(self):
        self.host.stop()
        self.boot()

    def threads(self):
        return [t for t in threading.enumerate() if t.name.startswith("myradio-ext-hosts")]
