"""The Brief, written down in code.

``docs/lanes/research.md`` describes the same document in prose. ``check_brief`` is the executable version: the
research tests run every brief they make through it, and the hosts lane and the director can use it in their own
tests to confirm they are handed what they expect. It returns a list of plain-language problems (empty when valid).
"""
from __future__ import annotations

import datetime as dt
import re

OUTCOMES = ("ready", "nothing_new", "error")
REVIEWS = ("pending", "approved", "rejected", "auto")
KINDS = ("reported", "commentary", "announcement", "unverified")
DATE_PRECISIONS = ("time", "day")
DATE_BASES = ("published", "updated", "last_modified")
SOURCE_STATUSES = ("ok", "unchanged", "error", "skipped")
SOURCE_FORMATS = ("", "rss", "atom", "jsonfeed", "rdf", "page", "text")
TRIGGERS = ("scheduled", "manual")
SKIP_REASONS = ("undated", "future", "too_old", "already_covered", "skipped_source", "skipped_topic", "not_relevant", "tone",
                "similar_to_aired", "duplicate", "source_limit", "no_room")
MAX_CATEGORIES = 5

BRIEF_KEYS = frozenset("""
id researcher_id researcher_name outcome message review trigger created_ts created_at reviewed_ts reviewed_at aired_ts aired_at
airable freshness_hours segment_seconds target_words anchor fresh_until_ts fresh_until_at items sources summary warnings
""".split())
ITEM_KEYS = frozenset("""
id rank headline headline_speech extract extract_speech extract_truncated source_name source_name_speech source_url url
published_ts published_at date_precision date_basis fetched_ts fetched_at kind kind_reason score matched why tone categories
""".split())
SOURCE_KEYS = frozenset("url name status message entries format checked_at".split())
SUMMARY_KEYS = frozenset("considered kept skipped explanation".split())
_CONTROL = set("<>[]{}|")
_ID = re.compile(r"[0-9a-f]{32}")
_ITEM_ID = re.compile(r"[0-9a-f]{16}")
_ANCHOR = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_OFFSET = re.compile(r".*[+-]\d\d:\d\d")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _same_instant(ts, text) -> bool:
    if not _number(ts) or not isinstance(text, str) or not _OFFSET.fullmatch(text):
        return False
    try:
        return abs(dt.datetime.fromisoformat(text).timestamp() - ts) < 1.0
    except ValueError:
        return False


def _check_item(item: dict, where: str, problems: list) -> None:
    if not isinstance(item, dict) or set(item) != ITEM_KEYS:
        problems.append(f"{where}: has the wrong set of fields")
        return
    for key in ("id", "headline", "headline_speech", "extract", "extract_speech", "source_name", "source_name_speech", "source_url", "kind_reason"):
        if not isinstance(item[key], str):
            problems.append(f"{where}.{key}: must be text")
    if isinstance(item["id"], str) and not _ITEM_ID.fullmatch(item["id"]):
        problems.append(f"{where}.id: must be 16 lowercase hex characters")
    if isinstance(item["source_url"], str) and not item["source_url"].startswith(("http://", "https://")):
        problems.append(f"{where}.source_url: must be an http(s) address")
    if not item["headline"]:
        problems.append(f"{where}.headline: must not be empty")
    if item["kind"] not in KINDS:
        problems.append(f"{where}.kind: must be one of {', '.join(KINDS)}")
    for key in ("headline_speech", "extract_speech", "source_name_speech"):
        if isinstance(item[key], str) and _CONTROL & set(item[key]):
            problems.append(f"{where}.{key}: contains speech control characters")
    if not isinstance(item["extract_truncated"], bool):
        problems.append(f"{where}.extract_truncated: must be true or false")
    if item["url"] is not None and not (isinstance(item["url"], str) and item["url"].startswith(("http://", "https://"))):
        problems.append(f"{where}.url: must be an http(s) address or null")
    if not _same_instant(item["published_ts"], item["published_at"]):
        problems.append(f"{where}.published_at: must be an ISO-8601 time with an offset matching published_ts")
    if not _same_instant(item["fetched_ts"], item["fetched_at"]):
        problems.append(f"{where}.fetched_at: must be an ISO-8601 time with an offset matching fetched_ts")
    if item["date_precision"] not in DATE_PRECISIONS:
        problems.append(f"{where}.date_precision: must be time or day")
    if item["date_basis"] not in DATE_BASES:
        problems.append(f"{where}.date_basis: must be one of {', '.join(DATE_BASES)}")
    if not _number(item["score"]):
        problems.append(f"{where}.score: must be a number")
    if not (isinstance(item["matched"], list) and all(isinstance(m, str) for m in item["matched"])):
        problems.append(f"{where}.matched: must be a list of text")
    if not (isinstance(item["why"], list) and item["why"] and all(isinstance(w, str) for w in item["why"])):
        problems.append(f"{where}.why: must be a non-empty list of text")
    tone = item["tone"]
    if not (isinstance(tone, dict) and set(tone) == {"penalty", "reasons"} and _number(tone["penalty"]) and isinstance(tone["reasons"], list)):
        problems.append(f"{where}.tone: must have a penalty and a list of reasons")
    if not (isinstance(item["categories"], list) and len(item["categories"]) <= MAX_CATEGORIES and all(isinstance(c, str) for c in item["categories"])):
        problems.append(f"{where}.categories: must be a list of at most {MAX_CATEGORIES} texts")


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _skipped_ok(skipped) -> bool:
    return isinstance(skipped, dict) and all(key in SKIP_REASONS and isinstance(value, int) and not isinstance(value, bool) and value > 0
                                             for key, value in skipped.items())


def _source_ok(entry) -> bool:
    return (isinstance(entry, dict) and set(entry) == SOURCE_KEYS and entry["status"] in SOURCE_STATUSES and entry["format"] in SOURCE_FORMATS
            and _count(entry["entries"]) and all(isinstance(entry[key], str) for key in ("url", "name", "message", "checked_at")))


def check_brief(brief) -> list:
    """Problems with a brief, in plain language. An empty list means it matches the documented schema."""
    problems: list = []
    if not isinstance(brief, dict):
        return ["a brief must be an object"]
    if set(brief) != BRIEF_KEYS:
        missing, extra = sorted(BRIEF_KEYS - set(brief)), sorted(set(brief) - BRIEF_KEYS)
        return [f"wrong set of fields (missing: {missing}, unexpected: {extra})"]
    for key in ("id", "researcher_id"):
        if not (isinstance(brief[key], str) and _ID.fullmatch(brief[key])):
            problems.append(f"{key}: must be 32 lowercase hex characters")
    if brief["outcome"] not in OUTCOMES:
        problems.append(f"outcome: must be one of {', '.join(OUTCOMES)}")
    if brief["trigger"] not in TRIGGERS:
        problems.append(f"trigger: must be one of {', '.join(TRIGGERS)}")
    if not (isinstance(brief["message"], str) and brief["message"]):
        problems.append("message: must be a sentence")
    if not _same_instant(brief["created_ts"], brief["created_at"]):
        problems.append("created_at: must match created_ts")
    for stem in ("reviewed", "aired"):
        ts, text = brief[f"{stem}_ts"], brief[f"{stem}_at"]
        if (ts is None) != (text is None) or (ts is not None and not _same_instant(ts, text)):
            problems.append(f"{stem}_at: must be null together with {stem}_ts, otherwise match it")
    if not isinstance(brief["airable"], bool):
        problems.append("airable: must be true or false")
    for key in ("freshness_hours", "segment_seconds", "target_words"):
        if not (isinstance(brief[key], int) and not isinstance(brief[key], bool) and brief[key] > 0):
            problems.append(f"{key}: must be a positive whole number")
    if not isinstance(brief["researcher_name"], str):
        problems.append("researcher_name: must be text")
    if brief["anchor"] is not None and not (isinstance(brief["anchor"], str) and _ANCHOR.fullmatch(brief["anchor"])):
        problems.append("anchor: must be a persona id (lowercase letters, numbers and dashes) or null")
    items = brief["items"]
    if not isinstance(items, list):
        return problems + ["items: must be a list"]
    ready = brief["outcome"] == "ready"
    if ready:
        if brief["review"] not in REVIEWS:
            problems.append(f"review: a ready brief is one of {', '.join(REVIEWS)}")
        if not items:
            problems.append("items: a ready brief has at least one item")
        if not _same_instant(brief["fresh_until_ts"], brief["fresh_until_at"]):
            problems.append("fresh_until_at: a ready brief says when it goes stale")
    else:
        if brief["review"] is not None:
            problems.append("review: only a ready brief is reviewed")
        if items:
            problems.append("items: only a ready brief has items")
        if brief["fresh_until_ts"] is not None or brief["fresh_until_at"] is not None or brief["airable"]:
            problems.append("fresh_until/airable: only a ready brief can air")
    for index, item in enumerate(items, 1):
        _check_item(item, f"items[{index}]", problems)
        if isinstance(item, dict) and item.get("rank") != index:
            problems.append(f"items[{index}].rank: items are numbered 1, 2, 3 in order")
    if ready and items and all(isinstance(i, dict) and _number(i.get("published_ts")) for i in items) and _number(brief["fresh_until_ts"]):
        expected = min(i["published_ts"] for i in items) + brief["freshness_hours"] * 3600
        if abs(expected - brief["fresh_until_ts"]) > 1.0:
            problems.append("fresh_until_ts: must be the oldest item's time plus the hours it may stay fresh")
    sources = brief["sources"]
    if not (isinstance(sources, list) and all(_source_ok(entry) for entry in sources)):
        problems.append("sources: each source reports url, name, status, message, entries, format and checked_at, in the documented forms")
    summary = brief["summary"]
    if not (isinstance(summary, dict) and set(summary) == SUMMARY_KEYS and _skipped_ok(summary["skipped"]) and _count(summary["considered"])
            and summary["kept"] == len(items) and isinstance(summary["explanation"], str)):
        problems.append("summary: must report considered, kept, skipped (counts by the documented reasons) and an explanation, with kept equal to the number of items")
    if not (isinstance(brief["warnings"], list) and all(isinstance(w, str) for w in brief["warnings"])):
        problems.append("warnings: must be a list of text")
    return problems

