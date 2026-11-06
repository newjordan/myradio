"""Browsing, searching and composing through PromptService: paging, filters, validation and wording."""
import tempfile
import unittest
from pathlib import Path

from myradio.prompts import PromptError, PromptService, PromptStore, load_library, replay

LIBRARY = load_library()


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = PromptStore.open(Path(cls.temp.name) / "prompts.sqlite3")
        cls.service = PromptService(cls.store, library=LIBRARY, seed_source=lambda: 4242)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.temp.cleanup()


class CatalogTests(Case):
    def test_paging_walks_the_whole_catalog_exactly_once(self):
        seen, offset, pages = [], 0, 0
        while offset is not None:
            page = self.service.catalog(offset=offset, limit=100)
            seen.extend(item["id"] for item in page["items"])
            self.assertEqual((page["total"], page["limit"], page["view"], page["catalog_version"]), (1278, 100, "presets", "1.1.0"))
            offset, pages = page["next_offset"], pages + 1
        self.assertEqual((pages, len(seen), len(set(seen))), (13, 1278, 1278))
        self.assertEqual(seen, [p.id for p in LIBRARY.presets])
        last = self.service.catalog(offset=1272, limit=24)
        self.assertEqual((len(last["items"]), last["next_offset"]), (6, None))
        beyond = self.service.catalog(offset=5000)
        self.assertEqual((beyond["items"], beyond["next_offset"], beyond["total"]), ([], None, 1278))

    def test_filters_combine_with_the_librarys_rules(self):
        library_match = {p.id for p in LIBRARY.browse(family="jazz", tags=["acoustic"], energy="calm", exclude_tags=["waltz"])}
        page = self.service.catalog(family="jazz", tags=["acoustic"], energy="calm", exclude_tags=["waltz"], limit=100)
        self.assertEqual({i["id"] for i in page["items"]}, library_match)
        self.assertEqual(page["total"], len(library_match))
        for item in page["items"]:
            self.assertEqual((item["family"], item["energy"]), ("jazz", "calm"))
            self.assertIn("acoustic", item["tags"])
        self.assertEqual(self.service.catalog(tags=["jazz", "no-such-tag"])["total"], 0)
        self.assertEqual(self.service.catalog(query="flute", limit=100)["total"], len(LIBRARY.browse(query="flute")))
        self.assertEqual(self.service.catalog(family="JAZZ")["total"], self.service.catalog(family="jazz")["total"])

    def test_each_view_has_its_own_shape(self):
        styles = self.service.catalog(view="styles", limit=100)
        self.assertEqual(styles["total"], 71)
        first = styles["items"][0]
        self.assertEqual(first["id"], "warm-brush-jazz")
        for key in ("name", "family", "tags", "energy", "bpm", "meter", "moods", "leads", "supports", "bass", "source", "favorite",
                    "presets", "introduced", "heritage_note"):
            self.assertIn(key, first)
        self.assertEqual({i["presets"] for i in styles["items"]}, {18})
        self.assertEqual({i["introduced"] for i in styles["items"]}, {"1.0.0", "1.1.0"})
        arrangements = self.service.catalog(view="arrangements", limit=100)
        self.assertEqual((arrangements["total"], arrangements["items"][0]["id"]), (18, "theme-and-return"))
        self.assertEqual(arrangements["items"][0]["sections"], {"simple": 3, "layered": 4, "intricate": 5})
        preset = self.service.catalog(limit=1)["items"][0]
        self.assertEqual(preset["style"], {"id": "warm-brush-jazz", "name": "Warm Brush Jazz"})
        self.assertEqual(preset["arrangement"]["id"], "theme-and-return")
        self.assertEqual(preset["source"], "builtin")

    def test_sorting_and_sources(self):
        by_name = [i["name"] for i in self.service.catalog(view="styles", sort="name", limit=100)["items"]]
        self.assertEqual(by_name, sorted(by_name, key=str.casefold))
        self.assertEqual(self.service.catalog(source="user")["total"], 0)
        self.assertEqual(self.service.catalog(source="builtin")["total"], 1278)
        self.assertEqual(self.service.catalog(view="arrangements", source="user")["total"], 0)

    def test_traditions_say_they_are_a_direction_not_a_claim(self):
        styles = {i["id"]: i for i in self.service.catalog(view="styles", limit=100)["items"]}
        for name in ("tango-nuevo", "kora-river-ostinato", "sunset-city-pop"):
            self.assertIn("not a claim of authenticity", styles[name]["heritage_note"])
        self.assertIsNone(styles["warm-brush-jazz"]["heritage_note"])
        self.assertIsNone(styles["chiptune-quest"]["heritage_note"])

    def test_facets_describe_the_whole_catalog_for_filter_chips(self):
        facets = self.service.catalog(limit=1)["facets"]
        self.assertEqual(sum(f["count"] for f in facets["families"]), 71)
        from collections import Counter
        self.assertEqual({f["id"]: f["count"] for f in facets["energy"]}, dict(Counter(p.energy for p in LIBRARY.palettes)))
        self.assertEqual(sum(f["count"] for f in facets["energy"]), 71)
        self.assertLessEqual(len(facets["tags"]), 40)
        self.assertEqual(facets["tags"][0]["id"], "acoustic")

    def test_bad_requests_get_plain_answers(self):
        cases = [
            ({"view": "songs"}, "view must be"), ({"source": "cloud"}, "source must be"), ({"sort": "random"}, "sort must be"),
            ({"energy": "loud"}, "energy must be one of"), ({"limit": 0}, "limit must be a whole number from 1 to 100"),
            ({"limit": 101}, "limit must be a whole number from 1 to 100"), ({"limit": True}, "limit must be"),
            ({"offset": -1}, "offset must be"), ({"offset": 10**9}, "offset must be"), ({"query": "x" * 101}, "at most 100 characters"),
            ({"query": "a b c d e f g h i"}, "at most 8 search words"), ({"tags": "jazz"}, "filters were not understood"),
            ({"tags": [None]}, "filters were not understood"), ({"family": ""}, "filters were not understood"),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(PromptError, expected):
                self.service.catalog(**kwargs)


class SearchTests(Case):
    def test_a_name_match_ranks_above_a_match_buried_in_the_details(self):
        found = self.service.search("jazz")
        self.assertEqual(found["query"], "jazz")
        top = [i["id"] for i in found["styles"][:3]]
        self.assertTrue(all("jazz" in name for name in top), top)
        self.assertEqual(found["styles"][0]["matched"][0], "name")
        self.assertTrue(found["total"] >= len(found["styles"]))

    def test_every_word_must_match_and_instruments_are_searchable(self):
        flute = self.service.search("flute", limit=25)
        self.assertGreaterEqual(len(flute["styles"]), 5)
        self.assertTrue(all("instruments" in i["matched"] or "mood" in i["matched"] or "details" in i["matched"] or "tags" in i["matched"]
                            or "name" in i["matched"] for i in flute["styles"]))
        both = self.service.search("flute celtic")
        self.assertTrue(both["styles"] and all(i["family"] == "celtic" or "celtic" in " ".join(i["tags"]) for i in both["styles"]))
        self.assertEqual(self.service.search("zzzzzz")["styles"], [])
        self.assertEqual(self.service.search("zzzzzz")["total"], 0)

    def test_arrangements_are_found_by_name(self):
        found = self.service.search("canon")
        self.assertEqual(found["arrangements"][0]["id"], "canon-and-release")

    def test_limits_and_empty_queries(self):
        self.assertLessEqual(len(self.service.search("a", limit=3)["styles"]), 3)
        for bad in ("", "   ", None):
            with self.assertRaisesRegex(PromptError, "Type a word or two"):
                self.service.search(bad)
        for limit in (0, 26, "5"):
            with self.assertRaisesRegex(PromptError, "limit must be"):
                self.service.search("jazz", limit=limit)

    def test_a_search_never_changes_anything(self):
        before = self.store.counts()
        self.service.search("jazz")
        self.service.catalog(query="jazz")
        self.assertEqual(self.store.counts(), before)


class ComposeRequestTests(Case):
    def test_the_answer_has_everything_needed_for_provenance(self):
        result = self.service.compose({"seed": 12, "remember": False, "tags": ["jazz"]})
        self.assertEqual(set(result), {"id", "prompt", "lyrics", "selection", "treatment", "provenance", "warnings", "remembered"})
        prompt = result["prompt"]
        self.assertEqual(result["id"], prompt["preset_id"])
        self.assertEqual(result["provenance"]["fingerprint"], prompt["fingerprint"])
        self.assertEqual((prompt["kind"], prompt["speech"], prompt["target_model"]), ("music", False, "Frosty40/YuE2-Turbo-Radio"))
        self.assertEqual(result["provenance"]["seed"], 12)
        self.assertRegex(result["provenance"]["composed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertIn(result["id"], {p.id for p in LIBRARY.browse(tags=["jazz"])})
        self.assertEqual(replay(prompt).fingerprint, prompt["fingerprint"])
        self.assertEqual(self.service.replay(prompt)["fingerprint"], prompt["fingerprint"])

    def test_the_same_request_gives_the_same_answer_and_a_missing_seed_is_drawn_and_reported(self):
        request = {"seed": 99, "sliders": {"energy": 30, "adventurousness": 60, "vocals": 40}, "remember": False}
        self.assertEqual(self.service.compose(request), self.service.compose(request))
        drawn = self.service.compose({"remember": False})
        self.assertEqual(drawn["provenance"]["seed"], 4242)
        self.assertEqual(drawn["prompt"], self.service.compose({"seed": 4242, "remember": False})["prompt"])

    def test_sliders_choose_the_treatment_and_explicit_settings_beat_them(self):
        sliders = {"energy": 90, "adventurousness": 10, "vocals": 0}
        result = self.service.compose({"seed": 1, "sliders": sliders, "remember": False})
        self.assertEqual(result["treatment"]["complexity"], "simple")
        self.assertEqual((result["treatment"]["density"], result["treatment"]["mode"], result["treatment"]["energy"]), ("lush", "instrumental", "driving"))
        self.assertEqual(LIBRARY.get(result["id"]).palette.energy, "driving")
        self.assertTrue(result["treatment"]["from_sliders"])
        self.assertIn("Lively and full", result["treatment"]["summary"])
        over = self.service.compose({"seed": 1, "sliders": sliders, "complexity": "intricate", "density": "sparse", "energy": "calm",
                                     "mode": "vocal", "remember": False})
        self.assertEqual((over["treatment"]["complexity"], over["treatment"]["density"], over["treatment"]["mode"]), ("intricate", "sparse", "vocal"))
        self.assertEqual(LIBRARY.get(over["id"]).palette.energy, "calm")

    def test_the_energy_slider_is_a_preference_and_an_explicit_setting_is_a_constraint(self):
        sliders = {"energy": 100, "adventurousness": 50, "vocals": 0}
        result = self.service.compose({"seed": 1, "styles": ["warm-brush-jazz"], "sliders": sliders, "remember": False, "avoid_repeats": False})
        self.assertTrue(result["id"].startswith("warm-brush-jazz/"), "the chosen style is kept")
        self.assertEqual((result["selection"]["relaxed"], result["treatment"]["energy"], result["treatment"]["density"]), ("slider-energy", None, "lush"))
        fits = self.service.compose({"seed": 1, "styles": ["pocket-funk"], "sliders": sliders, "remember": False, "avoid_repeats": False})
        self.assertEqual((fits["selection"]["relaxed"], fits["treatment"]["energy"]), (None, "driving"))
        with self.assertRaisesRegex(PromptError, "No styles match"):
            self.service.compose({"seed": 1, "styles": ["warm-brush-jazz"], "energy": "driving", "remember": False})
        with self.assertRaisesRegex(PromptError, "No styles match"):
            self.service.compose({"seed": 1, "tags": ["no-such-tag"], "sliders": sliders, "remember": False})

    def test_error_messages_do_not_grow_with_the_input(self):
        with self.assertRaises(PromptError) as caught:
            self.service.compose({"preset": "x" * 5000})
        self.assertLess(len(str(caught.exception)), 200)

    def test_naming_a_preset_ignores_the_choosing_filters_but_not_the_treatment(self):
        result = self.service.compose({"preset": "neo-soul/rondo-path", "seed": 4, "tags": ["acoustic"], "energy": "driving",
                                       "complexity": "intricate", "remember": False})
        self.assertEqual(result["id"], "neo-soul/rondo-path")
        self.assertEqual(result["prompt"]["complexity"], "intricate")

    def test_excluded_presets_and_styles_are_respected(self):
        pool = [p.id for p in LIBRARY.browse(tags=["bossa"])]
        result = self.service.compose({"seed": 1, "tags": ["bossa"], "exclude_presets": pool[:-1], "avoid_repeats": False, "remember": False})
        self.assertEqual(result["id"], pool[-1])
        only = self.service.compose({"seed": 1, "styles": ["Kora-River-Ostinato"], "avoid_repeats": False, "remember": False})
        self.assertTrue(only["id"].startswith("kora-river-ostinato/"))

    def test_bad_requests_get_plain_answers_and_change_nothing(self):
        before = self.store.counts()
        cases = [
            ("not an object", "Send the settings as an object"), ({"colour": 1}, "not recognised"),
            ({"seed": -1}, "seed must be a whole number"), ({"seed": True}, "seed must be"), ({"seed": 2**63}, "seed must be"),
            ({"seed": 1.5}, "seed must be"), ({"seed": "7"}, "seed must be"),
            ({"sliders": {"energy": 5}}, "all three sliders"), ({"sliders": {"energy": 5, "adventurousness": 5, "vocals": 500}}, "whole number from 0 to 100"),
            ({"sliders": "loud"}, "all three sliders"),
            ({"complexity": "wild"}, "complexity must be one of"), ({"density": "loud"}, "density must be one of"),
            ({"mode": "speech"}, "mode must be one of"), ({"energy": 50}, "energy must be one of"),
            ({"avoid_repeats": "yes"}, "avoid_repeats must be true or false"), ({"remember": 1}, "remember must be true or false"),
            ({"scaffold": "yes"}, "scaffold must be true or false"), ({"tag_style": "loud"}, "tag_style must be one of"),
            ({"lyrics": 5}, "lyrics must be text"), ({"lyrics": "  "}, "lyrics must be text"), ({"lyrics": "x" * 12001}, "at most 12,000 characters"),
            ({"lyrics": "a\x00b"}, "at most 12,000 characters"),
            ({"preset": 5}, "preset must be the id"), ({"preset": "nothing/here"}, "no style called"),
            ({"styles": "jazz"}, "styles must be a list"), ({"styles": [1]}, "styles must contain only"), ({"tags": ["x"] * 21}, "at most 20"),
            ({"query": ["x"]}, "Search words must be text"), ({"family": ""}, "family must be a short word"),
            ({"tags": ["no-such-tag"]}, "No styles match"), ({"styles": ["nothing"]}, "No styles match"),
            ({"energy": "calm", "tags": ["driving-only-tag"]}, "No styles match"),
        ]
        for request, expected in cases:
            with self.subTest(request=str(request)[:70]), self.assertRaisesRegex(PromptError, expected):
                self.service.compose(request)
        self.assertEqual(self.store.counts(), before)

    def test_every_catalog_preset_composes_through_the_service(self):
        for preset in LIBRARY.presets[::17]:
            result = self.service.compose({"preset": preset.id, "seed": 1, "remember": False,
                                           "sliders": {"energy": 50, "adventurousness": 50, "vocals": 100}})
            self.assertEqual(result["treatment"]["mode"], "vocal")
            self.assertEqual(result["lyrics"]["source"], "scaffold")
            self.assertEqual(result["prompt"]["lyrics"], lyrics_text(result))
        for seed in range(3):
            self.assertTrue(self.service.compose({"seed": seed, "remember": False})["id"])


def lyrics_text(result):
    return result["lyrics"]["text"]


class SliderAndLyricsRouteHelpersTests(Case):
    def test_sliders_and_lyrics_helpers_validate_and_do_not_remember(self):
        before = self.store.counts()
        self.assertEqual(self.service.sliders(10, 90, 100)["modes"], ["vocal"])
        outline = self.service.lyrics("neo-soul/rondo-path", 8, tag_style="lower", form="full")
        self.assertEqual(outline["source"], "scaffold")
        self.assertTrue(outline["lyrics"].startswith("[verse]"))
        for bad in (("nothing/here", 1, {}), ("neo-soul/rondo-path", -1, {}), ("neo-soul/rondo-path", True, {}),
                    ("neo-soul/rondo-path", 1, {"tag_style": "shout"}), ("neo-soul/rondo-path", 1, {"form": "epic"})):
            with self.subTest(bad=bad), self.assertRaises(PromptError):
                self.service.lyrics(bad[0], bad[1], **bad[2])
        self.assertEqual(self.store.counts(), before)

    def test_replay_refuses_what_it_cannot_prove(self):
        record = self.service.compose({"seed": 3, "remember": False})["prompt"]
        with self.assertRaises(PromptError):
            self.service.replay({**record, "seed": 4})
        with self.assertRaises(PromptError):
            self.service.replay({"preset_id": "user:mine"})
        with self.assertRaises(PromptError):
            self.service.replay("nonsense")


if __name__ == "__main__":
    unittest.main()
