"""The break builder: caps, separation, pacing, ranking, budget, house fallback, determinism and explanations."""
import json
import math
import random
import time
import unittest
from unittest import mock

from myradio.ads.engine import Audio, Play, build_break
from myradio.ads.words import STRUCTURAL

try:
    from test_ads_support import DAY, HOUR, MIN, NOW, context, make, play, utc
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import DAY, HOUR, MIN, NOW, context, make, play, utc

JARGON = ("PCM", "GGUF", "SSRF", "Traceback", "NoneType", "KeyError", "None", "{", "}")


def build(campaigns, history=(), *, budget=120.0, now=NOW, seed="seed", audio=None, **kw):
    return build_break(now, budget, context(campaigns, history, audio=audio, **kw), seed)


def campaign_reasons(plan):
    return {s.campaign: s.reason for s in plan.skipped if s.creative_id is None}


def chosen(plan):
    return [i.campaign for i in plan.items]


class LimitTests(unittest.TestCase):
    """One spot, one rule: does it play or not? NOW is Thursday 2026-11-05 15:00 UTC."""

    MONDAY = utc(2026, 11, 2)
    TODAY_START = utc(2026, 11, 5)

    def cases(self):
        n = NOW
        return [
            # label, campaign settings, play times, plays that were cut short, expected reason (None = it plays)
            ("hour cap reached", {"caps": {"per_hour": 2}}, [n - 600, n - 3000], [], "hour_cap"),
            ("hour cap, one is older than an hour", {"caps": {"per_hour": 2}}, [n - 600, n - 3601], [], None),
            ("hour cap, exactly an hour ago is outside the window", {"caps": {"per_hour": 2}}, [n - 600, n - 3600], [], None),
            ("hour cap, a second inside the window counts", {"caps": {"per_hour": 2}}, [n - 600, n - 3599], [], "hour_cap"),
            ("hour cap, a planned play later today does not count against the last hour", {"caps": {"per_hour": 1}}, [n + 600], [], None),
            ("day cap reached", {"caps": {"per_day": 3}}, [n - HOUR, n - 2 * HOUR, n - 3 * HOUR], [], "day_cap"),
            ("day cap, yesterday does not count", {"caps": {"per_day": 3}}, [n - HOUR, n - 2 * HOUR, n - 16 * HOUR], [], None),
            ("day cap, midnight itself starts the day", {"caps": {"per_day": 1}}, [LimitTests.TODAY_START], [], "day_cap"),
            ("day cap, one second before midnight is yesterday", {"caps": {"per_day": 1}}, [LimitTests.TODAY_START - 1], [], None),
            ("day cap counts a play planned later the same day", {"caps": {"per_day": 1}}, [n + 3 * HOUR], [], "day_cap"),
            ("day cap uses the campaign's own zone", {"timezone": "America/New_York", "caps": {"per_day": 2}},
             [utc(2026, 11, 5, 4, 30), utc(2026, 11, 5, 4, 59)], [], None),  # 23:30 and 23:59 on the 4th in New York
            ("day cap in the campaign's zone, after its midnight", {"timezone": "America/New_York", "caps": {"per_day": 2}},
             [utc(2026, 11, 5, 5, 0), utc(2026, 11, 5, 6, 0)], [], "day_cap"),
            ("week cap reached", {"caps": {"per_week": 5}},
             [n - HOUR, n - DAY, n - 2 * DAY, n - 3 * DAY, LimitTests.MONDAY + 1800], [], "week_cap"),
            ("week cap, Sunday belongs to the week before", {"caps": {"per_week": 5}},
             [n - HOUR, n - DAY, n - 2 * DAY, n - 3 * DAY, LimitTests.MONDAY - 1], [], None),
            ("gap since its own last play", {"separation": {"campaign_minutes": 20}}, [n - 19 * MIN], [], "too_soon_campaign"),
            ("gap, exactly long enough", {"separation": {"campaign_minutes": 20}}, [n - 20 * MIN], [], None),
            ("gap, a play planned soon after", {"separation": {"campaign_minutes": 20}}, [n + 10 * MIN], [], "too_soon_campaign"),
            ("gap, a play planned much later", {"separation": {"campaign_minutes": 20}}, [n + 21 * MIN], [], None),
            ("a cut-short play still counts toward the hourly limit", {"caps": {"per_hour": 1}}, [], [n - 10 * MIN], "hour_cap"),
            ("a cut-short play still counts toward the gap", {"separation": {"campaign_minutes": 20}}, [], [n - 10 * MIN], "too_soon_campaign"),
            ("daily plan met", {"plan": {"per_day": 2}, "pacing": "asap"}, [n - HOUR, n - 2 * HOUR], [], "plan_met"),
            ("daily plan, one cut short is not delivered", {"plan": {"per_day": 2}, "pacing": "asap"}, [n - HOUR], [n - 2 * HOUR], None),
            ("daily plan, yesterday does not count", {"plan": {"per_day": 1}, "pacing": "asap"}, [n - 20 * HOUR], [], None),
            ("no limits at all", {}, [n - 1, n - 2, n - 3], [], None),
        ]

    def test_every_limit_in_a_table(self):
        for label, settings, plays, cut, expected in self.cases():
            with self.subTest(case=label):
                campaign = make("Acme", **settings)
                history = [play(campaign, t) for t in plays] + [play(campaign, t, interrupted=True) for t in cut]
                plan = build([campaign], history)
                if expected is None:
                    self.assertEqual(chosen(plan), ["Acme"], plan.lines)
                else:
                    self.assertEqual(chosen(plan), [], plan.lines)
                    self.assertEqual(campaign_reasons(plan), {"Acme": expected})

    def test_a_total_plan_counts_plays_over_the_whole_run(self):
        campaign = make("Acme", plan={"total": 5}, pacing="asap")
        for delivered, expected in ((4, ["Acme"]), (5, []), (6, [])):
            with self.subTest(delivered=delivered):
                plan = build([campaign], delivered_totals={campaign.id: delivered})
                self.assertEqual(chosen(plan), expected)
                if not expected:
                    self.assertEqual(campaign_reasons(plan), {"Acme": "plan_met"})

    def test_the_first_limit_broken_is_the_reason_given(self):
        campaign = make("Acme", caps={"per_hour": 1, "per_day": 1}, separation={"campaign_minutes": 30}, plan={"per_day": 1}, pacing="asap")
        plan = build([campaign], [play(campaign, NOW - 5 * MIN)])
        self.assertEqual(campaign_reasons(plan), {"Acme": "plan_met"})
        campaign = make("Acme", caps={"per_hour": 1, "per_day": 1}, separation={"campaign_minutes": 30})
        plan = build([campaign], [play(campaign, NOW - 5 * MIN)])
        self.assertEqual(campaign_reasons(plan), {"Acme": "hour_cap"})

    def test_a_campaign_with_several_limits_needs_to_clear_all_of_them(self):
        campaign = make("Acme", caps={"per_hour": 3, "per_day": 3, "per_week": 3}, separation={"campaign_minutes": 5})
        history = [play(campaign, NOW - 2 * HOUR), play(campaign, NOW - 3 * HOUR)]
        self.assertEqual(chosen(build([campaign], history)), ["Acme"])
        history.append(play(campaign, NOW - 4 * HOUR))
        self.assertEqual(campaign_reasons(build([campaign], history)), {"Acme": "day_cap"})


class CategoryTests(unittest.TestCase):
    def test_a_similar_spot_too_close_blocks_in_both_directions(self):
        for label, own_minutes, other_minutes, ago, expected in (
                ("both ten minutes, five minutes ago", 10, 10, 5 * MIN, "too_soon_category"),
                ("both ten minutes, exactly ten ago", 10, 10, 10 * MIN, None),
                ("the other one asks for more room", 5, 30, 10 * MIN, "too_soon_category"),
                ("the other one asks for more room, enough passed", 5, 30, 31 * MIN, None),
                ("we ask for more room than the other", 30, 5, 10 * MIN, "too_soon_category"),
                ("no gap asked by anyone", 0, 0, 1 * MIN, None),
        ):
            with self.subTest(case=label):
                mine = make("Mine", category="bikes", separation={"category_minutes": own_minutes})
                other = make("Other", category="bikes", separation={"category_minutes": other_minutes}, paused=True)
                plan = build([mine, other], [play(other, NOW - ago)])
                self.assertEqual("Mine" in chosen(plan), expected is None, plan.lines)
                if expected:
                    self.assertEqual(campaign_reasons(plan)["Mine"], expected)
                    self.assertTrue(any("Other" in s.text for s in plan.skipped if s.campaign == "Mine"))

    def test_other_categories_and_no_category_do_not_interact(self):
        mine = make("Mine", category="bikes", separation={"category_minutes": 30})
        for label, other in (("another category", make("Other", category="coffee", separation={"category_minutes": 30}, paused=True)),
                             ("no category", make("Other", separation={"category_minutes": 30}, paused=True))):
            with self.subTest(case=label):
                self.assertEqual(chosen(build([mine, other], [play(other, NOW - MIN)])), ["Mine"])
        loner = make("Loner", separation={"category_minutes": 30})
        mate = make("Mate", separation={"category_minutes": 30})
        self.assertEqual(sorted(chosen(build([loner, mate]))), ["Loner", "Mate"])

    def test_two_similar_spots_do_not_share_a_break(self):
        high = make("High", category="bikes", priority=9, separation={"category_minutes": 10})
        low = make("Low", category="bikes", priority=2, separation={"category_minutes": 10})
        plan = build([low, high])
        self.assertEqual(chosen(plan), ["High"])
        self.assertEqual(campaign_reasons(plan), {"Low": "too_soon_category"})
        free = make("Free", category="bikes", priority=2, separation={"category_minutes": 0})
        plan = build([free, make("High", category="bikes", priority=9, separation={"category_minutes": 0})])
        self.assertEqual(sorted(chosen(plan)), ["Free", "High"])

    def test_a_play_by_a_removed_spot_still_keeps_similar_spots_apart(self):
        mine = make("Mine", category="bikes", separation={"category_minutes": 15})
        ghost = Play("f" * 32, "e" * 32, NOW - 5 * MIN, "bikes")
        plan = build([mine], [ghost])
        self.assertEqual(campaign_reasons(plan), {"Mine": "too_soon_category"})
        self.assertEqual(chosen(build([mine], [Play("f" * 32, "e" * 32, NOW - 16 * MIN, "bikes")])), ["Mine"])


def start_complaints(plan, ctx):
    """An independent check of the limits that depend on time, judged where each message really starts
    (the break's start plus its offset), against the history, the plays planned for later, and the messages
    ahead of it in the same break. Returns readable complaints; an empty list means every message is fine."""
    by_id = {c.id: c for c in ctx.campaigns}
    complaints = []
    for index, item in enumerate(plan.items):
        c = by_id[item.campaign_id]
        start = plan.at + item.offset
        starts = [p.at for p in ctx.history if p.campaign_id == c.id]
        if c.caps.per_hour is not None:
            n = sum(1 for t in starts if start - 3600 < t <= start)
            if n >= c.caps.per_hour:
                complaints.append(f"{c.name}: {n} plays in the hour before it starts (limit {c.caps.per_hour})")
        gap = c.separation.campaign_minutes * 60
        if gap and any(abs(t - start) < gap for t in starts):
            complaints.append(f"{c.name}: a play of its own less than {gap} s from its start")
        if c.category:
            near = [(p.at, by_id[p.campaign_id].separation.category_minutes if p.campaign_id in by_id else c.separation.category_minutes)
                    for p in ctx.history if p.category == c.category]
            near += [(plan.at + before.offset, by_id[before.campaign_id].separation.category_minutes)
                     for before in plan.items[:index] if by_id[before.campaign_id].category == c.category]
            for t, minutes in near:
                reach = max(c.separation.category_minutes, minutes) * 60
                if reach and abs(t - start) < reach:
                    complaints.append(f"{c.name}: a similar spot {abs(t - start):.1f} s from its start (asked {reach} s)")
                    break
        if sum(1 for p in ctx.history if start - 3600 < p.at <= start) + index >= ctx.max_per_hour:
            complaints.append(f"{c.name}: the station already played {ctx.max_per_hour} spots in the hour before it starts")
    return complaints


class StartOfEachMessageTests(unittest.TestCase):
    """Every limit that depends on time is judged at the moment the message would really start: the start of
    the break plus the seconds of the messages ahead of it. Judging it at the start of the break lets two similar
    spots sit half a minute apart and lets a spot play just before a play already planned."""

    def thirty_second_spots(self, *names, **settings):
        return [make(name, priority=10 - i, track=f"t{i}", **settings) for i, name in enumerate(names)]

    def test_two_similar_spots_in_one_break_are_a_full_gap_apart(self):
        filler1, filler2 = make("Filler1", priority=10, track="f1"), make("Filler2", priority=9, track="f2")
        x1 = make("X1", priority=5, category="x", track="x1", separation={"category_minutes": 1})
        x2 = make("X2", priority=4, category="x", track="x2", separation={"category_minutes": 1})
        plan = build([filler1, filler2, x1, x2], budget=600, max_per_break=5, max_per_hour=20)
        # X1 starts 60 s in. X2 would start 90 s in, 30 s after X1 although a minute was asked, so it waits for another break.
        self.assertEqual(chosen(plan), ["Filler1", "Filler2", "X1"])
        self.assertEqual(campaign_reasons(plan)["X2"], "too_soon_category")
        skip = next(s for s in plan.skipped if s.campaign == "X2")
        self.assertEqual((skip.detail["seconds"], skip.detail["ahead"]), (30.0, False))

    def test_a_five_minute_gap_holds_inside_one_break_too(self):
        fillers = [make(f"F{i:02d}", priority=10, track=f"f{i}") for i in range(11)]
        x1 = make("X1", priority=5, category="x", track="x1", separation={"category_minutes": 5})
        x2 = make("X2", priority=4, category="x", track="x2", separation={"category_minutes": 5})
        plan = build(fillers + [x1, x2], budget=600, max_per_break=20, max_per_hour=100)
        starts = {i.campaign: i.offset for i in plan.items}
        self.assertEqual(starts["X1"], 330.0)  # eleven 30-second spots first
        self.assertNotIn("X2", starts)  # it would start 360 s in, 30 s after X1 although 300 s were asked
        self.assertEqual(campaign_reasons(plan)["X2"], "too_soon_category")

    def test_similar_spots_exactly_a_gap_apart_may_share_a_break(self):
        x1 = make("X1", priority=10, category="x", track="x1", separation={"category_minutes": 1})
        middle = make("Middle", priority=9, track="mid")
        x2 = make("X2", priority=8, category="x", track="x2", separation={"category_minutes": 1})
        plan = build([x1, middle, x2], budget=600)
        # X2 starts a full minute after X1 starts. Measured from the start of the break it would have looked like it was 0 s away.
        self.assertEqual([(i.campaign, i.offset) for i in plan.items], [("X1", 0.0), ("Middle", 30.0), ("X2", 60.0)])

    def test_a_gap_to_a_play_planned_for_later_is_measured_from_where_the_message_starts(self):
        fillers = self.thirty_second_spots("Filler1", "Filler2")
        spot = make("Acme", priority=1, separation={"campaign_minutes": 20})
        planned = [play(spot, NOW + 20.5 * MIN)]
        plan = build(fillers + [spot], planned, budget=600)
        self.assertEqual(chosen(plan), ["Filler1", "Filler2"])  # Acme would start 60 s in, 19.5 minutes before the planned play
        self.assertEqual(campaign_reasons(plan)["Acme"], "too_soon_campaign")
        skip = next(s for s in plan.skipped if s.campaign == "Acme")
        self.assertEqual((skip.detail["seconds"], skip.detail["ahead"]), (1170.0, True))
        self.assertEqual(chosen(build([spot], planned, budget=600)), ["Acme"])  # started right away it is 20.5 minutes ahead of it

    def test_a_gap_after_a_play_in_the_past_is_measured_from_where_the_message_starts_too(self):
        # Acme played 19 minutes before the break starts. Placed first, it is 19 minutes after that play: too soon.
        # Placed a minute and a half into the break it is 20.5 minutes after: fine.
        spot = make("Acme", priority=1, separation={"campaign_minutes": 20})
        played = [play(spot, NOW - 19 * MIN)]
        self.assertEqual(campaign_reasons(build([spot], played)), {"Acme": "too_soon_campaign"})
        fillers = self.thirty_second_spots("Filler1", "Filler2", "Filler3")
        plan = build(fillers + [spot], played, budget=600, max_per_break=5)
        self.assertEqual([(i.campaign, i.offset) for i in plan.items][-1], ("Acme", 90.0))

    def test_the_hourly_limit_counts_plays_planned_before_the_message_starts(self):
        filler = make("Filler", priority=9, track="f1")
        spot = make("Acme", priority=1, caps={"per_hour": 1})
        planned = [play(spot, NOW + 20)]
        plan = build([filler, spot], planned)
        self.assertEqual(chosen(plan), ["Filler"])  # Acme would start 30 s in, after the play planned 20 s in
        self.assertEqual(campaign_reasons(plan)["Acme"], "hour_cap")
        self.assertEqual(chosen(build([spot], planned)), ["Acme"])  # first in the break it starts before that play, as always

    def test_the_hourly_limit_lets_an_old_play_drop_out_by_the_time_the_message_starts(self):
        filler = make("Filler", priority=9, track="f1")
        spot = make("Acme", priority=1, caps={"per_hour": 1})
        played = [play(spot, NOW - 3590)]  # 10 seconds short of an hour before the break
        self.assertEqual(campaign_reasons(build([spot], played)), {"Acme": "hour_cap"})
        self.assertEqual(chosen(build([filler, spot], played)), ["Filler", "Acme"])  # 30 s later it is more than an hour ago

    def test_the_station_hourly_limit_counts_plays_planned_before_a_message_starts(self):
        first, second = make("First", priority=9, track="a"), make("Second", priority=8, track="b")
        other = make("Other", paused=True)
        planned = [play(other, NOW + 20)]
        plan = build([first, second], planned, max_per_hour=2)
        self.assertEqual(chosen(plan), ["First"])  # Second would start 30 s in, the third play of that hour
        self.assertEqual(campaign_reasons(plan)["Second"], "hour_limit_station")
        self.assertEqual(chosen(build([first, second], [], max_per_hour=2)), ["First", "Second"])

    def test_random_breaks_keep_every_limit_where_each_message_starts(self):
        rng = random.Random(4242)
        broken = []
        for n in range(700):
            campaigns, audio = [], {}
            for i in range(rng.randint(2, 10)):
                c = make(f"C{i}", priority=rng.randint(1, 10), category=rng.choice([None, "bikes", "coffee"]),
                         caps={"per_hour": rng.choice([None, 1, 2, 3])},
                         separation={"campaign_minutes": rng.choice([0, 1, 5, 10, 20]), "category_minutes": rng.choice([0, 1, 2, 5, 10])})
                campaigns.append(c)
                audio[c.creatives[0].track_id] = Audio(rng.choice([15.0, 30.0, 45.0, 60.0, 90.0]))
            history = [play(rng.choice(campaigns), NOW + rng.uniform(-2 * HOUR, 40 * MIN)) for _ in range(rng.randint(0, 12))]
            ctx = context(campaigns, history, audio=audio, max_per_break=rng.choice([3, 3, 4, 5, 8]), max_per_hour=rng.choice([8, 20]))
            plan = build_break(NOW, rng.choice([120, 300, 600]), ctx, str(n))
            broken += [(n, text) for text in start_complaints(plan, ctx)]
        self.assertEqual(broken[:5], [])


class StationLimitTests(unittest.TestCase):
    def test_the_most_spots_in_a_break(self):
        spots = [make(f"S{i}", priority=10 - i) for i in range(5)]
        plan = build(spots, max_per_break=2)
        self.assertEqual(chosen(plan), ["S0", "S1"])
        self.assertEqual({s.reason for s in plan.skipped}, {"break_full"})
        self.assertEqual(build(spots, max_per_break=0).items, ())

    def test_the_most_spots_in_an_hour_counts_every_campaign(self):
        spots = [make(f"S{i}", priority=10 - i) for i in range(4)]
        other = make("Other", paused=True)
        history = [play(other, NOW - 10 * MIN), play(other, NOW - 20 * MIN), play(other, NOW - 3601)]
        plan = build(spots, history, max_per_hour=4, max_per_break=10)
        self.assertEqual(chosen(plan), ["S0", "S1"])  # two earlier plays in the hour, so two more fit
        self.assertEqual({s.reason for s in plan.skipped if s.campaign in ("S2", "S3")}, {"hour_limit_station"})

    def test_cut_short_plays_count_against_the_station_hour(self):
        spot, other = make("Spot"), make("Other", paused=True)
        plan = build([spot], [play(other, NOW - 10 * MIN, interrupted=True)], max_per_hour=1)
        self.assertEqual(campaign_reasons(plan), {"Spot": "hour_limit_station"})


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.work = [{"start": "2026-01-01", "dayparts": [{"from": "07:00", "to": "19:00"}]}]

    def spot(self, **kw):
        kw.setdefault("flights", self.work)
        kw.setdefault("pacing", "even")
        return make("Acme", **kw)

    def test_an_even_daily_plan_is_spread_over_the_open_hours(self):
        # Six plays over twelve hours: the nth is allowed once (n-1)/6 of the window has passed.
        spot = self.spot(plan={"per_day": 6})
        for label, hour, minute, delivered, expected in (
                ("the start of the window", 7, 0, 0, None), ("just after the start, nothing yet", 7, 5, 0, None),
                ("an hour in, one is already ahead of pace", 8, 0, 1, "ahead_of_pace"),
                ("two hours in, the second is due", 9, 0, 1, None),
                ("two hours in, two is ahead", 9, 0, 2, "ahead_of_pace"),
                ("three hours and a bit, two is behind", 10, 30, 2, "ahead_of_pace"),  # expected 1.75
                ("noon, three played and the fourth is due", 13, 0, 3, None),  # exactly 3.0 are due
                ("noon, four played is ahead", 13, 0, 4, "ahead_of_pace"),
                ("noon, two played", 13, 0, 2, None),
                ("almost the end, five played", 18, 59, 5, None),
                ("the plan is complete", 18, 59, 6, "plan_met")):
            with self.subTest(case=label):
                at = utc(2026, 11, 5, hour, minute)
                history = [play(spot, utc(2026, 11, 5, 7, 0) + i * 60) for i in range(delivered)]
                plan = build([spot], history, now=at)
                self.assertEqual(chosen(plan) == ["Acme"], expected is None, plan.lines)
                if expected:
                    self.assertEqual(campaign_reasons(plan), {"Acme": expected})

    def test_as_soon_as_possible_ignores_the_pace(self):
        spot = self.spot(plan={"per_day": 6}, pacing="asap")
        history = [play(spot, utc(2026, 11, 5, 7, 0) + i * 60) for i in range(3)]
        self.assertEqual(chosen(build([spot], history, now=utc(2026, 11, 5, 8, 0))), ["Acme"])

    def test_a_total_is_spread_over_the_playable_days_and_catches_up(self):
        # Monday 2 to Friday 13 November, weekdays: ten days, three plays a day.
        spot = make("Acme", plan={"total": 30}, pacing="even",
                    flights=[{"start": "2026-11-02", "end": "2026-11-13", "days": "1,2,3,4,5"}])
        wednesday_noon = utc(2026, 11, 4, 12, 0)  # two whole days and half of the third have gone: 7.5 expected
        for delivered, expected in ((7, ["Acme"]), (8, []), (0, ["Acme"])):
            with self.subTest(delivered=delivered):
                plan = build([spot], now=wednesday_noon, delivered_totals={spot.id: delivered})
                self.assertEqual(chosen(plan), expected)
                if not expected:
                    self.assertEqual(campaign_reasons(plan), {"Acme": "ahead_of_pace"})

    def test_without_a_plan_an_even_pace_spreads_the_daily_limit(self):
        spot = self.spot(caps={"per_day": 12})
        history = [play(spot, utc(2026, 11, 5, 7, 0)), play(spot, utc(2026, 11, 5, 7, 30))]
        self.assertEqual(campaign_reasons(build([spot], history, now=utc(2026, 11, 5, 8, 0))), {"Acme": "ahead_of_pace"})  # 1.0 due
        self.assertEqual(chosen(build([spot], history, now=utc(2026, 11, 5, 9, 0))), ["Acme"])  # 2.0 due
        no_pace = self.spot(caps={"per_hour": 5})
        self.assertEqual(chosen(build([no_pace], [play(no_pace, NOW - 1)] * 1, now=utc(2026, 11, 5, 7, 0))), ["Acme"])


class RankingTests(unittest.TestCase):
    def test_higher_priority_goes_first_and_takes_the_room(self):
        low, high = make("Low", priority=3), make("High", priority=9)
        plan = build([low, high], budget=100)
        self.assertEqual(chosen(plan), ["High", "Low"])
        plan = build([low, high], budget=40)
        self.assertEqual(chosen(plan), ["High"])
        self.assertEqual(campaign_reasons(plan), {"Low": "no_room"})

    def test_the_spot_furthest_behind_its_pace_goes_first(self):
        # Half the day has gone, so four of eight are due. "Level" has had its four, the last one 90 minutes ago.
        # "Behind" has had one, ten minutes ago. Waiting time alone would put Level first; being behind puts Behind first.
        behind = make("Behind", plan={"per_day": 8}, priority=5, pacing="even")
        level = make("Level", plan={"per_day": 8}, priority=5, pacing="even")
        at = utc(2026, 11, 5, 12, 0)
        history = [play(level, at - t) for t in (6 * HOUR, 5 * HOUR, 4 * HOUR, 90 * MIN)] + [play(behind, at - 10 * MIN)]
        self.assertEqual(max(p.at for p in history if p.campaign_id == level.id), at - 90 * MIN)
        for seed in range(12):
            self.assertEqual(chosen(build([level, behind], history, now=at, max_per_break=1, seed=str(seed))), ["Behind"])

    def test_the_spot_that_has_waited_longest_goes_first(self):
        a, b = make("A"), make("B")
        history = [play(a, NOW - 10 * MIN), play(b, NOW - 2 * HOUR)]
        self.assertEqual(chosen(build([a, b], history, max_per_break=1)), ["B"])
        history = [play(a, NOW - 3 * HOUR), play(b, NOW - 10 * MIN)]
        self.assertEqual(chosen(build([a, b], history, max_per_break=1)), ["A"])
        self.assertEqual(chosen(build([a, b], [play(a, NOW - 3 * HOUR)], max_per_break=1)), ["B"])  # never played goes first

    def test_with_asap_the_larger_backlog_goes_first(self):
        small = make("Small", plan={"per_day": 2}, pacing="asap")
        large = make("Large", plan={"per_day": 9}, pacing="asap")
        for seed in range(20):  # not a matter of the seed
            self.assertEqual(chosen(build([small, large], max_per_break=1, seed=str(seed))), ["Large"])
        done_some = [play(large, NOW - 5 * HOUR - i * HOUR) for i in range(8)]  # eight of nine delivered: the backlog is now one
        for seed in range(20):
            self.assertEqual(chosen(build([small, large], done_some, max_per_break=1, seed=str(seed))), ["Small"])

    def test_the_seed_only_breaks_exact_ties(self):
        spots = [make(f"Tie{i}") for i in range(6)]
        winners = {chosen(build(spots, seed=str(s), max_per_break=1))[0] for s in range(40)}
        self.assertGreater(len(winners), 1, "different seeds should be able to pick different spots")
        first = build(spots, seed="same", max_per_break=1)
        self.assertEqual(first, build(spots, seed="same", max_per_break=1))
        ranked = [make("Strong", priority=7)] + spots
        self.assertEqual({chosen(build(ranked, seed=str(s), max_per_break=1))[0] for s in range(20)}, {"Strong"})

    def test_a_campaign_rotates_through_its_messages(self):
        spot = make("Acme", creatives=[{"kind": "recorded", "track_id": f"t{i}", "title": f"Version {i}"} for i in range(3)])
        first, second, third = spot.creatives
        audio = {x.track_id: Audio(20.0) for x in spot.creatives}
        order = []
        history = []
        at = NOW
        for _ in range(4):
            plan = build([spot], history, now=at, audio=audio)
            order.append(plan.items[0].creative_id)
            history.append(play(spot, at, creative=plan.items[0].creative_id))
            at += 3 * HOUR
        self.assertEqual(len(set(order[:3])), 3, "every message plays once before any repeats")
        self.assertEqual(order[3], order[0])


class BudgetTests(unittest.TestCase):
    def test_an_exact_fit_is_allowed_and_a_hair_too_long_is_not(self):
        spot = make("Acme")
        self.assertEqual(chosen(build([spot], budget=30.0)), ["Acme"])
        plan = build([spot], budget=29.999)
        self.assertEqual((chosen(plan), campaign_reasons(plan)), ([], {"Acme": "no_room"}))
        self.assertEqual(build([spot], budget=0).items, ())
        self.assertEqual(build([spot], budget=-5).items, ())

    def test_a_spot_that_does_not_fit_is_passed_over_for_one_that_does(self):
        long_one = make("Long", priority=9, track="long")
        short_one = make("Short", priority=1, track="short")
        audio = {"long": Audio(50.0), "short": Audio(10.0)}
        plan = build([long_one, short_one], budget=40, audio=audio)
        self.assertEqual(chosen(plan), ["Short"])
        self.assertEqual(campaign_reasons(plan), {"Long": "no_room"})
        self.assertIn("50 seconds", [s for s in plan.skipped if s.campaign == "Long"][0].text)

    def test_a_shorter_message_of_the_same_spot_is_used_when_the_first_does_not_fit(self):
        spot = make("Acme", creatives=[{"kind": "recorded", "track_id": "slow", "title": "Long cut"},
                                       {"kind": "recorded", "track_id": "quick", "title": "Short cut"}])
        audio = {"slow": Audio(50.0), "quick": Audio(12.0)}
        for seed in range(10):
            plan = build([spot], budget=20, audio=audio, seed=str(seed))
            self.assertEqual([i.track_id for i in plan.items], ["quick"])

    def test_offsets_and_totals_add_up(self):
        spots = [make(f"S{i}", track=f"t{i}") for i in range(3)]
        audio = {f"t{i}": Audio(d) for i, d in enumerate((7.25, 11.5, 3.125))}
        plan = build(spots, audio=audio, budget=100)
        self.assertEqual(len(plan.items), 3)
        offsets = [i.offset for i in plan.items]
        self.assertEqual(offsets[0], 0.0)
        for earlier, later in zip(plan.items, plan.items[1:]):
            self.assertEqual(later.offset, earlier.offset + earlier.duration)
        self.assertEqual(plan.seconds_used, math.fsum(i.duration for i in plan.items))
        self.assertLessEqual(plan.seconds_used, plan.budget_seconds)

    def test_the_budget_is_capped_and_inputs_are_checked(self):
        from myradio.ads.model import AdsError
        spot = make("Acme")
        self.assertEqual(build([spot], budget=10 ** 9).budget_seconds, 600.0)
        for bad in (float("nan"), float("inf"), "30", None, True):
            with self.subTest(budget=bad), self.assertRaises(AdsError):
                build_break(NOW, bad, context([spot]), "s")
        for bad in (float("nan"), -1, 1e30, "now"):
            with self.subTest(now=bad), self.assertRaises(AdsError):
                build_break(bad, 30, context([spot]), "s")
        with self.assertRaises(AdsError):
            build_break(NOW, 30, {"campaigns": []}, "s")

    def test_estimated_lengths_are_labelled_and_only_listed_on_request(self):
        spot = make("Words", creatives=[{"kind": "script", "text": "Fixing bikes since 1998. Mention the station."}])
        audio = {}
        plan = build([spot], audio=audio)  # only finished audio by default
        self.assertEqual((plan.items, campaign_reasons(plan)), ((), {"Words": "audio_not_ready"}))
        plan = build([spot], audio=audio, ready_only=False)
        item = plan.items[0]
        self.assertEqual((item.estimated, item.ready, item.track_id), (True, False, None))
        self.assertGreaterEqual(item.duration, 3.0)
        self.assertIn("estimated", plan.lines[1])
        self.assertEqual(build([spot], audio=audio, ready_only=False, budget=2).items, ())


class FixedCheckTests(unittest.TestCase):
    def test_each_fixed_check_leaves_a_spot_out_with_its_reason(self):
        rights = {"voice_rights": False}
        cases = [
            ("paused", make("Acme", paused=True), {}, "paused"),
            ("not started", make("Acme", flights=[{"start": "2026-12-01"}]), {}, "not_started"),
            ("ended", make("Acme", flights=[{"start": "2026-01-01", "end": "2026-02-01"}]), {}, "ended"),
            ("wrong day", make("Acme", flights=[{"start": "2026-01-01", "days": "1,2"}]), {}, "wrong_day"),
            ("outside hours", make("Acme", flights=[{"start": "2026-01-01", "dayparts": [{"from": "01:00", "to": "02:00"}]}]), {}, "outside_hours"),
            ("wrong show", make("Acme", flights=[{"start": "2026-01-01", "show_ids": ["morning"]}]), {"show_id": "late"}, "wrong_show"),
            ("paid use not confirmed", make("Acme"), {"commercial_use": False}, "commercial_ack_needed"),
            ("voice rights not confirmed", make("Acme", creatives=[{"kind": "script", "text": "Hi there."}]), rights, "voice_rights_needed"),
            ("recording missing", make("Acme"), {"audio": {}}, "audio_missing"),
            ("recording unavailable", make("Acme"), {"audio": {"track-acme": Audio(30.0, available=False)}}, "audio_unavailable"),
            ("recording has no length", make("Acme"), {"audio": {"track-acme": Audio(0.0)}}, "audio_unavailable"),
            ("recording length is not a number", make("Acme"), {"audio": {"track-acme": Audio(float("nan"))}}, "audio_unavailable"),
            ("voice recording not made", make("Acme", creatives=[{"kind": "script", "text": "Hi there."}]), {"audio": {}}, "audio_not_ready"),
        ]
        for label, campaign, kw, reason in cases:
            with self.subTest(case=label):
                plan = build([campaign], **kw)
                self.assertEqual(plan.items, ())
                self.assertEqual(campaign_reasons(plan), {"Acme": reason})
                self.assertEqual([s.structural for s in plan.skipped], [reason in STRUCTURAL])

    def test_house_spots_do_not_need_the_paid_use_confirmation(self):
        promo = make("Promo", house=True)
        self.assertEqual(chosen(build([promo], commercial_use=False)), ["Promo"])

    def test_a_blocked_message_is_listed_but_the_spot_still_plays_its_other_message(self):
        spot = make("Acme", creatives=[{"kind": "script", "text": "Written copy."}, {"kind": "recorded", "track_id": "rec", "title": "Recorded"}])
        plan = build([spot], voice_rights=False, audio={"rec": Audio(15.0)})
        self.assertEqual([i.track_id for i in plan.items], ["rec"])
        blocked = [s for s in plan.skipped if s.creative_id]
        self.assertEqual([(s.reason, s.creative_id) for s in blocked], [("voice_rights_needed", spot.creatives[0].id)])

    def test_when_every_message_is_blocked_the_spot_gets_one_reason(self):
        spot = make("Acme", creatives=[{"kind": "recorded", "track_id": "a"}, {"kind": "recorded", "track_id": "b"}])
        plan = build([spot], audio={})
        self.assertEqual([(s.campaign, s.creative_id, s.reason) for s in plan.skipped], [("Acme", None, "audio_missing")])


class HouseFallbackTests(unittest.TestCase):
    def test_a_house_spot_fills_a_break_no_sponsor_can_use(self):
        sponsor, promo = make("Sponsor", paused=True), make("Promo", house=True, priority=1)
        plan = build([sponsor, promo])
        self.assertEqual(chosen(plan), ["Promo"])
        self.assertTrue(plan.items[0].house)
        self.assertIn("no sponsor message could play", plan.items[0].why)

    def test_a_house_spot_waits_while_a_sponsor_plays(self):
        sponsor, promo = make("Sponsor"), make("Promo", house=True)
        plan = build([sponsor, promo])
        self.assertEqual(chosen(plan), ["Sponsor"])
        self.assertEqual(campaign_reasons(plan), {"Promo": "house_standby"})
        self.assertTrue(plan.skipped[0].structural)

    def test_a_house_spot_can_top_up_a_break_when_asked(self):
        sponsor, promo = make("Sponsor"), make("Promo", house=True)
        plan = build([sponsor, promo], fill_with_house=True)
        self.assertEqual(chosen(plan), ["Sponsor", "Promo"])
        self.assertNotIn("no sponsor message", plan.items[1].why)

    def test_a_house_spot_covers_a_sponsor_that_does_not_fit(self):
        sponsor, promo = make("Sponsor", track="big"), make("Promo", house=True, track="small")
        plan = build([sponsor, promo], budget=20, audio={"big": Audio(45.0), "small": Audio(15.0)})
        self.assertEqual(chosen(plan), ["Promo"])
        self.assertEqual(campaign_reasons(plan), {"Sponsor": "no_room"})

    def test_house_spots_obey_the_same_limits(self):
        promo = make("Promo", house=True, caps={"per_hour": 1}, separation={"campaign_minutes": 30})
        self.assertEqual(chosen(build([promo], [play(promo, NOW - 61 * MIN)])), ["Promo"])
        self.assertEqual(campaign_reasons(build([promo], [play(promo, NOW - 20 * MIN)])), {"Promo": "hour_cap"})
        self.assertEqual(build([promo], [play(promo, NOW - 20 * MIN)]).items, ())
        gap_only = make("Promo", house=True, separation={"campaign_minutes": 30})
        self.assertEqual(campaign_reasons(build([gap_only], [play(gap_only, NOW - 20 * MIN)])), {"Promo": "too_soon_campaign"})

    def test_house_spots_count_toward_the_station_hour(self):
        promo, sponsor = make("Promo", house=True), make("Sponsor")
        plan = build([sponsor], [play(promo, NOW - 5 * MIN)], max_per_hour=1)
        self.assertEqual(campaign_reasons(plan), {"Sponsor": "hour_limit_station"})

    def test_a_house_spot_with_a_spoken_message_needs_voice_rights(self):
        promo = make("Promo", house=True, creatives=[{"kind": "script", "text": "Tell a friend."}])
        plan = build([promo], voice_rights=False, audio={})
        self.assertEqual(campaign_reasons(plan), {"Promo": "voice_rights_needed"})


class OddInputTests(unittest.TestCase):
    def test_house_spots_are_ranked_among_themselves_by_priority(self):
        low, high = make("Low promo", house=True, priority=2), make("High promo", house=True, priority=8)
        self.assertEqual(chosen(build([low, high], max_per_break=1)), ["High promo"])
        self.assertEqual(chosen(build([low, high], max_per_break=2)), ["High promo", "Low promo"])

    def test_totals_that_are_not_numbers_count_as_nothing_delivered(self):
        spot = make("Acme", plan={"total": 3})
        for odd in ("x", None, [], {}, -4, 2.9):
            with self.subTest(total=odd):
                plan = build([spot], delivered_totals={spot.id: odd})
                self.assertEqual(chosen(plan), ["Acme"])
        self.assertEqual(chosen(build([spot], delivered_totals={spot.id: 3.0})), [])

    def test_a_show_id_must_be_text(self):
        from myradio.ads.model import AdsError
        with self.assertRaises(AdsError):
            build_break(NOW, 30, context([make("Acme")], show_id=5), "s")

    def test_a_zone_the_computer_no_longer_knows_leaves_the_spot_out_plainly(self):
        import dataclasses
        spot = dataclasses.replace(make("Acme"), timezone="Mars/Base")
        plan = build([spot, make("Fine")])
        self.assertEqual(chosen(plan), ["Fine"])
        self.assertEqual(campaign_reasons(plan), {"Acme": "timezone_unknown"})
        self.assertIn("timezone this computer doesn't know", plan.skipped[0].text)
        self.assertFalse(plan.skipped[0].structural)  # something to fix, so it is counted as held back

    def test_a_spot_with_no_flights_at_all_is_simply_over(self):
        import dataclasses
        spot = dataclasses.replace(make("Acme"), flights=())
        plan = build([spot])
        self.assertEqual(campaign_reasons(plan), {"Acme": "ended"})
        self.assertEqual(plan.skipped[0].text, "Acme ended on an earlier date.")

    def test_a_campaign_listed_twice_is_still_chosen_once(self):
        spot = make("Acme")
        plan = build([spot, spot], max_per_break=3)
        self.assertEqual(len(plan.items), 1)

    def test_empty_stations_and_empty_histories_work(self):
        self.assertEqual(build([]).items, ())
        self.assertEqual(build([], [play(make("Gone"), NOW - 5)]).items, ())
        self.assertEqual(build([make("Acme")], []).items[0].campaign, "Acme")


class DeterminismAndPurityTests(unittest.TestCase):
    def scenario(self):
        spots = [make(f"S{i}", priority=1 + i % 3, category="bikes" if i % 2 else None, separation={"category_minutes": 10},
                      caps={"per_hour": 2}) for i in range(8)]
        history = [play(spots[i % 8], NOW - (i + 1) * 4 * MIN) for i in range(10)]
        return spots, history

    def test_the_same_inputs_and_seed_always_give_the_same_break(self):
        spots, history = self.scenario()
        first = build(spots, history, max_per_break=4, seed="alpha")
        for _ in range(5):
            self.assertEqual(build(spots, history, max_per_break=4, seed="alpha"), first)
        self.assertEqual(first.to_dict(), build(spots, history, max_per_break=4, seed="alpha").to_dict())

    def test_the_order_the_inputs_arrive_in_makes_no_difference(self):
        spots, history = self.scenario()
        reference = build(spots, history, max_per_break=4, seed="beta")
        rng = random.Random(7)
        for _ in range(10):
            shuffled_spots, shuffled_history = list(spots), list(history)
            rng.shuffle(shuffled_spots)
            rng.shuffle(shuffled_history)
            self.assertEqual(build(shuffled_spots, shuffled_history, max_per_break=4, seed="beta"), reference)

    def test_the_break_does_not_read_the_clock_or_global_random_numbers(self):
        spots, history = self.scenario()
        expected = build(spots, history, max_per_break=4, seed="gamma")
        with mock.patch.object(time, "time", side_effect=AssertionError("clock read")), \
                mock.patch.object(time, "monotonic", side_effect=AssertionError("clock read")), \
                mock.patch.object(random, "random", side_effect=AssertionError("random read")), \
                mock.patch.object(random, "choice", side_effect=AssertionError("random read")), \
                mock.patch.object(random, "shuffle", side_effect=AssertionError("random read")):
            self.assertEqual(build(spots, history, max_per_break=4, seed="gamma"), expected)

    def test_the_inputs_are_left_alone(self):
        spots, history = self.scenario()
        context_ = context(spots, history, max_per_break=4)
        before = (list(context_.campaigns), list(context_.history), dict(context_.audio))
        build_break(NOW, 90, context_, "delta")
        self.assertEqual((list(context_.campaigns), list(context_.history), dict(context_.audio)), before)

    def test_the_seed_cannot_change_a_choice_the_rules_already_decide(self):
        spots, history = self.scenario()  # every spot differs in priority or in when it last played
        results = {tuple(chosen(build(spots, history, max_per_break=2, seed=str(s)))) for s in range(30)}
        self.assertEqual(len(results), 1)


class ExplanationTests(unittest.TestCase):
    def test_every_spot_is_either_in_the_break_or_explained(self):
        spots = [make("Plays"), make("Paused", paused=True), make("Capped", caps={"per_hour": 1}), make("Late", flights=[{"start": "2027-01-01"}]),
                 make("Promo", house=True), make("Gap", separation={"campaign_minutes": 60})]
        history = [play(spots[2], NOW - 10 * MIN), play(spots[5], NOW - 10 * MIN)]
        plan = build(spots, history)
        accounted = {i.campaign for i in plan.items} | {s.campaign for s in plan.skipped}
        self.assertEqual(accounted, {c.name for c in spots})
        self.assertEqual(campaign_reasons(plan), {"Paused": "paused", "Capped": "hour_cap", "Late": "not_started",
                                                  "Promo": "house_standby", "Gap": "too_soon_campaign"})

    def test_explanations_are_complete_plain_sentences(self):
        spots = [make("Corner Bike Co.", paused=True), make("Capped", caps={"per_hour": 1}, plan={"per_day": 1}, pacing="asap"),
                 make("Gappy", separation={"campaign_minutes": 60}), make("Crowded", category="x", separation={"category_minutes": 30}),
                 make("Crowd 2", category="x", separation={"category_minutes": 30}, priority=1),
                 make("Nowhere", track="gone"), make("Wordy", creatives=[{"kind": "script", "text": "Hello."}]),
                 make("Big", track="big", priority=1)]
        history = [play(spots[1], NOW - 5 * MIN), play(spots[2], NOW - 5 * MIN)]
        audio = {"track-capped": Audio(30.0), "track-gappy": Audio(30.0), "track-crowded": Audio(30.0),
                 "track-crowd-2": Audio(30.0), "track-corner-bike-co": Audio(30.0), "big": Audio(500.0)}
        plan = build(spots, history, audio=audio, max_per_break=5, budget=200)
        self.assertTrue(plan.skipped)
        for skip in plan.skipped:
            with self.subTest(reason=skip.reason):
                self.assertTrue(skip.text.endswith("."), skip.text)
                self.assertGreater(len(skip.text), 15)
                self.assertIn(skip.campaign.split(" (")[0].split(".")[0][:5], skip.text) if skip.reason != "break_full" else None
                for word in JARGON:
                    self.assertNotIn(word, skip.text)
        for item in plan.items:
            self.assertTrue(item.why)
        for line in plan.lines:
            for word in ("None", "{", "}", "Traceback"):
                self.assertNotIn(word, line)

    def test_the_preview_reads_well_for_each_kind_of_break(self):
        plan = build([make("Corner Bike Co."), make("Harbor Coffee", track="coffee")], audio={"track-corner-bike-co": Audio(30.0), "coffee": Audio(25.0)})
        self.assertEqual(plan.lines[0], "The next break would play 2 spots (55 seconds).")
        self.assertTrue(plan.lines[1].startswith("1. "))
        empty = build([])
        self.assertEqual(empty.lines, ["The next break would have no spots.", "You have no spots yet. Add one to fill your breaks."])
        quiet = build([make("Acme", paused=True)])
        self.assertEqual(quiet.lines[:2], ["The next break would have no spots.", "No spot is scheduled for this time."])
        self.assertEqual(quiet.lines[-1], "1 other spot isn't scheduled for this time.")
        capped = make("Capped", caps={"per_hour": 1})
        why = build([capped], [play(capped, NOW - 60)])
        self.assertEqual(why.lines[1], "Why:")
        self.assertIn("already played 1 time in the last hour (limit 1)", why.lines[2])

    def test_a_plan_also_reads_as_the_ordered_list_of_messages(self):
        spots = [make("High", priority=9), make("Low", priority=1)]
        plan = build(spots)
        self.assertEqual([item.campaign for item in plan], ["High", "Low"])
        self.assertEqual((len(plan), plan[0].campaign, plan[-1].campaign, plan.creatives), (2, "High", "Low", plan.items))
        self.assertEqual(len(build([])), 0)

    def test_a_plan_is_json_ready(self):
        spots = [make(f"S{i}") for i in range(3)] + [make("Paused", paused=True)]
        data = build(spots).to_dict()
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["skipped_total"], len(data["skipped"]))
        self.assertTrue(data["at"].endswith("+00:00"))
        self.assertEqual(set(data), {"at", "seed", "show_id", "budget_seconds", "seconds_used", "items", "skipped", "skipped_total", "lines"})
        self.assertEqual(set(data["items"][0]), {"position", "campaign_id", "campaign", "advertiser", "house", "creative_id", "title",
                                                 "kind", "track_id", "duration", "estimated", "ready", "offset", "why", "role"})
        self.assertEqual({i["role"] for i in data["items"]}, {"spot"})
        many = build([make(f"Paused {i}", paused=True) for i in range(80)]).to_dict(skipped_limit=10)
        self.assertEqual((len(many["skipped"]), many["skipped_total"]), (10, 80))


class InvariantSweepTests(unittest.TestCase):
    """Many random stations. An independent re-check of every limit against the raw numbers."""

    def scenario(self, rng):
        count = rng.randint(1, 8)
        campaigns, audio = [], {}
        for i in range(count):
            settings = {"priority": rng.randint(1, 10), "pacing": rng.choice(["even", "asap"]), "house": rng.random() < 0.25,
                        "category": rng.choice([None, "bikes", "coffee"]),
                        "caps": {k: rng.choice([None, rng.randint(1, 6)]) for k in ("per_hour", "per_day", "per_week")},
                        "separation": {"campaign_minutes": rng.choice([0, 5, 20, 45]), "category_minutes": rng.choice([0, 10, 30])},
                        "paused": rng.random() < 0.1}
            if rng.random() < 0.3:
                settings["plan"] = {"per_day": rng.randint(1, 6)}
            campaign = make(f"C{i}", **settings)
            campaigns.append(campaign)
            audio[campaign.creatives[0].track_id] = Audio(rng.choice([5.0, 12.5, 30.0, 47.25, 90.0]))
        history = []
        for _ in range(rng.randint(0, 25)):
            who = rng.choice(campaigns)
            at = NOW - rng.choice([rng.uniform(0, HOUR), rng.uniform(0, 6 * HOUR), rng.uniform(0, 4 * DAY)])
            if rng.random() < 0.05:
                at = NOW + rng.uniform(0, 0.2 * DAY)  # a play already planned for later
            history.append(play(who, at, interrupted=rng.random() < 0.15))
        totals = {c.id: rng.randint(0, 20) for c in campaigns}
        return campaigns, history, audio, totals

    def check(self, plan, ctx, budget):
        now = plan.at
        by_id = {c.id: c for c in ctx.campaigns}
        day_low, day_high = utc(2026, 11, 5), utc(2026, 11, 6)
        week_low, week_high = utc(2026, 11, 2), utc(2026, 11, 9)
        self.assertLessEqual(math.fsum(i.duration for i in plan.items), budget)
        self.assertLessEqual(len(plan.items), ctx.max_per_break)
        self.assertEqual(len({i.campaign_id for i in plan.items}), len(plan.items))
        self.assertEqual({s.campaign_id for s in plan.skipped if s.creative_id is None} | {i.campaign_id for i in plan.items},
                         set(by_id))
        sponsors = [i for i in plan.items if not i.house]
        if not ctx.fill_with_house and any(i.house for i in plan.items):
            self.assertEqual(sponsors, [])
        self.assertEqual(start_complaints(plan, ctx), [])  # hourly limits, gaps and similar spots, judged where each message starts
        for index, item in enumerate(plan.items):
            c = by_id[item.campaign_id]
            self.assertFalse(c.paused)
            mine = [p for p in ctx.history if p.campaign_id == c.id]
            starts = [p.at for p in mine]
            if c.caps.per_day is not None:
                self.assertLess(sum(1 for t in starts if day_low <= t < day_high), c.caps.per_day)
            if c.caps.per_week is not None:
                self.assertLess(sum(1 for t in starts if week_low <= t < week_high), c.caps.per_week)
            if c.plan.per_day is not None:
                self.assertLess(sum(1 for p in mine if not p.interrupted and day_low <= p.at < day_high), c.plan.per_day)
            if c.plan.total is not None:
                self.assertLess(ctx.delivered_totals.get(c.id, 0), c.plan.total)
            self.assertEqual(item.offset, math.fsum(i.duration for i in plan.items[:index]))
        # A reason that names a limit must be true. A spot is weighed when the messages ahead of it are some leading
        # part of the final break, so its limit must have been true at one of those starting moments.
        moments = [now + math.fsum(i.duration for i in plan.items[:k]) for k in range(len(plan.items) + 1)]
        for skip in plan.skipped:
            c = by_id[skip.campaign_id]
            starts = [p.at for p in ctx.history if p.campaign_id == c.id]
            if skip.reason == "hour_cap":
                self.assertTrue(any(sum(1 for t in starts if m - 3600 < t <= m) >= c.caps.per_hour for m in moments))
            elif skip.reason == "day_cap":
                self.assertGreaterEqual(sum(1 for t in starts if day_low <= t < day_high), c.caps.per_day)
            elif skip.reason == "week_cap":
                self.assertGreaterEqual(sum(1 for t in starts if week_low <= t < week_high), c.caps.per_week)
            elif skip.reason == "too_soon_campaign":
                self.assertTrue(any(abs(t - m) < c.separation.campaign_minutes * 60 for t in starts for m in moments))
            elif skip.reason == "paused":
                self.assertTrue(c.paused)

    def test_three_hundred_random_stations(self):
        rng = random.Random(20261105)
        for n in range(300):
            campaigns, history, audio, totals = self.scenario(rng)
            budget = rng.choice([0, 10, 30, 60, 120, 300])
            ctx = context(campaigns, history, audio=audio, delivered_totals=totals, max_per_break=rng.randint(1, 5),
                          max_per_hour=rng.randint(1, 10), fill_with_house=rng.random() < 0.3)
            plan = build_break(NOW, budget, ctx, str(n))
            with self.subTest(scenario=n):
                self.check(plan, ctx, budget)
                self.assertEqual(plan, build_break(NOW, budget, ctx, str(n)))
                self.assertEqual(plan.to_dict(), json.loads(json.dumps(plan.to_dict())))


if __name__ == "__main__":
    unittest.main()
