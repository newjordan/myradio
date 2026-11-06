"""The planner: turns the week into a time-ordered list of plan items.

``plan(week, from_ts, to_ts, seed)`` is a pure function of the saved schedule, the window and the
seed. It never reads the clock, so it gives the same answer today, tomorrow and after a restart, and
two calls with overlapping windows describe the same items with the same ids.

How the time is cut up
    The timeline is first split into stretches with one show on air each (see ``timeline``). Each
    stretch is then cut at every top of a local hour into *blocks*. A show that starts or ends part-way
    through an hour gets a shorter block there; otherwise a block is a full hour (3600 seconds). Every
    block gets its own station ID, host breaks, spot break, update and songs from ``clocks.hour_plan``,
    and the estimated seconds of a block's items add up to the block's length (blocks under one minute
    hold nothing; blocks under five minutes hold one filler song).

An item belongs to a window when ``from_ts <= item.start < to_ts``, so consecutive windows never
repeat or lose an item. Items of the block that is already running when the window opens are included
if their start is later than ``from_ts``.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Optional

from .clocks import MAX_SEED, hour_plan
from .labels import ITEM_LABELS
from .timeline import DAY, HOUR, MAX_TS, blocks, check_timestamp, to_local_iso
from .model import ShowError

ROLES = ("song", "host_break", "id", "spot", "update")
ROLE_KIND = {"song": "music", "host_break": "speech", "id": "speech", "update": "speech", "spot": "ad"}
MAX_PLAN_SECONDS = 168 * HOUR         # one week at most per call
MARGIN = 2 * DAY                      # extra time looked at on both sides so blocks are cut identically in every window
NAMESPACE = uuid.UUID("d1c5a8a0-6f0e-5b7a-9a52-0c7f1d3b4e90")


@dataclass(frozen=True)
class PlanItem:
    id: str                      # 32 lowercase hex characters; the same slot, role and seed always give the same id
    start: float                 # soft earliest start, epoch seconds UTC
    role: str                    # song | host_break | id | spot | update
    kind: str                    # library kind a finished item imports as: music | speech | ad
    show_id: str                 # the show that owns the slot (the default show's id for unscheduled time)
    host_id: Optional[str]       # voice for speech roles; None means "the station's default voice"
    est_seconds: int             # planned length in whole seconds
    constraints: dict            # role-specific guidance, see ``constraints_for``
    slot_start: float            # epoch seconds where this item's block begins
    slot_seconds: int            # length of the block; the block's est_seconds add up to this
    position: int                # 0-based place in the block
    ordinal: int                 # 0-based place among items of the same role in the block
    fallback: bool               # True when the default show fills time no show is scheduled for
    signature: str               # 16 hex characters; changes when the show's rules for this item change

    def to_dict(self, zone: Optional[str] = None) -> dict:
        out = {
            "id": self.id, "start": self.start, "role": self.role, "kind": self.kind, "show_id": self.show_id,
            "host_id": self.host_id, "est_seconds": self.est_seconds, "constraints": json.loads(json.dumps(self.constraints)),
            "slot_start": self.slot_start, "slot_seconds": self.slot_seconds, "position": self.position,
            "ordinal": self.ordinal, "fallback": self.fallback, "signature": self.signature,
            "label": ITEM_LABELS[self.role],
        }
        if zone:
            out["start_iso"] = to_local_iso(zone, self.start)
            out["slot_start_iso"] = to_local_iso(zone, self.slot_start)
        return out


def constraints_for(role: str, show, est_seconds: int) -> dict:
    """Guidance for whoever makes the item. All of it is advice to the director, never a command."""
    if role == "song":
        style = show.style
        return {"palettes": list(style.palettes), "tags": list(style.tags), "energy": style.energy,
                "adventurousness": style.adventurousness, "vocals": style.vocals}
    if role == "host_break":
        return {"talk": show.talk, "max_seconds": est_seconds, "optional": True}
    if role == "id":
        return {"max_seconds": est_seconds, "optional": False}
    if role == "spot":
        return {"max_seconds": est_seconds, "optional": True}
    return {"researcher_id": show.update_researcher_id, "max_seconds": est_seconds, "optional": True,
            "if_nothing_new": "play_music"}


def check_seed(seed) -> int:
    if type(seed) is not int or not 0 <= seed <= MAX_SEED:
        raise ShowError(f"The seed must be a whole number from 0 to {MAX_SEED}.")
    return seed


def _signature(role: str, show_id: str, host_id: Optional[str], est: int, constraints: dict) -> str:
    text = json.dumps([role, show_id, host_id, est, constraints], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def plan(week, from_ts, to_ts, seed: int = 0) -> list:
    """Plan items whose start lies in ``[from_ts, to_ts)``, in time order. At most one week per call."""
    low_edge, high_edge = check_timestamp(from_ts, "start"), check_timestamp(to_ts, "end")
    start_f, end_f = float(from_ts), float(to_ts)
    if end_f <= start_f:
        raise ShowError("The end of the plan must be after its start.")
    if end_f - start_f > MAX_PLAN_SECONDS:
        raise ShowError("Ask for at most a week (168 hours) of plan at a time.")
    seed = check_seed(seed)
    segments = week.segments(max(0, low_edge - MARGIN), min(MAX_TS, high_edge + 1 + MARGIN))
    items: list = []
    for segment in segments:
        if segment.end <= start_f:
            continue
        if segment.start >= end_f:
            break
        for block_start, block_end in blocks(week.timezone, segment.start, segment.end):
            if block_end <= start_f:
                continue
            if block_start >= end_f:
                break
            seconds = block_end - block_start
            show = segment.show
            sequence = hour_plan(show, f"{seed}|{show.id}|{block_start}", seconds=seconds)
            moment = block_start
            for position, entry in enumerate(sequence):
                if start_f <= moment < end_f:
                    constraints = constraints_for(entry.role, show, entry.est_seconds)
                    identity = f"{seed}|{show.id}|{block_start}|{entry.role}|{entry.ordinal}"
                    items.append(PlanItem(
                        id=uuid.uuid5(NAMESPACE, identity).hex, start=float(moment), role=entry.role,
                        kind=ROLE_KIND[entry.role], show_id=show.id,
                        host_id=None if entry.role == "song" else show.host_id,
                        est_seconds=entry.est_seconds, constraints=constraints, slot_start=float(block_start),
                        slot_seconds=seconds, position=position, ordinal=entry.ordinal, fallback=segment.fallback,
                        signature=_signature(entry.role, show.id, None if entry.role == "song" else show.host_id,
                                             entry.est_seconds, constraints)))
                moment += entry.est_seconds
    return items
