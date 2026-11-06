"""Text hygiene for everything that can reach a speech job.

The speech model reads ``<|speaker:N|>`` markers and ``[event]`` tags literally. Only the
composer may place them. Everything else that ends up in a script (track titles, feed text,
advertiser copy, operator notes, even a saved host's own wording) first goes through
:func:`neutralize`, which removes markup, control characters and look-alike characters, and
which is idempotent: running it on its own output changes nothing.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, NamedTuple, Optional, Sequence

# The sound effects a host may use. Kept in one place so the generation lane can adjust it; an
# operator can replace it in config.json ("event_tags"). These are the names the product brief
# lists; they have not been checked against a real Drama 3 build here.
EVENT_TAGS = ("laughs", "whispers", "excited")
MAX_SPEAKER = 15
# The longest script the speech step accepts (the generation lane refuses text over 3,000 characters, the
# speaker marker included), so a script this package returns is never too long for it.
SPEECH_MAX_CHARS = 3000
MAX_SCRIPT_CHARS = SPEECH_MAX_CHARS

_SPEAKER_TOKEN = re.compile(r"<\|[^|<>]{0,64}\|>")
_ANGLE_TAG = re.compile(r"<[^<>]{0,48}>")
_SQUARE_TAG = re.compile(r"\[[^\[\]]{0,80}\]")
_BRACKETS = re.compile(r"[<>\[\]{}|\\]")
_STRAY = re.compile(r"[<>\[\]{}|\\`*_~^]")
_PARENS = re.compile(r"[()]")
_SPACES = re.compile(r"\s+")
_RUNS = re.compile(r"([^\w\s])\1{3,}")
_REMOVED_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn"))
_SPACE_CATEGORIES = frozenset(("Zs", "Zl", "Zp"))
# Letters and symbols that draw nothing, so a title made of them would look empty but not be.
_BLANKS = frozenset("\u115f\u1160\u3164\uffa0\u2800")
_SELECTORS = frozenset(range(0xFE00, 0xFE10)) | frozenset(range(0xE0100, 0xE01F0))
_EVENT = re.compile(r"\[([^\[\]]*)\]")
# Quoted text (brief items) keeps the words inside square brackets; only these stage directions, which a
# transcript puts in brackets and which are not words anyone said, are dropped with their brackets.
_CUE_WORDS = frozenset("""
laughs laughing laughter laughed whispers whispering whispered excited sighs sighing sigh coughs coughing cough gasps gasp
sobs sobbing cries crying groans groaning chuckles chuckling giggles giggling applause clapping cheers cheering booing
music singing humming pause silence beep sfx noise static crosstalk inaudible unintelligible indistinct sound
""".split())
_CUE_SPAN = re.compile(r"\[\s*([A-Za-z][A-Za-z ]{0,24}?)\s*\]")
_BRACKET_SPAN = re.compile(r"\[([^\[\]]{0,80})\]")
_QUOTE_STRAY = re.compile(r"[{}|\\`*_~^]")
_DANGLING = re.compile(r"[<\[][^<>\[\]]{0,100}\Z")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?\u2026])")


class Cleaned(NamedTuple):
    text: str
    flags: frozenset


def speaker_token(speaker: int) -> str:
    return f"<|speaker:{int(speaker)}|>"


def _drop_controls(value: str) -> "tuple[str, bool]":
    out: List[str] = []
    removed = False
    for ch in value:
        category = unicodedata.category(ch)
        if ch in "\t\n\r\f\v" or category in _SPACE_CATEGORIES or ch in _BLANKS:
            out.append(" ")
        elif category in _REMOVED_CATEGORIES or ord(ch) in _SELECTORS:
            removed = True
        else:
            out.append(ch)
    return "".join(out), removed


def _cut(text: str, limit: int) -> "tuple[str, bool]":
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    space = head.rfind(" ")
    if space >= int(limit * 0.6):
        head = head[:space]
    return head.rstrip(" ,;:-–—").strip(), True


def has_content(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def neutralize(value: object, limit: int = 200) -> Cleaned:
    """Return ``value`` as plain, bounded, single-line text that carries no markup.

    Flags: ``not_text`` (value was not a string), ``markup`` (tags or bracket characters were
    removed), ``control`` (control or invisible characters were removed), ``shortened`` (cut to
    ``limit`` characters at a word boundary), ``empty`` (nothing speakable was left).
    """
    flags = set()
    if not isinstance(value, str):
        return Cleaned("", frozenset({"not_text", "empty"}))
    bounded = value[: limit * 4 + 64]  # bound the work before any per-character processing
    if len(bounded) < len(value):
        flags.add("shortened")
    text = unicodedata.normalize("NFKC", bounded)[: limit * 3 + 64]
    text, removed = _drop_controls(text)
    if removed:
        flags.add("control")
    for _ in range(8):  # removing one tag can reveal another, so repeat until stable
        step = _SPEAKER_TOKEN.sub(" ", text)
        step = _ANGLE_TAG.sub(" ", step)
        step = _SQUARE_TAG.sub(" ", step)
        if step == text:
            break
        flags.add("markup")
        text = step
    if _BRACKETS.search(text):
        flags.add("markup")
    text = _STRAY.sub(" ", text)
    text = _PARENS.sub(" ", text)
    text = _RUNS.sub(lambda m: m.group(1) * 3, text)
    text = _SPACES.sub(" ", text).strip()
    text, cut = _cut(text, limit)
    if cut:
        flags.add("shortened")
    if not has_content(text):
        return Cleaned("", frozenset(flags | {"empty"}))
    return Cleaned(text, frozenset(flags))


_HTML_NAMES = ("a|abbr|article|aside|audio|b|big|blockquote|br|button|center|cite|code|dd|del|div|dl|dt|em|figure|font|footer|"
               "form|h[1-6]|header|hr|i|iframe|img|input|ins|li|mark|nav|ol|p|pre|q|s|script|section|small|source|span|strike|"
               "strong|style|sub|sup|svg|table|tbody|td|th|thead|tr|tt|u|ul|video")
# A tag has a known name and, if anything, attributes written name=value, so "x<b and b>c" (a comparison) is not one.
_HTML_TAG = re.compile(r"</?(?:" + _HTML_NAMES + r")(?![A-Za-z0-9-])"
                       r"(?:\s+[\w:-]{1,30}\s*=\s*(?:\"[^\"<>]{0,200}\"|'[^'<>]{0,200}'|[^\s\"'<>=]{1,100}))*\s*/?>", re.IGNORECASE)


def neutralize_quote(value: object, limit: int = 1200) -> Cleaned:
    """Like :func:`neutralize`, for text that is read back as somebody else's words (a research brief item).

    The words stay exactly as written. What is removed: speaker markers (``<|...|>``), HTML-style tags, stage
    directions in square brackets such as ``[laughs]``, control characters and invisible characters. Square
    brackets, braces, bars and backslashes around other words are dropped and the words kept ("would [not] help"
    reads "would not help"); parentheses stay, because they are ordinary punctuation. A ``<`` or ``>`` that is not
    part of a tag ("5 < 7") cannot be spoken without changing the meaning, so the text is flagged ``symbols`` and
    the caller decides (a brief item is left out).

    Flags: ``not_text``, ``markup`` (a marker, tag, stage direction or stray code character was removed),
    ``brackets`` (square brackets were dropped, their words kept), ``symbols`` (``<`` or ``>`` that cannot be read),
    ``control``, ``shortened`` and ``empty``.
    """
    flags = set()
    if not isinstance(value, str):
        return Cleaned("", frozenset({"not_text", "empty"}))
    bounded = value[: limit * 4 + 64]   # bound the work before any per-character processing
    cut_early = len(bounded) < len(value)
    text = unicodedata.normalize("NFKC", bounded)
    if len(text) > limit * 3 + 64:
        text, cut_early = text[: limit * 3 + 64], True
    if cut_early:
        flags.add("shortened")
        text = _DANGLING.sub(" ", text)     # a marker or tag the cut sliced in two is not part of the text
    text, removed = _drop_controls(text)
    if removed:
        flags.add("control")

    def cue(match: "re.Match[str]") -> str:
        return " " if match.group(1).strip().lower() in _CUE_WORDS else match.group(0)

    for _ in range(8):   # removing one marker can reveal another, so repeat until stable
        step = _SPEAKER_TOKEN.sub(" ", text)
        step = _HTML_TAG.sub(" ", step)
        step = _CUE_SPAN.sub(cue, step)
        if step == text:
            break
        flags.add("markup")
        text = step
    for _ in range(8):   # other square brackets: the words inside are kept
        step = _BRACKET_SPAN.sub(lambda m: " " + m.group(1) + " ", text)
        if step == text:
            break
        flags.add("brackets")
        text = step
    if "[" in text or "]" in text:
        flags.add("brackets")
        text = text.replace("[", " ").replace("]", " ")
    if re.search(r"[{}|\\]", text):
        flags.add("markup")
    text = _SPACES.sub(" ", _QUOTE_STRAY.sub(" ", text)).strip()
    if flags & {"markup", "brackets"}:
        text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)      # "seen [laughs]." must not become "seen ."
    text = _RUNS.sub(lambda m: m.group(1) * 3, text)
    text, cut = _cut(text, limit)
    if cut:
        flags.add("shortened")
    if "<" in text or ">" in text:      # judged on what is kept: a symbol in the part that was cut off does not matter
        flags.add("symbols")
        text = _SPACES.sub(" ", text.replace("<", " ").replace(">", " ")).strip()
    if not has_content(text):
        return Cleaned("", frozenset(flags | {"empty"}))
    return Cleaned(text, frozenset(flags))


def strip_markup(text: str) -> str:
    """Remove speaker markers and event tags, leaving only the words a listener would hear."""
    if not isinstance(text, str):
        return ""
    text = _SPEAKER_TOKEN.sub(" ", text)
    text = _EVENT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def audit_speech_text(text: object, speaker: int, allowed_tags: Iterable[str]) -> List[str]:
    """Problems that would make ``text`` unsafe to send to the speech model (empty list = safe).

    The text must open with exactly one marker for ``speaker``, may contain only the allowed
    ``[event]`` tags, and may not contain any other markup or control character.
    """
    problems: List[str] = []
    if not isinstance(text, str) or not text.strip():
        return ["The script is empty."]
    if len(text) > MAX_SCRIPT_CHARS:
        problems.append("The script is too long.")
    marker = speaker_token(speaker)
    if not text.startswith(marker):
        problems.append("The script does not start with the host's speaker marker.")
        return problems
    body = text[len(marker):]
    if "<" in body or ">" in body or "|" in body or "{" in body or "}" in body or "\\" in body:
        problems.append("The script contains voice codes that are not allowed.")
    allowed = set(allowed_tags)
    for tag in _EVENT.findall(body):
        if tag not in allowed:
            problems.append("The script uses a sound effect this host may not use.")
            break
    if "[" in _EVENT.sub("", body) or "]" in _EVENT.sub("", body):
        problems.append("The script contains stray brackets.")
    if any(unicodedata.category(ch) in _REMOVED_CATEGORIES for ch in body):
        problems.append("The script contains hidden characters.")
    if not has_content(strip_markup(body)):
        problems.append("The script has no words.")
    return problems


def clean_tags(tags: Optional[Sequence[object]], vocabulary: Iterable[str]) -> "tuple[tuple, list]":
    """Keep only known effect names. Returns (kept tags in order, rejected names).

    A single name is read as a list of one. Anything else that is not a list or tuple (a number, a true/false
    value, a mapping) is rejected as a whole, so a wrong type is reported like a wrong name."""
    known = set(vocabulary)
    kept: List[str] = []
    rejected: List[str] = []
    if tags is None:
        tags = ()
    elif isinstance(tags, str):
        tags = (tags,)
    elif not isinstance(tags, (list, tuple)):
        return (), [str(tags)[:30]]
    for tag in tags:
        name = tag.strip().lower() if isinstance(tag, str) else None
        if name in known:
            if name not in kept:
                kept.append(name)
        else:
            rejected.append(str(tag)[:30])
    return tuple(kept), rejected
