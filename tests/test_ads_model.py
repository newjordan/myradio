"""Campaign model: input checking, plain-language errors, the sponsorship line and speech-tag rules."""
import datetime as dt
import unittest

from myradio.ads.model import (DEFAULT_DISCLOSURE, FREQUENCY_PRESETS, AdsError, clean_text, count_weekdays, disclosure_ok,
                               estimate_seconds, house_name_ok, neutralize_tags, parse_campaign, render_disclosure,
                               sponsor_name_ok, spoken_text, validate_disclosure, zone_for)

try:
    from test_ads_support import TODAY
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import TODAY

JARGON = ("PCM", "GGUF", "SSRF", "Traceback", "NoneType", "KeyError", "TypeError", "ValueError", "Exception", "JSON")


def parse(**patch):
    raw = {"advertiser": "Corner Bike Co.", "timezone": "America/New_York",
           "creatives": [{"kind": "recorded", "track_id": "t-bikes"}]}
    raw.update(patch)
    return parse_campaign(raw, today=TODAY, campaign_id=raw.pop("id", None))


class DefaultsTests(unittest.TestCase):
    def test_a_minimal_spot_gets_regular_limits(self):
        campaign, warnings = parse()
        self.assertEqual((campaign.name, campaign.frequency, campaign.pacing, campaign.priority), ("Corner Bike Co.", "regular", "even", 5))
        self.assertEqual(campaign.caps.to_dict(), {"per_hour": 2, "per_day": 10, "per_week": 50})
        self.assertEqual(campaign.separation.to_dict(), {"campaign_minutes": 20, "category_minutes": 10})
        self.assertEqual(len(campaign.flights), 1)
        flight = campaign.flights[0]
        self.assertEqual((flight.start, flight.end, flight.days, flight.dayparts, flight.show_ids),
                         (TODAY, None, (1, 2, 3, 4, 5, 6, 7), (), ()))
        self.assertEqual(len(campaign.id), 32)
        self.assertTrue(any("exactly as recorded" in w for w in warnings))

    def test_frequency_presets_and_overrides(self):
        for name, preset in FREQUENCY_PRESETS.items():
            with self.subTest(preset=name):
                campaign, _ = parse(frequency=name)
                self.assertEqual(campaign.frequency, name)
                self.assertEqual(campaign.caps.per_hour, preset["per_hour"])
                self.assertEqual(campaign.separation.campaign_minutes, preset["campaign_minutes"])
        campaign, _ = parse(frequency="often", caps={"per_hour": 7})
        self.assertEqual((campaign.frequency, campaign.caps.per_hour, campaign.caps.per_day), ("custom", 7, 20))
        campaign, _ = parse(caps={"per_day": None})  # an explicit null means no limit
        self.assertIsNone(campaign.caps.per_day)
        self.assertEqual(campaign.caps.per_hour, 2)
        campaign, _ = parse(frequency="custom")
        self.assertEqual((campaign.caps.per_hour, campaign.separation.campaign_minutes), (None, 0))

    def test_house_spots_default_to_light_and_use_the_station_name(self):
        campaign, _ = parse_campaign({"house": True, "creatives": [{"kind": "script", "text": "Tell a friend."}]},
                                     today=TODAY, station_name="Test FM")
        self.assertEqual((campaign.advertiser, campaign.frequency, campaign.house, campaign.paid), ("Test FM", "light", True, False))
        self.assertEqual(campaign.creatives[0].disclosure, DEFAULT_DISCLOSURE)

    def test_a_spot_round_trips_through_its_own_dictionary(self):
        campaign, _ = parse(category="Bike Shops!", priority=8, pacing="asap", plan={"total": 40},
                            flights=[{"start": "2026-11-12", "end": "2026-12-31", "days": "1,3,5",
                                      "dayparts": [{"from": "07:00", "to": "12:00"}, {"from": "13:00", "to": "24:00"}],
                                      "show_ids": ["morning", "daytime"]}],
                            creatives=[{"kind": "recorded", "track_id": "t1", "title": "Tune-up"},
                                       {"kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex"}])
        again, _ = parse_campaign(campaign.to_dict(), today=TODAY, campaign_id=campaign.id,
                                  own_creatives=frozenset(c.id for c in campaign.creatives))
        self.assertEqual(again, campaign)
        self.assertEqual(campaign.category, "bike-shops")
        self.assertEqual(campaign.flights[0].dayparts, ((25200, 43200), (46800, 86400)))


class RejectionTests(unittest.TestCase):
    CASES = [
        ("not an object", None, "named fields"),
        ("missing advertiser", {"advertiser": None}, "say who the spot is for"),
        ("empty advertiser", {"advertiser": "   "}, "can't be empty"),
        ("long advertiser", {"advertiser": "x" * 81}, "too long"),
        ("unknown field", {"frequncy": "often"}, "Unknown setting \"frequncy\""),
        ("bad timezone", {"timezone": "Mars/Olympus"}, "Unknown timezone"),
        ("path-like timezone", {"timezone": "../../etc/passwd"}, "Unknown timezone"),
        ("directory timezone", {"timezone": "America"}, "Unknown timezone"),
        ("bad pacing", {"pacing": "fast"}, "even"),
        ("bad frequency", {"frequency": "daily"}, "light"),
        ("priority range", {"priority": 11}, "between 1 and 10"),
        ("priority type", {"priority": True}, "whole number"),
        ("cap zero", {"caps": {"per_hour": 0}}, "hourly limit"),
        ("cap type", {"caps": {"per_day": "5"}}, "whole number"),
        ("cap unknown key", {"caps": {"per_month": 5}}, "Unknown setting"),
        ("gap range", {"separation": {"campaign_minutes": 2000}}, "gap"),
        ("house type", {"house": "yes"}, "true or false"),
        ("paused type", {"paused": 1}, "true or false"),
        ("no creatives", {"creatives": []}, "at least one message"),
        ("too many creatives", {"creatives": [{"kind": "script", "text": "Hi"}] * 11}, "at most 10"),
        ("bad creative kind", {"creatives": [{"kind": "video", "track_id": "x"}]}, "recorded"),
        ("recorded without track", {"creatives": [{"kind": "recorded"}]}, "needs a recording"),
        ("recorded with text", {"creatives": [{"kind": "recorded", "track_id": "x", "text": "hi"}]}, "plays its recording as is"),
        ("script without text", {"creatives": [{"kind": "script"}]}, "something to say"),
        ("script blank text", {"creatives": [{"kind": "script", "text": "  "}]}, "can't be empty"),
        ("script too long", {"creatives": [{"kind": "script", "text": "x" * 401}]}, "too long"),
        ("bad voice", {"creatives": [{"kind": "script", "text": "Hi", "voice": "Dex Man"}]}, "voice"),
        ("bad track id", {"creatives": [{"kind": "recorded", "track_id": "../x"}]}, "recording id"),
        ("foreign creative id", {"creatives": [{"id": "f" * 32, "kind": "script", "text": "Hi"}]}, "isn't part of this spot"),
        ("flight dates reversed", {"flights": [{"start": "2026-12-01", "end": "2026-11-01"}]}, "on or after"),
        ("flight bad date", {"flights": [{"start": "2026-02-30"}]}, "real date"),
        ("flight bad format", {"flights": [{"start": "12/01/2026"}]}, "date like"),
        ("flight year range", {"flights": [{"start": "1999-01-01"}]}, "between the years"),
        ("flight too long", {"flights": [{"start": "2026-01-01", "end": "2040-01-01"}]}, "ten years"),
        ("no playable day", {"flights": [{"start": "2026-11-09", "end": "2026-11-09", "days": "6"}]}, "no day it can play"),
        ("bad days", {"flights": [{"days": "0,8"}]}, "weekday numbers"),
        ("days as list", {"flights": [{"days": [1, 2]}]}, "weekday numbers"),
        ("overlapping flights", {"flights": [{"start": "2026-11-01", "end": "2026-11-10"}, {"start": "2026-11-10"}]}, "overlap"),
        ("open flight not last", {"flights": [{"start": "2026-11-01"}, {"start": "2026-12-01"}]}, "overlap"),
        ("too many flights", {"flights": [{"start": f"2026-{m:02d}-01", "end": f"2026-{m:02d}-02"} for m in range(1, 13)] +
                              [{"start": "2027-01-01", "end": "2027-01-02"}]}, "at most 12"),
        ("window backwards", {"flights": [{"dayparts": [{"from": "19:00", "to": "07:00"}]}]}, "end after it starts"),
        ("window format", {"flights": [{"dayparts": [{"from": "7am", "to": "9am"}]}]}, "24-hour"),
        ("window from 24:00", {"flights": [{"dayparts": [{"from": "24:00", "to": "24:00"}]}]}, "24-hour"),
        ("windows overlap", {"flights": [{"dayparts": [{"from": "07:00", "to": "10:00"}, {"from": "09:00", "to": "11:00"}]}]}, "overlap"),
        ("show id", {"flights": [{"show_ids": ["Morning Rise"]}]}, "show id"),
        ("plan both", {"plan": {"total": 10, "per_day": 2}}, "not both"),
        ("plan range", {"plan": {"total": 0}}, "between 1"),
        ("plan type", {"plan": 5}, "total"),
        ("even total without end", {"plan": {"total": 10}}, "end date"),
    ]

    def test_every_refusal_is_plain_language_and_names_what_to_fix(self):
        for label, patch, expected in self.CASES:
            with self.subTest(case=label):
                with self.assertRaises(AdsError) as caught:
                    if patch is None:
                        parse_campaign("nope", today=TODAY)
                    else:
                        parse(**patch)
                message = str(caught.exception)
                self.assertIn(expected.lower(), message.lower())
                self.assertLessEqual(len(message), 320)
                for word in JARGON:
                    self.assertNotIn(word, message)

    def test_a_total_without_an_end_date_is_fine_when_pacing_is_asap(self):
        campaign, _ = parse(plan={"total": 10}, pacing="asap")
        self.assertEqual(campaign.plan.total, 10)

    def test_a_daily_limit_below_the_daily_plan_is_a_warning_not_an_error(self):
        _, warnings = parse(plan={"per_day": 5}, caps={"per_day": 3})
        self.assertTrue(any("can't be met" in w for w in warnings))

    def test_editing_an_existing_spot_requires_its_own_id(self):
        campaign, _ = parse()
        with self.assertRaises(AdsError):
            parse_campaign({"id": "e" * 32, "advertiser": "X", "creatives": [{"kind": "script", "text": "Hi"}]}, today=TODAY,
                           campaign_id=campaign.id)
        with self.assertRaises(AdsError):
            parse_campaign({"id": campaign.id, "advertiser": "X", "creatives": [{"kind": "script", "text": "Hi"}]}, today=TODAY)

    def test_creative_ids_survive_an_edit_and_cannot_repeat(self):
        campaign, _ = parse(creatives=[{"kind": "script", "text": "One."}, {"kind": "script", "text": "Two."}])
        own = frozenset(c.id for c in campaign.creatives)
        edited = campaign.to_dict()
        edited["creatives"][1]["text"] = "Two, changed."
        again, _ = parse_campaign(edited, today=TODAY, campaign_id=campaign.id, own_creatives=own)
        self.assertEqual([c.id for c in again.creatives], [c.id for c in campaign.creatives])
        edited["creatives"][1]["id"] = edited["creatives"][0]["id"]
        with self.assertRaises(AdsError):
            parse_campaign(edited, today=TODAY, campaign_id=campaign.id, own_creatives=own)

    def test_hostile_text_is_flattened(self):
        campaign, _ = parse(advertiser="Corner‮ Bike​ Co.\x00\n", name="Spring\ttune ups",
                            creatives=[{"kind": "script", "text": "Fix‮ it\r\nnow\x07!"}])
        self.assertEqual(campaign.advertiser, "Corner Bike Co.")
        self.assertEqual(campaign.name, "Spring tune ups")
        self.assertEqual(campaign.creatives[0].text, "Fix it now !")
        with self.assertRaises(AdsError):
            clean_text("x" * 100000, "Text", maximum=10)

    def test_weekday_counting(self):
        monday = dt.date(2026, 11, 2)
        self.assertEqual(count_weekdays(monday, monday + dt.timedelta(days=13), (1, 2, 3, 4, 5)), 10)
        self.assertEqual(count_weekdays(monday, monday, (2,)), 0)
        self.assertEqual(count_weekdays(monday, monday + dt.timedelta(days=9), (6, 7)), 2)  # Mon..Wed week 2: one weekend
        self.assertEqual(count_weekdays(monday + dt.timedelta(days=1), monday, (1, 2)), 0)

    def test_zone_lookup(self):
        self.assertEqual(zone_for("Europe/London").key, "Europe/London")
        for bad in ("", None, 5, "x" * 101, "Not/AZone"):
            with self.subTest(zone=bad), self.assertRaises(AdsError):
                zone_for(bad)


class DisclosureTests(unittest.TestCase):
    ACCEPT = [
        None, "", "   ", DEFAULT_DISCLOSURE,
        "This ad is sponsored by {sponsor}.",
        "Sponsored by {sponsor}",
        "This message is sponsored by Corner Bike Co.",
        "Today's tune-up tips are sponsored by {sponsor}, your local bike shop!",
        "SPONSORED BY {SPONSOR}".replace("{SPONSOR}", "{sponsor}"),
    ]
    REJECT = [
        ("no sponsor name", "This message is sponsored."),
        ("no word sponsored", "This message is brought to you by {sponsor}"),
        ("paid for by", "Paid for by {sponsor}"),
        ("only the name", "{sponsor}"),
        ("negated", "This message is not sponsored by {sponsor}"),
        ("negated contraction", "This message isn't sponsored by {sponsor}"),
        ("unsponsored", "This is an unsponsored message from {sponsor}"),
        ("never", "{sponsor} has never sponsored this message"),
        ("without", "This message is aired without being sponsored by {sponsor}"),
        ("non", "This is a non-sponsored message from {sponsor}"),
        ("other braces", "Sponsored by {sponsor} {other}"),
        ("stray brace", "Sponsored by {sponsor}}"),
        ("too long", "Sponsored by {sponsor} " + "x" * 200),
        ("not text", 5),
    ]

    def test_allowed_variants(self):
        for template in self.ACCEPT:
            with self.subTest(template=template):
                kept = validate_disclosure(template, "Corner Bike Co.")
                self.assertTrue(kept)
                self.assertTrue(disclosure_ok(kept.replace("{sponsor}", "Corner Bike Co."), "Corner Bike Co."))

    def test_variants_that_hide_who_is_paying_are_refused(self):
        for label, template in self.REJECT:
            with self.subTest(case=label), self.assertRaises(AdsError) as caught:
                validate_disclosure(template, "Corner Bike Co.")
            self.assertTrue(str(caught.exception))

    def test_a_sponsor_name_that_contains_the_word_cannot_satisfy_the_rule_alone(self):
        self.assertFalse(disclosure_ok("Hello from Sponsored Things Ltd", "Sponsored Things Ltd"))
        self.assertTrue(disclosure_ok("This message is sponsored by Sponsored Things Ltd", "Sponsored Things Ltd"))

    def test_short_sponsor_names_do_not_break_the_check(self):
        for name in ("S", "Red", "Spon", "ed", "or"):
            with self.subTest(name=name):
                self.assertTrue(disclosure_ok(f"This message is sponsored by {name}", name))
                self.assertFalse(disclosure_ok("This message is brought to you by " + name, name))

    def test_a_sponsor_name_must_be_a_whole_word_in_the_line(self):
        self.assertFalse(disclosure_ok("Sunday sponsored by Acme", "Sun"))
        self.assertTrue(disclosure_ok("Sponsored by Sun.", "Sun"))

    def test_a_negative_word_inside_the_sponsor_name_is_not_a_denial(self):
        self.assertTrue(disclosure_ok("This message is sponsored by No Limit Pizza", "No Limit Pizza"))

    def test_the_default_matches_the_brief(self):
        self.assertEqual(DEFAULT_DISCLOSURE, "This message is sponsored by {sponsor}")

    def test_every_script_is_spoken_with_the_line_first(self):
        text = spoken_text("Corner Bike Co.", "Fixing bikes since 1998.")
        self.assertEqual(text, "This message is sponsored by Corner Bike Co.. Fixing bikes since 1998.".replace("..", "."))
        self.assertTrue(text.startswith("This message is sponsored by Corner Bike Co."))
        custom = spoken_text("Acme", "Buy now!", "Today is sponsored by {sponsor}!")
        self.assertTrue(custom.startswith("Today is sponsored by Acme!"))

    def test_a_stored_line_that_no_longer_passes_falls_back_to_the_default(self):
        # The sponsor was renamed after the wording named the old sponsor literally.
        self.assertEqual(render_disclosure("Sponsored by Old Name", "New Name"), "This message is sponsored by New Name.")
        self.assertEqual(render_disclosure("nothing useful", "Acme"), "This message is sponsored by Acme.")
        self.assertEqual(render_disclosure("", "Acme"), "This message is sponsored by Acme.")

    def test_tags_in_the_sponsor_name_and_copy_are_neutralized_in_the_spoken_text(self):
        text = spoken_text("Acme <|speaker:2|> [laughs]", "Buy [whispers] now <|speaker:3|>!", "Sponsored by {sponsor}")
        for forbidden in ("<|", "|>", "[", "]"):
            self.assertNotIn(forbidden, text)
        self.assertIn("Acme", text)
        self.assertIn("sponsored", text.lower())

    def test_a_written_message_is_labelled_with_the_start_of_its_copy(self):
        from myradio.ads.model import short_title
        self.assertEqual(short_title("Fixing bikes since 1998."), "Fixing bikes since 1998.")
        self.assertEqual(short_title("Fixing bikes since 1998. Mention the station for a free tune-up."), "Fixing bikes since 1998. Mention the\u2026")
        self.assertEqual(short_title("x" * 100), "x" * 36 + "\u2026")
        self.assertEqual(short_title("aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj"), "aaaa bbbb cccc dddd eeee ffff gggg\u2026")  # not "... hh"
        campaign, _ = parse(creatives=[{"kind": "script", "text": "Fixing bikes since 1998. Mention the station for a free tune-up."}])
        self.assertEqual(campaign.creatives[0].title, "Fixing bikes since 1998. Mention the\u2026")

    def test_estimates_are_bounded_below(self):
        self.assertEqual(estimate_seconds("Hi"), 3.0)
        self.assertGreater(estimate_seconds("word " * 60), 20)


class SponsorNameTests(unittest.TestCase):
    """The spoken line has to name who is paying; a name with nothing left to read once speech tags are removed cannot."""

    NAMELESS = ["<|speaker:1|>", "<|speaker:1|> <|speaker:2|>", "[ ]", "( )", "...", "-", "\U0001F6B2", "<|", "|>"]
    READABLE = ["Acme", "A", "7", "[Acme]", "Acme <|speaker:1|>", "自転車屋", "Café No. 5", "<|speaker:1|>Acme"]

    def test_which_names_the_voice_can_read(self):
        for name in self.NAMELESS:
            with self.subTest(name=name):
                self.assertFalse(sponsor_name_ok(name))
        for name in self.READABLE:
            with self.subTest(name=name):
                self.assertTrue(sponsor_name_ok(name))
        for odd in (None, 5, b"Acme", ["Acme"]):
            self.assertFalse(sponsor_name_ok(odd))

    def test_a_written_message_for_a_nameless_sponsor_is_refused_in_plain_words(self):
        for name in self.NAMELESS:
            with self.subTest(name=name), self.assertRaises(AdsError) as caught:
                parse(advertiser=name, creatives=[{"kind": "script", "text": "Buy bikes."}])
            self.assertIn("letters or numbers", str(caught.exception))
            for word in JARGON:
                self.assertNotIn(word, str(caught.exception))
        with self.assertRaises(AdsError) as caught:  # a station promo is named after the station, and the sentence says so
            parse_campaign({"house": True, "creatives": [{"kind": "script", "text": "Tell a friend."}]}, today=TODAY, station_name="[ ]")
        self.assertIn("station's name", str(caught.exception))

    def test_a_recording_is_not_held_to_it_and_neither_is_reading_a_stored_spot_back(self):
        campaign, _ = parse(advertiser="<|speaker:1|>", creatives=[{"kind": "recorded", "track_id": "t1"}])
        self.assertEqual(campaign.advertiser, "<|speaker:1|>")
        stored, _ = parse_campaign({"advertiser": "<|speaker:1|>", "creatives": [{"kind": "script", "text": "Buy bikes."}]}, today=TODAY,
                                   check_names=False)
        self.assertEqual(stored.advertiser, "<|speaker:1|>")

    def test_the_line_for_a_nameless_sponsor_is_still_a_sentence_and_never_an_error(self):
        # Saving such a spot is refused and ads.spot_speech will not hand it out, but a preview of a break must not fail.
        self.assertEqual(render_disclosure("", "<|speaker:1|>"), "This message is sponsored by .")
        self.assertEqual(spoken_text("<|speaker:1|>", "Buy bikes."), "This message is sponsored by . Buy bikes.")

    def test_a_station_promo_carries_only_the_station_name(self):
        for advertiser, station, expected in (("Garden FM", "Garden FM", True), ("garden fm", "Garden FM", True), ("GARDEN  FM", "Garden FM", True),
                                              ("Garden FM", " Garden   FM ", True), ("Acme Bikes", "Garden FM", False), ("Garden", "Garden FM", False),
                                              ("Garden FM Bikes", "Garden FM", False), ("Garden FM", "", False), ("Garden FM", "   ", False)):
            with self.subTest(advertiser=advertiser, station=station):
                self.assertEqual(house_name_ok(advertiser, station), expected)


class NeutralizeTests(unittest.TestCase):
    CASES = [
        "<|speaker:1|>Hello",
        "Hello <|speaker:12|> there",
        "[laughs] funny [whispers] quiet [excited]",
        "<<|speaker:1|>|>nested",
        "<|<|speaker:1|>speaker:2|>",
        "<|speaker:1",
        "speaker:1|>",
        "＜｜speaker:1｜＞ full width",
        "［laughs］ full width brackets",
        "<|" + "a" * 200 + "|>",
        "[[laughs]]",
        "plain text with 100% honesty & ampersands",
        "",
    ]

    def test_no_speaker_marker_or_event_tag_survives(self):
        for text in self.CASES:
            with self.subTest(text=text[:40]):
                cleaned = neutralize_tags(text)
                for forbidden in ("<|", "|>", "[", "]", "｜", "［", "］"):
                    self.assertNotIn(forbidden, cleaned)
                self.assertEqual(cleaned, " ".join(cleaned.split()))

    def test_a_speaker_marker_is_removed_whole_not_just_its_brackets(self):
        # Leaving "speaker:2" behind would have the voice read it aloud.
        self.assertEqual(neutralize_tags("<|speaker:1|>Hello"), "Hello")
        self.assertEqual(neutralize_tags("Hello <|speaker:12|> there"), "Hello there")
        self.assertEqual(neutralize_tags("<<|speaker:1|>|>nested"), "< nested")  # a stray bracket is harmless; the marker is gone
        for text in ("<|speaker:2|> hi", "hi <|speaker:2|>", "<|speaker:2|>"):
            self.assertNotIn("speaker", neutralize_tags(text))

    def test_ordinary_words_are_kept(self):
        self.assertEqual(neutralize_tags("Save 20% on tune-ups & more!"), "Save 20% on tune-ups & more!")
        self.assertEqual(neutralize_tags("Call [today]"), "Call (today)")
        self.assertIn("laughs", neutralize_tags("[laughs]"))


if __name__ == "__main__":
    unittest.main()
