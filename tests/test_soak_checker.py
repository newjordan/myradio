"""The soak harness is only worth something if it fails when the engine is wrong.

These tests check the harness itself: its independent statement of the recurrence rules against hand-worked answers
and against the engine, its on-disk state checker against deliberately damaged data directories, and the whole
accelerated harness against engines that have been made wrong on purpose (mutants), which it must reject by name.
"""
import contextlib
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_core as C  # noqa: E402
import soak_station as S  # noqa: E402
from myradio import station as station_module  # noqa: E402
from myradio.station import FRAMES, SILENCE, Station  # noqa: E402


def local(year, month, day, hour, minute, hours, minutes=0):
    """The instant a wall time shows on a clock running at a fixed UTC offset (no time zone database involved)."""
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone(dt.timedelta(hours=hours, minutes=minutes))).timestamp()


DAILY = "1,2,3,4,5,6,7"


class RecurrenceRuleTests(unittest.TestCase):
    """Answers worked out by hand from the DST rules, using fixed offsets so the time zone database is not involved."""

    def test_new_york_spring_forward_skips_the_missing_time(self):
        after = local(2026, 3, 8, 0, 0, -5)
        self.assertEqual(C.oracle_next_due("America/New_York", "02:30", "7", after), local(2026, 3, 15, 2, 30, -4))
        self.assertEqual(C.oracle_next_due("America/New_York", "02:30", DAILY, after), local(2026, 3, 9, 2, 30, -4))
        self.assertEqual(C.oracle_next_due("America/New_York", "03:30", DAILY, after), local(2026, 3, 8, 3, 30, -4))

    def test_new_york_fall_back_airs_the_repeated_time_once_at_its_first_pass(self):
        before = local(2026, 11, 1, 0, 0, -4)
        first = local(2026, 11, 1, 1, 30, -4)
        self.assertEqual(C.oracle_next_due("America/New_York", "01:30", "7", before), first)
        self.assertEqual(C.oracle_next_due("America/New_York", "01:30", "7", first), local(2026, 11, 8, 1, 30, -5))
        # Between the first and second pass of 01:30 the second pass must not count: the next airing is tomorrow's.
        self.assertEqual(C.oracle_next_due("America/New_York", "01:30", DAILY, first + 600), local(2026, 11, 2, 1, 30, -5))

    def test_half_hour_dst_in_lord_howe(self):
        # 2026-04-05 02:00 +11:00 becomes 01:30 +10:30; 2026-10-04 02:00 +10:30 becomes 02:30 +11:00.
        self.assertEqual(C.oracle_next_due("Australia/Lord_Howe", "01:45", DAILY, local(2026, 4, 5, 1, 45, 11)), local(2026, 4, 6, 1, 45, 10, 30))
        self.assertEqual(C.oracle_next_due("Australia/Lord_Howe", "01:45", DAILY, local(2026, 4, 4, 1, 45, 11)), local(2026, 4, 5, 1, 45, 11))
        self.assertEqual(C.oracle_next_due("Australia/Lord_Howe", "02:15", DAILY, local(2026, 10, 3, 2, 15, 10, 30)), local(2026, 10, 5, 2, 15, 11))
        self.assertEqual(C.oracle_next_due("Australia/Lord_Howe", "02:30", DAILY, local(2026, 10, 3, 2, 30, 10, 30)), local(2026, 10, 4, 2, 30, 11))

    def test_ireland_counts_winter_as_negative_dst_but_the_clock_changes_the_same_way(self):
        self.assertEqual(C.oracle_next_due("Europe/Dublin", "01:30", DAILY, local(2026, 3, 28, 1, 30, 0)), local(2026, 3, 30, 1, 30, 1))
        self.assertEqual(C.oracle_next_due("Europe/Dublin", "01:30", DAILY, local(2026, 10, 24, 1, 30, 1)), local(2026, 10, 25, 1, 30, 1))

    def test_zones_without_dst_and_weekday_filters(self):
        self.assertEqual(C.oracle_next_due("Asia/Kolkata", "12:00", DAILY, local(2026, 6, 1, 12, 0, 5, 30)), local(2026, 6, 2, 12, 0, 5, 30))
        monday = dt.datetime(2026, 3, 2, 10, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(C.oracle_next_due("UTC", "10:00", "1", monday), monday + 7 * 86400)
        self.assertEqual(C.oracle_next_due("UTC", "10:00", "1", monday - 1), monday)
        self.assertEqual(C.oracle_next_due("UTC", "10:00", "3,1", monday), monday + 2 * 86400)

    def test_offset_changes_are_found_to_the_second(self):
        found = C.zone_transitions("America/New_York", local(2026, 1, 1, 0, 0, 0), local(2027, 1, 1, 0, 0, 0))
        hour = dt.timedelta(hours=1)
        self.assertEqual(found, [(int(local(2026, 3, 8, 2, 0, -5)), -5 * hour, -4 * hour), (int(local(2026, 11, 1, 2, 0, -4)), -4 * hour, -5 * hour)])
        self.assertEqual(C.zone_transitions("Asia/Kolkata", local(2026, 1, 1, 0, 0, 0), local(2030, 1, 1, 0, 0, 0)), [])

    def test_wall_instants_gap_and_repeat(self):
        zone = C.tz("America/New_York")
        self.assertEqual(C.wall_instants(zone, dt.datetime(2026, 3, 8, 2, 30)), [])
        self.assertEqual(C.wall_instants(zone, dt.datetime(2026, 11, 1, 1, 30)), [local(2026, 11, 1, 1, 30, -4), local(2026, 11, 1, 1, 30, -5)])
        self.assertEqual(len(C.wall_instants(zone, dt.datetime(2026, 7, 1, 12, 0))), 1)

    def test_agrees_with_the_engine_around_every_offset_change_of_the_zones_in_use(self):
        window = (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(2028, 1, 1, tzinfo=dt.timezone.utc))
        result = C.cross_check_recurrence(C.available_zones(C.ZONES), window=window, random_per_zone=20)
        self.assertTrue(result.ok, result.text())
        self.assertGreater(result.compared, 5000)
        self.assertGreater(result.transitions, 10)

    def test_the_cross_check_notices_a_wrong_engine(self):
        # An engine that adds 24 hours of UTC instead of moving to the next local day gets the day after a transition wrong.
        with mock.patch("myradio.scheduling.next_occurrence", lambda zone, t, days, after: after + 86400):
            window = (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))
            self.assertFalse(C.cross_check_recurrence(["America/New_York"], window=window, random_per_zone=5).ok)


class TapeTests(unittest.TestCase):
    def setUp(self):
        self.tapes = C.Tapes()

    def test_blocks_split_into_the_tracks_they_came_from(self):
        block = self.tapes.window(3, 10, 100) + self.tapes.window(7, 0, FRAMES - 100)
        self.assertEqual(self.tapes.parse(block), [(3, 10, 100), (7, 0, FRAMES - 100)])
        self.assertEqual(self.tapes.parse(SILENCE), [(None, 0, FRAMES)])
        self.assertEqual(self.tapes.parse(self.tapes.window(5, 4, 2000) + bytes(2410 * 4)), [(5, 4, 2000), (None, 0, 2410)])

    def test_anything_else_is_not_audio_of_a_known_track(self):
        good = self.tapes.window(2, 0, 100)
        self.assertIsNone(self.tapes.parse(good[:-1]))  # not whole frames
        self.assertIsNone(self.tapes.parse(good[:4] + b"\x01\x00\x02\x00" + good[8:]))  # left and right differ
        self.assertIsNone(self.tapes.parse(b"\xff\x7f\xff\x7f" * 10))  # a value beyond every track
        self.assertIsNone(self.tapes.parse(b"\x0e\x00\x0e\x00" * 10))  # position 13 does not exist: tapes repeat every 13 frames
        self.assertIsNone(self.tapes.parse(good[:40] + good[44:48] + good[40:44] + good[48:]))  # frames out of order: more than three runs
        self.assertIsNone(self.tapes.parse(self.tapes.window(1, 0, 10) + self.tapes.window(2, 0, 10) + self.tapes.window(3, 0, 10) + self.tapes.window(4, 0, 10)))

    def test_a_mono_import_is_the_same_tape_in_both_channels(self):
        with S.session(seed=2, fast_commits=True) as h:
            track = h.op_import(kind="music", frames=FRAMES, channels=1)
            data = (h.station.directory / "media" / (track.id + ".wav")).read_bytes()
            self.assertEqual(data[44:], self.tapes.window(track.slot, 0, FRAMES))

    def test_every_track_has_its_own_tape(self):
        self.assertEqual(len({self.tapes.window(slot, 0, 13) for slot in range(C.MAX_SLOTS)}), C.MAX_SLOTS)


class StateCheckerTests(unittest.TestCase):
    """Each damage below is something the engine must never leave behind; the checker has to name it."""

    @contextlib.contextmanager
    def station(self):
        with S.session(seed=3, fast_commits=True) as h:
            for _ in range(3):
                h.op_import(kind="music", frames=FRAMES * 2)
            yield h

    def damage(self, h, sql, *params, foreign_keys=True):
        conn = sqlite3.connect(str(h.dir / "station.sqlite3"))
        try:
            conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def problems(self, h):
        return C.check_persisted(h.dir, library=h.world.library_view())

    def assertNamed(self, problems, text):
        self.assertTrue(any(text in p for p in problems), f"{text!r} not reported in {problems}")

    def test_a_healthy_directory_has_no_problems(self):
        with self.station() as h:
            self.assertEqual(self.problems(h), [])

    def test_row_limits(self):
        with self.station() as h:
            track = next(iter(h.world.tracks))
            conn = sqlite3.connect(str(h.dir / "station.sqlite3"))
            conn.executemany("INSERT INTO queue(track_id) VALUES (?)", [(track,)] * 1001)
            conn.executemany("INSERT INTO history(title,at,event) VALUES ('t','a','e')", [()] * 200)
            conn.executemany("INSERT INTO schedules VALUES (?,?,?,?,?)", [(f"s{i}", track, "x", float(i), "missed") for i in range(1001)])
            conn.commit()
            conn.close()
            found = self.problems(h)
        for text in ("queue holds 1001", "history holds", "finished or cancelled schedules kept"):
            self.assertNamed(found, text)

    def test_files_and_rows_must_match(self):
        with self.station() as h:
            first, second, _ = list(h.world.tracks)
            media = h.dir / "media"
            (media / ("0" * 32 + ".wav")).write_bytes(b"x")
            (media / "half-written.tmp").write_bytes(b"x")
            (media / (first + ".wav")).unlink()
            (media / (second + ".wav")).write_bytes(b"short")
            found = self.problems(h)
        for text in ("orphan media file", "unexpected file in media", f"media file missing for track {first}", f"media file for track {second} has the wrong shape"):
            self.assertNamed(found, text)

    def test_files_of_tracks_the_harness_broke_on_purpose_are_not_errors(self):
        with self.station() as h:
            first = next(iter(h.world.tracks))
            (h.dir / "media" / (first + ".wav")).unlink()
            self.assertEqual(C.check_persisted(h.dir, library=h.world.library_view(), damaged={first}), [])

    def test_schedule_program_and_marker_rules(self):
        with self.station() as h:
            track = next(iter(h.world.tracks))
            self.damage(h, "INSERT INTO schedules VALUES ('a',?,'x',1.0,'weird')", track)
            self.damage(h, "INSERT INTO programs VALUES ('p',?,'UTC','10:00','8',1.0,NULL,NULL)", track)
            self.damage(h, "UPDATE settings SET value='\"nope\"' WHERE key='playing'")
            found = self.problems(h)
        for text in ("unknown status", "malformed rule", "playing marker"):
            self.assertNamed(found, text)

    def test_a_row_that_points_at_nothing(self):
        with self.station() as h:
            self.damage(h, "INSERT INTO queue(track_id) VALUES ('nothing')", foreign_keys=False)
            found = self.problems(h)
        self.assertNamed(found, "foreign_key_check")
        self.assertNamed(found, "queue refers to unknown track")

    def test_what_was_imported_must_be_what_is_stored(self):
        with self.station() as h:
            first = next(iter(h.world.tracks))
            library = h.world.library_view()
            library[first] = ("another title",) + library[first][1:]
            self.assertNamed(C.check_persisted(h.dir, library=library), "differs from what was imported")
            library.pop(first)
            self.assertNamed(C.check_persisted(h.dir, library=library), "library ids differ")

    def test_a_setting_that_is_not_readable_data_is_reported_not_raised(self):
        with self.station() as h:
            self.damage(h, "UPDATE settings SET value='not json at all' WHERE key='tagline'")
            found = self.problems(h)
        self.assertNamed(found, "'tagline' is not stored as readable data")

    def test_a_track_whose_length_is_not_a_number_is_reported_not_raised(self):
        with self.station() as h:
            track = next(iter(h.world.tracks))
            self.damage(h, "UPDATE tracks SET duration='long' WHERE id=?", track)
            found = self.problems(h)
        self.assertNamed(found, f"track {track} has inconsistent fields")

    def test_a_missing_playing_marker_is_reported_not_raised(self):
        with self.station() as h:
            self.damage(h, "DELETE FROM settings WHERE key='playing'")
            found = self.problems(h)
            self.assertEqual(C.db_state(h.station.db)["playing"], C.MISSING)
            self.assertEqual(C.persisted_state(h.dir)["playing"], C.MISSING)
        self.assertNamed(found, "playing marker row is missing")

    def test_an_unreadable_database_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            station = Station(directory, start_worker=False)
            station.close()
            Path(directory, "station.sqlite3").write_bytes(b"this is not a database" * 100)
            self.assertNamed(C.check_persisted(directory), "cannot be read")
            self.assertEqual(C.check_persisted(os.path.join(directory, "nowhere")), ["station.sqlite3 is missing"])


class MutantTests(unittest.TestCase):
    """Engines made wrong on purpose.  The harness must reject each one, and by the right invariant."""

    def setUp(self):
        # Scoped: the filters this test changes are put back afterwards.  (warnings.resetwarnings would wipe the process-wide list for every
        # test that runs later, which turns "python -W error" into a no-op after the first mutant test.)
        guard = warnings.catch_warnings()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)  # a rejected run leaves the broken engine's files open

    @contextlib.contextmanager
    def running(self, **kwargs):
        with S.session(seed=4, fast_commits=True, **kwargs) as h:
            for _ in range(4):
                h.op_import(kind="music", frames=FRAMES * 3)
            h.op_import(kind="ad", frames=FRAMES * 2)
            h.op_start()
            h.play(12)
            yield h

    def rejected(self, expected, drive, mutant, **kwargs):
        """``mutant`` is a patch that is entered only after a healthy warm-up, so the failure is caused by the wrong engine."""
        names = (expected,) if isinstance(expected, str) else tuple(expected)
        with self.running(**kwargs) as h:
            with mutant, self.assertRaises(C.SoakFailure) as caught:
                drive(h)
        self.assertIn(caught.exception.invariant, names, str(caught.exception))

    def test_the_unmodified_engine_passes_the_same_drive(self):
        with self.running() as h:
            h.play(60)
            h.sweep("test")
            self.assertEqual(h.failures, [])

    def test_a_dropped_audio_frame(self):
        real = Station.tick
        self.rejected("audio-exact", lambda h: h.play(5), mock.patch.object(Station, "tick", lambda self, now=None: real(self, now)[4:] + bytes(4)))

    def test_audio_from_the_wrong_position(self):
        real = Station.tick

        def swapped(self, now=None):
            block = real(self, now)
            return block[:4] + block[8:12] + block[4:8] + block[12:] if block != SILENCE else block

        self.rejected("audio-exact", lambda h: h.play(5), mock.patch.object(Station, "tick", swapped))

    def test_history_that_is_never_pruned(self):
        def event(self, title, kind):
            self.db.execute("INSERT INTO history(title,at,event) VALUES (?,?,?)", (title, station_module.utcnow(), kind))

        self.rejected("history-limit", lambda h: h.play(400), mock.patch.object(Station, "_event", event))

    def test_a_queue_that_changes_order_behind_the_engine_s_back(self):
        real = Station.tick

        def reorder(self, now=None):
            block = real(self, now)
            rows = self.db.execute("SELECT id,track_id FROM queue ORDER BY id").fetchall()
            if len(rows) >= 2:
                self.db.execute("UPDATE queue SET track_id=? WHERE id=?", (rows[1][1], rows[0][0]))
                self.db.execute("UPDATE queue SET track_id=? WHERE id=?", (rows[0][1], rows[1][0]))
                self.db.commit()
            return block

        def drive(h):
            for track in list(h.world.tracks.values())[:3]:
                h.op_queue_add(track)
            h.play(40)

        self.rejected("queue-law", drive, mock.patch.object(Station, "tick", reorder))

    def test_a_recurrence_that_ignores_daylight_saving(self):
        def utc_days(zone, local_time, weekdays, after):
            return after + 86400

        def drive(h):
            h.op_program_add(zone="America/New_York", local_time="12:00", weekdays=DAILY, label="mutant")

        self.rejected("program_add-effect", drive, mock.patch.object(station_module, "next_occurrence", utc_days))

    def test_a_quarantine_that_is_never_recorded(self):
        def forget(self, track, message):
            self._event(track["title"], "media unavailable")

        def drive(h):
            track = h.op_import(kind="music", frames=FRAMES * 3)
            h.op_queue_add(track)
            h.damage(track, "delete")
            h.play(200)

        self.rejected(("quarantine-matches", "autofill-not-quarantined", "media-verdict"), drive, mock.patch.object(Station, "_quarantine", forget))

    def test_elapsed_time_past_the_end_of_the_track(self):
        real = Station.tick

        def overrun(self, now=None):
            block = real(self, now)
            self.elapsed += 30
            return block

        self.rejected("elapsed-bounds", lambda h: h.play(3), mock.patch.object(Station, "tick", overrun))

    def test_a_transaction_left_open(self):
        real = Station.tick

        def leaky(self, now=None):
            block = real(self, now)
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('leak','1')")
            return block

        self.rejected("no-open-transaction", lambda h: h.play(3), mock.patch.object(Station, "tick", leaky))

    def test_a_skip_that_forgets_to_clear_the_playing_marker(self):
        real = Station._finish

        def forgetful(self, event):
            real(self, event)
            if self.current is None and event == "skipped":
                self.db.execute("UPDATE settings SET value=? WHERE key='playing'", ('"stale"',))

        self.rejected("skip-effect", lambda h: h.op_skip(), mock.patch.object(Station, "_finish", forgetful))

    def test_events_treated_as_later_than_they_are(self):
        real = Station.tick

        def late(self, now=None):
            return real(self, None if now is None else now + 45)

        def drive(h):
            h.scn_stale_boundary()

        self.rejected(("stale-boundary", "schedule-admission", "nothing-overdue", "autofill-music-only", "autofill-allowed", "queue-order", "queue-law"), drive,
                      mock.patch.object(Station, "tick", late))

    def test_a_descriptor_that_is_never_closed(self):
        kept = []
        real = Station.import_audio

        def leaky(self, *args, **kwargs):
            kept.append(os.open(os.devnull, os.O_RDONLY))
            return real(self, *args, **kwargs)

        try:
            self.rejected("file-descriptors", lambda h: (h.op_import(kind="music"), h.op_import(kind="music")), mock.patch.object(Station, "import_audio", leaky))
        finally:
            for fd in kept:
                os.close(fd)

    def test_a_thread_that_is_never_stopped(self):
        release = threading.Event()
        real = Station.tick
        started = []

        def spawning(self, now=None):
            if not started:
                started.append(threading.Thread(target=release.wait, daemon=True))
                started[0].start()
            return real(self, now)

        try:
            self.rejected("threads", lambda h: (h.play(2), h.sweep("test")), mock.patch.object(Station, "tick", spawning))
        finally:
            release.set()
            for thread in started:
                thread.join(timeout=2)

    def test_a_status_that_cannot_be_serialised(self):
        real = Station.status

        def broken(self):
            result = real(self)
            result["elapsed"] = float("nan")
            return result

        self.rejected("status-json", lambda h: h.sweep("test"), mock.patch.object(Station, "status", broken))

    def test_a_restart_that_forgets_the_interrupted_track(self):
        real = Station.__init__

        def forgetful(self, *args, **kwargs):
            real(self, *args, **kwargs)
            self.db.execute("DELETE FROM queue WHERE id=(SELECT MIN(id) FROM queue)")
            self.db.commit()

        def drive(h):
            h.play(5)
            h.restart()

        self.rejected("restart-queue", drive, mock.patch.object(Station, "__init__", forgetful))

    def test_a_playback_failure_that_nothing_caused(self):
        import myradio.media as media

        def flaky(self, count):
            raise OSError("flaky disk")

        self.rejected("playback-failure-cause", lambda h: h.play(5), mock.patch.object(media.Reader, "readframes", flaky))

    def test_an_engine_that_forgets_the_playing_marker_is_named_not_crashed(self):
        real = Station.__init__

        def forgetful(self, *args, **kwargs):
            real(self, *args, **kwargs)
            self.db.execute("DELETE FROM settings WHERE key='playing'")
            self.db.commit()

        self.rejected(("restart-marker-cleared", "persisted-state", "playing-marker"), lambda h: h.restart(), mock.patch.object(Station, "__init__", forgetful))

    def test_a_whole_run_against_such_an_engine_reports_a_failure_instead_of_raising(self):
        real = Station.__init__

        def forgetful(self, *args, **kwargs):
            real(self, *args, **kwargs)
            self.db.execute("DELETE FROM settings WHERE key='playing'")
            self.db.commit()

        with mock.patch.object(Station, "__init__", forgetful):
            result = S.run_accelerated(5, 600, scenarios="none", fast_commits=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.failures)
        self.assertIn(result.failures[0]["invariant"], ("persisted-state", "playing-marker", "restart-marker-cleared", "unexpected-state"))


class DirectedScenarioMutants(unittest.TestCase):
    """Engines that are wrong in exactly one place, found by mutation testing, that the first version of the short run did not notice.  Each is a
    copy of the real function with one line changed (see soak_core.source_mutant); the directed scenario for that rule must reject it by name,
    and the unmodified engine must pass the same scenario."""

    def setUp(self):
        guard = warnings.catch_warnings()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)

    @contextlib.contextmanager
    def library(self):
        with S.session(seed=4, fast_commits=True) as h:
            for _ in range(4):
                h.op_import(kind="music", frames=FRAMES * 3)
            h.op_import(kind="speech", frames=FRAMES * 2)
            h.op_import(kind="ad", frames=FRAMES * 2)
            h.op_start()
            h.play(12)
            yield h

    def rejected(self, expected, scenario, mutant):
        names = (expected,) if isinstance(expected, str) else tuple(expected)
        with self.library() as h:
            getattr(h, scenario)()  # the control: the unmodified engine passes the scenario
            self.assertEqual(h.failures, [])
        with self.library() as h:
            with mutant(), self.assertRaises(C.SoakFailure) as caught:
                getattr(h, scenario)()
        self.assertIn(caught.exception.invariant, names, str(caught.exception))

    def test_a_playback_failure_that_is_not_quarantined(self):
        def mutant():
            return mock.patch.object(Station, "_tick", C.source_mutant(
                Station._tick, 'self._quarantine(self.current, "Audio could not be read during playback. Repair the original file or reimport it, then retry.")', "pass", station_module))

        self.rejected("midplay-failure", "scn_midplay_truncation", mutant)

    def test_a_program_that_is_queued_while_the_station_is_stopped(self):
        def mutant():
            return mock.patch.object(Station, "_tick", C.source_mutant(
                Station._tick, 'if self.running and now - event["due"] <= 60 and slots:', 'if (self.running or "program" in event) and now - event["due"] <= 60 and slots:', station_module))

        self.rejected(("program-admission", "program-off-air"), "scn_program_off_air", mutant)

    def test_a_missed_program_that_catches_up_instead_of_moving_on(self):
        def mutant():
            return mock.patch.object(Station, "_tick", C.source_mutant(
                Station._tick, 'upcoming = next_occurrence(p["timezone"], p["local_time"], p["weekdays"], now)',
                'upcoming = next_occurrence(p["timezone"], p["local_time"], p["weekdays"], event["due"])', station_module))

        self.rejected("program-next-due", "scn_program_off_air", mutant)

    def test_a_length_check_that_was_loosened_to_a_second(self):
        import myradio.media as media_module

        def mutant():
            return mock.patch.object(station_module, "open_media", C.source_mutant(
                media_module.open_media, "abs(frames / 44100 - duration) > 1 / 44100", "abs(frames / 44100 - duration) > 1.0", media_module))

        self.rejected("media-verdict", "scn_quarantine_modes", mutant)

    def test_a_length_check_that_was_loosened_to_a_hundredth_of_a_second(self):
        import myradio.media as media_module

        def mutant():
            return mock.patch.object(station_module, "open_media", C.source_mutant(
                media_module.open_media, "abs(frames / 44100 - duration) > 1 / 44100", "abs(frames / 44100 - duration) > 0.011", media_module))

        self.rejected("media-verdict", "scn_quarantine_modes", mutant)

    def test_a_queue_that_accepts_one_entry_too_many(self):
        def mutant():
            return mock.patch.object(Station, "action", C.source_mutant(
                Station.action, 'self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0] >= 1000', 'self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0] >= 1001',
                station_module))

        self.rejected(("rejection-expected", "queue-fills-to-limit"), "scn_capacity", mutant)

    def test_admission_that_has_one_slot_too_many(self):
        def mutant():
            return mock.patch.object(Station, "_tick", C.source_mutant(
                Station._tick, "slots = max(0, 1000 - self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]) if due else 0",
                "slots = max(0, 1001 - self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]) if due else 0", station_module))

        self.rejected(("schedule-admission", "queue-law"), "scn_capacity", mutant)

    def test_a_delete_that_ignores_a_pending_schedule(self):
        def mutant():
            return mock.patch.object(Station, "action", C.source_mutant(
                Station.action, "or self.db.execute(\"SELECT 1 FROM schedules WHERE track_id=? AND status='pending'\", (track[\"id\"],)).fetchone() ", "", station_module))

        self.rejected("rejection-expected", "scn_delete_guards", mutant)

    def test_a_delete_that_ignores_a_recurring_program(self):
        def mutant():
            return mock.patch.object(Station, "action", C.source_mutant(
                Station.action, "or self.db.execute(\"SELECT 1 FROM programs WHERE track_id=?\", (track[\"id\"],)).fetchone():", ":", station_module))

        # With the guard gone the database's own foreign key rule refuses the delete, which latches the station: the refusal changed something.
        self.rejected(("rejection-expected", "rejection-changes-nothing"), "scn_delete_guards", mutant)

    def test_the_midplay_scenario_still_cuts_the_file_on_a_file_system_with_huge_blocks(self):
        """A file that is open is read through a buffer as big as the file system's block size, so on a file system with megabyte blocks the track
        must be longer for the cut to show.  The scenario sizes its track from the block size."""
        with mock.patch.object(S, "file_block_size", lambda path: 1 << 20), self.library() as h:
            h.scn_midplay_truncation()
            self.assertEqual(h.failures, [])
            self.assertEqual(h.metrics.counters["midplay_not_possible"], 0)
            self.assertGreater(h.metrics.checks["midplay-failure"], 0)

    def test_a_cut_that_cannot_be_made_is_reported_as_such_not_as_an_engine_fault(self):
        with mock.patch.object(S, "file_block_size", lambda path: 1 << 20), self.library() as h:
            real_import = h.op_import
            h.op_import = lambda kind=None, frames=None, channels=None: real_import(kind=kind, frames=FRAMES * 40, channels=channels)  # a track too short for the block size
            h.scn_midplay_truncation()
            self.assertEqual(h.failures, [])
            self.assertEqual(h.metrics.counters["midplay_not_possible"], 1)
            self.assertEqual(h.metrics.checks["midplay-failure"], 0)


class InstrumentTests(unittest.TestCase):
    def test_descriptor_counter_moves_by_exactly_one_per_open_file(self):
        before = C.fd_count()
        handle = open(os.devnull)
        try:
            self.assertEqual(C.fd_count(), before + 1)
        finally:
            handle.close()
        self.assertEqual(C.fd_count(), before)

    def test_resident_memory_is_reported(self):
        self.assertGreater(C.rss_bytes(), 1_000_000)
        self.assertGreaterEqual(C.peak_rss_bytes(), 1_000_000)

    def test_histogram_percentiles(self):
        h = C.Histogram()
        for _ in range(1000):
            h.add(0.001)
        for _ in range(10):
            h.add(0.1)
        self.assertAlmostEqual(h.percentile(0.5), 0.001, delta=0.0002)
        self.assertAlmostEqual(h.percentile(0.9995), 0.1, delta=0.02)
        self.assertEqual(h.maximum, 0.1)
        self.assertEqual(h.total, 1010)

    def test_memory_growth_is_measured_after_warm_up_only(self):
        with tempfile.TemporaryDirectory() as directory:
            h = S.Harness(directory)
            self.assertIsNone(h.rss_growth_after_warmup())
            h.metrics.rss_samples = [(i, size) for i, size in enumerate([10, 50, 50, 50, 52, 53, 51, 54])]
            self.assertEqual(h.rss_growth_after_warmup(), 4)  # warm-up ends at the third sample (50): later peak 54
            h.metrics.rss_samples = [(i, 50 + i * 3) for i in range(8)]
            self.assertEqual(h.rss_growth_after_warmup(), 15)  # a steady climb is growth


if __name__ == "__main__":
    unittest.main()
