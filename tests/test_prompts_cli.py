"""The command-line additions: check, lyrics, sliders, --scaffold-lyrics and --catalog."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from myradio.prompts import filters_from_sliders, load_library, lyrics_scaffold

ROOT = Path(__file__).resolve().parents[1]


class CliCase(unittest.TestCase):
    def run_cli(self, *args, cwd=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run([sys.executable, "-m", "myradio.prompts", *args], cwd=cwd or ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def fails_politely(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 2, (args, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        return result.stderr


class CheckAndValidateTests(CliCase):
    def test_check_reports_the_rules_and_exits_zero_for_the_shipped_catalog(self):
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual((report["palettes"], report["arrangements"], report["errors"], report["rules"]), (71, 18, [], 27))
        self.assertEqual({w["rule"] for w in report["warnings"]}, {"instrument.duplicate"})
        legacy = json.loads(self.run_cli("--catalog", "1.0.0", "check").stdout)
        self.assertEqual((legacy["palettes"], legacy["arrangements"]), (32, 12))

    def test_validate_includes_the_compatibility_summary(self):
        report = json.loads(self.run_cli("--catalog", "1.0.0", "validate").stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["compatibility"], {"ok": True, "errors": 0, "warnings": 7, "rules": 27})
        self.assertFalse(report["audio_evaluated"])


class CatalogSelectionTests(CliCase):
    def test_an_old_catalog_cannot_see_new_presets_and_an_unknown_one_is_refused(self):
        self.assertIn("Unknown preset", self.fails_politely("--catalog", "1.0.0", "generate", "--preset", "kora-river-ostinato/slow-tide"))
        ok = self.run_cli("generate", "--preset", "kora-river-ostinato/slow-tide", "--seed", "4")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["catalog_version"], "1.1.0")
        self.assertIn("Unknown catalog version", self.fails_politely("--catalog", "9.9.9", "list"))
        self.fails_politely("--catalog", "latest", "list")

    def test_the_same_old_preset_composes_the_same_text_in_either_catalog(self):
        args = ("generate", "--preset", "warm-brush-jazz/theme-and-return", "--seed", "31", "--complexity", "intricate")
        old = json.loads(self.run_cli("--catalog", "1.0.0", *args).stdout)
        new = json.loads(self.run_cli(*args).stdout)
        self.assertEqual((old["style"], old["bpm"]), (new["style"], new["bpm"]))
        self.assertEqual((old["catalog_version"], new["catalog_version"]), ("1.0.0", "1.1.0"))


class LyricsCommandTests(CliCase):
    def test_lyrics_prints_the_outline_record_or_just_the_text(self):
        record = json.loads(self.run_cli("lyrics", "--preset", "neo-soul/rondo-path", "--seed", "8").stdout)
        expected = lyrics_scaffold("neo-soul/rondo-path", 8)
        self.assertEqual((record["source"], record["speech"], record["lyrics"]), ("scaffold", False, expected.text))
        text = self.run_cli("lyrics", "--preset", "neo-soul/rondo-path", "--seed", "8", "--text", "--tag-style", "lower", "--form", "full")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout.rstrip("\n"), lyrics_scaffold("neo-soul/rondo-path", 8, tag_style="lower", form="full").text)

    def test_bad_lyrics_requests_fail_without_output(self):
        self.fails_politely("lyrics", "--preset", "nothing/here")
        self.fails_politely("lyrics", "--preset", "neo-soul/rondo-path", "--seed", "-1")
        self.fails_politely("lyrics", "--preset", "neo-soul/rondo-path", "--tag-style", "shout")
        self.fails_politely("lyrics")

    def test_generate_with_a_generated_outline_is_vocal_music_and_not_speech(self):
        result = self.run_cli("generate", "--preset", "neo-soul/rondo-path", "--seed", "8", "--scaffold-lyrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual((record["mode"], record["speech"], record["kind"]), ("vocal", False, "music"))
        self.assertEqual(record["lyrics"], lyrics_scaffold("neo-soul/rondo-path", 8).text)
        self.assertIn("Sing the supplied lyrics", record["style"])

    def test_own_lyrics_and_a_generated_outline_cannot_be_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mine.txt"
            path.write_text("[Verse]\nMy own words", encoding="utf-8")
            message = self.fails_politely("generate", "--preset", "neo-soul/rondo-path", "--scaffold-lyrics", "--lyrics-file", str(path))
            self.assertIn("not both", message)


class SlidersCommandTests(CliCase):
    def test_sliders_prints_what_the_positions_choose(self):
        record = json.loads(self.run_cli("sliders", "--energy", "10", "--adventurousness", "90", "--vocals", "50").stdout)
        self.assertEqual(record, filters_from_sliders(10, 90, 50).to_dict())
        self.assertEqual((record["energy_class"], record["density"], record["complexity"]), ("calm", "sparse", "intricate"))

    def test_out_of_range_sliders_are_refused_in_plain_words(self):
        for args in (("--energy", "101", "--adventurousness", "0", "--vocals", "0"), ("--energy", "x", "--adventurousness", "0", "--vocals", "0"),
                     ("--energy", "1", "--adventurousness", "-1", "--vocals", "0"), ("--energy", "1", "--adventurousness", "1")):
            result = self.run_cli("sliders", *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stderr)

    def test_the_library_still_has_what_the_cli_reports(self):
        self.assertEqual(len(load_library().presets), 1278)


if __name__ == "__main__":
    unittest.main()
