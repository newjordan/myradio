"""Reports against hand-computed numbers, and CSV that cannot run a formula in a spreadsheet."""
import csv
import datetime as dt
import io
import json
import sqlite3
import unittest

from myradio.ads.model import AdsError
from myradio.ads.reports import CSV_BASIS, MAX_DAYS, build_report, csv_cell, csv_header, planned_for_day, report_csv
from myradio.ads.store import AdsStore
from myradio.ads.words import REASON_LABELS, REPORTED_REASONS

try:
    from test_ads_support import NOW, make, utc
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import NOW, make, utc

NY = "America/New_York"


def memory_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return AdsStore(conn)


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.addCleanup(self.store.conn.close)

    def add(self, campaign, created=1.0):
        self.store.save_campaign(campaign, now=created, commercial_ack_id=None)
        return self.store.get(campaign.id)

    def play(self, campaign, at, **kw):
        creative = campaign.creatives[0]
        fields = dict(campaign_id=campaign.id, creative_id=creative.id, track_id=creative.track_id,
                      title=creative.title, category=None, paid=campaign.paid, spoken=False, started_at=at, source="event",
                      commercial_ack_id=1 if campaign.paid else None)
        fields.update(kw)
        return self.store.add_play(**fields)

    def report(self, first, last, **kw):
        campaigns = self.store.load(include_removed=True)
        return build_report(self.store, campaigns, now=kw.pop("now", NOW), first=first, last=last, **kw)


class HandComputedReportTests(ReportCase):
    def test_a_daily_plan_over_a_working_week(self):
        # Three plays a day on weekdays 2-6 November (Monday to Friday). Saturday and Sunday are not playable days.
        acme = self.add(make("Acme", plan={"per_day": 3}, flights=[{"start": "2026-11-02", "end": "2026-11-08", "days": "1,2,3,4,5"}]))
        for hour in (9, 12, 15):
            self.play(acme, utc(2026, 11, 2, hour))  # Monday: 3 of 3
        self.play(acme, utc(2026, 11, 3, 10))
        self.play(acme, utc(2026, 11, 3, 14))
        self.play(acme, utc(2026, 11, 3, 16), outcome="interrupted", detail="skipped")  # Tuesday: 2 delivered, 1 cut short
        for hour in (8, 10, 12, 14):  # Thursday: 4 of 3, one of them recovered afterwards
            self.play(acme, utc(2026, 11, 5, hour), source="reconciled" if hour == 8 else "event")
        self.play(acme, utc(2026, 11, 6, 23, 59, 59))  # the last second of Friday
        self.play(acme, utc(2026, 11, 7, 0, 0, 0))  # the first second of Saturday, a day with no plan
        self.store.bump_skips([("2026-11-03", acme.id, "hour_cap")] * 2 + [("2026-11-03", acme.id, "day_cap")]
                              + [("2026-11-05", acme.id, "ahead_of_pace")] * 4)
        report = self.report(dt.date(2026, 11, 2), dt.date(2026, 11, 8))
        [entry] = report["campaigns"]
        days = {d["date"]: d for d in entry["days"]}
        expected = {  # date: (plays, interrupted, recovered, planned, share, held back)
            "2026-11-02": (3, 0, 0, 3.0, 1.0, {}),
            "2026-11-03": (2, 1, 0, 3.0, 0.6667, {"hour_cap": 2, "day_cap": 1}),
            "2026-11-04": (0, 0, 0, 3.0, 0.0, {}),
            "2026-11-05": (4, 0, 1, 3.0, 1.3333, {"ahead_of_pace": 4}),
            "2026-11-06": (1, 0, 0, 3.0, 0.3333, {}),
            "2026-11-07": (1, 0, 0, 0.0, None, {}),
            "2026-11-08": (0, 0, 0, 0.0, None, {}),
        }
        self.assertEqual(sorted(days), sorted(expected))
        for date, (plays, cut, recovered, planned, share, held) in expected.items():
            with self.subTest(day=date):
                day = days[date]
                self.assertEqual((day["plays"], day["interrupted"], day["recovered"], day["planned"], day["delivered_share"],
                                  day["held_back"], day["held_back_total"]), (plays, cut, recovered, planned, share, held, sum(held.values())))
        # 11 delivered of 15 planned over the five playable days; 7 held back; 1 cut short; 1 recovered. None of the eleven
        # has an ending on record (this test writes the starts only), so all eleven are also counted as unconfirmed.
        self.assertEqual(entry["totals"], {"plays": 11, "interrupted": 1, "recovered": 1, "unconfirmed": 11, "planned": 15.0,
                                           "delivered_share": 0.7333, "held_back": {"hour_cap": 2, "day_cap": 1, "ahead_of_pace": 4},
                                           "held_back_total": 7})
        self.assertEqual((entry["name"], entry["plan"], entry["from"], entry["to"], entry["house"]), ("Acme", "3 plays a day", "2026-11-02", "2026-11-08", False))
        self.assertEqual((report["from"], report["to"]), ("2026-11-02", "2026-11-08"))

    def test_a_total_is_spread_evenly_over_the_playable_days(self):
        beta = self.add(make("Beta", plan={"total": 30}, flights=[{"start": "2026-11-02", "end": "2026-11-13", "days": "1,2,3,4,5"}]))
        for day in (2, 3, 4):
            self.play(beta, utc(2026, 11, day, 12))
        for day in (5, 6):
            self.play(beta, utc(2026, 11, day, 12))
            self.play(beta, utc(2026, 11, day, 13))
        [entry] = self.report(dt.date(2026, 11, 2), dt.date(2026, 11, 8))["campaigns"]
        self.assertEqual([d["planned"] for d in entry["days"]], [3.0, 3.0, 3.0, 3.0, 3.0, 0.0, 0.0])
        self.assertEqual([d["plays"] for d in entry["days"]], [1, 1, 1, 2, 2, 0, 0])
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["planned"], entry["totals"]["delivered_share"]), (7, 15.0, 0.4667))
        self.assertEqual(entry["plan"], "30 plays in total")
        [whole] = self.report(dt.date(2026, 11, 2), dt.date(2026, 11, 13))["campaigns"]
        self.assertEqual((whole["totals"]["planned"], whole["totals"]["delivered_share"]), (30.0, 0.2333))
        outside = self.report(dt.date(2026, 10, 1), dt.date(2026, 10, 3))["campaigns"][0]
        self.assertEqual((outside["totals"]["planned"], outside["totals"]["delivered_share"]), (0.0, None))

    def test_without_a_plan_there_is_no_planned_figure_and_no_share(self):
        free = self.add(make("Free"))
        self.play(free, utc(2026, 11, 5, 9))
        [entry] = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))["campaigns"]
        self.assertEqual((entry["plan"], entry["totals"]["planned"], entry["totals"]["delivered_share"]), (None, None, None))
        self.assertEqual((entry["days"][0]["planned"], entry["days"][0]["delivered_share"], entry["days"][0]["plays"]), (None, None, 1))

    def test_an_open_ended_total_has_no_daily_figure(self):
        spot = make("Open", plan={"total": 10}, pacing="asap")
        self.assertIsNone(planned_for_day(spot, dt.date(2026, 11, 5)))
        self.assertEqual(planned_for_day(make("Day", plan={"per_day": 4}), dt.date(2026, 11, 5)), 4.0)
        self.assertEqual(planned_for_day(make("Before", plan={"per_day": 4}, flights=[{"start": "2026-12-01"}]), dt.date(2026, 11, 5)), 0.0)

    def test_days_follow_the_spots_own_time_zone_and_the_clock_change(self):
        gamma = self.add(make("Gamma", timezone=NY))
        for at in (utc(2026, 11, 1, 3, 59, 59), utc(2026, 11, 1, 4, 0, 0), utc(2026, 11, 2, 4, 59, 59), utc(2026, 11, 2, 5, 0, 0)):
            self.play(gamma, at)  # Oct 31 23:59:59, Nov 1 00:00 EDT, Nov 1 23:59:59 EST, Nov 2 00:00 EST
        [entry] = self.report(dt.date(2026, 10, 31), dt.date(2026, 11, 2))["campaigns"]
        self.assertEqual([(d["date"], d["plays"]) for d in entry["days"]], [("2026-10-31", 1), ("2026-11-01", 2), ("2026-11-02", 1)])

    def test_the_default_range_is_the_last_seven_days_in_the_spots_own_zone(self):
        acme = self.add(make("Acme", timezone="Asia/Tokyo"))
        at = utc(2026, 11, 5, 16, 0)  # Friday 01:00 in Tokyo
        [entry] = self.report(None, None, now=at)["campaigns"]
        self.assertEqual((entry["from"], entry["to"], len(entry["days"])), ("2026-10-31", "2026-11-06", 7))
        self.play(acme, at - 3600)  # 15:00 UTC is exactly midnight at the start of the 6th in Tokyo
        self.play(acme, at - 3601)  # one second earlier is still the 5th
        [entry] = self.report(None, None, now=at)["campaigns"]
        self.assertEqual([d["plays"] for d in entry["days"][-2:]], [1, 1])

    def test_a_removed_spot_appears_only_when_it_has_plays_in_the_range(self):
        gone = self.add(make("Gone"))
        self.add(make("Kept"))
        self.play(gone, utc(2026, 11, 3, 12))
        self.store.remove(gone.id, utc(2026, 11, 4))
        names = [c["name"] for c in self.report(dt.date(2026, 11, 3), dt.date(2026, 11, 3))["campaigns"]]
        self.assertEqual(sorted(names), ["Gone", "Kept"])
        names = [c["name"] for c in self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))["campaigns"]]
        self.assertEqual(names, ["Kept"])
        [only] = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5), campaign_id=gone.id)["campaigns"]
        self.assertEqual((only["name"], only["removed"]), ("Gone", True))

    def test_one_spot_report_lists_its_plays(self):
        acme = self.add(make("Acme"))
        self.play(acme, utc(2026, 11, 5, 9), outcome="finished")
        self.play(acme, utc(2026, 11, 5, 10), outcome="interrupted", detail="skipped")
        self.play(acme, utc(2026, 11, 5, 11), source="reconciled")
        [entry] = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5), campaign_id=acme.id)["campaigns"]
        self.assertEqual([(p["outcome"], p["detail"], p["source"]) for p in entry["plays"]],
                         [("finished", None, "live"), ("interrupted", "skipped", "live"), ("unconfirmed", None, "recovered")])
        self.assertTrue(entry["plays"][0]["at"].startswith("2026-11-05T09:00:00"))
        self.assertNotIn("plays", self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))["campaigns"][0])

    def test_the_notes_say_what_the_numbers_mean_and_what_needs_attention(self):
        acme = self.add(make("Acme"))
        plain = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))
        self.assertEqual(len(plain["notes"]), 1)
        self.assertIn("do not show who listened", plain["notes"][0])
        self.play(acme, utc(2026, 11, 5, 9), commercial_ack_id=None, source="reconciled")
        notes = " ".join(self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))["notes"])
        self.assertIn("1 play was found afterwards", notes)
        self.assertIn("1 paid play aired without the paid-use confirmation", notes)

    def test_plays_whose_ending_was_never_seen_are_counted_as_played_and_shown_as_unconfirmed(self):
        acme = self.add(make("Acme", plan={"per_day": 4}))
        self.play(acme, utc(2026, 11, 5, 9), outcome="finished", ended_at=utc(2026, 11, 5, 9, 0, 30))
        self.play(acme, utc(2026, 11, 5, 10), outcome="finished", ended_at=utc(2026, 11, 5, 10, 0, 30))
        self.play(acme, utc(2026, 11, 5, 11))  # the end signal never arrived
        self.play(acme, utc(2026, 11, 5, 12), outcome="interrupted", detail="skipped")
        self.play(acme, utc(2026, 11, 6, 9))  # the next day, also without an ending
        report = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 6))
        [entry] = report["campaigns"]
        first, second = entry["days"]
        self.assertEqual((first["plays"], first["interrupted"], first["unconfirmed"]), (3, 1, 1))
        self.assertEqual((second["plays"], second["unconfirmed"]), (1, 1))
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["unconfirmed"]), (4, 2))
        self.assertEqual(entry["totals"]["delivered_share"], 0.5)  # they are still counted as played: 4 of 8 planned
        self.assertTrue(any(n.startswith("2 plays have no ending on record") and "counted as played" in n for n in report["notes"]), report["notes"])
        [listed] = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 6), campaign_id=acme.id)["campaigns"]
        self.assertEqual([p["outcome"] for p in listed["plays"]], ["finished", "finished", "unconfirmed", "interrupted", "unconfirmed"])

    def test_with_every_ending_on_record_nothing_is_unconfirmed_and_no_note_is_added(self):
        acme = self.add(make("Acme"))
        self.play(acme, utc(2026, 11, 5, 9), outcome="finished", ended_at=utc(2026, 11, 5, 9, 0, 30))
        report = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))
        self.assertEqual(report["campaigns"][0]["totals"]["unconfirmed"], 0)
        self.assertEqual(len(report["notes"]), 1)  # only the statement of what a count means
        single = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))
        self.play(acme, utc(2026, 11, 5, 11))
        notes = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))["notes"]
        self.assertTrue(any(n.startswith("1 play has no ending on record") and "It is counted as played" in n for n in notes), notes)
        self.assertEqual(single["campaigns"][0]["totals"]["unconfirmed"], 0)

    def test_refusals(self):
        self.add(make("Acme"))
        for label, first, last, extra in (("backwards", dt.date(2026, 11, 6), dt.date(2026, 11, 5), {}),
                                          ("too long", dt.date(2025, 1, 1), dt.date(2026, 11, 5), {}),
                                          ("unknown spot", None, None, {"campaign_id": "f" * 32}),
                                          ("too big", dt.date(2026, 1, 1), dt.date(2026, 11, 5), {"max_cells": 100})):
            with self.subTest(case=label), self.assertRaises(AdsError) as caught:
                self.report(first, last, **extra)
            self.assertTrue(str(caught.exception).endswith("."))
        self.assertEqual(MAX_DAYS, 366)

    def test_the_report_is_json_ready(self):
        acme = self.add(make("Acme", plan={"per_day": 2}))
        self.play(acme, utc(2026, 11, 5, 9))
        report = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(set(report), {"from", "to", "generated_at", "campaigns", "reasons", "notes"})
        self.assertEqual(report["reasons"], REASON_LABELS)

    def test_an_empty_station_still_gives_a_report(self):
        report = self.report(None, None)
        self.assertEqual(report["campaigns"], [])
        self.assertEqual(report_csv(report).splitlines(), [",".join(csv_cell(h) for h in csv_header())])


class CsvTests(ReportCase):
    HOSTILE = [
        ('=HYPERLINK("http://evil.example","click")', True), ("+1+1", True), ("-2+3", True), ("@SUM(1+1)", True),
        ("\t=1", True), ("\r=1", True), ("\n=1", True), (" =1+1", True), ("  \t@x", True), ("＝1+1", True), ("＋1", True), ("－1", True), ("＠x", True),
        ("=cmd|' /C calc'!A0", True), ("Acme, Inc.", False), ('Say "hi"', False), ("two\nlines", False), ("plain", False),
        ("a=b", False), ("1+1", False), ("Acme - friends", False), ("", False),
    ]

    def test_text_a_spreadsheet_could_run_is_neutralized(self):
        for text, dangerous in self.HOSTILE:
            with self.subTest(text=text):
                cell = csv_cell(text)
                [[_, parsed]] = list(csv.reader(io.StringIO("x," + cell + "\r\n")))
                if dangerous:
                    self.assertTrue(parsed.startswith("'"), parsed)
                    self.assertEqual(parsed[1:], text)
                else:
                    self.assertEqual(parsed, text.replace("\n", "\n"))
                self.assertNotIn(parsed[:1], ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠"))
                self.assertNotIn(parsed.lstrip()[:1], ("=", "+", "-", "@", "＝", "＋", "－", "＠"))

    def test_numbers_are_written_as_numbers_and_never_prefixed(self):
        self.assertEqual([csv_cell(v) for v in (0, 7, 3.0, 0.0, 0.6667, 1.5, 100.0, 12.25)], ["0", "7", "3", "0", "0.6667", "1.5", "100", "12.25"])
        self.assertEqual((csv_cell(None), csv_cell(True), csv_cell(False), csv_cell(float("nan"))), ("", "yes", "no", ""))

    def test_quoting_follows_the_usual_rules(self):
        self.assertEqual(csv_cell("a,b"), '"a,b"')
        self.assertEqual(csv_cell('say "x"'), '"say ""x"""')
        self.assertEqual(csv_cell("one\r\ntwo"), '"one\r\ntwo"')
        self.assertEqual(csv_cell("plain"), "plain")

    def test_a_whole_report_round_trips_and_no_cell_can_start_a_formula(self):
        names = ['=HYPERLINK("http://evil.example","x")', "+SUM(A1)", "-1", "@now", "Acme, Inc.", 'Say "hi"']
        for i, name in enumerate(names):
            spot = self.add(make(name, plan={"per_day": 2}), created=float(i))
            self.play(spot, utc(2026, 11, 5, 9 + i))
        self.store.bump_skips([("2026-11-05", self.store.load()[0].id, "hour_cap")])
        report = self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 6))
        text = report_csv(report)
        self.assertTrue(text.endswith("\r\n"))
        self.assertNotIn("\n\n", text.replace("\r\n", "\n"))
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], csv_header())
        self.assertEqual(len(rows), 1 + len(names) * 2)
        for row in rows[1:]:
            self.assertEqual(len(row), len(csv_header()))
            for cell in row:
                self.assertFalse(cell[:1] in ("=", "+", "-", "@", "\t", "\r"), cell)
        spot_names = sorted({row[1] for row in rows[1:]})
        self.assertIn("'=HYPERLINK(\"http://evil.example\",\"x\")", spot_names)
        self.assertIn("Acme, Inc.", spot_names)
        self.assertIn('Say "hi"', spot_names)
        first = next(r for r in rows[1:] if r[1] == "'=HYPERLINK(\"http://evil.example\",\"x\")" and r[0] == "2026-11-05")
        header = {name: i for i, name in enumerate(rows[0])}
        self.assertEqual((first[header["Plays"]], first[header["Planned plays"]], first[header["Delivered %"]], first[header["Type"]]),
                         ("1", "2", "50", "Paid"))
        self.assertEqual(first[header["Held back: " + REASON_LABELS["hour_cap"]]], "1")

    def test_the_header_names_every_reason_in_plain_words(self):
        header = csv_header()
        self.assertEqual(len(header), 10 + len(REPORTED_REASONS) + 2)  # the ten fixed columns, one per reason, then two closing ones
        self.assertEqual(header[-2:], ["Ending not confirmed", "Basis"])
        for name in header:
            self.assertNotIn("_", name)
            self.assertFalse(name[:1] in "=+-@")

    def test_the_spreadsheet_carries_what_a_play_means_and_how_many_endings_are_missing(self):
        acme = self.add(make("Acme", plan={"per_day": 2}))
        self.play(acme, utc(2026, 11, 5, 9), outcome="finished", ended_at=utc(2026, 11, 5, 9, 0, 30))
        self.play(acme, utc(2026, 11, 5, 10))  # no ending on record
        rows = list(csv.reader(io.StringIO(report_csv(self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 6))))))
        header = {name: i for i, name in enumerate(rows[0])}
        self.assertEqual(rows[0][-2:], ["Ending not confirmed", "Basis"])
        self.assertEqual(rows[0][:9], ["Date", "Spot", "Advertiser", "Type", "Plays", "Cut off", "Recovered", "Planned plays", "Delivered %"])  # unmoved
        self.assertEqual([(row[header["Plays"]], row[header["Ending not confirmed"]]) for row in rows[1:]], [("2", "1"), ("0", "0")])
        for row in rows[1:]:  # on every row, so the file explains itself after it has been passed on
            self.assertEqual(row[header["Basis"]], CSV_BASIS)
        self.assertIn("not listeners", CSV_BASIS)
        self.assertNotIn(",", CSV_BASIS)

    def test_house_spots_are_labelled_as_station_promos(self):
        promo = self.add(make("Promo", house=True))
        self.play(promo, utc(2026, 11, 5, 9), commercial_ack_id=None)
        rows = list(csv.reader(io.StringIO(report_csv(self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))))))
        self.assertEqual(rows[1][3], "Station promo")
        self.assertNotIn("without the paid-use confirmation", " ".join(self.report(dt.date(2026, 11, 5), dt.date(2026, 11, 5))["notes"]))


if __name__ == "__main__":
    unittest.main()
