"""The research service: researchers run on a schedule and turn what they read into briefs.

This module never calls a language model. It fetches, parses, filters and extracts; writing a spoken script from a
brief belongs to the hosts lane. Retrieved text is data: it is stored and returned as strings and never executed,
followed or placed in an operation result or the status snapshot.

Threading: one scheduler thread (``myradio-research``) wakes about every 30 seconds and runs at most one researcher at a
time. ``run_now`` only queues a request for that thread. Result delivery to other threads uses events, and the
status snapshot is replaced as a whole so reading it takes no lock.
"""
from __future__ import annotations

import collections
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..extensions import ExtensionError
from .errors import FetchError, fail
from .feeds import Entry, FeedError, decode_text, parse_feed, read_page, sniff
from .fetcher import Fetcher, http_date
from .model import (ID, MAX_RESEARCHERS, cadence_text, jitter_seconds, nominal_next, normalize_researcher, plausible_next,
                    public_spec)
from .selection import Candidate, SKIP_REASONS, humanize_age, limits_for, select_items
from .store import Store, day_of
from .textutil import canonical_key, clean_text, clean_url, cut_at_word, digest, iso_utc, url_host

TICK_SECONDS = 30.0
RUN_BUDGET_SECONDS = 180.0
MAX_JOBS_PER_TICK = 10
RETRY_MINUTES = 20
RETRY_LIMIT = 2
MAX_JITTER = 60.0
MIN_PLAUSIBLE_TS = 1_704_067_200.0     # 2024-01-01: a clock earlier than this is wrong
SKEW_WARN = 300.0
NETWORK_KINDS = frozenset({"dns", "dns_timeout", "dns_busy", "connect", "timeout"})
STATUS_NAME_CHARS = 40          # a researcher's name in the status snapshot (the full name is on GET /researchers); keeps 50 of them under the host's size limit
SKIP_TEXT = {
    "undated": "without a publication date",
    "future": "dated in the future",
    "too_old": "older than you asked for",
    "already_covered": "already covered",
    "skipped_source": "from a source you skip",
    "skipped_topic": "about a topic you skip",
    "not_relevant": "not matching your interests",
    "tone": "down-ranked by the tone word list",
    "similar_to_aired": "close to something already covered",
    "duplicate": "repeating another story",
    "source_limit": "left out to keep one source from dominating",
    "no_room": "that did not fit in the segment",
}


class _Aborted(Exception):
    """The station is shutting down; abandon the run without recording anything."""


@dataclass
class SourceOutcome:
    url: str
    name: str
    kind: Optional[str]
    status: str = "error"                 # ok, unchanged, error or skipped
    message: str = ""
    entries: list = field(default_factory=list)
    fmt: str = ""
    final_url: Optional[str] = None
    feed_url: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    error_kind: str = ""
    checked_ts: float = 0.0
    feed_link: Optional[str] = None
    keys: dict = field(default_factory=dict)     # entry index -> explicit key (page entries)


class PersistentBudget:
    """The daily request cap, kept in the research database so a restart does not reset it."""

    def __init__(self, store: Store, limit: int, wall_clock: Callable[[], float] = time.time):
        self.store, self.limit, self._clock = store, limit, wall_clock

    def take(self) -> None:
        if not self.store.take_request(day_of(self._clock()), self.limit):
            raise fail("daily_cap", cap=self.limit)

    def used(self) -> int:
        return self.store.requests_on(day_of(self._clock()))


class _Ticket:
    def __init__(self):
        self.event = threading.Event()
        self.state = "queued"
        self.result: Optional[dict] = None

    def finish(self, result: dict) -> None:
        self.result, self.state = result, "done"
        self.event.set()


class _Observations:
    """What recent fetches say about the network and the clock (for the doctor checks).

    A connection problem only counts as "cannot reach the web" when the run it happened in did not reach a single website:
    a mistyped or retired address among sources that answered says something about that address and not about the
    network, whatever order the sources are listed in. ``failed`` therefore holds problems back until ``settle``."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reached_ts: Optional[float] = None
        self.failures = 0
        self.last_failure: Optional[str] = None
        self.skews: collections.deque = collections.deque(maxlen=8)
        self._answers = 0                  # how many times any website has answered
        self._pending: list = []           # connection problems of the run in progress

    def mark(self) -> int:
        with self._lock:
            return self._answers

    def reached(self, ts: float, server_date: Optional[float] = None) -> None:
        with self._lock:
            self.reached_ts, self.failures = ts, 0
            self._answers += 1
            if server_date is not None:
                self.skews.append(server_date - ts)

    def failed(self, message: str) -> None:
        with self._lock:
            if len(self._pending) < 100:
                self._pending.append(message)

    def settle(self, mark: int, count: bool = True) -> None:
        """The run that began at ``mark`` has ended: its held-back connection problems count only if no website answered since.
        With ``count=False`` they are dropped (an address typed into "Check this address" tells us nothing about the network)."""
        with self._lock:
            pending, self._pending = self._pending, []
            if count and pending and self._answers == mark:
                self.failures += len(pending)
                self.last_failure = pending[-1]

    def snapshot(self) -> dict:
        with self._lock:
            skews = sorted(self.skews)
            return {"reached_ts": self.reached_ts, "failures": self.failures, "last_failure": self.last_failure,
                    "skew": skews[len(skews) // 2] if skews else None}


class ResearchService:
    def __init__(self, store: Store, fetcher: Fetcher, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, tick_seconds: float = TICK_SECONDS, tick_jitter: float = 0.1,
                 max_jitter: float = MAX_JITTER, run_budget: float = RUN_BUDGET_SECONDS, retry_minutes: float = RETRY_MINUTES,
                 rng: Optional[random.Random] = None, daily_cap: int = 500, config_notes: tuple = ()):
        self.store, self.fetcher, self.clock, self._mono = store, fetcher, clock, monotonic
        self.tick_seconds, self.tick_jitter, self.max_jitter = float(tick_seconds), float(tick_jitter), float(max_jitter)
        self.run_budget, self.retry_minutes = float(run_budget), float(retry_minutes)
        self.daily_cap = daily_cap
        self.config_notes = tuple(config_notes)
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._snap_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._requests: "collections.OrderedDict[str, _Ticket]" = collections.OrderedDict()
        self._current: Optional[str] = None
        self._current_ticket: Optional[_Ticket] = None
        self._snapshot: dict = {"researchers": [], "totals": {"researchers": 0, "pending_review": 0, "requests_today": 0,
                                                               "daily_cap": daily_cap}}
        self.last_tick_ts: Optional[float] = None
        self.tick_errors = 0
        self.observations = _Observations()

    # ---- lifecycle -------------------------------------------------------------------------------------

    def start(self, run_thread: bool = True) -> None:
        self._repair_schedule()
        self._refresh()
        if run_thread and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="myradio-research", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self.fetcher.cancel.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            tickets = list(self._requests.values())
            self._requests.clear()
        for ticket in tickets:
            ticket.finish({"state": "stopped", "message": "The station is shutting down."})
        self.store.close()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _next_delay(self) -> float:
        """Seconds until the scheduler next wakes: about 30, spread by +/-10% so nothing beats in lock-step."""
        return max(0.01, self.tick_seconds * (1.0 + self._rng.uniform(-self.tick_jitter, self.tick_jitter)))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad pass must not end the scheduler
                self.tick_errors += 1
            self._wake.wait(self._next_delay())
            self._wake.clear()

    # ---- scheduling --------------------------------------------------------------------------------------

    def _schedule(self, rid: str, cadence: dict, now: float, last_start: Optional[float]) -> float:
        nominal = nominal_next(cadence, now=now, last_start=last_start)
        return nominal + jitter_seconds(f"{rid}:{int(nominal)}", self.max_jitter)

    def _repair_schedule(self) -> None:
        """After a restart or a clock change: give any active researcher with an impossible due time a sane one."""
        now = self.clock()
        for row in self.store.list_all():
            if not row["paused"] and not plausible_next(row["spec"]["cadence"], row["next_run_ts"], now):
                self.store.set_next_run(row["id"], self._schedule(row["id"], row["spec"]["cadence"], now, row["last_run_ts"]))

    def tick(self) -> int:
        """Run what is due, one researcher at a time. Returns how many ran. The scheduler thread calls this; so may a test."""
        self.last_tick_ts = self.clock()
        ran = 0
        while ran < MAX_JOBS_PER_TICK and not self._stop.is_set():
            with self._job_lock:                      # choosing and running happen under one lock: no job can be picked twice
                job = self._pick()
                if job is None:
                    break
                self._run_locked(*job)
            ran += 1
        return ran

    def _pick(self):
        with self._lock:
            if self._requests:
                rid, ticket = next(iter(self._requests.items()))
                del self._requests[rid]
                return rid, ticket, "manual"
        row = self.store.next_due(self.clock())
        return (row["id"], _Ticket(), "scheduled") if row else None

    def _run_locked(self, rid: str, ticket: _Ticket, trigger: str) -> None:
        with self._lock:
            self._current, self._current_ticket = rid, ticket
        ticket.state = "running"
        self._refresh()
        result = {"state": "done", "outcome": "error", "message": "Something went wrong. It will be tried again later."}
        try:
            result = self._execute(rid, trigger)
        except _Aborted:
            result = {"state": "stopped", "message": "The station is shutting down."}
        except ExtensionError:
            result = {"state": "stopped", "message": "The research service is shutting down."}
        except Exception:  # noqa: BLE001 - a bug in one run must not stop the scheduler or leak internals
            self._after_crash(rid, trigger)
        finally:
            with self._lock:
                self._current, self._current_ticket = None, None
            ticket.finish(result)
            try:
                self._refresh()
            except ExtensionError:
                pass

    def _after_crash(self, rid: str, trigger: str) -> None:
        """A bug or odd input escaped a run: say so plainly and move the schedule on so it cannot spin."""
        try:
            row = self.store.get(rid)
            if row is None:
                return
            nxt = None
            if trigger == "scheduled" and not row["paused"]:
                nxt = self._schedule(rid, row["spec"]["cadence"], self.clock(), self.clock())
            self.store.note_failure(rid, "Something went wrong while checking your sources. It will try again at the next scheduled time.",
                                    self.clock(), nxt)
        except ExtensionError:
            pass

    # ---- one run -------------------------------------------------------------------------------------------

    def _execute(self, rid: str, trigger: str) -> dict:
        row = self.store.get(rid)
        if row is None:
            return {"state": "done", "outcome": "error", "message": "That researcher no longer exists."}
        spec, version = row["spec"], row["version"]
        force = trigger == "manual"
        started = self.clock()
        deadline = self._mono() + self.run_budget
        states = self.store.source_states(rid)
        outcomes = []
        mark = self.observations.mark()
        try:
            for source in spec["sources"]:
                if self._stop.is_set():
                    raise _Aborted()
                if self._mono() > deadline:
                    outcomes.append(SourceOutcome(url=source["url"], name=source["name"] or url_host(source["url"]), kind=source["kind"],
                                                  status="skipped", message="Skipped because this run ran out of time.",
                                                  checked_ts=self.clock()))
                    continue
                outcomes.append(self._read_source(source, states.get(source["url"]), force))
        finally:
            self.observations.settle(mark)
        if self._stop.is_set():
            raise _Aborted()
        now = self.clock()
        candidates = []
        for out in outcomes:
            for index, entry in enumerate(out.entries):
                candidates.append(Candidate(entry=entry, source_url=out.final_url or out.url, source_name=out.name, source_kind=out.kind,
                                            fetched_ts=out.checked_ts, feed_link=out.feed_link, key=out.keys.get(index, "")))
        seen = self.store.seen_index(rid)
        selection = select_items(spec, candidates, seen, now)
        brief = self._assemble(rid, spec, outcomes, selection, now, trigger)
        errors = row["consecutive_errors"] + 1 if brief["outcome"] == "error" else 0
        next_run = None
        if trigger == "scheduled":
            next_run = self._schedule(rid, spec["cadence"], self.clock(), started)
            if brief["outcome"] == "error" and errors <= RETRY_LIMIT:
                retry = self.clock() + self.retry_minutes * 60 + jitter_seconds(f"{rid}:retry:{errors}", self.max_jitter)
                next_run = min(next_run, retry)
        states_out = [self._state_row(o, states.get(o.url)) for o in outcomes if o.status in ("ok", "unchanged", "error")]
        status = self.store.commit_run(rid, version, brief, selection.used, states_out, now=now, next_run=next_run,
                                       consecutive_errors=errors)
        if status == "changed":
            self.store.set_next_run(rid, self.clock())     # settings were edited mid-run: redo it with the new ones
            return {"state": "done", "outcome": "error", "message": "Your settings changed while it was running, so it will run again now."}
        if status == "gone":
            return {"state": "done", "outcome": "error", "message": "That researcher was removed."}
        return {"state": "done", "outcome": brief["outcome"], "brief_id": brief["id"], "items": len(brief["items"]), "message": brief["message"]}

    @staticmethod
    def _state_row(out: SourceOutcome, previous: Optional[dict]) -> dict:
        """What to remember about a source. A failed check keeps the cache validators and the discovered feed address from the
        last good one, so one bad night does not make the next run start from scratch."""
        row = {"url": out.url, "final_url": out.final_url, "feed_url": out.feed_url, "etag": out.etag, "last_modified": out.last_modified,
               "name": out.name, "format": out.fmt, "checked_ts": out.checked_ts, "status": out.status, "message": out.message,
               "entries": len(out.entries)}
        if out.status == "error" and previous:
            for key in ("final_url", "feed_url", "etag", "last_modified", "name", "format"):
                row[key] = previous.get(key)
        return row

    def _assemble(self, rid: str, spec: dict, outcomes: list, selection, now: float, trigger: str) -> dict:
        reached = [o for o in outcomes if o.status in ("ok", "unchanged")]
        items = selection.items
        outcome = "ready" if items else ("nothing_new" if reached else "error")
        _, _, target_words = limits_for(spec["segment_seconds"])
        window = spec["freshness_hours"]
        warnings = [o.message for o in outcomes if o.status in ("error", "skipped")] if reached else []
        if outcome == "ready":
            review = "pending" if spec["review"] == "before_air" else "auto"
            plural = "update" if len(items) == 1 else "updates"
            message = (f"{len(items)} {plural} ready. "
                       + ("Waiting for your review." if review == "pending" else "It does not need your review, so it can be used on air."))
        elif outcome == "nothing_new":
            review = None
            message = "Nothing new and interesting this time, so there is no update to air. Nothing was made up to fill the gap."
            detail = self._explain_skips(selection.skipped, selection.considered)
            if detail:
                message += f" ({detail})"
        else:
            review = None
            first = next((o.message for o in outcomes if o.status == "error"), "No source could be read.")
            message = f"Could not read any of your sources, so no update was made. {first}"
        brief = {
            "id": uuid.uuid4().hex, "researcher_id": rid, "researcher_name": spec["name"], "outcome": outcome, "message": message,
            "review": review, "trigger": trigger, "created_ts": now, "created_at": iso_utc(now),
            "freshness_hours": window, "segment_seconds": spec["segment_seconds"], "target_words": target_words, "anchor": spec["anchor"],
            "fresh_until_ts": selection.fresh_until if items else None, "fresh_until_at": iso_utc(selection.fresh_until) if items else None,
            "items": items,
            "sources": [{"url": o.url, "name": o.name, "status": o.status, "message": o.message, "entries": len(o.entries),
                         "format": o.fmt, "checked_at": iso_utc(o.checked_ts)} for o in outcomes],
            "summary": {"considered": selection.considered, "kept": len(items), "skipped": selection.skipped,
                        "explanation": self._explain_choice(spec, selection, outcomes) if items else ""},
            "warnings": warnings,
        }
        return brief

    @staticmethod
    def _explain_skips(skipped: dict, considered: int) -> str:
        if not considered:
            return "no posts were found"
        parts = [f"{skipped[key]} {SKIP_TEXT[key]}" for key in SKIP_REASONS if skipped.get(key)]
        looked = f"looked at {considered} post{'s' if considered != 1 else ''}"
        return looked + (": " + ", ".join(parts) if parts else "")

    @staticmethod
    def _explain_choice(spec: dict, selection, outcomes: list) -> str:
        sources = len({o.url for o in outcomes if o.status in ("ok", "unchanged")})
        window = spec["freshness_hours"]
        span = "the last hour" if window == 1 else f"the last {window} hours"
        return (f"Chosen from {selection.considered} recent post{'s' if selection.considered != 1 else ''} across {sources} "
                f"source{'s' if sources != 1 else ''}: they match your interests, were published in {span}, and had not been covered before.")

    # ---- reading one source ----------------------------------------------------------------------------------

    def _validators(self, state: Optional[dict]) -> Optional[dict]:
        if state and state.get("final_url") and (state.get("etag") or state.get("last_modified")):
            return {"url": state["final_url"], "etag": state.get("etag"), "last_modified": state.get("last_modified")}
        return None

    def _note_error(self, error: FetchError) -> None:
        if error.kind in NETWORK_KINDS:
            self.observations.failed(error.message)
        elif error.kind not in ("cancelled", "daily_cap", "backoff"):
            self.observations.reached(self.clock())

    def _read_source(self, source: dict, state: Optional[dict], force: bool) -> SourceOutcome:
        """Fetch and read one configured source. A remembered feed address is tried first; if it no longer works the
        configured address is tried again from scratch. Never raises ``FetchError`` or ``FeedError``."""
        url = source["url"]
        name = source["name"] or (state or {}).get("name") or url_host(url)
        attempts = [state["feed_url"]] if state and state.get("feed_url") else []
        attempts.append(url)
        problem: Optional[tuple] = None
        for target in attempts:
            out = SourceOutcome(url=url, name=name, kind=source["kind"], checked_ts=self.clock())
            try:
                result = self.fetcher.fetch(target, validators=None if force else self._validators(state),
                                            on_behalf_of=url_host(url) if target != url else "")
                self.observations.reached(self.clock(), result.server_date)
                self._interpret(out, source, result, target, state)
                return out
            except FetchError as caught:
                self._note_error(caught)
                problem = (caught.kind, caught.message)
                if caught.kind == "cancelled":
                    raise _Aborted() from None
                if caught.kind in ("daily_cap", "backoff"):
                    break
            except FeedError as caught:
                problem = ("bad_response", f"{url_host(url)}: {caught}")
            except (_Aborted, ExtensionError):
                raise
            except Exception:  # noqa: BLE001 - one odd website must never cost the researcher's other sources their turn
                problem = ("bad_response", f"{url_host(url)} sent something the station could not make sense of, so it was skipped.")
        out = SourceOutcome(url=url, name=name, kind=source["kind"], checked_ts=self.clock(), status="error")
        out.error_kind, out.message = problem if problem else ("bad_response", "That address could not be read.")
        return out

    def _interpret(self, out: SourceOutcome, source: dict, result, target: str, state: Optional[dict]) -> None:
        host = url_host(source["url"])
        out.final_url = result.final_url
        if target != source["url"]:
            out.feed_url = target
        if result.not_modified:
            out.status, out.message, out.fmt = "unchanged", "Nothing has changed since the last check.", (state or {}).get("format") or ""
            out.etag, out.last_modified = result.etag, result.last_modified
            return
        out.etag, out.last_modified = result.etag, result.last_modified
        kind = sniff(result.body)
        if kind in ("xml", "json"):
            doc = parse_feed(result.body, result.charset, result.final_url)
            self._take_feed(out, doc, source)
            return
        if kind == "html":
            page = read_page(result.body, result.charset, result.final_url)
            link = self._same_site_feed(page, result.final_url, source["url"]) if target == source["url"] else None
            if link:
                found = self.fetcher.fetch(link, validators=self._validators(state) if state and state.get("final_url") == link else None,
                                           on_behalf_of=host)
                self.observations.reached(self.clock(), found.server_date)
                if found.not_modified:
                    out.status, out.message, out.fmt = "unchanged", "Nothing has changed since the last check.", (state or {}).get("format") or ""
                    out.final_url, out.feed_url = found.final_url, link
                    out.etag, out.last_modified = found.etag, found.last_modified
                    return
                doc = parse_feed(found.body, found.charset, found.final_url)
                out.final_url, out.feed_url = found.final_url, link
                out.etag, out.last_modified = found.etag, found.last_modified
                self._take_feed(out, doc, source, discovered=True)
                return
            self._take_page(out, page, result, source)
            return
        text = decode_text(result.body, result.charset)
        self._take_text(out, text, result)

    def _same_site_feed(self, page, final_url: str, configured: str) -> Optional[str]:
        site = url_host(final_url)
        for link, _kind, _title in page.feed_links:
            host = url_host(link)
            if host and site and (host == site or host.endswith("." + site) or site.endswith("." + host)):
                return link
        return None

    def _take_feed(self, out: SourceOutcome, doc, source: dict, discovered: bool = False) -> None:
        out.fmt, out.feed_link = doc.format, doc.link
        if not source["name"] and doc.title:
            out.name = clean_text(doc.title, 80)
        out.entries = doc.entries
        dated = sum(1 for e in doc.entries if e.published_ts is not None)
        count = len(doc.entries)
        posts = "1 post" if count == 1 else f"{count} posts"
        lead = "Found the site's feed and read " if discovered else "Read "
        out.status, out.message = "ok", f"{lead}{posts}" + (f" ({dated} with dates)." if dated != count else ".")

    def _take_page(self, out: SourceOutcome, page, result, source: dict) -> None:
        blocks = [b for b in page.blocks if len(b) >= 25] or list(page.blocks)
        text = clean_text(" ".join(blocks), 6000)
        title = page.title or (blocks[0][:120] if blocks else "")
        ts, precision, basis = page.published_ts, page.date_precision, "published"
        stamp = http_date(result.last_modified)
        if ts is None and stamp is not None and out.checked_ts - stamp > 120:    # a "last modified" of right now means a dynamic page
            ts, precision, basis = stamp, "time", "last_modified"
        out.fmt = "page"
        if not text and not title:
            out.status, out.message = "error", f"{url_host(source['url'])}: that page has no readable text."
            return
        link = page.canonical or clean_url(result.final_url)
        entry = Entry(title=title, link=link, published_ts=ts, date_precision=precision, date_basis=basis, content=text)
        out.entries = [entry]
        out.keys = {0: digest("p", canonical_key(result.final_url), str(int(ts or 0)))}
        out.feed_link = None
        dated = "" if ts is not None else " It has no date, so it cannot be treated as new. A feed address works better for this site."
        out.status, out.message = "ok", "Read the page." + dated

    def _take_text(self, out: SourceOutcome, text: str, result) -> None:
        lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
        out.fmt = "text"
        if not lines:
            out.status, out.message = "error", f"{url_host(out.url)}: that address returned no text."
            return
        stamp = http_date(result.last_modified)
        if stamp is not None and out.checked_ts - stamp <= 120:
            stamp = None
        body = clean_text(" ".join(lines[1:]) or lines[0], 6000)
        entry = Entry(title=clean_text(lines[0], 200), link=clean_url(result.final_url), published_ts=stamp,
                      date_precision="time" if stamp else None, date_basis="last_modified" if stamp else "", content=body)
        out.entries = [entry]
        out.keys = {0: digest("p", canonical_key(result.final_url), str(int(stamp or 0)))}
        out.status, out.message = "ok", "Read the text." + ("" if stamp else " It has no date, so it cannot be treated as new.")

    # ---- operations ---------------------------------------------------------------------------------------------

    def save(self, raw: dict) -> dict:
        spec = normalize_researcher(raw, self.fetcher.check_url)
        notes = spec.pop("_notes", [])
        now = self.clock()
        rid = raw.get("id")
        paused = raw.get("paused")
        if rid in (None, ""):
            rid = uuid.uuid4().hex
            self.store.create(rid, spec, bool(paused), now, self._schedule(rid, spec["cadence"], now, None), limit=MAX_RESEARCHERS)
            created = True
        else:
            if not isinstance(rid, str) or not ID.fullmatch(rid):
                raise ExtensionError("That researcher does not exist.")
            existing = self.store.get(rid)
            if existing is None:
                raise ExtensionError("That researcher no longer exists.")
            keep = existing["next_run_ts"]
            if existing["spec"]["cadence"] != spec["cadence"] or not plausible_next(spec["cadence"], keep, now):
                keep = self._schedule(rid, spec["cadence"], now, existing["last_run_ts"])
            if self.store.update(rid, spec, paused if isinstance(paused, bool) else None, now, keep) is None:
                raise ExtensionError("That researcher no longer exists.")
            created = False
        self._refresh()
        self._wake.set()
        return {"researcher": self.researcher(rid), "created": created, "notes": notes}

    def remove(self, rid: str) -> None:
        if not self.store.delete(rid):
            raise ExtensionError("That researcher does not exist.")
        with self._lock:
            ticket = self._requests.pop(rid, None)
        if ticket:
            ticket.finish({"state": "stopped", "message": "That researcher was removed."})
        self._refresh()

    def set_paused(self, rid: str, paused: bool) -> dict:
        row = self.store.get(rid)
        if row is None:
            raise ExtensionError("That researcher does not exist.")
        now = self.clock()
        nxt = None if paused else self._schedule(rid, row["spec"]["cadence"], now, row["last_run_ts"])
        self.store.set_paused(rid, paused, nxt, now)
        self._refresh()
        self._wake.set()
        return self.researcher(rid)

    def run_now(self, rid: str, wait: float = 0.0) -> dict:
        """Queue a manual run for the scheduler thread. With ``wait`` it also waits (up to that many seconds)
        for the result, so a caller gets the outcome instead of "started" when the run is quick."""
        if self._stop.is_set():
            raise ExtensionError("The station is shutting down.")
        if self.store.get(rid) is None:
            raise ExtensionError("That researcher does not exist.")
        with self._lock:
            if self._current == rid and self._current_ticket is not None:
                ticket = self._current_ticket
            else:
                ticket = self._requests.get(rid)
                if ticket is None:
                    ticket = self._requests[rid] = _Ticket()
        self._refresh()
        self._wake.set()
        if wait > 0:
            ticket.event.wait(wait)
        if ticket.result is not None:
            return dict(ticket.result)
        return {"state": ticket.state, "message": ("It is running now. Check back in a minute." if ticket.state == "running"
                                                    else "It is queued and will start in a moment.")}

    def review(self, brief_id: str, decision: str) -> dict:
        changed, brief = self.store.decide(brief_id, decision, self.clock())
        self._refresh()
        return {"brief_id": brief["id"], "researcher_id": brief["researcher_id"], "review": brief["review"], "changed": changed}

    def mark_aired(self, brief_id: str) -> bool:
        """For the director: record that a brief went to air. True once, False if it was not airable (already aired,
        rejected, still pending review, or stale)."""
        done = self.store.mark_aired(brief_id, self.clock())
        if done:
            self._refresh()
        return done

    def release_brief(self, brief_id: str) -> bool:
        """For the director: a claimed or marked brief never actually aired (for example its speech job failed), so make it
        available again. Whether it can then air is still decided by review and freshness."""
        done = self.store.release(brief_id)
        if done:
            self._refresh()
        return done

    def next_airable(self, researcher_id: Optional[str] = None, now: Optional[float] = None) -> Optional[dict]:
        """The oldest brief that may air now (approved or automatic, not aired, still fresh), or ``None`` which means
        "nothing suitable: play music"."""
        stamp = self.clock() if now is None else now
        found = self.store.airable(stamp, researcher_id, limit=1)
        return self._public_brief(found[0], stamp) if found else None

    def claim_airable(self, researcher_id: Optional[str] = None) -> Optional[dict]:
        """``next_airable`` and ``mark_aired`` as one atomic step, so two callers can never air the same brief."""
        now = self.clock()
        brief = self.store.claim(now, researcher_id)
        if brief is not None:
            self._refresh()
        return self._public_brief(brief, now) if brief else None

    # ---- reads ----------------------------------------------------------------------------------------------------

    def _public_brief(self, brief: dict, now: float) -> dict:
        out = dict(brief)
        out["airable"] = (out["outcome"] == "ready" and out["review"] in ("approved", "auto") and out["aired_ts"] is None
                          and (out["fresh_until_ts"] is None or now <= out["fresh_until_ts"]))
        for field_name in ("created", "reviewed", "aired", "fresh_until"):
            out[f"{field_name}_at"] = iso_utc(out.get(f"{field_name}_ts"))
        return out

    def briefs(self, *, researcher: Optional[str] = None, review: Optional[str] = None, outcome: Optional[str] = None,
               limit: int = 20, brief_id: Optional[str] = None, airable: bool = False) -> list:
        """Newest first. With ``airable=True`` only briefs that may air now are listed, and the limit applies to those."""
        now = self.clock()
        return [self._public_brief(b, now) for b in self.store.list_briefs(researcher=researcher, review=review, outcome=outcome,
                                                                            limit=limit, brief_id=brief_id,
                                                                            airable_at=now if airable else None)]

    def researcher(self, rid: str) -> dict:
        row = self.store.get(rid)
        if row is None:
            raise ExtensionError("That researcher does not exist.")
        return self._describe(row, self.store.pending_counts())

    def researchers(self) -> list:
        pending = self.store.pending_counts()
        return [self._describe(row, pending) for row in self.store.list_all()]

    def _describe(self, row: dict, pending: dict) -> dict:
        spec = row["spec"]
        with self._lock:
            state = "paused" if row["paused"] else "running" if self._current == row["id"] else "queued" if row["id"] in self._requests else "idle"
        out = public_spec(spec)
        out.update({
            "id": row["id"], "paused": row["paused"], "state": state, "cadence_text": cadence_text(spec["cadence"]),
            "created_ts": row["created_ts"], "created_at": iso_utc(row["created_ts"]),
            "updated_ts": row["updated_ts"], "updated_at": iso_utc(row["updated_ts"]),
            "next_run_ts": row["next_run_ts"], "next_run_at": iso_utc(row["next_run_ts"]),
            "last_run": None if row["last_run_ts"] is None else {
                "ts": row["last_run_ts"], "at": iso_utc(row["last_run_ts"]), "outcome": row["last_outcome"],
                "message": row["last_message"], "brief_id": row["last_brief_id"]},
            "pending_review": pending.get(row["id"], 0), "consecutive_errors": row["consecutive_errors"],
        })
        return out

    def sources(self) -> list:
        merged: dict = {}
        for row in self.store.list_all():
            for source in row["spec"]["sources"]:
                entry = merged.setdefault(source["url"], {"url": source["url"], "host": url_host(source["url"]), "name": source["name"] or "",
                                                          "kind": source["kind"], "used_by": [], "status": "unchecked",
                                                          "message": "Not checked yet.", "checked_ts": None, "format": "", "entries": 0,
                                                          "feed_url": None})
                entry["used_by"].append({"id": row["id"], "name": row["name"]})
        for state in self.store.all_source_states():
            entry = merged.get(state["url"])
            if entry is None or (entry["checked_ts"] or 0) > (state["checked_ts"] or 0):
                continue
            entry.update({"status": state["status"] or "unchecked", "message": state["message"] or "", "checked_ts": state["checked_ts"],
                          "format": state["format"] or "", "entries": state["entries"] or 0, "feed_url": state["feed_url"]})
            if not entry["name"] and state["name"]:
                entry["name"] = state["name"]
        for entry in merged.values():
            entry["checked_at"] = iso_utc(entry["checked_ts"])
            entry["name"] = entry["name"] or entry["host"]
        return sorted(merged.values(), key=lambda e: (e["name"].lower(), e["url"]))

    def check_source(self, url: str) -> dict:
        """Look at one address (for the 'Check this address' button) without saving anything."""
        try:
            address = self.fetcher.check_url(url)
        except FetchError as error:
            raise ExtensionError(error.message) from None
        if not self._job_lock.acquire(timeout=3):
            raise ExtensionError("The station is busy checking your researchers. Try again in a minute.")
        mark = self.observations.mark()
        try:
            out = self._read_source({"url": address, "name": "", "kind": None}, None, True)
        except _Aborted:
            raise ExtensionError("The station is shutting down.") from None
        finally:
            self.observations.settle(mark, count=False)
            self._job_lock.release()
        if out.status == "error":
            raise ExtensionError(out.message)
        dated = sum(1 for e in out.entries if e.published_ts is not None)
        return {"ok": True, "address": address, "kind": "page" if out.fmt in ("page", "text") else "feed", "format": out.fmt,
                "title": out.name, "posts": len(out.entries), "dated": dated, "feed_url": out.feed_url,
                "sample": [e.title for e in out.entries[:3]], "message": out.message}

    # ---- status ------------------------------------------------------------------------------------------------------

    def _refresh(self) -> None:
        with self._snap_lock:
            now = self.clock()
            pending = self.store.pending_counts()
            with self._lock:
                current, queued = self._current, set(self._requests)
            rows = self.store.list_all()
            researchers = []
            for row in rows:
                last = None
                if row["last_run_ts"] is not None:
                    last = {"at": iso_utc(row["last_run_ts"]), "outcome": row["last_outcome"], "message": (row["last_message"] or "")[:400],
                            "brief_id": row["last_brief_id"]}
                researchers.append({
                    "id": row["id"], "name": cut_at_word(row["name"], STATUS_NAME_CHARS), "paused": row["paused"],
                    "state": "paused" if row["paused"] else "running" if current == row["id"] else "queued" if row["id"] in queued else "idle",
                    "next_run_at": iso_utc(row["next_run_ts"]), "last_run": last, "pending_review": pending.get(row["id"], 0)})
            try:
                used = self.store.requests_on(day_of(now))
            except ExtensionError:
                used = 0
            self._snapshot = {"researchers": researchers,
                              "totals": {"researchers": len(rows), "pending_review": sum(pending.values()),
                                         "requests_today": used, "daily_cap": self.daily_cap}}

    def status(self) -> dict:
        """Cached, lock-free and O(1): the snapshot is replaced as a whole whenever something changes."""
        snap = self._snapshot
        return {"enabled": True, "scheduler": {"running": self.running, "last_tick_at": iso_utc(self.last_tick_ts), "problems": self.tick_errors}, **snap}

    def checks(self) -> list:
        now = self.clock()
        obs = self.observations.snapshot()
        out = []
        if obs["reached_ts"] is None and not obs["failures"]:
            out.append({"id": "network", "title": "Reaching the web", "status": "unknown",
                        "detail": "No researcher has read a source yet, so this has not been tested.",
                        "hint": "Add a researcher and press Run now to test it."})
        elif obs["failures"] >= 3:
            out.append({"id": "network", "title": "Reaching the web", "status": "fail",
                        "detail": f"The last {obs['failures']} attempts could not reach any website. {obs['last_failure'] or ''}".strip(),
                        "hint": "Check that this computer is online. Researchers keep trying on their schedule."})
        elif obs["failures"]:
            out.append({"id": "network", "title": "Reaching the web", "status": "warn",
                        "detail": f"The last attempt failed. {obs['last_failure'] or ''}".strip(),
                        "hint": "If it keeps happening, check that this computer is online."})
        else:
            out.append({"id": "network", "title": "Reaching the web", "status": "ok",
                        "detail": f"A website was reached {humanize_age(max(0.0, now - obs['reached_ts']))}.", "hint": ""})
        latest = None
        try:
            latest = self.store.latest_run_ts()
        except ExtensionError:
            pass
        if now < MIN_PLAUSIBLE_TS:
            clock = ("fail", "This computer's clock is set to a date that is long past, so freshness cannot be judged.",
                     "Set the date and time, ideally to update automatically.")
        elif latest is not None and now < latest - SKEW_WARN:
            clock = ("warn", "The clock moved backwards since the last research run.", "Check the date and time on this computer.")
        elif obs["skew"] is not None and abs(obs["skew"]) > SKEW_WARN:
            minutes = int(abs(obs["skew"]) // 60)
            clock = ("warn", f"This computer's clock differs from the websites it reads by about {minutes} minutes, which can make new "
                             "stories look old or old ones look new.", "Set the date and time to update automatically.")
        else:
            clock = ("ok", "The clock looks right." if obs["skew"] is not None else "The clock looks plausible. It has not yet been compared with a website.", "")
        out.append({"id": "clock", "title": "Computer clock", "status": clock[0], "detail": clock[1], "hint": clock[2]})
        try:
            used = self.store.requests_on(day_of(now))
        except ExtensionError:
            used = 0
        share = used / self.daily_cap if self.daily_cap else 0
        out.append({"id": "allowance", "title": "Daily web allowance", "status": "fail" if share >= 1 else "warn" if share >= 0.8 else "ok",
                    "detail": f"{used} of {self.daily_cap} web requests used today.",
                    "hint": "Researchers pause until tomorrow when the allowance is used up." if share >= 0.8 else ""})
        if self.config_notes:
            out.append({"id": "settings", "title": "Research settings", "status": "warn", "detail": " ".join(self.config_notes)[:380],
                        "hint": "Check config.json in the research folder, or delete it to use the defaults."})
        return out
