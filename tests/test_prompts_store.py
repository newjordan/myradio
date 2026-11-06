"""A person's own styles, favourites and repeat memory (the prompt library's SQLite file), plus the
service that composes from all of it. Disposable temporary directories only."""
import copy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from myradio.prompts import PromptError, PromptService, PromptStore, StoreError, load_library
from myradio.prompts import store as store_module
from myradio.prompts.userpresets import UserPresetError, build_user_preset, palette_to_doc

LIBRARY = load_library()
EDIT = {"id": "my-porch", "title": "My porch", "based_on": "porch-blues-slide/slow-tide",
        "palette": {"name": "My Porch Blues", "bpm": [60, 80]}}


def full_spec(slug="own-reels", **changes):
    palette = palette_to_doc(LIBRARY.get_palette("celtic-session-reels"))
    palette.update(name="Own Session Reels", tags=["celtic-inspired", "reel", "dance"])
    palette.update(changes)
    return {"id": slug, "palette": palette, "arrangement": "canon-and-release"}


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prompts.sqlite3"
        self.clock = [1_700_000_000.0]
        self.open_store()

    def open_store(self):
        self.store = PromptStore.open(self.path, clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.service = PromptService(self.store, library=LIBRARY, clock=lambda: self.clock[0])
        return self.service

    def reopen(self):
        self.store.close()
        return self.open_store()


class UserPresetValidationTests(Case):
    def build(self, spec):
        return build_user_preset(spec, library=LIBRARY, resolve=self.service.resolve)

    def test_an_edited_copy_a_full_style_and_a_borrowed_arrangement_are_all_accepted(self):
        record, warnings = self.build(EDIT)
        self.assertEqual((record.id, record.title, record.based_on), ("user:my-porch", "My porch", "porch-blues-slide/slow-tide"))
        self.assertEqual(record.palette["name"], "My Porch Blues")
        self.assertEqual(record.palette["bpm"], [60, 80])
        self.assertEqual(record.palette["id"], "my-porch")
        self.assertEqual(record.arrangement["id"], "slow-tide", "an untouched arrangement keeps its own id")
        self.assertEqual(warnings, [])
        full, _ = self.build(full_spec())
        self.assertEqual((full.palette["id"], full.arrangement["id"]), ("own-reels", "canon-and-release"))
        copy_only, _ = self.build({"id": "plain-copy", "based_on": "neo-soul/rondo-path"})
        self.assertEqual(copy_only.palette["id"], "neo-soul", "a copy with no changes stays the built-in style")
        self.assertEqual(copy_only.title, LIBRARY.get("neo-soul/rondo-path").title)

    def test_the_stored_copy_is_a_snapshot_not_a_reference(self):
        record, _ = self.build(EDIT)
        again, _ = self.build(copy.deepcopy(EDIT))
        self.assertEqual(record.digest, again.digest)
        changed = copy.deepcopy(EDIT)
        changed["palette"]["bpm"] = [62, 80]
        self.assertNotEqual(self.build(changed)[0].digest, record.digest)

    def test_the_same_schema_and_rules_as_the_catalog_apply_with_plain_messages(self):
        cases = [
            (None, "Send the style as an object"),
            ({**EDIT, "colour": "red"}, "not recognised"),
            ({**EDIT, "id": "Bad Id"}, "lowercase letters, numbers and hyphens"),
            ({**EDIT, "id": ""}, "lowercase letters, numbers and hyphens"),
            ({**EDIT, "id": "x" * 64}, "lowercase letters, numbers and hyphens"),
            ({**EDIT, "id": "-lead"}, "lowercase letters, numbers and hyphens"),
            ({**EDIT, "title": ""}, "title must be some text"),
            ({**EDIT, "title": "x" * 101}, "title must be some text"),
            ({**EDIT, "title": " padded"}, "title must be some text"),
            ({**EDIT, "based_on": "nothing/here"}, "is not in the library"),
            ({**EDIT, "based_on": 5}, "id of a style to copy"),
            ({**EDIT, "palette": "text"}, "palette must be an object"),
            ({**EDIT, "palette": {"banjo": 1}}, "not recognised"),
            ({**EDIT, "palette": {"tags": ["Late Night"]}}, "lowercase words joined by hyphens"),
            ({**EDIT, "palette": {"bpm": [90, 60]}}, "two whole numbers between 30 and 240"),
            ({**EDIT, "palette": {"energy": "loud"}}, "calm, steady or driving"),
            ({**EDIT, "palette": {"moods": ["same", "same"]}}, "same entry twice"),
            ({**EDIT, "palette": {"name": "x" * 101}}, "style name needs some text of at most 100 characters"),
            ({**EDIT, "palette": {"bass": " padded"}}, "spaces at the start or end"),
            ({**EDIT, "arrangement": "nope"}, "is not in the library"),
            ({**EDIT, "arrangement": 7}, "Choose an arrangement"),
            ({"id": "fresh", "arrangement": "slow-tide"}, "needs a palette"),
            ({"id": "fresh", "palette": {"name": "Only a name"}, "arrangement": "slow-tide"}, "missing"),
            ({"id": "fresh", "palette": full_spec()["palette"]}, "Choose an arrangement"),
            ({"id": "fresh", "palette": full_spec()["palette"], "arrangement": {"name": "Half"}}, "arrangement"),
        ]
        for spec, expected in cases:
            with self.subTest(expected=expected, spec=str(spec)[:60]):
                with self.assertRaises(UserPresetError) as caught:
                    self.build(spec)
                message = str(caught.exception)
                self.assertIn(expected, message)
                for jargon in ("palette[", "palettes[", "CatalogError", "Traceback", "NoneType", "unknown fields ["):
                    self.assertNotIn(jargon, message)

    def test_compatibility_errors_block_saving_and_explain_how_to_fix_it(self):
        broken = {**EDIT, "palette": {"meter": "7/8 (2+2+2)"}}
        with self.assertRaises(UserPresetError) as caught:
            self.build(broken)
        self.assertIn("cannot be saved yet", str(caught.exception))
        self.assertTrue(any(f.rule == "meter.grouping-sum" for f in caught.exception.findings))
        mixed = full_spec(leads=["sitar with sliding bends", "wooden flute with rolls"])
        with self.assertRaises(UserPresetError) as caught:
            self.build(mixed)
        self.assertIn("fusion", str(caught.exception))
        fusion = full_spec(leads=["sitar with sliding bends", "wooden flute with rolls"], tags=["celtic-inspired", "reel", "fusion"])
        self.assertEqual(self.build(fusion)[1], [])
        quiet = full_spec(supports=["a mystery contraption", "bodhran with tipper accents"])
        record, warnings = self.build(quiet)
        self.assertEqual([w.rule for w in warnings], ["instrument.unrecognized"])

    def test_a_hostile_nest_or_flood_is_refused_politely(self):
        deep = [[]]
        for _ in range(5000):
            deep = [deep]
        for spec in ({"id": "x-y", "based_on": "neo-soul/rondo-path", "palette": {"moods": deep}},
                     {"id": "x-y", "based_on": "neo-soul/rondo-path", "palette": {"moods": ["m" + str(i) for i in range(4500)]}}):
            with self.assertRaisesRegex(UserPresetError, "nested too deeply or has too many parts"):
                self.build(spec)

    def test_a_very_long_style_is_refused(self):
        huge = full_spec(motifs=[("a motif " + "x" * 1500) + str(i) for i in range(30)])
        with self.assertRaises(UserPresetError) as caught:
            self.build(huge)
        self.assertIn("too long", str(caught.exception))


class SavedStyleTests(Case):
    def test_save_list_compose_and_remove(self):
        saved = self.service.save_custom(EDIT)
        self.assertEqual((saved["id"], saved["revision"], saved["created"]), ("user:my-porch", 1, True))
        self.assertEqual([p.id for p in self.store.user_presets()], ["user:my-porch"])
        listed = self.service.catalog(source="user")
        self.assertEqual((listed["total"], listed["items"][0]["source"], listed["items"][0]["title"]), (1, "user", "My porch"))
        result = self.service.compose({"preset": "user:my-porch", "seed": 4, "remember": False})
        self.assertEqual(result["id"], "user:my-porch")
        self.assertEqual(result["provenance"]["user_preset"], {"id": "user:my-porch", "revision": 1, "digest": saved["digest"]})
        self.assertEqual(result["prompt"]["title"], "My porch")
        self.assertIn("My Porch Blues", result["prompt"]["style"])
        self.assertTrue(60 <= result["prompt"]["bpm"] <= 80)
        self.assertEqual(self.service.remove_custom("user:my-porch"), {"id": "user:my-porch", "removed": True})
        self.assertEqual(self.store.user_presets(), ())
        with self.assertRaisesRegex(PromptError, "no saved style"):
            self.service.remove_custom("user:my-porch")
        with self.assertRaisesRegex(PromptError, "no style called"):
            self.service.compose({"preset": "user:my-porch"})

    def test_built_in_styles_cannot_be_removed_and_user_ids_are_a_separate_namespace(self):
        for bad in ("neo-soul/rondo-path", "neo-soul", "", "user", None, 5):
            with self.subTest(bad=bad), self.assertRaises(PromptError):
                self.service.remove_custom(bad)
        self.assertEqual(len(LIBRARY.presets), 1278)
        self.service.save_custom({**EDIT, "id": "neo-soul"})  # same slug as a built-in style: still its own id
        self.assertEqual(self.service.resolve("user:neo-soul").id, "user:neo-soul")
        self.assertEqual(self.service.resolve("neo-soul/rondo-path").id, "neo-soul/rondo-path")

    def test_saved_styles_survive_a_restart_unchanged(self):
        saved = self.service.save_custom(EDIT)
        self.service.save_custom(full_spec())
        before = self.service.compose({"preset": "user:my-porch", "seed": 11, "remember": False})
        service = self.reopen()
        self.assertEqual([p.id for p in self.store.user_presets()], ["user:my-porch", "user:own-reels"])
        after = service.compose({"preset": "user:my-porch", "seed": 11, "remember": False})
        self.assertEqual(before["prompt"], after["prompt"])
        self.assertEqual(before["prompt"]["fingerprint"], after["prompt"]["fingerprint"])
        self.assertEqual(self.store.user_row("user:my-porch")["digest"], saved["digest"])
        mode = oct(self.path.stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600")

    def test_seeded_reproduction_depends_on_content_not_on_when_it_was_saved(self):
        self.service.save_custom(EDIT)
        first = self.service.compose({"preset": "user:my-porch", "seed": 5, "remember": False})
        self.clock[0] += 1000
        self.service.remove_custom("user:my-porch")
        self.service.save_custom(EDIT)  # deleted and written again: same content, same prompt
        again = self.service.compose({"preset": "user:my-porch", "seed": 5, "remember": False})
        self.assertEqual(first["prompt"]["fingerprint"], again["prompt"]["fingerprint"])
        other = self.service.compose({"preset": "user:my-porch", "seed": 6, "remember": False})
        self.assertNotEqual(first["prompt"]["style"], other["prompt"]["style"])
        edited = copy.deepcopy(EDIT)
        edited["palette"]["bpm"] = [70, 90]
        self.service.save_custom(edited)
        changed = self.service.compose({"preset": "user:my-porch", "seed": 5, "remember": False})
        self.assertNotEqual(first["prompt"]["fingerprint"], changed["prompt"]["fingerprint"])

    def test_the_random_choices_are_keyed_by_the_content_digest(self):
        self.service.save_custom(EDIT)

        def picks():
            found = []
            for seed in range(12):
                prompt = self.service.compose({"preset": "user:my-porch", "seed": seed, "remember": False})["prompt"]
                lines = prompt["style"].splitlines()
                found.append((prompt["bpm"], lines[0].split(";")[-1], lines[1]))
            return found

        before = picks()
        edited = copy.deepcopy(EDIT)
        edited["palette"]["production"] = "Only the production note changes, not the tempo, moods or instruments."
        self.service.save_custom(edited)
        after = picks()
        self.assertNotEqual(before, after, "a content change must start a new random sequence, not just edit one line")
        self.service.save_custom(EDIT)  # back to the first content: back to the first picks
        self.assertEqual(picks(), before)

    def test_revision_counts_content_changes_only(self):
        self.assertEqual(self.service.save_custom(EDIT)["revision"], 1)
        again = self.service.save_custom({**EDIT, "title": "Renamed"})
        self.assertEqual((again["revision"], again["created"]), (1, False), "a new title is not new music")
        edited = copy.deepcopy(EDIT)
        edited["palette"]["bpm"] = [66, 82]
        self.assertEqual(self.service.save_custom(edited)["revision"], 2)
        self.assertEqual(self.store.user_row("user:my-porch")["title"], "My porch")  # edited spec kept the original title
        self.assertEqual(self.store.user_row("user:my-porch")["created"], 1_700_000_000.0)

    def test_a_copy_of_a_heritage_style_keeps_its_note_and_other_copies_have_none(self):
        self.service.save_custom({"id": "my-kora", "based_on": "kora-river-ostinato/slow-tide", "palette": {"name": "My Kora Night", "bpm": [70, 90]}})
        self.service.save_custom({"id": "my-tango", "based_on": "tango-nuevo/slow-tide", "palette": {"name": "My Tango"}})  # no tag, but an old heritage style
        self.service.save_custom(EDIT)
        notes = {i["id"]: i["heritage_note"] for i in self.service.catalog(source="user", view="presets", limit=100)["items"]}
        self.assertIn("not a claim of authenticity", notes["user:my-kora"])
        self.assertIn("not a claim of authenticity", notes["user:my-tango"])
        self.assertIsNone(notes["user:my-porch"])
        styles = {i["id"]: i["heritage_note"] for i in self.service.catalog(source="user", view="styles", limit=100)["items"]}
        self.assertEqual({k: bool(v) for k, v in styles.items()}, {"user:my-kora": True, "user:my-tango": True, "user:my-porch": False})

    def test_a_saved_style_can_be_copied_by_another_saved_style(self):
        self.service.save_custom(EDIT)
        child = self.service.save_custom({"id": "my-porch-fast", "based_on": "user:my-porch", "palette": {"bpm": [80, 100], "energy": "steady"}})
        self.assertEqual((child["based_on"], child["revision"]), ("user:my-porch", 1))
        self.assertEqual(self.store.get_user_preset("user:my-porch-fast").palette.bpm, (80, 100))

    def test_the_number_of_saved_styles_is_bounded(self):
        with mock.patch.object(store_module, "MAX_USER_PRESETS", 3):
            for index in range(3):
                self.service.save_custom({**EDIT, "id": f"porch-{index}"})
            with self.assertRaisesRegex(PromptError, "3 saved styles, which is the most"):
                self.service.save_custom({**EDIT, "id": "porch-extra"})
            self.service.save_custom({**EDIT, "id": "porch-1", "title": "Replaced in place"})  # replacing is always allowed
            self.assertEqual(len(self.store.user_presets()), 3)

    def test_a_damaged_row_is_set_aside_and_never_deleted(self):
        self.service.save_custom(EDIT)
        self.service.save_custom(full_spec())
        self.store.close()
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE user_presets SET palette='{not json' WHERE slug='own-reels'")
        raw.commit()
        raw.close()
        service = self.open_store()
        self.assertEqual([p.id for p in self.store.user_presets()], ["user:my-porch"])
        self.assertEqual(self.store.damaged, ("own-reels",))
        self.assertEqual(service.snapshot()["damaged"], ["own-reels"])
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM user_presets").fetchone()[0], 2)
        raw.close()
        tampered = sqlite3.connect(self.path)
        tampered.execute("UPDATE user_presets SET digest='0000000000000000' WHERE slug='my-porch'")
        tampered.commit()
        tampered.close()
        self.reopen()
        self.assertEqual(set(self.store.damaged), {"own-reels", "my-porch"}, "content that does not match its digest is not trusted")

    def test_a_newer_file_is_refused_with_a_plain_message(self):
        self.store.close()
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA user_version=99")
        raw.commit()
        raw.close()
        with self.assertRaisesRegex(StoreError, "newer myradio"):
            PromptStore.open(self.path)

    def test_a_symbolic_link_is_not_followed(self):
        link = Path(self.temp.name) / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(StoreError, "symbolic link"):
            PromptStore.open(link)


class FavoriteTests(Case):
    def test_styles_presets_and_saved_styles_can_be_favourites(self):
        self.service.save_custom(EDIT)
        self.assertEqual(self.service.favorite("warm-brush-jazz", True), {"id": "warm-brush-jazz", "favorite": True, "kind": "style"})
        self.assertEqual(self.service.favorite("neo-soul/rondo-path", True)["kind"], "preset")
        self.assertEqual(self.service.favorite("user:my-porch", True)["kind"], "preset")
        self.assertEqual(self.store.favorites(), {"warm-brush-jazz": "style", "neo-soul/rondo-path": "preset", "user:my-porch": "preset"})
        self.assertEqual(self.service.favorite("warm-brush-jazz", True)["favorite"], True)  # again: still on, no error
        self.assertEqual(self.service.favorite("warm-brush-jazz", False)["favorite"], False)
        self.assertEqual(self.service.favorite("warm-brush-jazz", False)["favorite"], False)  # off twice is fine
        self.assertNotIn("warm-brush-jazz", self.store.favorites())

    def test_unknown_ids_get_a_helpful_message(self):
        for bad, text in (("nothing", "no style called"), ("neo-soul/nothing", "no preset called"), ("user:ghost", "no saved style"),
                          ("", "Say which"), ("  ", "Say which"), (5, "Say which")):
            with self.subTest(bad=bad), self.assertRaisesRegex(PromptError, text):
                self.service.favorite(bad, True)

    def test_favourites_show_up_in_the_catalog_and_filter_it(self):
        self.service.favorite("warm-brush-jazz", True)
        self.service.favorite("neo-soul/rondo-path", True)
        styles = self.service.catalog(view="styles", favorites=True)
        self.assertEqual([i["id"] for i in styles["items"]], ["warm-brush-jazz"])
        presets = self.service.catalog(view="presets", favorites=True, limit=100)
        self.assertEqual(presets["total"], 19)  # all 18 presets of the favourite style, plus the favourite preset
        flagged = {i["id"]: (i["favorite"], i["style_favorite"]) for i in presets["items"]}
        self.assertEqual(flagged["neo-soul/rondo-path"], (True, False))
        self.assertEqual(flagged["warm-brush-jazz/slow-tide"], (False, True))
        self.assertTrue(self.service.catalog(view="styles", query="warm brush")["items"][0]["favorite"])

    def test_removing_a_saved_style_also_clears_its_favourite_mark(self):
        self.service.save_custom(EDIT)
        self.service.favorite("user:my-porch", True)
        self.service.remove_custom("user:my-porch")
        self.assertEqual(self.store.favorites(), {})

    def test_favourites_persist_and_are_bounded(self):
        self.service.favorite("neo-soul", True)
        service = self.reopen()
        self.assertEqual(self.store.favorites(), {"neo-soul": "style"})
        with mock.patch.object(store_module, "MAX_FAVORITES", 2):
            service.favorite("deep-house", True)
            with self.assertRaisesRegex(PromptError, "2 favourites, which is the most"):
                service.favorite("dub-techno", True)
            service.favorite("neo-soul", False)
            service.favorite("dub-techno", True)

    def test_favorites_only_composes_from_favourites_and_says_so_when_there_are_none(self):
        with self.assertRaisesRegex(PromptError, "No styles match"):
            self.service.compose({"seed": 1, "favorites_only": True})
        self.service.favorite("ambient-garden", True)
        for seed in range(12):
            result = self.service.compose({"seed": seed, "favorites_only": True, "remember": False})
            self.assertTrue(result["id"].startswith("ambient-garden/"))


class RepeatMemoryTests(Case):
    def test_choice_without_memory_is_exactly_the_librarys_own_seeded_choice(self):
        for seed in (0, 5, 77, 123456):
            for filters in ({}, {"tags": ["jazz"]}, {"family": "celtic"}, {"energy": "driving"}):
                with self.subTest(seed=seed, filters=filters):
                    result = self.service.compose({"seed": seed, "avoid_repeats": False, "include_user": False, "remember": False, **filters})
                    self.assertEqual(result["id"], LIBRARY.choose(seed, **filters).id)
                    # and with an empty memory the same answer comes back even with avoidance on
                    fresh = self.service.compose({"seed": seed, "include_user": False, "remember": False, **filters})
                    self.assertEqual(fresh["id"], result["id"])

    def test_a_pool_is_walked_without_repeats_and_filters_are_never_relaxed(self):
        picked = []
        for seed in range(18):
            result = self.service.compose({"seed": seed, "tags": ["bossa"]})
            picked.append(result["id"])
            self.assertTrue(result["id"].startswith("bossa-courtyard/"), "the tag filter is never relaxed")
        self.assertEqual(len(set(picked)), 18, "no preset repeats until every one has been used")
        later = [self.service.compose({"seed": 100 + n, "tags": ["bossa"]}) for n in range(5)]
        for result in later:
            self.assertTrue(result["id"].startswith("bossa-courtyard/"))
            self.assertIn(result["selection"]["relaxed"], ("older-presets", "last-preset", "everything", "styles"))
        recent_ten = picked[-10:]
        self.assertNotIn(later[0]["id"], recent_ten, "after the pool is used up the last ten are still avoided")

    def test_a_style_is_not_repeated_inside_the_window_the_adventurousness_slider_sets(self):
        sliders = {"energy": 50, "adventurousness": 0, "vocals": 0}   # familiar: avoid the last 2 styles
        styles = ["warm-brush-jazz", "ambient-garden", "bossa-courtyard"]
        order = [self.service.compose({"seed": n, "styles": styles, "sliders": sliders, "energy": "calm"})["id"].split("/")[0] for n in range(30)]
        for index in range(len(order) - 2):
            self.assertEqual(len(set(order[index:index + 3])), 3, order[index:index + 3])
        wide = {"energy": 50, "adventurousness": 100, "vocals": 0}    # surprising: avoid the last 12 styles
        taken = [self.service.compose({"seed": 500 + n, "tags": ["acoustic"], "sliders": wide, "energy": "steady"})["id"].split("/")[0] for n in range(12)]
        self.assertEqual(len(set(taken)), 12)

    def test_the_memory_survives_a_restart(self):
        first = self.service.compose({"seed": 3, "styles": ["warm-brush-jazz", "ambient-garden"], "sliders": {"energy": 20, "adventurousness": 0, "vocals": 0}})
        service = self.reopen()
        second = service.compose({"seed": 3, "styles": ["warm-brush-jazz", "ambient-garden"], "sliders": {"energy": 20, "adventurousness": 0, "vocals": 0}})
        self.assertNotEqual(first["id"].split("/")[0], second["id"].split("/")[0])
        self.assertEqual(self.store.history_count(), 2)

    def test_previews_do_not_leave_a_trace_and_exact_requests_are_remembered(self):
        self.service.compose({"seed": 1, "remember": False})
        self.assertEqual(self.store.history_count(), 0)
        self.assertFalse(self.service.compose({"seed": 1, "remember": False})["remembered"])
        exact = self.service.compose({"preset": "neo-soul/rondo-path", "seed": 1})
        self.assertTrue(exact["remembered"])
        self.assertEqual(exact["selection"], {"method": "exact", "candidates": 1, "relaxed": None, "avoided": {}})
        self.assertEqual(self.store.recent(1)[0]["preset_id"], "neo-soul/rondo-path")

    def test_the_memory_keeps_only_the_most_recent_prompts(self):
        with mock.patch.object(store_module, "MAX_HISTORY", 5):
            for seed in range(9):
                self.service.compose({"seed": seed, "avoid_repeats": False})
            rows = self.store.recent(50)
            self.assertEqual(len(rows), 5)
            self.assertEqual([r["seed"] for r in rows], [8, 7, 6, 5, 4])
            self.assertEqual(self.store.history_count(), 5)
        self.assertEqual(self.store.forget_history(), 5)
        self.assertEqual(self.store.history_count(), 0)

    def test_history_rows_describe_what_was_composed(self):
        result = self.service.compose({"seed": 2, "sliders": {"energy": 80, "adventurousness": 90, "vocals": 0}})
        row = self.store.recent(1)[0]
        self.assertEqual((row["preset_id"], row["seed"], row["complexity"], row["density"], row["mode"]),
                         (result["id"], 2, "intricate", "lush", "instrumental"))
        self.assertEqual(row["fingerprint"], result["prompt"]["fingerprint"])
        self.assertEqual(row["style_key"], result["id"].split("/")[0])
        self.assertEqual(row["at"], 1_700_000_000.0)

    def test_concurrent_requests_all_get_an_answer_and_are_all_remembered(self):
        errors, results = [], []

        def worker(base):
            try:
                for n in range(25):
                    results.append(self.service.compose({"seed": base * 100 + n, "tags": ["acoustic"]})["id"])
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 200)
        self.assertEqual(self.store.history_count(), 200)
        self.assertEqual(self.store.recent(1000)[-1]["seq"], 1)


class StoreFailureTests(Case):
    def test_a_database_error_becomes_a_plain_message_and_nothing_changes(self):
        self.store._conn.close()
        for call in (lambda: self.service.save_custom(EDIT), lambda: self.service.favorite("neo-soul", True),
                     lambda: self.store.forget_history()):
            with self.assertRaises((PromptError, StoreError)) as caught:
                call()
            self.assertIn("Nothing was changed", str(caught.exception))
            self.assertNotIn("sqlite", str(caught.exception).lower())
        result = self.service.compose({"seed": 3})  # composing still works; only the memory update is lost
        self.assertFalse(result["remembered"])
        self.assertTrue(any("repeat memory" in w for w in result["warnings"]))

    def test_without_a_store_the_built_in_library_still_works_and_changes_say_why_they_failed(self):
        service = PromptService(None, library=LIBRARY)
        self.assertEqual(service.compose({"seed": 1})["remembered"], False)
        self.assertEqual(service.catalog(source="user")["total"], 0)
        for call in (lambda: service.save_custom(EDIT), lambda: service.favorite("neo-soul", True), lambda: service.remove_custom("user:x")):
            with self.assertRaisesRegex(PromptError, "Saved styles are not available"):
                call()
        self.assertFalse(service.snapshot()["store_available"])


if __name__ == "__main__":
    unittest.main()
