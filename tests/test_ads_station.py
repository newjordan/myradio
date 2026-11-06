"""The ads extension inside the real Station and the real HTTP server: real playout events, real restarts."""
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from myradio.ads.extension import AdsExtension, build
from myradio.ads.terms import TERMS, TERMS_VERSION
from myradio.extensions import Request, builtin_extensions
from myradio.server import Server
from myradio.station import Station, StationError

TOKEN = "t" * 40


def wav(seconds=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        out.writeframes(bytes(int(44100 * seconds) * 4))
    return buf.getvalue()


def wait_for(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class StationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "data"
        self.ext = build()
        self.station = self.open(self.ext)

    def open(self, *extensions):
        station = Station(self.directory, start_worker=False, extensions=list(extensions))
        self.addCleanup(station.close)
        return station

    def route(self, station, method, path, query=None, body=None):
        match = station.extensions.find_route(method, "/api/x/ads" + path)
        self.assertIsNotNone(match, path)
        raw = json.dumps(body).encode() if body is not None else b""
        return station.extensions.dispatch_route(match, Request(method, path, query or {}, body, raw))

    def ads_status(self, station=None):
        return (station or self.station).status()["extensions"]["ads"]["status"]

    def import_ad(self, title="Bike spot", seconds=1.0, kind="ad"):
        return self.station.import_wav(wav(seconds), title, kind=kind)["id"]

    def play_for(self, ticks):
        for _ in range(ticks):
            self.station.tick()

    def paid_spot(self, track, **extra):
        self.route(self.station, "POST", "/acknowledge", body={"commercial_use": True, "version": TERMS_VERSION})
        saved = self.station.action("ads.campaign_save", {"campaign": {"advertiser": "Corner Bike Co.", "timezone": "UTC",
                                                                       "creatives": [{"kind": "recorded", "track_id": track}], **extra}})
        return saved["campaign"]


class PlayoutTests(StationCase):
    def test_the_builtin_loader_finds_the_ads_extension(self):
        loaded = [e for e in builtin_extensions() if e.name == "ads"]
        self.assertEqual([type(e) for e in loaded], [AdsExtension])
        self.assertEqual(self.station.status()["extensions"]["ads"]["state"], "running")
        self.assertIn("ads.campaign_save", [o["name"] for o in self.station.operations()])

    def test_a_real_ad_play_is_logged_from_the_real_event_and_reported(self):
        track = self.import_ad("Bike spot", seconds=1.0)
        spot = self.paid_spot(track)
        self.station.action("queue.add", {"track_id": track})
        self.station.action("transport.start")
        self.play_for(2)  # the ad starts on the first tick
        self.assertTrue(wait_for(lambda: self.ads_status()["plays_logged"] == 1), "the start event never reached the extension")
        self.play_for(12)  # one second of audio is ten ticks; the next tick after it ends reports "finished"
        report = self.route(self.station, "GET", "/report", {"campaign": [spot["id"]]}).body
        self.assertTrue(wait_for(lambda: self.route(self.station, "GET", "/report", {"campaign": [spot["id"]]}).body["campaigns"][0]["plays"][0]["outcome"] == "finished"))
        report = self.route(self.station, "GET", "/report", {"campaign": [spot["id"]]}).body
        [entry] = report["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["interrupted"], entry["totals"]["recovered"]), (1, 0, 0))
        self.assertEqual(entry["plays"][0]["source"], "live")
        self.assertIsNotNone(self.ads_status()["last_play_at"])
        self.assertEqual(self.route(self.station, "GET", "/campaigns").body["campaigns"][0]["plays_last_7_days"], 1)

    def test_skipping_an_ad_is_reported_as_cut_short_not_as_delivered(self):
        track = self.import_ad("Long spot", seconds=5.0)
        spot = self.paid_spot(track)
        self.station.action("queue.add", {"track_id": track})
        self.station.action("transport.start")
        self.play_for(3)
        self.station.action("transport.skip")
        self.assertTrue(wait_for(lambda: self.route(self.station, "GET", "/report", {"campaign": [spot["id"]]}).body["campaigns"][0]["totals"]["interrupted"] == 1))
        [entry] = self.route(self.station, "GET", "/report", {"campaign": [spot["id"]]}).body["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["interrupted"]), (0, 1))
        self.assertEqual(entry["plays"][0]["detail"], "skipped")

    def test_music_and_ads_that_belong_to_no_spot_are_not_logged(self):
        song = self.import_ad("A song", seconds=0.5, kind="music")
        stray = self.import_ad("Stray ad", seconds=0.5)
        for track in (song, stray):
            self.station.action("queue.add", {"track_id": track})
        self.station.action("transport.start")
        self.play_for(15)
        time.sleep(0.2)
        self.assertEqual(self.ads_status()["plays_logged"], 0)

    def test_playout_never_waits_for_the_ads_extension(self):
        track = self.import_ad("Bike spot", seconds=1.0)
        self.paid_spot(track)
        self.station.action("queue.add", {"track_id": track})
        self.station.action("transport.start")
        self.ext._lock.acquire()  # the extension is busy (a long report, a slow disk): its own thread will wait here
        try:
            ticker = threading.Thread(target=lambda: self.play_for(30), daemon=True)
            ticker.start()
            ticker.join(20)
            self.assertFalse(ticker.is_alive(), "the audio clock must not wait for the ads extension")
            self.station.status()
            self.assertEqual(self.ads_status()["plays_logged"], 0, "the extension's own thread is the one that waits")
        finally:
            self.ext._lock.release()
        self.assertTrue(wait_for(lambda: self.ads_status()["plays_logged"] == 1))

    def test_the_station_snapshot_carries_the_ads_status_without_the_legal_text(self):
        self.route(self.station, "POST", "/acknowledge", body={"voice_rights": True, "version": TERMS_VERSION})
        snapshot = json.dumps(self.station.status())
        self.assertIn('"voice_rights"', snapshot)
        for kind in TERMS:
            self.assertNotIn(TERMS[kind]["text"][:30], snapshot)
        self.assertTrue(self.ads_status()["acknowledgements"]["voice_rights"]["acknowledged"])

    def test_a_refusal_comes_back_as_a_plain_station_error(self):
        track = self.import_ad("Bike spot")
        with self.assertRaises(StationError) as caught:
            self.station.action("ads.campaign_save", {"campaign": {"advertiser": "Acme", "creatives": [{"kind": "recorded", "track_id": track}]}})
        self.assertIn("confirmation", str(caught.exception))
        self.assertNotIn("Traceback", str(caught.exception))

    def test_the_database_is_listed_for_backups(self):
        names = [p.name for p in self.station.extensions.backup_files()["ads"]]
        self.assertEqual(names, ["ads.sqlite3"])


class RestartTests(StationCase):
    def test_plays_that_happened_while_the_extension_was_not_running_are_recovered(self):
        track = self.import_ad("Bike spot", seconds=1.0)
        spot = self.paid_spot(track)
        self.station.close()
        # The station runs for a while without the ads extension (it failed to load, say). Its own history still records the play.
        bare = self.open()
        bare.action("queue.add", {"track_id": track})
        bare.action("transport.start")
        for _ in range(14):
            bare.tick()
        bare.action("transport.stop")
        bare.close()
        again = build()
        station = self.open(again)
        self.assertEqual(station.status()["extensions"]["ads"]["state"], "running")
        report = self.route(station, "GET", "/report", {"campaign": [spot["id"]]}).body
        [entry] = report["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["recovered"]), (1, 1))
        self.assertEqual((entry["plays"][0]["source"], entry["plays"][0]["outcome"]), ("recovered", "finished"))
        self.assertTrue(any("found afterwards" in n for n in report["notes"]))
        # Asking again, or restarting again, adds nothing.
        self.route(station, "GET", "/report", {"campaign": [spot["id"]]})
        station.close()
        third = self.open(build())
        [entry] = self.route(third, "GET", "/report", {"campaign": [spot["id"]]}).body["campaigns"]
        self.assertEqual(entry["totals"]["plays"], 1)

    def test_a_recording_played_from_the_library_before_the_spot_existed_is_not_a_play_of_the_spot(self):
        track = self.import_ad("Bike spot", seconds=1.0)
        self.station.close()
        # The operator plays the recording once to hear it. No spot uses it yet, so nothing is counted.
        listening = self.open()
        listening.action("queue.add", {"track_id": track})
        listening.action("transport.start")
        for _ in range(14):
            listening.tick()
        listening.action("transport.stop")
        listening.close()
        # Only now is the spot made. The station's own history still shows the earlier play, but it was not a play of the spot.
        again = build()
        station = self.open(again)
        self.route(station, "POST", "/acknowledge", body={"commercial_use": True, "version": TERMS_VERSION})
        spot = station.action("ads.campaign_save", {"campaign": {"advertiser": "Corner Bike Co.", "timezone": "UTC",
                                                                 "creatives": [{"kind": "recorded", "track_id": track}]}})["campaign"]
        again._last_reconcile = -1e9  # as if 30 seconds had passed since the last look at the history
        report = self.route(station, "GET", "/report", {"campaign": [spot["id"]]}).body
        [entry] = report["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["recovered"], entry["plays"]), (0, 0, []))
        self.assertFalse(any("paid-use confirmation" in n or "found afterwards" in n for n in report["notes"]), report["notes"])
        self.assertEqual(self.ads_status(station)["plays_logged"], 0)
        # A play after the spot exists, with the extension not listening, is still found afterwards, as before.
        station.close()
        later = self.open()
        later.action("queue.add", {"track_id": track})
        later.action("transport.start")
        for _ in range(14):
            later.tick()
        later.action("transport.stop")
        later.close()
        station = self.open(build())
        [entry] = self.route(station, "GET", "/report", {"campaign": [spot["id"]]}).body["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["recovered"]), (1, 1))

    def test_an_ad_cut_off_by_a_restart_and_played_again_is_one_play_and_one_cut_off(self):
        track = self.import_ad("Bike spot", seconds=3.0)
        spot = self.paid_spot(track)
        self.station.action("queue.add", {"track_id": track})
        self.station.action("transport.start")
        self.play_for(5)  # the ad is on air ...
        self.assertTrue(wait_for(lambda: self.ads_status()["plays_logged"] == 1))
        self.station.close()  # ... when the station is switched off
        station = self.open(build())
        station.action("transport.start")  # the interrupted ad is back at the head of the queue and plays again from the top
        for _ in range(60):
            station.tick()

        def report():
            return self.route(station, "GET", "/report", {"campaign": [spot["id"]]}).body["campaigns"][0]

        self.assertTrue(wait_for(lambda: report()["plays"][-1]["outcome"] == "finished"), report()["plays"])
        entry = report()
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["interrupted"], entry["totals"]["unconfirmed"]), (1, 1, 0))
        self.assertEqual([(p["outcome"], p["detail"]) for p in entry["plays"]], [("interrupted", "station restarted"), ("finished", None)])

    def test_spots_and_confirmations_survive_a_restart(self):
        track = self.import_ad("Bike spot")
        spot = self.paid_spot(track)
        self.route(self.station, "POST", "/acknowledge", body={"voice_rights": True, "version": TERMS_VERSION})
        self.station.close()
        station = self.open(build())
        status = self.ads_status(station)
        self.assertEqual([c["id"] for c in status["campaigns"]], [spot["id"]])
        self.assertTrue(status["acknowledgements"]["commercial_use"]["acknowledged"])
        self.assertTrue(status["acknowledgements"]["voice_rights"]["acknowledged"])
        plan = self.route(station, "GET", "/break", {"seconds": ["30"]}).body
        self.assertEqual([i["track_id"] for i in plan["items"]], [track])


class HttpTests(StationCase):
    def setUp(self):
        super().setUp()
        with patch.dict("os.environ", {}, clear=True):
            self.server = Server(("127.0.0.1", 0), self.station, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, headers=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        sent = dict(headers or {})
        if token:
            sent["X-Myradio-Token"] = TOKEN
        if body is not None:
            sent["Content-Type"] = "application/json"
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data), response
        except ValueError:
            return response.status, data, response

    def test_confirmations_need_the_launch_code_and_a_same_origin_request(self):
        path = "/api/x/ads/acknowledge"
        self.assertEqual(self.call("POST", path, {"voice_rights": True, "version": TERMS_VERSION}, token=False)[0], 401)
        self.assertEqual(self.call("GET", path, token=False)[0], 401)
        self.assertEqual(self.call("POST", path, {"voice_rights": True, "version": TERMS_VERSION}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", path, {"voice_rights": True, "version": TERMS_VERSION}, headers={"Host": "evil.example"})[0], 403)
        self.assertFalse(self.ads_status()["acknowledgements"]["voice_rights"]["acknowledged"])
        status, body, _ = self.call("POST", path, {"voice_rights": True, "version": TERMS_VERSION})
        self.assertEqual((status, body["ok"], body["voice_rights"]["acknowledged"]), (200, True, True))
        self.assertTrue(self.ads_status()["acknowledgements"]["voice_rights"]["acknowledged"])
        self.assertEqual(self.call("POST", path, [1, 2])[0], 400)
        self.assertEqual(self.call("POST", path, {"voice_rights": "yes"})[0], 400)
        self.assertEqual(self.call("POST", path, {"pad": "x" * 2000})[0], 400)  # over this route's small body limit

    def test_no_operation_reaches_a_confirmation_through_the_action_endpoint(self):
        for operation in ("ads.acknowledge", "ads.voice_rights", "ads.confirm"):
            status, body, _ = self.call("POST", "/api/action", {"operation": operation, "arguments": {"voice_rights": True}})
            self.assertEqual((status, body["error"]), (400, "Unknown station operation."))
        status, body, _ = self.call("POST", "/api/action", {"operation": "ads.campaign_save", "arguments": {"campaign": {"voice_rights": True}}})
        self.assertEqual(status, 400)
        self.assertFalse(self.ads_status()["acknowledgements"]["voice_rights"]["acknowledged"])
        self.assertFalse(self.ads_status()["acknowledgements"]["commercial_use"]["acknowledged"])
        status, catalog, _ = self.call("GET", "/api/operations")
        names = [entry["name"] for entry in catalog if entry["name"].startswith("ads.")]
        self.assertIn("ads.campaign_remove", names)
        self.assertEqual([n for n in names if "ack" in n or "confirm" in n], [])

    def test_the_whole_journey_over_http(self):
        track = self.import_ad("Bike spot", seconds=1.0)
        self.assertEqual(self.call("POST", "/api/x/ads/acknowledge", {"commercial_use": True, "version": TERMS_VERSION})[0], 200)
        status, saved, _ = self.call("POST", "/api/action", {"operation": "ads.campaign_save", "arguments": {
            "campaign": {"advertiser": "Corner Bike Co.", "timezone": "UTC", "creatives": [{"kind": "recorded", "track_id": track}]}}})
        self.assertEqual(status, 200)
        spot = saved["campaign"]["id"]
        status, listing, _ = self.call("GET", "/api/x/ads/campaigns")
        self.assertEqual((status, [c["id"] for c in listing["campaigns"]]), (200, [spot]))
        status, preview, _ = self.call("GET", "/api/x/ads/break?seconds=60&show_id=morning")
        self.assertEqual((status, [i["track_id"] for i in preview["items"]]), (200, [track]))
        status, sheet, response = self.call("GET", "/api/x/ads/report.csv?campaign=" + spot)
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/csv; charset=utf-8")
        self.assertTrue(sheet.decode().startswith("Date,Spot,Advertiser,Type,Plays"))
        self.assertEqual(self.call("GET", "/api/x/ads/report.csv", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/x/ads/break?seconds=banana")[0], 400)
        self.assertEqual(self.call("POST", "/api/action", {"operation": "ads.campaign_pause", "arguments": {"campaign": spot}})[1]["state"], "paused")
        status, preview, _ = self.call("GET", "/api/x/ads/break?seconds=60")
        self.assertEqual(preview["items"], [])
        self.assertEqual(self.call("POST", "/api/action", {"operation": "ads.campaign_remove", "arguments": {"campaign": spot}})[0], 200)
        self.assertEqual(self.call("GET", "/api/x/ads/campaigns")[1]["campaigns"], [])


if __name__ == "__main__":
    unittest.main()
