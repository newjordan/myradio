"""The three studio sliders (energy, how adventurous, vocals) and what they choose.

The mapping is a plain table; these tests read it the way the documentation does and prove the
promise that complexity never implies loud."""
import unittest
from pathlib import Path

from myradio.prompts import PromptService, filters_from_sliders, load_library
from myradio.prompts.sliders import (
    ARRANGEMENT_WINDOW, COMPLEXITY_BANDS, DENSITY_BY_ENERGY, ENERGY_BANDS, PRESET_WINDOW, STYLE_WINDOW,
    vocal_words,
)

DOCS = Path(__file__).resolve().parents[1] / "docs" / "PROMPTS.md"


class TableTests(unittest.TestCase):
    ENERGY = {
        0: ("calm", "sparse"), 1: ("calm", "sparse"), 16: ("calm", "sparse"), 32: ("calm", "sparse"), 33: ("calm", "sparse"),
        34: ("steady", "balanced"), 50: ("steady", "balanced"), 65: ("steady", "balanced"), 66: ("steady", "balanced"),
        67: ("driving", "lush"), 83: ("driving", "lush"), 99: ("driving", "lush"), 100: ("driving", "lush"),
    }
    ADVENTURE = {
        0: ("simple", 2, 1, 100), 33: ("simple", 2, 1, 100), 34: ("layered", 6, 3, 200), 66: ("layered", 6, 3, 200),
        67: ("intricate", 12, 5, 400), 100: ("intricate", 12, 5, 400),
    }

    def test_energy_chooses_the_energy_class_and_the_texture(self):
        for value, (energy_class, density) in self.ENERGY.items():
            with self.subTest(energy=value):
                found = filters_from_sliders(value, 50, 50)
                self.assertEqual((found.energy_class, found.density), (energy_class, density))
                self.assertEqual(found.browse_filters(), {"energy": energy_class})

    def test_adventurousness_chooses_complexity_and_how_widely_the_station_rotates(self):
        for value, (complexity, styles, arrangements, presets) in self.ADVENTURE.items():
            with self.subTest(adventurousness=value):
                found = filters_from_sliders(50, value, 50)
                self.assertEqual((found.complexity, found.style_window, found.arrangement_window, found.preset_window),
                                 (complexity, styles, arrangements, presets))

    def test_vocals_chooses_how_often_a_song_is_sung(self):
        table = {0: (("instrumental",), 0.0, "instrumental only"), 1: (("instrumental", "vocal"), 0.01, "mostly instrumental"),
                 33: (("instrumental", "vocal"), 0.33, "mostly instrumental"),
                 34: (("instrumental", "vocal"), 0.34, "a mix of instrumentals and songs with words"),
                 66: (("instrumental", "vocal"), 0.66, "a mix of instrumentals and songs with words"),
                 67: (("instrumental", "vocal"), 0.67, "mostly songs with words"),
                 99: (("instrumental", "vocal"), 0.99, "mostly songs with words"), 100: (("vocal",), 1.0, "always songs with words")}
        for value, (modes, share, words) in table.items():
            with self.subTest(vocals=value):
                found = filters_from_sliders(50, 50, value)
                self.assertEqual((found.modes, found.vocal_share, vocal_words(value)), (modes, share, words))
                self.assertIn(words, found.summary())

    def test_the_bands_cover_zero_to_one_hundred_exactly_once(self):
        for bands in (ENERGY_BANDS, COMPLEXITY_BANDS):
            covered = [n for low, high, _ in bands for n in range(low, high + 1)]
            self.assertEqual(covered, list(range(101)))
        self.assertEqual(set(DENSITY_BY_ENERGY), {"calm", "steady", "driving"})
        self.assertEqual(set(STYLE_WINDOW), {"simple", "layered", "intricate"})
        self.assertEqual(set(ARRANGEMENT_WINDOW) | set(PRESET_WINDOW), {"simple", "layered", "intricate"})

    def test_the_documented_table_matches_the_code(self):
        text = DOCS.read_text(encoding="utf-8")
        for low, high, name in ENERGY_BANDS:
            self.assertRegex(text, rf"\|\s*{low} to {high}\s*\|\s*{name}\s*\|\s*{DENSITY_BY_ENERGY[name]}\s*\|", name)
        for low, high, name in COMPLEXITY_BANDS:
            self.assertRegex(
                text, rf"\|\s*{low} to {high}\s*\|\s*{name}\s*\|\s*{STYLE_WINDOW[name]}\s*\|\s*{ARRANGEMENT_WINDOW[name]}\s*\|\s*{PRESET_WINDOW[name]}\s*\|", name)


class ComplexNeverMeansLoudTests(unittest.TestCase):
    def test_energy_and_texture_ignore_adventurousness_and_complexity_ignores_energy(self):
        by_energy = {}
        by_adventure = {}
        for energy in range(101):
            for adventure in range(101):
                found = filters_from_sliders(energy, adventure, 0)
                by_energy.setdefault(energy, set()).add((found.energy_class, found.density))
                by_adventure.setdefault(adventure, set()).add(found.complexity)
        self.assertTrue(all(len(options) == 1 for options in by_energy.values()))
        self.assertTrue(all(len(options) == 1 for options in by_adventure.values()))

    def test_a_calm_station_stays_calm_at_every_level_of_adventure(self):
        library = load_library()
        calm = [p for p in library.presets if p.palette.energy == "calm"][::37]
        for adventure in (0, 50, 100):
            found = filters_from_sliders(10, adventure, 0)
            self.assertEqual(found.energy_class, "calm")
            for preset in calm:
                prompt = library.generate(preset.id, 9, complexity=found.complexity, density=found.density)
                self.assertIn("calm energy", prompt.style)
                self.assertTrue(preset.palette.bpm[0] <= prompt.bpm <= preset.palette.bpm[1])
        # the same seed keeps tempo and meter while only the writing gets more elaborate
        preset = calm[0]
        simple = library.generate(preset.id, 9, complexity="simple", density="sparse")
        intricate = library.generate(preset.id, 9, complexity="intricate", density="sparse")
        self.assertEqual((simple.bpm, simple.meter), (intricate.bpm, intricate.meter))
        self.assertNotEqual(simple.style, intricate.style)

    def test_the_catalog_filter_from_sliders_never_returns_a_louder_class_than_asked(self):
        library = load_library()
        for energy, expected in ((0, "calm"), (50, "steady"), (100, "driving")):
            picked = library.browse(**filters_from_sliders(energy, 100, 0).browse_filters())
            self.assertTrue(picked)
            self.assertEqual({p.palette.energy for p in picked}, {expected})


class VocalModeTests(unittest.TestCase):
    def test_extremes_are_certain_and_the_middle_follows_the_percentage(self):
        self.assertEqual({filters_from_sliders(5, 5, 0).mode_for(s) for s in range(200)}, {"instrumental"})
        self.assertEqual({filters_from_sliders(5, 5, 100).mode_for(s) for s in range(200)}, {"vocal"})
        for vocals in (10, 30, 50, 80):
            sung = sum(filters_from_sliders(5, 5, vocals).mode_for(seed) == "vocal" for seed in range(2000))
            self.assertAlmostEqual(sung / 2000, vocals / 100, delta=0.04)

    def test_the_same_seed_always_gives_the_same_answer_and_more_vocals_never_removes_a_song(self):
        for seed in range(0, 3000, 7):
            answers = [filters_from_sliders(5, 5, v).mode_for(seed) for v in range(0, 101, 5)]
            self.assertEqual(answers, [filters_from_sliders(5, 5, v).mode_for(seed) for v in range(0, 101, 5)])
            sung = [a == "vocal" for a in answers]
            self.assertEqual(sung, sorted(sung), "raising the slider only turns instrumentals into songs")


class ValidationTests(unittest.TestCase):
    def test_only_whole_numbers_from_zero_to_one_hundred_are_accepted(self):
        for bad in (-1, 101, 1000, 50.0, 50.5, "50", None, True, False, [50], float("nan")):
            for position in range(3):
                args = [50, 50, 50]
                args[position] = bad
                with self.subTest(bad=repr(bad), position=position):
                    with self.assertRaises(ValueError) as caught:
                        filters_from_sliders(*args)
                    self.assertIn("whole number from 0 to 100", str(caught.exception))

    def test_the_summary_is_plain_language(self):
        summary = filters_from_sliders(20, 80, 0).summary()
        self.assertEqual(summary, "Calm and spacious; surprising, richly layered writing; instrumental only")
        data = filters_from_sliders(20, 80, 0).to_dict()
        for key in ("energy_class", "density", "complexity", "vocal_share", "modes", "avoid_repeating", "summary"):
            self.assertIn(key, data)

    def test_the_service_turns_slider_mistakes_into_plain_messages(self):
        service = PromptService(None)
        with self.assertRaisesRegex(Exception, "whole number from 0 to 100"):
            service.sliders(101, 0, 0)
        self.assertEqual(service.sliders(0, 0, 0)["energy_class"], "calm")


if __name__ == "__main__":
    unittest.main()
