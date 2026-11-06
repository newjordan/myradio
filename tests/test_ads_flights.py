"""Flights across daylight-saving changes and time zones, calendar buckets and even-pace arithmetic."""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from myradio.ads.flights import (active_days_before, active_days_total, campaign_state, day_bounds, day_fraction,
                                 expected_by_now, is_active_day, local_datetime, next_change, pace_target, week_bounds,
                                 week_start, window_state)

try:
    from test_ads_support import local, make, tz_available, utc
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import local, make, tz_available, utc

NY = "America/New_York"
needs_zones = unittest.skipUnless(all(tz_available(z) for z in (NY, "Asia/Tokyo", "America/Los_Angeles", "Asia/Kolkata",
                                                                "Australia/Lord_Howe", "Pacific/Auckland", "Europe/London")),
                                  "this computer has no timezone database")


def reason(campaign, at, show=None):
    state = window_state(campaign, at, show)
    return "open" if state.open else state.reason


@needs_zones
class FlightDatesAcrossDaylightSavingTests(unittest.TestCase):
    def test_a_flight_ends_at_local_midnight_of_its_last_date_not_at_a_fixed_utc_hour(self):
        # New York springs forward on 2026-03-08: local midnight is 05:00 UTC before it and 04:00 UTC after it.
        campaign = make(timezone=NY, flights=[{"start": "2026-03-01", "end": "2026-03-08"}])
        self.assertEqual(reason(campaign, utc(2026, 3, 9, 3, 59, 59)), "open")  # 23:59:59 EDT on the 8th
        self.assertEqual(reason(campaign, utc(2026, 3, 9, 4, 0, 0)), "ended")  # 00:00 EDT on the 9th
        # A fixed "05:00 UTC" boundary would have kept the flight open for another hour.
        self.assertEqual(reason(campaign, utc(2026, 3, 9, 4, 30, 0)), "ended")

    def test_a_flight_starts_at_local_midnight_before_the_change(self):
        campaign = make(timezone=NY, flights=[{"start": "2026-03-08"}])
        self.assertEqual(reason(campaign, utc(2026, 3, 8, 4, 59, 59)), "not_started")  # 23:59:59 EST on the 7th
        self.assertEqual(reason(campaign, utc(2026, 3, 8, 5, 0, 0)), "open")  # 00:00 EST on the 8th

    def test_the_fall_back_day_is_twenty_five_hours_long_and_the_flight_covers_all_of_it(self):
        campaign = make(timezone=NY, flights=[{"start": "2026-11-01", "end": "2026-11-01"}])
        self.assertEqual(reason(campaign, utc(2026, 11, 1, 3, 59, 59)), "not_started")
        self.assertEqual(reason(campaign, utc(2026, 11, 1, 4, 0, 0)), "open")  # 00:00 EDT
        self.assertEqual(reason(campaign, utc(2026, 11, 2, 4, 59, 59)), "open")  # 23:59:59 EST, 25 hours later
        self.assertEqual(reason(campaign, utc(2026, 11, 2, 5, 0, 0)), "ended")

    def test_day_lengths(self):
        zone = ZoneInfo(NY)
        for day, hours in ((dt.date(2026, 3, 8), 23), (dt.date(2026, 11, 1), 25), (dt.date(2026, 6, 1), 24)):
            with self.subTest(day=day):
                low, high = day_bounds(day, zone)
                self.assertEqual((high - low) / 3600, hours)
        low, high = day_bounds(dt.date(2026, 10, 4), ZoneInfo("Australia/Lord_Howe"))  # a thirty-minute change
        self.assertEqual(high - low, 23.5 * 3600)

    def test_weeks_run_monday_to_monday_in_local_time(self):
        zone = ZoneInfo(NY)
        low, high = week_bounds(dt.date(2026, 3, 8), zone)  # Sunday, the spring-forward day
        self.assertEqual(week_start(dt.date(2026, 3, 8)), dt.date(2026, 3, 2))
        self.assertEqual((high - low) / 3600, 7 * 24 - 1)
        low, high = week_bounds(dt.date(2026, 11, 1), zone)
        self.assertEqual((high - low) / 3600, 7 * 24 + 1)

    def test_the_state_changes_exactly_at_the_flight_edges(self):
        campaign = make(timezone=NY, flights=[{"start": "2026-03-08", "end": "2026-03-08"}])
        before, start = utc(2026, 3, 8, 4, 59, 59), utc(2026, 3, 8, 5, 0, 0)
        self.assertEqual(campaign_state(campaign, before), "scheduled")
        self.assertEqual(campaign_state(campaign, start), "running")
        self.assertEqual(next_change(campaign, before), start)
        end = utc(2026, 3, 9, 4, 0, 0)  # local midnight after the 8th, an hour earlier in UTC than a fixed offset would say
        self.assertEqual(next_change(campaign, start), end)
        self.assertEqual(campaign_state(campaign, end - 1), "running")
        self.assertEqual(campaign_state(campaign, end), "ended")
        self.assertIsNone(next_change(campaign, end))

    def test_a_paused_spot_is_paused_whatever_its_dates(self):
        campaign = make(timezone=NY, paused=True)
        self.assertEqual(campaign_state(campaign, utc(2026, 11, 5, 15)), "paused")

    def test_a_gap_between_flights_counts_as_scheduled(self):
        campaign = make(flights=[{"start": "2026-11-01", "end": "2026-11-03"}, {"start": "2026-11-10"}])
        self.assertEqual(campaign_state(campaign, utc(2026, 11, 5, 12)), "scheduled")
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 12)), "not_started")
        self.assertEqual(window_state(campaign, utc(2026, 11, 5, 12)).detail, {"date": "2026-11-10"})


class CalendarTilingTests(unittest.TestCase):
    """Local days must tile time exactly, in every zone, however the clocks move (a midnight that does not exist,
    a thirty-minute change, a whole skipped day)."""

    ZONES = (("America/New_York", dt.date(2026, 1, 1), 365), ("Europe/London", dt.date(2026, 1, 1), 365), ("Australia/Lord_Howe", dt.date(2026, 1, 1), 365),
             ("America/Havana", dt.date(2026, 1, 1), 365), ("Asia/Kolkata", dt.date(2026, 1, 1), 365), ("Pacific/Auckland", dt.date(2026, 1, 1), 365),
             ("America/Sao_Paulo", dt.date(2018, 1, 1), 365), ("Africa/Cairo", dt.date(2026, 1, 1), 365), ("Pacific/Apia", dt.date(2011, 12, 20), 30))

    def test_days_follow_each_other_without_gaps_or_overlaps_and_every_instant_lands_on_its_own_date(self):
        import bisect
        checked = 0
        for name, first, days in self.ZONES:
            if not tz_available(name):
                continue
            zone = ZoneInfo(name)
            dates = [first + dt.timedelta(days=i) for i in range(days)]
            bounds = [day_bounds(d, zone) for d in dates]
            for (low, high), (next_low, _) in zip(bounds, bounds[1:]):
                self.assertEqual(high, next_low, name)  # no gap and no overlap between one day and the next
                self.assertLessEqual(low, high, name)
                self.assertLessEqual(high - low, 25 * 3600 + 1, name)
            starts = [b[0] for b in bounds]
            moment = bounds[1][0]
            end = bounds[-2][0]
            while moment < end:
                index = bisect.bisect_right(starts, moment) - 1
                self.assertEqual(dates[index], local_datetime(moment, zone).date(), (name, moment))
                moment += 900  # every quarter of an hour, which crosses every change in these years
                checked += 1
        self.assertGreater(checked, 20000)

    def test_a_week_is_seven_local_days(self):
        for name in ("America/New_York", "Australia/Lord_Howe", "America/Havana"):
            if not tz_available(name):
                continue
            zone = ZoneInfo(name)
            for offset in range(0, 365, 7):
                monday = dt.date(2026, 1, 5) + dt.timedelta(days=offset)
                low, high = week_bounds(monday, zone)
                self.assertEqual((low, high), (day_bounds(monday, zone)[0], day_bounds(monday + dt.timedelta(days=6), zone)[1]), (name, monday))


@needs_zones
class DaypartsAcrossDaylightSavingTests(unittest.TestCase):
    def test_hours_follow_the_wall_clock_in_summer_and_winter(self):
        campaign = make(timezone=NY, flights=[{"dayparts": [{"from": "07:00", "to": "19:00"}], "start": "2026-01-01"}])
        self.assertEqual(reason(campaign, local(NY, 2026, 7, 1, 6, 59, 59)), "outside_hours")
        self.assertEqual(reason(campaign, local(NY, 2026, 7, 1, 7, 0)), "open")
        self.assertEqual(reason(campaign, local(NY, 2026, 7, 1, 18, 59, 59)), "open")
        self.assertEqual(reason(campaign, local(NY, 2026, 7, 1, 19, 0)), "outside_hours")
        # The same UTC hour is inside the window in July and outside it in December.
        self.assertEqual(reason(campaign, utc(2026, 7, 1, 11, 0)), "open")  # 07:00 EDT
        self.assertEqual(reason(campaign, utc(2026, 12, 1, 11, 0)), "outside_hours")  # 06:00 EST

    def test_the_repeated_hour_matches_its_window_twice(self):
        campaign = make(timezone=NY, flights=[{"start": "2026-01-01", "dayparts": [{"from": "01:00", "to": "02:00"}]}])
        self.assertEqual(reason(campaign, utc(2026, 11, 1, 4, 59, 59)), "outside_hours")  # 00:59:59 EDT
        self.assertEqual(reason(campaign, utc(2026, 11, 1, 5, 30)), "open")  # 01:30 EDT, first time round
        self.assertEqual(reason(campaign, utc(2026, 11, 1, 6, 30)), "open")  # 01:30 EST, second time round
        self.assertEqual(reason(campaign, utc(2026, 11, 1, 7, 0)), "outside_hours")  # 02:00 EST

    def test_the_skipped_hour_never_matches_and_a_window_across_it_starts_when_the_clock_does(self):
        gap = make(timezone=NY, flights=[{"start": "2026-01-01", "dayparts": [{"from": "02:00", "to": "03:00"}]}])
        minute = utc(2026, 3, 8, 0, 0)
        for _ in range(24 * 60):
            self.assertNotEqual(reason(gap, minute), "open", dt.datetime.fromtimestamp(minute, dt.timezone.utc).isoformat())
            minute += 60
        across = make(timezone=NY, flights=[{"start": "2026-01-01", "dayparts": [{"from": "02:30", "to": "04:00"}]}])
        self.assertEqual(reason(across, utc(2026, 3, 8, 6, 59, 59)), "outside_hours")  # 01:59:59 EST
        self.assertEqual(reason(across, utc(2026, 3, 8, 7, 0, 0)), "open")  # 03:00 EDT: the wall clock has passed 02:30
        self.assertEqual(reason(across, utc(2026, 3, 8, 7, 59, 59)), "open")
        self.assertEqual(reason(across, utc(2026, 3, 8, 8, 0, 0)), "outside_hours")  # 04:00 EDT


@needs_zones
class TimeZoneTests(unittest.TestCase):
    def test_the_same_instant_is_a_different_weekday_in_different_zones(self):
        at = utc(2026, 11, 6, 20, 0)  # Friday 20:00 UTC
        weekdays = {"days": "1,2,3,4,5", "start": "2026-01-01"}
        tokyo = make("Tokyo", timezone="Asia/Tokyo", flights=[weekdays])  # Saturday 05:00
        los_angeles = make("LA", timezone="America/Los_Angeles", flights=[weekdays])  # Friday 12:00
        self.assertEqual(reason(tokyo, at), "wrong_day")
        self.assertEqual(window_state(tokyo, at).detail, {"weekday": 6})
        self.assertEqual(reason(los_angeles, at), "open")

    def test_the_same_flight_dates_start_at_different_instants_in_different_zones(self):
        flight = [{"start": "2026-11-10"}]
        auckland = make("Auckland", timezone="Pacific/Auckland", flights=flight)
        london = make("London", timezone="Europe/London", flights=flight)
        # 2026-11-09 11:00 UTC is already the 10th in Auckland (UTC+13) and still the 9th in London.
        at = utc(2026, 11, 9, 11, 0)
        self.assertEqual(reason(auckland, at), "open")
        self.assertEqual(reason(london, at), "not_started")

    def test_half_hour_zones(self):
        campaign = make(timezone="Asia/Kolkata", flights=[{"start": "2026-01-01", "dayparts": [{"from": "07:00", "to": "09:00"}]}])
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 1, 29, 59)), "outside_hours")
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 1, 30, 0)), "open")
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 3, 30, 0)), "outside_hours")

    def test_show_filters(self):
        campaign = make(flights=[{"start": "2026-01-01", "show_ids": ["morning", "daytime"]}])
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 15), "morning"), "open")
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 15), "late"), "wrong_show")
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 15), None), "wrong_show")
        anywhere = make("Anywhere")
        self.assertEqual(reason(anywhere, utc(2026, 11, 5, 15), "late"), "open")
        self.assertEqual(reason(anywhere, utc(2026, 11, 5, 15), None), "open")

    def test_the_order_of_reasons(self):
        campaign = make(flights=[{"start": "2026-11-02", "end": "2026-11-09", "days": "1", "show_ids": ["x"],
                                  "dayparts": [{"from": "01:00", "to": "02:00"}]}])
        # Thursday 15:00 inside the dates: the day is wrong before the hours or the show are looked at.
        self.assertEqual(reason(campaign, utc(2026, 11, 5, 15), "other"), "wrong_day")
        # Monday 15:00: the day is right, so the hours are checked before the show.
        self.assertEqual(reason(campaign, utc(2026, 11, 9, 15), "other"), "outside_hours")
        self.assertEqual(reason(campaign, utc(2026, 11, 9, 1, 30), "other"), "wrong_show")
        self.assertEqual(reason(campaign, utc(2026, 11, 9, 1, 30), "x"), "open")
        self.assertEqual(reason(campaign, utc(2026, 11, 10, 1, 30), "x"), "ended")

    def test_times_outside_the_supported_range_are_refused_plainly(self):
        from myradio.ads.model import AdsError
        with self.assertRaises(AdsError):
            local_datetime(1e30, ZoneInfo("UTC"))


class EvenPaceArithmeticTests(unittest.TestCase):
    def test_the_share_of_the_day_that_has_passed(self):
        make_flight = lambda dayparts: make(flights=[{"start": "2026-01-01", "dayparts": dayparts}]).flights[0]
        all_day, work = make_flight([]), make_flight([{"from": "07:00", "to": "19:00"}])
        split = make_flight([{"from": "07:00", "to": "12:00"}, {"from": "13:00", "to": "19:00"}])
        hour = 3600
        for flight, seconds, expected in (
                (all_day, 0, 0.0), (all_day, 12 * hour, 0.5), (all_day, 86400, 1.0),
                (work, 6 * hour, 0.0), (work, 7 * hour, 0.0), (work, 13 * hour, 0.5), (work, 19 * hour, 1.0), (work, 23 * hour, 1.0),
                (split, 12.5 * hour, 5 / 11), (split, 16 * hour, 8 / 11), (split, 20 * hour, 1.0)):
            with self.subTest(seconds=seconds):
                self.assertAlmostEqual(day_fraction(flight, seconds), expected)

    def test_which_target_an_even_pace_aims_at(self):
        self.assertEqual(pace_target(make(plan={"per_day": 4})), ("day", 4))
        self.assertEqual(pace_target(make(plan={"total": 30}, flights=[{"start": "2026-11-02", "end": "2026-11-13"}])), ("flight", 30))
        self.assertEqual(pace_target(make(caps={"per_day": 12})), ("day", 12))
        self.assertEqual(pace_target(make(plan={"per_day": 4}, caps={"per_day": 12})), ("day", 4))
        self.assertIsNone(pace_target(make(caps={"per_hour": 3})))

    def test_expected_plays_by_now_for_a_daily_plan(self):
        campaign = make(plan={"per_day": 6}, flights=[{"start": "2026-01-01", "dayparts": [{"from": "07:00", "to": "19:00"}]}])
        flight = campaign.flights[0]
        for hour, expected in ((7, 0.0), (10, 1.5), (13, 3.0), (18, 5.5), (19, 6.0)):
            with self.subTest(hour=hour):
                local = dt.datetime(2026, 11, 5, hour, 0, tzinfo=dt.timezone.utc)
                self.assertAlmostEqual(expected_by_now(campaign, flight, local, ("day", 6)), expected)

    def test_expected_plays_by_now_for_a_total_spread_over_the_flight(self):
        # Monday 2026-11-02 to Friday 2026-11-13, weekdays only: ten playable days, three plays a day.
        campaign = make(plan={"total": 30}, flights=[{"start": "2026-11-02", "end": "2026-11-13", "days": "1,2,3,4,5"}])
        flight, target = campaign.flights[0], ("flight", 30)
        self.assertEqual(active_days_total(campaign), 10)
        cases = ((dt.datetime(2026, 11, 2, 0, 0, tzinfo=dt.timezone.utc), 0.0),
                 (dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.timezone.utc), 7.5),  # two whole days and half of the third
                 (dt.datetime(2026, 11, 9, 0, 0, tzinfo=dt.timezone.utc), 15.0),  # the weekend adds nothing
                 (dt.datetime(2026, 11, 13, 23, 59, 59, tzinfo=dt.timezone.utc), 30 * (9 + 86399 / 86400) / 10))
        for local, expected in cases:
            with self.subTest(at=local.isoformat()):
                self.assertAlmostEqual(expected_by_now(campaign, flight, local, target), expected)

    def test_counting_playable_days_over_several_flights(self):
        campaign = make(flights=[{"start": "2026-11-02", "end": "2026-11-06", "days": "1,2,3,4,5"},
                                 {"start": "2026-11-09", "end": "2026-11-15", "days": "6,7"}])
        self.assertEqual(active_days_total(campaign), 5 + 2)
        self.assertEqual(active_days_before(campaign, dt.date(2026, 11, 2)), 0)
        self.assertEqual(active_days_before(campaign, dt.date(2026, 11, 4)), 2)
        self.assertEqual(active_days_before(campaign, dt.date(2026, 11, 10)), 5)
        self.assertEqual(active_days_before(campaign, dt.date(2026, 11, 14)), 5)
        self.assertEqual(active_days_before(campaign, dt.date(2026, 11, 15)), 6)
        self.assertEqual(active_days_before(campaign, dt.date(2026, 12, 1)), 7)
        self.assertIsNone(active_days_total(make()))  # an open-ended flight cannot be counted
        self.assertTrue(is_active_day(campaign, dt.date(2026, 11, 14)))
        self.assertFalse(is_active_day(campaign, dt.date(2026, 11, 10)))
        self.assertFalse(is_active_day(campaign, dt.date(2026, 11, 8)))


if __name__ == "__main__":
    unittest.main()
