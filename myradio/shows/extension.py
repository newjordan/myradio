"""The shows extension: clocks, shows and the planner for myradio.

It plugs into the station through the extension seam and nothing else: core playout never calls it,
it reads no station state, and its schedule lives in its own file. What it offers:

* operations  shows.list, shows.save, shows.remove, shows.apply_template, shows.set_timezone, shows.plan
* routes      GET /week, GET /plan?hours=N, GET /clock?show_id=ID, GET /conflicts
* status      the show on air now, when it next changes, counts and any overlaps (a cached snapshot)
* in-process  ``week()``, ``plan()`` and ``airing()`` for code that holds the extension instance
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..extensions import Arg, Extension, ExtensionError, Operation, Request, Response, Route
from .clocks import MAX_SEED, TEMPLATES, clock_notes, hour_plan, summarize
from .conflicts import (day_view, find_gaps, find_overlaps, overlap_message, report, suggest_fixes,
                        would_overlap_message)
from .labels import WEEK_MIN, mmss
from .model import (DEFAULT_SHOW_ID, MAX_NAME, MAX_SHOWS, MIN_LENGTH, Show, ShowError, clean_clock, clean_slug,
                    clean_talk)
from .planner import ROLE_KIND, plan as make_plan
from .starter import TEMPLATE_NAMES, WEEK_TEMPLATES
from .store import DB_FILE, Store, open_database
from .timeline import DAY, HOUR, check_timestamp, check_zone, parse_moment, to_local_iso
from .week import Airing, Week

MAX_PLAN_HOURS = 168
DEFAULT_PLAN_HOURS = 24
TALK_LEVELS = ("rare", "some", "often")
TIMEZONE_HELP = ("This computer has no time zone information, so shows cannot be placed on the clock. "
                 "Install your system's time zone data (the tzdata package) and restart myradio.")


def detect_system_timezone(environ=None, localtime: str = "/etc/localtime",
                           timezone_file: str = "/etc/timezone") -> Optional[str]:
    """Best-effort name of this computer's time zone, or None. Works on Linux and macOS: it looks at the
    TZ variable, at where /etc/localtime points, and at /etc/timezone. Nothing is guessed from abbreviations."""
    env = os.environ if environ is None else environ
    candidates = []
    tz = str(env.get("TZ", "")).strip()
    if tz:
        candidates.append(tz[1:] if tz.startswith(":") else tz)
    try:
        parts = os.readlink(localtime).replace("\\", "/").split("/")
    except OSError:
        parts = []
    for marker in ("zoneinfo", "zoneinfo.default"):
        if marker in parts:
            candidates.append("/".join(parts[len(parts) - parts[::-1].index(marker):]))
    try:
        with open(timezone_file, "r", encoding="utf-8", errors="ignore") as handle:
            candidates.append(handle.readline(100).strip())
    except OSError:
        pass
    for name in candidates:
        try:
            return check_zone(name)
        except ShowError:
            continue
    return None


@dataclass
class Snapshot:
    """One consistent view of the week. Replaced as a whole after every change; readers never lock."""
    week: Week
    revision: int
    seed: int
    zone_source: str
    problems: tuple
    overlap_count: int
    overlap_notes: tuple
    gap_hours: float
    cache: Optional[tuple] = field(default=None, repr=False)     # (valid_from, valid_to, status dict)


def _seconds(value: Optional[int]) -> Optional[float]:
    """Epoch seconds as floats at the edges, as the shared vocabulary says (None stays None)."""
    return None if value is None else float(value)


def analyze(week: Week) -> tuple:
    overlaps, _ = find_overlaps(week.shows)
    gap_minutes = sum(g.length for g in find_gaps(week.shows)) if week.shows else WEEK_MIN
    return len(overlaps), tuple(overlap_message(o) for o in overlaps[:3]), round(gap_minutes / 60, 1)


class ShowsExtension(Extension):
    name = "shows"

    def __init__(self, *, timezone: Optional[str] = None, detector: Callable[[], Optional[str]] = detect_system_timezone):
        self._timezone_arg = timezone
        self._detector = detector
        self._ctx = None
        self._store: Optional[Store] = None
        self._snap: Optional[Snapshot] = None
        self._write_lock = threading.Lock()

    # ---- lifecycle ----------------------------------------------------------------------------------
    def start(self, ctx) -> None:
        self._ctx = ctx
        now = float(ctx.clock())
        conn, note = open_database(ctx, now)
        store = Store(conn)
        try:
            store.bootstrap(self._first_timezone, now)
            loaded = store.load()
        except BaseException:
            store.close()
            raise
        self._store = store
        notes = ((note,) if note else ()) + loaded.problems
        week = loaded.week
        self._snap = self._snapshot(week, loaded.revision, loaded.seed, loaded.zone_source, notes)

    def stop(self) -> None:
        with self._write_lock:
            store, self._store = self._store, None
        if store is not None:
            store.close()

    def _first_timezone(self) -> tuple:
        """The zone a brand-new file starts with: the one given, else the computer's, else UTC."""
        if self._timezone_arg:
            return check_zone(self._timezone_arg), "configured"
        try:
            found = self._detector()
            if found:
                return check_zone(found), "detected"
        except Exception:                      # a failing or lying detector just means "unknown"
            pass
        try:
            return check_zone("UTC"), "fallback"
        except ShowError:
            raise ExtensionError(TIMEZONE_HELP) from None

    def persistent_files(self):
        return [DB_FILE]

    # ---- the in-process API (for code that holds this instance) ---------------------------------------
    def week(self) -> Optional[Week]:
        snap = self._snap
        return snap.week if snap else None

    def plan(self, from_ts, to_ts, seed: Optional[int] = None) -> list:
        snap = self._need()
        return make_plan(snap.week, from_ts, to_ts, snap.seed if seed is None else seed)

    def airing(self, ts=None) -> Airing:
        return self._need().week.airing(self._now() if ts is None else ts)

    # ---- helpers --------------------------------------------------------------------------------------
    def _now(self) -> float:
        return float(self._ctx.clock())

    def _need(self) -> Snapshot:
        snap = self._snap
        if snap is None or self._store is None:
            raise ExtensionError("The shows service is not running.")
        return snap

    def _snapshot(self, week: Week, revision: int, seed: int, source: str, problems: tuple) -> Snapshot:
        count, notes, gap_hours = analyze(week)
        return Snapshot(week, revision, seed, source, problems, count, notes, gap_hours)

    def _commit(self, write: Callable[[Store], int], week: Week, *, source: Optional[str] = None) -> Snapshot:
        """Write to the file, then replace the snapshot. Only called with the write lock held."""
        snap = self._need()
        analysis = analyze(week)                                  # computed first, so nothing can fail after the write
        try:
            revision = write(self._store)
        except sqlite3.Error:
            raise ExtensionError("Your change could not be saved, so nothing was changed. Check that the disk has "
                                 "free space and try again.") from None
        fresh = Snapshot(week, revision, snap.seed, source or snap.zone_source, snap.problems, *analysis)
        self._snap = fresh
        return fresh

    # ---- operations -------------------------------------------------------------------------------------
    def operations(self):
        return [
            Operation("list", "List the weekly schedule: every show (name, days, start, end, style, host, talk level), "
                      "the default show, the time zone and any overlaps. Read-only.", {}, self._op_list),
            Operation("save", "Create or change a show. Send a whole show, or an id plus only the fields to change. "
                      "Shows may not overlap; a gap plays the default show.",
                      {"show": Arg("object", f"Fields: id, name (up to {MAX_NAME} characters), days (like 1,2,3,4,5; Monday is 1), "
                                   "start and end (HH:MM; an end of 24:00, or earlier than the start, runs to or past midnight), "
                                   "style {palettes, tags, energy, adventurousness, vocals; 0-100}, host_id, talk (rare, some, "
                                   "often), spots_allowed, updates_allowed, update_researcher_id, color, clock (classic, "
                                   "music-heavy, talky).")}, self._op_save),
            Operation("remove", "Remove a show by id. Its hours then play the default show.",
                      {"id": Arg("string", "The id of the show to remove.", max_length=63)}, self._op_remove),
            Operation("apply_template", "Replace the whole week with a ready-made one: starter (five shows through the day) or "
                      "empty (no scheduled shows). If the week already has shows, set replace to true to confirm.",
                      {"name": Arg("string", "starter or empty.", enum=TEMPLATE_NAMES, max_length=20),
                       "replace": Arg("boolean", "True to replace the shows you already have.", required=False)},
                      self._op_apply_template),
            Operation("set_timezone", "Set the station's time zone as an IANA name such as Europe/London. Show times are "
                      "clock times in this zone.", {"timezone": Arg("string", "For example America/New_York.", max_length=64)},
                      self._op_set_timezone),
            Operation("plan", "Plan the next hours of airtime: ordered items (id, start, role, show, host, estimated seconds) "
                      "that the station prepares ahead. Read-only; the same seed always gives the same plan.",
                      {"hours": Arg("integer", f"How many hours ahead, 1 to {MAX_PLAN_HOURS}. Default 3.", required=False,
                                    minimum=1, maximum=MAX_PLAN_HOURS),
                       "from_ts": Arg("number", "Start, in seconds since 1970. Default: now.", required=False, minimum=0),
                       "seed": Arg("integer", "Shuffle seed. Default: the station's saved seed.", required=False, minimum=0)},
                      self._op_plan),
        ]

    def _op_list(self, ctx, args):
        return self._week_payload(self._need(), self._now(), days=False, fixes=False)

    def _op_save(self, ctx, args):
        data = args["show"]
        with self._write_lock:
            snap = self._need()
            week = snap.week
            base = None
            if isinstance(data, dict) and "id" in data:
                base = week.get(clean_slug(data["id"], "show id"))
            everyone = week.all_shows()
            show = Show.parse(data, base, taken_ids={s.id for s in everyone}, taken_colors=[s.color for s in everyone])
            if base is None and len(week.shows) >= MAX_SHOWS:
                raise ShowError(f"You already have {MAX_SHOWS} shows, which is the most myradio plans for. "
                                "Remove one before adding another.")
            candidate = week.with_show(show)
            if not show.is_default:
                clashes = [o for o in find_overlaps(candidate.shows)[0] if show.id in (o.first.id, o.second.id)]
                if clashes:
                    raise ShowError(would_overlap_message(show, clashes[0], suggest_fixes(candidate, clashes[0]),
                                                          extra=len(clashes) - 1))
            now = self._now()
            fresh = self._commit(lambda store: store.save_show(show, now), candidate)
        notes = []
        if show.crosses_midnight:
            notes.append("This show runs past midnight, so it carries on into the next day.")
        if not show.host_id:
            notes.append("No host is chosen, so this show has no talk breaks.")
        return {"show": show.to_dict(), "created": base is None, "revision": fresh.revision,
                "gap_hours": fresh.gap_hours, "notes": notes,
                "summary": f"Saved {show.name} ({show.when()})."}

    def _op_remove(self, ctx, args):
        show_id = clean_slug(args["id"], "show id")
        if show_id == DEFAULT_SHOW_ID:
            raise ShowError("The default show can't be removed. It plays whenever no other show is scheduled; "
                            "change its sound or host instead.")
        with self._write_lock:
            snap = self._need()
            show = snap.week.get(show_id)
            if show is None:
                raise ShowError(f"There is no show with the id {show_id}. Check the list of shows for the right id.")
            fresh = self._commit(lambda store: store.delete_show(show_id), snap.week.without(show_id))
        return {"removed": show_id, "name": show.name, "revision": fresh.revision,
                "summary": f"Removed {show.name}. Its hours now play {fresh.week.default.name}, your default show, "
                           "until you add another show."}

    def _op_apply_template(self, ctx, args):
        template = WEEK_TEMPLATES.get(args["name"])
        if template is None:
            raise ShowError("There is no ready-made week with that name. Choose one of: " + ", ".join(TEMPLATE_NAMES) + ".")
        with self._write_lock:
            snap = self._need()
            existing = len(snap.week.shows)
            if existing and not args.get("replace", False):
                raise ShowError(f"Your week already has {existing} show{'s' if existing != 1 else ''}, and applying "
                                f"'{template.title}' would replace {'them' if existing != 1 else 'it'}. "
                                "Ask again with replace set to true to confirm.")
            shows = template.build()
            candidate = snap.week.with_shows(shows)
            now = self._now()
            fresh = self._commit(lambda store: store.replace_shows(shows, now), candidate)
        return {"template": template.name, "shows": [s.id for s in shows], "revision": fresh.revision,
                "summary": (f"Applied '{template.title}': {len(shows)} show{'s' if len(shows) != 1 else ''}." if shows
                            else "Cleared the schedule. Your default show now plays all day.")}

    def _op_set_timezone(self, ctx, args):
        zone = check_zone(args["timezone"])
        with self._write_lock:
            snap = self._need()
            fresh = self._commit(lambda store: store.set_timezone(zone, "chosen"), snap.week.with_timezone(zone),
                                 source="chosen")
        return {"timezone": zone, "revision": fresh.revision,
                "summary": f"Shows now follow {zone} time. Each show keeps its clock times, so 6 am stays 6 am."}

    def _op_plan(self, ctx, args):
        snap = self._need()
        hours = args.get("hours", 3)
        start = check_timestamp(args["from_ts"], "start") if "from_ts" in args else check_timestamp(self._now(), "time")
        seed = args.get("seed", snap.seed)
        items = make_plan(snap.week, start, start + hours * HOUR, seed)
        zone = snap.week.timezone
        return {"from": float(start), "to": float(start + hours * HOUR), "from_iso": to_local_iso(zone, start),
                "to_iso": to_local_iso(zone, start + hours * HOUR), "timezone": zone, "seed": seed,
                "revision": snap.revision, "items": [item.to_dict(zone) for item in items]}

    # ---- routes ------------------------------------------------------------------------------------------
    def routes(self):
        return [Route("GET", "/week", self._route_week), Route("GET", "/plan", self._route_plan),
                Route("GET", "/clock", self._route_clock), Route("GET", "/conflicts", self._route_conflicts)]

    @staticmethod
    def _param(request: Request, name: str) -> Optional[str]:
        values = request.query.get(name) or []
        if not values:
            return None
        value = str(values[0])
        if len(value) > 100:
            raise ShowError(f"The {name} value is too long.")
        return value

    def _int_param(self, request: Request, name: str, default: Optional[int], low: int, high: int) -> Optional[int]:
        text = self._param(request, name)
        if text is None or not text.strip():
            return default
        value = int(text.strip()) if re.fullmatch(r"[0-9]{1,18}", text.strip()) else None
        if value is None or not low <= value <= high:
            raise ShowError(f"The {name} must be a whole number from {low} to {high}.")
        return value

    def _route_week(self, ctx, request):
        return self._week_payload(self._need(), self._now(), days=True, fixes=True)

    def _route_conflicts(self, ctx, request):
        return report(self._need().week)

    def _route_plan(self, ctx, request):
        snap = self._need()
        hours = self._int_param(request, "hours", DEFAULT_PLAN_HOURS, 1, MAX_PLAN_HOURS)
        zone = snap.week.timezone
        text = self._param(request, "from")
        start = parse_moment(text, zone, "start") if text else check_timestamp(self._now(), "time")
        seed = self._int_param(request, "seed", snap.seed, 0, MAX_SEED)
        items = make_plan(snap.week, start, start + hours * HOUR, seed)
        return {"from": float(start), "to": float(start + hours * HOUR), "from_iso": to_local_iso(zone, start),
                "to_iso": to_local_iso(zone, start + hours * HOUR), "timezone": zone, "hours": hours, "seed": seed,
                "revision": snap.revision, "count": len(items), "items": [item.to_dict(zone) for item in items]}

    def _flag(self, request: Request, name: str) -> Optional[bool]:
        text = self._param(request, name)
        if text is None:
            return None
        if text.strip().lower() in ("1", "true", "yes", "on"):
            return True
        if text.strip().lower() in ("0", "false", "no", "off"):
            return False
        raise ShowError(f"The {name} setting must be true or false.")

    def _route_clock(self, ctx, request):
        """The example hour of a saved show. Optional query settings (talk, host_id, spots_allowed, updates_allowed,
        template, seed) show what the hour would look like if they were changed, without saving anything."""
        snap = self._need()
        show_id = self._param(request, "show_id")
        if not show_id:
            raise ShowError("Choose a show to preview, for example /clock?show_id=morning.")
        show = snap.week.get(clean_slug(show_id, "show id"))
        if show is None:
            return Response({"error": "There is no show with that id. Check the list of shows for the right id."}, 404)
        changes, overrides = {}, {}
        if self._param(request, "talk"):
            changes["talk"] = overrides["talk"] = clean_talk(self._param(request, "talk"))
        if self._param(request, "host_id"):
            text = self._param(request, "host_id").strip().lower()
            changes["host_id"] = None if text in ("none", "null", "no") else clean_slug(text, "host id")
            overrides["host_id"] = changes["host_id"]
        for name in ("spots_allowed", "updates_allowed"):
            flag = self._flag(request, name)
            if flag is not None:
                changes[name] = overrides[name] = flag
        if changes.get("updates_allowed") is False:
            changes["update_researcher_id"] = None
        template = clean_clock(self._param(request, "template")) if self._param(request, "template") else show.clock
        if template != show.clock:
            overrides["template"] = template
        show = show.replace(**changes) if changes else show
        seed = self._int_param(request, "seed", None, 0, MAX_SEED)
        items = hour_plan(show, seed, template=template)
        offset, rows = 0, []
        for item in items:
            rows.append({"role": item.role, "kind": ROLE_KIND[item.role], "est_seconds": item.est_seconds,
                         "offset_seconds": offset, "length": mmss(item.est_seconds)})
            offset += item.est_seconds
        summary = summarize(items)
        return {"show_id": show.id, "show_name": show.name, "template": template, "template_title": TEMPLATES[template].title,
                "seed": seed, "overrides": overrides, "total_seconds": offset, "item_count": len(rows), "items": rows,
                "summary": summary, "music_seconds": sum(r["seconds"] for r in summary if r["role"] == "song"),
                "notes": clock_notes(show, template), "templates": [t.describe() for t in TEMPLATES.values()]}

    # ---- the week as one document -------------------------------------------------------------------------
    def _now_block(self, snap: Snapshot, now: float) -> dict:
        airing = snap.week.airing(now)
        zone = snap.week.timezone
        out = {"at": float(int(now)), "at_iso": to_local_iso(zone, now), "show_id": airing.show.id, "name": airing.show.name,
               "host_id": airing.show.host_id, "fallback": airing.fallback, "since": _seconds(airing.since),
               "until": _seconds(airing.until),
               "until_iso": to_local_iso(zone, airing.until) if airing.until else None, "next": None}
        if airing.next_show is not None:
            out["next"] = {"show_id": airing.next_show.id, "name": airing.next_show.name, "fallback": airing.next_fallback}
        return out

    def _week_payload(self, snap: Snapshot, now: float, *, days: bool, fixes: bool) -> dict:
        week = snap.week
        payload = {
            "timezone": week.timezone, "timezone_source": snap.zone_source, "revision": snap.revision,
            "plan_seed": snap.seed, "now": self._now_block(snap, now),
            "default_show": week.default.to_dict(), "shows": [s.to_dict() for s in week.shows],
            "conflicts": report(week, with_fixes=fixes),
            "templates": [t.describe() for t in WEEK_TEMPLATES.values()],
            "clock_templates": [t.describe() for t in TEMPLATES.values()],
            "limits": {"max_shows": MAX_SHOWS, "max_name_length": MAX_NAME, "min_minutes": MIN_LENGTH,
                       "talk_levels": list(TALK_LEVELS), "max_plan_hours": MAX_PLAN_HOURS},
            "notes": list(snap.problems),
        }
        if days:
            payload["days"] = day_view(week)
        return payload

    # ---- status and checks ------------------------------------------------------------------------------------
    def status(self) -> dict:
        snap = self._snap
        if snap is None:
            return {"ready": False}
        now = self._now()
        cache = snap.cache
        if cache is None or not cache[0] <= now < cache[1]:
            cache = self._status_cache(snap, now)
            snap.cache = cache
        return cache[2]

    def _status_cache(self, snap: Snapshot, now: float) -> tuple:
        week = snap.week
        zone = week.timezone
        airing = week.airing(now)
        current = {"show_id": airing.show.id, "name": airing.show.name, "host_id": airing.show.host_id,
                   "fallback": airing.fallback, "since": _seconds(airing.since), "until": _seconds(airing.until)}
        following = None
        if airing.until is not None and airing.next_show is not None:
            following = {"at": _seconds(airing.until), "at_iso": to_local_iso(zone, airing.until), "show_id": airing.next_show.id,
                         "name": airing.next_show.name, "fallback": airing.next_fallback}
        value = {"ready": True, "timezone": zone, "timezone_source": snap.zone_source, "revision": snap.revision,
                 "counts": {"shows": len(week.shows), "overlaps": snap.overlap_count, "gap_hours": snap.gap_hours},
                 "current": current, "next_change": following, "conflicts": list(snap.overlap_notes),
                 "notes": list(snap.problems)}
        valid_from = airing.since if airing.since is not None else int(now) - 3600
        valid_to = min(airing.until, int(now) + DAY) if airing.until is not None else int(now) + DAY
        return valid_from, valid_to, value

    def checks(self):
        snap = self._snap
        if snap is None:
            return []
        week = snap.week
        out = []
        if snap.zone_source == "fallback":
            out.append({"id": "timezone", "title": "Station time zone", "status": "warn",
                        "detail": "myradio could not tell which time zone this computer uses, so shows follow UTC.",
                        "hint": "Choose your time zone in Shows so each show starts at the right local time."})
        else:
            out.append({"id": "timezone", "title": "Station time zone", "status": "ok",
                        "detail": f"Shows follow {week.timezone} time.", "hint": ""})
        if snap.overlap_count:
            out.append({"id": "schedule", "title": "Show schedule", "status": "warn",
                        "detail": snap.overlap_notes[0] if snap.overlap_notes else "Two shows overlap.",
                        "hint": "Open Shows and shorten one of the overlapping shows so only one is on air at a time."})
        elif not week.shows:
            out.append({"id": "schedule", "title": "Show schedule", "status": "ok",
                        "detail": f"No shows are scheduled, so {week.default.name} plays all day.",
                        "hint": "Use the ready-made week in Shows to get started, or add your own shows."})
        elif snap.gap_hours:
            out.append({"id": "schedule", "title": "Show schedule", "status": "ok",
                        "detail": f"{len(week.shows)} shows. About {snap.gap_hours:g} hours a week use {week.default.name}, "
                                  "your default show.", "hint": ""})
        else:
            out.append({"id": "schedule", "title": "Show schedule", "status": "ok",
                        "detail": f"{len(week.shows)} shows cover every hour of the week.", "hint": ""})
        if snap.problems:
            out.append({"id": "data", "title": "Saved shows", "status": "warn", "detail": snap.problems[0],
                        "hint": "Check your shows in Shows, and restore a backup if something is missing."})
        return out


def build() -> ShowsExtension:
    """Entry point used by ``myradio.extensions.builtin_extensions()``."""
    return ShowsExtension()
