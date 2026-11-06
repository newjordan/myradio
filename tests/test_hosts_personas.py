"""Personas: the shipped cast, validation of user-made hosts, and persistence in the hosts database."""
import json
import os
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hosts_support import FULL, library, new_service  # noqa: E402

from myradio.hosts import HostsError, HostsService, validate_persona  # noqa: E402
from myradio.hosts.personas import ID_RE, PACES, load_builtin_personas, slugify  # noqa: E402
from myradio.hosts.segments import LEXICON_KEYS, SEGMENT_IDS  # noqa: E402
from myradio.hosts.store import MAX_CUSTOM  # noqa: E402
from myradio.hosts.textsafe import EVENT_TAGS, MAX_SPEAKER  # noqa: E402


def minimal(**changes):
    host = {"name": "Rhea", "role": "Afternoon host", "blurb": "Easygoing and kind.", "voice": "warm"}
    host.update(changes)
    return host


class BuiltInCastTests(unittest.TestCase):
    def setUp(self):
        self.voices = library().voices
        self.cast = load_builtin_personas(self.voices)

    def test_the_cast_is_at_least_fourteen_distinct_characters(self):
        self.assertGreaterEqual(len(self.cast), 14)
        self.assertEqual(len({p.name for p in self.cast.values()}), len(self.cast))
        self.assertEqual(len({p.blurb for p in self.cast.values()}), len(self.cast))
        self.assertEqual(len({p.role for p in self.cast.values()}), len(self.cast))
        self.assertGreaterEqual(len({p.voice for p in self.cast.values()}), 8)

    def test_the_product_brief_archetypes_are_present(self):
        roles = " | ".join(p.role.lower() for p in self.cast.values())
        for archetype in ("late-night host", "morning host", "dry-wit curator", "anchor", "community-radio volunteer",
                          "jazz-cafe", "maker-shop host", "deep-focus minimalist"):
            self.assertIn(archetype, roles.replace("café", "cafe"))
        # the four characters shown in the design preview keep their ids
        for preview_id in ("nova", "dex", "marlowe", "professor"):
            self.assertIn(preview_id, self.cast)

    def test_every_persona_validates_against_the_schema(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "myradio" / "hosts" / "data" / "personas.json").read_text("utf-8"))
        self.assertEqual(len(data["personas"]), len(self.cast))
        for entry in data["personas"]:
            with self.subTest(host=entry["id"]):
                persona, warnings = validate_persona(entry, self.voices, builtin=True)
                self.assertEqual(warnings, [])
                self.assertRegex(persona.id, ID_RE.pattern)
                self.assertIn(persona.pace, PACES)
                self.assertTrue(0 <= persona.speaker <= MAX_SPEAKER)
                self.assertTrue(1 <= persona.verbosity[0] <= persona.verbosity[1] <= 5)
                self.assertEqual(persona.language, "en")
                self.assertTrue(set(persona.allowed_tags) <= set(EVENT_TAGS))
                self.assertTrue(persona.traits and persona.prohibited_topics)
                self.assertTrue(persona.builtin)
                for key in LEXICON_KEYS:
                    self.assertTrue(persona.lexicon[key], key)

    def test_each_host_has_its_own_speaker_slot_and_a_real_script_style(self):
        speakers = [p.speaker for p in self.cast.values()]
        self.assertEqual(len(set(speakers)), len(speakers))
        for persona in self.cast.values():
            self.assertIn(persona.voice, self.voices)

    def test_hosts_are_fictional_and_say_so(self):
        for persona in self.cast.values():
            public = persona.to_public()
            self.assertTrue(public["fictional"])
            text = " ".join([persona.name, persona.role, persona.blurb, *persona.traits]).lower()
            for phrase in ("real person", "clone", "impersonat", "sounds like", "voice of"):
                self.assertNotIn(phrase, text)

    def test_public_view_is_json_and_complete(self):
        service = new_service()
        catalog = service.catalog()
        json.dumps(catalog)
        self.assertEqual(len(catalog["personas"]), len(self.cast))
        first = catalog["personas"][0]
        self.assertEqual(set(first), {"id", "name", "role", "blurb", "traits", "speaker", "allowed_tags", "pace", "wps",
                                      "verbosity", "language", "prohibited_topics", "voice", "voice_label", "lexicon", "tone",
                                      "builtin", "hidden", "fictional", "templates"})
        for host in catalog["personas"]:
            self.assertEqual(set(host["templates"]), set(SEGMENT_IDS))
            self.assertTrue(all(n >= 12 for n in host["templates"].values()), host["id"])
            self.assertEqual(service.template_count(host["id"], "song_intro"), host["templates"]["song_intro"])
        self.assertEqual({v["id"] for v in catalog["voices"]}, set(self.voices))
        self.assertEqual([s["id"] for s in catalog["segments"]][:2], ["station_id", "song_intro"])
        self.assertEqual(catalog["paces"], PACES)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.voices = library().voices

    def check(self, raw, **kw):
        return validate_persona(raw, self.voices, **kw)

    def test_a_minimal_host_gets_sensible_defaults(self):
        persona, warnings = self.check(minimal())
        self.assertEqual((persona.id, persona.pace, persona.verbosity, persona.language, persona.speaker),
                         ("rhea", "steady", (2, 3), "en", 0))
        self.assertEqual(warnings, [])
        self.assertEqual(persona.lexicon["audience"], self.voices["warm"].lexicon["audience"])
        self.assertRegex(persona.tone, r"^#[0-9a-f]{6}$")

    def test_bad_values_get_plain_language_errors(self):
        cases = [
            ("not an object", "just text", "name, a role"),
            ("no name", minimal(name=""), "name"),
            ("name without letters", minimal(name="123"), "letter"),
            ("no role", minimal(role=None), "role"),
            ("no blurb", minimal(blurb=""), "description"),
            ("speaker out of range", minimal(speaker=MAX_SPEAKER + 1), "voice slot"),
            ("speaker is a bool", minimal(speaker=True), "voice slot"),
            ("speaker is text", minimal(speaker="2"), "voice slot"),
            ("bad pace", minimal(pace="fast"), "slow, steady or brisk"),
            ("bad verbosity", minimal(verbosity=[4, 2]), "Talkativeness"),
            ("verbosity wrong shape", minimal(verbosity=3), "Talkativeness"),
            ("verbosity out of range", minimal(verbosity=[0, 9]), "Talkativeness"),
            ("language not supported", minimal(language="es"), "English"),
            ("unknown effect", minimal(allowed_tags=["shout"]), "sound effects"),
            ("unknown voice style", minimal(voice="shouty"), "script style"),
            ("bad id", minimal(id="Bad Id!"), "lowercase"),
            ("bad colour", minimal(tone="red"), "colour"),
            ("traits not a list", minimal(traits=5), "Traits"),
            ("phrases not a set", minimal(lexicon="hello"), "phrases"),
            ("marked as real", dict(minimal(), fictional=False), "made-up"),
            ("impersonation in the blurb", minimal(blurb="Sounds like a famous presenter."), "made-up"),
            ("clone in the role", minimal(role="Voice clone of someone"), "made-up"),
            ("impersonation in a phrase", minimal(lexicon={"aside": ["in the style of somebody"]}), "made-up"),
        ]
        for label, raw, expected in cases:
            with self.subTest(case=label), self.assertRaises(HostsError) as caught:
                self.check(raw)
            message = str(caught.exception)
            self.assertTrue(message.endswith("."), message)
            self.assertNotIn("Traceback", message)
            if expected:
                self.assertIn(expected, message)

    def test_a_value_of_the_wrong_kind_in_any_field_is_a_plain_error_never_a_crash(self):
        wrong = [None, True, False, 0, 7, -1, 1.5, float("nan"), "", "x", [], ["x"], [1], {}, {"a": 1}, (1, 2), b"bytes", object()]
        expected = {"pace": "slow, steady or brisk", "allowed_tags": "sound effects", "speaker": "voice slot", "verbosity": "Talkativeness",
                    "language": "English", "voice": "script style", "id": "lowercase", "tone": "colour", "traits": "Traits",
                    "prohibited_topics": "Topics", "lexicon": "phrases", "name": "name", "role": "role", "blurb": "description"}
        for field in expected:
            for value in wrong:
                with self.subTest(field=field, value=repr(value)[:24]):
                    try:
                        self.check(minimal(**{field: value}))
                    except HostsError as error:
                        message = str(error)
                        self.assertTrue(message[0].isupper() and message.endswith("."), message)
                        self.assertNotRegex(message, r"Traceback|Error\b|unhashable|iterable|object")
                    # (a few of these are fine: an empty list of traits, None for an optional field, and so on)
        for field, value in (("pace", ["slow"]), ("pace", {"a": 1}), ("pace", 5), ("allowed_tags", 5), ("allowed_tags", True),
                             ("allowed_tags", 1.5), ("allowed_tags", {"laughs": 1})):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(HostsError, expected[field]):
                self.check(minimal(**{field: value}))
        self.assertEqual(self.check(minimal(allowed_tags=None))[0].allowed_tags, ())

    def test_random_nonsense_is_only_ever_refused_in_plain_words(self):
        rng = random.Random(20261105)
        junk = [None, True, 0, 5, 2 ** 70, 1.5, float("inf"), "", "slow", "warm", "<|speaker:1|>", ["slow"], [], {}, {"a": 1}, (1, 2), [1, 2],
                ["a", 1], [[]], ["laughs", None], {"laughs": 1}, b"b", "\x00"]
        keys = ["id", "name", "role", "blurb", "traits", "speaker", "allowed_tags", "pace", "verbosity", "language", "prohibited_topics",
                "voice", "lexicon", "tone", "fictional"]
        refused = 0
        for _ in range(3000):
            raw = minimal()
            for key in rng.sample(keys, rng.randint(1, 4)):
                raw[key] = rng.choice(junk)
            if rng.random() < 0.2:
                raw["lexicon"] = {rng.choice(list(LEXICON_KEYS) + ["other"]): rng.choice(junk)}
            try:
                self.check(raw)
            except HostsError:
                refused += 1
        self.assertGreater(refused, 1000)

    def test_markup_in_any_field_is_removed_and_reported(self):
        persona, warnings = self.check(minimal(
            name="<|speaker:9|>Rhea[laughs]", role="Host <b>of</b> mornings", blurb="Kind [whispers] and calm <|speaker:2|>.",
            traits=["Warm\x00", "[excited]Quick"], prohibited_topics=["<|speaker:1|>gossip"],
            lexicon={"audience": ["friends <|speaker:3|>"], "aside": ["the {station} is on [laughs]"],
                     "sign_off": ["take care‮"], "vibe": ["calm"], "place": ["the desk"]}))
        everything = [persona.name, persona.role, persona.blurb, *persona.traits, *persona.prohibited_topics,
                      *(p for phrases in persona.lexicon.values() for p in phrases)]
        for text in everything:
            self.assertFalse(set("<>[]{}|\\\x00‮") & set(text), text)
        self.assertEqual(persona.name, "Rhea")
        self.assertTrue(any("Voice codes" in w for w in warnings))

    def test_lengths_and_counts_are_bounded(self):
        persona, warnings = self.check(minimal(name="N" * 90, blurb="b" * 900, traits=[f"trait{i}" for i in range(20)],
                                               prohibited_topics=[f"topic{i}" for i in range(40)],
                                               lexicon={"audience": [f"friend{i}" for i in range(30)]}))
        self.assertLessEqual(len(persona.name), 40)
        self.assertLessEqual(len(persona.blurb), 200)
        self.assertEqual(len(persona.traits), 5)
        self.assertEqual(len(persona.prohibited_topics), 12)
        self.assertEqual(len(persona.lexicon["audience"]), 8)
        self.assertTrue(warnings)

    def test_unknown_fields_are_ignored_with_a_note_but_round_trip_fields_are_not(self):
        public = new_service().catalog()["personas"][0]
        persona, warnings = self.check(dict(public, id="copy-of-nova", name="Nova Two"))
        self.assertEqual((persona.id, warnings), ("copy-of-nova", []))
        _, warnings = self.check(minimal(favourite_colour="green"))
        self.assertIn("favourite_colour", warnings[0])

    def test_effect_names_may_be_given_as_one_word(self):
        persona, _ = self.check(minimal(allowed_tags="laughs"))
        self.assertEqual(persona.allowed_tags, ("laughs",))

    def test_slugify(self):
        self.assertEqual(slugify("Professor Static"), "professor-static")
        self.assertEqual(slugify("  Élodie & Co. "), "elodie-co")
        self.assertEqual(slugify("???"), "host")
        self.assertTrue(len(slugify("x" * 200)) <= 40)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "hosts.sqlite3"

    def open(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")      # the tests do not need to survive a power cut
        self.addCleanup(conn.close)
        return conn

    def service(self):
        return HostsService(self.open(), library=library())

    def test_saving_creates_updates_and_returns_the_cleaned_host(self):
        service = self.service()
        result = service.save_persona(minimal())
        self.assertEqual((result["persona"]["id"], result["created"], result["persona"]["builtin"]), ("rhea", True, False))
        again = service.save_persona(minimal(id="rhea", blurb="Now with a new blurb."))
        self.assertEqual((again["created"], again["persona"]["blurb"]), (False, "Now with a new blurb."))
        self.assertEqual(service.persona("rhea").blurb, "Now with a new blurb.")
        self.assertEqual(service.store.counts, {"builtin": 16, "custom": 1, "hidden": 0, "visible": 17})

    def test_a_new_host_without_an_id_never_overwrites_another(self):
        service = self.service()
        service.save_persona(minimal(name="Rhea"))
        with self.assertRaisesRegex(HostsError, "already a host called"):
            service.save_persona(minimal(name="rhea"))
        second = service.save_persona(minimal(name="Rhea Two"))["persona"]["id"]
        self.assertEqual(second, "rhea-two")

    def test_ids_that_collide_with_the_built_in_cast_are_refused(self):
        service = self.service()
        with self.assertRaisesRegex(HostsError, "built-in host"):
            service.save_persona(minimal(id="nova"))
        with self.assertRaisesRegex(HostsError, "already a host called Nova"):
            service.save_persona(minimal(name="NOVA"))
        service.save_persona(minimal(name="Nova!!", role="Another"))   # a different name is fine
        self.assertEqual(service.persona("nova-2").name, "Nova!!")

    def test_hosts_survive_a_restart(self):
        service = self.service()
        service.save_persona(minimal(speaker=5, pace="brisk", allowed_tags=["laughs"],
                                     lexicon={"audience": ["gadabouts"]}))
        service.remove_persona("dex")      # a built-in: hidden, not deleted
        service.compose("rhea", "song_intro", FULL, seed=3)
        reopened = self.service()
        rhea = reopened.persona("rhea")
        self.assertEqual((rhea.speaker, rhea.pace, rhea.allowed_tags, rhea.lexicon["audience"]),
                         (5, "brisk", ("laughs",), ("gadabouts",)))
        self.assertEqual(reopened.store.counts["hidden"], 1)
        self.assertNotIn("dex", [p.id for p in reopened.personas()])
        self.assertIn("dex", [p.id for p in reopened.personas(include_hidden=True)])
        self.assertTrue(reopened.memory.recent("rhea", "song_intro", 5))   # repetition memory persisted

    def test_removing_a_built_in_host_only_hides_it(self):
        service = self.service()
        result = service.remove_persona("nova")
        self.assertEqual((result["removed"], result["hidden"]), (False, True))
        self.assertIn("hidden instead of deleted", result["message"])
        self.assertNotIn("nova", [p.id for p in service.personas()])
        self.assertEqual(service.persona("nova").name, "Nova")             # still resolves, so shows keep working
        self.assertTrue(service.compose("nova", "song_intro", FULL, seed=1, dry_run=True).text)
        self.assertEqual(service.remove_persona("nova")["hidden"], True)     # idempotent
        self.assertEqual(service.restore_persona("nova")["restored"], True)
        self.assertIn("nova", [p.id for p in service.personas()])
        self.assertEqual(service.restore_persona("nova")["restored"], False)
        self.assertEqual(service.store.counts["hidden"], 0)

    def test_removing_your_own_host_deletes_it_and_its_memory(self):
        service = self.service()
        service.save_persona(minimal())
        service.compose("rhea", "filler", FULL, seed=1)
        self.assertTrue(service.memory.recent("rhea", "filler", 3))
        result = service.remove_persona("rhea")
        self.assertEqual((result["removed"], result["hidden"]), (True, False))
        self.assertEqual(service.memory.recent("rhea", "filler", 3), [])
        with self.assertRaisesRegex(HostsError, "no host"):
            service.persona("rhea")
        with self.assertRaisesRegex(HostsError, "no host"):
            service.remove_persona("rhea")
        reopened = self.service()
        with self.assertRaises(HostsError):
            reopened.persona("rhea")

    def test_restore_only_applies_to_built_in_hosts(self):
        service = self.service()
        service.save_persona(minimal())
        for bad in ("rhea", "nobody", 5, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(HostsError, "built-in"):
                service.restore_persona(bad)

    def test_unknown_and_malformed_ids_are_plain_errors(self):
        service = self.service()
        for bad in ("nobody", "", None, 5, ["nova"], {"id": "nova"}, "../../etc/passwd", "NOVA"):
            with self.subTest(bad=bad), self.assertRaisesRegex(HostsError, "no host"):
                service.persona(bad)

    def test_the_number_of_hosts_is_capped(self):
        service = self.service()
        for n in range(MAX_CUSTOM):
            service.save_persona(minimal(name=f"Host number {chr(97 + n // 26)}{chr(97 + n % 26)}"))
        with self.assertRaisesRegex(HostsError, "hosts of your own"):
            service.save_persona(minimal(name="One too many"))
        service.remove_persona("host-number-aa")
        service.save_persona(minimal(name="One too many"))     # room again

    def test_unreadable_saved_rows_are_skipped_and_counted(self):
        service = self.service()
        service.save_persona(minimal())
        conn = self.open()
        conn.executemany("INSERT INTO personas VALUES (?,?,?,?)", [
            ("broken-json", "{not json", 1.0, 1.0), ("wrong-shape", json.dumps([1, 2]), 1.0, 1.0),
            ("nova", json.dumps(minimal(id="nova")), 1.0, 1.0),                 # collides with a built-in
            ("mismatch", json.dumps(minimal(id="other-id")), 1.0, 1.0)])
        conn.commit()
        reopened = self.service()
        self.assertEqual([p.id for p in reopened.personas() if not p.builtin], ["rhea"])
        self.assertEqual(reopened.store.bad_rows, 4)
        self.assertEqual(reopened.status()["unreadable_saved_hosts"], 4)

    def test_a_saved_effect_that_is_no_longer_allowed_is_dropped_on_load(self):
        service = self.service()
        service.save_persona(minimal())
        conn = self.open()
        row = json.loads(conn.execute("SELECT data FROM personas WHERE id='rhea'").fetchone()[0])
        row["allowed_tags"] = ["laughs", "retired-effect"]
        conn.execute("UPDATE personas SET data=? WHERE id='rhea'", (json.dumps(row),))
        conn.commit()
        self.assertEqual(self.service().persona("rhea").allowed_tags, ("laughs",))

    def test_a_database_failure_is_reported_plainly_and_changes_nothing(self):
        service = self.service()
        service.save_persona(minimal())
        service.store._conn.close()
        for action in (lambda: service.save_persona(minimal(name="Other")), lambda: service.remove_persona("rhea"),
                       lambda: service.remove_persona("nova")):
            with self.assertRaises(HostsError) as caught:
                action()
            self.assertIn("disk", str(caught.exception))
            self.assertNotIn("sqlite", str(caught.exception).lower())
        self.assertEqual(service.persona("rhea").name, "Rhea")
        self.assertNotIn("other", [p.id for p in service.personas()])
        self.assertEqual(service.store.counts["hidden"], 0)
        # writing a script still works when only the repetition memory cannot be saved
        self.assertTrue(service.compose("nova", "song_intro", FULL, seed=1).text)
        self.assertGreaterEqual(service.status()["storage_problems"], 2)

    def test_a_database_from_a_newer_version_is_refused(self):
        conn = self.open()
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        with self.assertRaises(Exception) as caught:
            HostsService(conn, library=library())
        self.assertIn("newer", str(caught.exception))

    def test_memory_rows_are_bounded_on_disk(self):
        service = self.service()
        for seed in range(200):
            service.compose("nova", "song_intro", FULL, seed=seed)
        rows = self.open().execute("SELECT COUNT(*) FROM memory WHERE persona='nova' AND kind='song_intro'").fetchone()[0]
        self.assertLessEqual(rows, 30)
        self.assertGreater(rows, 8)

    def test_concurrent_saves_and_scripts_do_not_corrupt_anything(self):
        service = self.service()
        errors = []

        def worker(n):
            try:
                for i in range(15):
                    service.save_persona(minimal(name=f"Voice {n} {chr(97 + i)}"))
                    service.compose("nova", "song_intro", FULL, seed=i + n * 100)
            except Exception as exc:     # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(service.store.counts["custom"], 60)
        reopened = self.service()
        self.assertEqual(reopened.store.counts["custom"], 60)
        self.assertEqual(reopened.store.bad_rows, 0)


class DataFileFailureTests(unittest.TestCase):
    """A damaged or missing shipped data file is reported in plain words, never as a crash."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.real = Path(__file__).resolve().parents[1] / "myradio" / "hosts" / "data"

    def patched(self):
        return patch("importlib.resources.files", lambda package: self.root)

    def test_missing_script_files(self):
        from myradio.hosts.templates import TemplateLibrary
        with self.patched(), self.assertRaisesRegex(HostsError, "Reinstall myradio"):
            TemplateLibrary.from_package()

    def test_a_damaged_script_file(self):
        from myradio.hosts.templates import TemplateLibrary
        (self.root / "data" / "voice-warm.json").write_text("{nope")
        with self.patched(), self.assertRaisesRegex(HostsError, "damaged.*Reinstall myradio"):
            TemplateLibrary.from_package()

    def test_two_script_styles_with_the_same_name(self):
        from myradio.hosts.templates import TemplateLibrary
        text = (self.real / "voice-warm.json").read_text("utf-8")
        (self.root / "data" / "voice-warm.json").write_text(text)
        (self.root / "data" / "voice-warm-copy.json").write_text(text)
        with self.patched(), self.assertRaisesRegex(HostsError, "twice"):
            TemplateLibrary.from_package()

    def test_a_script_file_that_breaks_the_rules_names_the_problem(self):
        from myradio.hosts.templates import TemplateLibrary
        data = json.loads((self.real / "voice-warm.json").read_text("utf-8"))
        data["segments"]["filler"][0] = ["We have twenty-three songs."]
        (self.root / "data" / "voice-warm.json").write_text(json.dumps(data))
        with self.patched(), self.assertRaisesRegex(HostsError, "voice-warm.json filler #1"):
            TemplateLibrary.from_package()

    def test_the_cast_file_problems(self):
        voices = library().voices
        path = self.root / "data" / "personas.json"
        good = json.loads((self.real / "personas.json").read_text("utf-8"))
        cases = {
            "not json": ("{nope", "could not be loaded"),
            "no list": ('{"personas": 5}', "empty or damaged"),
            "empty list": ('{"personas": []}', "empty or damaged"),
            "not an object": (json.dumps({"personas": ["nova"]}), "damaged"),
            "unknown style": (json.dumps({"personas": [dict(good["personas"][0], voice="nope")]}), "nova"),
            "same id twice": (json.dumps({"personas": [good["personas"][0], good["personas"][0]]}), "twice"),
            "marked as real": (json.dumps({"personas": [dict(good["personas"][0], fictional=False)]}), "made-up"),
        }
        for name, (text, expected) in cases.items():
            path.write_text(text)
            with self.subTest(case=name), self.patched(), self.assertRaisesRegex(HostsError, expected):
                load_builtin_personas(voices)

    def test_the_extension_reports_a_damaged_library_instead_of_breaking_the_station(self):
        from test_hosts_support import HostsCase

        class Boot(HostsCase):
            def runTest(self):
                pass

        case = Boot()
        case.setUp = lambda: None
        case.temp = tempfile.TemporaryDirectory()
        self.addCleanup(case.temp.cleanup)
        from test_hosts_support import FakeStation
        case.station = FakeStation()
        message = "The built-in host scripts could not be found. Reinstall myradio to restore them."
        with patch("myradio.hosts.service.TemplateLibrary.builtin", side_effect=HostsError(message)):
            from myradio.extensions import ExtensionHost, StationFacade
            from myradio.hosts.extension import build
            host = ExtensionHost(case.temp.name, StationFacade(case.station))
            host.register(build())
            host.start()
            self.addCleanup(host.stop)
            status = host.status()["hosts"]
            self.assertEqual(status["state"], "failed")
            self.assertIn("Reinstall myradio", status["error"])
            self.assertEqual([c["status"] for c in host.checks() if c["id"] == "hosts.start"], ["fail"])


if __name__ == "__main__":
    unittest.main()
