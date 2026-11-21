"""The director: keeps a few spoken clips made ahead of time and puts each one on air where it belongs.

It is the autopilot DJ. While it is on, every few songs get a host's intro, a station ID goes out every so
often, and approved research updates are read as news. It works only through the station's own operations
(``hosts.compose``, ``hosts.compose_update``, ``research.claim``, ``generation.enqueue``, ``generation.job``,
``queue.add``, ``library.delete``), so it can do nothing a person or an agent could not do from the studio.

It asks for speech at an ordinary priority, below the urgent level. On a machine that makes both music and
speech, the generation engine's music-first rule then decides when that speech is made: music first until it
is ahead, then a batch of speech, then music again. Requests from people (the studio, the bar, an agent) stay
urgent and are not held back.

Each clip is tied to where it will air. An intro belongs to one queued song and goes in front of it when it
is ready; if that song has already played, the clip is deleted rather than aired late. A station ID or a news
update goes after the next song. Nothing is aired twice: a clip is placed once and then forgotten.

``director.news_now`` looks a topic up on request: a dedicated, paused researcher reads a Google News search for it,
the newest sourced brief is written up by the chosen host and recorded at an urgent priority, and the update goes on
air next. It works whether or not the autopilot is on.

Off by default. Turn it on with ``director.configure`` (enabled: true) or ``"enabled": true`` in
``<data>/ext/director/config.json``.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

from ..extensions import Arg, Extension, ExtensionError, Operation

import re
from urllib.parse import quote_plus

TICK_S = 15.0
NEWS_PRIORITY = 85
STOP_WORDS = frozenset("""a about after all an and any are as at be been by can could did do does for from get give go going had has have
how i in info information is it its it's just last latest let me more my news of on or our please ran run say some tell that the their
them then there these they this those to up update updates us was we were what whats what's when where which who why will with would you
your general recent new""".split())


PUBLISHER_TAIL = re.compile(r"^(?P<title>.{8,}?)\s+[-\u2013\u2014|]\s+(?P<publisher>[^-\u2013\u2014|]{2,80})$")


def speakable_news(brief: dict, topic: str) -> dict:
    """Google News headlines end with " - Publisher"; read the headline alone and name the publisher as the source
    ("NASA reports: ..."), which is also the honest attribution. A bracketed domain such as "(.gov)" is dropped: a voice
    reads it badly and can stop early on it. Items from other sources are left as the research lane made them."""
    out = dict(brief)
    out["topic"] = topic
    items = []
    for item in brief.get("items") or []:
        item = dict(item)
        if "news.google.com" in str(item.get("source_url") or item.get("url") or ""):
            headline = str(item.get("headline_speech") or item.get("headline") or "").strip()
            match = PUBLISHER_TAIL.match(headline)
            if match:
                publisher = re.sub(r"\s*\([^)]*\)", "", match.group("publisher")).strip(" .")
                headline = match.group("title").strip()
                if publisher:
                    item["source_name_speech"] = publisher
            item["headline_speech"] = item["extract_speech"] = headline
            item["extract_truncated"] = False
        items.append(item)
    out["items"] = items
    return out


def topic_query(topic: str) -> tuple:
    """(search words, interests) for a free-text request such as "get me news on the Cerebras hackathon"."""
    words = [w for w in re.findall(r"[\w][\w.+&'-]*", topic) if w.lower().strip("'") not in STOP_WORDS and len(w) > 1]
    seen, keep = set(), []
    for word in words:
        if word.lower() not in seen:
            seen.add(word.lower())
            keep.append(word)
    keep = keep[:8] or [topic.strip()[:60]]
    return " ".join(keep), keep[:6]
DONE_STATES = ("succeeded", "failed", "cancelled", "expired")
DEFAULTS = {
    "enabled": False,
    "every_songs": 3,          # an intro before every third queued song
    "station_id_minutes": 20,  # 0 turns station IDs off
    "news_minutes": 30,        # 0 turns news off
    "hosts": [],               # host ids to rotate through for intros and IDs; empty means the first listed host
    "news_host": "",           # host for news; empty means the first of ``hosts``
    "ahead": 3,                # at most this many clips waiting to be made or placed
    "priority": 40,            # below the urgent level, so music comes first on a shared machine
}
LIMITS = {"every_songs": (1, 20), "station_id_minutes": (0, 240), "news_minutes": (0, 240), "ahead": (1, 12), "priority": (1, 79)}


def clean_settings(raw: dict, base: Optional[dict] = None) -> dict:
    out = dict(base or DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            raise ExtensionError(f"{key} is not a director setting. Use: " + ", ".join(sorted(DEFAULTS)) + ".")
        if key == "enabled":
            if not isinstance(value, bool):
                raise ExtensionError("enabled must be true or false.")
        elif key in LIMITS:
            low, high = LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise ExtensionError(f"{key} must be a whole number from {low} to {high}.")
        elif key == "hosts":
            if not isinstance(value, list) or len(value) > 16 or not all(isinstance(h, str) and 0 < len(h) <= 63 for h in value):
                raise ExtensionError("hosts must be a list of up to 16 host ids.")
        elif key == "news_host":
            if not isinstance(value, str) or len(value) > 63:
                raise ExtensionError("news_host must be a host id.")
        out[key] = value
    return out


class Director:
    """The decisions, separate from the thread so they can be tested step by step."""

    def __init__(self, station, settings: dict, clock: Callable[[], float] = time.time, state: Optional[dict] = None):
        self.station = station
        self.settings = settings
        self.clock = clock
        state = state or {}
        self.seen = dict(state.get("seen", {}))          # queue id -> True when it gets an intro, False otherwise
        self.counter = int(state.get("counter", 0))       # songs seen since the last intro was planned
        self.pending = list(state.get("pending", []))     # clips asked for and not placed yet
        self.last_id = float(state.get("last_id", 0.0))
        self.last_news = float(state.get("last_news", 0.0))
        self.host_turn = int(state.get("host_turn", 0))
        self.placed = int(state.get("placed", 0))
        self.news_researcher = state.get("news_researcher") or None
        self.requests = list(state.get("requests", []))[-20:]   # news lookups asked for by people
        self.dropped = int(state.get("dropped", 0))
        self.last_error = ""

    def state(self) -> dict:
        return {"seen": self.seen, "counter": self.counter, "pending": self.pending, "last_id": self.last_id,
                "last_news": self.last_news, "host_turn": self.host_turn, "placed": self.placed, "dropped": self.dropped,
                "news_researcher": self.news_researcher, "requests": self.requests[-20:]}

    # ---- one pass ------------------------------------------------------------------------------------
    def idle(self) -> bool:
        """Nothing to do: switched off, nothing waiting and no lookup in progress. Then a pass reads nothing at all."""
        return not self.settings["enabled"] and not self.pending and not any(r["state"] == "looking" for r in self.requests)

    def tick(self) -> None:
        if self.idle():
            return
        status = self.station.status()
        queue = status.get("queue") or []
        self._follow(queue)
        for request in self.requests:
            if request["state"] == "looking":
                self._look_up(request, status)
        if not self.settings["enabled"] or not status.get("running"):
            return
        roles = self._voice_roles(status)
        hosts = self._hosts()
        if not hosts:
            return
        self._plan_intros(queue, status, roles, hosts)
        now = self.clock()
        if self.settings["station_id_minutes"] and "id" in roles and now - self.last_id >= self.settings["station_id_minutes"] * 60 \
                and not self._waiting("id") and len(self.pending) < self.settings["ahead"]:
            self.last_id = now
            self._ask("station_id", None, self._next_host(hosts), status, queue)
        if self.settings["news_minutes"] and "update" in roles and now - self.last_news >= self.settings["news_minutes"] * 60 \
                and not self._waiting("update") and len(self.pending) < self.settings["ahead"]:
            self.last_news = now
            self._news(status)

    # ---- planning ------------------------------------------------------------------------------------
    def _voice_roles(self, status: dict) -> set:
        gen = ((status.get("extensions") or {}).get("generation") or {}).get("status") or {}
        roles = {role for role, ok in ((gen.get("capabilities") or {}).get("roles") or {}).items() if ok}
        return roles | set(gen.get("fixture_roles") or [])

    def _hosts(self) -> list:
        chosen = list(self.settings["hosts"])
        if chosen:
            return chosen
        try:
            listed = self.station.action("hosts.list", {}).get("hosts") or []
        except Exception as exc:  # the hosts extension may be missing or starting
            self.last_error = str(exc)[:200]
            return []
        return [h["id"] for h in listed if not h.get("hidden")][:1]

    def _next_host(self, hosts: list) -> str:
        host = hosts[self.host_turn % len(hosts)]
        self.host_turn += 1
        return host

    def _waiting(self, role: str) -> bool:
        return any(p["role"] == role for p in self.pending)

    def _plan_intros(self, queue: list, status: dict, roles: set, hosts: list) -> None:
        live = {str(entry["id"]) for entry in queue}
        self.seen = {qid: v for qid, v in self.seen.items() if qid in live}   # forget entries that aired or were removed
        if "host_break" not in roles:
            return
        previous = status.get("current")
        for entry in queue:
            qid = str(entry["id"])
            if entry.get("kind") != "music":
                previous = entry
                continue
            if qid not in self.seen:
                self.counter += 1
                talk = self.counter >= self.settings["every_songs"]
                if talk:
                    self.counter = 0
                self.seen[qid] = talk
            if self.seen[qid] is True and len(self.pending) < self.settings["ahead"]:
                self.seen[qid] = "asked"
                facts = {"next_title": entry.get("title") or "", "next_artist": entry.get("artist") or ""}
                if previous and previous.get("kind", "music") == "music":
                    facts.update(now_title=previous.get("title") or "", now_artist=previous.get("artist") or "")
                self._ask("song_intro", qid, self._next_host(hosts), status, queue, facts)
            previous = entry

    def _facts(self, status: dict, extra: Optional[dict] = None) -> dict:
        station = status.get("station") or {}
        local = time.localtime(self.clock())
        hour = local.tm_hour
        facts = {"station": station.get("name") or "myradio",
                 "daypart": "overnight" if hour < 5 else "morning" if hour < 10 else "midday" if hour < 15 else "afternoon" if hour < 19 else "evening",
                 "weekday": time.strftime("%A", local)}
        if station.get("tagline"):
            facts["tagline"] = station["tagline"]
        facts.update({k: v for k, v in (extra or {}).items() if v})
        return facts

    def _ask(self, segment: str, target: Optional[str], host: str, status: dict, queue: list, extra: Optional[dict] = None) -> None:
        try:
            script = self.station.action("hosts.compose", {"persona": host, "segment": segment, "facts": self._facts(status, extra)})
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return
        role = "id" if segment == "station_id" else "host_break"
        self._enqueue(role, script, target, f"director-{segment}-{target or int(self.clock())}")

    def _news(self, status: dict) -> None:
        try:
            brief = self.station.action("research.claim", {}).get("brief")
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return
        if not brief:
            return
        host = self.settings["news_host"] or (self.settings["hosts"] or self._hosts() or [""])[0]
        try:
            script = self.station.action("hosts.compose_update", {"persona": host, "brief": brief, "facts": self._facts(status)})
        except Exception as exc:
            self.last_error = str(exc)[:200]
            self._release(brief.get("id"))
            return
        if not self._enqueue("update", script, None, f"director-news-{brief.get('id')}", brief_id=brief.get("id")):
            self._release(brief.get("id"))

    def ask_news(self, topic: str, host: Optional[str]) -> dict:
        topic = " ".join(str(topic or "").split())
        if not 3 <= len(topic) <= 200:
            raise ExtensionError("Say what to look up in 3 to 200 characters, for example \"Cerebras hackathon\".")
        if host is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", host):
            raise ExtensionError("That host id does not look right.")
        self.news_serial = getattr(self, "news_serial", 0) + 1
        key = f"news-{int(self.clock() * 1000):x}-{self.news_serial}"
        request = {"key": key, "topic": topic, "host": host, "state": "looking", "message": "Looking it up.", "at": self.clock(),
                   "job_id": None, "track_id": None, "headlines": []}
        self.requests = (self.requests + [request])[-20:]
        return {"key": key, "state": "looking", "message": f"Looking up news on {topic}."}

    def _look_up(self, request: dict, status: dict) -> None:
        query, interests = topic_query(request["topic"])
        source = {"url": "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en",
                  "name": "Google News", "kind": "reported"}
        researcher = {"name": "News on request", "purpose": "Topics asked for from the bar or the studio", "interests": interests,
                      "sources": [source], "review": "auto", "freshness_hours": 720, "paused": True}
        if self.news_researcher:
            researcher["id"] = self.news_researcher
        try:
            saved = self.station.action("research.save", {"researcher": researcher})
        except Exception as exc:
            if self.news_researcher and "exist" in str(exc).lower():   # removed by hand: make a new one next time
                self.news_researcher = None
                return
            return self._news_failed(request, str(exc))
        self.news_researcher = (saved.get("researcher") or {}).get("id") or self.news_researcher
        try:
            ran = self.station.action("research.run_now", {"researcher_id": self.news_researcher})
        except Exception as exc:
            return self._news_failed(request, str(exc))
        if ran.get("state") != "done":
            return   # still reading its source: try again on the next pass
        brief = None
        while True:   # the newest brief is this request's; older ones are earlier topics and are used up
            try:
                got = self.station.action("research.claim", {"researcher_id": self.news_researcher}).get("brief")
            except Exception as exc:
                return self._news_failed(request, str(exc))
            if not got:
                break
            brief = got
        if not brief or ran.get("outcome") != "ready":
            # The research service says why in plain words (for example "2 older than you asked for").
            request.update(state="nothing", message=(ran.get("message") or f"No recent news found on {request['topic']}.")[:300])
            return
        request["headlines"] = [item.get("headline_speech") or item.get("headline") for item in (brief.get("items") or [])][:5]
        host = request.get("host") or self.settings["news_host"] or (self.settings["hosts"] or self._hosts() or [""])[0]
        try:
            script = self.station.action("hosts.compose_update", {"persona": host, "brief": speakable_news(brief, request["topic"]),
                                                                  "facts": self._facts(status), "target_seconds": 45})
        except Exception as exc:
            self._release(brief.get("id"))
            return self._news_failed(request, str(exc))
        if not self._enqueue("update", script, None, f"director-{request['key']}", brief_id=brief.get("id"),
                             priority=NEWS_PRIORITY, next_up=True, request=request["key"]):
            self._release(brief.get("id"))
            return self._news_failed(request, "The voice maker did not take it.")
        request.update(state="recording", message="Recording it.", job_id=self.pending[-1]["job_id"])

    def _news_failed(self, request: dict, why: str) -> None:
        request.update(state="failed", message=why[:200])

    def _request(self, key: Optional[str]) -> Optional[dict]:
        for request in self.requests:
            if request["key"] == key:
                return request
        return None

    def _release(self, brief_id: Optional[str]) -> None:
        if brief_id:
            try:
                self.station.action("research.release", {"brief_id": brief_id})
            except Exception:
                pass

    def _enqueue(self, role: str, script: dict, target: Optional[str], key: str, brief_id: Optional[str] = None, *,
                 priority: Optional[int] = None, next_up: bool = False, request: Optional[str] = None) -> bool:
        text = (script or {}).get("text")
        if not text:
            return False
        payload = {"text": text}
        if isinstance(script.get("speaker"), int):
            payload["speaker"] = script["speaker"]
        try:
            reply = self.station.action("generation.enqueue", {"role": role, "payload": payload,
                                                              "priority": priority or self.settings["priority"],
                                                              "idempotency_key": key[:120]})
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return False
        job = reply.get("job") or {}
        if not job.get("id"):
            return False
        self.pending.append({"job_id": job["id"], "role": role, "target": target, "brief_id": brief_id, "at": self.clock(),
                             "next": next_up, "request": request})
        return True

    # ---- placement -----------------------------------------------------------------------------------
    def _follow(self, queue: list) -> None:
        keep = []
        for clip in self.pending:
            try:
                job = self.station.action("generation.job", {"job_id": clip["job_id"]}).get("job") or {}
            except Exception as exc:
                self.last_error = str(exc)[:200]
                keep.append(clip)
                continue
            state = job.get("state")
            if state not in DONE_STATES:
                keep.append(clip)
                continue
            if state != "succeeded" or not job.get("track_id"):
                self.dropped += 1
                self._release(clip.get("brief_id"))
                request = self._request(clip.get("request"))
                if request:
                    self._news_failed(request, job.get("error") or "The recording failed.")
                continue
            self._place(clip, job["track_id"], queue)
        self.pending = keep

    def _place(self, clip: dict, track_id: str, queue: list) -> None:
        position = None
        if clip.get("target"):
            for index, entry in enumerate(queue):
                if str(entry["id"]) == clip["target"]:
                    position = index
                    break
            if position is None:   # its song already played or was removed: never air an intro late
                self.dropped += 1
                try:
                    self.station.action("library.delete", {"track_id": track_id})
                except Exception:
                    pass
                return
        else:
            position = 0 if clip.get("next") or not queue else 1   # a request plays next; other clips after the next song
        try:
            self.station.action("queue.add", {"track_id": track_id, "position": position})
            self.placed += 1
            request = self._request(clip.get("request"))
            if request:
                request.update(state="queued", message="Plays next.", track_id=track_id)
        except Exception as exc:
            self.last_error = str(exc)[:200]


class DirectorExtension(Extension):
    name = "director"

    def __init__(self, *, tick_s: float = TICK_S, autostart: bool = True):
        self.tick_s = tick_s
        self.autostart = autostart
        self.ctx = None
        self.director: Optional[Director] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, ctx) -> None:
        self.ctx = ctx
        saved = self._read("settings.json")
        settings = clean_settings(ctx.config())
        settings = clean_settings(saved, settings) if saved else settings
        self.director = Director(ctx.station, settings, ctx.clock, self._read("state.json"))
        if self.autostart:
            self._thread = threading.Thread(target=self._run, name="myradio-director", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _read(self, name: str) -> dict:
        try:
            value = json.loads((self.ctx.state_dir / name).read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.ctx.write_private("state.json", json.dumps(self.director.state()).encode())
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._wake.wait(self.tick_s)
            self._wake.clear()

    def step(self) -> None:
        with self._lock:
            try:
                self.director.tick()
            except Exception as exc:  # the station may be stopping; try again next time
                self.director.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._save()

    def on_event(self, event: dict) -> None:
        if event.get("type") in ("track_started", "queue_changed"):
            self._wake.set()

    def persistent_files(self):
        return ("settings.json", "state.json", "config.json")

    def status(self) -> dict:
        d = self.director
        if d is None:
            return {"enabled": False}
        return {"enabled": d.settings["enabled"], "settings": dict(d.settings), "waiting": len(d.pending),
                "placed": d.placed, "dropped": d.dropped, "last_error": d.last_error or None,
                "requests": [{k: r.get(k) for k in ("key", "topic", "state", "message", "track_id", "headlines")} for r in d.requests[-8:]]}

    def operations(self):
        return [Operation("configure", "Turn the autopilot DJ on or off and set how often it talks: enabled, every_songs (an intro before "
                                       "every Nth song), station_id_minutes, news_minutes (approved research updates; 0 turns a kind "
                                       "off), hosts (host ids to rotate), news_host, ahead (clips made in advance), priority.",
                          {"settings": Arg("object", "Any of the settings above.")}, self._op_configure),
                Operation("news_now", "Look a topic up in the news now and have a host read what was found on air next (sourced headlines "
                                      "from a Google News search). Returns at once; follow it by key in the director's status.",
                          {"topic": Arg("string", "What to look up, for example Cerebras hackathon.", max_length=200),
                           "host": Arg("string", "Host id to read it; leave out for the news host.", required=False, max_length=63)},
                          self._op_news_now)]

    def _op_news_now(self, ctx, args):
        with self._lock:
            reply = self.director.ask_news(args["topic"], args.get("host"))
        self._wake.set()
        return reply

    def _op_configure(self, ctx, args):
        raw = args.get("settings")
        if not isinstance(raw, dict):
            raise ExtensionError("Send the settings as an object, for example {\"enabled\": true}.")
        with self._lock:
            settings = clean_settings(raw, self.director.settings)
            changed = {k: v for k, v in settings.items() if k in raw}
            saved = {**self._read("settings.json"), **changed}
            self.ctx.write_private("settings.json", json.dumps(saved).encode())
            self.director.settings = settings
        self._wake.set()
        return {"settings": settings}


def build() -> DirectorExtension:
    return DirectorExtension()
