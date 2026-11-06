"""How long a script takes to say, estimated from an explicit words-per-second rate.

This is an estimate, not a measurement of any speech model. A host's pace sets the rate
(``PACES`` in personas.py: slow, steady, brisk). Pauses are added for punctuation and for
effect tags. The estimate is what the composer steers towards when it controls length, and it
is reported to the caller as ``est_seconds`` so a scheduler can plan around it.
"""
from __future__ import annotations

import functools
import re
from .textsafe import strip_markup

SENTENCE_PAUSE = 0.30   # seconds added after each sentence
CLAUSE_PAUSE = 0.12     # after each comma, semicolon or colon
TAG_PAUSE = 0.40        # for each effect tag such as [laughs]
_WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*")
_SENTENCE_END = re.compile(r"[.!?…]+")
_CLAUSE = re.compile(r"[,;:]")
_TAG = re.compile(r"\[[^\[\]]{1,40}\]")


def count_words(text: str) -> int:
    """Words as spoken: a number written with digits counts for the words it is read as."""
    plain = strip_markup(text)
    return len(_WORD.findall(plain)) + sum(len(digits) // 3 for digits in re.findall(r"\d+", plain))


@functools.lru_cache(maxsize=4096)
def _estimate(text: str, wps: float) -> float:
    plain = strip_markup(text)
    seconds = count_words(plain) / wps
    seconds += len(_SENTENCE_END.findall(plain)) * SENTENCE_PAUSE
    seconds += len(_CLAUSE.findall(plain)) * CLAUSE_PAUSE
    seconds += len(_TAG.findall(text)) * TAG_PAUSE
    return seconds


def estimate_raw(text: str, wps: float) -> float:
    """The estimate before rounding. It is additive: the estimate of two lines joined by a space is the
    sum of their estimates, which lets the composer choose lines by arithmetic."""
    if not isinstance(text, str) or not text.strip() or wps <= 0:
        return 0.0
    return _estimate(text, float(wps))


def estimate_seconds(text: str, wps: float) -> float:
    """Seconds needed to say ``text`` (a script, with or without its speaker marker and tags)."""
    return round(estimate_raw(text, wps), 1)


def words_for(seconds: float, wps: float) -> int:
    """About how many words fill ``seconds`` at ``wps`` once pauses are allowed for."""
    return max(1, int(round(seconds * wps * 0.9)))
