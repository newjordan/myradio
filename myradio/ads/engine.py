"""The break builder: pure, deterministic choice of sponsor messages for one ad break.

``build_break(now, budget_seconds, context, seed)`` reads only its arguments (no clock, files,
network or global random numbers), so the same inputs and seed always give the same break, whatever
order the campaigns or the history arrive in. It returns the ordered messages to play and a plain
sentence for every spot it left out.

Order of decisions
------------------
1. Fixed checks, in this order: paused, flight dates, day, hours, show, confirmation for paid use,
   confirmation for voice rights, and whether the recording exists and can be played. A spot that
   fails one is left out and the first failure is the reason given.
2. The spots that pass are ranked: higher priority first, then the one furthest behind its pace (or
   with the most plays still booked), then the one that has waited longest, then a seeded tiebreak.
3. In that order each spot is tried against the limits: plan already met, plays in the last hour,
   today and this week, gap since its own last play, gap from the same category, even pace, the most
   spots in a break, the most spots in an hour, and the seconds left in the break. The first limit it
   breaks is the reason given. A spot that fits is added and later spots see it as already played.
4. Station promos ("house" spots) are tried only if no sponsor message made it in, or when the
   caller asks for the break to be filled. They follow exactly the same limits.

Where a message starts. A message is added at the end of the break, so it starts at the break's start
plus the seconds of the messages already chosen. Every limit that depends on time (plays in the last
hour, the gap since or before its own play, the gap from the same category, the station's hour) is
judged at that moment, not at the start of the break, against the history, the plays already planned
for later, and the messages ahead of it in this break. (Dates, days, hours and the day and week
counts are still judged once, at the start of the break.)

The seconds of the chosen messages never add up to more than the budget.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import hashlib
import math
from typing import Mapping, Optional, Sequence

from . import words
from .flights import (MAX_TIMESTAMP, day_bounds, expected_by_now, pace_target, week_bounds, window_state)
from .model import ROLE, AdsError, Campaign, Creative, estimate_seconds, spoken_text, zone_for

MAX_BREAK_SECONDS = 600.0
MAX_BREAK_ITEMS = 20
MAX_REPORTED_SKIPS = 60


@dataclasses.dataclass(frozen=True)
class Play:
    """One time a message started (or is planned to start) on air."""
    campaign_id: str
    creative_id: str
    at: float
    category: Optional[str] = None
    interrupted: bool = False  # cut short by a skip or stop: still counts toward limits, not toward delivery


@dataclasses.dataclass(frozen=True)
class Audio:
    """What the library knows about a track."""
    duration: float
    available: bool = True


@dataclasses.dataclass(frozen=True)
class BreakContext:
    """Everything the builder needs besides the time."""
    campaigns: Sequence = ()
    history: Sequence = ()  # Play objects: recent plays plus plays already planned
    delivered_totals: Mapping = dataclasses.field(default_factory=dict)  # campaign id -> plays delivered so far
    audio: Mapping = dataclasses.field(default_factory=dict)  # track id -> Audio
    show_id: Optional[str] = None
    max_per_break: int = 3
    max_per_hour: int = 8
    voice_rights: bool = False
    commercial_use: bool = False
    fill_with_house: bool = False
    ready_only: bool = True  # False also lists written messages whose voice recording is not made yet


@dataclasses.dataclass(frozen=True)
class BreakItem:
    """One message in a break, in the shared vocabulary: ``role`` is always "spot"."""
    position: int
    campaign_id: str
    campaign: str
    advertiser: str
    house: bool
    creative_id: str
    title: str
    kind: str
    track_id: Optional[str]
    duration: float
    estimated: bool
    ready: bool
    offset: float
    why: str
    role: str = ROLE

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Skip:
    campaign_id: str
    campaign: str
    reason: str
    structural: bool
    text: str
    creative_id: Optional[str] = None
    detail: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"campaign_id": self.campaign_id, "campaign": self.campaign, "creative_id": self.creative_id,
                "reason": self.reason, "structural": self.structural, "text": self.text}


@dataclasses.dataclass(frozen=True)
class BreakPlan:
    at: float
    seed: str
    show_id: Optional[str]
    budget_seconds: float
    seconds_used: float
    items: tuple
    skipped: tuple

    @property
    def creatives(self) -> tuple:
        """The ordered messages to play."""
        return self.items

    # A plan also behaves like the ordered list of its items: list(plan), len(plan), plan[0].
    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    @property
    def lines(self) -> list:
        return words.plan_lines(self.items, self.skipped)

    def to_dict(self, skipped_limit: int = MAX_REPORTED_SKIPS) -> dict:
        shown = self.skipped[:skipped_limit]
        return {"at": dt.datetime.fromtimestamp(self.at, dt.timezone.utc).isoformat(), "seed": self.seed,
                "show_id": self.show_id, "budget_seconds": self.budget_seconds, "seconds_used": self.seconds_used,
                "items": [i.to_dict() for i in self.items], "skipped": [s.to_dict() for s in shown],
                "skipped_total": len(self.skipped), "lines": self.lines}


# ---------------------------------------------------------------------------------------------

def _tie(seed: str, key: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}\x1f{key}".encode()).digest()[:8], "big")


def _between(times: list, low: float, high: float) -> int:
    """Plays with low <= time < high."""
    return bisect.bisect_left(times, high) - bisect.bisect_left(times, low)


def _trailing(times: list, now: float, span: float) -> int:
    """Plays in the span before ``now``: now - span < time <= now."""
    return bisect.bisect_right(times, now) - bisect.bisect_right(times, now - span)


def _nearest(times: list, now: float, gap: float):
    """The closest play within ``gap`` seconds of ``now`` (before or after), as (seconds, is_ahead)."""
    i = bisect.bisect_left(times, now)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(times):
            distance = abs(times[j] - now)
            if distance < gap and (best is None or distance < best[0]):
                best = (distance, times[j] > now)
    return best


class _History:
    def __init__(self, plays):
        self.starts: dict = {}
        self.delivered: dict = {}
        self.cat_times: dict = {}
        self.cat_ids: dict = {}
        self.every: list = []
        self.last_campaign: dict = {}
        self.last_creative: dict = {}
        usable = [p for p in plays if isinstance(p.at, (int, float)) and not isinstance(p.at, bool) and math.isfinite(p.at)]
        for p in sorted(usable, key=lambda p: (p.at, p.campaign_id, p.creative_id)):
            self.every.append(p.at)
            self.starts.setdefault(p.campaign_id, []).append(p.at)
            if not p.interrupted:
                self.delivered.setdefault(p.campaign_id, []).append(p.at)
            if p.category:
                self.cat_times.setdefault(p.category, []).append(p.at)
                self.cat_ids.setdefault(p.category, []).append(p.campaign_id)
            self.last_campaign[p.campaign_id] = p.at
            self.last_creative[p.creative_id] = p.at


@dataclasses.dataclass
class _Choice:
    creative: Creative
    duration: float
    estimated: bool
    ready: bool
    track_id: Optional[str]


@dataclasses.dataclass
class _Candidate:
    campaign: Campaign
    choices: list
    day_lo: float = 0.0
    day_hi: float = 0.0
    week_lo: float = 0.0
    week_hi: float = 0.0
    delivered_today: int = 0
    delivered_flight: int = 0
    expected: Optional[float] = None
    pace_delivered: int = 0
    urgency: float = 0.0


def _skip(campaign: Campaign, reason: str, detail: Optional[dict] = None, creative: Optional[Creative] = None) -> Skip:
    detail = detail or {}
    name = campaign.name if creative is None else f"{campaign.name} ({creative.title or 'message'})"
    return Skip(campaign.id, campaign.name, reason, reason in words.STRUCTURAL, words.skip_text(reason, name, detail),
                creative.id if creative else None, detail)


class _Builder:
    def __init__(self, now: float, budget: float, context: BreakContext, seed: str):
        self.now, self.budget, self.context, self.seed = now, budget, context, seed
        self.max_items = max(0, min(int(context.max_per_break), MAX_BREAK_ITEMS))
        self.max_hour = max(0, int(context.max_per_hour))
        unique: dict = {}
        for campaign in sorted(context.campaigns, key=lambda c: (c.id, -c.updated_at, c.name)):
            unique.setdefault(campaign.id, campaign)  # a spot listed twice still plays once; the most recently saved copy wins
        self.campaigns = list(unique.values())
        self.hist = _History(context.history)
        self.minutes = {c.id: c.separation.category_minutes for c in self.campaigns}
        self.names = {c.id: c.name for c in self.campaigns}
        self.category_reach: dict = {}
        for c in self.campaigns:
            if c.category:
                self.category_reach[c.category] = max(self.category_reach.get(c.category, 0), c.separation.category_minutes)
        self.items: list = []
        self.synthetic: list = []  # (offset into the break, campaign id, category) of messages already chosen for this break
        self.skips: list = []

    # -- fixed checks ------------------------------------------------------------------------
    def _choice(self, campaign: Campaign, creative: Creative):
        """(choice, None) when the message can play, else (None, (reason, detail))."""
        ctx = self.context
        if creative.spoken and not ctx.voice_rights:
            return None, ("voice_rights_needed", {})
        if creative.track_id:
            audio = ctx.audio.get(creative.track_id)
            if audio is None:
                return None, ("audio_missing", {})
            if not audio.available or not math.isfinite(audio.duration) or audio.duration <= 0:
                return None, ("audio_unavailable", {})
            return _Choice(creative, float(audio.duration), False, True, creative.track_id), None
        if not creative.spoken:
            return None, ("audio_missing", {})
        if ctx.ready_only:
            return None, ("audio_not_ready", {})
        guess = estimate_seconds(spoken_text(campaign.advertiser, creative.text, creative.disclosure))
        return _Choice(creative, guess, True, False, None), None

    def _fixed(self, campaign: Campaign) -> Optional[_Candidate]:
        if campaign.paused:
            self.skips.append(_skip(campaign, "paused"))
            return None
        try:
            window = window_state(campaign, self.now, self.context.show_id)
            zone = zone_for(campaign.timezone)
        except AdsError:
            self.skips.append(_skip(campaign, "timezone_unknown"))
            return None
        if not window.open:
            self.skips.append(_skip(campaign, window.reason, window.detail))
            return None
        if campaign.paid and not self.context.commercial_use:
            self.skips.append(_skip(campaign, "commercial_ack_needed"))
            return None
        usable, blocked = [], []
        for creative in sorted(campaign.creatives, key=lambda c: c.position):
            choice, problem = self._choice(campaign, creative)
            if choice is not None:
                usable.append(choice)
            else:
                blocked.append((creative, problem))
        if not usable:
            reason, detail = blocked[0][1] if blocked else ("no_creative", {})
            self.skips.append(_skip(campaign, reason, detail))
            return None
        for creative, (reason, detail) in blocked:
            self.skips.append(_skip(campaign, reason, detail, creative))
        usable.sort(key=lambda ch: (self.hist.last_creative.get(ch.creative.id, -math.inf),
                                    _tie(self.seed, ch.creative.id), ch.creative.position))
        cand = _Candidate(campaign, usable)
        local = window.local
        cand.day_lo, cand.day_hi = day_bounds(local.date(), zone)
        cand.week_lo, cand.week_hi = week_bounds(local.date(), zone)
        cand.delivered_today = _between(self.hist.delivered.get(campaign.id, []), cand.day_lo, cand.day_hi)
        try:
            cand.delivered_flight = max(0, int(self.context.delivered_totals.get(campaign.id, 0)))
        except (TypeError, ValueError):
            cand.delivered_flight = 0
        target = pace_target(campaign) if campaign.pacing == "even" else None
        if target:
            cand.expected = expected_by_now(campaign, window.flight, local, target)
            cand.pace_delivered = cand.delivered_today if target[0] == "day" else cand.delivered_flight
            cand.urgency = cand.expected - cand.pace_delivered
        elif campaign.pacing == "asap" and campaign.plan.per_day is not None:
            cand.urgency = float(max(campaign.plan.per_day - cand.delivered_today, 0))
        elif campaign.pacing == "asap" and campaign.plan.total is not None:
            cand.urgency = float(max(campaign.plan.total - cand.delivered_flight, 0))
        return cand

    def _rank(self, cand: _Candidate):
        last = self.hist.last_campaign.get(cand.campaign.id)
        return (-cand.campaign.priority, -round(cand.urgency, 9), -math.inf if last is None else last,
                _tie(self.seed, cand.campaign.id), cand.campaign.id)

    # -- limits --------------------------------------------------------------------------------
    def _offset(self) -> float:
        """Seconds into the break at which the next message would start: the length of those already chosen."""
        return math.fsum(i.duration for i in self.items)

    def _category_conflict(self, c: Campaign, offset: float):
        """A play of the same category too close to a message starting ``offset`` seconds into the break."""
        category, own = c.category, c.separation.category_minutes
        reach = max(own, self.category_reach.get(category, 0)) * 60
        if not reach:
            return None
        start = self.now + offset
        found = []  # (distance in seconds, is it after the start, other campaign id)
        times, ids = self.hist.cat_times.get(category, []), self.hist.cat_ids.get(category, [])
        lo, hi = bisect.bisect_left(times, start - reach), bisect.bisect_right(times, start + reach)
        found.extend((abs(t - start), t > start, cid) for t, cid in zip(times[lo:hi], ids[lo:hi]))
        # Messages already chosen for this break are compared by their place in it, so no time rounding creeps in.
        found.extend((offset - earlier, False, cid) for earlier, cid, cat in self.synthetic if cat == category)
        for distance, ahead, other_id in found:
            gap = max(own, self.minutes.get(other_id, own))
            if gap and distance < gap * 60:
                return {"seconds": distance, "ahead": ahead, "gap": gap, "category": category,
                        "other": self.names.get(other_id, "another spot")}
        return None

    def _blocker(self, cand: _Candidate):
        """(reason, detail, choice): the first limit the spot breaks, or the message to play."""
        c = cand.campaign
        if c.plan.per_day is not None and cand.delivered_today >= c.plan.per_day:
            return "plan_met", {"scope": "day", "n": c.plan.per_day}, None
        if c.plan.total is not None and cand.delivered_flight >= c.plan.total:
            return "plan_met", {"scope": "flight", "n": c.plan.total}, None
        offset = self._offset()
        start = self.now + offset  # the moment this message would start
        starts = self.hist.starts.get(c.id, [])
        for reason, limit, played in (
                ("hour_cap", c.caps.per_hour, lambda: _trailing(starts, start, 3600)),
                ("day_cap", c.caps.per_day, lambda: _between(starts, cand.day_lo, cand.day_hi)),
                ("week_cap", c.caps.per_week, lambda: _between(starts, cand.week_lo, cand.week_hi))):
            if limit is not None:
                n = played()
                if n >= limit:
                    return reason, {"played": n, "limit": limit}, None
        gap = c.separation.campaign_minutes
        if gap:
            near = _nearest(starts, start, gap * 60)
            if near:
                return "too_soon_campaign", {"seconds": near[0], "ahead": near[1], "gap": gap}, None
        if c.category:
            conflict = self._category_conflict(c, offset)
            if conflict:
                return "too_soon_category", conflict, None
        if cand.expected is not None and cand.pace_delivered > cand.expected + 1e-9:
            return "ahead_of_pace", {}, None
        if len(self.items) >= self.max_items:
            return "break_full", {"n": self.max_items}, None
        played = _trailing(self.hist.every, start, 3600) + len(self.items)
        if played >= self.max_hour:
            return "hour_limit_station", {"played": played, "limit": self.max_hour}, None
        chosen = [i.duration for i in self.items]
        for choice in cand.choices:
            if math.fsum(chosen + [choice.duration]) <= self.budget:
                return None, {}, choice
        return "no_room", {"need": min(ch.duration for ch in cand.choices),
                           "left": max(0.0, self.budget - math.fsum(chosen))}, None

    # -- building --------------------------------------------------------------------------------
    def _why(self, cand: _Candidate, fallback: bool) -> str:
        c = cand.campaign
        if c.house:
            return ("Station promo, used because no sponsor message could play." if fallback
                    else "Station promo, added to fill the break.")
        parts = [f"Priority {c.priority} of 10."]
        if cand.expected is not None and cand.expected - cand.pace_delivered >= 0.5:
            parts.append("It is behind its even pace.")
        elif c.pacing == "asap" and cand.urgency >= 1:
            parts.append("It is booked to play as soon as it can.")
        if c.id not in self.hist.last_campaign:
            parts.append("It hasn't played yet.")
        return " ".join(parts)

    def _consider(self, cand: _Candidate, fallback: bool = False) -> None:
        reason, detail, choice = self._blocker(cand)
        if reason:
            self.skips.append(_skip(cand.campaign, reason, detail))
            return
        c, creative = cand.campaign, choice.creative
        offset = self._offset()
        self.items.append(BreakItem(len(self.items) + 1, c.id, c.name, c.advertiser, c.house, creative.id,
                                    creative.title or c.name, creative.kind, choice.track_id, choice.duration,
                                    choice.estimated, choice.ready, offset, self._why(cand, fallback)))
        if c.category:
            self.synthetic.append((offset, c.id, c.category))

    def run(self) -> BreakPlan:
        candidates = [cand for cand in (self._fixed(c) for c in self.campaigns) if cand is not None]
        sponsors = sorted((c for c in candidates if not c.campaign.house), key=self._rank)
        houses = sorted((c for c in candidates if c.campaign.house), key=self._rank)
        for cand in sponsors:
            self._consider(cand)
        if not self.items or self.context.fill_with_house:
            fallback = not self.items
            for cand in houses:
                self._consider(cand, fallback)
        else:
            self.skips.extend(_skip(c.campaign, "house_standby") for c in houses)
        skipped = sorted(self.skips, key=lambda s: (s.structural, s.campaign, s.campaign_id, s.creative_id or ""))
        return BreakPlan(self.now, self.seed, self.context.show_id, self.budget, math.fsum(i.duration for i in self.items),
                         tuple(self.items), tuple(skipped))


def _finite(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AdsError(f"{label} must be a number.")
    return float(value)


def build_break(now: float, budget_seconds: float, context: BreakContext, seed="") -> BreakPlan:
    """Choose the sponsor messages for one break.

    ``now`` is the epoch second the break is expected to start. ``budget_seconds`` is the most the
    break may last. The result is deterministic for the same ``context`` and ``seed``."""
    now = _finite(now, "The break time")
    if not 0 <= now <= MAX_TIMESTAMP:
        raise AdsError("That time is outside the range the station can handle.")
    budget = min(max(_finite(budget_seconds, "The break length"), 0.0), MAX_BREAK_SECONDS)
    if not isinstance(context, BreakContext):
        raise AdsError("The break needs its campaigns and history.")
    if context.show_id is not None and not isinstance(context.show_id, str):
        raise AdsError("A show id must be text.")
    return _Builder(now, budget, context, str(seed)[:200]).run()
