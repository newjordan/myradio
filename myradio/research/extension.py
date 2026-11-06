"""The research extension: user-owned, interest-first researchers.

``build()`` is what ``myradio.extensions.builtin_extensions()`` calls. A researcher reads only sources its owner chose,
keeps what matches the owner's interests and is recent and new, and leaves a *brief* for the hosts and the director.
When nothing suitable turns up the brief says so, and consumers play music instead of a bulletin. Nothing here calls a language model.

Operations (typed by the host): ``research.save``, ``research.remove``, ``research.pause``, ``research.resume``,
``research.run_now``, ``research.review``. Routes: ``GET /researchers``, ``GET /briefs``, ``GET /sources``, ``GET /tone``,
``POST /sources/check``. Operation results and the status snapshot never contain text taken from the web.
"""
from __future__ import annotations

import os
import ssl
from typing import Optional

from ..extensions import Arg, Context, Extension, ExtensionError, Operation, Request, Route
from .fetcher import Fetcher
from .model import ID, LIMITS
from .selection import MAX_PENALTY, MIN_RELEVANCE, TONE_LEXICON
from .service import PersistentBudget, ResearchService
from .store import Store

DB_NAME = "research.sqlite3"
REVIEWS = ("pending", "approved", "rejected", "auto")
OUTCOMES = ("ready", "nothing_new", "error")
DEFAULTS = {"daily_request_cap": 500, "host_min_interval_seconds": 3.0, "request_timeout_seconds": 20.0, "contact": ""}
# The pause between two requests to one website is at most 30 seconds, which is the longest wait the fetcher will sit through.
RANGES = {"daily_request_cap": (10, 5000), "host_min_interval_seconds": (1.0, 30.0), "request_timeout_seconds": (5.0, 60.0)}
RUN_NOW_WAIT = 8.0

RESEARCHER_HELP = (
    "A researcher object. Needed: name (text), interests (list of words or phrases), sources (list of https addresses, or of "
    "{url, name?, kind?} where kind is reported, commentary or announcement). Optional: id (to change an existing one), purpose, "
    "questions (list), exclude_topics (list), exclude_sources (list of sites or names to skip), cadence ({type:'every_hours', hours:1-168} "
    "or {type:'daily', time:'HH:MM', timezone:'Europe/London', weekdays:'1,2,3,4,5'}), freshness_hours (1-720, default 24), "
    "segment_seconds (15-600, default 60), anchor (the host's short name), review ('before_air' by default, or 'auto'), "
    "tone_filter (true), tone_words, tone_allow, paused.")


def read_settings(config: dict) -> tuple:
    """Operator-written, non-secret tuning from ``config.json``. Out-of-range numbers are clamped, anything unknown is
    ignored (and mentioned in the doctor check). Nothing here can relax a safety rule."""
    settings, notes = dict(DEFAULTS), []
    for key, value in config.items():
        if key in RANGES:
            low, high = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                notes.append(f"{key} should be a number, so the default is used.")
            else:
                settings[key] = min(high, max(low, value))
                if settings[key] != value:
                    notes.append(f"{key} was outside {low:g} to {high:g}, so {settings[key]:g} is used.")
        elif key == "contact":
            if isinstance(value, str) and value.strip():
                settings["contact"] = value.strip()
        else:
            notes.append(f"The setting {str(key)[:40]!r} is not used.")
    settings["daily_request_cap"] = int(settings["daily_request_cap"])
    return settings, notes


class ResearchExtension(Extension):
    name = "research"

    def __init__(self, *, fetcher: Optional[Fetcher] = None, autostart: bool = True, run_now_wait: float = RUN_NOW_WAIT,
                 **service_options):
        # ``fetcher``, ``autostart``, ``run_now_wait`` and ``service_options`` exist so tests can supply a local fixture fetcher
        # and a fast clock. ``build()`` passes nothing, and nothing in config.json or an operation can reach them.
        self._fetcher = fetcher
        self._autostart = autostart
        self._run_now_wait = run_now_wait
        self._options = service_options
        self.service: Optional[ResearchService] = None
        self.ctx: Optional[Context] = None

    # ---- lifecycle ------------------------------------------------------------------------------------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        conn = ctx.open_db(DB_NAME)
        try:
            store = Store(conn)
            notes: list = []
            try:
                config = ctx.config()
            except ExtensionError as error:
                config, notes = {}, [f"config.json was not used: {error}"]
            settings, more = read_settings(config)
            notes.extend(more)
            fetcher = self._fetcher
            if fetcher is None:
                fetcher = Fetcher(contact=settings["contact"], deadline_seconds=settings["request_timeout_seconds"],
                                  host_min_interval=settings["host_min_interval_seconds"],
                                  budget=PersistentBudget(store, settings["daily_request_cap"], ctx.clock))
            options = dict(self._options)
            options.setdefault("daily_cap", settings["daily_request_cap"])
            self.service = ResearchService(store, fetcher, clock=ctx.clock, config_notes=tuple(notes), **options)
            self.service.start(run_thread=self._autostart)
        except BaseException:
            conn.close()
            raise

    def stop(self) -> None:
        if self.service is not None:
            self.service.stop()

    def persistent_files(self):
        return (DB_NAME, "config.json")

    # ---- operations ------------------------------------------------------------------------------------

    def _service(self) -> ResearchService:
        if self.service is None:
            raise ExtensionError("The research service is not running.")
        return self.service

    def operations(self):
        one = {"researcher_id": Arg("string", "The researcher's id", max_length=32)}
        return [
            Operation("save", "Create a researcher, or change one by passing its id. It reads only the sources you list and keeps recent "
                              "items that match your interests. Each update waits for your approval unless you choose auto.",
                      {"researcher": Arg("object", RESEARCHER_HELP)}, self._op_save),
            Operation("remove", "Delete a researcher together with its briefs and memory of what it covered.", one, self._op_remove),
            Operation("pause", "Stop a researcher from running on its schedule. Its settings and briefs are kept.", one, self._op_pause),
            Operation("resume", "Let a paused researcher run on its schedule again.", one, self._op_resume),
            Operation("run_now", "Check a researcher's sources now instead of waiting for its schedule. Returns the outcome if it finishes "
                                 "quickly, otherwise says it is running.", one, self._op_run_now),
            Operation("review", "Approve or reject a researcher's ready update before it can air.",
                      {"brief_id": Arg("string", "The update's id", max_length=32),
                       "decision": Arg("string", "approved or rejected", enum=("approved", "rejected"))}, self._op_review),
        ]

    @staticmethod
    def _id(args: dict, key: str) -> str:
        value = args[key]
        if not ID.fullmatch(value):
            raise ExtensionError("That id does not look right. Use the id shown for the researcher or update.")
        return value

    def _op_save(self, ctx, args):
        return self._service().save(args["researcher"])

    def _op_remove(self, ctx, args):
        rid = self._id(args, "researcher_id")
        self._service().remove(rid)
        return {"removed": rid}

    def _op_pause(self, ctx, args):
        rid = self._id(args, "researcher_id")
        row = self._service().set_paused(rid, True)
        return {"researcher_id": rid, "paused": row["paused"], "state": row["state"]}

    def _op_resume(self, ctx, args):
        rid = self._id(args, "researcher_id")
        row = self._service().set_paused(rid, False)
        return {"researcher_id": rid, "paused": row["paused"], "state": row["state"], "next_run_at": row["next_run_at"]}

    def _op_run_now(self, ctx, args):
        rid = self._id(args, "researcher_id")
        return {"researcher_id": rid, **self._service().run_now(rid, wait=self._run_now_wait)}

    def _op_review(self, ctx, args):
        return self._service().review(self._id(args, "brief_id"), args["decision"])

    # ---- routes ----------------------------------------------------------------------------------------

    def routes(self):
        return [Route("GET", "/researchers", self._get_researchers),
                Route("GET", "/briefs", self._get_briefs),
                Route("GET", "/sources", self._get_sources),
                Route("GET", "/tone", self._get_tone),
                Route("POST", "/sources/check", self._post_check, max_body=4096)]

    def _get_researchers(self, ctx, request: Request):
        return {"researchers": self._service().researchers(), "limits": LIMITS}

    def _get_sources(self, ctx, request: Request):
        return {"sources": self._service().sources()}

    def _get_tone(self, ctx, request: Request):
        """The built-in tone word list, so a screen can show people exactly what is being down-ranked and why."""
        return {
            "explanation": ("Headlines and opening sentences are checked against this plain word list. A match lowers an item's rank; it never "
                            "raises one. It is a list of words, not an understanding of the story, so it can miss bait and can mark a fair story "
                            "down. On each researcher you can add your own words or allow ones you like."),
            "words": [{"phrase": phrase, "weight": weight, "list": label} for phrase, weight, label in TONE_LEXICON],
            "penalty_cap": MAX_PENALTY, "minimum_relevance": MIN_RELEVANCE,
        }

    @staticmethod
    def _query(request: Request, key: str) -> Optional[str]:
        values = request.query.get(key) or []
        return values[0] if values and values[0] != "" else None

    def _get_briefs(self, ctx, request: Request):
        service = self._service()
        researcher = self._query(request, "researcher") or self._query(request, "researcher_id")
        if researcher is not None and not ID.fullmatch(researcher):
            raise ExtensionError("That researcher id does not look right.")
        review, outcome, brief_id = (self._query(request, k) for k in ("review", "outcome", "id"))
        if review is not None and review not in REVIEWS:
            raise ExtensionError("Review must be one of: " + ", ".join(REVIEWS) + ".")
        if outcome is not None and outcome not in OUTCOMES:
            raise ExtensionError("Outcome must be one of: " + ", ".join(OUTCOMES) + ".")
        if brief_id is not None and not ID.fullmatch(brief_id):
            raise ExtensionError("That update id does not look right.")
        raw = self._query(request, "limit")
        try:
            limit = 20 if raw is None else int(raw)
        except ValueError:
            raise ExtensionError("The limit must be a whole number from 1 to 50.") from None
        if not 1 <= limit <= 50:
            raise ExtensionError("The limit must be a whole number from 1 to 50.")
        airable = self._query(request, "airable") in ("1", "true", "yes")
        # "airable" is decided in the database, before the limit is applied: filtering the newest few afterwards would miss a ready
        # update whenever newer "nothing new" runs have pushed it out of the newest few.
        return {"briefs": service.briefs(researcher=researcher, review=review, outcome=outcome, limit=limit, brief_id=brief_id,
                                         airable=airable)}

    def _post_check(self, ctx, request: Request):
        url = (request.json or {}).get("url")
        if not isinstance(url, str) or not url.strip():
            raise ExtensionError("Type the address of a website or feed to check.")
        return self._service().check_source(url)

    # ---- status and checks ---------------------------------------------------------------------------------

    def status(self) -> dict:
        service = self.service
        return service.status() if service is not None else {"enabled": False}

    def checks(self):
        service = self.service
        out = list(service.checks()) if service is not None else []
        if self._fetcher is None:
            paths = ssl.get_default_verify_paths()
            found = bool(paths.cafile or paths.capath or os.environ.get("SSL_CERT_FILE"))
            out.append({"id": "certificates", "title": "Trusted website certificates",
                        "status": "ok" if found else "warn",
                        "detail": "This computer has a list of trusted website certificates." if found else
                                  "No list of trusted website certificates was found, so secure websites cannot be verified.",
                        "hint": "" if found else "On a Mac, open the Python folder in Applications and run 'Install Certificates.command', then restart."})
        return out


def build() -> ResearchExtension:
    return ResearchExtension()
