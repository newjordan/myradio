"""Shared helpers for the ads tests. This file has no tests of its own."""
import datetime as dt
import hashlib
import json
import re
import tempfile
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from myradio.ads.engine import Audio, BreakContext, Play
from myradio.ads.extension import build
from myradio.ads.model import parse_campaign
from myradio.ads.terms import TERMS_VERSION
from myradio.extensions import ExtensionHost, Request, StationFacade

UTC = dt.timezone.utc
TODAY = dt.date(2026, 11, 5)  # a Thursday


def utc(year, month, day, hour=0, minute=0, second=0):
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp()


def local(zone, year, month, day, hour=0, minute=0, second=0, fold=0):
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(zone), fold=fold).timestamp()


def tz_available(name):
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


NOW = utc(2026, 11, 5, 15, 0)  # Thursday 15:00 UTC
MIN = 60.0
HOUR = 3600.0
DAY = 86400.0


def cid(name):
    """A stable 32-hex id for a campaign name."""
    return hashlib.sha256(name.encode()).hexdigest()[:32]


def make(advertiser="Acme", *, track=None, today=TODAY, **settings):
    """A validated campaign. Limits default to none and pacing to "asap" so each test switches on only what it studies."""
    track = track or "track-" + re.sub(r"[^a-z0-9]+", "-", advertiser.lower()).strip("-")
    raw = {"advertiser": advertiser, "timezone": "UTC", "frequency": "custom", "caps": {}, "separation": {}, "pacing": "asap",
           "creatives": [{"kind": "recorded", "track_id": track, "title": advertiser + " spot"}]}
    raw.update(settings)
    campaign, _ = parse_campaign(raw, today=today, campaign_id=cid(raw.get("name", advertiser)))
    return campaign


def play(campaign, at, *, creative=None, interrupted=False):
    return Play(campaign.id, creative or campaign.creatives[0].id, at, campaign.category, interrupted)


def context(campaigns, history=(), audio=None, **kw):
    """A break context where every referenced recording exists and is 30 seconds long."""
    if audio is None:
        audio = {x.track_id: Audio(30.0) for c in campaigns for x in c.creatives if x.track_id}
    kw.setdefault("voice_rights", True)
    kw.setdefault("commercial_use", True)
    return BreakContext(campaigns=tuple(campaigns), history=tuple(history), audio=audio, **kw)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeStation:
    """Stands in for the station behind the extension seam. It only answers status()."""

    def __init__(self, name="Test FM"):
        self.library, self.history, self.name, self.calls, self._n = [], [], name, 0, 0
        self.fail = False
        self._add = threading.Lock()  # several test threads add tracks at once

    def add_track(self, title, kind="ad", duration=30.0, available=True):
        with self._add:
            self._n += 1
            track_id = f"{self._n:032x}"
            self.library.append({"id": track_id, "title": title, "artist": "", "duration": duration, "kind": kind,
                                 "available": available})
        return track_id

    def status(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("station is busy")
        return {"library": [dict(t) for t in self.library],
                "history": [dict(h) if isinstance(h, dict) else h for h in self.history],
                "station": {"name": self.name}, "current": None}

    def import_audio(self, *args, **kwargs):  # the ads extension never imports audio itself
        raise AssertionError("ads must not import audio")

    def action(self, *args, **kwargs):
        raise AssertionError("ads must not drive the station")


class Harness:
    """An ads extension running inside a real ExtensionHost with a fake station and a movable clock."""

    def __init__(self, test, *, config=None, clock=None):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.station = FakeStation()
        self.clock = clock or Clock()
        if config is not None:
            state = self.directory / "ext" / "ads"
            state.mkdir(parents=True, mode=0o700)
            (state / "config.json").write_text(json.dumps(config))
        self._boot()

    def _boot(self):
        self.host = ExtensionHost(self.directory, StationFacade(self.station), clock=self.clock)
        self.ext = build()
        self.host.register(self.ext)
        self.host.start()
        self.test.addCleanup(self.host.stop)
        if self.ext._store is not None:  # these tests are about behaviour, not about surviving a power cut
            self.ext._store.conn.execute("PRAGMA synchronous=OFF")

    def restart(self):
        """Stop the extension and start a fresh one on the same data folder, as a station restart would."""
        self.host.stop()
        self._boot()

    def call(self, operation, **arguments):
        return self.host.call("ads." + operation, arguments)

    def route(self, method, path, query=None, body=None):
        match = self.host.find_route(method, "/api/x/ads" + path)
        assert match is not None, f"no route {method} {path}"
        raw = json.dumps(body).encode() if body is not None else b""
        return self.host.dispatch_route(match, Request(method, path, query or {}, body, raw))

    def confirm(self, *kinds):
        for kind in kinds:  # a screen says which wording of the note it showed
            reply = self.route("POST", "/acknowledge", body={kind: True, "version": TERMS_VERSION})
            assert reply.status == 200, reply.body

    def status(self):
        return self.host.status()["ads"]["status"]

    @staticmethod
    def started(track_id, at, title="Spot", kind="ad", duration=30.0):
        return {"type": "track_started", "at": at, "track": {"id": track_id, "title": title, "artist": "", "duration": duration,
                                                             "kind": kind, "available": True, "playback_error": None}}

    @staticmethod
    def finished(track_id, at, result="finished", title="Spot", kind="ad", duration=30.0):
        return {"type": "track_finished", "at": at, "result": result,
                "track": {"id": track_id, "title": title, "artist": "", "duration": duration, "kind": kind, "available": True,
                          "playback_error": None}}

    def save(self, **campaign):
        """Save a campaign through the operation, supplying a station-library recording when none is given."""
        campaign.setdefault("timezone", "UTC")
        if "creatives" not in campaign:
            campaign["creatives"] = [{"kind": "recorded", "track_id": self.station.add_track("Spot " + campaign.get("advertiser", "x"))}]
        return self.call("campaign_save", campaign=campaign)["campaign"]
