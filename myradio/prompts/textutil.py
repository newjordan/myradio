"""Small text helpers shared by the catalog, the rules and the saved-style checks.

They exist for two reasons. A message shown to a person must never grow with what that person
sent, so anything quoted back is cut short first. And text that reaches a stored record or a
fingerprint must be text that can really be written down: no control characters that could add
a line to a prompt, no text-direction overrides, and no half characters left over from cutting an
emoji in two.

This file is plain ASCII on purpose: the special characters it looks for are written as code
points so that none of them can hide in the source itself.
"""

from __future__ import annotations

import unicodedata

# Characters that change how text is laid out or read, apart from the C0 and C1 control
# characters (category Cc), which are caught by category: line and paragraph separators and the
# text-direction controls (marks, embeddings, overrides and isolates).
_LAYOUT_CONTROLS = frozenset(map(chr, (
    0x2028, 0x2029,
    0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
)))
_UNPRINTABLE = chr(0xFFFD)  # shown in place of a character that cannot be printed safely

BROKEN_TEXT_MESSAGE = (
    "Some of the text contains a broken character, for example half of an emoji that was cut off "
    "by a length limit. Retype that part and try again."
)


def shown(value: object, limit: int = 60) -> str:
    """Text a person sent, made printable and shortened so a message can never grow with the input."""
    text = str(value)
    if len(text) > limit:
        text = text[: max(1, limit - 3)] + "..."
    return "".join(ch if ch.isprintable() else _UNPRINTABLE for ch in text)


def shown_names(names: object, limit: int = 5) -> str:
    """A short, bounded list of unrecognised names for an error message."""
    items = sorted(str(n) for n in names)
    text = ", ".join(shown(n, 30) for n in items[:limit])
    return text + (f" and {len(items) - limit} more" if len(items) > limit else "")


def has_odd_character(text: str) -> bool:
    """True for a control character, a line or paragraph separator or a text-direction override.

    Ordinary spaces are fine here; whether the spacing is tidy is a separate check.
    """
    return any(unicodedata.category(ch) == "Cc" or ch in _LAYOUT_CONTROLS for ch in text)


def is_tidy_line(text: str) -> bool:
    """One line, single spaces, nothing at either end and no odd characters (the catalog's own habit)."""
    return text == " ".join(text.split()) and not has_odd_character(text)


def encodable(text: str) -> bool:
    """False when ``text`` holds a lone surrogate or anything else that cannot be written as UTF-8."""
    if text.isascii():
        return True
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def broken_text(value: object, *, budget: int = 20000) -> bool:
    """True when any text (or key) inside a JSON-like value cannot be written as UTF-8.

    Walks without recursion and stops after ``budget`` values; a value that large is refused by the
    size checks elsewhere, so reporting False here is safe.
    """
    stack = [value]
    seen = 0
    while stack:
        item = stack.pop()
        seen += 1
        if seen > budget:
            return False
        if isinstance(item, str):
            if not encodable(item):
                return True
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and not encodable(key):
                    return True
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False
