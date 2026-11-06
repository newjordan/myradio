"""Choosing what is worth saying: relevance, exclusions, freshness, de-duplication, diversity and tone.

This is a plain, inspectable scoring method. It is **heuristic**: it counts word and phrase matches, it does not
understand stories, and it is not sentiment analysis. Every number that decides whether an item is kept is
recorded next to the item as a reason a person can read.

Scoring, for each interest or research question (a "term"):

    phrase in the headline          6.0      phrase in the entry's tags      5.0      phrase in the text   3.5
    otherwise, by words (after dropping filler words and folding plurals):
        one-word term               3.0 if in the headline, 1.5 if in the text
        longer term                 needs 60% of its words present, then 1.0 + 2.0 * share + 1.0 * share in headline
    interests count in full, research questions at 0.8.

    relevance = best term + 0.25 * the next three best terms (at most 10)
    kept only if relevance - tone penalty >= 1.5

The tone filter subtracts the weight of every entry of a visible word list (outrage, fear and click-bait wording)
found in the headline (half weight if only in the first 300 characters of the text), up to 6.0. The list is
extendable per researcher (``tone_words``) and can be overridden (``tone_allow``). Nothing is ever boosted for
sounding cheerful.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .feeds import Entry
from .textutil import (build_extract, canonical_key, clean_text, contains_phrase, core_tokens, cut_at_word, digest, fold, iso_utc,
                       neutralize_speech, url_host)

MIN_RELEVANCE = 1.5
QUESTION_WEIGHT = 0.8
BODY_MATCH_CHARS = 3000
LEAD_CHARS = 300
MAX_PENALTY = 6.0
FUTURE_TOLERANCE = 15 * 60
SIMILARITY = 0.7
MAX_SCORED = 300

KINDS = ("reported", "commentary", "announcement", "unverified")
SKIP_REASONS = ("undated", "future", "too_old", "already_covered", "skipped_source", "skipped_topic", "not_relevant", "tone",
                "similar_to_aired", "duplicate", "source_limit", "no_room")

# (phrase, weight, list name). Matched as whole words or phrases after lower-casing and removing accents.
TONE_LEXICON = (
    ("outrage", 1.5, "outrage"), ("outraged", 1.5, "outrage"), ("furious", 1.5, "outrage"), ("fury", 1.5, "outrage"),
    ("slams", 1.5, "outrage"), ("slammed", 1.5, "outrage"), ("blasts", 1.5, "outrage"), ("blasted", 1.5, "outrage"),
    ("rips into", 1.5, "outrage"), ("lashes out", 1.5, "outrage"), ("eviscerates", 1.5, "outrage"), ("destroys", 1.0, "outrage"),
    ("obliterates", 1.0, "outrage"), ("erupts", 1.0, "outrage"), ("meltdown", 1.5, "outrage"), ("backlash", 1.0, "outrage"),
    ("scandal", 1.5, "outrage"), ("disgrace", 1.5, "outrage"), ("disgusting", 1.0, "outrage"), ("shameful", 1.0, "outrage"),
    ("unhinged", 1.5, "outrage"), ("feud", 1.0, "outrage"), ("clap back", 1.5, "outrage"), ("savage", 1.0, "outrage"),
    ("terrifying", 1.5, "fear"), ("horrifying", 1.5, "fear"), ("horrific", 1.5, "fear"), ("alarming", 1.0, "fear"),
    ("panic", 1.5, "fear"), ("nightmare", 1.5, "fear"), ("deadly", 1.0, "fear"), ("catastrophe", 1.5, "fear"),
    ("catastrophic", 1.5, "fear"), ("chaos", 1.0, "fear"), ("doom", 1.5, "fear"), ("doomed", 1.5, "fear"),
    ("devastating", 1.0, "fear"), ("terror", 1.5, "fear"), ("dread", 1.0, "fear"), ("scary", 1.0, "fear"),
    ("crisis", 1.0, "fear"), ("collapse", 1.0, "fear"), ("warning", 0.5, "fear"), ("threat", 0.5, "fear"),
    ("you won't believe", 2.0, "click-bait"), ("what happened next", 2.0, "click-bait"),
    ("will shock you", 2.0, "click-bait"), ("shocking", 1.5, "click-bait"), ("shocked", 1.0, "click-bait"),
    ("jaw-dropping", 1.5, "click-bait"), ("mind-blowing", 1.5, "click-bait"), ("bombshell", 1.5, "click-bait"),
    ("explosive", 1.0, "click-bait"), ("must-see", 1.0, "click-bait"),
    ("gone wrong", 1.0, "click-bait"), ("goes viral", 1.0, "click-bait"), ("went viral", 1.0, "click-bait"),
    ("here's why", 1.0, "click-bait"), ("the truth about", 1.0, "click-bait"), ("one weird trick", 2.0, "click-bait"),
    ("doctors hate", 2.0, "click-bait"), ("breaking news", 1.0, "click-bait"), ("urgent", 1.0, "click-bait"),
    ("exposed", 1.0, "click-bait"),
)
_HEDGES = ("rumor", "rumors", "rumour", "rumours", "rumored", "rumoured", "unconfirmed", "allegedly", "alleged", "reportedly",
           "speculation", "speculate", "speculated", "leaked", "leak", "purported", "unverified", "sources say", "insiders say",
           "according to sources", "claims that", "claimed that", "unofficial")
_COMMENTARY_LABELS = frozenset(["opinion", "editorial", "analysis", "commentary", "column", "op ed", "oped", "perspective", "viewpoint", "review", "essay"])
_ANNOUNCE = ("announces", "announced", "announcing", "introducing", "introduces", "launches", "launched", "launching",
             "now available", "available now", "release notes", "changelog", "press release", "we're excited", "we are excited",
             "pleased to announce", "is now live", "has released")


@dataclass(frozen=True)
class Term:
    raw: str
    phrase: str
    core: tuple
    weight: float = 1.0


def compile_terms(phrases, weight: float = 1.0) -> list:
    out = []
    for raw in phrases or ():
        phrase = fold(raw)
        if phrase:
            out.append(Term(raw, phrase, tuple(core_tokens(phrase)), weight))
    return out


class MatchContext:
    """Folded views of one entry, built once."""

    def __init__(self, title: str, body: str, cats):
        self.title = fold(title)
        self.title_tokens = set(core_tokens(self.title))
        self.body = fold(body[:BODY_MATCH_CHARS])
        self.body_tokens = set(core_tokens(self.body))
        self.lead = fold(body[:LEAD_CHARS])
        self.cats = fold(" ".join(cats))
        self.cat_tokens = set(core_tokens(self.cats))
        self.lead_tokens = set(core_tokens(fold(body[:600])))


def match_term(term: Term, ctx: MatchContext):
    """``(points, where)`` for one term against one entry, or ``(0.0, "")``."""
    if contains_phrase(ctx.title, term.phrase):
        return 6.0 * term.weight, "headline"
    if ctx.cats and contains_phrase(ctx.cats, term.phrase):
        return 5.0 * term.weight, "tags"
    if contains_phrase(ctx.body, term.phrase):
        return 3.5 * term.weight, "text"
    words = term.core
    if not words:
        return 0.0, ""
    in_head = sum(1 for w in words if w in ctx.title_tokens or w in ctx.cat_tokens)
    in_text = sum(1 for w in words if w in ctx.body_tokens and w not in ctx.title_tokens and w not in ctx.cat_tokens)
    if len(words) == 1:
        if in_head:
            return 3.0 * term.weight, "headline"
        return (1.5 * term.weight, "text") if in_text else (0.0, "")
    share = (in_head + in_text) / len(words)
    if share < 0.6:
        return 0.0, ""
    return (1.0 + 2.0 * share + 1.0 * (in_head / len(words))) * term.weight, ("headline" if in_head else "text")


def relevance(terms: list, ctx: MatchContext):
    """``(relevance, [(term, where, points), ...])`` best first."""
    hits = []
    for term in terms:
        points, where = match_term(term, ctx)
        if points > 0:
            hits.append((term.raw, where, round(points, 2)))
    hits.sort(key=lambda h: -h[2])
    if not hits:
        return 0.0, []
    total = hits[0][2] + 0.25 * sum(h[2] for h in hits[1:4])
    return min(10.0, round(total, 2)), hits


# ---- exclusions ----------------------------------------------------------------------------------

def topic_excluded(terms: list, ctx: MatchContext) -> Optional[str]:
    """The skipped topic an entry mentions, if any. Deliberately broad: a missed exclusion is worse than a
    lost item, so a multi-word topic also counts when all of its words appear in the headline, tags or lead."""
    for term in terms:
        if contains_phrase(ctx.title, term.phrase) or contains_phrase(ctx.cats, term.phrase) or contains_phrase(ctx.body, term.phrase):
            return term.raw
        if term.core and all(w in ctx.title_tokens or w in ctx.cat_tokens or w in ctx.lead_tokens for w in term.core):
            return term.raw
    return None


def compile_source_patterns(patterns) -> list:
    """Each pattern is a host name (``example.com`` also covers its sub-domains), an address with a path prefix, or a
    source name."""
    out = []
    for raw in patterns or ():
        text = clean_text(raw, 200).lower()
        if not text:
            continue
        if "/" in text or "://" in text:
            probe = text if "://" in text else "https://" + text
            host = url_host(probe)
            path = probe.split("://", 1)[1].split("/", 1)[1] if "/" in probe.split("://", 1)[1] else ""
            out.append(("url", host[4:] if host.startswith("www.") else host, path.strip("/"), raw))
        elif "." in text and " " not in text:
            out.append(("host", text[4:] if text.startswith("www.") else text, "", raw))
        else:
            out.append(("name", fold(text), "", raw))
    return out


def source_excluded(patterns: list, *, source_url: str, source_name: str, item_url: Optional[str]) -> Optional[str]:
    hosts = []
    for url in (source_url, item_url):
        host = url_host(url) if url else ""
        if host:
            hosts.append((host[4:] if host.startswith("www.") else host, url))
    name = fold(source_name or "")
    for kind, value, path, raw in patterns:
        if kind in ("host", "url"):
            for host, url in hosts:
                if host == value or host.endswith("." + value):
                    if kind == "host" or not path:
                        return raw
                    rest = url.split("://", 1)[-1].split("/", 1)[-1].strip("/")
                    if rest.startswith(path):
                        return raw
        elif value and (name == value or contains_phrase(name, value)):
            return raw
    return None


# ---- tone ----------------------------------------------------------------------------------------

class Lexicon:
    def __init__(self, extra=(), allow=(), enabled: bool = True):
        self.enabled = enabled
        allowed = {fold(a) for a in allow or ()}
        entries = [(fold(p), w, label, p) for p, w, label in TONE_LEXICON if fold(p) not in allowed]
        entries += [(fold(p), 1.5, "your own", clean_text(p, 60)) for p in extra or () if fold(p) and fold(p) not in allowed]
        self.entries = [e for e in entries if e[0]]

    def penalty(self, ctx: MatchContext):
        """``(penalty, [reason, ...])``."""
        if not self.enabled:
            return 0.0, []
        total, reasons = 0.0, []
        for phrase, weight, label, raw in self.entries:
            if contains_phrase(ctx.title, phrase):
                total += weight
                reasons.append(f"the headline uses \u201c{raw}\u201d ({label} word list, -{weight:g})")
            elif contains_phrase(ctx.lead, phrase):
                total += weight / 2
                reasons.append(f"the opening text uses \u201c{raw}\u201d ({label} word list, -{weight / 2:g})")
        return min(MAX_PENALTY, round(total, 2)), reasons


# ---- kind ----------------------------------------------------------------------------------------

def classify_kind(entry: Entry, source_kind: Optional[str], ctx: MatchContext):
    """``(kind, reason)`` from cues in the text and the label the operator gave the source. Heuristic."""
    for hedge in _HEDGES:
        if contains_phrase(ctx.title, hedge) or contains_phrase(ctx.lead, hedge):
            return "unverified", f"The text hedges (\u201c{hedge}\u201d), so it is treated as unconfirmed."
    labels = {fold(c) for c in entry.categories}
    path = (entry.link or "").lower()
    title = entry.title or ""
    head = title.split(":", 1)[0] if ":" in title else ""
    tagged = (labels & _COMMENTARY_LABELS or {w for w in _COMMENTARY_LABELS if f"/{w.replace(' ', '-')}/" in path}
              or ({fold(head)} & _COMMENTARY_LABELS if 0 < len(head) <= 20 else set()))
    if tagged:
        return "commentary", "It is tagged or titled as opinion, analysis or a review."
    if source_kind in ("reported", "commentary", "announcement"):
        return source_kind, f"You marked this source as {_KIND_WORDS[source_kind]}."
    if any(contains_phrase(ctx.title, fold(cue)) for cue in _ANNOUNCE):
        return "announcement", "The headline reads like the source announcing its own news."
    return "unverified", ("You have not said what kind of source this is, so nothing here is presented as confirmed. "
                          "Mark the source as news, opinion or announcements to change that.")


_KIND_WORDS = {"reported": "news or reporting", "commentary": "opinion or commentary", "announcement": "announcements from the maker"}


# ---- similarity ----------------------------------------------------------------------------------

def title_signature(title: str) -> frozenset:
    return frozenset(core_tokens(fold(title)))


def similar(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / len(a | b) >= SIMILARITY


@dataclass
class SeenIndex:
    keys: set = field(default_factory=set)
    signatures: list = field(default_factory=list)

    def has(self, key: str) -> bool:
        return key in self.keys

    def similar(self, signature: frozenset) -> bool:
        return any(similar(signature, other) for other in self.signatures)


# ---- candidates and selection ------------------------------------------------------------------------

@dataclass
class Candidate:
    entry: Entry
    source_url: str
    source_name: str
    source_kind: Optional[str]
    fetched_ts: float
    feed_link: Optional[str] = None
    key: str = ""

    def __post_init__(self):
        if not self.key:
            self.key = item_key(self.entry, self.feed_link)

    @property
    def text(self) -> str:
        entry = self.entry
        return entry.summary if len(entry.summary) >= 40 else (entry.content or entry.summary)


def item_key(entry: Entry, feed_link: Optional[str] = None) -> str:
    """Stable identity of an entry: its own address when that identifies it, otherwise its id, otherwise its title.

    An address that is the feed's own home page, or just a site's front page, does not identify an entry (some
    feeds give every entry the same link), so the entry's id or title is used instead.
    """
    link = canonical_key(entry.link) if entry.link else ""
    feed = canonical_key(feed_link) if feed_link else ""
    generic = bool(link) and (link == feed or link.split("/", 1)[-1] == "")
    if link and not generic:
        return digest("u", link)
    if entry.guid:
        return digest("g", entry.guid, link)
    return digest("t", fold(entry.title), link)


@dataclass
class Selection:
    items: list
    skipped: dict
    considered: int
    used: list            # [(item_key, signature_text)] to remember
    fresh_until: Optional[float]


def humanize_age(seconds: float) -> str:
    """'just now', '5 minutes ago', '1 hour ago', '3 days ago'."""
    if seconds < 90:
        return "just now"
    for limit, unit, size in ((3600, "minute", 60), (36 * 3600, "hour", 3600), (float("inf"), "day", 86400)):
        if seconds < limit:
            count = max(1, int(round(seconds / size)))
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "a while ago"


def limits_for(segment_seconds: int):
    """``(max_items, extract_chars, target_words)`` for a segment length."""
    max_items = max(1, min(8, int(round(segment_seconds / 20))))
    chars = max(200, min(700, int(segment_seconds * 2.5 / max_items * 6)))
    return max_items, chars, int(segment_seconds * 2.5)


def select_items(spec: dict, candidates: list, seen: SeenIndex, now: float) -> Selection:
    """Apply the whole pipeline to ``candidates`` and return the chosen items plus a count of why others were left out."""
    skipped = {reason: 0 for reason in SKIP_REASONS}
    window = float(spec["freshness_hours"]) * 3600
    interests = compile_terms(spec["interests"])
    questions = compile_terms(spec.get("questions", ()), QUESTION_WEIGHT)
    topics = compile_terms(spec.get("exclude_topics", ()))
    patterns = compile_source_patterns(spec.get("exclude_sources", ()))
    lexicon = Lexicon(spec.get("tone_words", ()), spec.get("tone_allow", ()), spec.get("tone_filter", True))
    max_items, extract_chars, _ = limits_for(spec["segment_seconds"])
    survivors = []
    seen_in_batch = set()
    for cand in candidates:
        entry = cand.entry
        ts = entry.published_ts
        if ts is None:
            skipped["undated"] += 1
        elif ts > now + FUTURE_TOLERANCE:
            skipped["future"] += 1
        elif now - ts > window:
            skipped["too_old"] += 1
        elif seen.has(cand.key) or cand.key in seen_in_batch:
            skipped["already_covered"] += 1
        else:
            seen_in_batch.add(cand.key)
            survivors.append(cand)
    survivors.sort(key=lambda c: -(c.entry.published_ts or 0))
    skipped["no_room"] += max(0, len(survivors) - MAX_SCORED)
    scored = []
    for cand in survivors[:MAX_SCORED]:
        entry = cand.entry
        ctx = MatchContext(entry.title, cand.text, entry.categories)
        blocked = source_excluded(patterns, source_url=cand.source_url, source_name=cand.source_name, item_url=entry.link)
        if blocked:
            skipped["skipped_source"] += 1
            continue
        if topic_excluded(topics, ctx):
            skipped["skipped_topic"] += 1
            continue
        score, hits = relevance(interests + questions, ctx)
        if score < MIN_RELEVANCE:
            skipped["not_relevant"] += 1
            continue
        penalty, reasons = lexicon.penalty(ctx)
        if score - penalty < MIN_RELEVANCE:
            skipped["tone"] += 1
            continue
        signature = title_signature(entry.title)
        if seen.similar(signature):
            skipped["similar_to_aired"] += 1
            continue
        kind, kind_reason = classify_kind(entry, cand.source_kind, ctx)
        scored.append({"cand": cand, "score": round(score - penalty, 2), "relevance": score, "hits": hits, "penalty": penalty,
                       "tone": reasons, "sig": signature, "kind": kind, "kind_reason": kind_reason,
                       "lead_fold": fold(cand.text[:160]) if len(cand.text) >= 60 else ""})
    scored.sort(key=lambda s: (-s["score"], -(s["cand"].entry.published_ts or 0), s["cand"].key))
    kept = []
    for item in scored:
        if any(similar(item["sig"], other["sig"]) or (item["lead_fold"] and item["lead_fold"] == other["lead_fold"]) for other in kept):
            skipped["duplicate"] += 1
            continue
        kept.append(item)
    chosen = _diversify(kept, max_items, skipped)
    items = [make_item(rank, picked, spec, now, extract_chars) for rank, picked in enumerate(chosen, 1)]
    fresh = None
    if items:
        fresh = min(i["published_ts"] for i in items) + window
    return Selection(items=items, skipped={k: v for k, v in skipped.items() if v}, considered=len(candidates),
                     used=[(p["cand"].key, " ".join(sorted(p["sig"]))[:200]) for p in chosen], fresh_until=fresh)


def _diversify(kept: list, max_items: int, skipped: dict) -> list:
    """Highest score first, but each extra item from the same source counts for less, and no source supplies more
    than two items (or more than the segment holds) while other sources have something to offer. Once the other sources
    have nothing left, the limit is lifted so that the segment is not left short while good items are waiting."""
    sources = {k["cand"].source_url for k in kept}
    cap = max_items if len(sources) < 2 else max(1, min(2, max_items))
    chosen, taken = [], {}
    pool = list(kept)
    lifted = False
    while pool and len(chosen) < max_items:
        best, best_value = None, -math.inf
        for item in pool:
            used = taken.get(item["cand"].source_url, 0)
            if used >= cap:
                continue
            value = item["score"] * (0.75 ** used)
            if value > best_value:
                best, best_value = item, value
        if best is None:
            if lifted:
                break
            cap, lifted = max_items, True            # everything left comes from sources already at the limit: nobody else to make room for
            continue
        chosen.append(best)
        pool.remove(best)
        taken[best["cand"].source_url] = taken.get(best["cand"].source_url, 0) + 1
    left = len(kept) - len(chosen)
    if left:
        capped = 0 if lifted else sum(1 for item in pool if taken.get(item["cand"].source_url, 0) >= cap)
        skipped["source_limit"] += capped
        skipped["no_room"] += left - capped
    return chosen


def make_item(rank: int, picked: dict, spec: dict, now: float, extract_chars: int) -> dict:
    cand, entry = picked["cand"], picked["cand"].entry
    extract, truncated = build_extract(cand.text, extract_chars) if cand.text else ("", False)
    headline = entry.title or cut_at_word(cand.text, 100)       # an entry without a title is introduced by the start of its own text
    why = [f"Matches \u201c{term}\u201d in the {where}" if where != "tags" else f"Matches \u201c{term}\u201d in its tags"
           for term, where, _ in picked["hits"][:3]]
    if entry.published_ts is not None:
        basis = " (dated by its last update)" if entry.date_basis == "updated" else ""
        why.append(f"Published {humanize_age(max(0.0, now - entry.published_ts))}{basis}")
    why.append(f"From a source you chose: {cand.source_name}")
    why.extend(f"Down-ranked because {reason}" for reason in picked["tone"])
    return {
        "id": cand.key[:16],
        "rank": rank,
        "headline": headline,
        "headline_speech": neutralize_speech(headline),
        "extract": extract,
        "extract_speech": neutralize_speech(extract),
        "extract_truncated": truncated,
        "source_name": cand.source_name,
        "source_name_speech": neutralize_speech(cand.source_name),
        "source_url": cand.source_url,
        "url": entry.link,
        "published_ts": entry.published_ts,
        "published_at": iso_utc(entry.published_ts),
        "date_precision": entry.date_precision,
        "date_basis": entry.date_basis or "published",
        "fetched_ts": cand.fetched_ts,
        "fetched_at": iso_utc(cand.fetched_ts),
        "kind": picked["kind"],
        "kind_reason": picked["kind_reason"],
        "score": picked["score"],
        "matched": [hit[0] for hit in picked["hits"][:5]],
        "why": why,
        "tone": {"penalty": picked["penalty"], "reasons": picked["tone"]},
        "categories": [clean_text(c, 60) for c in entry.categories[:5]],
    }
