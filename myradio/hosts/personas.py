"""On-air personalities: the schema, validation and the built-in library.

Hosts are fictional characters. A persona is a name, a role, a personality and a way of
speaking; it holds no recording, no voice sample and no reference to any real person.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import unicodedata
from importlib import resources
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import HostsError
from .segments import LEXICON_KEYS
from .textsafe import EVENT_TAGS, MAX_SPEAKER, clean_tags, neutralize

ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PACES: Dict[str, float] = {"slow": 2.1, "steady": 2.5, "brisk": 2.9}   # words per second
SUPPORTED_LANGUAGES = ("en",)
MAX_TRAITS, MAX_TOPICS, MAX_LEXICON_VARIANTS, MAX_TAGS = 5, 12, 8, 5
LIMITS = {"name": 40, "role": 60, "blurb": 200, "trait": 24, "topic": 40, "phrase": 60}
TONES = ("#d8edac", "#ff946e", "#7b9daa", "#ffe9bd", "#b9d6b2", "#c9ced9", "#ffd08a")
_TONE = re.compile(r"#[0-9a-fA-F]{6}")
# Phrases that suggest a real person or a copied voice. A coarse guard, not a guarantee.
_IMPERSONATION = re.compile(
    r"\b(clone[ds]?|cloning|impersonat\w*|imitat\w*|sounds?\s+like|sounding\s+like|voice\s+of|real\s+person|"
    r"in\s+the\s+style\s+of|deepfake\w*|lookalike|sound-?alike)\b", re.IGNORECASE)
_ROUNDTRIP_KEYS = frozenset(("builtin", "hidden", "fictional", "wps", "voice_label", "templates", "note", "created_at", "updated_at"))
_KNOWN_KEYS = frozenset(("id", "name", "role", "blurb", "traits", "speaker", "allowed_tags", "pace", "verbosity",
                         "language", "prohibited_topics", "voice", "lexicon", "tone"))


def slugify(text: str, fallback: str = "host") -> str:
    folded = unicodedata.normalize("NFKD", text if isinstance(text, str) else "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:40].strip("-")
    return slug or fallback


@dataclasses.dataclass(frozen=True)
class Persona:
    id: str
    name: str
    role: str
    blurb: str
    traits: Tuple[str, ...]
    speaker: int
    allowed_tags: Tuple[str, ...]
    pace: str
    verbosity: Tuple[int, int]
    language: str
    prohibited_topics: Tuple[str, ...]
    voice: str
    lexicon: Mapping[str, Tuple[str, ...]]
    tone: str
    builtin: bool = False

    @property
    def wps(self) -> float:
        return PACES[self.pace]

    def grounding_texts(self) -> List[str]:
        """What this host may say about itself: its name and its own phrases."""
        out = [self.name]
        for phrases in self.lexicon.values():
            out.extend(phrases)
        return out

    def to_public(self, *, hidden: bool = False, voice_label: str = "") -> dict:
        return {
            "id": self.id, "name": self.name, "role": self.role, "blurb": self.blurb, "traits": list(self.traits),
            "speaker": self.speaker, "allowed_tags": list(self.allowed_tags), "pace": self.pace, "wps": self.wps,
            "verbosity": list(self.verbosity), "language": self.language,
            "prohibited_topics": list(self.prohibited_topics), "voice": self.voice, "voice_label": voice_label,
            "lexicon": {k: list(v) for k, v in self.lexicon.items()}, "tone": self.tone,
            "builtin": self.builtin, "hidden": hidden, "fictional": True,
        }

    def to_storage(self) -> dict:
        data = self.to_public()
        for key in ("wps", "voice_label", "builtin", "hidden", "fictional"):
            data.pop(key, None)
        return data


def _text(value: Any, key: str, limit: int, warnings: List[str], *, required: bool, message: str) -> str:
    if value is None or value == "":
        if required:
            raise HostsError(message)
        return ""
    if not isinstance(value, str):
        raise HostsError(message)
    cleaned = neutralize(value, limit)
    if "empty" in cleaned.flags:
        if required:
            raise HostsError(message)
        return ""
    if "markup" in cleaned.flags or "control" in cleaned.flags:
        warnings.append(f"Voice codes or hidden characters were removed from the {key}.")
    if "shortened" in cleaned.flags:
        warnings.append(f"The {key} was shortened to {limit} characters.")
    return cleaned.text


def _string_list(value: Any, key: str, item_limit: int, max_items: int, warnings: List[str], message: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise HostsError(message)
    out: List[str] = []
    for item in value[: max_items + 8]:
        if not isinstance(item, str):
            raise HostsError(message)
        cleaned = neutralize(item, item_limit)
        if cleaned.text and cleaned.text.lower() not in (x.lower() for x in out):
            out.append(cleaned.text)
        if ("markup" in cleaned.flags or "control" in cleaned.flags) and cleaned.text:
            warnings.append(f"Voice codes or hidden characters were removed from the {key}.")
    if len(out) > max_items:
        warnings.append(f"Only the first {max_items} {key} were kept.")
        out = out[:max_items]
    return tuple(out)


def validate_persona(raw: Any, voices: Mapping[str, Any], *, event_tags: Sequence[str] = EVENT_TAGS,
                     builtin: bool = False, lenient_tags: bool = False) -> "Tuple[Persona, List[str]]":
    """Check and clean a persona object. ``voices`` maps script-style ids to packs (anything with
    ``lexicon``). Returns (persona, warnings); raises HostsError with a plain-language message.
    ``lenient_tags`` drops effect tags that are no longer allowed instead of refusing (used when
    loading hosts that were saved under an older tag list)."""
    if not isinstance(raw, Mapping):
        raise HostsError("A host needs a name, a role and a short description of their personality.")
    warnings: List[str] = []
    ignored = sorted(str(k)[:24] for k in raw if k not in _KNOWN_KEYS and k not in _ROUNDTRIP_KEYS)
    if ignored:
        warnings.append("These fields are not used and were ignored: " + ", ".join(ignored[:6]) + ".")
    if raw.get("fictional") is False:
        raise HostsError("Hosts are made-up characters. A host cannot be marked as a real person.")

    name = _text(raw.get("name"), "name", LIMITS["name"], warnings, required=True,
                 message="Give the host a name of 1 to 40 characters.")
    if not any(ch.isalpha() for ch in name):
        raise HostsError("The host's name needs at least one letter.")
    role = _text(raw.get("role"), "role", LIMITS["role"], warnings, required=True,
                 message="Give the host a role such as Late-night host (up to 60 characters).")
    blurb = _text(raw.get("blurb"), "description", LIMITS["blurb"], warnings, required=True,
                  message="Add a short description of the host's personality (up to 200 characters).")
    traits = _string_list(raw.get("traits"), "traits", LIMITS["trait"], MAX_TRAITS, warnings,
                          "Traits should be a short list of words, such as Warm and Calm.")
    topics = _string_list(raw.get("prohibited_topics"), "topics to avoid", LIMITS["topic"], MAX_TOPICS, warnings,
                          "Topics to avoid should be a short list of words or phrases.")
    for text in (name, role, blurb, *traits):
        if _IMPERSONATION.search(text):
            raise HostsError("Hosts are made-up characters. Describe a personality, not a real person or a copied voice.")

    speaker = raw.get("speaker", 0)
    if isinstance(speaker, bool) or not isinstance(speaker, int) or not 0 <= speaker <= MAX_SPEAKER:
        raise HostsError(f"The voice slot must be a whole number from 0 to {MAX_SPEAKER}.")
    pace = raw.get("pace", "steady")
    if not isinstance(pace, str) or pace not in PACES:
        raise HostsError("Pace must be slow, steady or brisk.")
    verbosity = raw.get("verbosity", [2, 3])
    if (not isinstance(verbosity, (list, tuple)) or len(verbosity) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5 for v in verbosity)
            or verbosity[0] > verbosity[1]):
        raise HostsError("Talkativeness is two numbers from 1 to 5, the least and the most this host says, such as [2, 4].")
    language = raw.get("language", "en")
    if language not in SUPPORTED_LANGUAGES:
        raise HostsError("Scripts can only be written in English for now. Leave the language as en.")
    given_tags = raw.get("allowed_tags")
    if isinstance(given_tags, str):
        given_tags = [given_tags]
    allowed, rejected = clean_tags(given_tags if given_tags is not None else [], event_tags)
    if (rejected and not lenient_tags) or len(allowed) > MAX_TAGS:
        raise HostsError("Choose sound effects from: " + ", ".join(event_tags) + ".")
    voice = raw.get("voice", "warm")
    if not isinstance(voice, str) or voice not in voices:
        raise HostsError("Choose a script style: " + ", ".join(sorted(voices)) + ".")

    given = raw.get("lexicon")
    if given is not None and not isinstance(given, Mapping):
        raise HostsError("The phrases should be a set of labelled lists.")
    lexicon: Dict[str, Tuple[str, ...]] = {}
    for key in LEXICON_KEYS:
        items = _string_list((given or {}).get(key), f"{key.replace('_', ' ')} phrases", LIMITS["phrase"],
                             MAX_LEXICON_VARIANTS, warnings, "Phrases should be short pieces of text.")
        lexicon[key] = items or tuple(voices[voice].lexicon[key])
    unknown_lex = sorted(str(k)[:20] for k in (given or {}) if k not in LEXICON_KEYS)
    if unknown_lex:
        warnings.append("These phrase lists are not used and were ignored: " + ", ".join(unknown_lex[:6]) + ".")
    for phrases in lexicon.values():
        for phrase in phrases:
            if _IMPERSONATION.search(phrase):
                raise HostsError("Hosts are made-up characters. Describe a personality, not a real person or a copied voice.")

    given_id = raw.get("id")
    if given_id in (None, ""):
        host_id = slugify(name)
    elif isinstance(given_id, str) and ID_RE.fullmatch(given_id):
        host_id = given_id
    else:
        raise HostsError("The id may use lowercase letters, numbers and hyphens, up to 63 characters.")
    tone = raw.get("tone")
    if tone in (None, ""):
        tone = TONES[hashlib.sha256(host_id.encode()).digest()[0] % len(TONES)]
    elif not isinstance(tone, str) or not _TONE.fullmatch(tone):
        raise HostsError("The colour should look like #d8edac.")
    persona = Persona(host_id, name, role, blurb, traits, speaker, allowed, pace, (int(verbosity[0]), int(verbosity[1])),
                      language, topics, voice, lexicon, tone.lower(), builtin)
    return persona, warnings


def load_builtin_personas(voices: Mapping[str, Any], event_tags: Sequence[str] = EVENT_TAGS) -> Dict[str, Persona]:
    """The shipped cast, in display order. Raises HostsError if the data file is damaged."""
    try:
        data = json.loads(resources.files(__package__).joinpath("data", "personas.json").read_text("utf-8"))
    except (OSError, ValueError, ModuleNotFoundError):
        raise HostsError("The built-in hosts could not be loaded. Reinstall myradio to restore them.") from None
    entries = data.get("personas") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise HostsError("The built-in hosts file is empty or damaged. Reinstall myradio to restore it.")
    out: Dict[str, Persona] = {}
    for entry in entries:
        try:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise HostsError("every built-in host needs an id")
            persona, _ = validate_persona(entry, voices, event_tags=event_tags, builtin=True, lenient_tags=True)
        except HostsError as exc:
            label = entry.get("id", "?") if isinstance(entry, dict) else "?"
            raise HostsError(f"A built-in host is damaged ({str(label)[:30]}): {exc}") from None
        if persona.id in out:
            raise HostsError(f"The built-in hosts list {persona.id} twice.")
        out[persona.id] = persona
    return out
