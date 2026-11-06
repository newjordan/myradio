"""Text helpers for Drama 3 speech: tag handling, sentence splitting and prefixes.

Drama 3 reads two kinds of markers inside its input: speaker switches such as ``<|speaker:0|>`` and
event tags such as ``[laughs]``. Text that comes from outside the station operator (track titles, feed
items, advertiser copy) must never be able to carry those markers into a speech job, so every speech
payload goes through :func:`neutralize_speech_tags` unless the producer says the text is the station's
own script (``allow_tags``), in which case only well-formed markers survive (:func:`restrict_speech_tags`).

Producers (hosts, ads, research) may import :func:`neutralize_speech_tags` to defang outside text
before they build a script.
"""
from __future__ import annotations

import re
import unicodedata

SPEAKER_RE = re.compile(r"<\|speaker:(\d{1,2})\|>")
_ANY_MARKER_RE = re.compile(r"<\|[^<>\n]{0,80}\|>")
_BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,40})\]")
_EVENT_TAG_RE = re.compile(r"\[[a-z][a-z \-]{1,24}\]")
_BIDI = dict.fromkeys(map(ord, "​‎‏‪‫‬‭‮⁦⁧⁨⁩﻿"), None)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…。！？])\s+|\n+")
_CLAUSE_END_RE = re.compile(r"(?<=[,;:、，；：])\s*")

MAX_SPEAKER = 31


def clean_line(value: str) -> str:
    """One line of plain text: no control characters, no direction overrides, single spaces."""
    value = unicodedata.normalize("NFC", value).translate(_BIDI)
    value = _CONTROL_RE.sub(" ", value)
    return " ".join(value.split())


def clean_block(value: str) -> str:
    """Multi-line text (lyrics): control characters removed, line endings normalised, lines trimmed."""
    value = unicodedata.normalize("NFC", value).translate(_BIDI).replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_RE.sub("", value.replace("\t", " "))
    return "\n".join(line.rstrip() for line in value.split("\n")).strip()


def neutralize_speech_tags(text: str) -> str:
    """Make outside text safe to speak: remove every ``<|...|>`` marker and turn ``[tag]`` into ``(tag)``.

    The result contains no speaker switch and no event tag, so it is read as ordinary words.
    """
    text = clean_line(text)
    text = _ANY_MARKER_RE.sub(" ", text)
    text = text.replace("<|", " ").replace("|>", " ")
    text = _BRACKET_RE.sub(lambda m: "(" + m.group(1) + ")", text)
    text = text.replace("[", "(").replace("]", ")")
    return " ".join(text.split())


def restrict_speech_tags(text: str) -> str:
    """For the station's own scripts: keep well-formed ``<|speaker:N|>`` and ``[event]`` tags, defang the rest."""
    text = clean_line(text)

    def keep_speaker(match: "re.Match") -> str:
        return match.group(0) if int(match.group(1)) <= MAX_SPEAKER else " "

    pieces = []
    last = 0
    for match in SPEAKER_RE.finditer(text):
        pieces.append(_defang_plain(text[last:match.start()]))
        pieces.append(keep_speaker(match))
        last = match.end()
    pieces.append(_defang_plain(text[last:]))
    return " ".join("".join(pieces).split())


def _plain_brackets(piece: str) -> str:
    piece = _BRACKET_RE.sub(lambda m: "(" + m.group(1) + ")", piece)
    return piece.replace("[", "(").replace("]", ")")


def _defang_plain(chunk: str) -> str:
    """Between speaker markers: keep well-formed ``[event]`` tags, turn every other bracket into a parenthesis."""
    chunk = _ANY_MARKER_RE.sub(" ", chunk).replace("<|", " ").replace("|>", " ")
    out, last = [], 0
    for match in _EVENT_TAG_RE.finditer(chunk):
        out.append(_plain_brackets(chunk[last:match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(_plain_brackets(chunk[last:]))
    return "".join(out)


def speaker_prefix(speaker: int) -> str:
    return f"<|speaker:{int(speaker)}|> "


def _hard_split(piece: str, limit: int) -> list:
    out = []
    while len(piece) > limit:
        cut = piece.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        out.append(piece[:cut].strip())
        piece = piece[cut:].strip()
    if piece:
        out.append(piece)
    return out


def _pieces(text: str, limit: int) -> list:
    """Sentences, with any sentence longer than ``limit`` cut at clauses and then at spaces."""
    out = []
    for sentence in _SENTENCE_END_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= limit:
            out.append(sentence)
            continue
        current = ""
        for clause in _CLAUSE_END_RE.split(sentence):
            clause = clause.strip()
            if not clause:
                continue
            if len(clause) > limit:
                if current:
                    out.append(current)
                    current = ""
                out.extend(_hard_split(clause, limit))
            elif current and len(current) + 1 + len(clause) > limit:
                out.append(current)
                current = clause
            else:
                current = f"{current} {clause}".strip()
        if current:
            out.append(current)
    return out


def split_takes(text: str, speaker: int, max_chars: int) -> list:
    """Split a script into takes of at most ``max_chars`` (before the speaker prefix), at sentence ends.

    Every take starts with a speaker marker. When the script switches speaker itself, the speaker in
    force at the start of each take is carried forward so a take never begins with the wrong voice.
    """
    max_chars = max(40, int(max_chars))
    current_speaker = int(speaker)
    takes = []
    buffer = ""
    buffer_speaker = current_speaker

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            body = buffer.strip()
            takes.append(body if SPEAKER_RE.match(body) else speaker_prefix(buffer_speaker) + body)
        buffer = ""

    for piece in _pieces(text, max_chars):
        found = SPEAKER_RE.findall(piece)
        if buffer and len(buffer) + 1 + len(piece) > max_chars:
            flush()
        if not buffer:
            buffer_speaker = current_speaker
        buffer = f"{buffer} {piece}".strip()
        if found:
            current_speaker = int(found[-1])
    flush()
    return takes
