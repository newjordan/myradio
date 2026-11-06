"""The hosts service: one thread-safe object that holds the cast, the composer and the memory.

The extension is a thin adapter over this class, and other lanes (the director, the shows planner)
can use it directly. It never speaks, never makes audio and never makes a network call.
"""
from __future__ import annotations

import collections
import datetime as dt
import sqlite3
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional

from .briefs import compose_update
from .composer import ComposerConfig, Script, TemplateComposer
from .errors import HostsError
from .facts import facts_from_status
from .formats import FORMATS, compose_format
from .llm import DEFAULT_TIMEOUT, LlmComposer
from .memory import Memory
from .personas import LIMITS, MAX_LEXICON_VARIANTS, MAX_TOPICS, MAX_TRAITS, PACES, Persona
from .segments import LEXICON_KEYS, SEGMENTS
from .store import MAX_CUSTOM, HostsStore, SqliteMemory
from .templates import MIN_TEMPLATES, TemplateLibrary
from .textsafe import EVENT_TAGS, MAX_SPEAKER
from .personas import load_builtin_personas

RECENT_MUSIC = 8


class HostsService:
    def __init__(self, conn: Optional[sqlite3.Connection] = None, *, clock: Callable[[], float] = time.time,
                 tag_map: Optional[Mapping[str, Optional[str]]] = None, library: Optional[TemplateLibrary] = None,
                 timezone: Optional[str] = None):
        self.clock = clock
        self.library = library or TemplateLibrary.builtin()
        self.lock = threading.RLock()
        self._conn = conn
        if conn is not None:
            # In write-ahead-log mode this keeps the file safe from corruption while skipping a disk sync on
            # every repetition-memory write. Saved hosts are still synced (the extension checkpoints after them).
            conn.execute("PRAGMA synchronous=NORMAL")
        builtin = load_builtin_personas(self.library.voices, EVENT_TAGS)
        self.store = HostsStore(conn, builtin, self.library.voices, EVENT_TAGS, lock=self.lock, clock=clock)
        self.memory: Memory = SqliteMemory(conn, self.lock) if conn is not None else Memory()
        self.composer = TemplateComposer(self.library, self.memory, ComposerConfig(clock=clock), tag_map)
        self.tz = _zone(timezone)
        self._llm: Optional[LlmComposer] = None
        self._stats = {"total": 0, "template": 0, "plain": 0, "assistant": 0, "format": 0, "update": 0, "last_at": None}
        self._stats_lock = threading.Lock()
        self._recent: Deque[Dict[str, str]] = collections.deque(maxlen=RECENT_MUSIC)
        self._recent_lock = threading.Lock()
        counts = self.library.counts()
        self._template_total = sum(n for per in counts.values() for n in per.values())
        self._template_min = min(n for per in counts.values() for n in per.values())

    # ---- the cast -----------------------------------------------------------------------------------
    def personas(self, include_hidden: bool = False) -> List[Persona]:
        return self.store.personas(include_hidden)

    def persona(self, persona_id: Any) -> Persona:
        return self.store.get(persona_id)

    def save_persona(self, raw: Any) -> dict:
        persona, created, warnings = self.store.save(raw)
        return {"persona": self.public(persona), "created": created, "warnings": warnings}

    def remove_persona(self, persona_id: Any) -> dict:
        result = self.store.remove(persona_id)
        if result.get("removed"):
            self.memory.forget(persona_id)
        return result

    def restore_persona(self, persona_id: Any) -> dict:
        return self.store.restore(persona_id)

    def public(self, persona: Persona) -> dict:
        pack = self.library.voices.get(persona.voice)
        data = persona.to_public(hidden=self.store.is_hidden(persona.id), voice_label=pack.label if pack else "")
        data["templates"] = {kind: len(items) for kind, items in pack.templates.items()} if pack else {}
        return data

    def template_count(self, persona_id: Any, segment: str) -> int:
        """How many ready-made lines this host can draw on for one kind of script."""
        persona = self.persona(persona_id)
        return len(self.library.templates(persona.voice, segment)) if segment in SEGMENTS else 0

    # ---- writing ------------------------------------------------------------------------------------
    def set_writer(self, complete: Optional[Callable[[str, str], str]], *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Connect (or, with None, disconnect) an optional writing assistant. The function is called as
        ``complete(system, user)`` and must return text; this module never calls the network itself."""
        self._llm = LlmComposer(complete, self.composer, timeout=timeout) if complete is not None else None

    def compose(self, persona_id: Any, segment: Any = None, facts: Any = None, *, format: Any = None, seed: Any = None,
                target_seconds: Any = None, dry_run: bool = False, use_ai: bool = False) -> Script:
        if (segment is None) == (format is None):
            raise HostsError("Choose either what the host should say or a format, not both and not neither.")
        persona = self.persona(persona_id)
        remember = not dry_run
        if format is not None:
            script = compose_format(self.composer, persona, format, facts, seed=seed, target_seconds=target_seconds,
                                    remember=remember)
        elif use_ai and self._llm is not None:
            script = self._llm.compose(persona, segment, facts, seed=seed, target_seconds=target_seconds, remember=remember)
        else:
            script = self.composer.compose(persona, segment, facts, seed=seed, target_seconds=target_seconds, remember=remember)
            if use_ai:
                script = _with_warning(script, "No writing assistant is connected, so the built-in script was used.")
        self._count(script, dry_run)
        return script

    def compose_update(self, persona_id: Any, brief: Any, facts: Any = None, *, seed: Any = None, target_seconds: Any = None,
                       dry_run: bool = False) -> Script:
        persona = self.persona(persona_id)
        script = compose_update(self.composer, persona, brief, facts, seed=seed, target_seconds=target_seconds,
                                remember=not dry_run)
        self._count(script, dry_run)
        return script

    def _count(self, script: Script, dry_run: bool) -> None:
        if dry_run:
            return
        with self._stats_lock:
            self._stats["total"] += 1
            key = ("update" if script.segment == "update" else "format" if script.source == "format"
                   else "assistant" if script.source == "llm" else "plain" if script.source == "plain" else "template")
            self._stats[key] += 1
            self._stats["last_at"] = self.clock()

    # ---- live facts ---------------------------------------------------------------------------------
    def note_event(self, event: Any) -> None:
        """Remember the music that has played (from station events), so a back announcement can name it."""
        if not isinstance(event, Mapping) or event.get("type") != "track_started":
            return
        track = event.get("track")
        if isinstance(track, Mapping) and track.get("kind") == "music" and isinstance(track.get("title"), str):
            artist = track.get("artist") if isinstance(track.get("artist"), str) else ""
            with self._recent_lock:
                self._recent.append({"title": track["title"][:200], "artist": artist[:200]})

    def live_facts(self, status: Any, override: Any = None) -> Dict[str, Any]:
        """Facts taken from the station's current state, with anything in ``override`` taking precedence."""
        with self._recent_lock:
            recent = list(self._recent)
        facts = facts_from_status(status, recent_music=recent, now=self.clock(), tz=self.tz)
        if isinstance(override, Mapping):
            facts.update({k: v for k, v in override.items()})
        return facts

    # ---- catalogue and status -----------------------------------------------------------------------
    def catalog(self, include_hidden: bool = False) -> dict:
        return {
            "personas": [self.public(p) for p in self.personas(include_hidden)],
            "voices": [v.to_public() for v in self.library.voices.values()],
            "segments": [self._segment_public(s) for s in SEGMENTS.values()],
            "paces": dict(PACES),
            "event_tags": list(EVENT_TAGS),
            "languages": ["en"],
            "limits": {"custom_hosts": MAX_CUSTOM, "name": LIMITS["name"], "role": LIMITS["role"], "blurb": LIMITS["blurb"],
                       "traits": MAX_TRAITS, "trait": LIMITS["trait"], "topics": MAX_TOPICS, "topic": LIMITS["topic"],
                       "phrases": MAX_LEXICON_VARIANTS, "phrase": LIMITS["phrase"], "speaker_max": MAX_SPEAKER},
            "lexicon_keys": list(LEXICON_KEYS),
            "note": "Hosts are made-up characters. They write scripts only from facts the station has.",
        }

    @staticmethod
    def _segment_public(seg: Any) -> dict:
        return {"id": seg.id, "role": seg.role, "library_kind": seg.library_kind, "label": seg.label, "summary": seg.summary,
                "requires": list(seg.requires), "default_seconds": [seg.low, seg.high],
                "min_seconds": seg.min_s, "max_seconds": seg.max_s}

    def formats(self) -> List[dict]:
        return [f.to_public() for f in FORMATS.values()]

    def checkpoint(self, truncate: bool = False) -> None:
        """Fold the write-ahead log into the main database file, so a backup of that file alone is complete."""
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(%s)" % ("TRUNCATE" if truncate else "PASSIVE"))
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        """Checkpoint and close the database. Safe to call twice."""
        with self.lock:
            conn, self._conn = self._conn, None
            if conn is None:
                return
            for statement in ("PRAGMA wal_checkpoint(TRUNCATE)",):
                try:
                    conn.execute(statement)
                except sqlite3.Error:
                    pass
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def status(self) -> dict:
        """Cheap, lock-free summary for the station's status page."""
        stats = dict(self._stats)
        return {
            "ready": True,
            "personas": dict(self.store.counts),
            "segments": len(SEGMENTS),
            "formats": len(FORMATS),
            "voices": len(self.library.voices),
            "templates": {"total": self._template_total, "fewest_per_kind": self._template_min, "required": MIN_TEMPLATES},
            "writer": "assistant" if self._llm is not None else "templates",
            "composed": stats,
            "unreadable_saved_hosts": self.store.bad_rows,
            "storage_problems": self.store.write_errors + getattr(self.memory, "write_errors", 0),
        }


def _with_warning(script: Script, message: str) -> Script:
    import dataclasses
    return dataclasses.replace(script, warnings=script.warnings + (message,))


def _zone(name: Optional[str]) -> Optional[dt.tzinfo]:
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None
