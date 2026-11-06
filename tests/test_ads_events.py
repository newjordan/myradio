"""The as-run log: events, missed events, reconciliation against the station's history, and failure handling."""
import datetime as dt
import gc
import sqlite3
import threading
import time
import unittest
import warnings
from unittest import mock

from myradio.ads import extension as extension_module
from myradio.extensions import ExtensionError

try:
    from test_ads_support import NOW, Harness
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import NOW, Harness

JARGON = ("PCM", "GGUF", "SSRF", "Traceback", "NoneType", "KeyError", "TypeError", "ValueError", "sqlite", "Exception")


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


class LogCase(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("commercial_use")
        self.track = self.h.station.add_track("Bike spot")
        self.spot = self.h.save(advertiser="Corner Bike Co.", creatives=[{"kind": "recorded", "track_id": self.track}])

    def rows(self, where=""):
        with self.h.ext._lock:
            return [dict(r) for r in self.h.ext._store.conn.execute("SELECT * FROM plays " + where + " ORDER BY started_at")]

    def made_at(self, when):
        """Pretend the spots were made at ``when``, with their recordings attached then and the paid-use confirmation
        given a moment before (the harness makes everything at NOW)."""
        with self.h.ext._lock, self.h.ext._store.conn:
            self.h.ext._store.conn.execute("UPDATE campaigns SET created_at=?", (when,))
            self.h.ext._store.conn.execute("UPDATE attachments SET attached_at=?", (when,))
            self.h.ext._store.conn.execute("UPDATE acknowledgements SET at=?", (when - 1,))
        self.h.ext._reload()


class AsRunLogTests(LogCase):
    def test_a_started_ad_is_logged_with_the_confirmation_it_aired_under(self):
        self.h.ext.on_event(self.h.started(self.track, NOW))
        [row] = self.rows()
        self.assertEqual((row["campaign_id"], row["track_id"], row["started_at"], row["source"], row["paid"], row["spoken"]),
                         (self.spot["id"], self.track, NOW, "event", 1, 0))
        self.assertIsNotNone(row["commercial_ack_id"])  # recorded together with the paid play
        self.assertIsNone(row["voice_ack_id"])
        self.assertEqual((self.h.status()["plays_logged"], self.h.status()["last_play_at"]), (1, iso(NOW)))
        self.assertEqual(self.h.route("GET", "/campaigns").body["campaigns"][0]["plays_last_7_days"], 1)

    def test_a_play_that_aired_before_the_confirmation_is_recorded_without_one(self):
        self.h.ext.on_event(self.h.started(self.track, NOW - 3600))  # the confirmation was given "now", an hour later
        self.assertIsNone(self.rows()[0]["commercial_ack_id"])
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertTrue(any("without the paid-use confirmation" in n for n in report["notes"]))

    def test_a_promo_is_logged_as_free_and_a_spoken_play_records_the_voice_confirmation(self):
        self.h.confirm("voice_rights")
        promo_track = self.h.station.add_track("Promo read")
        promo = self.h.save(house=True, creatives=[{"kind": "script", "text": "Tell a friend."}])
        self.h.call("spot_audio", campaign=promo["id"], creative=promo["creatives"][0]["id"], track_id=promo_track)
        self.h.ext.on_event(self.h.started(promo_track, NOW + 60))
        [row] = self.rows("WHERE track_id='%s'" % promo_track)
        self.assertEqual((row["paid"], row["spoken"], row["commercial_ack_id"]), (0, 1, None))
        self.assertIsNotNone(row["voice_ack_id"])

    def test_the_same_event_delivered_twice_is_one_play(self):
        event = self.h.started(self.track, NOW)
        for _ in range(3):
            self.h.ext.on_event(dict(event))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.h.status()["plays_logged"], 1)

    def test_only_ads_that_belong_to_a_spot_are_logged(self):
        other_ad = self.h.station.add_track("Somebody else's ad")
        self.h.ext.on_event(self.h.started(other_ad, NOW))  # an ad file no spot uses
        self.h.ext.on_event(self.h.started(self.track, NOW + 1, kind="music"))  # the wrong kind of track, even with the right id
        self.h.ext.on_event(self.h.started("not-in-the-library", NOW + 2))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.h.ext._unattributed, 2)
        self.assertEqual(self.h.status()["plays_logged"], 0)

    def test_odd_events_are_ignored_without_error(self):
        track = {"id": self.track, "kind": "ad", "title": "x", "duration": 30.0}
        odd = [None, "started", 5, [], {}, {"type": "track_started"}, {"type": "track_started", "track": "x"}, {"type": "track_started", "track": {}},
               {"type": "track_started", "track": {"id": 5, "kind": "ad"}}, {"type": "track_started", "track": {"id": self.track}},
               {"type": "track_finished", "track": track}, {"type": "track_finished", "track": track, "result": ["finished"]},
               {"type": "track_finished", "track": track, "result": "exploded"}, {"type": "transport", "state": "started"},
               {"type": ["track_started"], "track": track}]
        for event in odd:
            with self.subTest(event=str(event)[:50]):
                self.h.ext.on_event(event)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.h.host.status()["ads"]["state"], "running")

    def test_a_bad_time_falls_back_to_now(self):
        for bad in (float("nan"), float("inf"), -5.0, "later", True, None):
            with self.subTest(at=bad):
                event = self.h.started(self.track, bad)
                self.h.ext.on_event(event)
        [row] = self.rows()  # every one of them landed at the same clock reading, so they collapse into one play
        self.assertEqual(row["started_at"], NOW)

    def test_finishing_closes_the_play(self):
        for result, outcome, detail in (("finished", "finished", None), ("skipped", "interrupted", "skipped"),
                                        ("stopped", "interrupted", "stopped"), ("playback failed", "interrupted", "playback failed")):
            with self.subTest(result=result):
                start = NOW + len(self.rows()) * 600
                self.h.ext.on_event(self.h.started(self.track, start))
                self.h.ext.on_event(self.h.finished(self.track, start + 12, result))
                row = self.rows()[-1]
                self.assertEqual((row["outcome"], row["detail"], row["ended_at"]), (outcome, detail, start + 12))

    def test_a_restarted_ad_counts_twice_but_only_the_complete_play_is_delivered(self):
        self.h.ext.on_event(self.h.started(self.track, NOW))
        self.h.ext.on_event(self.h.finished(self.track, NOW + 8, "stopped"))  # the station was stopped mid-spot
        self.h.ext.on_event(self.h.started(self.track, NOW + 120))  # it came back at the head of the queue
        self.h.ext.on_event(self.h.finished(self.track, NOW + 150))
        self.assertEqual([r["outcome"] for r in self.rows()], ["interrupted", "finished"])
        [entry] = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["interrupted"]), (1, 1))

    def test_a_missed_start_is_recovered_from_the_finish(self):
        self.h.ext.on_event(self.h.finished(self.track, NOW + 30))
        [row] = self.rows()
        self.assertEqual((row["source"], row["outcome"], row["started_at"], row["ended_at"]), ("reconciled", "finished", NOW, NOW + 30))
        self.h.ext.on_event(self.h.finished(self.track, NOW + 30))  # and delivering it again adds nothing
        self.assertEqual(len(self.rows()), 1)
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual(report["campaigns"][0]["totals"]["recovered"], 1)

    def test_a_finish_without_a_start_that_was_cut_short_is_not_made_up(self):
        self.h.ext.on_event(self.h.finished(self.track, NOW + 30, "skipped"))
        self.assertEqual(self.rows(), [])

    def test_a_finish_for_an_ad_that_began_before_the_spot_was_made_is_not_credited_to_it(self):
        # The operator makes the spot while the ad is already on air. The start was missed because nothing used the ad yet.
        self.made_at(NOW - 10)
        self.h.ext.on_event(self.h.finished(self.track, NOW + 5))  # 30 seconds long: it began at NOW - 25, before the spot
        self.assertEqual(self.rows(), [])
        self.h.ext.on_event(self.h.finished(self.track, NOW + 40))  # this one began at NOW + 10, after the spot
        self.assertEqual([r["started_at"] for r in self.rows()], [NOW + 10])

    def test_stopping_an_ad_and_starting_it_again_within_moments_is_two_plays(self):
        events = self.h.ext.on_event
        events(self.h.started(self.track, NOW))
        events(self.h.finished(self.track, NOW + 1, "stopped"))
        events(self.h.started(self.track, NOW + 2.5))  # back at the head of the queue, 2.5 seconds after it first began
        events(self.h.finished(self.track, NOW + 32.5))
        self.assertEqual([(r["started_at"], r["outcome"]) for r in self.rows()], [(NOW, "interrupted"), (NOW + 2.5, "finished")])
        [entry] = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["interrupted"]), (1, 1))  # the full play is not lost
        events(self.h.started(self.track, NOW + 2.5))  # the same start signal arriving twice is still one play
        events(self.h.started(self.track, NOW))
        self.assertEqual(len(self.rows()), 2)

    def test_the_start_signal_of_a_short_ad_is_matched_within_half_its_length(self):
        short = self.h.station.add_track("Quick", duration=2.0)
        self.h.save(advertiser="Quick Co", creatives=[{"kind": "recorded", "track_id": short}])
        events = self.h.ext.on_event
        events(self.h.started(short, NOW, duration=2.0))
        events(self.h.started(short, NOW + 0.4, duration=2.0))  # the same start, stamped a little differently: one play
        self.assertEqual(len(self.rows("WHERE track_id='%s'" % short)), 1)
        events(self.h.started(short, NOW + 2.2, duration=2.0))  # it airs again 2.2 seconds later (the first finish was missed)
        self.assertEqual(len(self.rows("WHERE track_id='%s'" % short)), 2)

    def test_a_station_that_stops_in_the_middle_of_an_ad_does_not_count_it_as_played(self):
        self.h.ext.on_event(self.h.started(self.track, NOW))  # no finish ever arrives: the station is switched off
        self.h.restart()
        [row] = self.rows()
        self.assertEqual((row["outcome"], row["detail"]), ("interrupted", "station restarted"))
        self.h.ext.on_event(self.h.started(self.track, NOW + 60))  # the station starts it again from the top
        self.h.ext.on_event(self.h.finished(self.track, NOW + 90))
        [entry] = self.h.route("GET", "/report", {"campaign": [self.spot["id"]], "from": ["2026-11-05"], "to": ["2026-11-05"]}).body["campaigns"]
        self.assertEqual((entry["totals"]["plays"], entry["totals"]["interrupted"], entry["totals"]["unconfirmed"]), (1, 1, 0))
        self.assertEqual([p["outcome"] for p in entry["plays"]], ["interrupted", "finished"])

    def test_an_ad_replayed_at_once_after_a_restart_is_a_new_play_not_a_repeat_of_the_cut_one(self):
        self.h.ext.on_event(self.h.started(self.track, NOW))
        self.h.clock.advance(1.0)  # the new run of the station notices the stop one second later ...
        self.h.restart()
        self.h.ext.on_event(self.h.started(self.track, NOW + 1.5))  # ... and the ad begins again within the 3-second match window
        self.assertEqual([r["outcome"] for r in self.rows()], ["interrupted", None])

    def test_the_status_remembers_the_last_play_after_a_restart(self):
        self.h.ext.on_event(self.h.started(self.track, NOW))
        self.assertEqual(self.h.status()["last_play_at"], iso(NOW))
        self.h.restart()
        self.assertEqual((self.h.status()["plays_logged"], self.h.status()["last_play_at"]), (1, iso(NOW)))

    def test_a_removed_spot_stops_logging_but_keeps_its_past(self):
        self.h.ext.on_event(self.h.started(self.track, NOW))
        self.h.call("campaign_remove", campaign=self.spot["id"])
        self.h.ext.on_event(self.h.started(self.track, NOW + 600))
        self.assertEqual(len(self.rows()), 1)

    def test_events_after_the_extension_stopped_are_ignored(self):
        self.h.host.stop()
        self.h.ext.on_event(self.h.started(self.track, NOW))
        self.h.host.stop()  # stopping twice is harmless

    def test_a_write_failure_is_reported_in_plain_words_and_never_raised(self):
        self.h.ext._store.conn.close()  # the disk went away underneath the extension
        self.h.ext.on_event(self.h.started(self.track, NOW))
        self.assertIn("as-run log could not be written", self.h.status()["problem"])
        with self.assertRaises(ExtensionError) as caught:
            self.h.call("campaign_pause", campaign=self.spot["id"])
        for word in JARGON:
            self.assertNotIn(word, str(caught.exception))
        checks = {c["id"]: c for c in self.h.host.checks()}
        self.assertEqual(checks["ads.storage"]["status"], "warn")
        self.assertEqual(self.h.route("GET", "/report").status, 400)


class ReconcileTests(LogCase):
    def setUp(self):
        super().setUp()
        self.coffee_track = self.h.station.add_track("Harbor Coffee")
        self.coffee = self.h.save(advertiser="Harbor Coffee Roasters", creatives=[{"kind": "recorded", "track_id": self.coffee_track}])
        self.h.station.add_track("Evening Song", kind="music")
        self.made_at(NOW - 7200)  # the plays recovered below happened in the last 15 minutes: both spots were there by then

    def history(self, *rows):
        """Rows as the station gives them: newest first. Pass them oldest first."""
        self.h.station.history = [{"title": t, "at": iso(at), "event": e} for t, at, e in reversed(rows)]

    def reconcile(self):
        return self.h.ext._reconcile(self.h.ext._station_view(0.0))

    def test_plays_the_live_signal_missed_are_written_down_once(self):
        self.history(("Bike spot", NOW - 900, "playing"), ("Bike spot", NOW - 870, "finished"),
                     ("Evening Song", NOW - 870, "playing"), ("Harbor Coffee", NOW - 300, "playing"), ("Harbor Coffee", NOW - 270, "skipped"))
        first = self.reconcile()
        self.assertEqual((first["added"], first["closed"]), (2, 2))
        rows = self.rows()
        self.assertEqual([(r["track_id"], r["source"], r["outcome"], r["detail"]) for r in rows],
                         [(self.track, "reconciled", "finished", None), (self.coffee_track, "reconciled", "interrupted", "skipped")])
        self.assertAlmostEqual(rows[0]["started_at"], NOW - 900, places=3)
        again = self.reconcile()
        self.assertEqual((again["added"], again["closed"]), (0, 0))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.h.status()["plays_logged"], 2)

    def test_a_play_already_logged_live_is_recognised_by_its_track_and_time(self):
        self.h.ext.on_event(self.h.started(self.track, NOW - 900))
        self.history(("Bike spot", NOW - 900 + 0.0004, "playing"))  # the history stamp is a few microseconds off the event's
        self.assertEqual(self.reconcile()["added"], 0)
        self.assertEqual(len(self.rows()), 1)
        self.history(("Bike spot", NOW - 900 + 5.0, "playing"))  # five seconds apart is another play of a 30-second spot
        self.assertEqual(self.reconcile()["added"], 1)

    def test_a_play_that_was_logged_without_its_ending_gets_the_ending_from_history(self):
        self.h.ext.on_event(self.h.started(self.track, NOW - 900))
        self.history(("Bike spot", NOW - 900, "playing"), ("Bike spot", NOW - 880, "skipped"))
        result = self.reconcile()
        self.assertEqual((result["added"], result["closed"]), (0, 1))
        self.assertEqual(self.rows()[0]["outcome"], "interrupted")

    def test_a_play_from_before_the_spot_existed_is_not_credited_to_it(self):
        # The operator test-played the ad from the library, then made the spot. The first play was not a play of the spot.
        self.made_at(NOW - 600)
        self.history(("Bike spot", NOW - 900, "playing"), ("Bike spot", NOW - 870, "finished"),
                     ("Harbor Coffee", NOW - 500, "playing"), ("Harbor Coffee", NOW - 470, "finished"))
        result = self.reconcile()
        self.assertEqual((result["added"], result["closed"]), (1, 1))
        self.assertEqual([r["track_id"] for r in self.rows()], [self.coffee_track])
        report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        self.assertEqual({c["name"]: c["totals"]["plays"] for c in report["campaigns"]}, {"Corner Bike Co.": 0, "Harbor Coffee Roasters": 1})
        self.assertFalse(any("paid-use confirmation" in n for n in report["notes"]))  # no phantom "aired without confirmation" either
        self.assertEqual(self.reconcile()["added"], 0)  # and looking again does not change the answer

    def test_a_play_exactly_when_the_spot_was_made_counts(self):
        self.made_at(NOW - 900)
        self.history(("Bike spot", NOW - 900, "playing"), ("Bike spot", NOW - 870, "finished"))
        self.assertEqual(self.reconcile()["added"], 1)

    def test_a_recording_attached_to_a_written_message_later_is_credited_only_from_then(self):
        self.h.confirm("voice_rights")
        voice = self.h.station.add_track("Voice for Garden")
        promo = self.h.save(house=True, creatives=[{"kind": "script", "text": "Tell a friend."}])  # made at NOW, no recording yet
        self.h.clock.advance(100)
        self.h.call("spot_audio", campaign=promo["id"], creative=promo["creatives"][0]["id"], track_id=voice)  # attached at NOW + 100
        self.history(("Voice for Garden", NOW + 50, "playing"), ("Voice for Garden", NOW + 80, "finished"),  # played before it was attached
                     ("Voice for Garden", NOW + 150, "playing"), ("Voice for Garden", NOW + 180, "finished"))
        result = self.reconcile()
        self.assertEqual((result["added"], result["closed"]), (1, 1))
        self.assertEqual([r["started_at"] for r in self.rows("WHERE track_id='%s'" % voice)], [NOW + 150])

    def test_swapping_a_spots_recording_restarts_the_clock_for_the_new_one(self):
        other = self.h.station.add_track("Second cut")
        self.h.clock.advance(300)
        self.h.call("campaign_save", campaign={"id": self.spot["id"], "creatives": [
            {"id": self.spot["creatives"][0]["id"], "kind": "recorded", "track_id": other}]})
        self.history(("Second cut", NOW + 100, "playing"), ("Second cut", NOW + 130, "finished"),  # before the swap
                     ("Second cut", NOW + 400, "playing"), ("Second cut", NOW + 430, "finished"))
        self.reconcile()
        self.assertEqual([r["started_at"] for r in self.rows("WHERE track_id='%s'" % other)], [NOW + 400])

    def test_titles_that_cannot_be_told_apart_are_left_alone(self):
        self.h.station.add_track("Bike spot", kind="music")  # a second track with the same title
        self.history(("Bike spot", NOW - 900, "playing"))
        self.assertEqual(self.reconcile()["added"], 0)
        self.assertEqual(self.rows(), [])

    def test_unknown_titles_and_ads_that_belong_to_no_spot_are_ignored(self):
        self.h.station.add_track("Unused ad")
        self.history(("Unused ad", NOW - 900, "playing"), ("Deleted long ago", NOW - 800, "playing"), ("Evening Song", NOW - 700, "playing"))
        self.assertEqual(self.reconcile()["added"], 0)

    def test_history_rows_that_are_not_understood_are_skipped(self):
        self.h.station.history = [{"title": "Bike spot", "at": "yesterday", "event": "playing"}, {"title": "Bike spot", "at": 5, "event": "playing"},
                                  {"title": None, "at": iso(NOW), "event": "playing"}, {"at": iso(NOW)}, {"title": "Bike spot", "at": iso(NOW), "event": "imported"},
                                  {"title": "Bike spot", "at": iso(NOW), "event": "finished"}, "junk", 7, None]
        self.assertEqual(self.reconcile()["added"], 0)
        self.h.station.history = [{"title": "Bike spot", "at": "2026-11-05T14:00:00Z", "event": "playing"},
                                  {"title": "Harbor Coffee", "at": "2026-11-05T14:30:00", "event": "playing"}]
        self.assertEqual(self.reconcile()["added"], 2)  # a trailing Z and a missing offset (read as UTC) are both fine

    def test_an_ending_without_its_start_in_the_window_is_ignored(self):
        self.history(("Bike spot", NOW - 100, "finished"))
        self.assertEqual(self.reconcile(), {"checked": True, "added": 0, "closed": 0})

    def test_it_runs_when_the_extension_starts_after_a_station_restart(self):
        self.history(("Bike spot", NOW - 900, "playing"), ("Bike spot", NOW - 870, "finished"))
        self.h.restart()
        [row] = self.rows()
        self.assertEqual((row["track_id"], row["source"], row["outcome"]), (self.track, "reconciled", "finished"))
        self.assertEqual(self.h.status()["plays_logged"], 1)

    def test_it_runs_before_a_report_so_the_numbers_are_current(self):
        self.history(("Bike spot", NOW - 900, "playing"), ("Bike spot", NOW - 870, "finished"))
        with mock.patch.object(extension_module, "RECONCILE_EVERY", 0.0):
            report = self.h.route("GET", "/report", {"from": ["2026-11-05"], "to": ["2026-11-05"]}).body
        totals = {c["name"]: c["totals"] for c in report["campaigns"]}
        self.assertEqual((totals["Corner Bike Co."]["plays"], totals["Corner Bike Co."]["recovered"]), (1, 1))
        self.assertTrue(any("found afterwards" in n for n in report["notes"]))

    def test_it_does_not_hammer_the_station(self):
        first = self.h.ext._reconcile_now(force=True)
        self.assertTrue(first["checked"])
        calls = self.h.station.calls
        self.assertEqual(self.h.ext._reconcile_now(), {"checked": False})
        self.assertEqual(self.h.station.calls, calls)

    def test_a_station_that_will_not_answer_never_stops_the_extension_starting(self):
        self.h.station.fail = True
        self.h.restart()
        self.assertEqual(self.h.host.status()["ads"]["state"], "running")


class ConcurrencyTests(LogCase):
    def test_many_threads_at_once_leave_a_consistent_station(self):
        errors, done = [], threading.Event()

        def worker(index):
            try:
                for n in range(12):
                    track = self.h.station.add_track(f"T{index}-{n}")
                    saved = self.h.save(advertiser=f"Thread {index} spot {n}", creatives=[{"kind": "recorded", "track_id": track}])
                    self.h.call("campaign_pause", campaign=saved["id"])
                    self.h.call("campaign_resume", campaign=saved["id"])
                    self.h.call("break_build", seconds=60, record=True)
                    self.h.route("GET", "/campaigns")
                    if n % 3 == 0:
                        self.h.call("campaign_remove", campaign=saved["id"])
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

        def events():
            try:
                for n in range(60):
                    self.h.ext.on_event(self.h.started(self.track, NOW + n * 40))
                    self.h.ext.on_event(self.h.finished(self.track, NOW + n * 40 + 30))
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        def poll():
            while not done.is_set():
                self.h.status()
                self.h.host.checks()
                time.sleep(0.002)

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)] + [threading.Thread(target=events)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        done.set()
        poller.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(all(not t.is_alive() for t in threads))
        with self.h.ext._lock:
            self.assertEqual(self.h.ext._store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        # the spot from setUp, plus 4 threads x 12 saves, minus 4 threads x 4 removals.
        self.assertEqual(self.h.status()["campaigns_total"], 1 + 48 - 16)
        self.assertEqual(len(self.rows("WHERE outcome='finished'")), 60)


class StartupFailureTests(unittest.TestCase):
    def test_data_from_a_newer_myradio_stops_only_this_extension_and_says_why(self):
        h = Harness(self)
        h.host.stop()
        conn = sqlite3.connect(h.directory / "ext" / "ads" / "ads.sqlite3")
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        conn.close()
        h._boot()
        entry = h.host.status()["ads"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("newer myradio", entry["error"])
        checks = {c["id"]: c for c in h.host.checks()}
        self.assertEqual(checks["ads.start"]["status"], "fail")
        self.assertEqual(h.host.catalog(), [])

    def test_a_file_that_is_not_a_database_is_reported_in_plain_words(self):
        h = Harness(self)
        h.host.stop()
        (h.directory / "ext" / "ads" / "ads.sqlite3").write_bytes(b"this is not a database" * 100)
        for leftover in (h.directory / "ext" / "ads").glob("ads.sqlite3-*"):
            leftover.unlink()
        with warnings.catch_warnings():
            # The host's own open_db leaves its half-opened connection to the garbage collector when the file is not a
            # database; that is outside this extension (see "Requested changes to shared files").
            warnings.simplefilter("ignore", ResourceWarning)
            h._boot()
            gc.collect()
        entry = h.host.status()["ads"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("damaged or can't be read", entry["error"])
        self.assertNotIn("sqlite3.", entry["error"])


if __name__ == "__main__":
    unittest.main()
