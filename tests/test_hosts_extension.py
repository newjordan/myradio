"""The hosts extension inside the real extension seam: operations, routes, status, persistence and shutdown."""
import http.client
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hosts_support import BRIEF, FULL, FakeStation, HostsCase, research_brief, research_item  # noqa: E402

from myradio.extensions import ExtensionError, Request, builtin_extensions  # noqa: E402
from myradio.hosts import audit_speech_text  # noqa: E402
from myradio.hosts.extension import HostsExtension, build  # noqa: E402
from myradio.server import Server  # noqa: E402
from myradio.station import Station, StationError  # noqa: E402

TOKEN = "t" * 40


def route(case, method, path, body=None, query=None):
    match = case.host.find_route(method, "/api/x/hosts" + path)
    case.assertIsNotNone(match, path)
    return case.host.dispatch_route(match, Request(method, path, query or {}, body, json.dumps(body).encode() if body else b""))


class SeamTests(HostsCase):
    def test_the_extension_loads_by_package_name_and_runs(self):
        found = [e for e in builtin_extensions() if e.name == "hosts"]
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], HostsExtension)
        status = self.host.status()["hosts"]
        self.assertEqual((status["state"], status["error"]), ("running", None))

    def test_the_operations_are_typed_described_and_in_the_catalog(self):
        catalog = {c["name"]: c for c in self.host.catalog() if c["name"].startswith("hosts.")}
        self.assertEqual(set(catalog), {"hosts.save", "hosts.remove", "hosts.restore", "hosts.list", "hosts.compose",
                                        "hosts.compose_update"})
        for entry in catalog.values():
            self.assertTrue(10 < len(entry["description"]) <= 400)
        self.assertEqual(catalog["hosts.save"]["required"], ["persona"])
        self.assertEqual(catalog["hosts.save"]["arguments"]["persona"]["type"], "object")
        self.assertEqual(catalog["hosts.remove"]["required"], ["id"])
        self.assertEqual(catalog["hosts.compose"]["required"], ["persona"])
        self.assertIn("song_intro", catalog["hosts.compose"]["arguments"]["segment"]["enum"])
        self.assertIn("word-salad-poem", catalog["hosts.compose"]["arguments"]["format"]["enum"])
        self.assertEqual(catalog["hosts.compose"]["arguments"]["seed"]["minimum"], 0)
        self.assertEqual(sorted(catalog["hosts.compose_update"]["required"]), ["brief", "persona"])

    def test_the_host_validates_arguments_before_any_handler_runs(self):
        bad = [("hosts.save", {}), ("hosts.save", {"persona": "Nova"}), ("hosts.save", {"persona": {}, "extra": 1}),
               ("hosts.remove", {}), ("hosts.remove", {"id": ""}), ("hosts.remove", {"id": 5}), ("hosts.remove", {"id": "x" * 64}),
               ("hosts.compose", {}), ("hosts.compose", {"persona": "nova", "segment": "poem"}),
               ("hosts.compose", {"persona": "nova", "segment": "song_intro", "seed": -1}),
               ("hosts.compose", {"persona": "nova", "segment": "song_intro", "seed": True}),
               ("hosts.compose", {"persona": "nova", "segment": "song_intro", "target_seconds": 0}),
               ("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": "text"}),
               ("hosts.compose", {"persona": "nova", "segment": "song_intro", "dry_run": "yes"}),
               ("hosts.compose", {"persona": "nova", "format": "poem"}),
               ("hosts.compose_update", {"persona": "nova"}), ("hosts.compose_update", {"persona": "nova", "brief": []}),
               ("hosts.list", {"include_hidden": 1}), ("hosts.list", {"x": 1})]
        for name, args in bad:
            with self.subTest(op=name, args=str(args)[:40]), self.assertRaises(ExtensionError):
                self.host.call(name, args)

    def test_save_list_compose_remove_restore_round_trip(self):
        saved = self.host.call("hosts.save", {"persona": {"name": "Rhea", "role": "Afternoon host", "blurb": "Easygoing.",
                                                           "voice": "warm", "speaker": 6, "allowed_tags": ["laughs"]}})
        self.assertEqual((saved["ok"], saved["operation"], saved["created"], saved["persona"]["id"]), (True, "hosts.save", True, "rhea"))
        listed = self.host.call("hosts.list", {})
        self.assertEqual(listed["count"], 17)
        self.assertEqual({h["id"] for h in listed["hosts"]} >= {"nova", "rhea"}, True)
        self.assertEqual(set(listed["hosts"][0]), {"id", "name", "role", "blurb", "voice", "speaker", "builtin", "hidden"})
        composed = self.host.call("hosts.compose", {"persona": "rhea", "segment": "song_intro", "facts": FULL, "seed": 4})
        self.assertEqual((composed["ok"], composed["speaker"], composed["role"], composed["persona"]), (True, 6, "host_break", "rhea"))
        self.assertTrue(composed["text"].startswith("<|speaker:6|>"))
        hidden = self.host.call("hosts.remove", {"id": "nova"})
        self.assertEqual((hidden["removed"], hidden["hidden"]), (False, True))
        self.assertNotIn("nova", [h["id"] for h in self.host.call("hosts.list", {})["hosts"]])
        self.assertIn("nova", [h["id"] for h in self.host.call("hosts.list", {"include_hidden": True})["hosts"]])
        self.assertTrue(self.host.call("hosts.restore", {"id": "nova"})["restored"])
        self.assertEqual(self.host.call("hosts.remove", {"id": "rhea"})["removed"], True)
        for name, args in (("hosts.remove", {"id": "rhea"}), ("hosts.restore", {"id": "rhea"}), ("hosts.compose", {"persona": "rhea", "segment": "filler"})):
            with self.subTest(op=name), self.assertRaises(ExtensionError):
                self.host.call(name, args)

    def test_errors_are_plain_language_and_never_internal(self):
        for name, args in (("hosts.compose", {"persona": "nobody", "segment": "filler"}),
                           ("hosts.compose", {"persona": "nova", "segment": "station_id", "facts": {}}),
                           ("hosts.save", {"persona": {"name": ""}}),
                           ("hosts.compose_update", {"persona": "nova", "brief": {"topic": "t", "items": []}})):
            with self.assertRaises(ExtensionError) as caught:
                self.host.call(name, args)
            message = str(caught.exception)
            self.assertNotRegex(message, r"Traceback|sqlite|\.py\b|Error\b")
            self.assertTrue(message[0].isupper() and message.endswith("."), message)

    def test_a_host_with_a_value_of_the_wrong_kind_is_refused_in_plain_words_not_with_the_generic_failure(self):
        for field, value, expected in (("pace", ["slow"], "Pace must be slow, steady or brisk."), ("pace", {"a": 1}, "Pace must be slow"),
                                       ("allowed_tags", 5, "Choose sound effects from"), ("allowed_tags", True, "Choose sound effects from"),
                                       ("speaker", [3], "voice slot"), ("verbosity", "3", "Talkativeness")):
            persona = {"name": "Rhea", "role": "Host", "blurb": "Easygoing.", "voice": "warm", field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ExtensionError) as caught:
                self.host.call("hosts.save", {"persona": persona})
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("failed", str(caught.exception))
        self.assertEqual(self.host.call("hosts.list", {})["count"], 16)       # nothing was saved by any of them

    def test_compose_update_returns_citations(self):
        result = self.host.call("hosts.compose_update", {"persona": "marlowe", "brief": BRIEF, "facts": {"station": "Porchlight FM"},
                                                         "seed": 2, "target_seconds": 60})
        self.assertEqual((result["role"], result["segment"]), ("update", "update"))
        self.assertEqual([c["item_id"] for c in result["citations"]], ["i1", "i2", "i3"])
        self.assertEqual(result["citations"][0]["url"], "https://example.org/meteor")

    def test_the_research_lanes_own_brief_goes_through_the_operation_and_the_preview_route(self):
        brief = research_brief([research_item(1, "Bright meteor", "A bright meteor was seen on Tuesday night. Observers were pleased.", "Example Astronomy Club"),
                                research_item(2, "Saturn watch", "", "Example Sky Notes", kind="unverified")])
        result = self.host.call("hosts.compose_update", {"persona": "marlowe", "brief": brief, "facts": {"station": "Porchlight FM"}, "seed": 2,
                                                         "target_seconds": 60})
        self.assertEqual([(c["source"], c["kind"]) for c in result["citations"]],
                         [("Example Astronomy Club", "reported"), ("Example Sky Notes", "uncertain")])
        self.assertIn("Sky watcher", result["plain_text"])
        previewed = route(self, "POST", "/preview", {"persona": "marlowe", "segment": "update", "brief": brief, "seed": 2, "target_seconds": 60,
                                                      "facts": {"station": "Porchlight FM"}})
        self.assertEqual(previewed.body["citations"], result["citations"])
        self.assertEqual((previewed.body["audio"], previewed.body["preview"]), (False, True))

    def test_a_dry_run_leaves_no_trace(self):
        before = self.host.status()["hosts"]["status"]["composed"]["total"]
        for _ in range(3):
            self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 1, "dry_run": True})
        self.assertEqual(self.host.status()["hosts"]["status"]["composed"]["total"], before)
        self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 1})
        self.assertEqual(self.host.status()["hosts"]["status"]["composed"]["total"], before + 1)

    def test_scripts_asked_for_at_the_same_time_stay_valid(self):
        results, errors = [], []

        def worker(n):
            try:
                for i in range(10):
                    results.append(self.host.call("hosts.compose", {"persona": "nova", "segment": "filler", "facts": FULL, "seed": n * 100 + i}))
            except Exception as exc:     # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual((errors, len(results)), ([], 60))
        self.assertTrue(all(audit_speech_text(r["text"], 1, ["whispers"]) == [] for r in results))


class StatusAndChecksTests(HostsCase):
    def test_status_is_small_json_and_complete(self):
        status = self.host.status()["hosts"]["status"]
        json.dumps(status)
        self.assertLess(len(json.dumps(status)), 2000)
        self.assertEqual((status["ready"], status["segments"], status["formats"], status["voices"], status["writer"]),
                         (True, 10, 3, 8, "templates"))
        self.assertEqual(status["personas"], {"builtin": 16, "custom": 0, "hidden": 0, "visible": 16})
        self.assertEqual(status["templates"], {"total": 960, "fewest_per_kind": 12, "required": 12})
        self.assertEqual(status["composed"]["total"], 0)
        self.assertEqual((status["unreadable_saved_hosts"], status["storage_problems"]), (0, 0))

    def test_status_never_asks_the_station_and_never_waits_for_a_lock(self):
        service = self.ext.service
        for _ in range(20):
            self.host.status()
        self.assertEqual(self.station.status_calls, 0)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with service.lock:
                held.set()
                release.wait(5)

        blocker = threading.Thread(target=hold, daemon=True)
        blocker.start()
        self.assertTrue(held.wait(2))
        answer = []
        asker = threading.Thread(target=lambda: answer.append(self.host.status()), daemon=True)
        started = time.monotonic()
        asker.start()
        asker.join(1.0)
        release.set()
        blocker.join(2)
        self.assertFalse(asker.is_alive(), "status() waited for a lock")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(answer[0]["hosts"]["state"], "running")

    def test_counts_follow_changes_immediately(self):
        self.host.call("hosts.save", {"persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "warm"}})
        self.host.call("hosts.remove", {"id": "dex"})
        counts = self.host.status()["hosts"]["status"]["personas"]
        self.assertEqual(counts, {"builtin": 16, "custom": 1, "hidden": 1, "visible": 16})

    def test_the_doctor_checks_are_plain_and_ok_on_a_fresh_install(self):
        checks = {c["id"]: c for c in self.host.checks() if c["id"].startswith("hosts.")}
        self.assertEqual(set(checks), {"hosts.cast", "hosts.writing", "hosts.storage"})
        for check in checks.values():
            self.assertEqual(check["status"], "ok")
            self.assertNotRegex(check["detail"] + check["title"], r"PCM|GGUF|SSRF|stack|Traceback|sqlite")
        self.assertIn("16 built-in hosts", checks["hosts.cast"]["detail"])

    def test_the_checks_warn_when_saved_hosts_cannot_be_read(self):
        self.host.call("hosts.save", {"persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "warm"}})
        conn = self.ext._conn
        conn.execute("INSERT INTO personas VALUES ('broken', '{nope', 1, 1)")
        conn.commit()
        self.reboot()
        checks = {c["id"]: c for c in self.host.checks()}
        self.assertEqual(checks["hosts.storage"]["status"], "warn")
        self.assertIn("1 saved host", checks["hosts.storage"]["detail"])
        self.assertIn("Check free disk space", checks["hosts.storage"]["hint"])


class PersistenceAndShutdownTests(HostsCase):
    def test_hosts_and_repetition_memory_survive_a_restart(self):
        self.host.call("hosts.save", {"persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "bright", "speaker": 9}})
        self.host.call("hosts.remove", {"id": "wren"})
        first = self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 1, "target_seconds": 10})
        self.reboot()
        listed = {h["id"] for h in self.host.call("hosts.list", {})["hosts"]}
        self.assertIn("rhea", listed)
        self.assertNotIn("wren", listed)
        again = self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 1, "target_seconds": 10})
        self.assertNotEqual(first["template"], again["template"])        # the memory carried over
        self.assertEqual(self.host.status()["hosts"]["status"]["composed"]["total"], 1)   # counters restart

    def test_the_database_is_private_and_listed_for_backups(self):
        import sqlite3
        self.host.call("hosts.save", {"persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "warm"}})
        files = self.host.backup_files()["hosts"]
        self.assertEqual([p.name for p in files], ["hosts.sqlite3"])
        self.assertEqual(stat.S_IMODE(os.stat(files[0]).st_mode), 0o600)
        # saving checkpoints the write-ahead log, so a backup that copies only the main file still has the host
        copy = sqlite3.connect(f"file:{files[0]}?mode=ro", uri=True)
        try:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM personas").fetchone()[0], 1)
        finally:
            copy.close()

    def test_a_copy_of_only_the_main_database_file_holds_everything_that_was_saved(self):
        # A backup that copies hosts.sqlite3 alone (without its write-ahead log) must still have the hosts: reading the
        # file in place would find the log beside it and hide the problem, so the copy is made somewhere else.
        self.host.call("hosts.save", {"persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "warm"}})
        self.host.call("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 1})
        self.host.call("hosts.remove", {"id": "wren"})
        files = self.host.backup_files()["hosts"]
        where = Path(self.temp.name) / "backup-copy"
        where.mkdir()
        shutil.copy(files[0], where / "hosts.sqlite3")
        copy = sqlite3.connect(where / "hosts.sqlite3")
        try:
            self.assertEqual([r[0] for r in copy.execute("SELECT id FROM personas")], ["rhea"])
            self.assertEqual([r[0] for r in copy.execute("SELECT id FROM hidden")], ["wren"])
        finally:
            copy.close()

    def test_the_config_file_is_listed_once_it_exists(self):
        base = Path(self.temp.name) / "ext" / "hosts"
        self.assertEqual([p.name for p in self.host.backup_files()["hosts"] if p.name == "config.json"], [])
        (base / "config.json").write_text('{"timezone": "UTC"}')
        self.assertIn("config.json", [p.name for p in self.host.backup_files()["hosts"]])

    def test_stopping_closes_the_database_and_leaves_no_threads(self):
        self.host.emit({"type": "track_started", "track": {"kind": "music", "title": "A", "artist": "B"}})
        conn = self.ext._conn
        self.host.stop()
        self.assertIsNone(self.ext._conn)
        with self.assertRaises(Exception):
            conn.execute("SELECT 1")
        self.assertEqual(self.threads(), [])
        self.host.stop()                          # stopping twice is harmless
        self.assertEqual(self.host.status()["hosts"]["state"], "stopped")
        with self.assertRaises(ExtensionError):
            self.host.call("hosts.list", {})

    def test_a_blocked_database_name_fails_the_extension_not_the_station(self):
        self.host.stop()
        base = Path(self.temp.name) / "ext" / "hosts"
        (base / "hosts.sqlite3").unlink()
        for leftover in base.glob("hosts.sqlite3-*"):
            leftover.unlink()
        (base / "elsewhere.sqlite3").write_bytes(b"")
        os.symlink(base / "elsewhere.sqlite3", base / "hosts.sqlite3")
        self.boot()
        status = self.host.status()["hosts"]
        self.assertEqual(status["state"], "failed")
        self.assertIn("symbolic link", status["error"])
        self.assertEqual(self.host.catalog(), [])


    def test_a_damaged_database_fails_the_extension_in_plain_words(self):
        self.host.stop()
        base = Path(self.temp.name) / "ext" / "hosts"
        for leftover in base.glob("hosts.sqlite3-*"):
            leftover.unlink()
        (base / "hosts.sqlite3").write_bytes(b"this is not a database" * 100)
        self.boot()
        status = self.host.status()["hosts"]
        self.assertEqual(status["state"], "failed")
        self.assertIn("damaged", status["error"])
        self.assertNotIn("not a database", status["error"].lower())      # the library's own wording is not shown
        self.assertNotIn("databaseerror", status["error"].lower())
        self.assertEqual(self.host.catalog(), [])
        self.assertEqual([c["status"] for c in self.host.checks() if c["id"] == "hosts.start"], ["fail"])


class ConfigTests(HostsCase):
    def write_config(self, text):
        (Path(self.temp.name) / "ext" / "hosts" / "config.json").write_text(text)
        self.reboot()

    def test_a_bad_config_never_stops_hosts_and_is_reported(self):
        for text in ("{nope", "[1]", '{"event_tags": 5}', '{"event_tags": {"shout": "x"}}', '{"event_tags": {"laughs": "Bad Name!"}}',
                     '{"timezone": 5}'):
            with self.subTest(config=text):
                self.write_config(text)
                self.assertEqual(self.host.status()["hosts"]["state"], "running")
                warnings = [c for c in self.host.checks() if c["id"] == "hosts.config"]
                self.assertTrue(warnings and all(w["status"] == "warn" for w in warnings))
                self.assertTrue(self.host.call("hosts.compose", {"persona": "nova", "segment": "filler", "facts": FULL})["text"])

    def test_every_problem_in_the_config_is_its_own_check_with_its_own_id(self):
        # the doctor keeps only the first check it sees with a given id, so problems that share one would be lost
        self.write_config('{"timezone": 5, "event_tags": {"laughs": "Not Valid!", "bogus": "x", "excited": 7}}')
        checks = [c for c in self.host.checks() if c["id"].startswith("hosts.config")]
        ids = [c["id"] for c in checks]
        self.assertEqual(ids, ["hosts.config", "hosts.config-2", "hosts.config-3", "hosts.config-4"])
        self.assertEqual(len({c["detail"] for c in checks}), 4)
        for check in checks:
            self.assertEqual(check["status"], "warn")
            self.assertIn("config.json", check["hint"])
        self.write_config('{"timezone": "UTC"}')
        self.assertEqual([c for c in self.host.checks() if c["id"].startswith("hosts.config")], [])

    def test_a_config_full_of_problems_cannot_flood_the_doctor(self):
        junk = ", ".join(f'"junk{n}": "x"' for n in range(60))
        self.write_config('{"event_tags": {%s}}' % junk)
        checks = [c for c in self.host.checks() if c["id"].startswith("hosts.config")]
        self.assertEqual(len(checks), 8)
        self.assertEqual(len({c["id"] for c in checks}), 8)
        self.assertIn("more settings", checks[-1]["detail"])
        self.assertEqual(self.host.status()["hosts"]["state"], "running")

    def test_effect_names_can_be_renamed_or_switched_off(self):
        self.write_config('{"event_tags": {"excited": "very excited", "laughs": null}}')
        texts = " ".join(self.host.call("hosts.compose", {"persona": "dex", "segment": seg, "facts": FULL, "seed": s, "dry_run": True})["text"]
                         for s in range(60) for seg in ("song_intro", "song_back_announce", "filler"))
        self.assertIn("[very excited]", texts)
        self.assertNotIn("[laughs]", texts)
        self.assertNotIn("[excited]", texts)
        self.assertEqual([c for c in self.host.checks() if c["id"] == "hosts.config"], [])

    def test_the_timezone_is_used_for_live_times(self):
        self.write_config('{"timezone": "Pacific/Kiritimati"}')
        self.assertIsNotNone(self.ext.service.tz)
        facts = self.ext.service.live_facts(FakeStation().status())
        self.assertRegex(facts["local_time"], r"^\d\d:\d\d$")


class EventAndRouteTests(HostsCase):
    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_recent_music_comes_from_station_events(self):
        for event in ({"type": "track_started", "track": {"kind": "music", "title": "First", "artist": "A"}},
                      {"type": "track_started", "track": {"kind": "speech", "title": "Break", "artist": ""}},
                      {"type": "track_started", "track": {"kind": "music", "title": "Second", "artist": "B"}},
                      {"type": "transport", "state": "started"}, {"type": "track_started", "track": "bogus"}, {"type": "track_finished"}):
            self.host.emit(event)
        self.assertTrue(self.wait_for(lambda: len(self.ext.service._recent) == 2))
        self.assertEqual([r["title"] for r in self.ext.service._recent], ["First", "Second"])
        speech_status = {"current": {"title": "Break", "kind": "speech"}, "queue": []}
        facts = self.ext.service.live_facts(speech_status)
        self.assertEqual((facts["now_title"], facts["now_artist"]), ("Second", "B"))     # what just played, from events

    def test_the_personas_route_describes_everything_a_screen_needs(self):
        reply = route(self, "GET", "/personas")
        self.assertEqual(reply.status, 200)
        body = reply.body
        json.dumps(body)
        self.assertEqual(len(body["personas"]), 16)
        self.assertEqual(body["event_tags"], ["laughs", "whispers", "excited"])
        self.assertEqual(body["paces"], {"slow": 2.1, "steady": 2.5, "brisk": 2.9})
        self.assertEqual(body["limits"]["custom_hosts"], 60)
        self.assertEqual(body["lexicon_keys"], ["audience", "sign_off", "vibe", "place", "aside"])
        self.assertTrue(all(p["fictional"] and p["builtin"] for p in body["personas"]))
        self.host.call("hosts.remove", {"id": "nova"})
        self.assertEqual(len(route(self, "GET", "/personas").body["personas"]), 15)
        shown = route(self, "GET", "/personas", query={"hidden": ["1"]}).body["personas"]
        self.assertEqual(len(shown), 16)
        self.assertTrue(next(p for p in shown if p["id"] == "nova")["hidden"])

    def test_the_formats_route_lists_every_notice(self):
        body = route(self, "GET", "/formats").body
        self.assertEqual(len(body["formats"]), 3)
        self.assertTrue(all(f["fictional"] and f["disclosure"] for f in body["formats"]))

    def test_a_preview_returns_text_only_and_uses_nothing_up(self):
        before = self.host.status()["hosts"]["status"]["composed"]["total"]
        memory_before = self.ext.service.memory.size()
        reply = route(self, "POST", "/preview", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 5, "target_seconds": 10})
        self.assertEqual(reply.status, 200)
        body = reply.body
        self.assertEqual((body["preview"], body["audio"], body["note"]), (True, False, "Text only. No audio was made."))
        self.assertEqual((body["speaker"], body["role"], body["seed"]), (1, "host_break", 5))
        self.assertIn("Paper Lantern Weather", body["plain_text"])
        again = route(self, "POST", "/preview", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 5, "target_seconds": 10})
        self.assertEqual(again.body["text"], body["text"])
        self.assertEqual(self.host.status()["hosts"]["status"]["composed"]["total"], before)
        self.assertEqual(self.ext.service.memory.size(), memory_before)
        self.assertEqual(self.station.status_calls, 0)

    def test_previews_cover_segments_formats_and_updates(self):
        poem = route(self, "POST", "/preview", {"persona": "professor", "format": "word-salad-poem", "seed": 2}).body
        self.assertTrue(poem["fictional"])
        self.assertTrue(poem["plain_text"].startswith("This next bit is a made-up poem"))
        update = route(self, "POST", "/preview", {"persona": "marlowe", "segment": "update", "brief": BRIEF, "facts": {"station": "S"},
                                                  "target_seconds": 60, "seed": 1}).body
        self.assertEqual((update["role"], len(update["citations"]), update["audio"]), ("update", 3, False))
        sponsor = route(self, "POST", "/preview", {"persona": "dex", "segment": "spot_wrapper", "facts": FULL, "seed": 1}).body
        self.assertRegex(sponsor["plain_text"], r"(?i)sponsor")
        with_ai = route(self, "POST", "/preview", {"persona": "nova", "segment": "filler", "facts": FULL, "use_ai": True}).body
        self.assertTrue(any("No writing assistant" in w for w in with_ai["warnings"]))

    def test_a_live_preview_uses_what_the_station_is_playing(self):
        body = route(self, "POST", "/preview", {"persona": "dex", "segment": "song_intro", "live": True, "seed": 3}).body
        self.assertIn("Paper Lantern Weather", body["plain_text"])           # the next music item, skipping the spoken break
        self.assertEqual(self.station.status_calls, 1)
        override = route(self, "POST", "/preview", {"persona": "dex", "segment": "song_intro", "live": True, "seed": 3,
                                                    "facts": {"next_title": "Chosen By Hand"}}).body
        self.assertIn("Chosen By Hand", override["plain_text"])
        self.assertNotIn("Paper Lantern Weather", override["plain_text"])

    def test_a_live_preview_survives_an_unreadable_station(self):
        self.station.status = lambda: (_ for _ in ()).throw(RuntimeError("secret internals"))
        body = route(self, "POST", "/preview", {"persona": "dex", "segment": "filler", "live": True, "facts": {"station": "S"}}).body
        self.assertTrue(any("could not be read" in w for w in body["warnings"]))
        self.assertNotIn("secret", json.dumps(body))

    def test_bad_previews_are_plain_400s_not_crashes(self):
        for body in (None, {}, [], {"persona": "nova"}, {"persona": "nova", "segment": "nope"}, {"persona": "nobody", "segment": "filler"},
                     {"persona": "nova", "segment": "filler", "extra": 1}, {"persona": "nova", "segment": "filler", "live": "yes"},
                     {"persona": "nova", "segment": "update"}, {"persona": "nova", "segment": "filler", "format": "word-salad-poem"},
                     {"persona": "nova", "segment": "filler", "target_seconds": "ten"}, {"persona": "nova", "segment": "filler", "seed": -3},
                     {"persona": "nova", "segment": "filler", "facts": "text"}, {"persona": ["nova"], "segment": "filler"}):
            with self.subTest(body=str(body)[:50]):
                reply = route(self, "POST", "/preview", body)
                self.assertEqual(reply.status, 400)
                self.assertTrue(reply.body["error"].endswith("."))
                self.assertNotRegex(reply.body["error"], r"Traceback|\.py|sqlite")

    def test_unknown_routes_and_methods_are_not_found(self):
        for method, path in (("POST", "/api/x/hosts/personas"), ("GET", "/api/x/hosts/preview"), ("GET", "/api/x/hosts/nothing"),
                             ("GET", "/api/x/hosts/../personas"), ("DELETE", "/api/x/hosts/personas")):
            self.assertIsNone(self.host.find_route(method, path))


class StationAndHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.station = Station(Path(self.temp.name) / "data", start_worker=False, extensions=[build()])
        self.addCleanup(self.station.close)
        with patch.dict("os.environ", {}, clear=True):
            self.server = Server(("127.0.0.1", 0), self.station, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, headers=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        send = dict(headers or {})
        if token:
            send["X-Myradio-Token"] = TOKEN
        if body is not None:
            send.setdefault("Content-Type", "application/json")
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=send)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data)

    def test_the_station_runs_hosts_operations_through_its_own_action_layer(self):
        names = [o["name"] for o in self.station.operations()]
        self.assertTrue({"hosts.save", "hosts.compose", "hosts.compose_update"} <= set(names))
        result = self.station.action("hosts.compose", {"persona": "nova", "segment": "song_intro", "facts": FULL, "seed": 2})
        self.assertEqual((result["ok"], result["operation"], result["speaker"]), (True, "hosts.compose", 1))
        self.assertTrue(result["text"].startswith("<|speaker:1|>"))
        for args in ({}, {"persona": "nova", "segment": "poem"}, {"persona": "nova", "segment": "song_intro", "seed": -2}):
            with self.subTest(args=args), self.assertRaises(StationError):
                self.station.action("hosts.compose", args)
        with self.assertRaisesRegex(StationError, "no host"):
            self.station.action("hosts.compose", {"persona": "nobody", "segment": "filler"})

    def test_hosts_never_touch_the_library_or_the_queue(self):
        before = self.station.status()
        self.station.action("hosts.compose", {"persona": "nova", "segment": "spot_wrapper", "facts": FULL})
        self.station.action("hosts.save", {"persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "warm"}})
        after = self.station.status()
        self.assertEqual((after["library"], after["queue"]), (before["library"], before["queue"]))
        self.assertEqual(after["extensions"]["hosts"]["state"], "running")
        self.assertEqual(sorted(os.listdir(Path(self.temp.name) / "data" / "media")), [])

    def test_the_routes_need_the_launch_code_and_a_local_origin(self):
        self.assertEqual(self.call("GET", "/api/x/hosts/personas", token=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/x/hosts/preview", {"persona": "nova", "segment": "filler"}, token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/x/hosts/personas", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/x/hosts/personas", headers={"Host": "evil.example"})[0], 403)
        status, body = self.call("GET", "/api/x/hosts/personas")
        self.assertEqual((status, len(body["personas"])), (200, 16))
        status, body = self.call("GET", "/api/x/hosts/formats")
        self.assertEqual((status, len(body["formats"])), (200, 3))

    def test_a_preview_over_http_is_text_only_and_bounded(self):
        status, body = self.call("POST", "/api/x/hosts/preview", {"persona": "dex", "segment": "song_intro", "facts": FULL, "seed": 1})
        self.assertEqual((status, body["audio"], body["preview"]), (200, False, True))
        status, body = self.call("POST", "/api/x/hosts/preview", {"persona": "dex", "segment": "filler", "facts": {"notes": ["x" * 70000]}})
        self.assertEqual(status, 400)                     # over the route's own size limit
        status, body = self.call("POST", "/api/x/hosts/preview", {"persona": "dex", "segment": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertEqual(self.call("POST", "/api/x/hosts/preview", None)[0], 400)

    def test_agents_can_manage_hosts_through_the_shared_action_endpoint(self):
        status, body = self.call("POST", "/api/action", {"operation": "hosts.save", "arguments": {
            "persona": {"name": "Rhea", "role": "r", "blurb": "b", "voice": "warm"}}})
        self.assertEqual((status, body["persona"]["id"]), (200, "rhea"))
        status, body = self.call("POST", "/api/action", {"operation": "hosts.remove", "arguments": {"id": "rhea"}})
        self.assertEqual((status, body["removed"]), (200, True))
        status, body = self.call("POST", "/api/action", {"operation": "hosts.remove", "arguments": {"id": "rhea"}})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
