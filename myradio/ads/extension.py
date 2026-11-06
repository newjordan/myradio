"""Sponsor messages ("spots") as a station extension.

Campaigns, scheduling rules, break building, the as-run log and honest reports. Core playout never
calls into this module: station events arrive on this extension's own thread, state lives in
``<data>/ext/ads/ads.sqlite3``, and ``status()`` answers from a cached snapshot.

Two confirmations gate risky use: *voice rights* (spoken, written-script spots) and *paid use*
(any sponsor campaign). They are stored with a timestamp and are set only by the authenticated HTTP
route ``POST /acknowledge``. No operation can set them, so a chat model or an outside agent cannot
tick a legal box for the operator.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import functools
import json
import math
import sqlite3
import threading
import time
import uuid
from typing import Optional

from myradio.extensions import Arg, Extension, ExtensionError, Operation, Request, Response, Route

from .engine import Audio, BreakContext, Play, build_break
from .flights import MAX_TIMESTAMP, campaign_state, local_datetime, next_change
from .model import (HOUSE_NAME_MESSAGE, ID_RE, MAX_ADVERTISER, MAX_CAMPAIGNS, NAMELESS_MESSAGE, NAMELESS_STATION_MESSAGE, ROLE,
                    SHOW_RE, AdsError, Campaign, estimate_seconds, house_name_ok, parse_campaign, parse_date, sponsor_name_ok,
                    spoken_text, zone_for)
from .reports import build_report, report_csv
from .store import STEPS, AdsStore
from .terms import KINDS, NEEDS_COMMERCIAL_USE, NEEDS_VOICE_RIGHTS, TERMS, TERMS_VERSION
from .words import schedule_text

HISTORY_DAYS = 9  # how far back a break looks for plays (covers a calendar week in any timezone)
PLANNED_GRACE = 45 * 60  # a planned play that has not aired this long after its time is forgotten
RECONCILE_EVERY = 30.0  # seconds between looks at the station's own history
LIBRARY_MAX_AGE = 10.0  # seconds a library snapshot is reused for read-only screens
STATUS_CAMPAIGNS = 25  # campaigns named in the status summary (the station's assistant drops a status over 4,000 characters)
STATUS_CHARS = 3000  # ... and the names are cut off before they take more than this much room
TERMINAL = {"finished": "finished", "skipped": "interrupted", "stopped": "interrupted", "playback failed": "interrupted"}
NOT_FOUND = "That spot isn't in your list any more. Reload the list and try again."
CAMPAIGN_HELP = (
    "The spot to save. Send the whole object, or only the fields to change together with its id. Fields: "
    "advertiser (name of who it is for), name, category (a short word such as bikes, used to keep similar spots apart), "
    "house (true only for your own station promo, which carries the station's name; a business that pays is a paid spot), "
    "priority 1-10, pacing (even or asap), frequency (light, regular or often), "
    "timezone (for example Europe/London), flights [{start YYYY-MM-DD, end, days \"1,2,3,4,5\" (Monday=1), "
    "dayparts [{from HH:MM, to HH:MM}], show_ids}], caps {per_hour, per_day, per_week}, "
    "separation {campaign_minutes, category_minutes}, plan {total} or {per_day}, and creatives: "
    "{kind recorded, track_id of a library track imported as an ad} or {kind script, text, voice}. "
    "Spoken scripts need the voice-rights confirmation and sponsor spots need the paid-use confirmation; "
    "neither can be given through an operation. A written message keeps its voice recording only while its words, "
    "voice and the sponsor's name stay the same.")


def _guard(method):
    """Turn a model problem or a storage problem into a plain message for the operator."""
    @functools.wraps(method)
    def wrapper(self, ctx, arg):
        try:
            return method(self, ctx, arg)
        except AdsError as exc:
            raise ExtensionError(str(exc)) from None
        except sqlite3.Error:
            self._problem = "The spots list could not be saved. Check free disk space and try again."
            raise ExtensionError(self._problem) from None
    return wrapper


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _first(query, key, default=None):
    values = query.get(key) or []
    return values[0] if values else default


@dataclasses.dataclass(frozen=True)
class _Snapshot:
    campaigns: tuple
    by_id: dict
    track_map: dict  # library track id -> (campaign id, creative id)
    acks: dict
    since: dict = dataclasses.field(default_factory=dict)  # creative id -> earliest start that may count as a play of it


@dataclasses.dataclass(frozen=True)
class _Derived:
    snap: _Snapshot
    counts: dict
    brief: list
    valid_until: float  # the next moment a flight starts or ends
    computed_at: float = 0.0


@dataclasses.dataclass(frozen=True)
class _StationView:
    library: dict
    titles: dict  # title -> [track ids], every kind
    history: tuple
    name: str
    fetched: float


class AdsExtension(Extension):
    name = "ads"

    def __init__(self):
        self._ctx = None
        self._store: Optional[AdsStore] = None
        self._lock = threading.RLock()
        self._closed = True
        self._snap: Optional[_Snapshot] = None
        self._derived: Optional[_Derived] = None
        self._view: Optional[_StationView] = None
        self._last_reconcile = -1e9
        self._last_play_at: Optional[float] = None
        self._plays_logged = 0
        self._unattributed = 0
        self._problem: Optional[str] = None
        self._settings_cache: Optional[dict] = None
        self._settings_at = -1e9
        self._settings_problem: Optional[str] = None

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self, ctx) -> None:
        self._ctx = ctx
        conn = None
        try:
            conn = ctx.open_db("ads.sqlite3")
            self._store = AdsStore(conn, steps=STEPS)
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            raise ExtensionError("The spots list file is damaged or can't be read. Restore it from a backup, or move "
                                 "ext/ads/ads.sqlite3 out of your station folder to start with an empty list.") from None
        except Exception:
            if conn is not None:
                conn.close()
            raise
        self._lock = self._store.lock
        self._closed = False
        self._reload()
        try:
            self._store.prune(ctx.clock())
            self._plays_logged = self._store.play_count()
            self._last_play_at = self._store.newest_play_time()
        except Exception:
            pass
        try:
            self._reconcile_now(force=True)  # first: the station's own history can say how an ad really ended
        except Exception:
            pass  # a missed look at the station's history is retried later and never stops startup
        try:
            # Nothing can be on air as the station starts, so a play whose ending was never seen was cut off by the stop.
            # Counting it as played would show two plays for one ad that the station then starts again from the top.
            self._store.close_open_plays(ctx.clock(), "station restarted")
        except Exception:
            pass

    def stop(self) -> None:
        store = self._store
        if store is None or self._closed:
            return
        self._closed = True
        store.close()

    def persistent_files(self):
        return ["ads.sqlite3", "config.json"]

    def _clock(self) -> float:
        return self._ctx.clock()

    # ---- settings the operator may write in config.json ---------------------------------------------
    def _settings(self) -> dict:
        now = time.monotonic()
        if self._settings_cache is not None and now - self._settings_at < 10:
            return self._settings_cache
        values = {"timezone": "UTC", "max_per_break": 3, "max_per_hour": 8}
        problem = None
        try:
            raw = self._ctx.config()
        except ExtensionError:
            raw, problem = {}, "config.json could not be read, so the standard limits are used."
        zone = raw.get("timezone")
        if zone is not None:
            try:
                zone_for(zone)
                values["timezone"] = zone
            except AdsError:
                problem = "config.json names a timezone this computer doesn't know, so UTC is used."
        for key, name, high in (("max_per_break", "max_spots_per_break", 20), ("max_per_hour", "max_spots_per_hour", 100)):
            if name in raw:
                number = raw[name]
                if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= high:
                    values[key] = number
                else:
                    problem = f"config.json: {name} must be a whole number from 1 to {high}, so the standard limit is used."
        self._settings_cache, self._settings_at, self._settings_problem = values, now, problem
        return values

    # ---- the cached snapshot ---------------------------------------------------------------------------
    def _reload(self) -> None:
        with self._lock:
            campaigns = tuple(self._store.load())
            acks = self._store.ack_state()
            attached = self._store.attached_times()
        by_id = {c.id: c for c in campaigns}
        track_map = {x.track_id: (c.id, x.id) for c in campaigns for x in c.creatives if x.track_id}
        since = {}
        for c in campaigns:
            for x in c.creatives:
                if x.track_id:
                    known = attached.get(x.id)  # a recording is only counted from the moment it was attached to the message
                    since[x.id] = max(c.created_at, known[1]) if known and known[0] == x.track_id else c.created_at
        self._snap = _Snapshot(campaigns, by_id, track_map, acks, since)
        self._derived = None

    def _derive(self, snap: _Snapshot, now: float) -> _Derived:
        counts = {"running": 0, "scheduled": 0, "paused": 0, "ended": 0}
        brief, valid, room, full = [], math.inf, STATUS_CHARS, False
        for campaign in snap.campaigns:
            try:
                state = campaign_state(campaign, now)
                change = next_change(campaign, now)
            except AdsError:
                state, change = "ended", None
            counts[state] += 1
            if change is not None:
                valid = min(valid, change)
            if len(brief) < STATUS_CAMPAIGNS and not full:
                entry = {"id": campaign.id, "name": campaign.name, "state": state, "house": campaign.house}
                size = len(json.dumps(entry, ensure_ascii=False)) + 2
                if size <= room:
                    brief.append(entry)
                    room -= size
                else:
                    full = True  # long names: stop here, so the list is always the first spots and never a patchwork
        return _Derived(snap, counts, brief, valid, now)

    @staticmethod
    def _ack_public(acks: dict) -> dict:
        return {kind: {"acknowledged": a["acknowledged"], "at": _iso(a["at"]), "needs_reconfirm": a["needs_reconfirm"]}
                for kind, a in acks.items()}

    def status(self) -> dict:
        """Answered from memory: no database, no locks, no call back into the station."""
        snap = self._snap
        if snap is None:
            return {}
        now = self._clock()
        derived = self._derived
        if derived is None or derived.snap is not snap or not derived.computed_at <= now < derived.valid_until:
            derived = self._derived = self._derive(snap, now)
        out = {**derived.counts, "campaigns": list(derived.brief), "campaigns_total": len(snap.campaigns),
               "acknowledgements": self._ack_public(snap.acks), "last_play_at": _iso(self._last_play_at),
               "plays_logged": self._plays_logged}
        if self._problem:
            out["problem"] = self._problem
        return out

    def checks(self):
        snap, acks = self._snap, (self._snap.acks if self._snap else {})
        if snap is None:
            return []
        try:
            self._settings()  # notices a config.json the operator has just broken or fixed
        except Exception:
            pass
        out = []
        if not snap.campaigns:
            out.append({"id": "spots", "title": "Sponsor spots", "status": "ok", "detail": "No spots are set up.",
                        "hint": "That's fine if you don't run sponsor messages. Add one in Spots when you do."})
        else:
            out.append({"id": "spots", "title": "Sponsor spots", "status": "ok",
                        "detail": f"{len(snap.campaigns)} {'spot' if len(snap.campaigns) == 1 else 'spots'} set up.", "hint": ""})
        if any(c.has_script for c in snap.campaigns) and not acks["voice_rights"]["acknowledged"]:
            out.append({"id": "voice_rights", "title": "Voice rights", "status": "warn",
                        "detail": "You have spoken spots, but voice rights are not confirmed, so they stay on hold.",
                        "hint": "Open Spots, read the voice note and confirm it, or use recorded spots."})
        if any(c.paid for c in snap.campaigns) and not acks["commercial_use"]["acknowledged"]:
            out.append({"id": "commercial_use", "title": "Paid spots", "status": "warn",
                        "detail": "You have paid spots, but paid use is not confirmed, so they stay on hold.",
                        "hint": "Open Spots, read the note about licenses and confirm it."})
        if any(not _zone_known(c.timezone) for c in snap.campaigns):
            out.append({"id": "timezones", "title": "Spot timezones", "status": "warn",
                        "detail": "A spot uses a timezone this computer doesn't know, so it is not playing.",
                        "hint": "Open Spots, edit that spot and pick another timezone."})
        if self._problem or self._store is not None and self._store.damaged:
            out.append({"id": "storage", "title": "Spots list", "status": "warn",
                        "detail": self._problem or "Some saved spots could not be read back and are not playing.",
                        "hint": "Check free disk space, then re-save the spots that are missing."})
        if self._settings_problem:
            out.append({"id": "settings", "title": "Spot settings", "status": "warn", "detail": self._settings_problem,
                        "hint": "Fix or remove the setting in ext/ads/config.json inside your station folder."})
        return out

    # ---- what the station tells us ----------------------------------------------------------------------------
    def _station_view(self, max_age: float = 0.0) -> _StationView:
        view = self._view
        if view is not None and time.monotonic() - view.fetched <= max_age:
            return view
        try:
            status = self._ctx.station.status()
        except Exception:
            raise ExtensionError("The station isn't answering right now, so I can't check your library. "
                                 "Wait a moment and try again.") from None
        library, titles = {}, {}
        for track in status.get("library") or []:
            if isinstance(track, dict) and isinstance(track.get("id"), str):
                library[track["id"]] = {key: track.get(key) for key in ("id", "title", "kind", "duration", "available")}
                if isinstance(track.get("title"), str):
                    titles.setdefault(track["title"], []).append(track["id"])
        name = (status.get("station") or {}).get("name")
        view = _StationView(library, titles, tuple(r for r in status.get("history") or [] if isinstance(r, dict)),
                            name if isinstance(name, str) and name.strip() else "this station", time.monotonic())
        self._view = view
        if time.monotonic() - self._last_reconcile >= RECONCILE_EVERY:
            try:
                self._reconcile(view)
            except Exception:
                pass
        return view

    def on_event(self, event: dict) -> None:
        if self._closed or not isinstance(event, dict):
            return
        try:
            kind = event.get("type")
            if kind == "track_started":
                self._on_started(event)
            elif kind == "track_finished":
                self._on_finished(event)
            self._problem = None
        except sqlite3.Error:
            self._problem = "The as-run log could not be written. Check free disk space."
        except AdsError:
            pass

    @staticmethod
    def _ad_track(event: dict):
        track = event.get("track")
        if isinstance(track, dict) and track.get("kind") == "ad" and isinstance(track.get("id"), str):
            return track
        return None

    def _event_time(self, event: dict) -> float:
        at = event.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) and 0 < at < MAX_TIMESTAMP:
            return float(at)
        return self._clock()

    def _on_started(self, event: dict) -> None:
        track = self._ad_track(event)
        if track is None:
            return
        link = self._snap.track_map.get(track["id"])
        if link is None:
            self._unattributed += 1  # an ad file that no spot uses
            return
        self._log_play(link, track["id"], track.get("title"), self._event_time(event), "event", duration=track.get("duration"))

    def _on_finished(self, event: dict) -> None:
        track = self._ad_track(event)
        if track is None:
            return
        link = self._snap.track_map.get(track["id"])
        if link is None:
            return
        at, result = self._event_time(event), event.get("result")
        outcome = TERMINAL.get(result) if isinstance(result, str) else None
        if outcome is None:
            return
        with self._lock:
            row = self._store.open_play(track["id"], at)
            if row is not None:
                self._store.close_play(row["id"], at, outcome, None if outcome == "finished" else result)
                return
            duration = track.get("duration")
            if outcome == "finished" and isinstance(duration, (int, float)) and duration > 0:
                # The start was missed; the finish tells us it played.
                self._log_play(link, track["id"], track.get("title"), max(0.0, at - float(duration)), "reconciled",
                               duration=duration, ended=at, outcome="finished")

    def _counts_from(self, campaign: Campaign, creative) -> float:
        """The earliest start that may be written down, after the fact, as a play of this message."""
        return max(campaign.created_at, self._snap.since.get(creative.id, 0.0))

    def _log_play(self, link: tuple, track_id: str, title, at: float, source: str, *, duration=None,
                  ended: Optional[float] = None, outcome: Optional[str] = None, detail: Optional[str] = None):
        """Write one as-run line, once. Returns (row id, whether a new line was written)."""
        campaign_id, creative_id = link
        campaign = self._snap.by_id.get(campaign_id)
        creative = next((x for x in campaign.creatives if x.id == creative_id), None) if campaign else None
        if campaign is None or creative is None:
            return None, False
        tolerance = 3.0
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            tolerance = min(3.0, max(0.05, float(duration) / 2))
        with self._lock:
            if self._closed:
                return None, False
            found = self._store.find_play(track_id, at, tolerance)
            # The same play, seen again: a repeated signal, or the station's history and the live signal telling the
            # same story. A play that was over (its end is on record, before this start) is not: the ad was stopped
            # and started again within moments, and the second time is a play of its own.
            if found is not None and (found["ended_at"] is None or at <= found["ended_at"]):
                return found["id"], False
            if source == "reconciled" and at < self._counts_from(campaign, creative):
                # It aired before this spot, or before this recording was attached to it: for example a recording that
                # the operator test-played from the library before making the spot. That was not a play of this spot.
                return None, False
            planned = self._store.link_planned(creative_id, at, PLANNED_GRACE)
            play_id = self._store.add_play(
                campaign_id=campaign_id, creative_id=creative_id, track_id=track_id,
                title=(title if isinstance(title, str) and title.strip() else creative.title or campaign.name)[:200],
                category=campaign.category, paid=campaign.paid, spoken=creative.spoken, started_at=at, source=source,
                ended_at=ended, outcome=outcome, detail=detail, planned_id=planned,
                commercial_ack_id=self._store.ack_id_at("commercial_use", at) if campaign.paid else None,
                voice_ack_id=self._store.ack_id_at("voice_rights", at) if creative.spoken else None)
            self._plays_logged += 1
            if self._last_play_at is None or at > self._last_play_at:
                self._last_play_at = at
            return play_id, True

    # ---- catching up on missed events -------------------------------------------------------------------------------
    def _reconcile_now(self, force: bool = False) -> dict:
        if not force and time.monotonic() - self._last_reconcile < RECONCILE_EVERY:
            return {"checked": False}
        return self._reconcile(self._station_view(0.0))

    def _reconcile(self, view: _StationView) -> dict:
        """Compare the station's own history with the as-run log and write down plays that were missed.

        Safe to run any number of times: a play already in the log is recognised by its track and
        start time, so a second pass adds nothing. Only titles that belong to exactly one library
        track can be matched, so an ambiguous title is left alone rather than guessed."""
        self._last_reconcile = time.monotonic()
        snap = self._snap
        added = closed = 0
        pending: dict = {}
        for row in reversed(view.history):  # oldest first
            title, event = row.get("title"), row.get("event")
            at = _parse_history_time(row.get("at"))
            ids = view.titles.get(title) if isinstance(title, str) else None
            if at is None or not ids or len(ids) != 1:
                continue
            link = snap.track_map.get(ids[0])
            if link is None:
                continue
            if event == "playing":
                track = view.library.get(ids[0], {})
                play_id, created = self._log_play(link, ids[0], title, at, "reconciled", duration=track.get("duration"))
                added += int(created)
                if play_id is not None:
                    pending[title] = play_id
            elif event in TERMINAL and title in pending:
                play_id = pending.pop(title)
                outcome = TERMINAL[event]
                with self._lock:
                    closed += int(self._store.close_play(play_id, at, outcome, None if outcome == "finished" else event))
        return {"checked": True, "added": added, "closed": closed}

    # ---- operations ----------------------------------------------------------------------------------------------------
    def operations(self):
        campaign_id = Arg("string", "The id of the spot (from the status summary or the list).", max_length=32)
        return [
            Operation("campaign_save", "Create a sponsor spot or station promo, or change one by sending its id. "
                      "Checks the rules and the recordings, and refuses spoken or paid spots until the operator has "
                      "confirmed voice rights and paid use on the Spots screen.",
                      {"campaign": Arg("object", CAMPAIGN_HELP)}, self._op_campaign_save),
            Operation("campaign_get", "Show one spot in full: its rules, messages, and what is stopping it from playing.",
                      {"campaign": campaign_id}, self._op_campaign_get),
            Operation("campaign_pause", "Pause a spot so it stops playing. Its history is kept.",
                      {"campaign": campaign_id}, self._op_campaign_pause),
            Operation("campaign_resume", "Let a paused spot play again.", {"campaign": campaign_id}, self._op_campaign_resume),
            Operation("campaign_remove", "Remove a spot from rotation for good. Its past plays stay in reports.",
                      {"campaign": campaign_id}, self._op_campaign_remove),
            Operation("break_build", "Choose the sponsor messages for the next ad break and explain what was left out. "
                      "Does not queue anything. With record true it also remembers the plan so the next break respects "
                      "it; leave record off to just look.",
                      {"seconds": Arg("number", "Most seconds the break may last.", minimum=1, maximum=600),
                       "show_id": Arg("string", "The show the break is in.", required=False, max_length=63),
                       "at": Arg("string", "When the break starts, ISO-8601 with offset. Default: now.", required=False, max_length=40),
                       "seed": Arg("string", "Repeat a choice exactly by reusing its seed.", required=False, max_length=64),
                       "record": Arg("boolean", "Remember this plan. Default false.", required=False),
                       "ready_only": Arg("boolean", "Only messages whose audio is ready. Default true.", required=False)},
                      self._op_break_build),
            Operation("spot_speech", "Get the exact words the voice should read for a written spot: the sponsorship line "
                      "first, then the copy, with speaker and event tags removed. Refused until voice rights are confirmed.",
                      {"campaign": campaign_id, "creative": Arg("string", "The id of the written message.", max_length=32)},
                      self._op_spot_speech),
            Operation("spot_audio", "Attach the finished voice recording (a library track imported as an ad) to a written spot.",
                      {"campaign": campaign_id, "creative": Arg("string", "The id of the written message.", max_length=32),
                       "track_id": Arg("string", "The library track that holds the recording.", max_length=64)},
                      self._op_spot_audio),
            Operation("report", "Plays, delivered share against the plan, and why spots were held back, per spot and day.",
                      {"campaign": Arg("string", "Only this spot.", required=False, max_length=32),
                       "from_date": Arg("string", "First day, YYYY-MM-DD. Default: six days ago.", required=False, max_length=10),
                       "to_date": Arg("string", "Last day, YYYY-MM-DD. Default: today.", required=False, max_length=10)},
                      self._op_report),
        ]

    def _need_open(self) -> None:
        if self._closed or self._store is None:
            raise ExtensionError("Spots are not available right now. Restart the station and try again.")

    def _find(self, campaign_id) -> Campaign:
        if not isinstance(campaign_id, str) or not ID_RE.fullmatch(campaign_id):
            raise AdsError(NOT_FOUND)
        campaign = self._snap.by_id.get(campaign_id)
        if campaign is None:
            raise AdsError(NOT_FOUND)
        return campaign

    def _find_creative(self, campaign: Campaign, creative_id):
        creative = next((x for x in campaign.creatives if x.id == creative_id), None)
        if creative is None:
            raise AdsError("That message isn't part of this spot any more. Reload the spot and try again.")
        return creative

    @staticmethod
    def _check_track(library: dict, track_id: str) -> dict:
        track = library.get(track_id)
        if track is None:
            raise AdsError("That recording isn't in your library. Import it as an ad first, then pick it.")
        if track.get("kind") != "ad":
            raise AdsError(f"\"{track.get('title', 'That recording')}\" is in your library as {track.get('kind')}, not as an ad. "
                           "Import it again as an ad to use it in a spot.")
        return track

    @staticmethod
    def _settle_recordings(old: Campaign, new: Campaign):
        """Decide which voice recordings stay on an edited spot. A recording is the voice reading one exact text, so it
        only stays while the words it was made from (the sponsorship line, with the sponsor's name, and the copy) and
        the voice are the same. That does not depend on the sender sending the recording back: leaving it out of a
        title-only edit keeps it; sending it back with changed words, or after the sponsor was renamed, takes it off.
        A different recording named by the sender is a new recording and is kept. Returns (campaign, warnings)."""
        before = {x.id: x for x in old.creatives}
        creatives, notes = [], []
        for x in new.creatives:
            was = before.get(x.id)
            if x.spoken and was is not None and was.track_id and (x.track_id is None or x.track_id == was.track_id):
                same = (was.spoken and x.voice in (None, was.voice)
                        and spoken_text(old.advertiser, was.text, was.disclosure) == spoken_text(new.advertiser, x.text, x.disclosure))
                if same:
                    x = dataclasses.replace(x, track_id=was.track_id, voice=was.voice if x.voice is None else x.voice)
                else:
                    x = dataclasses.replace(x, track_id=None)
                    notes.append(f"The words for \"{x.title}\" changed, so its voice recording was taken off. "
                                 "Make a new recording for the new words.")
            creatives.append(x)
        return dataclasses.replace(new, creatives=tuple(creatives)), notes

    @_guard
    def _op_campaign_save(self, ctx, args):
        self._need_open()
        raw = dict(args["campaign"])
        view = self._station_view(0.0)
        now = self._clock()
        settings = self._settings()
        with self._lock:
            snap = self._snap
            rid = raw.pop("id", None)
            existing, merged = None, raw
            if rid is not None:
                existing = self._find(rid)
                merged = existing.to_dict()
                if "frequency" in raw:
                    if "caps" not in raw:
                        merged.pop("caps", None)
                    if "separation" not in raw:
                        merged.pop("separation", None)
                advertiser = raw.get("advertiser")
                if isinstance(advertiser, dict) and "category" in advertiser and "category" not in raw:
                    merged.pop("category", None)
                merged.update(raw)
                merged["id"] = rid
            elif self._store.count_active() >= MAX_CAMPAIGNS:
                raise AdsError(f"You have {MAX_CAMPAIGNS} spots, which is the most this station keeps. Remove one first.")
            zone_name = merged.get("timezone", settings["timezone"])
            today = local_datetime(now, zone_for(zone_name)).date()
            station_name = view.name[:MAX_ADVERTISER]
            campaign, warnings = parse_campaign(
                merged, today=today, timezone=settings["timezone"], station_name=station_name, campaign_id=rid,
                own_creatives=frozenset(x.id for x in existing.creatives) if existing else frozenset(),
                paused=existing.paused if existing else False)
            # "Station promo" is a statement about who the spot is for. It is how a spot skips the paid-use confirmation, so
            # it may only carry the station's own name: a business cannot be filed as a promo, new or by editing a paid spot.
            # (A promo saved under an earlier station name keeps that name while it is not changed.)
            unchanged = existing is not None and existing.house and campaign.advertiser == existing.advertiser
            if campaign.house and not (unchanged or house_name_ok(campaign.advertiser, station_name)):
                raise AdsError(HOUSE_NAME_MESSAGE)
            acks = snap.acks
            if campaign.paid and not acks["commercial_use"]["acknowledged"]:
                raise AdsError(NEEDS_COMMERCIAL_USE)
            if campaign.has_script and not acks["voice_rights"]["acknowledged"]:
                raise AdsError(NEEDS_VOICE_RIGHTS)
            if existing is not None:
                campaign, notes = self._settle_recordings(existing, campaign)
                warnings.extend(notes)
            creatives = []
            for creative in campaign.creatives:
                if creative.track_id:
                    track = self._check_track(view.library, creative.track_id)
                    owner = snap.track_map.get(creative.track_id)
                    if owner and owner[0] != campaign.id:
                        raise AdsError(f"The recording \"{track.get('title')}\" is already used by \"{snap.by_id[owner[0]].name}\". "
                                       "Import a second copy if you want to use it twice.")
                    if not creative.title and not creative.spoken:
                        creative = dataclasses.replace(creative, title=str(track.get("title") or "Recording")[:60])
                if not creative.title:
                    creative = dataclasses.replace(creative, title="Recording")
                creatives.append(creative)
            campaign = dataclasses.replace(campaign, creatives=tuple(creatives))
            ack_id = acks["commercial_use"]["ack_id"] if campaign.paid else None
            self._store.save_campaign(campaign, now=now, commercial_ack_id=ack_id)
            self._reload()
            saved = self._snap.by_id[campaign.id]
        return {"created": existing is None, "warnings": warnings, "campaign": self._campaign_view(saved, now, view)}

    @_guard
    def _op_campaign_get(self, ctx, args):
        self._need_open()
        campaign = self._find(args["campaign"])
        return {"campaign": self._campaign_view(campaign, self._clock(), self._station_view(LIBRARY_MAX_AGE))}

    def _set_paused(self, campaign_id, paused: bool) -> dict:
        self._need_open()
        with self._lock:
            campaign = self._find(campaign_id)
            self._store.set_paused(campaign.id, paused, self._clock())
            self._reload()
            now_state = campaign_state(self._snap.by_id[campaign.id], self._clock())
        return {"campaign": campaign.id, "name": campaign.name, "state": now_state}

    @_guard
    def _op_campaign_pause(self, ctx, args):
        return self._set_paused(args["campaign"], True)

    @_guard
    def _op_campaign_resume(self, ctx, args):
        return self._set_paused(args["campaign"], False)

    @_guard
    def _op_campaign_remove(self, ctx, args):
        self._need_open()
        with self._lock:
            campaign = self._find(args["campaign"])
            self._store.remove(campaign.id, self._clock())
            self._reload()
        return {"campaign": campaign.id, "name": campaign.name, "removed": True}

    # -- breaks ---------------------------------------------------------------------------------------------------
    def _parse_at(self, value, now: float) -> float:
        if value is None:
            return now
        sample = "Use a time like 2026-12-01T09:00:00+00:00, with the timezone offset."
        try:
            parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise AdsError(sample)
            ts = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            raise AdsError(sample) from None
        if not now - 3600 <= ts <= now + 2 * 86400:
            raise AdsError("Plan a break from an hour ago to two days ahead.")
        return ts

    def _break_context(self, at: float, now: float, show_id, ready_only: bool, view: _StationView) -> BreakContext:
        snap, settings = self._snap, self._settings()
        with self._lock:
            rows = self._store.recent_plays(at - HISTORY_DAYS * 86400, at + 86400)
            planned = self._store.active_planned(now, PLANNED_GRACE)
            totals = dict(self._store.delivered_totals())
        history = [Play(r["campaign_id"], r["creative_id"], r["started_at"], r["category"], r["outcome"] == "interrupted")
                   for r in rows]
        for r in planned:
            owner = snap.by_id.get(r["campaign_id"])
            history.append(Play(r["campaign_id"], r["creative_id"], r["planned_at"], owner.category if owner else None))
            totals[r["campaign_id"]] = totals.get(r["campaign_id"], 0) + 1
        audio = {}
        for campaign in snap.campaigns:
            for creative in campaign.creatives:
                track = view.library.get(creative.track_id) if creative.track_id else None
                if track is not None and isinstance(track.get("duration"), (int, float)):
                    audio[creative.track_id] = Audio(float(track["duration"]), bool(track.get("available", True)))
        return BreakContext(campaigns=snap.campaigns, history=history, delivered_totals=totals, audio=audio,
                            show_id=show_id, max_per_break=settings["max_per_break"], max_per_hour=settings["max_per_hour"],
                            voice_rights=snap.acks["voice_rights"]["acknowledged"],
                            commercial_use=snap.acks["commercial_use"]["acknowledged"], ready_only=ready_only)

    def _plan(self, seconds, show_id, at_text, seed, ready_only, view: _StationView, now: float):
        if show_id is not None and (not isinstance(show_id, str) or not SHOW_RE.fullmatch(show_id)):
            raise AdsError("A show id is lowercase letters, numbers, dashes or underscores.")
        at = self._parse_at(at_text, now)
        if not at < MAX_TIMESTAMP:
            raise AdsError("That time is outside the range the station can handle.")
        context = self._break_context(at, now, show_id, ready_only, view)
        seed = seed if seed else f"{show_id or ''}|{int(at // 300)}"
        return build_break(at, seconds, context, seed)

    def _record(self, plan, now: float) -> str:
        """Remember a break the station really built: planned plays count toward the next break's
        limits until they air, and the reasons spots were held back feed the reports."""
        break_id = uuid.uuid4().hex
        rows = [(i.campaign_id, i.creative_id, i.track_id, plan.at + i.offset) for i in plan.items if i.track_id]
        held = []
        for skip in plan.skipped:
            owner = self._snap.by_id.get(skip.campaign_id)
            # Only a spot that was left out counts. A message that could not play (its recording is not made yet) is
            # listed in the preview, but the spot may still have played through its other message.
            if skip.structural or skip.creative_id is not None or owner is None:
                continue
            try:
                day = local_datetime(plan.at, zone_for(owner.timezone)).date().isoformat()
            except AdsError:
                continue  # a spot with an unknown timezone has no days to count held-back plays against
            held.append((day, skip.campaign_id, skip.reason))
        with self._lock:
            if rows:
                self._store.add_planned(break_id, rows, now)
            if held:
                self._store.bump_skips(held)
        return break_id

    @_guard
    def _op_break_build(self, ctx, args):
        self._need_open()
        view = self._station_view(0.0)
        now = self._clock()
        record = bool(args.get("record", False))
        # A break that will be remembered is chosen and recorded in one step, so two callers can never both
        # take the same spot; a look at the next break does not hold anyone up.
        with (self._lock if record else contextlib.nullcontext()):
            plan = self._plan(args["seconds"], args.get("show_id"), args.get("at"), args.get("seed"),
                              args.get("ready_only", True), view, now)
            break_id = self._record(plan, now) if record else None
        out = plan.to_dict(skipped_limit=30)
        out.update(break_id=break_id, recorded=record)
        return out

    def _route_break(self, ctx, request: Request):
        self._need_open()
        try:
            seconds = float(_first(request.query, "seconds", 60))
        except ValueError:
            raise AdsError("Break length must be a number of seconds.") from None
        if not math.isfinite(seconds) or not 1 <= seconds <= 600:
            raise AdsError("Break length must be between 1 and 600 seconds.")
        view = self._station_view(LIBRARY_MAX_AGE)
        now = self._clock()
        ready_only = _first(request.query, "ready_only", "true").lower() not in ("false", "0", "no")
        plan = self._plan(seconds, _first(request.query, "show_id"), _first(request.query, "at"), None, ready_only, view, now)
        out = plan.to_dict()
        out.update(break_id=None, recorded=False)
        return out

    # -- the voice ----------------------------------------------------------------------------------------------------
    def _voice_gate(self, campaign: Campaign) -> None:
        acks = self._snap.acks
        if not acks["voice_rights"]["acknowledged"]:
            raise AdsError(NEEDS_VOICE_RIGHTS)
        if campaign.paid and not acks["commercial_use"]["acknowledged"]:
            raise AdsError(NEEDS_COMMERCIAL_USE)
        if not sponsor_name_ok(campaign.advertiser):  # a spot saved before names were checked; nobody would be named
            raise AdsError(NAMELESS_STATION_MESSAGE if campaign.house else NAMELESS_MESSAGE)

    @_guard
    def _op_spot_speech(self, ctx, args):
        self._need_open()
        campaign = self._find(args["campaign"])
        creative = self._find_creative(campaign, args["creative"])
        if not creative.spoken:
            raise AdsError("That message is a recording, so there is nothing for the voice to read.")
        self._voice_gate(campaign)
        text = spoken_text(campaign.advertiser, creative.text, creative.disclosure)
        return {"role": ROLE, "campaign": campaign.id, "creative": creative.id, "sponsor": campaign.advertiser, "text": text,
                "voice": creative.voice, "estimated_seconds": estimate_seconds(text), "includes_sponsorship_line": True}

    @_guard
    def _op_spot_audio(self, ctx, args):
        self._need_open()
        view = self._station_view(0.0)
        with self._lock:
            campaign = self._find(args["campaign"])
            creative = self._find_creative(campaign, args["creative"])
            if not creative.spoken:
                raise AdsError("Only written messages get a voice recording attached. To change a recorded spot, save it with a "
                               "different recording.")
            self._voice_gate(campaign)
            track = self._check_track(view.library, args["track_id"])
            owner = self._snap.track_map.get(args["track_id"])
            if owner and owner[1] != creative.id:
                raise AdsError("That recording is already used by another spot.")
            self._store.set_creative_track(campaign.id, creative.id, args["track_id"], self._clock())
            self._reload()
        return {"campaign": campaign.id, "creative": creative.id, "track_id": args["track_id"],
                "duration": track.get("duration")}

    # ---- views and reports ------------------------------------------------------------------------------------------------
    def _creative_view(self, campaign: Campaign, creative, view: Optional[_StationView], acks: dict) -> dict:
        problem, ready = None, True
        track = None
        if creative.spoken and not acks["voice_rights"]["acknowledged"]:
            problem, ready = "Waiting for your voice-rights confirmation.", False
        elif creative.track_id and view is not None:
            track = view.library.get(creative.track_id)
            if track is None:
                problem, ready = "The recording is no longer in your library.", False
            elif not track.get("available", True):
                problem, ready = "The recording can't be played right now. Check it in the Library.", False
        elif not creative.track_id:
            problem, ready = ("The voice recording isn't made yet." if creative.spoken else "No recording chosen."), False
        out = {"id": creative.id, "kind": creative.kind, "title": creative.title, "track_id": creative.track_id,
               "ready": ready, "problem": problem, "duration": track.get("duration") if track else None}
        if creative.spoken:
            text = spoken_text(campaign.advertiser, creative.text, creative.disclosure)
            out.update(text=creative.text, disclosure=creative.disclosure, voice=creative.voice, spoken_text=text,
                       estimated_seconds=estimate_seconds(text))
        return out

    def _campaign_view(self, campaign: Campaign, now: float, view: Optional[_StationView], plays7=None, last=None) -> dict:
        if plays7 is None:
            plays7, last = self._store.plays_since(now - 7 * 86400), self._store.last_plays()
        acks = self._snap.acks
        try:
            state = campaign_state(campaign, now)
        except AdsError:
            state = "ended"
        data = campaign.to_dict()
        data.update(state=state, schedule_text=schedule_text(campaign), plays_last_7_days=plays7.get(campaign.id, 0),
                    last_played_at=_iso(last.get(campaign.id)), created_at=_iso(campaign.created_at),
                    creatives=[self._creative_view(campaign, x, view, acks) for x in campaign.creatives])
        waits = []
        try:
            zone_for(campaign.timezone)
            data["problem"] = None
        except AdsError:
            data["problem"] = "This spot uses a timezone this computer doesn't know. Edit it and pick another."
        if campaign.paid and not acks["commercial_use"]["acknowledged"]:
            waits.append("Waiting for your paid-use confirmation.")
        if campaign.has_script and not acks["voice_rights"]["acknowledged"]:
            waits.append("Waiting for your voice-rights confirmation.")
        data["waiting_for"] = waits
        return data

    def _route_campaigns(self, ctx, request: Request):
        self._need_open()
        view = self._station_view(LIBRARY_MAX_AGE)
        now = self._clock()
        wanted = _first(request.query, "campaign")
        with self._lock:
            chosen = [self._find(wanted)] if wanted else list(self._snap.campaigns)
            plays7, last = self._store.plays_since(now - 7 * 86400), self._store.last_plays()
            views = [self._campaign_view(c, now, view, plays7, last) for c in chosen]
        settings = self._settings()
        return {"campaigns": views, "now": _iso(now), "acknowledgements": self._ack_public(self._snap.acks),
                "limits": {"max_spots_per_break": settings["max_per_break"], "max_spots_per_hour": settings["max_per_hour"],
                           "max_spots": MAX_CAMPAIGNS}, "timezone": settings["timezone"], "problem": self._problem}

    def _build_report(self, campaign_id, first, last, **limits) -> dict:
        self._need_open()
        self._station_view(0.0)  # a fresh look, which also catches up on plays the live signal missed
        first = parse_date(first, "The first day") if first else None
        last = parse_date(last, "The last day") if last else None
        if campaign_id is not None and (not isinstance(campaign_id, str) or not ID_RE.fullmatch(campaign_id)):
            raise AdsError(NOT_FOUND)
        with self._lock:
            campaigns = self._store.load(include_removed=True)
            return build_report(self._store, campaigns, now=self._clock(), first=first, last=last,
                                campaign_id=campaign_id, **limits)

    def _route_report(self, ctx, request: Request):
        return self._build_report(_first(request.query, "campaign"), _first(request.query, "from"), _first(request.query, "to"))

    def _route_report_csv(self, ctx, request: Request):
        report = self._build_report(_first(request.query, "campaign"), _first(request.query, "from"), _first(request.query, "to"))
        return Response(report_csv(report), 200, "text/csv; charset=utf-8")

    @_guard
    def _op_report(self, ctx, args):
        # A tool result is read by a model, so it stays small: at most 200 spot-days (about 40 KB), not the 20,000 of the route.
        return self._build_report(args.get("campaign"), args.get("from_date"), args.get("to_date"), max_days=31, max_cells=200)

    # ---- the two confirmations (routes only) ------------------------------------------------------------------------------------
    def _terms_view(self) -> dict:
        acks = self._ack_public(self._snap.acks)
        out = {"version": TERMS_VERSION}
        for kind in KINDS:
            out[kind] = {"title": TERMS[kind]["title"], "text": TERMS[kind]["text"], **acks[kind]}
        return out

    def _route_terms(self, ctx, request: Request):
        self._need_open()
        return self._terms_view()

    def _route_acknowledge(self, ctx, request: Request):
        self._need_open()
        body = request.json
        if not isinstance(body, dict) or not body:
            raise AdsError("Send the confirmation as JSON, for example {\"voice_rights\": true}.")
        extra = set(body) - set(KINDS) - {"version"}
        if extra:
            raise AdsError("You can confirm voice_rights and commercial_use, and nothing else.")
        if not any(kind in body for kind in KINDS):
            raise AdsError("Say which confirmation you are making: voice_rights or commercial_use.")
        if any(body.get(kind) is True for kind in KINDS) and "version" not in body:
            # A confirmation says "I read this wording", so the screen has to say which wording it showed. Taking a
            # confirmation back needs no such proof.
            raise AdsError("To confirm, the screen must say which version of the note it showed. "
                           "Reload the page, read the note again and confirm.")
        if "version" in body and (type(body["version"]) is not int or body["version"] != TERMS_VERSION):
            raise AdsError("The wording of these notes has changed. Reload the page and read them again.")
        changes = {}
        for kind in KINDS:
            if kind in body:
                if not isinstance(body[kind], bool):
                    raise AdsError(f"{kind} must be true (confirm) or false (withdraw).")
                changes[kind] = body[kind]
        now = self._clock()
        with self._lock:
            for kind, value in changes.items():
                self._store.set_ack(kind, value, now)
            self._reload()
        return {"ok": True, **self._terms_view()}

    # ---- routes ------------------------------------------------------------------------------------------------------------------------
    def routes(self):
        def wrap(handler):
            @functools.wraps(handler)
            def run(ctx, request):
                try:
                    return handler(ctx, request)
                except AdsError as exc:
                    raise ExtensionError(str(exc)) from None
                except sqlite3.Error:
                    self._problem = "The spots list could not be read or saved. Check free disk space and try again."
                    raise ExtensionError(self._problem) from None
            return run

        return [Route("GET", "/campaigns", wrap(self._route_campaigns)),
                Route("GET", "/break", wrap(self._route_break)),
                Route("GET", "/report", wrap(self._route_report)),
                Route("GET", "/report.csv", wrap(self._route_report_csv)),
                Route("GET", "/acknowledge", wrap(self._route_terms)),
                Route("POST", "/acknowledge", wrap(self._route_acknowledge), max_body=1024)]


def _zone_known(name: str) -> bool:
    try:
        zone_for(name)
        return True
    except AdsError:
        return False


def _parse_history_time(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def build() -> AdsExtension:
    return AdsExtension()
