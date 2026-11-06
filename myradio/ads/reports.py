"""Reports per campaign and day: plays, delivered share against the plan, and why spots were held back.

What the numbers mean
---------------------
* ``plays`` counts messages that started on the station's own audio clock and were not cut short.
  A message the operator skipped or stopped counts as ``interrupted`` instead. Nothing here shows who
  listened, or whether an outside stream received the audio.
* ``planned`` is what the booked plan asks of that day: the daily plan, or the total spread evenly
  over every day the campaign may play. Without a plan there is no ``planned`` and no share.
* ``delivered_share`` is ``plays / planned``.
* ``held_back`` counts the times a spot that was due to play was left out of a break the station
  built and recorded, by reason. Reasons that only mean "not scheduled for this time" are not counted.
* ``recovered`` counts plays found afterwards in the station's own history because the live signal
  was missed.
* ``unconfirmed`` counts the plays (already included in ``plays``) whose ending was never seen: an ad
  that is still on air, or whose end signal was missed. They are counted as played, and shown so nobody
  has to guess.
"""
from __future__ import annotations

import bisect
import datetime as dt
from typing import Optional

from .flights import day_bounds, is_active_day, active_days_total, local_datetime
from .model import AdsError, Campaign, zone_for
from .words import REASON_LABELS, REPORTED_REASONS

MAX_DAYS = 366
MAX_CELLS = 20000  # campaigns x days in one report
MAX_PLAYS_LISTED = 200
DAY = dt.timedelta(days=1)

BASIS = ("Counts show what this station played on its own audio clock. They do not show who listened or whether an "
         "outside stream received the audio.")
CSV_BASIS = "Station plays (not listeners)"  # on every row of the spreadsheet, which is the file most likely to be passed on

_FORMULA_FIRST = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def planned_for_day(campaign: Campaign, day: dt.date) -> Optional[float]:
    """Plays the plan asks for on ``day``, 0.0 on a day the campaign may not play, None without a plan."""
    plan = campaign.plan
    if plan.total is None and plan.per_day is None:
        return None
    if not is_active_day(campaign, day):
        return 0.0
    if plan.per_day is not None:
        return float(plan.per_day)
    days = active_days_total(campaign)
    return plan.total / days if days else None


def _share(plays: int, planned: Optional[float]) -> Optional[float]:
    return round(plays / planned, 4) if planned else None


def _plan_text(campaign: Campaign) -> Optional[str]:
    if campaign.plan.total is not None:
        return f"{campaign.plan.total} plays in total"
    if campaign.plan.per_day is not None:
        return f"{campaign.plan.per_day} plays a day"
    return None


def build_report(store, campaigns, *, now: float, first: Optional[dt.date] = None, last: Optional[dt.date] = None,
                 campaign_id: Optional[str] = None, max_days: int = MAX_DAYS, max_cells: int = MAX_CELLS) -> dict:
    """The report as plain data (JSON-ready). ``campaigns`` may include removed ones; those appear
    only if they have plays in the range."""
    chosen = list(campaigns)
    if campaign_id is not None:
        chosen = [c for c in chosen if c.id == campaign_id]
        if not chosen:
            raise AdsError("That spot isn't in your list. Reload the list and try again.")
    if first is not None and last is not None and first > last:
        raise AdsError("The report must start on or before the day it ends.")
    out_campaigns, notes = [], []
    recovered_total = unconfirmed_total = unsure_total = 0
    shown_first = shown_last = None
    cells = 0
    for campaign in sorted(chosen, key=lambda c: (c.created_at, c.id)):
        try:
            zone = zone_for(campaign.timezone)
        except AdsError:
            notes.append(f"{campaign.name} is left out because its timezone is one this computer doesn't know. Edit the spot and pick another.")
            continue
        end_day = last or local_datetime(now, zone).date()
        start_day = first or (end_day - 6 * DAY)
        span = (end_day - start_day).days + 1
        if span < 1 or span > max_days:
            raise AdsError(f"Choose a range of 1 to {max_days} days.")
        days = [start_day + i * DAY for i in range(span)]
        bounds = [day_bounds(d, zone) for d in days]
        rows = store.play_times(campaign.id, bounds[0][0], bounds[-1][1])
        removed = campaign.removed_at is not None
        if removed and not rows and campaign_id is None:
            continue
        cells += span
        if cells > max_cells:
            raise AdsError("That report would be very large. Choose fewer days, or pick one spot.")
        starts = [b[0] for b in bounds]
        plays = [0] * span
        cut = [0] * span
        recovered = [0] * span
        unsure = [0] * span
        for row in rows:
            i = bisect.bisect_right(starts, row["started_at"]) - 1
            if not 0 <= i < span:
                continue
            if row["outcome"] == "interrupted":
                cut[i] += 1
            else:
                plays[i] += 1
                if row["outcome"] is None:
                    unsure[i] += 1
            if row["source"] == "reconciled":
                recovered[i] += 1
            if row["paid"] and row["commercial_ack_id"] is None and row["outcome"] != "interrupted":
                unconfirmed_total += 1
        held = store.skip_counts(campaign.id, days[0].isoformat(), days[-1].isoformat())
        day_rows, planned_sum, any_plan = [], 0.0, False
        for i, day in enumerate(days):
            planned = planned_for_day(campaign, day)
            if planned is not None:
                any_plan, planned_sum = True, planned_sum + planned
            reasons = {k: v for k, v in held.get(day.isoformat(), {}).items() if v}
            day_rows.append({"date": day.isoformat(), "plays": plays[i], "interrupted": cut[i], "recovered": recovered[i],
                             "unconfirmed": unsure[i],
                             "planned": None if planned is None else round(planned, 4), "delivered_share": _share(plays[i], planned),
                             "held_back": reasons, "held_back_total": sum(reasons.values())})
        totals_held: dict = {}
        for entry in day_rows:
            for reason, n in entry["held_back"].items():
                totals_held[reason] = totals_held.get(reason, 0) + n
        total_plays = sum(plays)
        recovered_total += sum(recovered)
        unsure_total += sum(unsure)
        entry = {"id": campaign.id, "name": campaign.name, "advertiser": campaign.advertiser, "house": campaign.house,
                 "removed": removed, "timezone": campaign.timezone, "plan": _plan_text(campaign),
                 "from": days[0].isoformat(), "to": days[-1].isoformat(),
                 "totals": {"plays": total_plays, "interrupted": sum(cut), "recovered": sum(recovered),
                            "unconfirmed": sum(unsure),
                            "planned": round(planned_sum, 4) if any_plan else None,
                            "delivered_share": _share(total_plays, planned_sum if any_plan else None),
                            "held_back": totals_held, "held_back_total": sum(totals_held.values())},
                 "days": day_rows}
        if campaign_id is not None:
            listed = rows[-MAX_PLAYS_LISTED:]
            entry["plays"] = [{"at": dt.datetime.fromtimestamp(r["started_at"], dt.timezone.utc).isoformat(), "title": r["title"],
                               "outcome": r["outcome"] or "unconfirmed", "detail": r["detail"],
                               "source": "recovered" if r["source"] == "reconciled" else "live"} for r in listed]
        out_campaigns.append(entry)
        shown_first = days[0].isoformat() if shown_first is None else min(shown_first, days[0].isoformat())
        shown_last = days[-1].isoformat() if shown_last is None else max(shown_last, days[-1].isoformat())
    notes.insert(0, BASIS)
    if recovered_total:
        notes.append(f"{recovered_total} {'play was' if recovered_total == 1 else 'plays were'} found afterwards in the station's "
                     "history because the live signal was missed.")
    if unsure_total:
        notes.append(f"{unsure_total} {'play has' if unsure_total == 1 else 'plays have'} no ending on record: "
                     f"{'it may still be on air, or its' if unsure_total == 1 else 'some may still be on air, or their'} "
                     f"end was missed. {'It is' if unsure_total == 1 else 'They are'} counted as played.")
    if unconfirmed_total:
        notes.append(f"{unconfirmed_total} paid {'play' if unconfirmed_total == 1 else 'plays'} aired without the paid-use "
                     "confirmation on record. Confirm it in Spots.")
    fallback = (first or last or local_datetime(now, dt.timezone.utc).date())
    return {"from": shown_first or (first or fallback).isoformat(), "to": shown_last or (last or fallback).isoformat(),
            "generated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
            "campaigns": out_campaigns, "reasons": dict(REASON_LABELS), "notes": notes}


# ---------------------------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------------------------

def csv_cell(value) -> str:
    """One CSV cell. Text that a spreadsheet could run as a formula gets a leading apostrophe, and
    anything with a comma, quote or line break is quoted."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".") if value == value else ""
    else:
        text = str(value)
        if text.lstrip()[:1] in _FORMULA_FIRST or text[:1] in _FORMULA_FIRST:
            text = "'" + text
    if any(ch in text for ch in ',"\r\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def csv_header() -> list:
    return (["Date", "Spot", "Advertiser", "Type", "Plays", "Cut off", "Recovered", "Planned plays", "Delivered %",
             "Held back (total)"] + [f"Held back: {REASON_LABELS[r]}" for r in REPORTED_REASONS]
            + ["Ending not confirmed", "Basis"])


def report_csv(report: dict) -> str:
    """One row per spot and day, with a header row. Lines end with CRLF. "Ending not confirmed" is how many of the
    plays have no ending on record, and "Basis" says on every row what a play is, so the file still explains itself
    after it has been passed on."""
    lines = [",".join(csv_cell(h) for h in csv_header())]
    for campaign in report["campaigns"]:
        kind = "Station promo" if campaign["house"] else "Paid"
        for day in campaign["days"]:
            share = day["delivered_share"]
            cells = [day["date"], campaign["name"], campaign["advertiser"], kind, day["plays"], day["interrupted"],
                     day["recovered"], day["planned"], None if share is None else round(share * 100, 1), day["held_back_total"]]
            cells.extend(day["held_back"].get(reason, 0) for reason in REPORTED_REASONS)
            cells.extend([day.get("unconfirmed", 0), CSV_BASIS])
            lines.append(",".join(csv_cell(c) for c in cells))
    return "\r\n".join(lines) + "\r\n"
