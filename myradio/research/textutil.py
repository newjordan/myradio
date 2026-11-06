"""Pure, bounded text helpers for the research extension: cleaning, matching, URLs, dates.

Nothing here touches the network or the disk. Everything that comes out of a fetched document goes
through ``clean_text`` first, so invisible characters, control characters and lone surrogates never
reach the database, the screen or a speech engine.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from email.utils import parsedate_tz
from typing import Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

UTC = dt.timezone.utc

# Characters a person never sees but a program may act on: control characters, the default-ignorable
# characters (soft hyphen, combining grapheme joiner, Hangul and Khmer fillers, Mongolian variation selectors,
# zero-width and direction marks, invisible operators, the half-width Hangul filler), private-use and lone
# surrogate code points, and the whole "tag" block of U+E0000 to U+E0FFF (the Unicode tag characters and the
# variation-selector supplement, both used to smuggle hidden text). The emoji variation selectors U+FE00 to
# U+FE0F and the joiners U+200C and U+200D are kept because emoji and some scripts need them.
# Tab, newline and carriage return are handled as spaces.
_INVISIBLE = re.compile(
    "[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f"
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
    "\u200b\u200e\u200f"
    "\u202a-\u202e\u2060-\u206f"
    "\u3164\ufeff\uffa0\ufff0-\ufffc"
    "\ud800-\udfff"
    "\ue000-\uf8ff"
    "\U0001d173-\U0001d17a"
    "\U000e0000-\U000e0fff"
    "\U000f0000-\U0010ffff]")
_SPACE = re.compile(r"[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")
_NONWORD = re.compile(r"[\W_]+")
_POSSESSIVE = re.compile(r"(?<=\w)['\u2019]s\b")
_APOSTROPHE = re.compile(r"(?<=\w)['\u2019](?=\w)")

STOP_WORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further had has have having he her here hers him his
how i if in into is it its just me more most my no nor not now of off on once only or other our out over own same she
should so some such than that the their theirs them then there these they this those through to too under until up
very was we were what when where which while who whom why will with would you your yours new latest news update
updates today get gets got via per vs
""".split())

_KEEP_PLURAL = frozenset("""
news series species physics politics economics mathematics analytics logistics genetics electronics linguistics
lens gas bus this his its was has is us as yes plus focus status virus bonus campus corpus canvas atlas bias
chaos cosmos madness happiness business success process access address class glass grass mass pass less
""".split())

# Plurals whose stem the general rules cannot work out: Greek and Latin plurals ('analyses' for 'analysis'), a doubled
# 'z' ('quizzes' for 'quiz'), and the three-letter nouns that take 'es' ('gases', 'buses').
_IRREGULAR_PLURALS = {
    "analyses": "analysis", "paralyses": "paralysis", "diagnoses": "diagnosis", "prognoses": "prognosis",
    "hypotheses": "hypothesis", "theses": "thesis", "syntheses": "synthesis", "parentheses": "parenthesis",
    "crises": "crisis", "emphases": "emphasis", "oases": "oasis", "ellipses": "ellipsis", "synopses": "synopsis",
    "quizzes": "quiz", "whizzes": "whiz", "gases": "gas", "buses": "bus", "yeses": "yes",
}
# Nouns whose singular ends in "-oe", so that the plural "-oes" drops only the "s" ("shoes" -> "shoe", but "heroes" -> "hero").
_OE_SINGULARS = frozenset("shoe canoe oboe floe aloe sloe tiptoe mistletoe horseshoe snowshoe overshoe".split())


_CLEAN_SLICE = 65_536


def clean_text(value, limit: int = 0) -> str:
    """Printable, single-spaced text. Returns ``""`` for anything that is not a string."""
    if not isinstance(value, str):
        return ""
    if len(value) > _CLEAN_SLICE:
        text = _clean_long(value, limit)
    else:
        text = _SPACE.sub(" ", _INVISIBLE.sub("", value)).strip()
    if limit and len(text) > limit:
        text = cut_at_word(text, limit)
    return text


def _clean_long(value: str, limit: int) -> str:
    """``clean_text`` for long text, a slice at a time: one pass over 2 MB of invisible characters held the interpreter lock
    about 110 ms. Both patterns match single characters or runs of blanks, so each slice is cleaned on its own and a blank run
    cut by a slice edge is joined back into one space. With a ``limit`` it stops once it holds more than enough text to be cut
    exactly as the whole would be (more than ``limit + 1`` characters, ending in a letter, so trailing blanks cannot matter)."""
    parts, size = [], 0
    for start in range(0, len(value), _CLEAN_SLICE):
        piece = _SPACE.sub(" ", _INVISIBLE.sub("", value[start:start + _CLEAN_SLICE]))
        if not parts:
            piece = piece.lstrip(" ")
        elif piece.startswith(" ") and parts[-1].endswith(" "):
            piece = piece[1:]
        if piece:
            parts.append(piece)
            size += len(piece)
            if limit and size > limit + 1 and not piece.endswith(" "):
                break
    return "".join(parts).strip()


def cut_at_word(text: str, limit: int) -> str:
    """The longest prefix of at most ``limit`` characters that does not end inside a word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace():
        space = cut.rfind(" ")
        if space >= limit * 0.6:
            cut = cut[:space]
    return cut.rstrip(" ,;:-\u2013\u2014")


def fold(text: str) -> str:
    """Lower-case, accent-free, punctuation-free form used only for matching (never shown)."""
    text = _APOSTROPHE.sub("", _POSSESSIVE.sub("", text.casefold()))
    if not text.isascii():
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _NONWORD.sub(" ", text).strip()


def stem(token: str) -> str:
    """Plural-only stemming: 'planets' and 'planet' match, nothing cleverer.

    The one rule that matters is that a word and its plural give the *same* stem, whichever way each is written
    ('indie' and 'indies', 'hero' and 'heroes', 'house' and 'houses', 'analysis' and 'analyses', 'quiz' and 'quizzes').
    The stem itself is only a matching key and is never shown. Irregular plurals ('mice', 'knives') are not folded."""
    if len(token) <= 3 or token in _KEEP_PLURAL or not token.isascii():
        return token
    known = _IRREGULAR_PLURALS.get(token)
    if known:
        return known
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"                               # stories -> story
    if token.endswith("ie") and len(token) > 4:
        return token[:-2] + "y"                               # indie -> indy, the stem of "indies"
    if token.endswith("oes") and len(token) > 4 and token[:-1] not in _OE_SINGULARS:
        return token[:-2]                                     # heroes -> hero, but shoes -> shoe
    if token.endswith(("ses", "zes")) and len(token) <= 5:
        return token[:-1]                                     # cases -> case, sizes -> size, uses -> use
    if token.endswith("es") and (token[-3] in "sxz" or token.endswith(("ches", "shes"))):
        return token[:-2]                                     # boxes -> box, glasses -> glass, houses -> hous
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    if (len(token) > 4 and token.endswith(("se", "ze"))) or (len(token) > 3 and token.endswith("che")):
        return token[:-1]                                     # house -> hous, the stem of "houses"; cache -> cach
    return token


def core_tokens(folded: str) -> list:
    """Meaningful words of an already folded string: stop words removed, plurals folded."""
    out, seen = [], set()
    for token in folded.split():
        if token in STOP_WORDS or (len(token) < 2 and not token.isdigit()):
            continue
        token = stem(token)
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def is_cjk(text: str) -> bool:
    """True for scripts written without spaces, where whole-word matching does not apply."""
    return any("\u2e80" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" or "\u3040" <= ch <= "\u30ff" for ch in text)


def contains_phrase(folded_text: str, folded_phrase: str) -> bool:
    """Whole-word phrase match on folded strings."""
    if not folded_phrase:
        return False
    if is_cjk(folded_phrase):
        return folded_phrase in folded_text
    return f" {folded_phrase} " in f" {folded_text} "


def digest(*parts: str, size: int = 32) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:size]


# ---- speech safety -----------------------------------------------------------------------------

_SPEECH_TOKEN = re.compile(r"<\|[^>\n]{0,100}>")
_SPEECH_ANGLE = re.compile(r"<[A-Za-z/!?][^<>\n]{0,100}>")
_SPEECH_EVENT = re.compile(r"\[[A-Za-z][A-Za-z :_-]{0,30}\]")
_SPEECH_RESIDUE = str.maketrans({"<": " ", ">": " ", "[": "(", "]": ")", "{": "(", "}": ")", "|": ",", "`": "'", "\\": "/"})


def neutralize_speech(text: str) -> str:
    """Remove speaker and event markup (``<|speaker:N|>``, ``[laughs]``, ``<tags>``) from outside text.

    The result contains none of the characters ``< > [ ] { } |``, so it cannot be read as control
    syntax by Drama 3 or by a chat model, and none of the invisible characters ``clean_text`` removes (so this is safe
    to call on text that was not cleaned first). The original wording is otherwise left alone.
    """
    text = _SPEECH_TOKEN.sub(" ", _INVISIBLE.sub("", text))
    text = _SPEECH_ANGLE.sub(" ", text)
    text = _SPEECH_EVENT.sub(" ", text)
    return _SPACE.sub(" ", text.translate(_SPEECH_RESIDUE)).strip()


# ---- URLs --------------------------------------------------------------------------------------

_TRACKING = frozenset("""
fbclid gclid dclid gbraid wbraid gclsrc msclkid yclid igshid mc_cid mc_eid _hsenc _hsmi mkt_tok ref_src ref_url
s_cid ncid ocid cmpid spm wt.mc_id at_medium at_campaign rb_clickid vero_id oly_enc_id oly_anon_id soc_src soc_trk _ga _gl
""".split())
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f\\<>\"`{}|^]")
MAX_URL = 2000


def _split(url: str):
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  (raises ValueError for a bad port)
    except ValueError:
        return None
    return parts


def _query_without_tracking(query: str) -> list:
    pairs = parse_qsl(query, keep_blank_values=True)
    return [(k, v) for k, v in pairs if not (k.lower().startswith("utm_") or k.lower() in _TRACKING)]


def clean_url(url, limit: int = MAX_URL) -> Optional[str]:
    """A tidy http(s) address for display, or ``None``. Tracking parameters and fragments are dropped.

    Addresses with a user name or password, whitespace, control characters or odd delimiters are refused.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > limit or _BAD_URL_CHARS.search(url):
        return None
    parts = _split(url)
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    query = urlencode(_query_without_tracking(parts.query))
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", query, ""))


def canonical_key(url) -> str:
    """Identity of a page for de-duplication: ignores http/https, 'www.', default ports, tracking
    parameters, parameter order, fragments and a trailing slash. Returns ``""`` for unusable input."""
    cleaned = clean_url(url) if isinstance(url, str) else None
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    if port not in (None, 80, 443):
        host = f"{host}:{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = urlencode(sorted(_query_without_tracking(parts.query)))
    return f"{host}{path}" + (f"?{query}" if query else "")


def url_host(url) -> str:
    parts = _split(url) if isinstance(url, str) else None
    return (parts.hostname or "").lower().rstrip(".") if parts else ""


# ---- dates -------------------------------------------------------------------------------------

_ISO = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?)?\s*$",
    re.IGNORECASE)
_MONTHS = {name: number for number, names in enumerate(
    ("january jan", "february feb", "march mar", "april apr", "may", "june jun", "july jul", "august aug", "september sep sept",
     "october oct", "november nov", "december dec"), 1) for name in names.split()}      # English names written out, so the computer's locale never matters
_WORDY = re.compile(r"^\s*(?:[A-Za-z]{3,9},?\s+)?(?:(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})|([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?),?\s+(\d{4})\s*$")
MIN_YEAR, MAX_YEAR = 1990, 2100


def _stamp(year, month, day, hour=0, minute=0, second=0, offset=0):
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        moment = dt.datetime(year, month, day, hour, minute, min(second, 59), tzinfo=UTC)
    except ValueError:
        return None
    return moment.timestamp() - offset


def parse_date(value) -> Tuple[Optional[float], Optional[str]]:
    """Parse a feed or page date into ``(epoch_seconds_utc, precision)``.

    Precision is ``"time"`` or ``"day"`` (a date with no time of day, taken as the start of that day UTC so
    that staleness is judged conservatively). Returns ``(None, None)`` for anything unreadable. The machine's
    own time zone is never consulted.
    """
    if not isinstance(value, str):
        return None, None
    value = clean_text(value, 120)
    if not value:
        return None, None
    match = _ISO.match(value)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if match.group(4) is None:
            ts = _stamp(year, month, day)
            return (ts, "day") if ts is not None else (None, None)
        zone = match.group(7)
        offset = 0
        if zone and zone.upper() != "Z":
            digits = zone[1:].replace(":", "")
            offset = (int(digits[:2]) * 3600 + int(digits[2:4] or 0) * 60) * (1 if zone[0] == "+" else -1)
            if abs(offset) > 18 * 3600:
                return None, None
        hour, minute, second = int(match.group(4)), int(match.group(5)), int(match.group(6) or 0)
        if hour > 23 or minute > 59:
            return None, None
        ts = _stamp(year, month, day, hour, minute, second, offset)
        return (ts, "time") if ts is not None else (None, None)
    try:
        parsed = parsedate_tz(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed:
        year, month, day, hour, minute, second = parsed[:6]
        offset = parsed[9] or 0
        if abs(offset) <= 18 * 3600 and hour <= 23 and minute <= 59:
            ts = _stamp(year, month, day, hour, minute, second, offset)
            if ts is not None:
                return ts, "time"
    match = _WORDY.match(value)
    if match:
        if match.group(1):
            day, name = int(match.group(1)), match.group(2)
        else:
            day, name = int(match.group(4)), match.group(3)
        month = _MONTHS.get(name.lower())
        if month:
            ts = _stamp(int(match.group(5)), month, day)
            if ts is not None:
                return ts, "day"
    return None, None


def iso_utc(ts) -> Optional[str]:
    if ts is None:
        return None
    try:
        return dt.datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


# ---- extracts ----------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?\u2026])[\"\u201d\u2019)\]]*(?=\s+[\"\u201c\u2018(\[]?[A-Z0-9])")


def build_extract(text: str, max_chars: int) -> Tuple[str, bool]:
    """A verbatim prefix of ``text``: whole sentences when possible, otherwise cut at a word boundary.

    Returns ``(extract, truncated)``. The extract is always a contiguous substring of ``text``.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    best = 0
    for match in _SENTENCE_END.finditer(text):
        if match.end() > max_chars:
            break
        best = match.end()
    if best >= min(80, max_chars // 3):
        return text[:best].strip(), True
    cut = cut_at_word(text, max_chars)
    return cut, True


def word_count(text: str) -> int:
    return len(text.split())
