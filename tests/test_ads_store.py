"""The ads database: migrations, persistence across a reopen, uniqueness, retention and the confirmation log."""
import dataclasses
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myradio.ads import store as store_module
from myradio.ads.model import AdsError
from myradio.ads.store import STEPS, AdsStore
from myradio.ads.terms import TERMS_VERSION
from myradio.extensions import Context, ExtensionError

try:
    from test_ads_support import NOW, make
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import NOW, make


def forget_times(campaign):
    return dataclasses.replace(campaign, created_at=0.0, updated_at=0.0)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "ext" / "ads"
        self.state.mkdir(parents=True, mode=0o700)
        self.ctx = Context("ads", Path(self.temp.name), self.state, None)

    def open(self, steps=STEPS):
        conn = self.ctx.open_db("ads.sqlite3")
        conn.execute("PRAGMA synchronous=OFF")  # these tests are about data, not about surviving a power cut
        store = AdsStore(conn, steps=steps)
        self.addCleanup(lambda: store.conn.close() if not self.closed(store) else None)
        return store

    @staticmethod
    def closed(store):
        try:
            store.conn.execute("SELECT 1")
            return False
        except sqlite3.ProgrammingError:
            return True

    def play(self, store, campaign, at, **kw):
        creative = campaign.creatives[0]
        fields = dict(campaign_id=campaign.id, creative_id=creative.id, track_id=creative.track_id, title="t", category=campaign.category,
                      paid=campaign.paid, spoken=False, started_at=at, source="event")
        fields.update(kw)
        return store.add_play(**fields)


class MigrationTests(StoreCase):
    def test_a_new_database_is_at_the_latest_version_and_private(self):
        store = self.open()
        self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], len(STEPS))
        self.assertEqual(stat.S_IMODE(os.stat(self.state / "ads.sqlite3").st_mode), 0o600)
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"campaigns", "creatives", "acknowledgements", "planned", "plays", "skips"} <= tables)

    def test_reopening_applies_nothing_twice(self):
        store = self.open()
        store.save_campaign(make("Acme"), now=1.0, commercial_ack_id=None)
        store.conn.close()
        again = self.open()
        self.assertEqual(again.conn.execute("PRAGMA user_version").fetchone()[0], len(STEPS))
        self.assertEqual([c.name for c in again.load()], ["Acme"])

    def test_the_first_step_is_safe_to_run_again_after_a_crash_before_the_version_was_written(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        self.play(store, campaign, NOW)
        store.set_ack("voice_rights", True, 5.0)
        with store.conn:
            store.conn.execute("PRAGMA user_version=0")  # the schema exists but the version number never got written
        store.conn.close()
        again = self.open()
        self.assertEqual(again.conn.execute("PRAGMA user_version").fetchone()[0], len(STEPS))
        self.assertEqual(([c.name for c in again.load()], again.play_count(), again.ack_state()["voice_rights"]["at"]), (["Acme"], 1, 5.0))

    def test_a_later_step_is_applied_to_an_older_database_and_keeps_the_data(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        self.play(store, campaign, NOW)
        store.conn.close()
        newer = self.open(STEPS + ["ALTER TABLE plays ADD COLUMN note TEXT;"])
        self.assertEqual(newer.conn.execute("PRAGMA user_version").fetchone()[0], len(STEPS) + 1)
        self.assertIn("note", [r[1] for r in newer.conn.execute("PRAGMA table_info(plays)")])
        self.assertEqual(newer.play_count(), 1)
        self.assertEqual([c.name for c in newer.load()], ["Acme"])

    def test_a_version_one_database_learns_when_its_recordings_were_attached_from_when_the_spots_were_made(self):
        store = self.open()
        campaign = make("Acme", creatives=[{"kind": "recorded", "track_id": "a", "title": "A"}, {"kind": "script", "text": "Buy bikes."}])
        store.save_campaign(campaign, now=100.0, commercial_ack_id=None)
        self.play(store, campaign, NOW)
        with store.conn:  # what an install from before this step looked like: no table, version 1
            store.conn.execute("DROP TABLE attachments")
            store.conn.execute("PRAGMA user_version=1")
        store.conn.close()
        again = self.open()
        self.assertEqual(again.conn.execute("PRAGMA user_version").fetchone()[0], len(STEPS))
        self.assertEqual(again.attached_times(), {campaign.creatives[0].id: ("a", 100.0)})  # the written message has no recording
        self.assertEqual(([c.name for c in again.load()], again.play_count()), (["Acme"], 1))

    def test_the_attachment_step_is_safe_to_run_again_and_never_resets_a_time(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=100.0, commercial_ack_id=None)
        creative = campaign.creatives[0].id
        self.assertTrue(store.set_creative_track(campaign.id, creative, "swapped", 250.0))
        with store.conn:
            store.conn.execute("PRAGMA user_version=1")  # the table and its rows exist, but the version number was never written
        store.conn.close()
        again = self.open()
        self.assertEqual(again.attached_times(), {creative: ("swapped", 250.0)})

    def test_data_from_a_newer_myradio_is_refused_with_a_plain_message(self):
        store = self.open(STEPS + ["CREATE TABLE later(x);"])
        store.conn.close()
        conn = self.ctx.open_db("ads.sqlite3")
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(ExtensionError, "newer myradio"):
            AdsStore(conn)


class CampaignPersistenceTests(StoreCase):
    def test_everything_about_a_spot_survives_a_reopen(self):
        campaign = make("Acme", category="bikes", priority=8, plan={"total": 40}, pacing="asap", timezone="America/New_York",
                        flights=[{"start": "2026-11-12", "end": "2026-12-31", "days": "1,3,5", "show_ids": ["morning"],
                                  "dayparts": [{"from": "07:00", "to": "12:00"}]}],
                        creatives=[{"kind": "recorded", "track_id": "rec-1", "title": "Tune-up"},
                                   {"kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex",
                                    "disclosure": "Today is sponsored by {sponsor}"}],
                        caps={"per_hour": 2, "per_day": None, "per_week": 9}, separation={"campaign_minutes": 15, "category_minutes": 7})
        store = self.open()
        store.save_campaign(campaign, now=100.0, commercial_ack_id=None)
        store.set_paused(campaign.id, True, 200.0)
        store.conn.close()
        again = self.open()
        [loaded] = again.load()
        self.assertEqual(forget_times(loaded), forget_times(dataclasses.replace(campaign, paused=True)))
        self.assertEqual((loaded.created_at, loaded.updated_at), (100.0, 200.0))
        self.assertEqual(again.get(campaign.id), loaded)

    def test_a_second_save_updates_in_place_and_drops_messages_left_out(self):
        store = self.open()
        campaign = make("Acme", creatives=[{"kind": "recorded", "track_id": "a", "title": "A"}, {"kind": "recorded", "track_id": "b", "title": "B"}])
        store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        edited = dataclasses.replace(campaign, name="Acme Two", creatives=(campaign.creatives[1],))
        store.save_campaign(edited, now=2.0, commercial_ack_id=None)
        [loaded] = store.load()
        self.assertEqual((loaded.name, [c.track_id for c in loaded.creatives], loaded.created_at, loaded.updated_at), ("Acme Two", ["b"], 1.0, 2.0))
        rows = store.conn.execute("SELECT id, removed_at FROM creatives ORDER BY position, id").fetchall()
        self.assertEqual(sorted(r["removed_at"] is None for r in rows), [False, True])

    def test_repeated_edits_cannot_grow_the_file_without_limit(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        with mock.patch.object(store_module, "MAX_OLD_MESSAGES_KEPT", 3):
            for i in range(12):  # every save brings brand-new messages, as an agent that leaves out the ids would
                fresh = make("Acme", creatives=[{"kind": "recorded", "track_id": f"take-{i}", "title": f"Take {i}"}])
                store.save_campaign(dataclasses.replace(campaign, creatives=fresh.creatives), now=2.0 + i, commercial_ack_id=None)
        rows = store.conn.execute("SELECT removed_at IS NULL AS live, COUNT(*) AS n FROM creatives WHERE campaign_id=? GROUP BY live",
                                  (campaign.id,)).fetchall()
        self.assertEqual({r["live"]: r["n"] for r in rows}, {1: 1, 0: 3})
        self.assertEqual([c.track_id for c in store.get(campaign.id).creatives], ["take-11"])
        store.remove(campaign.id, 100.0)  # a removed spot still reads back with the message it had when it was removed
        [gone] = store.load(include_removed=True)
        self.assertEqual([c.track_id for c in gone.creatives], ["take-11"])

    def test_a_recording_belongs_to_one_live_message_at_a_time(self):
        store = self.open()
        first, second = make("First", track="shared"), make("Second", track="shared")
        store.save_campaign(first, now=1.0, commercial_ack_id=None)
        with self.assertRaisesRegex(AdsError, "already used by another spot"):
            store.save_campaign(second, now=2.0, commercial_ack_id=None)
        self.assertEqual([c.name for c in store.load()], ["First"])  # the failed save left nothing behind
        store.remove(first.id, 3.0)
        store.save_campaign(second, now=4.0, commercial_ack_id=None)  # free again once the first is removed
        self.assertEqual([c.name for c in store.load()], ["Second"])

    def test_two_messages_can_swap_recordings_in_one_save(self):
        store = self.open()
        campaign = make("Acme", creatives=[{"kind": "recorded", "track_id": "a", "title": "A"}, {"kind": "recorded", "track_id": "b", "title": "B"}])
        store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        one, two = campaign.creatives
        swapped = dataclasses.replace(campaign, creatives=(dataclasses.replace(one, track_id="b"), dataclasses.replace(two, track_id="a")))
        store.save_campaign(swapped, now=2.0, commercial_ack_id=None)
        self.assertEqual([c.track_id for c in store.get(campaign.id).creatives], ["b", "a"])

    def test_removing_keeps_the_history_for_reports(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        self.play(store, campaign, NOW)
        store.add_planned("b" * 32, [(campaign.id, campaign.creatives[0].id, "track-acme", NOW + 60)], NOW)
        self.assertTrue(store.remove(campaign.id, NOW + 1))
        self.assertFalse(store.remove(campaign.id, NOW + 2))
        self.assertEqual(store.load(), [])
        self.assertEqual(store.count_active(), 0)
        [kept] = store.load(include_removed=True)
        self.assertEqual(kept.removed_at, NOW + 1)
        self.assertEqual(store.play_count(), 1)
        self.assertEqual(store.active_planned(NOW, 3600), [])
        self.assertFalse(store.set_paused(campaign.id, True, NOW))

    def test_the_oldest_removed_spots_are_forgotten_beyond_the_limit(self):
        store = self.open()
        with mock.patch.object(store_module, "MAX_REMOVED_KEPT", 3):
            for i in range(6):
                campaign = make(f"Gone {i}")
                store.save_campaign(campaign, now=float(i), commercial_ack_id=None)
                self.play(store, campaign, NOW + i)
                store.remove(campaign.id, 100.0 + i)
        names = sorted(c.name for c in store.load(include_removed=True))
        self.assertEqual(names, ["Gone 3", "Gone 4", "Gone 5"])
        self.assertEqual(store.play_count(), 3)

    def test_an_unreadable_row_is_skipped_and_counted_not_fatal(self):
        store = self.open()
        good, bad = make("Good"), make("Bad")
        store.save_campaign(good, now=1.0, commercial_ack_id=None)
        store.save_campaign(bad, now=2.0, commercial_ack_id=None)
        with store.conn:
            store.conn.execute("UPDATE campaigns SET settings='{not json' WHERE id=?", (bad.id,))
        self.assertEqual([c.name for c in store.load()], ["Good"])
        self.assertEqual(store.damaged, 1)
        with store.conn:
            store.conn.execute("UPDATE campaigns SET priority=99 WHERE id=?", (good.id,))
        self.assertEqual(store.load(), [])
        self.assertEqual(store.damaged, 2)

    def test_a_timezone_the_computer_has_lost_keeps_the_spot_visible(self):
        store = self.open()
        spot = make("Acme")
        store.save_campaign(spot, now=1.0, commercial_ack_id=None)
        with store.conn:
            store.conn.execute("UPDATE campaigns SET timezone='Mars/Base' WHERE id=?", (spot.id,))
        [loaded] = store.load()
        self.assertEqual((loaded.name, loaded.timezone, store.damaged), ("Acme", "Mars/Base", 0))

    def test_the_paid_use_confirmation_a_spot_was_saved_under_is_remembered(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=1.0, commercial_ack_id=7)
        self.assertEqual(store.commercial_ack_for(campaign.id), 7)
        self.assertIsNone(store.commercial_ack_for("0" * 32))


class AttachmentTests(StoreCase):
    """A recording only counts from the moment it was attached to a message."""

    def test_a_recording_keeps_its_time_while_it_stays_and_starts_counting_again_when_it_changes(self):
        store = self.open()
        campaign = make("Acme", creatives=[{"kind": "recorded", "track_id": "a", "title": "A"}, {"kind": "script", "text": "Buy bikes."}])
        store.save_campaign(campaign, now=100.0, commercial_ack_id=None)
        recorded, script = campaign.creatives
        self.assertEqual(store.attached_times(), {recorded.id: ("a", 100.0)})  # the written message has no recording yet
        store.save_campaign(dataclasses.replace(campaign, name="Acme Two"), now=200.0, commercial_ack_id=None)
        self.assertEqual(store.attached_times()[recorded.id], ("a", 100.0))  # saved again with the same recording: same time
        swapped = dataclasses.replace(campaign, creatives=(dataclasses.replace(recorded, track_id="b"), script))
        store.save_campaign(swapped, now=300.0, commercial_ack_id=None)
        self.assertEqual(store.attached_times()[recorded.id], ("b", 300.0))  # a different recording counts from now
        self.assertTrue(store.set_creative_track(campaign.id, script.id, "voice", 400.0))  # the voice recording arrives later
        self.assertEqual(store.attached_times()[script.id], ("voice", 400.0))
        self.assertTrue(store.set_creative_track(campaign.id, script.id, "voice", 500.0))
        self.assertEqual(store.attached_times()[script.id], ("voice", 400.0))  # attaching the one it has changes nothing

    def test_taking_a_recording_off_or_the_message_out_forgets_its_time(self):
        store = self.open()
        campaign = make("Acme", creatives=[{"kind": "recorded", "track_id": "a", "title": "A"}, {"kind": "script", "text": "Buy bikes."}])
        store.save_campaign(campaign, now=100.0, commercial_ack_id=None)
        recorded, script = campaign.creatives
        store.set_creative_track(campaign.id, script.id, "voice", 150.0)
        self.assertEqual(set(store.attached_times()), {recorded.id, script.id})
        store.save_campaign(campaign, now=200.0, commercial_ack_id=None)  # saved again with the written message bare
        self.assertEqual(set(store.attached_times()), {recorded.id})  # the voice recording was taken off it
        store.save_campaign(dataclasses.replace(campaign, creatives=(dataclasses.replace(script, track_id=None),)), now=300.0,
                            commercial_ack_id=None)
        self.assertEqual(store.attached_times(), {})  # the recorded message is gone from the spot

    def test_the_times_survive_a_reopen(self):
        store = self.open()
        campaign = make("Acme")
        store.save_campaign(campaign, now=100.0, commercial_ack_id=None)
        store.conn.close()
        self.assertEqual(self.open().attached_times(), {campaign.creatives[0].id: ("track-acme", 100.0)})

    def test_forgetting_the_oldest_removed_spots_forgets_their_attachment_times_too(self):
        store = self.open()
        with mock.patch.object(store_module, "MAX_REMOVED_KEPT", 1):
            for i in range(3):
                spot = make(f"Gone {i}")
                store.save_campaign(spot, now=float(i), commercial_ack_id=None)
                store.remove(spot.id, 100.0 + i)
        self.assertEqual(len(store.attached_times()), 1)


class LogTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store = self.open()
        self.campaign = make("Acme")
        self.store.save_campaign(self.campaign, now=1.0, commercial_ack_id=None)

    def test_the_same_play_cannot_be_written_twice(self):
        self.play(self.store, self.campaign, NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.play(self.store, self.campaign, NOW)
        self.assertEqual(self.store.play_count(), 1)

    def test_a_play_is_found_by_track_and_nearby_start_time(self):
        self.play(self.store, self.campaign, NOW)
        for offset, tolerance, found in ((0.0, 0.05, True), (0.5, 1.0, True), (-0.5, 1.0, True), (2.0, 1.0, False), (1.0, 1.0, True)):
            with self.subTest(offset=offset, tolerance=tolerance):
                self.assertEqual(self.store.find_play("track-acme", NOW + offset, tolerance) is not None, found)
        self.assertIsNone(self.store.find_play("another-track", NOW, 5.0))

    def test_open_plays_are_closed_once(self):
        first = self.play(self.store, self.campaign, NOW)
        second = self.play(self.store, self.campaign, NOW + 600)
        self.assertEqual(self.store.open_play("track-acme", NOW + 601)["id"], second)
        self.assertEqual(self.store.open_play("track-acme", NOW + 100)["id"], first)
        self.assertIsNone(self.store.open_play("track-acme", NOW - 100))
        self.assertTrue(self.store.close_play(second, NOW + 630, "interrupted", "skipped"))
        self.assertFalse(self.store.close_play(second, NOW + 700, "finished", None))  # already closed
        self.assertEqual(self.store.open_play("track-acme", NOW + 700)["id"], first)

    def test_counts_leave_out_plays_that_were_cut_short(self):
        self.play(self.store, self.campaign, NOW)
        self.play(self.store, self.campaign, NOW + 100, outcome="interrupted", detail="skipped")
        self.play(self.store, self.campaign, NOW + 200, outcome="finished")
        self.assertEqual(self.store.delivered_totals(), {self.campaign.id: 2})
        self.assertEqual(self.store.plays_since(NOW + 150), {self.campaign.id: 1})
        self.assertEqual(self.store.last_plays(), {self.campaign.id: NOW + 200})
        self.assertEqual([r["started_at"] for r in self.store.recent_plays(NOW - 1, NOW + 150)], [NOW, NOW + 100])

    def test_planned_plays_wait_to_be_matched_and_are_forgotten_after_the_grace(self):
        creative = self.campaign.creatives[0].id
        self.store.add_planned("b" * 32, [(self.campaign.id, creative, "track-acme", NOW + 60), (self.campaign.id, creative, "track-acme", NOW + 660)], NOW)
        self.assertEqual([r["planned_at"] for r in self.store.active_planned(NOW, 1800)], [NOW + 60, NOW + 660])
        self.assertEqual([r["planned_at"] for r in self.store.active_planned(NOW + 3000, 1800)], [])
        first = self.store.link_planned(creative, NOW + 70, 1800)
        self.assertEqual([r["planned_at"] for r in self.store.active_planned(NOW, 1800)], [NOW + 660])  # the oldest one was used up
        second = self.store.link_planned(creative, NOW + 700, 1800)
        self.assertEqual((first is not None, second is not None, first != second), (True, True, True))
        self.assertIsNone(self.store.link_planned(creative, NOW + 800, 1800))  # nothing waiting any more
        self.assertEqual(self.store.active_planned(NOW, 1800), [])

    def test_only_the_newest_planned_plays_are_kept(self):
        creative = self.campaign.creatives[0].id
        with mock.patch.object(store_module, "MAX_PLANNED_KEPT", 5):
            for i in range(9):
                self.store.add_planned(f"{i:032x}", [(self.campaign.id, creative, "track-acme", NOW + i)], NOW)
        times = [r["planned_at"] for r in self.store.active_planned(NOW, 3600)]
        self.assertEqual(times, [NOW + i for i in range(4, 9)])

    def test_held_back_counts_accumulate_per_day_spot_and_reason(self):
        self.store.bump_skips([("2026-11-05", self.campaign.id, "hour_cap")] * 3 + [("2026-11-05", self.campaign.id, "day_cap"),
                                                                                      ("2026-11-06", self.campaign.id, "hour_cap")])
        self.store.bump_skips([("2026-11-05", self.campaign.id, "hour_cap")])
        self.assertEqual(self.store.skip_counts(self.campaign.id, "2026-11-05", "2026-11-06"),
                         {"2026-11-05": {"hour_cap": 4, "day_cap": 1}, "2026-11-06": {"hour_cap": 1}})
        self.assertEqual(self.store.skip_counts(self.campaign.id, "2026-11-06", "2026-11-06"), {"2026-11-06": {"hour_cap": 1}})
        self.assertEqual(self.store.skip_counts("0" * 32, "2026-11-05", "2026-11-06"), {})

    def test_old_history_is_pruned(self):
        old = NOW - 801 * 86400
        self.play(self.store, self.campaign, old)
        self.play(self.store, self.campaign, NOW - 10)
        self.store.bump_skips([("2024-01-01", self.campaign.id, "hour_cap"), ("2026-11-05", self.campaign.id, "hour_cap")])
        self.store.add_planned("b" * 32, [(self.campaign.id, self.campaign.creatives[0].id, "track-acme", NOW - 3 * 86400)], NOW)
        self.store.prune(NOW)
        self.assertEqual(self.store.play_count(), 1)
        self.assertEqual(list(self.store.skip_counts(self.campaign.id, "2000-01-01", "2030-01-01")), ["2026-11-05"])
        self.assertEqual(self.store.active_planned(NOW - 10 * 86400, 10 ** 9), [])

    def test_a_clock_far_in_the_future_cannot_wipe_the_history(self):
        # A dead clock battery or a restored virtual machine can leave the date years ahead. Going by that date, every
        # play would be "older than 800 days" and the billing evidence would be deleted for good.
        self.play(self.store, self.campaign, NOW - 10)
        self.play(self.store, self.campaign, NOW - 400 * 86400)
        self.store.bump_skips([("2026-11-05", self.campaign.id, "hour_cap")])
        self.store.add_planned("b" * 32, [(self.campaign.id, self.campaign.creatives[0].id, "track-acme", NOW + 60)], NOW)
        self.store.prune(NOW + 5000 * 86400)
        self.assertEqual(self.store.play_count(), 2)
        self.assertEqual(list(self.store.skip_counts(self.campaign.id, "2000-01-01", "2030-01-01")), ["2026-11-05"])
        self.assertEqual(len(self.store.active_planned(NOW, 3600)), 1)

    def test_pruning_still_works_after_a_long_quiet_spell(self):
        self.play(self.store, self.campaign, NOW - 900 * 86400)
        self.play(self.store, self.campaign, NOW - 10 * 86400)
        self.store.prune(NOW)  # nothing new for ten days: the cut-off is a day after the newest record, not today
        self.assertEqual(self.store.play_count(), 1)
        self.store.prune(NOW + 2000 * 86400)
        self.assertEqual(self.store.play_count(), 1)  # ... and a wild clock changes nothing more

    def test_plays_that_never_ended_can_be_closed_as_cut_short(self):
        self.assertIsNone(self.store.newest_play_time())
        first = self.play(self.store, self.campaign, NOW)
        self.play(self.store, self.campaign, NOW + 100, outcome="finished", ended_at=NOW + 130)
        far = self.play(self.store, self.campaign, NOW + 5000)  # a start stamped after the moment of the restart
        self.assertEqual(self.store.close_open_plays(NOW + 400, "station restarted"), 2)
        rows = {r["id"]: r for r in self.store.conn.execute("SELECT * FROM plays")}
        self.assertEqual((rows[first]["outcome"], rows[first]["detail"], rows[first]["ended_at"]), ("interrupted", "station restarted", NOW + 400))
        self.assertEqual(rows[far]["ended_at"], NOW + 5000)  # never before it started
        self.assertEqual([r["outcome"] for r in self.store.conn.execute("SELECT outcome FROM plays ORDER BY started_at")],
                         ["interrupted", "finished", "interrupted"])
        self.assertEqual(self.store.close_open_plays(NOW + 500, "again"), 0)
        self.assertEqual(self.store.newest_play_time(), NOW + 5000)
        self.assertEqual(self.store.delivered_totals(), {self.campaign.id: 1})  # only the one that finished is delivered


class ConfirmationTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store = self.open()

    def test_nothing_is_confirmed_until_it_is_recorded(self):
        for state in self.store.ack_state().values():
            self.assertEqual((state["acknowledged"], state["at"], state["ack_id"]), (False, None, None))

    def test_a_confirmation_is_stored_with_its_time_and_wording_version(self):
        self.assertTrue(self.store.set_ack("voice_rights", True, 1000.0))
        state = self.store.ack_state()["voice_rights"]
        self.assertEqual((state["acknowledged"], state["at"], state["version"]), (True, 1000.0, TERMS_VERSION))
        self.assertFalse(self.store.ack_state()["commercial_use"]["acknowledged"])
        self.assertFalse(self.store.set_ack("voice_rights", True, 2000.0))  # nothing changes, the time stays
        self.assertEqual(self.store.ack_state()["voice_rights"]["at"], 1000.0)

    def test_a_confirmation_can_be_withdrawn_and_given_again(self):
        self.store.set_ack("commercial_use", True, 1000.0)
        self.assertTrue(self.store.set_ack("commercial_use", False, 2000.0))
        self.assertFalse(self.store.set_ack("commercial_use", False, 2500.0))
        state = self.store.ack_state()["commercial_use"]
        self.assertEqual((state["acknowledged"], state["at"]), (False, None))
        self.assertTrue(self.store.set_ack("commercial_use", True, 3000.0))
        self.assertEqual(self.store.ack_state()["commercial_use"]["at"], 3000.0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 3)

    def test_withdrawing_something_never_confirmed_does_nothing(self):
        self.assertFalse(self.store.set_ack("voice_rights", False, 1000.0))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 0)

    def test_a_withdrawal_stays_a_withdrawal_when_the_clock_was_put_back(self):
        # The computer's date is corrected backwards between a confirmation and its withdrawal. The withdrawal is dated
        # earlier, but it was made later, and it is the last word: the legal gate must close.
        self.store.set_ack("voice_rights", True, 1000.0)
        self.assertTrue(self.store.set_ack("voice_rights", False, 900.0))
        state = self.store.ack_state()["voice_rights"]
        self.assertEqual((state["acknowledged"], state["at"], state["ack_id"]), (False, None, None))
        self.assertIsNone(self.store.ack_id_at("voice_rights", 1200.0))
        self.assertTrue(self.store.set_ack("voice_rights", True, 800.0))  # confirmed again, still with the clock behind
        self.assertTrue(self.store.ack_state()["voice_rights"]["acknowledged"])
        self.assertIsNotNone(self.store.ack_id_at("voice_rights", 1200.0))
        self.assertIsNone(self.store.ack_id_at("voice_rights", 799.0))  # but nothing was confirmed before 800

    def test_the_confirmation_in_force_at_a_moment(self):
        self.store.set_ack("commercial_use", True, 1000.0)
        self.store.set_ack("commercial_use", False, 2000.0)
        self.store.set_ack("commercial_use", True, 3000.0)
        ids = [self.store.ack_id_at("commercial_use", t) for t in (999.0, 1000.0, 1999.0, 2000.0, 2999.0, 3000.0, 9999.0)]
        self.assertIsNone(ids[0])
        self.assertIsNotNone(ids[1])
        self.assertEqual(ids[1], ids[2])
        self.assertIsNone(ids[3])
        self.assertIsNone(ids[4])
        self.assertIsNotNone(ids[5])
        self.assertNotEqual(ids[5], ids[1])
        self.assertEqual(ids[5], ids[6])
        self.assertIsNone(self.store.ack_id_at("voice_rights", 9999.0))

    def test_new_wording_asks_for_a_fresh_confirmation(self):
        self.store.set_ack("voice_rights", True, 1000.0)
        with mock.patch.object(store_module, "TERMS_VERSION", TERMS_VERSION + 1):
            state = self.store.ack_state()["voice_rights"]
            self.assertEqual((state["acknowledged"], state["needs_reconfirm"]), (False, True))
            self.assertIsNone(self.store.ack_id_at("voice_rights", 5000.0))
            self.assertTrue(self.store.set_ack("voice_rights", True, 6000.0))
            self.assertTrue(self.store.ack_state()["voice_rights"]["acknowledged"])

    def test_a_confirmation_survives_a_reopen_and_unknown_kinds_are_refused(self):
        self.store.set_ack("voice_rights", True, 1000.0)
        self.store.conn.close()
        again = self.open()
        self.assertEqual(again.ack_state()["voice_rights"]["at"], 1000.0)
        with self.assertRaises(AdsError):
            again.set_ack("everything", True, 1.0)

    def test_the_log_is_bounded_but_keeps_what_a_play_points_to(self):
        campaign = make("Acme")
        self.store.save_campaign(campaign, now=1.0, commercial_ack_id=None)
        self.store.set_ack("commercial_use", True, 1.0)
        first = self.store.ack_id_at("commercial_use", 1.0)
        self.play(self.store, campaign, 5.0, commercial_ack_id=first)
        with mock.patch.object(store_module, "MAX_ACK_ROWS", 20), mock.patch.object(store_module, "KEEP_ACK_ROWS", 5):
            for i in range(100):
                self.store.set_ack("commercial_use", i % 2 == 1, 10.0 + i)
        total = self.store.conn.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0]
        self.assertLess(total, 30)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM acknowledgements WHERE id=?", (first,)).fetchone()[0], 1)
        self.assertEqual(self.store.ack_state()["commercial_use"]["acknowledged"], True)


if __name__ == "__main__":
    unittest.main()
