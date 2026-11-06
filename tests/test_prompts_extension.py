"""The prompts extension inside the real extension host, the real Station and the real loopback HTTP
server. Temporary directories, 127.0.0.1 port 0 and a made-up token only: nothing outside this process."""
import http.client
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from myradio.extensions import ExtensionError, ExtensionHost, Request, StationFacade, builtin_extensions
from myradio.prompts import extension as extension_module
from myradio.prompts.extension import PromptsExtension, build
from myradio.server import Server
from myradio.station import Station, StationError

TOKEN = "t" * 40
EDIT = {"id": "my-porch", "title": "My porch", "based_on": "porch-blues-slide/slow-tide", "palette": {"name": "My Porch Blues"}}


class ForbiddenStation:
    """Extensions must never call station.status() (the host guards against that); this proves it."""

    def status(self):
        raise AssertionError("prompts must not call station.status()")

    def import_audio(self, *args, **kwargs):
        raise AssertionError("prompts never imports audio")

    def action(self, *args, **kwargs):
        raise AssertionError("prompts never drives the station")


class HostCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.threads_before = {t.name for t in threading.enumerate()}
        self.boot()

    def boot(self):
        self.host = ExtensionHost(self.temp.name, StationFacade(ForbiddenStation()))
        self.extension = build()
        self.host.register(self.extension)
        self.host.start()
        self.addCleanup(self.host.stop)

    def reboot(self):
        self.host.stop()
        self.boot()

    def route(self, method, path, query=None, body=None):
        match = self.host.find_route(method, f"/api/x/prompts{path}")
        self.assertIsNotNone(match, f"{method} {path} is not routed")
        raw = json.dumps(body).encode() if body is not None else b""
        return self.host.dispatch_route(match, Request(method, path, query or {}, body, raw))

    @property
    def state_dir(self):
        return Path(self.temp.name) / "ext" / "prompts"


class RegistrationTests(HostCase):
    def test_it_is_a_built_in_extension_that_loads(self):
        self.assertEqual(self.extension.name, "prompts")
        found = [e for e in builtin_extensions() if e.name == "prompts"]
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], PromptsExtension)

    def test_operations_routes_and_schemas(self):
        catalog = {entry["name"]: entry for entry in self.host.catalog()}
        self.assertEqual(set(catalog), {"prompts.favorite", "prompts.save_custom", "prompts.remove_custom"})
        favorite = catalog["prompts.favorite"]
        self.assertEqual(set(favorite["required"]), {"id", "on"})
        self.assertEqual(favorite["arguments"]["on"]["type"], "boolean")
        self.assertEqual(catalog["prompts.save_custom"]["arguments"]["preset"]["type"], "object")
        self.assertEqual(catalog["prompts.remove_custom"]["required"], ["id"])
        for entry in catalog.values():
            self.assertLessEqual(len(entry["description"]), 400)
        for method, path in (("GET", "/catalog"), ("GET", "/search"), ("GET", "/sliders"), ("GET", "/lyrics"), ("POST", "/compose")):
            self.assertIsNotNone(self.host.find_route(method, f"/api/x/prompts{path}"))
        for method, path in (("POST", "/catalog"), ("GET", "/compose"), ("GET", "/favorite")):
            self.assertIsNone(self.host.find_route(method, f"/api/x/prompts{path}"))

    def test_the_private_file_lives_in_the_extensions_own_folder_and_is_listed_for_backup(self):
        database = self.state_dir / "prompts.sqlite3"
        self.assertTrue(database.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(database).st_mode), 0o600)
        self.assertEqual(self.extension.persistent_files(), ["prompts.sqlite3"])
        self.assertEqual([p.name for p in self.host.backup_files()["prompts"]], ["prompts.sqlite3"])
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir() if not p.name.endswith(("-wal", "-shm"))), ["prompts.sqlite3"])

    def test_nothing_is_left_running_after_stop(self):
        self.host.stop()
        self.host.stop()  # idempotent
        extra = {t.name for t in threading.enumerate()} - self.threads_before
        self.assertEqual({n for n in extra if "prompts" in n}, set())
        self.assertIsNone(self.extension._store)
        with self.assertRaises(Exception):
            self.extension.service.store._conn.execute("SELECT 1")


class OperationTests(HostCase):
    def test_favourites_saved_styles_and_removal_through_the_validated_operation_layer(self):
        self.assertEqual(self.host.call("prompts.favorite", {"id": "neo-soul", "on": True}),
                         {"ok": True, "operation": "prompts.favorite", "id": "neo-soul", "favorite": True, "kind": "style"})
        saved = self.host.call("prompts.save_custom", {"preset": EDIT})
        self.assertEqual((saved["id"], saved["revision"], saved["created"], saved["warnings"]), ("user:my-porch", 1, True, []))
        self.assertEqual(self.host.call("prompts.favorite", {"id": "user:my-porch", "on": True})["kind"], "preset")
        status = self.host.status()["prompts"]["status"]
        self.assertEqual((status["saved_styles"], status["favorites"]), (1, 2))
        self.assertEqual(self.host.call("prompts.remove_custom", {"id": "user:my-porch"})["removed"], True)
        status = self.host.status()["prompts"]["status"]
        self.assertEqual((status["saved_styles"], status["favorites"]), (0, 1))

    def test_bad_arguments_are_refused_by_the_host_and_by_the_extension_with_plain_words(self):
        bad = [
            ("prompts.favorite", {}), ("prompts.favorite", {"id": "neo-soul"}), ("prompts.favorite", {"id": "neo-soul", "on": "yes"}),
            ("prompts.favorite", {"id": "", "on": True}), ("prompts.favorite", {"id": "x" * 121, "on": True}),
            ("prompts.favorite", {"id": "neo-soul", "on": True, "extra": 1}),
            ("prompts.save_custom", {}), ("prompts.save_custom", {"preset": "text"}), ("prompts.save_custom", {"preset": {"k": "y" * 70000}}),
            ("prompts.remove_custom", {"id": ""}), ("prompts.remove_custom", {"id": "x" * 81}),
        ]
        for name, arguments in bad:
            with self.subTest(name=name, arguments=str(arguments)[:40]), self.assertRaises(ExtensionError):
                self.host.call(name, arguments)
        for name, arguments, expected in (
            ("prompts.favorite", {"id": "nothing", "on": True}, "no style called"),
            ("prompts.save_custom", {"preset": {"id": "Bad Id"}}, "lowercase letters"),
            ("prompts.save_custom", {"preset": {**EDIT, "palette": {"meter": "7/8 (2+2+2)"}}}, "cannot be saved yet"),
            ("prompts.remove_custom", {"id": "neo-soul/rondo-path"}, "start with"),
            ("prompts.remove_custom", {"id": "user:ghost"}, "no saved style"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ExtensionError, expected):
                self.host.call(name, arguments)
        self.assertEqual(self.host.status()["prompts"]["state"], "running")

    def test_an_unexpected_fault_is_reported_generically_and_never_leaks(self):
        with patch.object(self.extension.service, "favorite", side_effect=RuntimeError("secret at /home/x")):
            with self.assertRaises(ExtensionError) as caught:
                self.host.call("prompts.favorite", {"id": "neo-soul", "on": True})
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("/home", str(caught.exception))

    def test_everything_survives_a_restart(self):
        self.host.call("prompts.favorite", {"id": "neo-soul", "on": True})
        self.host.call("prompts.save_custom", {"preset": EDIT})
        first = self.route("POST", "/compose", body={"seed": 8, "tags": ["acoustic"]}).body
        self.reboot()
        status = self.host.status()["prompts"]["status"]
        self.assertEqual((status["saved_styles"], status["favorites"], status["remembered"]), (1, 1, 1))
        listed = self.route("GET", "/catalog", {"source": ["user"]}).body
        self.assertEqual([i["id"] for i in listed["items"]], ["user:my-porch"])
        again = self.route("POST", "/compose", body={"preset": "user:my-porch", "seed": 2, "remember": False}).body
        self.assertEqual(again["id"], "user:my-porch")
        second = self.route("POST", "/compose", body={"seed": 8, "tags": ["acoustic"]}).body
        self.assertNotEqual(first["id"], second["id"], "the repeat memory survived the restart")


class RouteTests(HostCase):
    def test_catalog_route_pages_and_filters(self):
        page = self.route("GET", "/catalog").body
        self.assertEqual((page["total"], len(page["items"]), page["limit"], page["next_offset"]), (1278, 24, 24, 24))
        small = self.route("GET", "/catalog", {"view": ["styles"], "limit": ["5"], "offset": ["5"], "family": ["celtic"]}).body
        self.assertEqual((small["total"], [i["id"] for i in small["items"]]), (2, []))
        jazz = self.route("GET", "/catalog", {"view": ["styles"], "tag": ["jazz", "acoustic"], "exclude_tag": ["waltz"],
                                              "energy": ["calm"], "limit": ["100"]}).body
        self.assertTrue(jazz["items"])
        self.assertTrue(all("jazz" in i["tags"] and "acoustic" in i["tags"] and i["energy"] == "calm" for i in jazz["items"]))
        self.assertEqual(self.route("GET", "/catalog", {"q": ["flute celtic"], "view": ["styles"]}).body["total"], 1)
        self.assertEqual(self.route("GET", "/catalog", {"favorites": ["true"]}).body["total"], 0)

    def test_catalog_route_rejects_bad_input_with_plain_messages(self):
        cases = [
            ({"colour": ["red"]}, "not recognised"), ({"limit": ["0"]}, "'limit' must be a whole number from 1 to 100"),
            ({"limit": ["abc"]}, "'limit' must be"), ({"limit": ["1", "2"]}, "only once"), ({"offset": ["-1"]}, "'offset' must be"),
            ({"limit": ["9" * 30]}, "'limit' must be"), ({"favorites": ["maybe"]}, "'favorites' must be true or false"),
            ({"view": ["songs"]}, "view must be"), ({"energy": ["loud"]}, "energy must be"),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                reply = self.route("GET", "/catalog", query)
                self.assertEqual(reply.status, 400)
                self.assertIn(expected, reply.body["error"])

    def test_messages_never_grow_with_what_the_caller_sent(self):
        long = "x" * 5000
        replies = [
            self.route("GET", "/catalog", {long: ["1"]}), self.route("GET", "/search", {"q": ["a"], long: ["1"]}),
            self.route("GET", "/lyrics", {"preset": [long], "seed": ["1"]}),
            self.route("POST", "/compose", body={"preset": long}), self.route("POST", "/compose", body={long: 1}),
        ]
        for reply in replies:
            self.assertEqual(reply.status, 400)
            self.assertLess(len(reply.body["error"]), 300, reply.body["error"][:80])
        for name, arguments in (("prompts.save_custom", {"preset": {"id": "a-b", "based_on": long}}),
                                ("prompts.save_custom", {"preset": {"id": "a-b", long: 1}}),
                                ("prompts.save_custom", {"preset": {"id": "a-b", "palette": {long: 1}, "based_on": "neo-soul/rondo-path"}}),
                                ("prompts.favorite", {"id": long[:100], "on": True})):
            with self.assertRaises(ExtensionError) as caught:
                self.host.call(name, arguments)
            self.assertLess(len(str(caught.exception)), 300)

    def test_search_sliders_and_lyrics_routes(self):
        found = self.route("GET", "/search", {"q": ["waltz"]}).body
        self.assertEqual(found["query"], "waltz")
        self.assertIn("musette-waltz-accordion", [i["id"] for i in found["styles"]])
        self.assertEqual(self.route("GET", "/search", {}).status, 400)
        self.assertEqual(self.route("GET", "/search", {"q": ["a"], "limit": ["99"]}).status, 400)
        sliders = self.route("GET", "/sliders", {"energy": ["20"], "adventurousness": ["80"], "vocals": ["100"]}).body
        self.assertEqual((sliders["energy_class"], sliders["complexity"], sliders["modes"]), ("calm", "intricate", ["vocal"]))
        for query in ({}, {"energy": ["20"]}, {"energy": ["x"], "adventurousness": ["1"], "vocals": ["1"]},
                      {"energy": ["101"], "adventurousness": ["1"], "vocals": ["1"]}, {"energy": ["1"], "adventurousness": ["1"], "vocals": ["1"], "z": ["1"]}):
            self.assertEqual(self.route("GET", "/sliders", query).status, 400, query)
        outline = self.route("GET", "/lyrics", {"preset": ["neo-soul/rondo-path"], "seed": ["8"]}).body
        self.assertEqual((outline["source"], outline["speech"], outline["preset_id"]), ("scaffold", False, "neo-soul/rondo-path"))
        self.assertTrue(outline["lyrics"].startswith("[Verse 1]"))
        for query in ({}, {"preset": ["neo-soul/rondo-path"]}, {"seed": ["1"]}, {"preset": ["nothing"], "seed": ["1"]},
                      {"preset": ["neo-soul/rondo-path"], "seed": ["-1"]}, {"preset": ["neo-soul/rondo-path"], "seed": ["1"], "tag_style": ["x"]}):
            self.assertEqual(self.route("GET", "/lyrics", query).status, 400, query)
        self.assertEqual(self.host.status()["prompts"]["status"]["remembered"], 0, "browsing and previewing leave no memory")

    def test_compose_route_returns_a_prompt_with_its_stable_id(self):
        reply = self.route("POST", "/compose", body={"seed": 31, "tags": ["jazz"], "sliders": {"energy": 30, "adventurousness": 50, "vocals": 0}})
        self.assertEqual(reply.status, 200)
        body = reply.body
        self.assertEqual(body["id"], body["prompt"]["preset_id"])
        self.assertEqual(body["provenance"]["fingerprint"], body["prompt"]["fingerprint"])
        self.assertTrue(body["remembered"])
        self.assertEqual(self.host.status()["prompts"]["status"]["remembered"], 1)
        again = self.route("POST", "/compose", body={"preset": body["id"], "seed": 31, "complexity": body["treatment"]["complexity"],
                                                    "density": body["treatment"]["density"], "remember": False}).body
        self.assertEqual(again["prompt"]["style"], body["prompt"]["style"])

    def test_compose_route_rejects_what_it_cannot_understand(self):
        for body, expected in ((None, "JSON object"), ([1, 2], "JSON object"), ({"seed": -1}, "seed must be"), ({"colour": 1}, "not recognised"),
                               ({"preset": "nothing"}, "no style called")):
            reply = self.route("POST", "/compose", body=body) if body is not None else self.host.dispatch_route(
                self.host.find_route("POST", "/api/x/prompts/compose"), Request("POST", "/compose", {}, None, b"not json"))
            with self.subTest(body=body):
                self.assertEqual(reply.status, 400)
                self.assertIn(expected, reply.body["error"])
        self.assertEqual(self.host.status()["prompts"]["status"]["remembered"], 0)

    def test_an_unexpected_route_fault_is_a_generic_500(self):
        with patch.object(self.extension.service, "catalog", side_effect=RuntimeError("secret internals")):
            reply = self.route("GET", "/catalog")
        self.assertEqual(reply.status, 500)
        self.assertNotIn("secret", json.dumps(reply.body))


class StatusAndChecksTests(HostCase):
    def test_status_is_a_cached_snapshot_that_never_touches_the_database_or_the_station(self):
        status = self.host.status()["prompts"]
        self.assertEqual(status["state"], "running")
        body = status["status"]
        self.assertEqual((body["ready"], body["catalog_version"], body["styles"], body["arrangements"], body["presets"]), (True, "1.1.0", 71, 18, 1278))
        self.assertEqual((body["saved_styles"], body["favorites"], body["remembered"], body["store_available"], body["rules_ok"]), (0, 0, 0, True, True))
        self.assertIsNone(body["last_composed"])
        with patch.object(self.extension.service, "snapshot", side_effect=AssertionError("status must not recompute")), \
                patch.object(self.extension.service.store, "counts", side_effect=AssertionError("status must not read the database")):
            started = time.monotonic()
            for _ in range(2000):
                self.host.status()
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.host.status()["prompts"]["status"], body)

    def test_status_does_not_wait_for_a_write_in_progress(self):
        store = self.extension.service.store
        entered = threading.Event()
        release = threading.Event()
        original = store.set_favorite

        def slow(*args, **kwargs):
            with store._lock:
                entered.set()
                release.wait(5)
            return original(*args, **kwargs)

        with patch.object(store, "set_favorite", side_effect=slow):
            worker = threading.Thread(target=lambda: self.host.call("prompts.favorite", {"id": "neo-soul", "on": True}), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            self.host.status()
            self.assertLess(time.monotonic() - started, 0.5, "status must stay lock-free while the database lock is held")
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_a_passing_database_problem_shows_in_status_and_clears_itself(self):
        from myradio.prompts.store import StoreError
        with patch.object(self.extension.service, "snapshot", side_effect=StoreError("Your saved styles could not be read right now.")):
            self.extension._refresh()
        shown = self.host.status()["prompts"]["status"]
        self.assertEqual((shown["store_available"], shown["error"]), (False, "Your saved styles could not be read right now."))
        self.assertEqual({c["id"]: c for c in self.host.checks()}["prompts.saved-styles"]["status"], "warn")
        self.extension._refresh()  # the file answers again
        recovered = self.host.status()["prompts"]["status"]
        self.assertEqual((recovered["store_available"], recovered["error"]), (True, None))
        self.assertEqual({c["id"]: c for c in self.host.checks()}["prompts.saved-styles"]["status"], "ok")

    def test_status_and_checks_contain_no_secrets_or_paths(self):
        text = json.dumps([self.host.status(), self.host.checks()])
        self.assertNotIn(self.temp.name, text)
        self.assertNotIn("sqlite3", text)

    def test_doctor_checks_are_plain_and_cheap(self):
        started = time.monotonic()
        checks = {c["id"]: c for c in self.host.checks()}
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(set(checks), {"prompts.catalog", "prompts.rules", "prompts.saved-styles", "prompts.repeat-memory"})
        self.assertTrue(all(c["status"] == "ok" for c in checks.values()), checks)
        self.assertIn("71 styles and 18 arrangement designs", checks["prompts.catalog"]["detail"])
        self.assertIn("1,278 combinations", checks["prompts.catalog"]["detail"])
        for check in checks.values():
            self.assertTrue(check["title"] and check["detail"])
        self.assertIn("memory is empty", checks["prompts.repeat-memory"]["detail"])
        self.host.call("prompts.favorite", {"id": "neo-soul", "on": True})
        self.route("POST", "/compose", body={"seed": 1})
        later = {c["id"]: c for c in self.host.checks()}
        self.assertIn("1 favourite", later["prompts.saved-styles"]["detail"])
        self.assertIn("last 1 composed prompt (up to 1,000)", later["prompts.repeat-memory"]["detail"])

    def test_a_broken_catalog_rule_is_reported_as_a_warning_not_a_crash(self):
        self.extension._rules = {"ok": False, "errors": [{"message": "An instrument does not belong."}], "warnings": 0, "rules": 27}
        self.extension._refresh()
        rules = {c["id"]: c for c in self.host.checks()}["prompts.rules"]
        self.assertEqual(rules["status"], "warn")
        self.assertIn("An instrument does not belong.", rules["detail"])
        self.assertEqual(self.host.status()["prompts"]["state"], "running")


class StatusFollowsTheServiceTests(HostCase):
    """Other code (the generation lane, the station recipes) calls PromptService.compose directly. The status
    and the doctor must still be right afterwards, without anyone calling a route."""

    def test_in_process_composes_update_the_status_and_the_doctor(self):
        service = self.extension.service
        self.assertEqual(self.host.status()["prompts"]["status"]["remembered"], 0)
        for seed in range(5):
            service.compose({"seed": seed})
        status = self.host.status()["prompts"]["status"]
        self.assertEqual(status["remembered"], 5)
        self.assertIsNotNone(status["last_composed"])
        memory = {c["id"]: c for c in self.host.checks()}["prompts.repeat-memory"]
        self.assertIn("last 5 composed prompts", memory["detail"])
        self.assertNotIn("empty", memory["detail"])

    def test_previews_leave_the_status_alone_and_every_kind_of_change_updates_it(self):
        service = self.extension.service
        service.compose({"seed": 1, "remember": False})
        self.assertEqual(self.host.status()["prompts"]["status"]["remembered"], 0)
        service.save_custom(EDIT)
        service.favorite("neo-soul", True)
        status = self.host.status()["prompts"]["status"]
        self.assertEqual((status["saved_styles"], status["favorites"]), (1, 1))
        service.favorite("neo-soul", False)
        self.assertEqual(self.host.status()["prompts"]["status"]["favorites"], 0)
        service.remove_custom("user:my-porch")
        self.assertEqual(self.host.status()["prompts"]["status"]["saved_styles"], 0)

    def test_the_route_and_the_operations_still_leave_the_status_right(self):
        self.route("POST", "/compose", body={"seed": 2})
        self.host.call("prompts.save_custom", {"preset": EDIT})
        status = self.host.status()["prompts"]["status"]
        self.assertEqual((status["remembered"], status["saved_styles"]), (1, 1))


class BrokenCharacterThroughTheHostTests(HostCase):
    """Half an emoji (for example from a text box that counts UTF-16 units) must be a plain 400, not a 500."""

    def test_the_compose_route_answers_400_in_plain_words(self):
        half = chr(0xD83C)
        reply = self.route("POST", "/compose", body={"seed": 1, "lyrics": "[Verse]\nhello " + half + " world"})
        self.assertEqual(reply.status, 400)
        self.assertIn("broken character", reply.body["error"])
        self.assertEqual(self.host.status()["prompts"]["status"]["remembered"], 0)

    def test_saving_a_style_gives_a_plain_message_and_the_extension_keeps_running(self):
        half = chr(0xD83C)
        with self.assertRaises(ExtensionError) as caught:
            self.host.call("prompts.save_custom", {"preset": {**EDIT, "title": "Blues " + half}})
        self.assertIn("broken character", str(caught.exception))
        self.assertNotIn("No retry", str(caught.exception))
        self.assertEqual(self.host.status()["prompts"]["state"], "running")


class DegradedModeTests(unittest.TestCase):
    """If the saved-styles file cannot be opened the built-in library must keep working."""

    def test_an_unusable_file_leaves_the_built_in_styles_working_and_says_how_to_fix_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "ext" / "prompts"
            state.mkdir(parents=True, mode=0o700)
            (state / "prompts.sqlite3").mkdir()  # a folder where the file should be
            host = ExtensionHost(directory, StationFacade(ForbiddenStation()))
            host.register(build())
            host.start()
            try:
                entry = host.status()["prompts"]
                self.assertEqual(entry["state"], "running")
                self.assertFalse(entry["status"]["store_available"])
                self.assertIn("could not be opened", entry["status"]["error"])
                checks = {c["id"]: c for c in host.checks()}
                self.assertEqual(checks["prompts.saved-styles"]["status"], "warn")
                self.assertIn("restart", checks["prompts.saved-styles"]["hint"])
                match = host.find_route("GET", "/api/x/prompts/catalog")
                self.assertEqual(host.dispatch_route(match, Request("GET", "/catalog", {}, None, b"")).body["total"], 1278)
                compose = host.find_route("POST", "/api/x/prompts/compose")
                body = host.dispatch_route(compose, Request("POST", "/compose", {}, {"seed": 4}, b"{}")).body
                self.assertFalse(body["remembered"])
                for name, arguments in (("prompts.favorite", {"id": "neo-soul", "on": True}), ("prompts.save_custom", {"preset": EDIT})):
                    with self.assertRaisesRegex(ExtensionError, "Saved styles are not available"):
                        host.call(name, arguments)
            finally:
                host.stop()

    def test_a_file_from_a_newer_myradio_is_refused_politely_and_leaves_no_connection_open(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "ext" / "prompts"
            state.mkdir(parents=True, mode=0o700)
            raw = sqlite3.connect(state / "prompts.sqlite3")
            raw.execute("PRAGMA user_version=99")
            raw.commit()
            raw.close()
            made, real = [], sqlite3.connect

            def spy(*args, **kwargs):
                conn = real(*args, **kwargs)
                if str(args[0]).endswith("prompts.sqlite3"):
                    made.append(conn)
                return conn

            host = ExtensionHost(directory, StationFacade(ForbiddenStation()))
            host.register(build())
            with patch.object(sqlite3, "connect", side_effect=spy):
                host.start()
            try:
                entry = host.status()["prompts"]
                self.assertEqual(entry["state"], "running")
                self.assertFalse(entry["status"]["store_available"])
                self.assertIn("newer myradio", entry["status"]["error"])
                self.assertEqual(len(made), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    made[0].execute("SELECT 1")
            finally:
                host.stop()

    def test_a_broken_catalog_fails_the_extension_visibly_instead_of_half_working(self):
        with tempfile.TemporaryDirectory() as directory:
            host = ExtensionHost(directory, StationFacade(ForbiddenStation()))
            host.register(build())
            with patch.object(extension_module, "shared_library", side_effect=ValueError("catalog is damaged")):
                host.start()
            try:
                entry = host.status()["prompts"]
                self.assertEqual(entry["state"], "failed")
                self.assertIn("catalog is damaged", entry["error"])
                self.assertEqual({c["id"] for c in host.checks()}, {"prompts.start"})
            finally:
                host.stop()


class StationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.station = Station(Path(self.temp.name) / "data", start_worker=False, extensions=[build()])
        with patch.dict("os.environ", {}, clear=True):
            self.server = Server(("127.0.0.1", 0), self.station, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.station.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, headers=None, token=True, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if token:
            sent["X-Myradio-Token"] = TOKEN
        data = raw
        if body is not None:
            sent.setdefault("Content-Type", "application/json")
            data = json.dumps(body)
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        try:
            return response.status, json.loads(payload)
        except ValueError:
            return response.status, payload

    def test_routes_need_the_launch_token_and_refuse_foreign_origins(self):
        self.assertEqual(self.call("GET", "/api/x/prompts/catalog", token=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/x/prompts/compose", {"seed": 1}, token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/x/prompts/catalog", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/x/prompts/catalog", headers={"Host": "evil.example"})[0], 403)

    def test_the_whole_flow_over_http(self):
        status, catalog = self.call("GET", "/api/x/prompts/catalog?view=styles&limit=3&tag=jazz&tag=acoustic")
        self.assertEqual(status, 200)
        self.assertTrue(catalog["items"] and all("jazz" in i["tags"] for i in catalog["items"]))
        self.assertEqual(self.call("GET", "/api/x/prompts/catalog?limit=0")[0], 400)
        self.assertEqual(self.call("GET", "/api/x/prompts/search?q=kora")[1]["styles"][0]["id"], "kora-river-ostinato")
        status, composed = self.call("POST", "/api/x/prompts/compose", {"seed": 5, "sliders": {"energy": 80, "adventurousness": 20, "vocals": 100}})
        self.assertEqual(status, 200)
        self.assertEqual(composed["lyrics"]["source"], "scaffold")
        self.assertEqual(composed["treatment"]["density"], "lush")
        self.assertEqual(self.call("POST", "/api/x/prompts/compose", raw="not json", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.call("POST", "/api/x/prompts/compose", raw="{}", headers={"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.call("POST", "/api/x/prompts/compose", {"pad": "x" * 140000})[0], 400)  # over the route's own size limit
        self.assertEqual(self.call("POST", "/api/x/prompts/compose", [1])[0], 400)
        status, saved = self.call("POST", "/api/action", {"operation": "prompts.save_custom", "arguments": {"preset": EDIT}})
        self.assertEqual((status, saved["id"]), (200, "user:my-porch"))
        self.assertEqual(self.call("POST", "/api/action", {"operation": "prompts.favorite", "arguments": {"id": "user:my-porch", "on": True}})[0], 200)
        status, mine = self.call("GET", "/api/x/prompts/catalog?source=user&favorites=1")
        self.assertEqual([i["id"] for i in mine["items"]], ["user:my-porch"])
        self.assertEqual(self.call("POST", "/api/action", {"operation": "prompts.remove_custom", "arguments": {"id": "user:my-porch"}})[0], 200)
        self.assertEqual(self.call("POST", "/api/action", {"operation": "prompts.remove_custom", "arguments": {"id": "user:my-porch"}})[0], 400)

    def test_the_compose_body_limit_is_the_routes_own_and_is_reported_in_words(self):
        compose = next(route for route in build().routes() if (route.method, route.path) == ("POST", "/compose"))
        self.assertEqual(compose.max_body, 131072)
        # A request with only valid settings is refused for its size alone, and says so
        status, over = self.call("POST", "/api/x/prompts/compose", {"query": "x" * 140000})
        self.assertEqual(status, 400)
        self.assertIn("no larger than 131072 bytes", over["error"])
        # A big but allowed request gets through the size gate and is judged by the service instead
        status, under = self.call("POST", "/api/x/prompts/compose", {"lyrics": "x" * 100000})
        self.assertEqual(status, 400)
        self.assertIn("at most 12,000 characters", under["error"])
        self.assertNotIn("no larger than", under["error"])

    def test_operations_catalog_and_station_status_include_the_extension(self):
        _, operations = self.call("GET", "/api/operations")
        names = {o["name"] for o in operations}
        self.assertTrue({"prompts.favorite", "prompts.save_custom", "prompts.remove_custom"} <= names)
        _, status = self.call("GET", "/api/status")
        self.assertEqual(status["extensions"]["prompts"]["state"], "running")
        self.assertEqual(status["extensions"]["prompts"]["status"]["presets"], 1278)
        self.assertIn("prompts.catalog", {c["id"] for c in self.station.extension_checks()} if hasattr(self.station, "extension_checks")
                      else {c["id"] for c in self.station.extensions.checks()})

    def test_a_faulted_station_still_answers_reads_but_not_changes(self):
        with self.station.lock:
            self.station._fail_safe()
        self.assertEqual(self.call("GET", "/api/x/prompts/catalog")[0], 200)
        self.assertEqual(self.call("POST", "/api/x/prompts/compose", {"seed": 1})[0], 400)
        with self.assertRaises(StationError):
            self.station.action("prompts.favorite", {"id": "neo-soul", "on": True})

    def test_composing_never_disturbs_the_playout_clock(self):
        from myradio.station import FRAMES
        started = time.monotonic()
        for seed in range(30):
            self.call("POST", "/api/x/prompts/compose", {"seed": seed, "remember": seed % 2 == 0})
            self.assertEqual(len(self.station.tick()), FRAMES * 4)
        self.assertLess(time.monotonic() - started, 20)


if __name__ == "__main__":
    unittest.main()
