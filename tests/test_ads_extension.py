"""The ads extension inside a real extension host with a fake station: operations, gates, routes, status."""
import json
import threading
import time
import unittest
from unittest import mock

from myradio.ads import extension as extension_module
from myradio.ads.terms import TERMS, TERMS_VERSION
from myradio.extensions import ExtensionError

try:
    from test_ads_support import NOW, Harness, tz_available, utc
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import NOW, Harness, tz_available, utc

JARGON = ("PCM", "GGUF", "SSRF", "Traceback", "NoneType", "KeyError", "TypeError", "ValueError", "sqlite", "Exception")


def reasons(plan):
    return {s["campaign"]: s["reason"] for s in plan["skipped"] if s["creative_id"] is None}


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)

    def test_the_operations_the_brief_asks_for_are_there_and_described(self):
        catalog = {entry["name"]: entry for entry in self.h.host.catalog()}
        for name in ("ads.campaign_save", "ads.campaign_pause", "ads.campaign_resume", "ads.campaign_remove"):
            self.assertIn(name, catalog)
        self.assertEqual(catalog["ads.campaign_save"]["required"], ["campaign"])
        self.assertEqual(catalog["ads.campaign_save"]["arguments"]["campaign"]["type"], "object")
        for entry in catalog.values():
            self.assertLessEqual(len(entry["description"]), 400, entry["name"])
            for word in JARGON:
                self.assertNotIn(word, entry["description"])
        self.assertEqual(sorted(catalog), sorted("ads." + n for n in (
            "campaign_save", "campaign_get", "campaign_pause", "campaign_resume", "campaign_remove", "break_build",
            "spot_speech", "spot_audio", "report")))

    def test_the_routes_the_brief_asks_for_are_there(self):
        for method, path in (("GET", "/campaigns"), ("GET", "/break"), ("GET", "/report"), ("GET", "/report.csv"),
                             ("POST", "/acknowledge"), ("GET", "/acknowledge")):
            self.assertIsNotNone(self.h.host.find_route(method, "/api/x/ads" + path), (method, path))
        self.assertIsNone(self.h.host.find_route("POST", "/api/x/ads/campaigns"))
        self.assertIsNone(self.h.host.find_route("GET", "/api/x/ads/acknowledgements"))

    def test_the_extension_declares_what_belongs_in_a_backup(self):
        self.assertEqual(self.h.ext.persistent_files(), ["ads.sqlite3", "config.json"])
        files = self.h.host.backup_files()["ads"]
        self.assertEqual([p.name for p in files], ["ads.sqlite3"])  # config.json only once the operator has written one

    def test_the_station_never_has_to_start_it_in_a_particular_state(self):
        self.assertEqual(self.h.host.status()["ads"]["state"], "running")
        self.assertEqual(self.h.status()["campaigns"], [])


class ConfirmationsAreNotOperationsTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)

    def test_no_operation_can_set_either_confirmation(self):
        for operation in self.h.ext.operations():
            self.assertFalse({"voice_rights", "commercial_use", "acknowledge", "acknowledged", "confirm"} & set(operation.arguments), operation.name)
            for word in ("acknowledg", "confirm", "agree", "consent"):
                self.assertNotIn(word, operation.name)
        catalog = json.dumps(self.h.host.catalog())
        self.assertNotIn("acknowledge\"", catalog)

    def test_calling_for_one_by_name_is_an_unknown_operation(self):
        for name in ("acknowledge", "voice_rights", "commercial_use", "confirm", "campaign_acknowledge", "set_acknowledgement"):
            for args in ({}, {"voice_rights": True}, {"commercial_use": True}):
                with self.subTest(name=name, args=args), self.assertRaisesRegex(ExtensionError, "Unknown station operation"):
                    self.h.host.call("ads." + name, args)
        self.assertFalse(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])

    def test_a_spot_cannot_carry_a_confirmation_in_with_it(self):
        track = self.h.station.add_track("Bike spot")
        for smuggled in ({"voice_rights": True}, {"commercial_use": True}, {"acknowledgements": {"voice_rights": True}},
                         {"acknowledged": True}, {"confirm": True}):
            with self.subTest(smuggled=smuggled), self.assertRaisesRegex(ExtensionError, "Unknown setting"):
                self.h.call("campaign_save", campaign={"advertiser": "Acme", "creatives": [{"kind": "recorded", "track_id": track}], **smuggled})
        with self.assertRaisesRegex(ExtensionError, "Unknown setting"):
            self.h.call("campaign_save", campaign={"advertiser": "Acme", "creatives": [{"kind": "recorded", "track_id": track, "voice_rights": True}]})
        with self.assertRaises(ExtensionError):  # an unknown argument next to the campaign is refused by the host
            self.h.call("campaign_save", campaign={"advertiser": "x"}, voice_rights=True)
        self.assertEqual(self.h.status()["acknowledgements"]["commercial_use"]["acknowledged"], False)

    def test_the_route_is_the_only_way_and_it_records_a_time(self):
        before = time.time()
        reply = self.h.route("POST", "/acknowledge", body={"voice_rights": True, "version": TERMS_VERSION})
        self.assertEqual(reply.status, 200)
        state = self.h.status()["acknowledgements"]
        self.assertTrue(state["voice_rights"]["acknowledged"])
        self.assertTrue(state["voice_rights"]["at"].startswith("2026-11-05T15:00:00"))  # the harness clock
        self.assertFalse(state["commercial_use"]["acknowledged"])
        self.assertIsNone(state["commercial_use"]["at"])
        self.assertLess(before, time.time())

    def test_the_route_checks_what_it_is_sent(self):
        bad = [({}, "JSON"), (None, "JSON"), ({"voice_rights": "yes"}, "true"), ({"voice_rights": 1}, "true"), ({"everything": True}, "nothing else"),
               ({"version": TERMS_VERSION}, "which confirmation"), ({"voice_rights": True, "version": TERMS_VERSION + 1}, "wording"),
               ({"voice_rights": True, "other": 1}, "nothing else")]
        for body, expected in bad:
            with self.subTest(body=body):
                reply = self.h.route("POST", "/acknowledge", body=body)
                self.assertEqual(reply.status, 400)
                self.assertIn(expected.lower(), reply.body["error"].lower())
        self.assertFalse(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])
        self.assertEqual(self.h.route("POST", "/acknowledge", body={"voice_rights": True, "version": TERMS_VERSION}).status, 200)

    def test_a_confirmation_can_be_withdrawn(self):
        self.h.confirm("voice_rights")
        self.h.route("POST", "/acknowledge", body={"voice_rights": False})
        self.assertFalse(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])

    def test_a_withdrawal_closes_the_gate_even_when_the_computers_clock_was_put_back(self):
        self.h.confirm("voice_rights")
        track = self.h.station.add_track("Promo read")
        promo = self.h.save(house=True, creatives=[{"kind": "script", "text": "Tell a friend."}])
        self.h.call("spot_audio", campaign=promo["id"], creative=promo["creatives"][0]["id"], track_id=track)
        self.assertEqual(len(self.h.call("break_build", seconds=60)["items"]), 1)
        self.h.clock.now = NOW - 30  # the clock is corrected backwards, so the withdrawal is dated before the confirmation
        reply = self.h.route("POST", "/acknowledge", body={"voice_rights": False})
        self.assertEqual((reply.status, reply.body["voice_rights"]["acknowledged"]), (200, False))
        self.assertFalse(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])
        plan = self.h.call("break_build", seconds=60)
        self.assertEqual((plan["items"], reasons(plan)), ([], {"Test FM": "voice_rights_needed"}))
        with self.assertRaisesRegex(ExtensionError, "voice"):
            self.h.call("spot_speech", campaign=promo["id"], creative=promo["creatives"][0]["id"])

    def test_a_confirmation_has_to_say_which_wording_the_person_was_shown(self):
        reply = self.h.route("POST", "/acknowledge", body={"voice_rights": True})
        self.assertEqual(reply.status, 400)
        self.assertIn("version", reply.body["error"])
        self.assertTrue(reply.body["error"].endswith("."))
        for word in JARGON:
            self.assertNotIn(word, reply.body["error"])
        reply = self.h.route("POST", "/acknowledge", body={"voice_rights": False, "commercial_use": True})  # one true is enough
        self.assertEqual(reply.status, 400)
        state = self.h.status()["acknowledgements"]
        self.assertEqual((state["voice_rights"]["acknowledged"], state["commercial_use"]["acknowledged"]), (False, False))
        shown = self.h.route("GET", "/acknowledge").body["version"]  # a screen reads the version from the wording it shows
        self.assertEqual(self.h.route("POST", "/acknowledge", body={"voice_rights": True, "version": shown}).status, 200)
        self.assertTrue(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])

    def test_taking_a_confirmation_back_needs_no_proof_of_reading_but_a_wrong_version_is_still_refused(self):
        self.h.confirm("voice_rights", "commercial_use")
        for body in ({"voice_rights": False}, {"voice_rights": False, "commercial_use": False}):
            with self.subTest(body=body):
                self.assertEqual(self.h.route("POST", "/acknowledge", body=body).status, 200)
        state = self.h.status()["acknowledgements"]
        self.assertEqual((state["voice_rights"]["acknowledged"], state["commercial_use"]["acknowledged"]), (False, False))
        for version in (TERMS_VERSION + 1, 0, "1", None, True, 1.0):
            with self.subTest(version=version):
                reply = self.h.route("POST", "/acknowledge", body={"voice_rights": True, "version": version})
                self.assertEqual(reply.status, 400)
        self.assertFalse(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])

    def test_the_legal_wording_is_never_part_of_the_status_or_the_lists(self):
        self.h.confirm("voice_rights", "commercial_use")
        self.h.save(advertiser="Acme")
        shown = json.dumps(self.h.host.status()) + json.dumps(self.h.route("GET", "/campaigns").body) + json.dumps(self.h.route("GET", "/break").body)
        for kind in TERMS:
            self.assertNotIn(TERMS[kind]["text"], shown)
            self.assertNotIn(TERMS[kind]["text"][:40], shown)
        terms = self.h.route("GET", "/acknowledge")
        self.assertEqual(terms.status, 200)
        self.assertEqual(terms.body["voice_rights"]["text"], TERMS["voice_rights"]["text"])
        self.assertEqual(terms.body["commercial_use"]["acknowledged"], True)
        self.assertEqual(terms.body["version"], TERMS_VERSION)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.track = self.h.station.add_track("Bike spot")

    def paid(self, **extra):
        return {"advertiser": "Corner Bike Co.", "creatives": [{"kind": "recorded", "track_id": self.track}], **extra}

    def test_a_paid_spot_is_refused_until_paid_use_is_confirmed(self):
        with self.assertRaises(ExtensionError) as caught:
            self.h.call("campaign_save", campaign=self.paid())
        message = str(caught.exception)
        self.assertIn("confirmation", message)
        self.assertIn("Open Spots", message)
        for word in JARGON:
            self.assertNotIn(word, message)
        self.assertEqual(self.h.status()["campaigns"], [])
        self.h.confirm("commercial_use")
        self.assertTrue(self.h.call("campaign_save", campaign=self.paid())["created"])

    def test_a_station_promo_needs_no_paid_use_confirmation(self):
        saved = self.h.call("campaign_save", campaign={"house": True, "creatives": [{"kind": "recorded", "track_id": self.track}]})["campaign"]
        self.assertEqual((saved["house"], saved["advertiser"]), (True, "Test FM"))

    def test_a_written_spot_is_refused_until_voice_rights_are_confirmed_even_for_a_promo(self):
        promo = {"house": True, "creatives": [{"kind": "script", "text": "Tell a friend."}]}
        with self.assertRaises(ExtensionError) as caught:
            self.h.call("campaign_save", campaign=promo)
        self.assertIn("voice", str(caught.exception))
        self.h.confirm("commercial_use")  # the wrong confirmation changes nothing
        with self.assertRaises(ExtensionError):
            self.h.call("campaign_save", campaign=promo)
        self.h.confirm("voice_rights")
        self.assertTrue(self.h.call("campaign_save", campaign=promo)["created"])

    def test_a_paid_written_spot_needs_both(self):
        spot = {"advertiser": "Acme", "creatives": [{"kind": "script", "text": "Buy bikes."}]}
        self.h.confirm("voice_rights")
        with self.assertRaisesRegex(ExtensionError, "Paid spots"):
            self.h.call("campaign_save", campaign=spot)
        self.h.confirm("commercial_use")
        self.assertTrue(self.h.call("campaign_save", campaign=spot)["created"])

    def test_a_recorded_spot_never_needs_the_voice_confirmation(self):
        self.h.confirm("commercial_use")
        self.assertTrue(self.h.call("campaign_save", campaign=self.paid())["created"])
        self.assertFalse(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"])

    def test_nothing_is_generated_for_the_voice_until_rights_are_confirmed(self):
        self.h.confirm("commercial_use")
        self.h.confirm("voice_rights")
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        creative = saved["creatives"][0]["id"]
        self.h.route("POST", "/acknowledge", body={"voice_rights": False})
        for operation, args in (("spot_speech", {}), ("spot_audio", {"track_id": self.track})):
            with self.subTest(operation=operation), self.assertRaisesRegex(ExtensionError, "voice"):
                self.h.call(operation, campaign=saved["id"], creative=creative, **args)
        with self.assertRaisesRegex(ExtensionError, "voice"):  # nor can the spot be saved again with its script
            self.h.call("campaign_save", campaign={"id": saved["id"], "priority": 7})
        self.h.confirm("voice_rights")
        speech = self.h.call("spot_speech", campaign=saved["id"], creative=creative)
        self.assertEqual(speech["role"], "spot")

    def test_a_paid_written_spot_gets_neither_words_nor_a_voice_link_while_paid_use_is_withdrawn(self):
        self.h.confirm("voice_rights", "commercial_use")
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        creative = saved["creatives"][0]["id"]
        self.h.route("POST", "/acknowledge", body={"commercial_use": False})
        for operation, args in (("spot_speech", {}), ("spot_audio", {"track_id": self.track})):
            with self.subTest(operation=operation), self.assertRaisesRegex(ExtensionError, "Paid spots"):
                self.h.call(operation, campaign=saved["id"], creative=creative, **args)
        self.assertIsNone(self.h.call("campaign_get", campaign=saved["id"])["campaign"]["creatives"][0]["track_id"])
        self.h.confirm("commercial_use")
        self.assertEqual(self.h.call("spot_speech", campaign=saved["id"], creative=creative)["role"], "spot")

    def test_revoking_paid_use_stops_a_paid_spot_from_airing_but_keeps_it(self):
        self.h.confirm("commercial_use")
        self.h.save(advertiser="Acme")
        self.assertEqual(len(self.h.call("break_build", seconds=60)["items"]), 1)
        self.h.route("POST", "/acknowledge", body={"commercial_use": False})
        plan = self.h.call("break_build", seconds=60)
        self.assertEqual((plan["items"], reasons(plan)), ([], {"Acme": "commercial_ack_needed"}))
        self.assertEqual(self.h.status()["running"], 1)
        view = self.h.call("campaign_get", campaign=self.h.status()["campaigns"][0]["id"])["campaign"]
        self.assertEqual(view["waiting_for"], ["Waiting for your paid-use confirmation."])

    def test_revoking_voice_rights_holds_back_a_written_spot_that_is_already_saved(self):
        self.h.confirm("voice_rights", "commercial_use")
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        self.h.call("spot_audio", campaign=saved["id"], creative=saved["creatives"][0]["id"], track_id=self.track)
        self.assertEqual(len(self.h.call("break_build", seconds=60)["items"]), 1)
        self.h.route("POST", "/acknowledge", body={"voice_rights": False})
        plan = self.h.call("break_build", seconds=60)
        self.assertEqual(reasons(plan), {"Acme": "voice_rights_needed"})
        self.assertEqual(self.h.call("campaign_get", campaign=saved["id"])["campaign"]["creatives"][0]["problem"],
                         "Waiting for your voice-rights confirmation.")


class StationPromoNamingTests(unittest.TestCase):
    """"Station promo" is how a spot skips the paid-use confirmation, so it may only carry the station's own name.
    Otherwise one extra field would file any business as a promo, and its plays would be reported as free."""

    def setUp(self):
        self.h = Harness(self)
        self.h.station.name = "Garden FM"
        self.track = self.h.station.add_track("Promo read")

    def promo(self, **extra):
        return {"house": True, "creatives": [{"kind": "recorded", "track_id": self.track}], **extra}

    def test_a_business_cannot_be_filed_as_a_station_promo(self):
        for confirmed in (False, True):  # the refusal is about the name, so it is the same with or without the paid-use confirmation
            if confirmed:
                self.h.confirm("commercial_use")
            with self.subTest(confirmed=confirmed):
                for spot in (self.promo(advertiser="Acme Bikes"), self.promo(advertiser={"name": "Acme Bikes", "category": "bikes"}),
                             self.promo(advertiser="Garden FM Bikes"), self.promo(advertiser="Garden")):
                    with self.assertRaises(ExtensionError) as caught:
                        self.h.call("campaign_save", campaign=spot)
                    message = str(caught.exception)
                    self.assertIn("station promo", message)
                    self.assertIn("paid", message)
                    for word in JARGON:
                        self.assertNotIn(word, message)
                self.assertEqual(self.h.status()["campaigns"], [])

    def test_a_promo_carries_the_name_of_the_station_however_it_is_written(self):
        cases = ((None, "Garden FM"), ("Garden FM", "Garden FM"), ("garden fm", "garden fm"), ("  GARDEN   FM ", "GARDEN FM"),
                 ({"name": "Garden FM", "category": "own"}, "Garden FM"))
        for index, (advertiser, stored) in enumerate(cases):
            with self.subTest(advertiser=advertiser):
                track = self.h.station.add_track(f"Promo cut {index}")
                spot = {"house": True, "creatives": [{"kind": "recorded", "track_id": track}]}
                if advertiser is not None:
                    spot["advertiser"] = advertiser
                saved = self.h.call("campaign_save", campaign=spot)["campaign"]
                self.assertEqual((saved["house"], saved["advertiser"], saved["waiting_for"]), (True, stored, []))
        self.assertEqual(self.h.status()["campaigns_total"], len(cases))

    def test_a_paid_spot_cannot_be_turned_into_a_promo_by_editing_it(self):
        self.h.confirm("commercial_use")
        paid = self.h.call("campaign_save", campaign={"advertiser": "Acme Bikes", "creatives": [{"kind": "recorded", "track_id": self.track}]})["campaign"]
        for change in ({"house": True}, {"house": True, "advertiser": "Acme Bikes"}, {"house": True, "advertiser": {"name": "Acme Bikes"}}):
            with self.subTest(change=change), self.assertRaisesRegex(ExtensionError, "station promo"):
                self.h.call("campaign_save", campaign={"id": paid["id"], **change})
        again = self.h.call("campaign_get", campaign=paid["id"])["campaign"]
        self.assertEqual((again["house"], again["advertiser"]), (False, "Acme Bikes"))
        self.h.route("POST", "/acknowledge", body={"commercial_use": False})  # and withdrawing paid use still holds it back
        self.assertEqual({s["campaign"]: s["reason"] for s in self.h.call("break_build", seconds=60)["skipped"]}, {"Acme Bikes": "commercial_ack_needed"})

    def test_a_promo_cannot_be_renamed_to_a_business_but_may_be_named_after_the_station(self):
        saved = self.h.call("campaign_save", campaign=self.promo())["campaign"]
        with self.assertRaisesRegex(ExtensionError, "station promo"):
            self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "Acme Bikes"})
        renamed = self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "GARDEN FM"})["campaign"]
        self.assertEqual((renamed["house"], renamed["advertiser"]), (True, "GARDEN FM"))

    def test_a_promo_made_under_an_earlier_station_name_can_still_be_edited(self):
        saved = self.h.call("campaign_save", campaign=self.promo())["campaign"]
        self.h.station.name = "River Radio"  # the station was renamed afterwards
        edited = self.h.call("campaign_save", campaign={"id": saved["id"], "priority": 8})["campaign"]
        self.assertEqual((edited["priority"], edited["advertiser"], edited["house"]), (8, "Garden FM", True))
        everything = self.h.call("campaign_get", campaign=saved["id"])["campaign"]
        for key in ("state", "schedule_text", "plays_last_7_days", "last_played_at", "created_at", "waiting_for", "problem"):
            everything.pop(key)
        everything["creatives"] = [{k: v for k, v in c.items() if k in ("id", "kind", "title", "track_id")} for c in everything["creatives"]]
        self.assertEqual(self.h.call("campaign_save", campaign=everything)["campaign"]["advertiser"], "Garden FM")  # sent back whole
        with self.assertRaisesRegex(ExtensionError, "station promo"):
            self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "Acme Bikes"})
        self.assertEqual(self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "River Radio"})["campaign"]["advertiser"], "River Radio")

    def test_a_promo_turned_into_a_paid_spot_is_held_to_the_paid_rule(self):
        saved = self.h.call("campaign_save", campaign=self.promo())["campaign"]
        with self.assertRaisesRegex(ExtensionError, "Paid spots"):
            self.h.call("campaign_save", campaign={"id": saved["id"], "house": False, "advertiser": "Acme Bikes"})

    def test_the_paid_use_refusal_does_not_tell_a_tool_how_to_skip_it(self):
        with self.assertRaises(ExtensionError) as caught:
            self.h.call("campaign_save", campaign={"advertiser": "Acme Bikes", "creatives": [{"kind": "recorded", "track_id": self.track}]})
        message = str(caught.exception).lower()
        self.assertIn("paid spots need one confirmation", message)
        for hint in ("promo", "house", "do not need", "don't need", "exempt"):
            self.assertNotIn(hint, message)
        from myradio.ads.terms import NEEDS_COMMERCIAL_USE
        self.assertNotIn("promo", NEEDS_COMMERCIAL_USE.lower())


class SpokenScriptTests(unittest.TestCase):
    """What the voice is asked to read. Everything that reaches a speech job passes through here."""

    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("voice_rights", "commercial_use")

    def speech(self, copy, advertiser="Corner Bike Co.", **creative):
        saved = self.h.save(advertiser=advertiser, creatives=[{"kind": "script", "text": copy, **creative}])
        return self.h.call("spot_speech", campaign=saved["id"], creative=saved["creatives"][0]["id"])

    def test_the_words_for_the_voice_start_with_the_sponsorship_line(self):
        reply = self.speech("Fixing bikes since 1998. Mention the station for a free tune-up.", voice="dex")
        self.assertEqual(reply["text"], "This message is sponsored by Corner Bike Co. Fixing bikes since 1998. Mention the station for a free tune-up.")
        self.assertEqual((reply["role"], reply["sponsor"], reply["voice"], reply["includes_sponsorship_line"]), ("spot", "Corner Bike Co.", "dex", True))
        self.assertGreater(reply["estimated_seconds"], 5)
        self.assertEqual(len(reply["campaign"]), 32)
        self.assertEqual(len(reply["creative"]), 32)

    def test_every_spoken_message_carries_the_line_whatever_it_says(self):
        for number, copy in enumerate(("Buy bikes.", "Sponsored by nobody at all.", "x", "It is not sponsored by anyone, honest."), start=1):
            with self.subTest(copy=copy):
                text = self.speech(copy, advertiser=f"Acme {number}")["text"]  # a fixed name: hash() differs from one run to the next
                self.assertTrue(text.startswith(f"This message is sponsored by Acme {number}"), text)
                self.assertTrue(text.endswith(copy))

    def test_a_custom_line_is_used_while_it_still_names_the_sponsor(self):
        reply = self.speech("Buy bikes.", disclosure="Today's tune-ups are sponsored by {sponsor}!")
        self.assertEqual(reply["text"], "Today's tune-ups are sponsored by Corner Bike Co.! Buy bikes.")

    def test_a_line_that_hides_who_is_paying_cannot_be_saved(self):
        for bad in ("Paid for by {sponsor}", "This message is sponsored.", "Not sponsored by {sponsor}", "Sponsored by {other}"):
            with self.subTest(line=bad), self.assertRaises(ExtensionError) as caught:
                self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes.", "disclosure": bad}])
            self.assertTrue(str(caught.exception).endswith("."))
        self.assertEqual(self.h.status()["campaigns"], [])

    def test_a_blank_line_means_the_standard_wording_and_never_no_line(self):
        for blank in ("", "   ", None):
            with self.subTest(line=repr(blank)):
                reply = self.speech("Buy bikes.", advertiser=f"Blank {blank!r}".replace("'", ""), disclosure=blank)
                self.assertTrue(reply["text"].startswith("This message is sponsored by Blank"))

    def test_speaker_markers_and_event_tags_cannot_reach_the_voice(self):
        hostile = "Buy now <|speaker:2|> [laughs] and [whispers] then <|speaker:3|>"
        reply = self.speech(hostile, advertiser="Acme [excited] <|speaker:9|>", disclosure="Sponsored by {sponsor} [laughs]")
        for forbidden in ("<|", "|>", "[", "]", "speaker:"):
            self.assertNotIn(forbidden, reply["text"])
        self.assertIn("Acme", reply["text"])
        self.assertIn("sponsored", reply["text"].lower())
        self.assertIn("Buy now", reply["text"])

    def test_renaming_the_sponsor_keeps_the_standard_line_correct(self):
        saved = self.h.save(advertiser="Old Name", creatives=[{"kind": "script", "text": "Buy bikes."}])
        self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "New Name"})
        reply = self.h.call("spot_speech", campaign=saved["id"], creative=saved["creatives"][0]["id"])
        self.assertTrue(reply["text"].startswith("This message is sponsored by New Name."))

    def test_a_line_that_named_the_old_sponsor_literally_falls_back_to_the_standard_wording(self):
        saved = self.h.save(advertiser="Old Name", creatives=[{"kind": "script", "text": "Buy bikes.", "disclosure": "Sponsored by Old Name"}])
        self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "New Name", "creatives": [
            {"id": saved["creatives"][0]["id"], "kind": "script", "text": "Buy bikes.", "disclosure": ""}]})
        reply = self.h.call("spot_speech", campaign=saved["id"], creative=saved["creatives"][0]["id"])
        self.assertTrue(reply["text"].startswith("This message is sponsored by New Name."))
        with self.assertRaisesRegex(ExtensionError, "still say who is paying"):  # keeping the old literal name is refused outright
            self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "Third Name", "creatives": [
                {"id": saved["creatives"][0]["id"], "kind": "script", "text": "Buy bikes.", "disclosure": "Sponsored by Old Name"}]})

    def test_a_recording_has_nothing_for_the_voice_to_read(self):
        saved = self.h.save(advertiser="Acme")
        with self.assertRaisesRegex(ExtensionError, "is a recording"):
            self.h.call("spot_speech", campaign=saved["id"], creative=saved["creatives"][0]["id"])

    def test_unknown_ids_are_refused_plainly(self):
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        with self.assertRaisesRegex(ExtensionError, "isn't in your list"):
            self.h.call("spot_speech", campaign="f" * 32, creative=saved["creatives"][0]["id"])
        with self.assertRaisesRegex(ExtensionError, "isn't part of this spot"):
            self.h.call("spot_speech", campaign=saved["id"], creative="f" * 32)

    def test_a_house_promo_is_read_with_the_line_too(self):
        saved = self.h.save(house=True, creatives=[{"kind": "script", "text": "Tell a friend about us."}])
        reply = self.h.call("spot_speech", campaign=saved["id"], creative=saved["creatives"][0]["id"])
        self.assertEqual(reply["text"], "This message is sponsored by Test FM. Tell a friend about us.")

    def test_attaching_audio_only_works_for_written_messages_and_each_recording_once(self):
        track = self.h.station.add_track("Read by the voice")
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        creative = saved["creatives"][0]["id"]
        recorded = self.h.save(advertiser="Other")
        with self.assertRaisesRegex(ExtensionError, "Only written messages"):
            self.h.call("spot_audio", campaign=recorded["id"], creative=recorded["creatives"][0]["id"], track_id=track)
        self.h.call("spot_audio", campaign=saved["id"], creative=creative, track_id=track)
        second = self.h.save(advertiser="Third", creatives=[{"kind": "script", "text": "More bikes."}])
        with self.assertRaisesRegex(ExtensionError, "already used by another spot"):
            self.h.call("spot_audio", campaign=second["id"], creative=second["creatives"][0]["id"], track_id=track)
        with self.assertRaisesRegex(ExtensionError, "isn't in your library"):
            self.h.call("spot_audio", campaign=saved["id"], creative=creative, track_id="0" * 32)


class StaleRecordingTests(unittest.TestCase):
    """A voice recording is one exact reading of the sponsorship line and the copy. When the words or the sponsor
    change it must not keep airing under the new words, and an edit that does not touch the words must not lose it."""

    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("voice_rights", "commercial_use")
        saved = self.h.save(advertiser="Old Name", creatives=[{"kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex"}])
        self.spot, self.creative = saved["id"], saved["creatives"][0]["id"]
        self.voice = self.h.station.add_track("Read by the voice", duration=12.0)
        self.h.call("spot_audio", campaign=self.spot, creative=self.creative, track_id=self.voice)

    def message(self):
        return self.h.call("campaign_get", campaign=self.spot)["campaign"]["creatives"][0]

    def airs(self):
        return [i["track_id"] for i in self.h.call("break_build", seconds=60)["items"]]

    def edit(self, **creative):
        body = {"id": self.creative, "kind": "script", "text": "Fixing bikes since 1998.", **creative}
        return self.h.call("campaign_save", campaign={"id": self.spot, "creatives": [body]})

    def assertRecordingKept(self, reply=None):
        self.assertEqual((self.message()["track_id"], self.message()["ready"], self.airs()), (self.voice, True, [self.voice]))
        if reply is not None:
            self.assertEqual(reply["warnings"], [])

    def assertRecordingTakenOff(self, reply):
        self.assertEqual((self.message()["track_id"], self.message()["ready"], self.airs()), (None, False, []))
        self.assertEqual(self.message()["problem"], "The voice recording isn't made yet.")
        [warning] = reply["warnings"]
        self.assertIn("voice recording was taken off", warning)
        self.assertIn("Make a new recording", warning)
        for word in JARGON:
            self.assertNotIn(word, warning)

    def test_the_recording_airs_before_any_edit(self):
        self.assertRecordingKept()

    def test_renaming_the_sponsor_takes_the_old_recording_off(self):
        reply = self.h.call("campaign_save", campaign={"id": self.spot, "advertiser": "New Name"})
        self.assertRecordingTakenOff(reply)
        self.assertIn("This message is sponsored by New Name.", self.message()["spoken_text"])

    def test_editing_the_copy_takes_it_off(self):
        self.assertRecordingTakenOff(self.edit(text="Fixing bikes since 1999."))

    def test_editing_the_copy_takes_it_off_even_when_the_recording_is_sent_back_with_it(self):
        # This is how an editor behaves: the message comes back exactly as it was read, with only the words changed.
        self.assertRecordingTakenOff(self.edit(text="Fixing bikes since 1999.", track_id=self.voice))

    def test_changing_the_sponsorship_line_takes_it_off(self):
        self.assertRecordingTakenOff(self.edit(disclosure="Today's repairs are sponsored by {sponsor}", voice="dex"))

    def test_changing_the_voice_takes_it_off(self):
        self.assertRecordingTakenOff(self.edit(voice="alex"))

    def test_an_edit_that_leaves_the_words_alone_keeps_it_with_or_without_the_recording_in_the_request(self):
        self.assertRecordingKept(self.edit(title="Bike repair"))  # nothing about the recording or the voice is sent
        self.assertEqual(self.message()["title"], "Bike repair")
        self.assertEqual(self.h.call("campaign_get", campaign=self.spot)["campaign"]["creatives"][0]["voice"], "dex")  # the voice it was made with
        self.assertRecordingKept(self.edit(title="Bike repair again", track_id=self.voice, voice="dex"))
        self.assertRecordingKept(self.h.call("campaign_save", campaign={"id": self.spot, "priority": 8}))
        self.assertRecordingKept(self.h.call("campaign_save", campaign={"id": self.spot, "advertiser": "Old Name"}))  # same name again

    def test_a_different_recording_named_in_the_edit_is_kept_as_the_new_voice_recording(self):
        newer = self.h.station.add_track("Read again", duration=13.0)
        reply = self.edit(text="Fixing bikes since 1999.", track_id=newer)
        self.assertEqual(reply["warnings"], [])
        self.assertEqual((self.message()["track_id"], self.message()["ready"]), (newer, True))

    def test_a_recording_taken_off_can_be_attached_again_and_the_old_file_stays_in_the_library(self):
        self.edit(text="Fixing bikes since 1999.")
        self.assertTrue(any(t["id"] == self.voice for t in self.h.station.library))
        self.h.call("spot_audio", campaign=self.spot, creative=self.creative, track_id=self.voice)  # e.g. a new reading, imported again
        self.assertEqual(self.message()["track_id"], self.voice)

    def test_turning_a_recorded_message_into_a_written_one_does_not_make_that_recording_its_voice(self):
        other = self.h.station.add_track("A recorded ad")
        saved = self.h.save(advertiser="Mixed", creatives=[{"kind": "recorded", "track_id": other}])
        creative = saved["creatives"][0]["id"]
        reply = self.h.call("campaign_save", campaign={"id": saved["id"], "creatives": [
            {"id": creative, "kind": "script", "text": "Now it is written.", "track_id": other}]})
        view = reply["campaign"]["creatives"][0]
        self.assertEqual((view["kind"], view["track_id"], view["ready"]), ("script", None, False))
        self.assertEqual(len(reply["warnings"]), 1)

    def test_other_messages_of_the_spot_are_judged_one_by_one(self):
        second = self.h.call("campaign_save", campaign={"id": self.spot, "creatives": [
            {"id": self.creative, "kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex"},
            {"kind": "script", "text": "A second message."}]})["campaign"]["creatives"][1]["id"]
        other = self.h.station.add_track("Second reading", duration=9.0)
        self.h.call("spot_audio", campaign=self.spot, creative=second, track_id=other)
        reply = self.h.call("campaign_save", campaign={"id": self.spot, "creatives": [
            {"id": self.creative, "kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex"},
            {"id": second, "kind": "script", "text": "A changed second message."}]})
        tracks = {c["id"]: c["track_id"] for c in reply["campaign"]["creatives"]}
        self.assertEqual(tracks, {self.creative: self.voice, second: None})
        self.assertEqual(len(reply["warnings"]), 1)
        self.assertIn("A changed second message", reply["warnings"][0])


class NamelessSponsorTests(unittest.TestCase):
    """The spoken line has to name who is paying. A name that is empty once speech markers are taken out names nobody."""

    NAMELESS = ("<|speaker:1|>", "<|speaker:1|><|speaker:2|>", "[ ]", "( )", "...", "\U0001F6B2")

    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("voice_rights", "commercial_use")

    def test_a_written_spot_for_a_nameless_sponsor_is_refused_in_plain_words(self):
        for name in self.NAMELESS:
            with self.subTest(name=name), self.assertRaises(ExtensionError) as caught:
                self.h.save(advertiser=name, creatives=[{"kind": "script", "text": "Buy bikes."}])
            message = str(caught.exception)
            self.assertIn("letters or numbers", message)
            self.assertTrue(message.endswith("."))
            for word in JARGON:
                self.assertNotIn(word, message)
        self.assertEqual(self.h.status()["campaigns"], [])

    def test_names_in_other_scripts_and_with_tags_around_a_real_name_are_fine(self):
        for index, name in enumerate(("Acme <|speaker:1|>", "[Acme]", "自転車屋", "7-Eleven", "A")):
            with self.subTest(name=name):
                saved = self.h.save(advertiser=name, creatives=[{"kind": "script", "text": f"Buy bikes {index}."}])
                speech = self.h.call("spot_speech", campaign=saved["id"], creative=saved["creatives"][0]["id"])
                self.assertTrue(speech["text"].startswith("This message is sponsored by "))
                self.assertNotIn("sponsored by .", speech["text"])

    def test_a_recording_has_nothing_to_read_so_its_sponsor_name_is_not_held_to_it(self):
        saved = self.h.save(advertiser="<|speaker:1|>", creatives=[{"kind": "recorded", "track_id": self.h.station.add_track("A spot")}])
        self.assertTrue(saved["id"])

    def test_a_station_promo_needs_a_station_name_the_voice_can_read(self):
        self.h.station.name = "<|speaker:1|>"
        with self.assertRaises(ExtensionError) as caught:
            self.h.save(house=True, creatives=[{"kind": "script", "text": "Tell a friend."}])
        self.assertIn("station's name", str(caught.exception))

    def test_a_spot_saved_before_the_rule_still_shows_but_gets_no_words_for_the_voice(self):
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        with self.h.ext._lock, self.h.ext._store.conn:  # as an older version would have stored it
            self.h.ext._store.conn.execute("UPDATE campaigns SET advertiser='<|speaker:1|>' WHERE id=?", (saved["id"],))
        self.h.ext._reload()
        self.assertEqual(self.h.status()["campaigns_total"], 1)  # it did not vanish
        view = self.h.call("campaign_get", campaign=saved["id"])["campaign"]
        self.assertEqual(view["advertiser"], "<|speaker:1|>")
        self.assertEqual(len(self.h.call("break_build", seconds=60, ready_only=False)["items"]), 1)  # previews do not fail either
        for operation, args in (("spot_speech", {}), ("spot_audio", {"track_id": self.h.station.add_track("Voice")})):
            with self.subTest(operation=operation), self.assertRaises(ExtensionError) as caught:
                self.h.call(operation, campaign=saved["id"], creative=saved["creatives"][0]["id"], **args)
            self.assertIn("letters or numbers", str(caught.exception))
        with self.assertRaisesRegex(ExtensionError, "letters or numbers"):  # and editing it needs a name
            self.h.call("campaign_save", campaign={"id": saved["id"], "priority": 3})
        self.assertEqual(self.h.call("campaign_save", campaign={"id": saved["id"], "advertiser": "Acme"})["campaign"]["advertiser"], "Acme")


class SaveAndEditTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("voice_rights", "commercial_use")

    def test_a_new_spot_gets_its_defaults_a_recording_title_and_a_reminder(self):
        track = self.h.station.add_track("Tune-up offer")
        reply = self.h.call("campaign_save", campaign={"advertiser": "Corner Bike Co.", "timezone": "America/New_York",
                                                       "creatives": [{"kind": "recorded", "track_id": track}]})
        spot = reply["campaign"]
        self.assertEqual((reply["created"], len(spot["id"])), (True, 32))
        self.assertEqual((spot["name"], spot["state"], spot["frequency"], spot["pacing"], spot["priority"]), ("Corner Bike Co.", "running", "regular", "even", 5))
        self.assertEqual(spot["creatives"][0]["title"], "Tune-up offer")
        self.assertEqual((spot["creatives"][0]["ready"], spot["creatives"][0]["problem"], spot["creatives"][0]["duration"]), (True, None, 30.0))
        self.assertEqual(spot["schedule_text"], "Every day, from Nov 5")
        self.assertEqual(spot["flights"][0]["start"], "2026-11-05")  # today's date in the spot's own timezone (10:00 in New York)
        self.assertTrue(any("exactly as recorded" in w for w in reply["warnings"]))
        self.assertEqual((spot["plays_last_7_days"], spot["last_played_at"], spot["waiting_for"]), (0, None, []))

    def test_the_start_date_is_today_where_the_spot_is_not_where_the_server_is(self):
        track = self.h.station.add_track("Late offer")
        self.h.clock.now = utc(2026, 11, 5, 23, 30)  # already the 6th in Tokyo, still the 5th in New York
        tokyo = self.h.call("campaign_save", campaign={"advertiser": "T", "timezone": "Asia/Tokyo", "creatives": [{"kind": "recorded", "track_id": track}]})
        self.assertEqual(tokyo["campaign"]["flights"][0]["start"], "2026-11-06")

    def test_a_written_spot_shows_exactly_what_the_voice_will_read(self):
        reply = self.h.call("campaign_save", campaign={"advertiser": "Corner Bike Co.", "creatives": [{"kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex"}]})
        creative = reply["campaign"]["creatives"][0]
        self.assertEqual(creative["spoken_text"], "This message is sponsored by Corner Bike Co. Fixing bikes since 1998.")
        self.assertEqual((creative["disclosure"], creative["voice"], creative["ready"], creative["problem"]),
                         ("This message is sponsored by {sponsor}", "dex", False, "The voice recording isn't made yet."))
        self.assertGreaterEqual(creative["estimated_seconds"], 3.0)

    def test_changing_one_thing_keeps_the_rest(self):
        spot = self.h.save(advertiser="Acme", priority=3, category="bikes", plan={"per_day": 4}, timezone="Europe/London",
                           flights=[{"start": "2026-11-01", "end": "2026-12-31", "days": "1,2,3,4,5"}])
        reply = self.h.call("campaign_save", campaign={"id": spot["id"], "priority": 9})
        again = reply["campaign"]
        self.assertFalse(reply["created"])
        self.assertEqual(again["priority"], 9)
        for key in ("id", "name", "advertiser", "category", "plan", "timezone", "flights", "caps", "separation", "creatives", "pacing"):
            self.assertEqual(again[key], spot[key], key)
        self.assertEqual(self.h.status()["campaigns_total"], 1)

    def test_changing_how_often_replaces_the_limits_that_came_with_the_old_preset(self):
        spot = self.h.save(advertiser="Acme", frequency="light")
        self.assertEqual(spot["caps"]["per_hour"], 1)
        often = self.h.call("campaign_save", campaign={"id": spot["id"], "frequency": "often"})["campaign"]
        self.assertEqual((often["frequency"], often["caps"]["per_hour"], often["separation"]["campaign_minutes"]), ("often", 3, 10))
        custom = self.h.call("campaign_save", campaign={"id": spot["id"], "caps": {"per_hour": 5}})["campaign"]
        self.assertEqual((custom["frequency"], custom["caps"]["per_hour"], custom["caps"]["per_day"]), ("custom", 5, 20))

    def test_changing_the_category_through_the_advertiser_object(self):
        spot = self.h.save(advertiser="Acme", category="bikes")
        moved = self.h.call("campaign_save", campaign={"id": spot["id"], "advertiser": {"name": "Acme", "category": "Coffee Shops"}})["campaign"]
        self.assertEqual(moved["category"], "coffee-shops")

    def test_replacing_the_messages_keeps_the_ids_of_those_that_stay(self):
        a, b = self.h.station.add_track("A"), self.h.station.add_track("B")
        spot = self.h.save(advertiser="Acme", creatives=[{"kind": "recorded", "track_id": a}, {"kind": "recorded", "track_id": b}])
        keep = spot["creatives"][1]["id"]
        edited = self.h.call("campaign_save", campaign={"id": spot["id"], "creatives": [{"id": keep, "kind": "recorded", "track_id": b, "title": "Renamed"}]})["campaign"]
        self.assertEqual([(c["id"], c["title"]) for c in edited["creatives"]], [(keep, "Renamed")])
        with self.assertRaisesRegex(ExtensionError, "isn't part of this spot"):
            self.h.call("campaign_save", campaign={"id": spot["id"], "creatives": [{"id": spot["creatives"][0]["id"], "kind": "recorded", "track_id": a}]})

    def test_editing_a_spot_that_is_not_there_is_refused_plainly(self):
        for bad in ("f" * 32, "nope", "", 5, None):
            with self.subTest(id=bad):
                if bad is None:
                    continue
                with self.assertRaisesRegex(ExtensionError, "isn't in your list any more"):
                    self.h.call("campaign_save", campaign={"id": bad, "priority": 2})
        spot = self.h.save(advertiser="Acme")
        self.h.call("campaign_remove", campaign=spot["id"])
        with self.assertRaisesRegex(ExtensionError, "isn't in your list any more"):
            self.h.call("campaign_save", campaign={"id": spot["id"], "priority": 2})

    def test_recordings_are_checked_against_the_library(self):
        music = self.h.station.add_track("A song", kind="music")
        speech = self.h.station.add_track("A link", kind="speech")
        for track, expected in (("0" * 32, "isn't in your library"), (music, "as music, not as an ad"), (speech, "as speech, not as an ad")):
            with self.subTest(track=track), self.assertRaisesRegex(ExtensionError, expected):
                self.h.call("campaign_save", campaign={"advertiser": "Acme", "creatives": [{"kind": "recorded", "track_id": track}]})
        self.assertEqual(self.h.status()["campaigns"], [])

    def test_a_recording_can_serve_one_spot_only(self):
        track = self.h.station.add_track("Shared")
        self.h.save(advertiser="First", creatives=[{"kind": "recorded", "track_id": track}])
        with self.assertRaisesRegex(ExtensionError, "already used by \"First\""):
            self.h.call("campaign_save", campaign={"advertiser": "Second", "creatives": [{"kind": "recorded", "track_id": track}]})
        self.assertEqual(self.h.status()["campaigns_total"], 1)

    def test_a_recording_is_checked_again_when_it_is_attached_later(self):
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        creative = saved["creatives"][0]["id"]
        music = self.h.station.add_track("A song", kind="music")
        with self.assertRaisesRegex(ExtensionError, "not as an ad"):
            self.h.call("spot_audio", campaign=saved["id"], creative=creative, track_id=music)

    def test_the_number_of_spots_is_bounded(self):
        with mock.patch.object(extension_module, "MAX_CAMPAIGNS", 3):
            for i in range(3):
                self.h.save(advertiser=f"S{i}")
            with self.assertRaisesRegex(ExtensionError, "most this station keeps"):
                self.h.save(advertiser="One too many")
            first = self.h.status()["campaigns"][0]["id"]
            self.h.call("campaign_remove", campaign=first)
            self.h.save(advertiser="Room again")

    def test_a_spot_is_saved_paused_or_resumed_with_the_save_too(self):
        spot = self.h.save(advertiser="Acme", paused=True)
        self.assertEqual((spot["paused"], spot["state"]), (True, "paused"))
        self.assertTrue(self.h.call("campaign_save", campaign={"id": spot["id"], "priority": 4})["campaign"]["paused"])  # kept when not mentioned
        self.assertFalse(self.h.call("campaign_save", campaign={"id": spot["id"], "paused": False})["campaign"]["paused"])

    def test_the_station_name_names_a_promo_that_has_no_advertiser(self):
        self.h.station.name = "Garden FM"
        track = self.h.station.add_track("Promo")
        spot = self.h.call("campaign_save", campaign={"house": True, "creatives": [{"kind": "recorded", "track_id": track}]})["campaign"]
        self.assertEqual((spot["advertiser"], spot["name"], spot["frequency"]), ("Garden FM", "Garden FM", "light"))

    def test_saving_does_not_work_while_the_library_cannot_be_read(self):
        track = self.h.station.add_track("Spot")
        self.h.station.fail = True
        with self.assertRaisesRegex(ExtensionError, "isn't answering"):
            self.h.call("campaign_save", campaign={"advertiser": "Acme", "creatives": [{"kind": "recorded", "track_id": track}]})
        self.h.station.fail = False
        self.assertEqual(self.h.status()["campaigns"], [])


class PauseResumeRemoveTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("commercial_use")
        self.spot = self.h.save(advertiser="Acme")
        self.other = self.h.save(advertiser="Other")

    def test_pause_and_resume_change_the_state_and_the_counts(self):
        self.assertEqual({k: self.h.status()[k] for k in ("running", "scheduled", "paused", "ended")}, {"running": 2, "scheduled": 0, "paused": 0, "ended": 0})
        reply = self.h.call("campaign_pause", campaign=self.spot["id"])
        self.assertEqual((reply["state"], reply["name"]), ("paused", "Acme"))
        self.assertEqual({k: self.h.status()[k] for k in ("running", "paused")}, {"running": 1, "paused": 1})
        self.assertEqual(self.h.call("campaign_pause", campaign=self.spot["id"])["state"], "paused")  # asking twice is harmless
        plan = self.h.call("break_build", seconds=120)
        self.assertEqual([i["campaign"] for i in plan["items"]], ["Other"])
        self.assertEqual(reasons(plan), {"Acme": "paused"})
        self.assertEqual(self.h.call("campaign_resume", campaign=self.spot["id"])["state"], "running")
        self.assertEqual(self.h.status()["paused"], 0)
        self.assertEqual(len(self.h.call("break_build", seconds=120, seed="x")["items"]), 2)

    def test_remove_takes_the_spot_out_for_good(self):
        reply = self.h.call("campaign_remove", campaign=self.spot["id"])
        self.assertEqual((reply["removed"], reply["name"]), (True, "Acme"))
        self.assertEqual([c["name"] for c in self.h.status()["campaigns"]], ["Other"])
        self.assertEqual([c["name"] for c in self.h.route("GET", "/campaigns").body["campaigns"]], ["Other"])
        for operation in ("campaign_remove", "campaign_pause", "campaign_resume", "campaign_get"):
            with self.subTest(operation=operation), self.assertRaisesRegex(ExtensionError, "isn't in your list any more"):
                self.h.call(operation, campaign=self.spot["id"])

    def test_unknown_or_malformed_ids_are_refused_plainly(self):
        for operation in ("campaign_remove", "campaign_pause", "campaign_resume", "campaign_get"):
            for bad in ("f" * 32, "not-an-id", "DROP TABLE campaigns", "../etc", "A" * 32):
                with self.subTest(operation=operation, id=bad), self.assertRaisesRegex(ExtensionError, "isn't in your list any more"):
                    self.h.call(operation, campaign=bad)
        with self.assertRaises(ExtensionError):
            self.h.call("campaign_pause", campaign="")
        with self.assertRaisesRegex(ExtensionError, "characters"):
            self.h.call("campaign_pause", campaign="a" * 40)
        self.assertEqual(self.h.status()["campaigns_total"], 2)

    def test_the_status_counts_move_with_the_calendar_without_asking_the_station(self):
        later = self.h.save(advertiser="Later", flights=[{"start": "2026-11-10", "end": "2026-11-12"}])
        self.assertEqual({k: self.h.status()[k] for k in ("running", "scheduled", "ended")}, {"running": 2, "scheduled": 1, "ended": 0})
        calls = self.h.station.calls
        self.h.clock.now = utc(2026, 11, 10, 0, 0, 1)
        self.assertEqual({k: self.h.status()[k] for k in ("running", "scheduled", "ended")}, {"running": 3, "scheduled": 0, "ended": 0})
        self.h.clock.now = utc(2026, 11, 13, 0, 0, 0)
        self.assertEqual({k: self.h.status()[k] for k in ("running", "scheduled", "ended")}, {"running": 2, "scheduled": 0, "ended": 1})
        self.assertEqual(self.h.station.calls, calls)
        self.assertEqual(next(c for c in self.h.status()["campaigns"] if c["id"] == later["id"])["state"], "ended")


class BreakTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("voice_rights", "commercial_use")

    def test_a_break_is_a_preview_unless_asked_to_remember_itself(self):
        self.h.save(advertiser="Acme", frequency="custom", caps={"per_hour": 1})
        first = self.h.call("break_build", seconds=60, seed="s")
        again = self.h.call("break_build", seconds=60, seed="s")
        self.assertEqual((first["recorded"], first["break_id"]), (False, None))
        self.assertEqual(first, again)
        self.assertEqual([i["campaign"] for i in first["items"]], ["Acme"])
        self.assertEqual(self.h.ext._store.active_planned(NOW, 3600), [])
        remembered = self.h.call("break_build", seconds=60, seed="s", record=True)
        self.assertEqual((remembered["recorded"], len(remembered["break_id"])), (True, 32))
        again = self.h.call("break_build", seconds=60, seed="s")
        self.assertEqual((again["items"], reasons(again)), ([], {"Acme": "hour_cap"}))  # the planned play already counts

    def test_planned_plays_stop_counting_once_they_are_overdue(self):
        self.h.save(advertiser="Acme", frequency="custom", caps={"per_day": 1}, pacing="asap")
        self.h.call("break_build", seconds=60, record=True)
        self.assertEqual(self.h.call("break_build", seconds=60)["items"], [])
        self.h.clock.advance(46 * 60)  # more than the grace after the planned time: it did not air
        plan = self.h.call("break_build", seconds=60)
        self.assertEqual([i["campaign"] for i in plan["items"]], ["Acme"])

    def test_planned_plays_leave_the_next_break_a_clear_view_once_they_air(self):
        self.h.save(advertiser="Acme", frequency="custom", caps={"per_hour": 2}, pacing="asap")
        track = self.h.station.library[0]["id"]
        self.h.call("break_build", seconds=60, record=True)
        self.h.ext.on_event(self.h.started(track, NOW + 1))  # it airs: the planned entry is used up, not counted twice
        plan = self.h.call("break_build", seconds=60, at="2026-11-05T15:05:00+00:00")
        self.assertEqual([i["campaign"] for i in plan["items"]], ["Acme"])  # one play in the hour so far, limit two
        self.h.ext.on_event(self.h.started(track, NOW + 400))
        plan = self.h.call("break_build", seconds=60, at="2026-11-05T15:10:00+00:00")
        self.assertEqual(reasons(plan), {"Acme": "hour_cap"})

    def test_what_was_held_back_is_counted_only_for_breaks_that_were_remembered(self):
        track = self.h.station.add_track("Capped spot")
        self.h.save(advertiser="Capped", frequency="custom", caps={"per_hour": 1}, pacing="asap",
                    creatives=[{"kind": "recorded", "track_id": track}])
        self.h.ext.on_event(self.h.started(track, NOW - 600))
        self.h.save(advertiser="Paused", paused=True)
        for _ in range(3):
            self.h.call("break_build", seconds=60)
            self.h.route("GET", "/break", {"seconds": ["60"]})
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual([c["totals"]["held_back"] for c in report["campaigns"]], [{}, {}])
        for _ in range(2):
            self.h.call("break_build", seconds=60, record=True)
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        held = {c["name"]: c["totals"]["held_back"] for c in report["campaigns"]}
        self.assertEqual(held, {"Capped": {"hour_cap": 2}, "Paused": {}})  # a paused spot is "not scheduled", not "held back"

    def test_a_recorded_break_plans_each_message_where_it_will_start(self):
        for name in ("First", "Second", "Third"):
            self.h.save(advertiser=name)
        plan = self.h.call("break_build", seconds=120, record=True)
        self.assertEqual([i["offset"] for i in plan["items"]], [0.0, 30.0, 60.0])
        planned = self.h.ext._store.active_planned(NOW - 3600, 7200)
        self.assertEqual([r["planned_at"] for r in planned], [NOW, NOW + 30, NOW + 60])  # not all at the start of the break

    def test_a_spot_that_played_through_one_message_is_not_counted_as_held_back_for_the_other(self):
        ready = self.h.station.add_track("Recorded read")
        self.h.save(advertiser="Mixed", creatives=[{"kind": "recorded", "track_id": ready}, {"kind": "script", "text": "Buy bikes."}])
        plan = self.h.call("break_build", seconds=60, record=True)
        self.assertEqual([i["campaign"] for i in plan["items"]], ["Mixed"])
        self.assertEqual([(s["reason"], bool(s["creative_id"])) for s in plan["skipped"]], [("audio_not_ready", True)])  # still explained
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual(report["campaigns"][0]["totals"]["held_back"], {})

    def test_a_spot_that_was_left_out_is_counted_once_even_with_a_message_that_is_not_ready(self):
        ready = self.h.station.add_track("Recorded read")
        self.h.save(advertiser="Mixed", frequency="custom", caps={"per_hour": 1}, pacing="asap",
                    creatives=[{"kind": "recorded", "track_id": ready}, {"kind": "script", "text": "Buy bikes."}])
        self.h.ext.on_event(self.h.started(ready, NOW - 600))
        plan = self.h.call("break_build", seconds=60, record=True)
        self.assertEqual(plan["items"], [])
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual(report["campaigns"][0]["totals"]["held_back"], {"hour_cap": 1})

    def test_a_spot_whose_every_message_is_not_ready_is_counted_as_held_back(self):
        self.h.save(advertiser="Silent", creatives=[{"kind": "script", "text": "Buy bikes."}])
        self.h.call("break_build", seconds=60, record=True)
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual(report["campaigns"][0]["totals"]["held_back"], {"audio_not_ready": 1})

    def test_the_default_seed_is_stable_and_a_given_seed_is_repeatable(self):
        for i in range(4):
            self.h.save(advertiser=f"Tie{i}")
        first = self.h.call("break_build", seconds=40)
        self.assertEqual(first, self.h.call("break_build", seconds=40))
        self.assertEqual(first["seed"], f"|{int(NOW // 300)}")
        picks = {self.h.call("break_build", seconds=40, seed=str(s))["items"][0]["campaign"] for s in range(30)}
        self.assertGreater(len(picks), 1)
        self.assertEqual(self.h.call("break_build", seconds=40, seed="7"), self.h.call("break_build", seconds=40, seed="7"))

    def test_shows_decide_which_spots_may_play(self):
        self.h.save(advertiser="Morning only", flights=[{"show_ids": ["morning"]}])
        self.h.save(advertiser="Anywhere")
        for show, expected in (("morning", ["Anywhere", "Morning only"]), ("late", ["Anywhere"]), (None, ["Anywhere"])):
            with self.subTest(show=show):
                args = {"seconds": 120} if show is None else {"seconds": 120, "show_id": show}
                self.assertEqual(sorted(i["campaign"] for i in self.h.call("break_build", **args)["items"]), expected)
                query = {"seconds": ["120"]} if show is None else {"seconds": ["120"], "show_id": [show]}
                self.assertEqual(sorted(i["campaign"] for i in self.h.route("GET", "/break", query).body["items"]), expected)
        with self.assertRaisesRegex(ExtensionError, "show id"):
            self.h.call("break_build", seconds=60, show_id="Not A Slug")
        self.assertEqual(self.h.route("GET", "/break", {"show_id": ["Not A Slug"]}).status, 400)

    def test_the_break_uses_the_lengths_and_availability_from_the_library(self):
        track = self.h.station.add_track("Long spot", duration=75.0)
        self.h.save(advertiser="Long", creatives=[{"kind": "recorded", "track_id": track}])
        self.assertEqual(self.h.call("break_build", seconds=60)["items"], [])
        plan = self.h.call("break_build", seconds=90)
        self.assertEqual((plan["items"][0]["duration"], plan["seconds_used"]), (75.0, 75.0))
        self.h.station.library[0]["available"] = False
        self.assertEqual(reasons(self.h.call("break_build", seconds=90)), {"Long": "audio_unavailable"})
        del self.h.station.library[0]
        self.assertEqual(reasons(self.h.call("break_build", seconds=90)), {"Long": "audio_missing"})
        view = self.h.route("GET", "/campaigns").body["campaigns"][0]
        self.assertEqual(view["creatives"][0]["problem"], "The recording is no longer in your library.")

    def test_a_written_spot_waits_for_its_voice_and_is_listed_ready_once_it_has_one(self):
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Fixing bikes since 1998."}])
        creative = saved["creatives"][0]["id"]
        plan = self.h.call("break_build", seconds=60)
        self.assertEqual((plan["items"], reasons(plan)), ([], {"Acme": "audio_not_ready"}))
        early = self.h.call("break_build", seconds=60, ready_only=False)
        self.assertEqual([(i["ready"], i["track_id"], i["estimated"]) for i in early["items"]], [(False, None, True)])
        voice = self.h.station.add_track("Acme read", duration=14.5)
        attached = self.h.call("spot_audio", campaign=saved["id"], creative=creative, track_id=voice)
        self.assertEqual((attached["track_id"], attached["duration"]), (voice, 14.5))
        plan = self.h.call("break_build", seconds=60)
        self.assertEqual([(i["ready"], i["track_id"], i["duration"], i["kind"]) for i in plan["items"]], [(True, voice, 14.5, "script")])
        view = self.h.call("campaign_get", campaign=saved["id"])["campaign"]["creatives"][0]
        self.assertEqual((view["ready"], view["problem"], view["track_id"]), (True, None, voice))

    def test_the_limits_of_a_break_come_from_the_operators_config_file(self):
        h = Harness(self, config={"max_spots_per_break": 1, "max_spots_per_hour": 50, "timezone": "Europe/London"})
        h.confirm("commercial_use")
        for i in range(3):
            h.save(advertiser=f"S{i}")
        plan = h.call("break_build", seconds=300)
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual({s["reason"] for s in plan["skipped"]}, {"break_full"})
        listing = h.route("GET", "/campaigns").body
        self.assertEqual((listing["limits"]["max_spots_per_break"], listing["limits"]["max_spots_per_hour"], listing["timezone"]), (1, 50, "Europe/London"))
        track = h.station.add_track("T")
        spot = h.call("campaign_save", campaign={"advertiser": "UK", "creatives": [{"kind": "recorded", "track_id": track}]})["campaign"]
        self.assertEqual(spot["timezone"], "Europe/London")

    def test_a_broken_config_file_falls_back_and_is_reported(self):
        h = Harness(self, config={"max_spots_per_break": 99, "timezone": "Mars/Base"})
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["ads.spots"]["status"], "ok")
        h.route("GET", "/campaigns")  # reading the settings notices the problem
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["ads.settings"]["status"], "warn")
        self.assertIn("config.json", checks["ads.settings"]["hint"])
        self.assertEqual(h.route("GET", "/campaigns").body["limits"]["max_spots_per_break"], 3)

    def test_two_callers_remembering_a_break_at_the_same_time_cannot_both_take_the_same_spot(self):
        self.h.save(advertiser="Only", frequency="custom", caps={"per_hour": 1}, pacing="asap")
        start, results = threading.Barrier(8), []

        def caller():
            start.wait(5)
            results.append(self.h.call("break_build", seconds=60, record=True)["items"])

        threads = [threading.Thread(target=caller) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(len(items) for items in results), 1)

    def test_a_break_can_be_planned_ahead_but_only_within_reason(self):
        self.h.save(advertiser="Later", flights=[{"start": "2026-11-06"}])
        self.assertEqual(self.h.call("break_build", seconds=60)["items"], [])
        plan = self.h.call("break_build", seconds=60, at="2026-11-06T09:00:00+00:00")
        self.assertEqual([i["campaign"] for i in plan["items"]], ["Later"])
        for bad in ("tomorrow", "2026-11-06T09:00:00", "2026-11-05T13:00:00+00:00", "2026-12-06T09:00:00+00:00", "2026-11-06T09:00:00Zjunk"):
            with self.subTest(at=bad), self.assertRaises(ExtensionError):
                self.h.call("break_build", seconds=60, at=bad)
        self.assertEqual(self.h.call("break_build", seconds=60, at="2026-11-06T09:00:00Z")["at"], "2026-11-06T09:00:00+00:00")


class WeeklyCapTests(unittest.TestCase):
    """The weekly limit through the whole extension: the plays the store keeps, the window a break reads back, and
    the spot's own Monday-to-Sunday week. (NOW is Thursday 5 November 2026, 15:00 UTC.)"""

    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("commercial_use")

    def spot(self, per_week, **extra):
        saved = self.h.save(advertiser="Acme", frequency="custom", caps={"per_week": per_week}, pacing="asap", **extra)
        return saved, saved["creatives"][0]["track_id"]

    def play_at(self, track, *moments):
        for at in moments:
            self.h.ext.on_event(self.h.started(track, at))

    def verdict(self):
        plan = self.h.call("break_build", seconds=60)
        return [i["campaign"] for i in plan["items"]], {s["campaign"]: s["reason"] for s in plan["skipped"]}

    def test_plays_earlier_in_the_same_week_count_even_three_days_back(self):
        _, track = self.spot(3)
        self.play_at(track, utc(2026, 11, 2, 9), utc(2026, 11, 3, 9))  # Monday and Tuesday: more than three days ago
        self.assertEqual(self.verdict(), (["Acme"], {}))  # two of three
        self.play_at(track, utc(2026, 11, 4, 9))
        self.assertEqual(self.verdict(), ([], {"Acme": "week_cap"}))

    def test_plays_from_the_week_before_do_not_count(self):
        _, track = self.spot(3)
        self.play_at(track, utc(2026, 10, 30, 9), utc(2026, 10, 31, 9), utc(2026, 11, 1, 23, 59))  # Friday to Sunday night
        self.assertEqual(self.verdict(), (["Acme"], {}))
        self.play_at(track, utc(2026, 11, 2, 0, 0, 1), utc(2026, 11, 2, 9))  # Monday morning: this week
        self.assertEqual(self.verdict(), (["Acme"], {}))
        self.play_at(track, utc(2026, 11, 4, 9))
        self.assertEqual(self.verdict(), ([], {"Acme": "week_cap"}))

    @unittest.skipUnless(tz_available("Pacific/Kiritimati"), "this computer has no Kiritimati time zone")
    def test_the_week_is_the_spots_own_week_in_a_zone_far_ahead_of_utc(self):
        # Kiritimati is 14 hours ahead: its Monday starts on Sunday 10:00 UTC, and it is already Friday there.
        _, track = self.spot(3, timezone="Pacific/Kiritimati")
        self.play_at(track, utc(2026, 11, 1, 9, 0))  # Sunday 23:00 there: still last week
        self.play_at(track, utc(2026, 11, 1, 11, 0), utc(2026, 11, 2, 12, 0))  # Monday 01:00 and Tuesday 02:00 there: this week
        self.assertEqual(self.verdict(), (["Acme"], {}))  # two this week, so one more is allowed
        self.play_at(track, utc(2026, 11, 4, 12, 0))
        self.assertEqual(self.verdict(), ([], {"Acme": "week_cap"}))

    @unittest.skipUnless(tz_available("Pacific/Pago_Pago"), "this computer has no Pago Pago time zone")
    def test_the_week_is_the_spots_own_week_in_a_zone_far_behind_utc(self):
        # Pago Pago is 11 hours behind: its Monday starts at 11:00 UTC, and it is Thursday 04:00 there.
        _, track = self.spot(1, timezone="Pacific/Pago_Pago")
        self.play_at(track, utc(2026, 11, 2, 9, 0))  # Sunday 22:00 there: last week there, though it is already Monday in UTC
        self.assertEqual(self.verdict(), (["Acme"], {}))
        self.play_at(track, utc(2026, 11, 2, 12, 0))  # Monday 01:00 there: this week
        self.assertEqual(self.verdict(), ([], {"Acme": "week_cap"}))

    def test_the_cap_shows_up_in_the_held_back_counts_of_a_recorded_break(self):
        _, track = self.spot(1)
        self.play_at(track, utc(2026, 11, 2, 9))
        self.h.call("break_build", seconds=60, record=True)
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual(report["campaigns"][0]["totals"]["held_back"], {"week_cap": 1})


class LostTimezoneTests(unittest.TestCase):
    """The operating system can lose a timezone name after an upgrade. The spot must stay visible and explain itself."""

    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("commercial_use")
        self.good = self.h.save(advertiser="Good")
        self.lost = self.h.save(advertiser="Lost", timezone="America/New_York")
        with self.h.ext._lock, self.h.ext._store.conn:
            self.h.ext._store.conn.execute("UPDATE campaigns SET timezone='Mars/Base' WHERE id=?", (self.lost["id"],))
        self.h.ext._reload()

    def test_it_is_listed_with_a_plain_explanation_and_does_not_play(self):
        view = self.h.call("campaign_get", campaign=self.lost["id"])["campaign"]
        self.assertEqual((view["name"], view["state"]), ("Lost", "ended"))
        self.assertIn("timezone this computer doesn't know", view["problem"])
        self.assertIsNone(self.h.call("campaign_get", campaign=self.good["id"])["campaign"]["problem"])
        plan = self.h.call("break_build", seconds=120)
        self.assertEqual([i["campaign"] for i in plan["items"]], ["Good"])
        self.assertEqual(reasons(plan), {"Lost": "timezone_unknown"})
        self.assertEqual(self.h.status()["campaigns_total"], 2)

    def test_the_doctor_says_so_and_reports_and_recording_do_not_fail(self):
        checks = {c["id"]: c for c in self.h.host.checks()}
        self.assertEqual(checks["ads.timezones"]["status"], "warn")
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual([c["name"] for c in report["campaigns"]], ["Good"])
        self.assertTrue(any("Lost is left out" in note for note in report["notes"]))
        self.h.call("break_build", seconds=60, record=True)  # held-back counts for a spot with no usable zone are skipped quietly
        self.assertEqual(self.h.route("GET", "/report.csv").status, 200)

    def test_saving_a_new_timezone_repairs_it(self):
        with self.assertRaisesRegex(ExtensionError, "Unknown timezone"):
            self.h.call("campaign_save", campaign={"id": self.lost["id"], "priority": 3})  # still names the lost zone
        fixed = self.h.call("campaign_save", campaign={"id": self.lost["id"], "timezone": "Europe/London"})["campaign"]
        self.assertEqual((fixed["timezone"], fixed["problem"], fixed["state"]), ("Europe/London", None, "running"))
        self.assertEqual({c["id"] for c in self.h.host.checks()}, {"ads.spots"})


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("commercial_use")

    def test_the_list_of_spots(self):
        spot = self.h.save(advertiser="Acme")
        reply = self.h.route("GET", "/campaigns")
        self.assertEqual(reply.status, 200)
        self.assertEqual([c["id"] for c in reply.body["campaigns"]], [spot["id"]])
        self.assertEqual(set(reply.body), {"campaigns", "now", "acknowledgements", "limits", "timezone", "problem"})
        self.assertEqual(self.h.route("GET", "/campaigns", {"campaign": [spot["id"]]}).body["campaigns"][0]["name"], "Acme")
        self.assertEqual(self.h.route("GET", "/campaigns", {"campaign": ["f" * 32]}).status, 400)
        json.dumps(reply.body)

    def test_the_preview_of_the_next_break(self):
        self.h.save(advertiser="Acme")
        reply = self.h.route("GET", "/break", {"seconds": ["45"], "show_id": ["morning"]})
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["lines"][0], "The next break would play 1 spot (30 seconds).")
        self.assertEqual((reply.body["budget_seconds"], reply.body["show_id"], reply.body["recorded"]), (45.0, "morning", False))
        self.assertEqual(self.h.route("GET", "/break").body["budget_seconds"], 60.0)  # the default
        for bad in ("abc", "0", "601", "-5", "nan", "inf", ""):
            with self.subTest(seconds=bad):
                reply = self.h.route("GET", "/break", {"seconds": [bad]})
                self.assertEqual(reply.status, 400)
                self.assertIn("seconds", reply.body["error"])

    def test_the_report_in_json_and_in_a_spreadsheet(self):
        spot = self.h.save(advertiser="Acme", plan={"per_day": 2})
        track = self.h.station.library[0]["id"]
        self.h.ext.on_event(self.h.started(track, NOW - 3600))
        self.h.ext.on_event(self.h.finished(track, NOW - 3570))
        query = {"campaign": [spot["id"]], "from": ["2026-11-05"], "to": ["2026-11-05"]}
        report = self.h.route("GET", "/report", query)
        self.assertEqual(report.status, 200)
        [entry] = report.body["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["planned"], entry["totals"]["delivered_share"]), (1, 2.0, 0.5))
        self.assertEqual(entry["plays"][0]["outcome"], "finished")
        sheet = self.h.route("GET", "/report.csv", query)
        self.assertEqual((sheet.status, sheet.content_type), (200, "text/csv; charset=utf-8"))
        lines = sheet.body.split("\r\n")
        self.assertTrue(lines[0].startswith("Date,Spot,Advertiser,Type,Plays,Cut off,Recovered,Planned plays,Delivered %"))
        self.assertEqual(lines[1].split(",")[:9], ["2026-11-05", "Acme", "Acme", "Paid", "1", "0", "0", "2", "50"])
        for bad in ({"from": ["tomorrow"]}, {"to": ["2026-13-45"]}, {"from": ["2026-11-06"], "to": ["2026-11-05"]},
                    {"campaign": ["f" * 32]}, {"campaign": ["' OR 1=1"]}, {"from": ["2020-01-01"], "to": ["2026-11-05"]}):
            for path in ("/report", "/report.csv"):
                with self.subTest(path=path, query=bad):
                    self.assertEqual(self.h.route("GET", path, bad).status, 400)

    def test_the_report_operation_is_a_smaller_version_of_the_route(self):
        self.h.save(advertiser="Acme")
        data = self.h.call("report", from_date="2026-11-01", to_date="2026-11-05")
        self.assertEqual(len(data["campaigns"][0]["days"]), 5)
        with self.assertRaisesRegex(ExtensionError, "1 to 31 days"):
            self.h.call("report", from_date="2026-01-01", to_date="2026-11-05")

    def test_the_report_operation_stays_small_enough_to_hand_to_a_model(self):
        with mock.patch.object(extension_module, "MAX_CAMPAIGNS", 300):
            for i in range(30):
                self.h.save(advertiser=f"Spot {i}")
        with self.assertRaisesRegex(ExtensionError, "very large"):
            self.h.call("report", from_date="2026-11-01", to_date="2026-11-07")  # 30 spots x 7 days = 210 spot-days
        data = self.h.call("report", from_date="2026-11-01", to_date="2026-11-06")  # 180 spot-days
        self.assertEqual(sum(len(c["days"]) for c in data["campaigns"]), 180)
        self.assertLess(len(json.dumps(data)), 60000)
        one = self.h.call("report", campaign=data["campaigns"][0]["id"], from_date="2026-10-06", to_date="2026-11-05")  # one spot, 31 days
        self.assertEqual(len(one["campaigns"][0]["days"]), 31)
        self.assertEqual(self.h.route("GET", "/report", {"from": ["2026-10-01"], "to": ["2026-11-05"]}).status, 200)  # the screen's route is not limited so

    def test_every_route_error_is_plain_language(self):
        for method, path, query, body in (("GET", "/break", {"seconds": ["x"]}, None), ("GET", "/report", {"from": ["x"]}, None),
                                          ("GET", "/campaigns", {"campaign": ["x"]}, None), ("POST", "/acknowledge", {}, {"nope": 1})):
            reply = self.h.route(method, path, query, body)
            self.assertEqual(reply.status, 400)
            for word in JARGON:
                self.assertNotIn(word, reply.body["error"])
            self.assertTrue(reply.body["error"].endswith("."))


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("commercial_use")

    def test_status_is_a_cached_snapshot_that_never_calls_the_station(self):
        for i in range(3):
            self.h.save(advertiser=f"S{i}")
        calls = self.h.station.calls
        for _ in range(500):
            self.h.ext.status()
        self.assertEqual(self.h.station.calls, calls)

    def test_status_does_not_wait_for_a_busy_extension(self):
        self.h.save(advertiser="Acme")
        done, release = threading.Event(), threading.Event()

        def hold_the_lock():
            with self.h.ext._lock:
                done.set()
                release.wait(30)

        holder = threading.Thread(target=hold_the_lock, daemon=True)
        holder.start()
        self.assertTrue(done.wait(10))
        answers = []
        asker = threading.Thread(target=lambda: answers.append(self.h.ext.status()), daemon=True)
        asker.start()
        asker.join(10)  # generous: it only takes this long if status() is stuck behind the lock
        answered_while_the_lock_was_held = not asker.is_alive()
        release.set()
        holder.join(10)
        asker.join(10)
        self.assertTrue(answered_while_the_lock_was_held, "status() must not wait for the extension's lock")
        self.assertEqual(answers[0]["running"], 1)

    def test_status_names_a_bounded_number_of_spots_and_stays_small(self):
        with mock.patch.object(extension_module, "MAX_CAMPAIGNS", 300):
            for i in range(45):
                self.h.save(advertiser=f"Spot number {i}")
        status = self.h.status()
        self.assertEqual((status["campaigns_total"], len(status["campaigns"])), (45, extension_module.STATUS_CAMPAIGNS))
        self.assertLess(len(json.dumps(status)), 12000)
        self.assertEqual(set(status["campaigns"][0]), {"id", "name", "state", "house"})

    def test_status_with_ordinary_names_names_25_spots_and_stays_under_what_the_stations_assistant_will_read(self):
        # The assistant drops a status of more than 4,000 characters (the entry as a whole), and with it the ids of the spots.
        with mock.patch.object(extension_module, "MAX_CAMPAIGNS", 300):
            for i in range(45):
                self.h.save(advertiser=f"Spot number {i}")
        entry = self.h.host.status()["ads"]
        self.assertLess(len(json.dumps(entry, ensure_ascii=False)), 4000)
        self.assertEqual(len(entry["status"]["campaigns"]), 25)
        self.assertEqual(entry["status"]["campaigns_total"], 45)

    def test_status_with_the_longest_names_is_named_only_as_far_as_there_is_room(self):
        with mock.patch.object(extension_module, "MAX_CAMPAIGNS", 300):
            for i in range(60):
                self.h.save(advertiser=("Long name for spot number %03d " % i).ljust(60, "x"))
        entry = self.h.host.status()["ads"]
        self.assertLess(len(json.dumps(entry, ensure_ascii=False)), 4000)
        named = entry["status"]["campaigns"]
        self.assertTrue(10 < len(named) < 25, len(named))  # still a useful number of ids, but fewer than the ordinary 25
        listing = [c["name"] for c in self.h.route("GET", "/campaigns").body["campaigns"]]
        self.assertEqual([c["name"] for c in named], listing[:len(named)])  # the first spots in the list, never a patchwork
        self.assertEqual(entry["status"]["campaigns_total"], 60)

    def test_status_follows_a_clock_that_is_set_back(self):
        self.h.save(advertiser="Later", flights=[{"start": "2026-11-10"}])
        self.h.clock.now = utc(2026, 11, 11)
        self.assertEqual((self.h.status()["running"], self.h.status()["scheduled"]), (1, 0))
        self.h.clock.now = utc(2026, 11, 1)  # the computer's clock was corrected backwards
        self.assertEqual((self.h.status()["running"], self.h.status()["scheduled"]), (0, 1))

    def test_status_shape(self):
        self.h.save(advertiser="Acme")
        status = self.h.status()
        self.assertEqual(set(status), {"running", "scheduled", "paused", "ended", "campaigns", "campaigns_total", "acknowledgements",
                                       "last_play_at", "plays_logged"})
        self.assertEqual(set(status["acknowledgements"]), {"voice_rights", "commercial_use"})
        self.assertEqual(set(status["acknowledgements"]["voice_rights"]), {"acknowledged", "at", "needs_reconfirm"})
        self.assertEqual((status["last_play_at"], status["plays_logged"]), (None, 0))

    def test_the_checks_for_the_doctor(self):
        def checks():
            return {c["id"]: c for c in self.h.host.checks()}

        self.assertEqual(checks()["ads.spots"]["detail"], "No spots are set up.")
        self.h.confirm("voice_rights")
        self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Hello."}])
        self.assertEqual(checks()["ads.spots"]["detail"], "1 spot set up.")
        self.assertNotIn("ads.voice_rights", checks())
        self.h.route("POST", "/acknowledge", body={"voice_rights": False, "commercial_use": False})
        found = checks()
        self.assertEqual((found["ads.voice_rights"]["status"], found["ads.commercial_use"]["status"]), ("warn", "warn"))
        for check in found.values():
            for word in JARGON:
                self.assertNotIn(word, check["detail"] + check["hint"])
        self.h.confirm("voice_rights", "commercial_use")
        self.assertEqual(set(checks()), {"ads.spots"})


if __name__ == "__main__":
    unittest.main()
