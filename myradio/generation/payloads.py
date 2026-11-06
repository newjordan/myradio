"""Roles and the exact shape of the work a producer may ask for.

Roles (shared vocabulary): ``song`` (music, YuE2), ``host_break`` (host speech between songs, Drama 3),
``id`` (station ID, Drama 3), ``spot`` (sponsor message, Drama 3) and ``update`` (research bulletin read
by an anchor, Drama 3). A song lands in the library as ``music``; host_break, id and update land as
``speech``; a spot lands as ``ad``.

Payload for a speech role (``host_break``, ``id``, ``spot``, ``update``)::

    text        required string, 2 to 3000 characters of plain script
    speaker     integer 0 to 31, default 0 (written as <|speaker:N|> for the voice maker)
    language    short code such as "en" or "pt-BR", default "en" (recorded; the voice maker infers it from the text)
    seed        integer 0 to 4294967295; leave out to let the engine choose and record one
    max_seconds number 1 to 180, the longest result accepted (default depends on the role)
    title       optional library title, up to 120 characters
    allow_tags  boolean, default false. Set true only for scripts the station wrote itself: it keeps
                well-formed <|speaker:N|> and [event] tags. With false every marker is defused.
    meta        optional small object of your own bookkeeping (kept with the job, never interpreted)

Payload for ``song``::

    tags        required string, 3 to 800 characters of style description
    lyrics      optional string up to 3000 characters. Leave out (or empty) for an instrumental. When
                present it needs at least one section line such as [verse] or [chorus].
    duration_s  number 5 to 580 (and no more than the configured song limit), default 150
    prompt_id   optional preset slug ([a-z0-9][a-z0-9-]{0,62}) kept in provenance
    seed        integer 0 to 4294967295, as above
    steps       integer 1 to 64, default 8
    title       optional library title, up to 120 characters
    meta        as above

Anything else is rejected, so a misspelt field never silently does nothing.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Mapping

from .errors import GenerationError
from .speech import clean_block, clean_line, neutralize_speech_tags, restrict_speech_tags
from .util import finite_number

ROLES = ("song", "host_break", "id", "spot", "update")
SPEECH_ROLES = ("host_break", "id", "spot", "update")
KIND_FOR_ROLE = {"song": "music", "host_break": "speech", "id": "speech", "spot": "ad", "update": "speech"}
ROLE_LABEL = {"song": "Song", "host_break": "Host break", "id": "Station ID", "spot": "Sponsor message", "update": "Update"}
ROLE_LABEL_PLURAL = {"song": "songs", "host_break": "host breaks", "id": "station IDs", "spot": "sponsor messages", "update": "updates"}
DEFAULT_MAX_SECONDS = {"host_break": 45.0, "id": 15.0, "spot": 75.0, "update": 150.0}
CHARS_PER_SECOND = 14.0  # typical speech rate, used only to judge whether a result is plausibly complete

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
LANG_RE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")
KEY_RE = re.compile(r"[A-Za-z0-9._:/@-]{1,120}")
META_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
SECTION_RE = re.compile(r"^\[[^\]\n]{2,60}\]\s*$", re.M)
MAX_SEED = 2 ** 32 - 1
MAX_PAYLOAD_BYTES = 16384

SPEECH_FIELDS = frozenset({"text", "speaker", "language", "seed", "max_seconds", "title", "allow_tags", "meta"})
SONG_FIELDS = frozenset({"tags", "lyrics", "duration_s", "prompt_id", "seed", "steps", "title", "meta"})


@dataclasses.dataclass(frozen=True)
class Limits:
    max_song_seconds: int = 420
    max_speech_chars: int = 3000


@dataclasses.dataclass(frozen=True)
class Normalized:
    role: str
    data: dict            # canonical, JSON-safe, what is stored with the job
    seed_pinned: bool     # True when the producer fixed the seed
    title: str            # library title
    summary: str          # one short line for lists
    est_seconds: float    # rough length of the result, used only to estimate how much is on the way
    min_s: float          # shortest plausible result
    max_s: float          # longest accepted result
    fingerprint: str      # identifies the request for idempotency (the chosen seed is not part of it)


def _fail(message: str) -> GenerationError:
    return GenerationError(message)


def _integer(data: Mapping, key: str, lo: int, hi: int, default=None):
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not finite_number(value) or value != int(value):
        raise _fail(f"{key} must be a whole number from {lo} to {hi}.")
    value = int(value)
    if not lo <= value <= hi:
        raise _fail(f"{key} must be a whole number from {lo} to {hi}.")
    return value


def _number(data: Mapping, key: str, lo: float, hi: float, default=None):
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not finite_number(value):
        raise _fail(f"{key} must be a number from {lo:g} to {hi:g}.")
    if not lo <= value <= hi:
        raise _fail(f"{key} must be a number from {lo:g} to {hi:g}.")
    return float(value)


def _text(data: Mapping, key: str, lo: int, hi: int, *, multiline=False, required=True):
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _fail(f"{key} is required.")
        return None
    if not isinstance(value, str):
        raise _fail(f"{key} must be text.")
    if len(value) > hi * 4:
        raise _fail(f"{key} is too long (at most {hi} characters).")
    value = clean_block(value) if multiline else clean_line(value)
    if not lo <= len(value) <= hi:
        raise _fail(f"{key} must be {lo} to {hi} characters." if lo > 1 else f"{key} must be at most {hi} characters.")
    return value


def _meta(data: Mapping):
    meta = data.get("meta")
    if meta is None:
        return {}
    if not isinstance(meta, dict) or len(meta) > 16:
        raise _fail("meta must be a small object with at most 16 entries.")
    clean = {}
    for key, value in meta.items():
        if not isinstance(key, str) or not META_KEY_RE.fullmatch(key):
            raise _fail("meta names may use letters, digits, dot, dash and underscore (up to 40 characters).")
        if isinstance(value, str):
            value = clean_line(value)[:200]
        elif isinstance(value, bool) or value is None:
            pass
        elif isinstance(value, (int, float)):
            if not finite_number(value):
                raise _fail("meta values must be text, numbers, true/false or empty.")
        else:
            raise _fail("meta values must be text, numbers, true/false or empty.")
        clean[key] = value
    if len(json.dumps(clean)) > 2048:
        raise _fail("meta is too large (at most about 2 KB).")
    return clean


def _reject_unknown(payload: Mapping, allowed, role: str) -> None:
    extra = sorted(str(k) for k in payload if k not in allowed)
    if extra:
        raise _fail(f"Unknown field(s) for a {role} job: {', '.join(extra[:5])}. Allowed: {', '.join(sorted(allowed))}.")


def _fingerprint(role: str, data: Mapping) -> str:
    canonical = json.dumps({"role": role, "data": {k: v for k, v in data.items() if k != "seed_choice"}},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _snippet(text: str, words: int = 6, limit: int = 60) -> str:
    out = " ".join(text.split()[:words])
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def normalize_payload(role: str, payload: Any, limits: Limits = Limits()) -> Normalized:
    """Validate and clean a payload. Raises :class:`GenerationError` with a plain-language reason."""
    if role not in ROLES:
        raise _fail(f"Unknown kind of audio: {role!r}. Choose one of {', '.join(ROLES)}.")
    if not isinstance(payload, dict):
        raise _fail("The details of the job must be an object.")
    try:
        if len(json.dumps(payload)) > MAX_PAYLOAD_BYTES * 2:
            raise _fail("The details of the job are too large.")
    except (TypeError, ValueError, RecursionError):
        raise _fail("The details of the job could not be read.") from None
    if role == "song":
        return _normalize_song(payload, limits)
    return _normalize_speech(role, payload, limits)


def _normalize_speech(role: str, payload: dict, limits: Limits) -> Normalized:
    _reject_unknown(payload, SPEECH_FIELDS, role)
    allow_tags = payload.get("allow_tags", False)
    if not isinstance(allow_tags, bool):
        raise _fail("allow_tags must be true or false.")
    raw = payload.get("text")
    if not isinstance(raw, str):
        raise _fail("text is required." if raw is None else "text must be text.")
    if len(raw) > limits.max_speech_chars * 3:
        raise _fail(f"text is too long (at most {limits.max_speech_chars} characters).")
    text = restrict_speech_tags(raw) if allow_tags else neutralize_speech_tags(raw)
    spoken = spoken_text(text)
    if len(spoken) < 2:
        raise _fail("text needs at least a couple of words to speak.")
    if len(text) > limits.max_speech_chars:
        raise _fail(f"text is too long (at most {limits.max_speech_chars} characters).")
    speaker = _integer(payload, "speaker", 0, 31, 0)
    language = payload.get("language", "en")
    if language is None:
        language = "en"
    if not isinstance(language, str) or not LANG_RE.fullmatch(language):
        raise _fail('language must be a short code such as "en" or "pt-BR".')
    seed = _integer(payload, "seed", 0, MAX_SEED, None)
    max_seconds = _number(payload, "max_seconds", 1, 180, DEFAULT_MAX_SECONDS[role])
    title = _text(payload, "title", 1, 120, required=False)
    meta = _meta(payload)
    data = {"text": text, "speaker": speaker, "language": language, "max_seconds": max_seconds}
    if allow_tags:
        data["allow_tags"] = True
    if seed is not None:
        data["seed"] = seed
    if title:
        data["title"] = title
    if meta:
        data["meta"] = meta
    expected = len(spoken) / CHARS_PER_SECOND
    est = max(1.0, min(max_seconds, expected))
    min_s, max_s = window_for(role, data)
    default_title = f"{ROLE_LABEL[role]}: {_snippet(spoken)}" if role in ("host_break", "update") else ROLE_LABEL[role]
    return Normalized(role, data, seed is not None, title or default_title[:120], _snippet(spoken, 12, 90), est,
                      min_s, max_s, _fingerprint(role, data))


def _normalize_song(payload: dict, limits: Limits) -> Normalized:
    _reject_unknown(payload, SONG_FIELDS, "song")
    tags = _text(payload, "tags", 3, 800)
    if tags.startswith("-"):
        raise _fail("tags cannot start with a dash.")
    lyrics = payload.get("lyrics")
    if lyrics is None or (isinstance(lyrics, str) and not lyrics.strip()):
        lyrics = ""
    else:
        lyrics = _text(payload, "lyrics", 10, 3000, multiline=True)
        if lyrics.startswith("-"):
            raise _fail("lyrics cannot start with a dash.")
        if not SECTION_RE.search(lyrics):
            raise _fail("lyrics need at least one section line on its own, such as [verse] or [chorus].")
    ceiling = max(5, min(580, int(limits.max_song_seconds)))
    duration = _integer(payload, "duration_s", 5, ceiling, 150)
    if duration is None or duration > ceiling:
        duration = min(150, ceiling)
    prompt_id = payload.get("prompt_id")
    if prompt_id is not None:
        if not isinstance(prompt_id, str) or not SLUG_RE.fullmatch(prompt_id):
            raise _fail("prompt_id must be a lowercase slug such as warm-tape-loop.")
    seed = _integer(payload, "seed", 0, MAX_SEED, None)
    steps = _integer(payload, "steps", 1, 64, 8)
    title = _text(payload, "title", 1, 120, required=False)
    meta = _meta(payload)
    data = {"tags": tags, "duration_s": duration, "steps": steps}
    if lyrics:
        data["lyrics"] = lyrics
    if prompt_id:
        data["prompt_id"] = prompt_id
    if seed is not None:
        data["seed"] = seed
    if title:
        data["title"] = title
    if meta:
        data["meta"] = meta
    if title:
        shown = title
    elif prompt_id:
        shown = prompt_id.replace("-", " ").title()
    else:
        shown = "Generated song"
    summary = shown if title or prompt_id else _snippet(tags, 10, 90)
    min_s, max_s = window_for("song", data)
    return Normalized("song", data, seed is not None, shown[:120], summary, float(duration), min_s, max_s, _fingerprint("song", data))


def is_instrumental(data: Mapping) -> bool:
    return not data.get("lyrics")


def spoken_text(text: str) -> str:
    """The words that will actually be heard: the script without speaker and event markers."""
    return " ".join(re.sub(r"<\|speaker:\d+\|>|\[[a-z \-]+\]", " ", text).split())


def window_for(role: str, data: Mapping) -> tuple:
    """``(min_s, max_s)``: the shortest plausible and the longest accepted result for a normalised payload."""
    if role == "song":
        duration = float(data["duration_s"])
        return max(1.0, min(0.3 * duration, 45.0)), min(duration * 1.5 + 15.0, 580.0)
    max_seconds = float(data["max_seconds"])
    expected = len(spoken_text(data["text"])) / CHARS_PER_SECOND
    return max(0.4, min(0.35 * expected, max_seconds * 0.8)), max_seconds + 3.0
