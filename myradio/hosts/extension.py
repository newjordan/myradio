"""The hosts extension: on-air personalities and the script composer.

It writes text only. It never makes audio, never calls a model and never touches the network by
default; the generation lane turns a script into speech. Everything it knows is kept in its own
SQLite file under the extension's directory.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Mapping, Optional, Tuple

from myradio.extensions import Arg, Context, Extension, ExtensionError, Operation, Request, Route

from .errors import HostsError
from .formats import FORMATS
from .segments import SEGMENT_IDS
from .service import HostsService
from .textsafe import EVENT_TAGS

DB_FILE = "hosts.sqlite3"
_TAG_NAME = re.compile(r"[a-z][a-z ]{0,24}")
_PREVIEW_KEYS = frozenset(("persona", "segment", "format", "facts", "brief", "seed", "target_seconds", "live", "use_ai"))
PREVIEW_NOTE = "Text only. No audio was made."
MAX_CONFIG_NOTES = 8     # problems found in config.json that are shown (a file full of junk cannot flood the doctor)


class HostsExtension(Extension):
    name = "hosts"

    def __init__(self) -> None:
        self.service: Optional[HostsService] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._notes: List[str] = []

    # ---- lifecycle -------------------------------------------------------------------------------
    def start(self, ctx: Context) -> None:
        tag_map, zone = self._read_config(ctx)
        try:
            conn = ctx.open_db(DB_FILE)
            try:
                self.service = HostsService(conn, clock=ctx.clock, tag_map=tag_map, timezone=zone)
            except sqlite3.Error:
                conn.close()
                raise
            except Exception:
                conn.close()
                raise
        except sqlite3.Error:
            raise HostsError("The saved hosts file is damaged. Move hosts.sqlite3 out of the hosts folder and restart "
                             "myradio; the built-in hosts will work again.") from None
        self._conn = conn      # kept so tests can see that stop() released it; the service owns it

    def stop(self) -> None:
        service, self._conn = self.service, None
        if service is not None:
            service.close()

    def _read_config(self, ctx: Context) -> "Tuple[Dict[str, Optional[str]], Optional[str]]":
        """Optional operator settings in config.json: ``timezone`` and ``event_tags`` (a map from the
        effect names scripts use to the names the speech model expects, or null to switch one off).
        A bad file never stops the extension; it is reported by the doctor check instead."""
        self._notes = []
        try:
            config = ctx.config()
        except ExtensionError as exc:
            self._notes.append(f"config.json was not used: {exc}")
            return {}, None
        tag_map: Dict[str, Optional[str]] = {}
        given = config.get("event_tags", {})
        if isinstance(given, dict):
            for key, value in given.items():
                if key in EVENT_TAGS and (value is None or (isinstance(value, str) and _TAG_NAME.fullmatch(value))):
                    tag_map[key] = value
                else:
                    self._notes.append(f"The effect setting {str(key)[:20]!r} in config.json was ignored.")
        elif given:
            self._notes.append("The event_tags setting in config.json must be an object, so it was ignored.")
        zone = config.get("timezone")
        if zone is not None and not (isinstance(zone, str) and 0 < len(zone) <= 64):
            self._notes.append("The timezone setting in config.json was ignored.")
            zone = None
        if len(self._notes) > MAX_CONFIG_NOTES:
            more = len(self._notes) - MAX_CONFIG_NOTES + 1
            self._notes = self._notes[:MAX_CONFIG_NOTES - 1] + [f"{more} more settings in config.json were ignored."]
        return tag_map, zone

    # ---- operations ----------------------------------------------------------------------------------
    def operations(self) -> List[Operation]:
        return [
            Operation("save", "Create or update one of your own on-air hosts: a made-up character with a name, role, "
                              "personality, voice slot, pace and phrases. Built-in hosts cannot be edited.",
                      {"persona": Arg("object", "The host: name, role, blurb, traits, speaker, pace, verbosity, voice, "
                                                "lexicon, allowed_tags, prohibited_topics.")}, self._op_save),
            Operation("remove", "Remove one of your own hosts. A built-in host cannot be removed, so it is hidden instead.",
                      {"id": Arg("string", "The host's id.", max_length=63)}, self._op_remove),
            Operation("restore", "Bring back a built-in host that was hidden.",
                      {"id": Arg("string", "The host's id.", max_length=63)}, self._op_restore),
            Operation("list", "List the on-air hosts with their roles and descriptions.",
                      {"include_hidden": Arg("boolean", "Also list hidden built-in hosts.", required=False)}, self._op_list),
            Operation("compose", "Write the words a host will say, using only the facts given. Text only, no audio. "
                                 "Choose a segment or a format, not both.",
                      {"persona": Arg("string", "Host id.", max_length=63),
                       "segment": Arg("string", "What to say.", required=False, enum=SEGMENT_IDS),
                       "format": Arg("string", "A made-up entertainment piece instead of a segment.", required=False,
                                     enum=tuple(FORMATS)),
                       "facts": Arg("object", "Facts: station, tagline, host, now_title, now_artist, next_title, next_artist, "
                                              "local_time (HH:MM), daypart, weekday, notes, sponsor, sponsor_copy, topic, "
                                              "item_count, sources.", required=False),
                       "seed": Arg("integer", "Repeat a choice by reusing its seed.", required=False, minimum=0),
                       "target_seconds": Arg("number", "About how long it should take to say.", required=False, minimum=1),
                       "dry_run": Arg("boolean", "Preview without using up the host's lines.", required=False),
                       "use_ai": Arg("boolean", "Ask the connected writing assistant, if there is one.", required=False)},
                      self._op_compose),
            Operation("compose_update", "Turn a research brief into a spoken update that names every source and reads each "
                                        "item exactly as written. Text only, no audio.",
                      {"persona": Arg("string", "Host id.", max_length=63),
                       "brief": Arg("object", "The research lane's Brief as it is, or topic and items, each with text, source and "
                                              "optionally url, published_at and kind."),
                       "facts": Arg("object", "Station facts such as station, host and local_time.", required=False),
                       "seed": Arg("integer", "Repeat a choice by reusing its seed.", required=False, minimum=0),
                       "target_seconds": Arg("number", "About how long the whole update should take.", required=False, minimum=1),
                       "dry_run": Arg("boolean", "Preview without using up the host's lines.", required=False)},
                      self._op_compose_update),
        ]

    def _need(self) -> HostsService:
        if self.service is None:
            raise HostsError("Hosts are not ready yet. Try again in a moment.")
        return self.service

    def _op_save(self, ctx: Context, args: dict) -> dict:
        result = self._need().save_persona(args["persona"])
        self._checkpoint()
        return result

    def _op_remove(self, ctx: Context, args: dict) -> dict:
        result = self._need().remove_persona(args["id"])
        self._checkpoint()
        return result

    def _op_restore(self, ctx: Context, args: dict) -> dict:
        result = self._need().restore_persona(args["id"])
        self._checkpoint()
        return result

    def _op_list(self, ctx: Context, args: dict) -> dict:
        service = self._need()
        hosts = [{"id": p["id"], "name": p["name"], "role": p["role"], "blurb": p["blurb"], "voice": p["voice"],
                  "speaker": p["speaker"], "builtin": p["builtin"], "hidden": p["hidden"]}
                 for p in (service.public(x) for x in service.personas(bool(args.get("include_hidden"))))]
        return {"hosts": hosts, "count": len(hosts)}

    def _op_compose(self, ctx: Context, args: dict) -> dict:
        script = self._need().compose(args["persona"], args.get("segment"), args.get("facts"), format=args.get("format"),
                                      seed=args.get("seed"), target_seconds=args.get("target_seconds"),
                                      dry_run=bool(args.get("dry_run")), use_ai=bool(args.get("use_ai")))
        return script.to_dict()

    def _op_compose_update(self, ctx: Context, args: dict) -> dict:
        script = self._need().compose_update(args["persona"], args["brief"], args.get("facts"), seed=args.get("seed"),
                                             target_seconds=args.get("target_seconds"), dry_run=bool(args.get("dry_run")))
        return script.to_dict()

    def _checkpoint(self) -> None:
        """Keep the main database file current so a backup that copies only it is complete."""
        if self.service is not None:
            self.service.checkpoint()

    # ---- routes ----------------------------------------------------------------------------------------
    def routes(self) -> List[Route]:
        return [Route("GET", "/personas", self._route_personas),
                Route("GET", "/formats", self._route_formats),
                Route("POST", "/preview", self._route_preview, max_body=65536)]

    def _route_personas(self, ctx: Context, request: Request) -> dict:
        flag = (request.query.get("hidden") or ["0"])[0]
        return self._need().catalog(include_hidden=str(flag).lower() in ("1", "true", "yes"))

    def _route_formats(self, ctx: Context, request: Request) -> dict:
        return {"formats": self._need().formats(),
                "note": "These are made-up pieces. Each one starts with a plain notice that it is not real."}

    def _route_preview(self, ctx: Context, request: Request) -> dict:
        """A dry run: the words a host would say, never audio, never using up the host's lines."""
        service = self._need()
        body = request.json
        if not isinstance(body, dict):
            raise HostsError("The request was not understood. Say which host, what it should say, and the facts to use.")
        unknown = sorted(str(k)[:20] for k in body if k not in _PREVIEW_KEYS)
        if unknown:
            raise HostsError("These fields are not understood: " + ", ".join(unknown[:5]) + ".")
        extra_warnings: List[str] = []
        facts = body.get("facts")
        if body.get("live") is True:
            try:
                facts = service.live_facts(ctx.station.status(), facts if isinstance(facts, Mapping) else None)
            except Exception:
                extra_warnings.append("The station's current songs could not be read, so only the facts you gave were used.")
        elif body.get("live") not in (None, False):
            raise HostsError("Live must be true or false.")
        persona, segment = body.get("persona"), body.get("segment")
        if segment == "update":
            if body.get("format") is not None:
                raise HostsError("Choose an update or a format, not both.")
            script = service.compose_update(persona, body.get("brief"), facts, seed=body.get("seed"),
                                            target_seconds=body.get("target_seconds"), dry_run=True)
        else:
            script = service.compose(persona, segment, facts, format=body.get("format"), seed=body.get("seed"),
                                     target_seconds=body.get("target_seconds"), dry_run=True,
                                     use_ai=body.get("use_ai") is True)
        payload = script.to_dict()
        payload["warnings"] = list(payload["warnings"]) + extra_warnings
        payload.update({"preview": True, "audio": False, "note": PREVIEW_NOTE})
        return payload

    # ---- status, checks, backup, events -----------------------------------------------------------------
    def status(self) -> dict:
        return self.service.status() if self.service is not None else {}

    def checks(self) -> List[dict]:
        service = self.service
        if service is None:
            return []
        counts = service.store.counts
        out = [{"id": "cast", "title": "On-air hosts", "status": "ok",
                "detail": f"{counts['builtin']} built-in hosts and {counts['custom']} of your own are ready.", "hint": ""},
               {"id": "writing", "title": "Script writing", "status": "ok",
                "detail": ("A writing assistant is connected. Its text is used only when it passes the fact check."
                           if service.status()["writer"] == "assistant" else
                           "Scripts are written from templates, using only facts the station has. No AI assistant is needed."),
                "hint": ""}]
        problems = service.store.bad_rows
        storage = service.store.write_errors + getattr(service.memory, "write_errors", 0)
        if problems or storage:
            out.append({"id": "storage", "title": "Saved hosts", "status": "warn",
                        "detail": (f"{problems} saved host(s) could not be read and were left out. " if problems else "")
                                  + ("Some changes could not be saved." if storage else ""),
                        "hint": "Check free disk space. Your built-in hosts are not affected."})
        else:
            out.append({"id": "storage", "title": "Saved hosts", "status": "ok",
                        "detail": "Your own hosts are stored safely.", "hint": ""})
        for number, note in enumerate(self._notes, 1):
            # one warning per problem; the ids differ so the doctor (which keeps the first check of an id) shows them all
            out.append({"id": "config" if number == 1 else f"config-{number}", "title": "Host settings", "status": "warn",
                        "detail": note, "hint": "Fix or remove the setting in the hosts config.json file."})
        return out

    def persistent_files(self) -> List[str]:
        return [DB_FILE, "config.json"]

    def on_event(self, event: dict) -> None:
        if self.service is not None:
            self.service.note_event(event)


def build() -> HostsExtension:
    return HostsExtension()
