import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from myradio.prompts import (
    COMPLEXITIES, DENSITIES, MUSIC_MODEL, CatalogError, Library,
    NoMatchingPreset, load_library,
)
from myradio.prompts.library import MAX_LYRICS_CHARS, MAX_SEED, MAX_STYLE_CHARS


ROOT = Path(__file__).resolve().parents[1]


class CatalogChecks:
    """The original catalog tests, unchanged in what they assert.

    They run twice: against the catalog as first released (``through="1.0.0"``, exactly the numbers
    the tests always asserted: 32 palettes, 12 arrangements, 384 presets, 6,912 variants) and against
    the current catalog, where only the size constants differ.
    """

    THROUGH = None
    PALETTES = ARRANGEMENTS = PRESETS = VARIANTS = 0

    @classmethod
    def setUpClass(cls):
        cls.library = load_library(through=cls.THROUGH)

    def test_ships_a_large_library_not_a_handful_of_examples(self):
        self.assertGreaterEqual(len(self.library.presets), 384)
        self.assertEqual(len(self.library.presets), self.PRESETS)
        self.assertEqual(len(self.library.palettes), self.PALETTES)
        self.assertEqual(len(self.library.arrangements), self.ARRANGEMENTS)
        self.assertGreaterEqual(len({p.family for p in self.library.palettes}), 12)

    def test_stable_ids_and_titles_are_unique(self):
        presets = self.library.presets
        self.assertEqual(len({p.id for p in presets}), len(presets))
        self.assertEqual(len({p.title for p in presets}), len(presets))
        for preset in presets:
            self.assertIs(self.library.get(preset.id), preset)

    def test_every_variant_is_structured_bounded_and_music_only(self):
        fingerprints = set()
        styles = set()
        count = 0
        for preset in self.library.presets:
            for complexity in COMPLEXITIES:
                for density in DENSITIES:
                    for mode in ("instrumental", "vocal"):
                        with self.subTest(preset=preset.id, complexity=complexity, density=density, mode=mode):
                            lyrics = "[Verse]\nWe make room for a brighter day." if mode == "vocal" else None
                            prompt = self.library.generate(
                                preset.id, 57, complexity=complexity, density=density, mode=mode, lyrics=lyrics,
                            )
                            self.assertLessEqual(len(prompt.style), MAX_STYLE_CHARS)
                            for heading in ("Harmony:", "Rhythm:", "Motif:", "Development:", "Form:", "Dynamics:", "Production:", "Ending:"):
                                self.assertIn(heading, prompt.style)
                            self.assertIn(preset.palette.meter, prompt.style)
                            self.assertTrue(preset.palette.bpm[0] <= prompt.bpm <= preset.palette.bpm[1])
                            self.assertEqual(prompt.target_model, MUSIC_MODEL)
                            self.assertEqual(prompt.kind, "music")
                            self.assertFalse(prompt.speech)
                            self.assertEqual(prompt.lyrics, lyrics)
                            self.assertEqual(prompt.catalog_version, self.library.version)
                            self.assertEqual(prompt.to_dict()["fingerprint"], prompt.fingerprint)
                            if mode == "instrumental":
                                self.assertIn("No vocals, sung lyrics, spoken words", prompt.style)
                            else:
                                self.assertNotIn("No vocals", prompt.style)
                                self.assertIn("Sing the supplied lyrics", prompt.style)
                            fingerprints.add(prompt.fingerprint)
                            styles.add(prompt.style)
                            count += 1
        self.assertEqual(count, self.VARIANTS)
        self.assertEqual(len(fingerprints), count)
        self.assertEqual(len(styles), count)

    def test_complexity_is_not_an_alias_for_density(self):
        preset = "warm-brush-jazz/interlocking-lines"
        simple = self.library.generate(preset, 3, complexity="simple", density="sparse")
        intricate = self.library.generate(preset, 3, complexity="intricate", density="sparse")
        lush = self.library.generate(preset, 3, complexity="intricate", density="lush")
        self.assertIn("Sparse texture", intricate.style)
        self.assertNotEqual(simple.style, intricate.style)
        for heading in ("Harmony:", "Rhythm:", "Motif:", "Form:"):
            lines = [next(line for line in p.style.splitlines() if line.startswith(heading)) for p in (intricate, lush)]
            self.assertEqual(*lines)
        self.assertEqual(simple.bpm, intricate.bpm)

    def test_every_palette_and_arrangement_has_real_complexity_choices(self):
        for palette in self.library.palettes:
            self.assertEqual(len({palette.harmony.at(c) for c in COMPLEXITIES}), 3)
            self.assertEqual(len({palette.rhythm.at(c) for c in COMPLEXITIES}), 3)
        for arrangement in self.library.arrangements:
            self.assertEqual(len({arrangement.sections.at(c) for c in COMPLEXITIES}), 3)
            self.assertEqual(len({arrangement.development.at(c) for c in COMPLEXITIES}), 3)

    def test_same_seed_replays_and_other_seeds_vary_without_mutation(self):
        preset = self.library.presets[0].id
        first = self.library.generate(preset, 123)
        self.assertEqual(first, load_library(through=self.THROUGH).generate(preset, 123))
        self.assertEqual(first.fingerprint, load_library(through=self.THROUGH).generate(preset, 123).fingerprint)
        self.assertGreater(len({self.library.generate(preset, seed).style for seed in range(20)}), 10)
        self.assertEqual(first, self.library.generate(preset, 123))
        self.assertEqual(self.library.choose(900).id, self.library.choose(900).id)

    def test_browse_applies_all_filters_and_searches_musical_details(self):
        matches = self.library.browse(family="JAZZ", tags=["acoustic"], energy="calm", exclude_tags=["waltz"])
        self.assertTrue(matches)
        for preset in matches:
            self.assertEqual(preset.palette.family, "jazz")
            self.assertEqual(preset.palette.energy, "calm")
            self.assertIn("acoustic", preset.palette.tags)
            self.assertNotIn("waltz", preset.palette.tags)
        self.assertTrue(self.library.browse(query="TRITONE"))
        self.assertEqual(self.library.browse(tags=["jazz", "nonexistent"]), ())
        self.assertEqual(self.library.browse(family="unknown"), ())

    def test_history_does_not_repeat_until_explicitly_relaxed(self):
        pool = self.library.browse(tags=["bossa"])
        recent = []
        for _ in pool:
            preset = self.library.choose(42, tags=["bossa"], recent_ids=recent)
            self.assertNotIn(preset.id, recent)
            recent.append(preset.id)
        with self.assertRaises(NoMatchingPreset):
            self.library.choose(42, tags=["bossa"], recent_ids=recent)
        with self.assertRaises(NoMatchingPreset):
            self.library.choose(42, tags=["bossa"], recent_palettes=["bossa-courtyard"])
        chosen = self.library.choose(42, recent_palettes=["bossa-courtyard"])
        self.assertNotEqual(chosen.palette.id, "bossa-courtyard")

    def test_empty_matches_do_not_fall_back_to_unwanted_styles(self):
        with self.assertRaises(NoMatchingPreset):
            self.library.choose(1, tags=["jazz"], exclude_tags=["jazz"])
        with self.assertRaises(NoMatchingPreset):
            self.library.get("unknown/preset")

    def test_validation_rejects_bad_inputs(self):
        preset = self.library.presets[0].id
        for seed in (-1, MAX_SEED + 1, True, 1.5, "1", None):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):
                    self.library.generate(preset, seed)
                with self.assertRaises(ValueError):
                    self.library.choose(seed)
        for kwargs in (
            {"complexity": "extreme"}, {"density": "loud"}, {"mode": "speech"},
            {"lyrics": "words"}, {"mode": "vocal"}, {"mode": "vocal", "lyrics": "  "},
            {"mode": "vocal", "lyrics": "x" * (MAX_LYRICS_CHARS + 1)},
            {"mode": "vocal", "lyrics": "words\x00"},
        ):
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(ValueError):
                    self.library.generate(preset, 0, **kwargs)
        for kwargs in ({"tags": "jazz"}, {"exclude_tags": [None]}, {"energy": "loud"}, {"query": None}, {"family": ""}):
            with self.assertRaises(ValueError):
                self.library.browse(**kwargs)

    def test_vocal_music_preserves_lyrics_and_never_routes_to_drama(self):
        lyrics = "[Verse]\nDes étoiles dans nos mains.\n[Chorus]\n明日は明るい。"
        prompt = self.library.generate(self.library.presets[0].id, 0, mode="vocal", lyrics=lyrics)
        self.assertEqual(prompt.lyrics, lyrics)
        self.assertNotIn(lyrics, prompt.style)
        self.assertEqual(prompt.target_model, MUSIC_MODEL)
        self.assertFalse(prompt.speech)

    def test_catalog_rejects_corrupt_data(self):
        root = files("myradio.prompts").joinpath("data")
        pd = json.loads(root.joinpath("palettes.json").read_text(encoding="utf-8"))
        ad = json.loads(root.joinpath("arrangements.json").read_text(encoding="utf-8"))
        mutations = [
            lambda p, a: p.update(schema_version=2),
            lambda p, a: p.update(schema_version=True),
            lambda p, a: p.update(palettes=[]),
            lambda p, a: p["palettes"].append(copy.deepcopy(p["palettes"][0])),
            lambda p, a: p["palettes"][0].update(bpm=[90, 60]),
            lambda p, a: p["palettes"][0].update(bpm=[True, 90]),
            lambda p, a: p["palettes"][0].update(energy="loud"),
            lambda p, a: p["palettes"][0]["harmony"].pop("intricate"),
            lambda p, a: p["palettes"][0].update(motifs=["same", "same"]),
            lambda p, a: p["palettes"][0].update(unexpected="field"),
            lambda p, a: a.update(catalog_version="different"),
            lambda p, a: a["arrangements"][0]["sections"].update(simple=["Too short"]),
        ]
        for mutation in mutations:
            p, a = copy.deepcopy(pd), copy.deepcopy(ad)
            mutation(p, a)
            with self.assertRaises(CatalogError):
                Library.from_documents(p, a)


class CommandChecks:
    """The original command-line tests, run against the 1.0.0 catalog (``--catalog 1.0.0``) and the current one."""

    CATALOG_ARGS: tuple = ()
    BOSSA = PRESETS = VARIANTS = 0

    def run_cli(self, *args, cwd=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "myradio.prompts", *self.CATALOG_ARGS, *args], cwd=cwd or ROOT,
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )

    def test_list_and_generation_work_outside_the_project_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("list", "--tag", "bossa", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)), self.BOSSA)
            result = self.run_cli(
                "generate", "--preset", "bossa-courtyard/theme-and-variations",
                "--complexity", "intricate", "--density", "sparse", "--seed", "23", cwd=directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record["seed"], 23)
            self.assertEqual(record["complexity"], "intricate")
            self.assertEqual(record["density"], "sparse")
            self.assertEqual(record["target_model"], MUSIC_MODEL)

    def test_export_is_complete_parseable_jsonl(self):
        result = self.run_cli("export", "--complexity", "intricate", "--seed", "17")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(records), self.PRESETS)
        self.assertEqual(len({r["preset_id"] for r in records}), self.PRESETS)
        self.assertTrue(all(r["complexity"] == "intricate" for r in records))

    def test_validate_reports_no_audio_evaluation(self):
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["compiled_variants"], self.VARIANTS)
        self.assertFalse(report["audio_evaluated"])

    def test_cli_rejects_errors_without_partial_output(self):
        for args in (
            ("generate", "--seed", "-1"),
            ("generate", "--preset", "bad/id"),
            ("generate", "--preset", "bossa-courtyard/theme-and-return", "--exclude-tag", "bossa"),
            ("generate", "--preset", "bossa-courtyard/theme-and-return", "--recent", "bossa-courtyard/theme-and-return"),
            ("generate", "--tag", "missing"),
            ("generate", "--lyrics-file", "missing-lyrics-file.txt"),
            ("export", "--tag", "missing"),
        ):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_cli_uses_supplied_lyrics_only_for_vocal_music(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lyrics.txt"
            path.write_text("[Verse]\nA warm new morning.", encoding="utf-8")
            result = self.run_cli("generate", "--lyrics-file", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record["mode"], "vocal")
            self.assertEqual(record["lyrics"], path.read_text(encoding="utf-8"))
            self.assertFalse(record["speech"])


class LegacyCatalogTests(CatalogChecks, unittest.TestCase):
    THROUGH, PALETTES, ARRANGEMENTS, PRESETS, VARIANTS = "1.0.0", 32, 12, 384, 6912


class CatalogTests(CatalogChecks, unittest.TestCase):  # keeps its original name and test ids
    THROUGH, PALETTES, ARRANGEMENTS, PRESETS, VARIANTS = None, 71, 18, 1278, 23004


class LegacyCommandTests(CommandChecks, unittest.TestCase):
    CATALOG_ARGS, BOSSA, PRESETS, VARIANTS = ("--catalog", "1.0.0"), 12, 384, 6912


class CommandTests(CommandChecks, unittest.TestCase):  # keeps its original name and test ids
    CATALOG_ARGS, BOSSA, PRESETS, VARIANTS = (), 18, 1278, 23004


if __name__ == "__main__":
    unittest.main()
