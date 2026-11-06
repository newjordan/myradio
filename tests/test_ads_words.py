"""Plain-language wording: times, schedules and the sentences that explain a break."""
import unittest

from myradio.ads import words
from myradio.ads.engine import build_break

try:
    from test_ads_support import MIN, NOW, context, make, play
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import MIN, NOW, context, make, play


class ClockAndScheduleTests(unittest.TestCase):
    def test_times_of_day(self):
        for seconds, text in ((0, "midnight"), (86400, "midnight"), (25200, "7 am"), (43200, "12 pm"), (45900, "12:45 pm"), (3600, "1 am"),
                              (82800, "11 pm"), (30600, "8:30 am"), (47400, "1:10 pm")):
            with self.subTest(seconds=seconds):
                self.assertEqual(words.clock_text(seconds), text)
        self.assertEqual(words.windows_text(((25200, 43200), (46800, 68400))), "7 am – 12 pm, 1 pm – 7 pm")
        self.assertEqual(words.windows_text(()), "all day")

    def test_days(self):
        for days, text in (((1, 2, 3, 4, 5, 6, 7), "Every day"), ((1, 2, 3, 4, 5), "Weekdays"), ((6, 7), "Weekends"), ((1, 3, 5), "Mon, Wed, Fri"),
                           ((5, 1, 3), "Mon, Wed, Fri"), ((2,), "Tue")):
            with self.subTest(days=days):
                self.assertEqual(words.days_text(days), text)

    def test_schedules_read_like_the_screen_in_the_design(self):
        spot = make("Acme", flights=[{"start": "2026-11-12", "end": "2026-12-31", "days": "1,2,3,4,5", "dayparts": [{"from": "07:00", "to": "19:00"}]}])
        self.assertEqual(words.schedule_text(spot), "Weekdays, 7 am – 7 pm, Nov 12 to Dec 31")
        self.assertEqual(words.schedule_text(make("Open", flights=[{"start": "2026-11-12"}])), "Every day, from Nov 12")
        show = make("Show", flights=[{"start": "2026-11-12", "end": "2026-11-20", "show_ids": ["morning"]}])
        self.assertEqual(words.schedule_text(show), "Every day, Nov 12 to Nov 20, some shows only")
        several = make("Several", flights=[{"start": "2026-11-01", "end": "2026-11-10"}, {"start": "2026-12-01", "end": "2026-12-10"}])
        self.assertEqual(words.schedule_text(several), "Every day, Nov 1 to Nov 10 (and 1 more run)")

    def test_lengths(self):
        for seconds, text in ((1, "1 second"), (30, "30 seconds"), (59.6, "1 minute"), (60, "1 minute"), (65, "1 minute 5 seconds"), (121, "2 minutes 1 second"),
                              (29.4, "29 seconds")):
            with self.subTest(seconds=seconds):
                self.assertEqual(words.seconds_text(seconds), text)
        self.assertEqual((words.minutes_text(60), words.minutes_text(90), words.minutes_text(1500), words.minutes_text(-600)),
                         ("1 minute", "2 minutes", "25 minutes", "10 minutes"))


class SentenceTests(unittest.TestCase):
    def test_every_reason_the_engine_can_give_has_a_sentence_and_a_report_label(self):
        reasons = set(words.REASON_LABELS) | words.STRUCTURAL
        for reason in sorted(reasons):
            with self.subTest(reason=reason):
                text = words.skip_text(reason, "Acme", {})
                self.assertTrue(text.endswith("."), text)
                if reason not in ("break_full", "hour_limit_station"):
                    self.assertIn("Acme", text)
                self.assertNotIn("was left out", text)
        self.assertEqual(words.skip_text("something_new", "Acme", {}), "Acme was left out.")  # unknown reasons still read as a sentence
        self.assertEqual(set(words.REPORTED_REASONS), set(words.REASON_LABELS))
        self.assertTrue(words.STRUCTURAL.isdisjoint(words.REASON_LABELS))

    def test_sentences_with_numbers(self):
        cases = [
            ("hour_cap", {"played": 2, "limit": 2}, "Acme already played 2 times in the last hour (limit 2)."),
            ("hour_cap", {"played": 1, "limit": 1}, "Acme already played 1 time in the last hour (limit 1)."),
            ("day_cap", {"played": 10, "limit": 10}, "Acme already played 10 times today (limit 10)."),
            ("week_cap", {"played": 50, "limit": 50}, "Acme already played 50 times this week (limit 50)."),
            ("plan_met", {"scope": "day", "n": 6}, "Acme has already played all 6 times planned for today."),
            ("plan_met", {"scope": "flight", "n": 60}, "Acme has already played all 60 times it was booked for."),
            ("too_soon_campaign", {"seconds": 300, "ahead": False, "gap": 20}, "Acme played 5 minutes ago. It waits at least 20 minutes between plays."),
            ("too_soon_campaign", {"seconds": 600, "ahead": True, "gap": 1}, "Acme is planned 10 minutes from now. It waits at least 1 minute between plays."),
            ("too_soon_category", {"seconds": 300, "ahead": False, "gap": 10, "other": "Bike Barn", "category": "bikes"},
             "A similar spot (Bike Barn) played 5 minutes ago. Acme waits at least 10 minutes from the same kind of spot."),
            ("no_room", {"need": 45, "left": 20}, "Acme needs 45 seconds but only 20 seconds are left in this break."),
            ("break_full", {"n": 3}, "The break already has the most spots allowed (3)."),
            ("hour_limit_station", {"played": 8, "limit": 8}, "The station already played 8 spots in the last hour (limit 8)."),
            ("not_started", {"date": "2026-12-01"}, "Acme starts on 2026-12-01."),
            ("ended", {"date": "2026-02-01"}, "Acme ended on 2026-02-01."),
            ("wrong_day", {"weekday": 6}, "Acme doesn't play on Saturdays."),
            ("outside_hours", {"windows": [(25200, 68400)]}, "Acme only plays 7 am – 7 pm."),
        ]
        for reason, detail, expected in cases:
            with self.subTest(reason=reason, detail=detail):
                self.assertEqual(words.skip_text(reason, "Acme", detail), expected)

    def test_a_play_that_is_only_moments_away_is_read_in_seconds_never_as_zero_minutes(self):
        wait = "It waits at least 20 minutes between plays."
        cases = [
            ("too_soon_campaign", {"seconds": 20, "ahead": False, "gap": 20}, f"Acme played 20 seconds ago. {wait}"),
            ("too_soon_campaign", {"seconds": 20, "ahead": True, "gap": 20}, f"Acme is planned 20 seconds from now. {wait}"),
            ("too_soon_campaign", {"seconds": 1, "ahead": False, "gap": 20}, f"Acme played 1 second ago. {wait}"),
            ("too_soon_campaign", {"seconds": 0, "ahead": True, "gap": 20}, f"Acme is planned right now. {wait}"),
            ("too_soon_campaign", {"seconds": 0.2, "ahead": False, "gap": 20}, f"Acme played just now. {wait}"),
            ("too_soon_campaign", {"seconds": 90, "ahead": False, "gap": 20}, f"Acme played 1 minute 30 seconds ago. {wait}"),
            ("too_soon_campaign", {"seconds": 119, "ahead": True, "gap": 20}, f"Acme is planned 1 minute 59 seconds from now. {wait}"),
            ("too_soon_campaign", {"seconds": 120, "ahead": False, "gap": 20}, f"Acme played 2 minutes ago. {wait}"),
            ("too_soon_category", {"seconds": 30, "ahead": False, "gap": 1, "other": "Bike Barn", "category": "bikes"},
             "A similar spot (Bike Barn) played 30 seconds ago. Acme waits at least 1 minute from the same kind of spot."),
            ("too_soon_category", {"seconds": 45, "ahead": True, "gap": 5, "other": "Bike Barn", "category": "bikes"},
             "A similar spot (Bike Barn) is planned 45 seconds from now. Acme waits at least 5 minutes from the same kind of spot."),
        ]
        for reason, detail, expected in cases:
            with self.subTest(reason=reason, detail=detail):
                self.assertEqual(words.skip_text(reason, "Acme", detail), expected)
        for seconds in range(0, 400, 7):
            for ahead in (False, True):
                self.assertNotIn("0 minutes", words.distance_text(seconds, ahead))

    def test_counts_of_one_read_naturally(self):
        cases = [
            ("plan_met", {"scope": "day", "n": 1}, "Acme has already played the 1 time planned for today."),
            ("plan_met", {"scope": "flight", "n": 1}, "Acme has already played the 1 time it was booked for."),
            ("plan_met", {"scope": "day", "n": 2}, "Acme has already played all 2 times planned for today."),
            ("plan_met", {"scope": "flight"}, "Acme has already played all the times it was booked for."),
            ("hour_limit_station", {"played": 1, "limit": 1}, "The station already played 1 spot in the last hour (limit 1)."),
            ("hour_limit_station", {"played": 2, "limit": 2}, "The station already played 2 spots in the last hour (limit 2)."),
        ]
        for reason, detail, expected in cases:
            with self.subTest(reason=reason, detail=detail):
                self.assertEqual(words.skip_text(reason, "Acme", detail), expected)

    def test_a_long_list_of_reasons_is_shortened(self):
        spots = [make(f"Capped {i}", caps={"per_hour": 1}) for i in range(12)]
        history = [play(c, NOW - 5 * MIN) for c in spots]
        plan = build_break(NOW, 60, context(spots, history), "s")
        lines = plan.lines
        self.assertEqual(lines[0], "The next break would have no spots.")
        self.assertEqual(lines[1], "Why:")
        self.assertEqual(len([line for line in lines if line.startswith("- ") and "already played" in line]), 8)
        self.assertEqual(lines[-1], "- and 4 more.")


if __name__ == "__main__":
    unittest.main()
