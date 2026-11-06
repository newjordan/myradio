"""Catalog growth: packs, content rules, determinism, replay and the user-facing wording of the data.

The original 384 presets are frozen by test_prompts_golden.py. These tests cover everything added
around them. No model is run and no audio is made: passing here says the data is well formed and
consistent, never that it sounds good.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from importlib.resources import files
from pathlib import Path

from myradio.prompts import (
    COMPLEXITIES, CatalogError, Library, NoMatchingPreset, filter_presets, load_library, replay, shared_library,
)
from myradio.prompts.library import PACKS, Arrangement, Palette
from myradio.prompts.service import HERITAGE_NOTE_IDS

ROOT = Path(__file__).resolve().parents[1]
DATA = files("myradio.prompts").joinpath("data")
LEGACY_SHA256 = {
    "palettes.json": "73db58e34567538975f0d01c096c97bb1e69ef6889c2cf99a98569a7a6ffb7e5",
    "arrangements.json": "676f05336f2e9e90b03743b5bafbf3ee875cc2cabf39d8584da5127f4661d502",
}
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
# Capitalised words allowed inside a sentence: chord letters, mode and scale names, numerals, a
# French horn. Anything else capitalised mid-sentence would be a person or brand name.
ALLOWED_CAPITALS = {
    "A", "B", "C", "D", "E", "F", "G", "Dorian", "Phrygian", "Lydian", "Mixolydian", "Aeolian", "FM",
    "French", "Hijaz", "Hicaz", "Hicaz-flavored", "I-IV-V", "IV",
}
LOADED_WORDS = re.compile(
    r"\b(?:exotic|tribal|primitive|oriental|ethnic|gypsy|savage|genuine|world music)\b|authentic(?! cadence)",
    re.IGNORECASE,
)


def read(name):
    return json.loads(DATA.joinpath(name).read_text(encoding="utf-8"))


def sha256(name):
    return hashlib.sha256(DATA.joinpath(name).read_bytes()).hexdigest()


def fields_of(palette):
    from myradio.prompts.compat import _palette_fields
    return _palette_fields(palette)


class PackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = load_library()
        cls.legacy = load_library(through="1.0.0")

    def test_packaged_packs_are_listed_and_live_directly_in_the_data_folder(self):
        self.assertEqual(PACKS[0], ("palettes.json", "arrangements.json"))
        for palettes, arrangements in PACKS:
            for name in (palettes, arrangements):
                self.assertNotIn("/", name, "pyproject's package data pattern data/*.json only covers this folder level")
                self.assertTrue(DATA.joinpath(name).is_file())

    def test_the_original_data_files_are_byte_for_byte_unchanged(self):
        for name, expected in LEGACY_SHA256.items():
            self.assertEqual(sha256(name), expected, f"{name} is frozen; add content in a new pack instead")

    def test_catalog_growth_meets_the_brief(self):
        self.assertEqual(self.library.version, "1.1.0")
        self.assertEqual(self.library.versions(), ("1.0.0", "1.1.0"))
        new_palettes = [p for p in self.library.palettes if self.library.origins[f"palette:{p.id}"] == "1.1.0"]
        new_arrangements = [a for a in self.library.arrangements if self.library.origins[f"arrangement:{a.id}"] == "1.1.0"]
        self.assertGreaterEqual(len(new_palettes), 32)
        self.assertGreaterEqual(len(new_arrangements), 4)
        self.assertGreaterEqual(len(self.library.palettes), 64)
        self.assertEqual(len(self.library.presets), len(self.library.palettes) * len(self.library.arrangements))
        self.assertGreaterEqual(len({p.family for p in self.library.palettes}), 30)

    def test_through_returns_the_original_library_in_the_original_order(self):
        self.assertEqual((len(self.legacy.palettes), len(self.legacy.arrangements), len(self.legacy.presets)), (32, 12, 384))
        self.assertEqual(self.legacy.version, "1.0.0")
        self.assertEqual([p.id for p in self.legacy.palettes], [p.id for p in self.library.palettes[:32]])
        self.assertEqual([a.id for a in self.legacy.arrangements], [a.id for a in self.library.arrangements[:12]])
        self.assertIs(self.library.through("1.1.0"), self.library)
        with self.assertRaises(CatalogError):
            self.library.through("9.9.9")
        with self.assertRaises(ValueError):
            load_library(through=1)

    def test_old_presets_use_the_old_random_key_and_new_ones_the_new(self):
        old = self.library.get("warm-brush-jazz/theme-and-return")
        self.assertEqual(self.library.epoch_for(old), "1.0.0")
        self.assertEqual(self.library.epoch_for(self.library.get("warm-brush-jazz/slow-tide")), "1.1.0")
        self.assertEqual(self.library.epoch_for(self.library.get("kora-river-ostinato/theme-and-return")), "1.1.0")
        # the same words come out of either catalog; only the recorded catalog version differs
        a = self.legacy.generate(old.id, 99, complexity="intricate", density="lush")
        b = self.library.generate(old.id, 99, complexity="intricate", density="lush")
        self.assertEqual((a.style, a.bpm, a.title), (b.style, b.bpm, b.title))
        self.assertEqual((a.catalog_version, b.catalog_version), ("1.0.0", "1.1.0"))
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_pack_rules_are_enforced(self):
        p1, a1 = read("palettes.json"), read("arrangements.json")
        p2, a2 = read("palettes-1.1.0.json"), read("arrangements-1.1.0.json")
        self.assertEqual(len(Library.from_packs([(p1, a1), (p2, a2)]).presets), 1278)
        bad_versions = (
            [],                              # nothing to load
            [(p2, a2), (p1, a1)],            # versions must increase
            [(p1, a1), (p1, a1)],            # not strictly increasing, and duplicate ids
            [(p1, a2)],                      # a pack's two documents must share a version
            [(p1, a1), ("nope", a2)],        # not a document
            [(p1, a1), (p2,)],               # not a pair
        )
        for packs in bad_versions:
            with self.subTest(packs=str(packs)[:30]), self.assertRaises(CatalogError):
                Library.from_packs(packs)
        loose = json.loads(json.dumps(p2))
        loose["catalog_version"] = "next"
        loose_a = json.loads(json.dumps(a2))
        loose_a["catalog_version"] = "next"
        with self.assertRaises(CatalogError):
            Library.from_packs([(p1, a1), (loose, loose_a)])
        clash = json.loads(json.dumps(p2))
        clash["palettes"][0]["id"] = p1["palettes"][0]["id"]
        with self.assertRaises(CatalogError):
            Library.from_packs([(p1, a1), (clash, a2)])

    def test_filtering_is_one_code_path_for_library_and_helper(self):
        for kwargs in ({"tags": ["jazz"]}, {"energy": "calm", "family": "jazz"}, {"query": "flute"},
                       {"exclude_tags": ["acoustic"], "energy": "driving"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.library.browse(**kwargs), filter_presets(self.library.presets, **kwargs))

    def test_shared_library_is_cached_and_equal_to_a_fresh_load(self):
        self.assertIs(shared_library(), shared_library())
        self.assertEqual(shared_library().presets, load_library().presets)
        self.assertEqual(shared_library("1.0.0").presets, self.legacy.presets)

    def test_lookup_helpers_reject_unknown_ids_politely(self):
        for call in (lambda: self.library.get_palette("nope"), lambda: self.library.get_arrangement("nope"),
                     lambda: self.library.get(None)):
            with self.assertRaises(NoMatchingPreset):
                call()


class ContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = load_library()
        cls.new_palettes = [p for p in cls.library.palettes if cls.library.origins[f"palette:{p.id}"] == "1.1.0"]
        cls.new_arrangements = [a for a in cls.library.arrangements if cls.library.origins[f"arrangement:{a.id}"] == "1.1.0"]

    def test_every_new_entry_is_valid_in_the_original_schema(self):
        for document, kind, parse in ((read("palettes-1.1.0.json"), "palettes", Palette.parse),
                                      (read("arrangements-1.1.0.json"), "arrangements", Arrangement.parse)):
            self.assertEqual(set(document), {"schema_version", "catalog_version", kind})
            self.assertEqual((document["schema_version"], document["catalog_version"]), (1, "1.1.0"))
            for index, entry in enumerate(document[kind]):
                with self.subTest(kind=kind, entry=entry["id"]):
                    parse(entry, f"{kind}[{index}]")

    def test_new_palettes_carry_real_musical_content(self):
        for palette in self.new_palettes:
            with self.subTest(palette=palette.id):
                self.assertRegex(palette.id, ID)
                self.assertTrue(3 <= len(palette.tags) <= 6)
                self.assertGreaterEqual(len(palette.moods), 2)
                self.assertGreaterEqual(len(palette.leads), 2)
                self.assertGreaterEqual(len(palette.supports), 2)
                self.assertGreaterEqual(len(palette.motifs), 2)
                self.assertLessEqual(palette.bpm[1] - palette.bpm[0], 40)
                for level in COMPLEXITIES:
                    self.assertGreater(len(palette.harmony.at(level)), 30)
                    self.assertGreater(len(palette.rhythm.at(level)), 30)
                self.assertEqual(len({palette.harmony.at(l) for l in COMPLEXITIES}), 3)
                self.assertEqual(len({palette.rhythm.at(l) for l in COMPLEXITIES}), 3)

    def test_new_arrangements_are_designs_with_three_real_levels(self):
        for arrangement in self.new_arrangements:
            with self.subTest(arrangement=arrangement.id):
                counts = [len(arrangement.sections.at(level)) for level in COMPLEXITIES]
                self.assertTrue(all(3 <= n <= 6 for n in counts))
                self.assertLess(counts[0], counts[2], "the intricate design has more sections than the simple one")
                self.assertEqual(len({arrangement.development.at(l) for l in COMPLEXITIES}), 3)

    def test_names_ids_and_titles_stay_unique_across_packs(self):
        self.assertEqual(len({p.name for p in self.library.palettes}), len(self.library.palettes))
        self.assertEqual(len({a.name for a in self.library.arrangements}), len(self.library.arrangements))
        self.assertEqual(len({p.title for p in self.library.presets}), len(self.library.presets))

    def test_text_conventions_hold_for_every_palette_old_and_new(self):
        for palette in self.library.palettes:
            for name, text in fields_of(palette):
                sentence = name.startswith(("harmony", "rhythm")) or name == "production"
                with self.subTest(palette=palette.id, field=name):
                    self.assertEqual(text.endswith("."), sentence)
                    self.assertEqual(text, " ".join(text.split()), "no doubled or stray spaces")

    def test_new_text_names_no_people_and_makes_no_claim_of_authority(self):
        from myradio.prompts.compat import _arrangement_fields
        candidates = [(p.id, fields_of(p)) for p in self.new_palettes]
        candidates += [(a.id, _arrangement_fields(a)) for a in self.new_arrangements]
        for owner, items in candidates:
            for name, text in items:
                with self.subTest(owner=owner, field=name):
                    self.assertIsNone(LOADED_WORDS.search(text), "loaded or authority-claiming wording")
                    self.assertIsNone(re.search(r"\b[A-Z][A-Za-z]+-(?:style|esque|like)\b", text), "named-person style label")
                    for sentence in re.split(r"(?<=[.!?;:])\s+", text):
                        words = re.findall(r"[A-Za-z][A-Za-z'+-]*", sentence)
                        for word in words[1:]:
                            if word[0].isupper():
                                self.assertIn(word, ALLOWED_CAPITALS, "a capitalised word may be a person or brand name")
                        if words:
                            self.assertNotIn("'s", words[0], "a possessive first word may be a person's name")

    def test_heritage_styles_say_what_they_are(self):
        from myradio.prompts.service import PromptService
        service = PromptService(None, library=self.library)
        for palette in self.library.palettes:
            note = service.catalog(view="styles", query=palette.id.split("-")[0], limit=100)
            item = next(i for i in note["items"] if i["id"] == palette.id)
            expected = palette.id in HERITAGE_NOTE_IDS or "heritage-inspired" in palette.tags
            self.assertEqual(bool(item["heritage_note"]), expected, palette.id)
        self.assertGreaterEqual(sum(1 for p in self.new_palettes if "heritage-inspired" in p.tags), 25)
        text = service._note(self.library.get_palette("kora-river-ostinato"))
        self.assertIn("not a claim of authenticity", text)

    def test_the_documentation_lists_every_style_and_design_and_marks_heritage_styles_correctly(self):
        text = (ROOT / "docs" / "PROMPTS.md").read_text(encoding="utf-8")
        for palette in self.library.palettes:
            self.assertRegex(text, rf"\| `{palette.id}`( \(H\))? \|", f"{palette.id} is missing from docs/PROMPTS.md")
        for arrangement in self.library.arrangements:
            self.assertIn(f"`{arrangement.id}`", text, arrangement.id)
        marked = set(re.findall(r"\| `([a-z0-9-]+)` \(H\) \|", text))
        tagged = {p.id for p in self.library.palettes if "heritage-inspired" in p.tags}
        self.assertEqual(marked, tagged, "the (H) marks in the documentation must match the heritage-inspired tag")
        for number in ("71 authored style palettes", "18 authored arrangement designs", "1,278 stable preset identities", "23,004"):
            self.assertIn(number, text)

    def test_the_old_tags_the_original_tests_rely_on_were_not_reused(self):
        self.assertEqual([p.id for p in self.library.palettes if "bossa" in p.tags], ["bossa-courtyard"])

    def test_catalog_texts_never_ask_for_loudness_or_speech(self):
        from myradio.prompts.compat import check_library
        report = check_library(self.library)
        bad = [f for f in report["errors"] if f["rule"] in ("complexity.loudness", "energy.loud-claim", "text.speech-markup")]
        self.assertEqual(bad, [])


class CompiledPromptConsistencyTests(unittest.TestCase):
    """The compiled prompt must not contradict itself about tempo, meter or vocals."""

    def test_every_compiled_prompt_has_one_tempo_one_meter_and_one_mode(self):
        library = load_library()
        signature = re.compile(r"(?<![\w/])(\d{1,2})/(\d{1,2})(?![\w/])")
        checked = 0
        for preset in library.presets:
            n, d = (int(x) for x in re.match(r"(\d+)/(\d+)", preset.palette.meter).groups())
            for mode, lyrics in (("instrumental", None), ("vocal", "[Verse]\nA line.")):
                prompt = library.generate(preset.id, 4, complexity="intricate", density="lush", mode=mode, lyrics=lyrics)
                style = prompt.style
                self.assertEqual(len(re.findall(r"\bBPM\b", style)), 1, preset.id)
                self.assertIn(f"{prompt.bpm} BPM; meter {preset.palette.meter};", style)
                self.assertTrue(preset.palette.bpm[0] <= prompt.bpm <= preset.palette.bpm[1])
                for a, b in signature.findall(style):
                    self.assertEqual(int(a) * d, n * int(b), f"{preset.id}: {a}/{b} is not the bar length of {preset.palette.meter}")
                self.assertEqual("No vocals" in style, mode == "instrumental")
                self.assertEqual("Sing the supplied lyrics" in style, mode == "vocal")
                checked += 1
        self.assertEqual(checked, 2 * len(library.presets))


class DeterminismAndReplayTests(unittest.TestCase):
    def test_same_inputs_give_the_same_prompt_in_separate_processes(self):
        code = (
            "import json\nfrom myradio.prompts import load_library, lyrics_scaffold\nlib = load_library()\n"
            "out = [lib.generate(i, s, complexity='intricate', density='sparse').fingerprint "
            "for i, s in (('kora-river-ostinato/slow-tide', 7), ('warm-brush-jazz/theme-and-return', 8), "
            "('kopanitsa-village-dance/verse-and-refrain', 2**62))]\n"
            "out.append(lyrics_scaffold('celtic-session-reels/canon-and-release', 31).text)\n"
            "out.append(lib.choose(77, tags=['acoustic']).id)\nprint(json.dumps(out))"
        )
        results = []
        for hash_seed in ("1", "2", "random"):
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONHASHSEED": hash_seed}
            run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            results.append(run.stdout)
        self.assertEqual(len(set(results)), 1, "output must not depend on hash randomisation or the process")

    def test_records_replay_to_the_same_fingerprint_and_tampering_is_caught(self):
        library = load_library()
        for through, preset in ((None, "afrobeat-long-vamp/gradual-bloom"), ("1.0.0", "neo-soul/rondo-path"), (None, "neo-soul/rondo-path")):
            record = load_library(through=through).generate(preset, 21, complexity="layered", density="lush").to_dict()
            self.assertEqual(replay(record).fingerprint, record["fingerprint"])
        record = library.generate("neo-soul/rondo-path", 5).to_dict()
        for change in ({"seed": 6}, {"density": "sparse"}, {"fingerprint": "0" * 64}, {"preset_id": "neo-soul/arch-form"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replay({**record, **change})
        for broken in ({}, {"catalog_version": "9.9.9", **{k: record[k] for k in record if k != "catalog_version"}},
                       {"catalog_version": 5}, "not a record", None):
            with self.subTest(broken=str(broken)[:30]), self.assertRaises(ValueError):
                replay(broken)

    def test_replay_of_a_sung_record_keeps_the_exact_lyrics(self):
        library = load_library()
        record = library.generate("celesta-lullaby/slow-tide", 3, mode="vocal", lyrics="[Verse]\nSoft light.").to_dict()
        self.assertEqual(replay(record).lyrics, "[Verse]\nSoft light.")


if __name__ == "__main__":
    unittest.main()
