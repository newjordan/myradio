"""Regression tests for the defects an independent review found in the prompts lane.

Each class names the defect it guards. Every test here fails on the code as it was reviewed and passes
on the fixed code. Nothing here runs a model or makes audio: these are checks of text, files and
threads. Disposable temporary directories only.

Special characters are written as code points (chr(0x202E)) on purpose, so no invisible character can
hide in this file.
"""
import copy
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from importlib.resources import files
from pathlib import Path
from unittest import mock

from myradio.prompts import (
    PromptError, PromptService, PromptStore, StoreError, UserPresetError, check_palette, load_library,
    lyrics_scaffold,
)
from myradio.prompts import store as store_module
from myradio.prompts import userpresets as userpresets_module
from myradio.prompts.compat import HOST_PHRASES, parse_meter, speaks_like_a_host
from myradio.prompts.instruments import INSTRUMENTS
from myradio.prompts.library import (
    MAX_STYLE_CHARS, Library, Palette, Preset, FieldsError, longest_style_chars,
)
from myradio.prompts.lyric_words import DJ_SPEECH_PHRASES, WORLDS
from myradio.prompts.lyrics import line_syllables_for_meter
from myradio.prompts.textutil import broken_text, encodable, has_odd_character, is_tidy_line, shown, shown_names
from myradio.prompts.userpresets import arrangement_to_doc, build_user_preset, palette_to_doc

ROOT = Path(__file__).resolve().parents[1]
DATA = files("myradio.prompts").joinpath("data")
LIBRARY = load_library()
EDIT = {"id": "my-porch", "title": "My porch", "based_on": "porch-blues-slide/slow-tide",
        "palette": {"name": "My Porch Blues", "bpm": [60, 80]}}

LONE_SURROGATE = chr(0xD83C)           # half of an emoji, as left by cutting a UTF-16 string
RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
LINE_SEPARATOR = chr(0x2028)
NO_BREAK_SPACE = chr(0xA0)
ESCAPE = chr(0x1B)
ARABIC_INDIC_ZERO = chr(0x660)


def celtic(**changes):
    """A complete palette document (the shipped celtic-session-reels with a new name and tags)."""
    doc = palette_to_doc(LIBRARY.get_palette("celtic-session-reels"))
    doc.update(name="Test Reels", tags=["celtic-inspired", "reel", "dance"])
    doc.update(changes)
    return doc


def full_spec(slug="test-reels", arrangement="canon-and-release", **changes):
    return {"id": slug, "palette": celtic(**changes), "arrangement": arrangement}


def filler(size):
    """Harmless words, exactly ``size`` characters, one tidy line that names no instrument, tempo or meter."""
    text = ("warm and open " * (size // 14 + 2))[:size]
    return text[:-1] + "x" if text.endswith(" ") else text


def run_guarded(code, seconds=30.0):
    """Run ``code`` in a fresh interpreter; stop it, and anything it started, if it takes longer than
    ``seconds`` (generous: the lane runs at the lowest priority beside a live station, and a child that
    finishes normally takes well under a second). Returns (finished, stdout). A hang is reported as
    finished == False instead of freezing the whole test run."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code)], cwd=str(ROOT), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, _ = process.communicate(timeout=seconds)
        return True, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = process.communicate()
        return False, out or ""


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prompts.sqlite3"
        self.store = PromptStore.open(self.path)
        self.addCleanup(self.store.close)
        self.service = PromptService(self.store, library=LIBRARY)

    def refusal(self, spec):
        with self.assertRaises(PromptError) as caught:
            self.service.save_custom(spec)
        return caught.exception


# ---------------------------------------------------------------------------------------------------------
# A bar with no beats made the lyric outline loop forever (a blocker: one request thread at full speed)

class ZeroLengthBarTests(StoreCase):
    BAD_METERS = ("0/4", "00/8", "01/4", "0/2", "0/16", ARABIC_INDIC_ZERO + "/4", "10/8 (0+5+5)", "7/8 (2+0+5)")

    def test_no_reader_of_meters_accepts_a_bar_with_no_beats_or_foreign_digits(self):
        for meter in self.BAD_METERS:
            with self.subTest(meter=meter):
                self.assertIsNone(parse_meter(meter))
        for meter in ("1/4", "4/4", "12/8 (3+3+3+3)", "7/8 (2+2+3)", "16/16", "99/2"):
            with self.subTest(meter=meter):
                self.assertIsNotNone(parse_meter(meter))
        self.assertIsNone(parse_meter(None))
        self.assertIsNone(parse_meter(4))

    def test_the_rule_meter_format_fires_for_each_bad_meter(self):
        for meter in self.BAD_METERS:
            with self.subTest(meter=meter):
                palette = Palette.parse(celtic(meter=meter, id="zero-bar"), "x")
                self.assertIn("meter.format", {f.rule for f in check_palette(palette)})

    def test_saving_a_style_with_such_a_meter_is_refused_in_plain_words(self):
        for meter in self.BAD_METERS[:6]:
            with self.subTest(meter=meter):
                error = self.refusal({"id": "zero-bar", "based_on": "warm-brush-jazz/slow-tide", "palette": {"meter": meter}})
                self.assertIn("cannot be saved yet", str(error))
                self.assertIn("meter", str(error))
                self.assertEqual([f.rule for f in error.findings], ["meter.format"])
        self.assertEqual(self.store.user_presets(), ())

    def test_the_syllable_counter_refuses_instead_of_looping(self):
        meters = ", ".join(ascii(m) for m in self.BAD_METERS + ("free time", "", "4/4 swing"))
        finished, out = run_guarded(f"""
            from myradio.prompts.lyrics import line_syllables_for_meter
            for meter in [{meters}]:
                try:
                    line_syllables_for_meter(meter)
                    print("accepted", flush=True)
                except ValueError:
                    print("refused", flush=True)
            """)
        self.assertTrue(finished, f"line_syllables_for_meter looped forever; output so far: {out!r}")
        self.assertEqual(out.split(), ["refused"] * (len(self.BAD_METERS) + 3))

    def test_the_loop_is_also_guarded_if_a_reader_ever_lets_a_zero_through(self):
        """Defence in depth: whatever the reader returns, a bar with no length is refused, never looped on."""
        finished, out = run_guarded("""
            from unittest import mock
            from myradio.prompts import lyrics
            with mock.patch.object(lyrics, "parse_meter", return_value=(0, 4, None)):
                try:
                    lyrics.line_syllables_for_meter("0/4")
                    print("accepted", flush=True)
                except ValueError:
                    print("refused", flush=True)
            """)
        self.assertTrue(finished, "line_syllables_for_meter looped forever on a zero-length bar")
        self.assertEqual(out.split(), ["refused"])

    def test_readable_meters_keep_their_line_lengths(self):
        for meter, expected in {"1/4": 8, "1/2": 8, "1/8": 8, "1/16": 8, "2/2": 8, "3/2": 6, "4/4": 8, "5/8": 10, "99/2": 7}.items():
            with self.subTest(meter=meter):
                self.assertEqual(line_syllables_for_meter(meter), expected)

    def test_a_hand_edited_file_with_such_a_meter_cannot_freeze_a_request(self):
        """The saved-styles file is the person's own; if a bad meter got in without going through the
        checks, asking for an outline must give a plain message, not spin a thread at full speed."""
        finished, out = run_guarded("""
            import tempfile
            from pathlib import Path
            from myradio.prompts import PromptService, PromptStore, load_library
            from myradio.prompts.userpresets import UserPresetRecord, arrangement_to_doc, digest_of, palette_to_doc
            library = load_library()
            base = library.get("warm-brush-jazz/slow-tide")
            palette = palette_to_doc(base.palette)
            palette.update(id="zero-bar", meter="0/4")
            arrangement = arrangement_to_doc(base.arrangement)
            record = UserPresetRecord("zero-bar", "Zero bar", None, palette, arrangement, digest_of(palette, arrangement))
            with tempfile.TemporaryDirectory() as directory:
                store = PromptStore.open(Path(directory) / "p.sqlite3")
                store.save_user_preset(record)
                service = PromptService(store, library=library)
                calls = (("lyrics", lambda: service.lyrics("user:zero-bar", 1)),
                         ("compose", lambda: service.compose({"preset": "user:zero-bar", "seed": 1, "mode": "vocal", "remember": False})))
                for label, call in calls:
                    try:
                        call()
                        print(label, "no error", flush=True)
                    except Exception as exc:
                        print(label, type(exc).__name__, "|", exc, flush=True)
                store.close()
            """)
        self.assertTrue(finished, f"the request never finished; output so far: {out!r}")
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2, out)
        for line in lines:
            self.assertIn("PromptError", line)
            self.assertIn("meter could not be read", line)
            for jargon in ("Traceback", "ValueError", "parse"):
                self.assertNotIn(jargon, line)


# ---------------------------------------------------------------------------------------------------------
# A saved style's choices depended on the catalog version, against the documented guarantee

class SavedStyleSurvivesCatalogReleasesTests(StoreCase):
    @staticmethod
    def later_library():
        """The shipped catalog plus a made-up 1.2.0 pack (one extra style and one extra design): a fixture."""
        def read(name):
            return json.loads(DATA.joinpath(name).read_text(encoding="utf-8"))
        palettes_old, arrangements_old = read("palettes.json"), read("arrangements.json")
        palettes_mid, arrangements_mid = read("palettes-1.1.0.json"), read("arrangements-1.1.0.json")
        extra_palette = copy.deepcopy(palettes_mid["palettes"][0])
        extra_palette.update(id="synthetic-later-style", name="Synthetic Later Style")
        extra_arrangement = copy.deepcopy(arrangements_mid["arrangements"][0])
        extra_arrangement.update(id="synthetic-later-design", name="Synthetic Later Design")
        palettes_new = {"schema_version": 1, "catalog_version": "1.2.0", "palettes": [extra_palette]}
        arrangements_new = {"schema_version": 1, "catalog_version": "1.2.0", "arrangements": [extra_arrangement]}
        return Library.from_packs([(palettes_old, arrangements_old), (palettes_mid, arrangements_mid), (palettes_new, arrangements_new)])

    def test_the_same_saved_style_and_seed_compose_the_same_choices_in_every_catalog_release(self):
        self.service.save_custom(EDIT)
        preset = self.store.get_user_preset("user:my-porch")
        libraries = {"1.0.0": load_library(through="1.0.0"), "1.1.0": LIBRARY, "1.2.0": self.later_library()}
        self.assertEqual({v: lib.version for v, lib in libraries.items()}, {"1.0.0": "1.0.0", "1.1.0": "1.1.0", "1.2.0": "1.2.0"})
        for seed in range(12):
            for complexity, density, mode, lyrics in (("simple", "sparse", "instrumental", None), ("intricate", "lush", "vocal", "[Verse]\nla")):
                with self.subTest(seed=seed, complexity=complexity):
                    made = {v: lib.compose(preset, seed, complexity=complexity, density=density, mode=mode, lyrics=lyrics)
                            for v, lib in libraries.items()}
                    self.assertEqual({p.style for p in made.values()}, {made["1.1.0"].style})
                    self.assertEqual({p.bpm for p in made.values()}, {made["1.1.0"].bpm})
                    self.assertEqual({p.title for p in made.values()}, {"My porch"})
                    self.assertEqual({v: p.catalog_version for v, p in made.items()}, {v: v for v in libraries},
                                     "the record still says which catalog wrote it")

    def test_other_content_still_changes_the_choices_and_the_seed_still_matters(self):
        self.service.save_custom(EDIT)
        preset = self.store.get_user_preset("user:my-porch")
        styles = {LIBRARY.compose(preset, seed).style for seed in range(20)}
        self.assertGreater(len(styles), 10, "different seeds must still give different choices")


# ---------------------------------------------------------------------------------------------------------
# A style that passed every rule could still be impossible to write out (6,000 characters)

class WrittenOutLengthTests(StoreCase):
    @staticmethod
    def wordy(size, slug="wordy-one"):
        """Every free-text field about ``size`` characters: well under the 24,000 character document cap."""
        long_level = {level: filler(size - 1) + "." for level in ("simple", "layered", "intricate")}
        return full_spec(
            slug, moods=[filler(size), filler(size - 5)], leads=["fiddle " + filler(size - 8), "wooden flute"],
            harmony=dict(long_level), rhythm=dict(long_level), production=filler(size - 1) + ".",
        )

    @staticmethod
    def longest(spec):
        palette = Palette.parse({**spec["palette"], "id": spec["id"]}, "x")
        return longest_style_chars(Preset(palette, LIBRARY.get_arrangement(spec["arrangement"])))

    def edge_specs(self, slug):
        """Two styles that differ by one character of production notes: the first is written out in at most
        exactly MAX_STYLE_CHARS characters, the second in one character more."""
        def with_production(size):
            spec = self.wordy(800, slug)   # about 4,800 characters before the production notes are counted
            spec["palette"]["production"] = filler(size - 1) + "."
            return spec

        base = self.longest(with_production(100))
        size = 100 + (MAX_STYLE_CHARS - base)   # one more character of notes is one more character written out
        at_limit, over = with_production(size), with_production(size + 1)
        self.assertEqual((self.longest(at_limit), self.longest(over)), (MAX_STYLE_CHARS, MAX_STYLE_CHARS + 1))
        return at_limit, over

    def test_a_style_that_cannot_be_written_out_is_refused_when_it_is_saved(self):
        spec = self.wordy(1100)
        self.assertLess(len(json.dumps(spec)), userpresets_module.MAX_DOC_CHARS, "the document cap alone must not be what refuses it")
        error = self.refusal(spec)
        message = str(error)
        self.assertIn("too long for the music maker", message)
        self.assertIn("6,000", message)
        self.assertRegex(message, r"longest version is [\d,]+ characters")
        for internal in ("compiled", "digest", "user:", "CatalogError"):
            self.assertNotIn(internal, message)
        self.assertEqual(self.store.user_presets(), ())

    def test_the_check_is_exact_one_character_either_side_of_the_limit(self):
        at_limit, over = self.edge_specs("edge-style")
        self.assertEqual(self.service.save_custom(at_limit)["id"], "user:edge-style")
        message = str(self.refusal({**over, "id": "edge-over"}))
        self.assertIn("too long for the music maker", message)
        self.assertIn("by about 1 character,", message)
        self.assertIn("6,001 characters", message)

    def test_a_style_written_out_at_the_limit_composes_for_every_seed_and_treatment(self):
        at_limit, _ = self.edge_specs("edge-style")
        self.service.save_custom(at_limit)
        for complexity in ("simple", "layered", "intricate"):
            for density in ("sparse", "balanced", "lush"):
                for mode in ("instrumental", "vocal"):
                    for seed in range(8):
                        result = self.service.compose({"preset": "user:edge-style", "seed": seed, "complexity": complexity,
                                                       "density": density, "mode": mode, "remember": False})
                        self.assertLessEqual(len(result["prompt"]["style"]), MAX_STYLE_CHARS)

    def test_the_longest_possible_style_is_the_longest_of_every_combination_a_seed_can_pick(self):
        """Checked by writing out every combination of mood, lead, support and motif, not by trying seeds."""
        from itertools import product
        from myradio.prompts.library import _write_style
        arrangement = LIBRARY.get_arrangement("slow-tide")
        for palette in LIBRARY.palettes:
            preset = Preset(palette, arrangement)
            for complexity, density, mode in (("intricate", "lush", "vocal"), ("simple", "sparse", "instrumental")):
                brute = max(
                    len(_write_style(palette, arrangement, bpm, mood, lead, support, motif, complexity, density, mode))
                    for mood, lead, support, motif in product(palette.moods, palette.leads, palette.supports, palette.motifs)
                    for bpm in (palette.bpm[0], palette.bpm[1]))
                self.assertEqual(longest_style_chars(preset, (complexity,), (density,), (mode,)), brute, palette.id)
        # and a real seed can never go past it
        preset = LIBRARY.get("celtic-session-reels/canon-and-release")
        bound = longest_style_chars(preset, ("intricate",), ("lush",), ("vocal",))
        for seed in range(300):
            style = LIBRARY.compose(preset, seed, complexity="intricate", density="lush", mode="vocal", lyrics="[Verse]\nla").style
            self.assertLessEqual(len(style), bound)
        everything = max(longest_style_chars(preset, (c,), (d,), (m,)) for c in ("simple", "layered", "intricate")
                         for d in ("sparse", "balanced", "lush") for m in ("instrumental", "vocal"))
        self.assertEqual(longest_style_chars(preset), everything)


class OlderLongStyleTests(StoreCase):
    """A style saved before the length check existed (or put in the file by hand) must not break requests."""

    def put_in_file(self, spec):
        with mock.patch.object(userpresets_module, "longest_style_chars", return_value=100):
            record, _ = build_user_preset(spec, library=LIBRARY, resolve=self.service.resolve)
        self.store.save_user_preset(record)
        return record

    def intricate_only_too_long(self, slug):
        arrangement = arrangement_to_doc(LIBRARY.get_arrangement("canon-and-release"))
        arrangement["sections"]["intricate"] = [f"{filler(990)} part {word}." for word in ("one", "two", "three", "four", "five", "six")]
        spec = full_spec(slug, name="Wordy Reels", tags=["celtic-inspired", "reel", "dance", "wordy-test"])
        spec["arrangement"] = arrangement
        return spec

    def test_the_fixture_is_too_long_only_where_it_should_be(self):
        record = self.put_in_file(self.intricate_only_too_long("wordy-one"))
        preset = self.store.get_user_preset(record.id)
        self.assertGreater(longest_style_chars(preset, ("intricate",)), MAX_STYLE_CHARS)
        self.assertLessEqual(longest_style_chars(preset, ("simple", "layered")), MAX_STYLE_CHARS)

    def test_a_named_style_that_cannot_be_written_gets_a_plain_message_not_an_internal_one(self):
        self.put_in_file(self.intricate_only_too_long("wordy-one"))
        with self.assertRaises(PromptError) as caught:
            self.service.compose({"preset": "user:wordy-one", "seed": 1, "complexity": "intricate", "remember": False})
        message = str(caught.exception)
        self.assertIn("too long for the music maker", message)
        for internal in ("compiled", "could not be written:", "user:wordy-one", "CatalogError"):
            self.assertNotIn(internal, message)
        ok = self.service.compose({"preset": "user:wordy-one", "seed": 1, "complexity": "simple", "remember": False})
        self.assertEqual(ok["id"], "user:wordy-one", "the same style still works where it fits")

    def test_choosing_among_styles_skips_one_that_cannot_be_written_at_this_treatment(self):
        self.put_in_file(self.intricate_only_too_long("wordy-one"))
        self.put_in_file(full_spec("fits-fine", name="Fits Fine Reels", tags=["celtic-inspired", "reel", "dance", "wordy-test"]))
        request = {"tags": ["wordy-test"], "avoid_repeats": False, "remember": False}
        for seed in range(40):
            result = self.service.compose({**request, "seed": seed, "complexity": "intricate"})
            self.assertEqual(result["id"], "user:fits-fine", "the long one is never chosen where it cannot be written")
            self.assertTrue(any("Wordy Reels" in w and "too long" in w for w in result["warnings"]), result["warnings"])
        picked = {self.service.compose({**request, "seed": seed, "complexity": "simple"})["id"] for seed in range(40)}
        self.assertEqual(picked, {"user:wordy-one", "user:fits-fine"}, "at a treatment where it fits, it is a candidate again")

    def test_when_nothing_can_be_written_the_message_says_why(self):
        self.put_in_file(self.intricate_only_too_long("wordy-one"))
        with self.assertRaises(PromptError) as caught:
            self.service.compose({"tags": ["wordy-test"], "seed": 3, "complexity": "intricate", "remember": False})
        message = str(caught.exception)
        self.assertIn("too long to be written out", message)
        self.assertNotIn("No styles match", message)
        self.assertEqual(self.store.history_count(), 0)


# ---------------------------------------------------------------------------------------------------------
# Repeat avoidance was not atomic across concurrent requests

class RepeatAvoidanceUnderThreadsTests(unittest.TestCase):
    def test_requests_started_together_still_get_different_presets_until_the_pool_is_used_up(self):
        for trial in range(3):
            with tempfile.TemporaryDirectory() as directory:
                store = PromptStore.open(Path(directory) / "p.sqlite3")
                service = PromptService(store, library=LIBRARY)
                gate = threading.Barrier(6)
                picked, errors = [], []

                def worker(number):
                    try:
                        gate.wait(10)
                        for step in range(3):
                            picked.append(service.compose({"seed": number * 10 + step, "tags": ["bossa"], "include_user": False})["id"])
                    except Exception as exc:  # reported by the assertion below
                        errors.append(repr(exc))

                threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
                store.close()
                self.assertEqual(errors, [])
                self.assertEqual(len(picked), 18)
                self.assertEqual(len(set(picked)), 18, f"trial {trial}: a preset came round twice before all 18 were used")

    def test_two_services_over_one_store_share_the_same_memory_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PromptStore.open(Path(directory) / "p.sqlite3")
            self.addCleanup(store.close)
            first, second = PromptService(store, library=LIBRARY), PromptService(store, library=LIBRARY)
            self.assertIs(store.exclusive(), store.exclusive())
            picked = [s.compose({"seed": n, "tags": ["bossa"], "include_user": False})["id"] for n in range(6) for s in (first, second)]
            self.assertEqual(len(set(picked)), 12)


# ---------------------------------------------------------------------------------------------------------
# Error messages repeated whatever the caller sent, and the wording was not plain

class BoundedMessagesTests(StoreCase):
    LONG = "x" * 5000

    def paths(self):
        long = self.LONG
        palette = celtic()
        arrangement = arrangement_to_doc(LIBRARY.get_arrangement("canon-and-release"))
        base = "porch-blues-slide/slow-tide"
        return {
            "unknown top-level setting": {"id": "a-b", long: 1},
            "unknown palette key (copy)": {"id": "a-b", "based_on": base, "palette": {long: 1}},
            "unknown palette key (new)": {"id": "a-b", "palette": {**palette, long: 1}, "arrangement": "canon-and-release"},
            "unknown harmony key (copy)": {"id": "a-b", "based_on": base, "palette": {"harmony": {**palette["harmony"], long: "x."}}},
            "unknown harmony key (new)": {"id": "a-b", "palette": {**palette, "harmony": {**palette["harmony"], long: "x."}}, "arrangement": "canon-and-release"},
            "unknown rhythm key (new)": {"id": "a-b", "palette": {**palette, "rhythm": {**palette["rhythm"], long: "x."}}, "arrangement": "canon-and-release"},
            "unknown arrangement key (copy)": {"id": "a-b", "based_on": base, "arrangement": {long: 1}},
            "unknown arrangement key (new)": {"id": "a-b", "palette": palette, "arrangement": {**arrangement, long: 1}},
            "unknown sections key": {"id": "a-b", "palette": palette, "arrangement": {**arrangement, "sections": {**arrangement["sections"], long: ["a."] * 3}}},
            "unknown development key": {"id": "a-b", "palette": palette, "arrangement": {**arrangement, "development": {**arrangement["development"], long: "a."}}},
            "five thousand unknown keys": {"id": "a-b", "palette": {**palette, **{f"k{n}": 1 for n in range(3000)}}, "arrangement": "canon-and-release"},
            "long based_on": {"id": "a-b", "based_on": long},
            "long arrangement id": {"id": "a-b", "based_on": base, "arrangement": long},
            "long id": {"id": long},
            "long title": {"id": "a-b", "based_on": base, "title": long},
            "missing parts, long name": {"id": "a-b", "palette": {"name": long}, "arrangement": "canon-and-release"},
        }

    def test_no_save_path_repeats_more_than_a_little_of_what_was_sent(self):
        for label, spec in self.paths().items():
            with self.subTest(path=label):
                message = str(self.refusal(spec))
                self.assertLess(len(message), 300, f"{label}: {len(message)} characters: {message[:120]}")
                self.assertNotIn("x" * 61, message)

    def test_text_quoted_from_a_failed_rule_is_also_cut_short(self):
        lead = "a steady conga pattern " + "z" * 1500
        message = str(self.refusal(full_spec(leads=[lead, "wooden flute with rolls"])))
        self.assertIn("cannot carry a melody", message)
        self.assertLess(len(message), 500)
        self.assertNotIn("z" * 61, message)
        self.assertIn("...", message)

    def test_unknown_names_are_listed_by_count_when_there_are_many(self):
        message = str(self.refusal({"id": "a-b", "palette": {**celtic(), **{f"k{n:04d}": 1 for n in range(40)}}, "arrangement": "canon-and-release"}))
        self.assertIn("and 35 more", message)

    def test_a_missing_part_is_named_in_words_not_as_a_list(self):
        palette = celtic()
        palette["harmony"].pop("intricate")
        message = str(self.refusal({"id": "a-b", "palette": palette, "arrangement": "canon-and-release"}))
        self.assertIn("harmony", message)
        self.assertIn("intricate", message)
        self.assertIn("missing", message)
        for jargon in ("[", "]", "unknown parts", "palette", "None"):
            self.assertNotIn(jargon, message)
        palette = celtic()
        del palette["family"], palette["tags"]
        message = str(self.refusal({"id": "a-b", "palette": palette, "arrangement": "canon-and-release"}))
        self.assertIn("The style is missing: style family and tags.", message)
        self.assertNotIn("[", message)

    def test_nested_unknown_parts_say_which_part_and_what_is_allowed(self):
        palette = celtic()
        palette["rhythm"]["fourth"] = "Another level."
        message = str(self.refusal({"id": "a-b", "palette": palette, "arrangement": "canon-and-release"}))
        self.assertIn("The rhythm can only have a simple, a layered and an intricate version", message)
        self.assertIn("fourth", message)

    def test_counts_and_object_wording_are_grammatical(self):
        cases = [
            ({"id": "a-b", "based_on": "warm-brush-jazz/slow-tide", "palette": {"tags": []}}, "The tags must be a list with at least 1 entry."),
            ({"id": "a-b", "based_on": "warm-brush-jazz/slow-tide",
              "arrangement": {"sections": {"simple": ["a."], "layered": ["a.", "b.", "c."], "intricate": ["a.", "b.", "c."]}}},
             "The sections must be a list with at least 3 entries."),
            ({"id": "a-b", "based_on": "warm-brush-jazz/slow-tide", "palette": {"harmony": "just words"}},
             "The harmony needs a simple, a layered and an intricate version."),
            ({"id": "a-b", "based_on": "warm-brush-jazz/slow-tide", "palette": {"moods": ["one"]}}, "The moods must be a list with at least 2 entries."),
        ]
        for spec, expected in cases:
            with self.subTest(expected=expected):
                message = str(self.refusal(spec))
                self.assertEqual(message, expected)

    def test_the_catalog_loader_also_bounds_what_it_repeats(self):
        palette = copy.deepcopy(json.loads(DATA.joinpath("palettes-1.1.0.json").read_text(encoding="utf-8"))["palettes"][0])
        palette[self.LONG] = 1
        with self.assertRaises(FieldsError) as caught:
            Palette.parse(palette, "palettes[0]")
        self.assertLess(len(str(caught.exception)), 200)
        self.assertEqual(len(caught.exception.extra), 1, "the names stay available to the caller")
        self.assertEqual(caught.exception.path, "palettes[0]")

    def test_shown_helpers_cut_and_clean(self):
        self.assertEqual(shown("short"), "short")
        self.assertEqual(len(shown("y" * 500)), 60)
        self.assertTrue(shown("y" * 500).endswith("..."))
        self.assertNotIn(ESCAPE, shown("a" + ESCAPE + "b"))
        self.assertNotIn(RIGHT_TO_LEFT_OVERRIDE, shown("a" + RIGHT_TO_LEFT_OVERRIDE + "b"))
        self.assertEqual(shown(LONE_SURROGATE), chr(0xFFFD))
        self.assertEqual(shown_names({"b", "a"}), "a, b")
        self.assertTrue(shown_names({str(n) for n in range(12)}).endswith("and 7 more"))
        self.assertLess(len(shown_names({"z" * 400 for _ in range(3)} | {"y" * 400})), 100)


# ---------------------------------------------------------------------------------------------------------
# Characters that cannot be written down, and text that could add a line to a prompt

class BrokenCharacterTests(StoreCase):
    def test_half_an_emoji_is_refused_politely_everywhere_text_goes_in(self):
        broken = "hello " + LONE_SURROGATE + " world"
        cases = {
            "compose lyrics": lambda: self.service.compose({"seed": 1, "lyrics": "[Verse]\n" + broken, "remember": False}),
            "compose query": lambda: self.service.compose({"seed": 1, "query": broken, "remember": False}),
            "compose tags": lambda: self.service.compose({"seed": 1, "tags": [broken], "remember": False}),
            "save title": lambda: self.service.save_custom({**EDIT, "title": "Blues " + LONE_SURROGATE}),
            "save style name": lambda: self.service.save_custom({**EDIT, "palette": {"name": "Blues " + LONE_SURROGATE}}),
            "save a mood": lambda: self.service.save_custom({**EDIT, "palette": {"moods": ["calm", broken]}}),
            "save an unknown key": lambda: self.service.save_custom({**EDIT, "palette": {broken: 1}}),
        }
        for label, call in cases.items():
            with self.subTest(label), self.assertRaises(PromptError) as caught:
                call()
            self.assertIn("broken character", str(caught.exception))
            self.assertNotIn("codec", str(caught.exception))
        self.assertEqual(self.store.user_presets(), ())
        self.assertEqual(self.store.history_count(), 0)

    def test_the_library_itself_refuses_unwritable_lyrics_with_a_clear_message(self):
        with self.assertRaises(ValueError) as caught:
            LIBRARY.generate("neo-soul/rondo-path", 1, mode="vocal", lyrics="la " + LONE_SURROGATE)
        self.assertNotIsInstance(caught.exception, UnicodeError)
        self.assertIn("broken character", str(caught.exception))

    def test_ordinary_text_in_other_scripts_and_emoji_is_fine(self):
        mine = "[Verse]\nDes étoiles dans nos mains \U0001F319\n\n[Chorus]\n明日は明るい"
        result = self.service.compose({"preset": "neo-soul/rondo-path", "seed": 3, "lyrics": mine, "remember": False})
        self.assertEqual(result["prompt"]["lyrics"], mine)
        saved = self.service.save_custom({**EDIT, "title": "Café Blues \U0001F319"})
        self.assertEqual(saved["title"], "Café Blues \U0001F319")

    def test_the_text_helpers(self):
        self.assertTrue(encodable("plain") and encodable("café") and not encodable(LONE_SURROGATE))
        self.assertTrue(broken_text({"a": ["fine", {"b": LONE_SURROGATE}]}))
        self.assertTrue(broken_text({LONE_SURROGATE: 1}))
        self.assertFalse(broken_text({"a": ["fine", {"b": "café"}], "c": 3, "d": None}))
        deep = []
        for _ in range(5000):
            deep = [deep]
        self.assertFalse(broken_text(deep), "a deep nest is not walked with recursion")


class TidyTextTests(StoreCase):
    """Saved-style text follows the shipped catalog's habit: one tidy line, nothing hidden."""

    def test_odd_characters_and_untidy_spacing_are_refused_in_every_text_field(self):
        untidy = {
            "a line break": "Raw but warm acoustic recording.\nNo drums.",
            "a section line": "Raw but warm acoustic recording.\n[Verse 1]\nNo drums.",
            "a tab": "Raw but warm\tacoustic recording.",
            "doubled spaces": "Raw but  warm acoustic recording.",
            "a no-break space": "Raw but" + NO_BREAK_SPACE + "warm acoustic recording.",
            "a line separator": "Raw but" + LINE_SEPARATOR + "warm acoustic recording.",
            "an escape character": "Raw but warm" + ESCAPE + "[31m acoustic recording.",
            "a direction override": "Raw but warm " + RIGHT_TO_LEFT_OVERRIDE + "acoustic recording.",
            "a null character": "Raw but warm" + chr(0) + " acoustic recording.",
        }
        for label, text in untidy.items():
            for field in ("production", "bass", "name"):
                with self.subTest(label=label, field=field):
                    value = text.rstrip(".") if field != "production" else text
                    error = self.refusal({"id": "text-test", "based_on": "porch-blues-slide/slow-tide", "palette": {field: value}})
                    self.assertTrue(
                        "one line" in str(error) or "spaces at the start or end" in str(error),
                        f"{label} in {field}: {error}")
            with self.subTest(label=label, field="title"):
                self.assertIn("title", str(self.refusal({**EDIT, "title": text})))

    def test_other_places_text_goes_in_are_held_to_the_same_habit(self):
        palette = celtic()
        palette["harmony"]["simple"] = "Stay on one chord.\nThen move."
        self.assertIn("one line", str(self.refusal({"id": "a-b", "palette": palette, "arrangement": "canon-and-release"})))
        arrangement = arrangement_to_doc(LIBRARY.get_arrangement("canon-and-release"))
        arrangement["sections"]["simple"][0] = "A first part.\n[Verse 1]"
        self.assertIn("one line", str(self.refusal({"id": "a-b", "palette": celtic(), "arrangement": arrangement})))
        moods = celtic(moods=["calm", "warm" + LINE_SEPARATOR + "and open"])
        self.assertIn("one line", str(self.refusal({"id": "a-b", "palette": moods, "arrangement": "canon-and-release"})))

    def test_tidy_text_helpers(self):
        self.assertTrue(is_tidy_line("A warm, open sound."))
        self.assertFalse(is_tidy_line("two  spaces"))
        self.assertFalse(is_tidy_line(" padded"))
        self.assertFalse(is_tidy_line("line\nbreak"))
        self.assertTrue(has_odd_character("a" + ESCAPE) and has_odd_character(RIGHT_TO_LEFT_OVERRIDE) and has_odd_character(LINE_SEPARATOR))
        self.assertFalse(has_odd_character("café 明日 \U0001F319 and a plain space"))

    def test_the_whole_shipped_catalog_already_follows_the_habit(self):
        from myradio.prompts.compat import _arrangement_fields, _palette_fields
        for palette in LIBRARY.palettes:
            for name, text in _palette_fields(palette):
                self.assertTrue(is_tidy_line(text), f"{palette.id} {name}")
        for arrangement in LIBRARY.arrangements:
            for name, text in _arrangement_fields(arrangement):
                self.assertTrue(is_tidy_line(text), f"{arrangement.id} {name}")


class HostWordingTests(StoreCase):
    def test_announcer_wording_is_refused_in_style_text_and_titles(self):
        said = [
            "You're listening to the station, welcome back to the show.",
            "Hello listeners, thanks for listening.",
            "Stay tuned for more after the break.",
            "Good evening ladies and gentlemen.",
            "You" + chr(0x2019) + "re listening to us.",   # a curly apostrophe is the same words
            "Brought to you by our sponsor.",
        ]
        for text in said:
            with self.subTest(text=text):
                error = self.refusal({"id": "host-test", "based_on": "porch-blues-slide/slow-tide", "palette": {"production": text}})
                self.assertIn("cannot be saved yet", str(error))
                self.assertEqual({f.rule for f in error.findings}, {"text.speech-markup"})
                self.assertIn("In the production notes", str(error))
        self.assertIn("title", str(self.refusal({**EDIT, "title": "Welcome back listeners"})))
        self.assertIn("speech", str(self.refusal({"id": "host-test", "based_on": "porch-blues-slide/slow-tide", "palette": {"name": "Welcome Back Blues"}})))

    def test_the_message_names_the_words_that_were_found(self):
        error = self.refusal({"id": "host-test", "based_on": "porch-blues-slide/slow-tide",
                              "palette": {"production": "Gentle enough for late-night listeners, and welcome back."}})
        self.assertIn('(found "listeners", "welcome back")', str(error))
        title = str(self.refusal({**EDIT, "title": "Thanks for listening"}))
        self.assertIn('(found "thanks for listening")', title)
        tag = str(self.refusal({"id": "host-test", "based_on": "porch-blues-slide/slow-tide", "palette": {"moods": ["a [laughs] moment", "warm"]}}))
        self.assertIn('found "[laughs]"', tag)

    def test_ordinary_musical_words_that_look_a_little_alike_are_not_refused(self):
        fine = [
            "Warm studio room sound, close and dry.", "The players tune in to each other over a steady pulse.",
            "Radio-friendly length without any edits.", "Low frequency warmth under the melody.",
            "This is a loose, open groove.", "A line coming up from the bass, listening to the drums.",
        ]
        for text in fine:
            with self.subTest(text=text):
                self.assertFalse(speaks_like_a_host(text))
                self.service.save_custom({"id": "host-ok", "based_on": "porch-blues-slide/slow-tide", "palette": {"production": text}})

    def test_arrangements_are_held_to_the_same_wording(self):
        error = self.refusal({"id": "arr-host", "based_on": "neo-soul/rondo-path", "arrangement": {"ending": "Say welcome back to the listeners."}})
        self.assertEqual({f.rule for f in error.findings}, {"text.speech-markup"})

    def test_the_list_is_drawn_from_the_lyric_guard_and_leaves_out_everyday_words(self):
        self.assertTrue(set(HOST_PHRASES) <= set(DJ_SPEECH_PHRASES) | {"d.j."})
        for everyday in ("studio", "radio", "frequency", "tune in", "this is", "listening to", "coming up", "broadcast"):
            self.assertNotIn(everyday, HOST_PHRASES)
        self.assertIn("welcome back", HOST_PHRASES)


# ---------------------------------------------------------------------------------------------------------
# The list of problems did not say where each one was

class ProblemListTests(StoreCase):
    def epic(self):
        sentence = "Build to a huge climax with a crescendo."
        return {"id": "epic-one", "based_on": "warm-brush-jazz/slow-tide", "palette": {
            "harmony": {level: sentence for level in ("simple", "layered", "intricate")},
            "rhythm": {level: sentence for level in ("simple", "layered", "intricate")}}}

    def test_each_problem_says_where_it_is_and_one_sentence_is_said_once(self):
        error = self.refusal(self.epic())
        message = str(error)
        self.assertTrue(message.startswith("This style cannot be saved yet. 1. In the"))
        self.assertEqual(message.count("This text makes the music more complex"), 1)
        self.assertIn("harmony", message)
        self.assertIn("rhythm", message)
        self.assertRegex(message, r"and \d+ more places")

    def test_the_findings_stay_on_the_error_so_a_screen_can_mark_the_fields(self):
        error = self.refusal(self.epic())
        self.assertEqual(len(error.findings), 6)
        self.assertEqual({f.rule for f in error.findings}, {"complexity.loudness"})
        self.assertEqual({f.field.split(".")[0] for f in error.findings}, {"harmony", "rhythm"})
        with self.assertRaises(UserPresetError) as caught:
            build_user_preset(self.epic(), library=LIBRARY, resolve=self.service.resolve)
        self.assertEqual(len(caught.exception.findings), 6)
        self.assertEqual(self.refusal({**EDIT, "title": ""}).findings, ())

    def test_places_are_named_in_words(self):
        palette = celtic(leads=["a steady conga pattern", "wooden flute with rolls"], bass="bright flute figures",
                         moods=["a DJ announcing the next song", "warm evening"])
        message = str(self.refusal({"id": "a-b", "palette": palette, "arrangement": "canon-and-release"}))
        for place in ("lead instrument 1", "the bass part", "mood 1"):
            self.assertIn(place, message)

    def test_arrangement_places_are_named_too(self):
        error = self.refusal({"id": "a-b", "based_on": "neo-soul/rondo-path", "arrangement": {"dynamics": "Slow to 60 BPM, then fade.", "ending": "End on a solo trumpet."}})
        message = str(error)
        self.assertIn("In the dynamics", message)
        self.assertIn("In the ending", message)

    def test_more_than_four_different_problems_are_summarised_by_sentence_not_by_place(self):
        loud = "Build to a huge climax with a crescendo."
        palette = celtic(meter="four/four", bass="bright flute figures", leads=["a steady conga pattern", "wooden flute"],
                         moods=["a DJ announcing", "warm"], rhythm={level: loud for level in ("simple", "layered", "intricate")})
        message = str(self.refusal({"id": "a-b", "palette": palette, "arrangement": "canon-and-release"}))
        # five different problems, one of them in three places: seven findings, but only one more to mention
        self.assertEqual(message.count("(1 more)"), 1, message)
        self.assertEqual(message.count("This text makes the music more complex"), 1)
        self.assertLess(len(message), 2500)


# ---------------------------------------------------------------------------------------------------------
# An exact preset reported the energy that was asked for, not the one it has

class NamedPresetEnergyTests(StoreCase):
    def test_a_named_style_reports_its_own_energy_and_says_so(self):
        preset = LIBRARY.get("ambient-garden/slow-tide")
        self.assertEqual(preset.palette.energy, "calm")
        result = self.service.compose({"preset": preset.id, "energy": "driving", "seed": 1, "remember": False})
        self.assertEqual(result["treatment"]["energy"], "calm")
        self.assertIn("calm energy", result["prompt"]["style"])
        self.assertTrue(any("kept" in w and "calm" in w and "driving" in w for w in result["warnings"]), result["warnings"])

    def test_with_sliders_the_summary_says_the_named_style_was_kept(self):
        sliders = {"energy": 95, "adventurousness": 50, "vocals": 0}
        result = self.service.compose({"preset": "ambient-garden/slow-tide", "sliders": sliders, "seed": 1, "remember": False})
        self.assertEqual(result["treatment"]["energy"], "calm")
        self.assertEqual(result["treatment"]["density"], "lush", "the texture setting still applies to a named style")
        self.assertIn("Lively and full", result["treatment"]["summary"])
        self.assertIn("You named a calm style, so its own energy was kept.", result["treatment"]["summary"])
        same = self.service.compose({"preset": "pocket-funk/slow-tide", "sliders": sliders, "seed": 1, "remember": False})
        self.assertEqual(same["treatment"]["energy"], "driving")
        self.assertNotIn("kept", same["treatment"]["summary"])

    def test_chosen_styles_still_report_the_class_that_was_asked_for(self):
        result = self.service.compose({"seed": 1, "energy": "driving", "remember": False})
        self.assertEqual(result["treatment"]["energy"], "driving")
        self.assertEqual(LIBRARY.get(result["id"]).palette.energy, "driving")


# ---------------------------------------------------------------------------------------------------------
# The "style direction, not authenticity" note was lost on a copy of a copy

class HeritageLineageTests(StoreCase):
    def notes(self, view="presets"):
        items = self.service.catalog(source="user", view=view, limit=100)["items"]
        return {item["id"]: bool(item["heritage_note"]) for item in items}

    def test_a_copy_of_a_copy_of_a_traditional_style_keeps_the_note(self):
        self.service.save_custom({"id": "my-tango", "based_on": "tango-nuevo/slow-tide", "palette": {"name": "My Tango"}})
        self.service.save_custom({"id": "my-tango-two", "based_on": "user:my-tango", "palette": {"bpm": [100, 110]}})
        self.service.save_custom({"id": "my-tango-three", "based_on": "user:my-tango-two", "palette": {"name": "Third Tango"}})
        self.service.save_custom(EDIT)
        expected = {"user:my-tango": True, "user:my-tango-two": True, "user:my-tango-three": True, "user:my-porch": False}
        self.assertEqual(self.notes("presets"), expected)
        self.assertEqual(self.notes("styles"), expected)

    def test_editing_the_heritage_tag_out_does_not_remove_the_note(self):
        self.service.save_custom({"id": "my-kora", "based_on": "kora-river-ostinato/slow-tide", "palette": {"tags": ["plucked", "cycle"]}})
        self.service.save_custom({"id": "my-kora-two", "based_on": "user:my-kora", "palette": {"tags": ["plucked", "cycle", "quiet"]}})
        self.assertNotIn("heritage-inspired", self.store.get_user_preset("user:my-kora").palette.tags)
        self.assertEqual(self.notes(), {"user:my-kora": True, "user:my-kora-two": True})

    def test_removing_a_style_in_the_middle_keeps_the_lineage(self):
        self.service.save_custom({"id": "my-tango", "based_on": "tango-nuevo/slow-tide", "palette": {"name": "My Tango"}})
        self.service.save_custom({"id": "my-tango-two", "based_on": "user:my-tango", "palette": {"bpm": [100, 110]}})
        self.service.remove_custom("user:my-tango")
        self.assertEqual(self.store.user_row("user:my-tango-two")["based_on"], "tango-nuevo/slow-tide")
        self.assertEqual(self.notes(), {"user:my-tango-two": True})
        self.service.save_custom({"id": "fresh-one", "palette": celtic(name="Fresh One"), "arrangement": "canon-and-release"})
        self.service.save_custom({"id": "fresh-two", "based_on": "user:fresh-one", "palette": {"bpm": [100, 110]}})
        self.service.remove_custom("user:fresh-one")
        self.assertIsNone(self.store.user_row("user:fresh-two")["based_on"], "a style copied from a brand new one has no parent left")

    def test_a_loop_in_the_lineage_ends(self):
        self.service.save_custom({**EDIT, "id": "style-a"})
        self.service.save_custom({**EDIT, "id": "style-b", "based_on": "user:style-a", "palette": {"name": "Style B"}})
        self.service.save_custom({**EDIT, "id": "style-a", "based_on": "user:style-b", "palette": {"name": "Style A Again"}})
        self.assertEqual(self.notes(), {"user:style-a": False, "user:style-b": False})


# ---------------------------------------------------------------------------------------------------------
# The first-run setup could be left half done, and a refused file kept its connection open

class StoreSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prompts.sqlite3"

    def inspect(self):
        raw = sqlite3.connect(self.path)
        try:
            tables = sorted(row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            return tables, raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_file_left_half_built_is_finished_on_the_next_start(self):
        raw = sqlite3.connect(self.path)
        raw.execute(  # what an interrupted first start left behind: the first table only, version number still 0
            "CREATE TABLE user_presets(slug TEXT PRIMARY KEY, title TEXT NOT NULL, based_on TEXT, palette TEXT NOT NULL, "
            "arrangement TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")
        raw.commit()
        raw.close()
        self.assertEqual(self.inspect(), (["user_presets"], 0))
        store = PromptStore.open(self.path)
        self.addCleanup(store.close)
        self.assertEqual(self.inspect(), (["favorites", "history", "user_presets"], 1))
        service = PromptService(store, library=LIBRARY)
        service.save_custom(EDIT)
        service.favorite("neo-soul", True)
        self.assertEqual(service.compose({"seed": 2})["remembered"], True)

    def test_a_setup_that_fails_part_way_leaves_nothing_behind(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)

        def refuse_the_history_table(action, arg1, arg2, database, source):
            sqlite_deny, sqlite_ok, sqlite_create_table = 1, 0, 2
            return sqlite_deny if (action == sqlite_create_table and arg1 == "history") else sqlite_ok

        conn.set_authorizer(refuse_the_history_table)
        with self.assertRaises(StoreError):
            PromptStore(conn)
        with self.assertRaises(sqlite3.ProgrammingError):   # the store closed the connection it could not use
            conn.execute("SELECT 1")
        self.assertEqual(self.inspect(), ([], 0), "no table and no version number: all or nothing")
        store = PromptStore.open(self.path)   # a later start does the whole job
        store.close()
        self.assertEqual(self.inspect(), (["favorites", "history", "user_presets"], 1))

    def test_reopening_a_finished_file_changes_nothing(self):
        PromptStore.open(self.path).close()
        before = self.inspect()
        PromptStore.open(self.path).close()
        self.assertEqual(self.inspect(), before)


class ConnectionHygieneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prompts.sqlite3"

    def newer_file(self):
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA user_version=99")
        raw.commit()
        raw.close()

    def assertClosed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_a_store_that_refuses_a_newer_file_closes_the_connection_it_made(self):
        self.newer_file()
        made, real = [], sqlite3.connect

        def spy(*args, **kwargs):
            conn = real(*args, **kwargs)
            made.append(conn)
            return conn

        with mock.patch.object(sqlite3, "connect", side_effect=spy):
            with self.assertRaisesRegex(StoreError, "newer myradio"):
                PromptStore.open(self.path)
        self.assertEqual(len(made), 1)
        self.assertClosed(made[0])

    def test_a_connection_handed_to_a_store_that_cannot_use_it_is_closed(self):
        self.newer_file()
        conn = sqlite3.connect(self.path, check_same_thread=False)
        with self.assertRaises(StoreError):
            PromptStore(conn)
        self.assertClosed(conn)

    def test_a_file_that_is_not_a_database_closes_its_connection_too(self):
        self.path.write_bytes(b"this is not a database file " * 40)
        made, real = [], sqlite3.connect

        def spy(*args, **kwargs):
            conn = real(*args, **kwargs)
            made.append(conn)
            return conn

        with mock.patch.object(sqlite3, "connect", side_effect=spy):
            with self.assertRaises(StoreError):
                PromptStore.open(self.path)
        self.assertEqual(len(made), 1)
        self.assertClosed(made[0])

    def test_an_unexpected_fault_while_starting_also_closes_the_connection(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        with mock.patch.object(store_module, "_migrate", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                PromptStore(conn)
        self.assertClosed(conn)


class RecordsTheStoreCannotReadBackTests(StoreCase):
    """The store never writes a style it would then set aside as damaged, and it sets aside (never deletes) one
    that an older build saved with text the rules now refuse."""

    @staticmethod
    def record(production, slug="old-style"):
        base = LIBRARY.get("porch-blues-slide/slow-tide")
        palette = palette_to_doc(base.palette)
        palette.update(id=slug, production=production)
        arrangement = arrangement_to_doc(base.arrangement)
        from myradio.prompts.userpresets import UserPresetRecord, digest_of
        return UserPresetRecord(slug, "Old style", None, palette, arrangement, digest_of(palette, arrangement))

    def test_a_record_that_cannot_be_read_back_is_refused_politely_and_leaves_the_file_alone(self):
        before = self.store.counts()
        with self.assertRaises(StoreError) as caught:
            self.store.save_user_preset(self.record("Raw but warm.\nNo drums."))
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertNotIsInstance(caught.exception, KeyError)
        self.assertEqual(self.store.counts(), before)
        self.assertEqual((self.store.user_presets(), self.store.damaged), ((), ()))
        self.assertEqual(self.store.save_user_preset(self.record("Raw but warm. No drums."))["revision"], 1)

    def test_a_style_saved_by_an_older_build_with_a_line_break_is_set_aside_not_lost(self):
        legacy = self.record("Raw but warm.\nNo drums.")
        self.store.close()
        raw = sqlite3.connect(self.path)    # what the older build wrote: it did not mind the line break
        raw.execute("INSERT INTO user_presets(slug, title, based_on, palette, arrangement, digest, revision, created, updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (legacy.slug, legacy.title, None, json.dumps(legacy.palette), json.dumps(legacy.arrangement), legacy.digest, 1, 1.0, 1.0))
        raw.commit()
        raw.close()
        store = PromptStore.open(self.path)
        self.addCleanup(store.close)
        self.assertEqual((store.damaged, store.user_presets()), (("old-style",), ()))
        service = PromptService(store, library=LIBRARY)
        self.assertEqual(service.snapshot()["damaged"], ["old-style"])
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM user_presets").fetchone()[0], 1, "still in the file")
        raw.close()


# ---------------------------------------------------------------------------------------------------------
# A copy of just the database file must hold the saved styles and favourites (the lane's backup promise)

class PlainCopyTests(StoreCase):
    def copy_rows(self):
        copy_path = Path(self.temp.name) / "copy.sqlite3"
        copy_path.write_bytes(self.path.read_bytes())   # the main file only: no -wal and no -shm beside it
        raw = sqlite3.connect(copy_path)
        try:
            return (sorted(r[0] for r in raw.execute("SELECT slug FROM user_presets")),
                    sorted(r[0] for r in raw.execute("SELECT id FROM favorites")))
        finally:
            raw.close()

    def test_a_plain_copy_taken_while_the_store_is_open_is_complete_after_every_change(self):
        self.assertEqual(self.store._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.service.save_custom(EDIT)
        self.assertEqual(self.copy_rows(), (["my-porch"], []))
        self.service.favorite("neo-soul", True)
        self.assertEqual(self.copy_rows(), (["my-porch"], ["neo-soul"]))
        self.service.favorite("neo-soul", False)
        self.assertEqual(self.copy_rows(), (["my-porch"], []))
        self.service.save_custom({**EDIT, "id": "my-porch-two"})
        self.service.remove_custom("user:my-porch")
        self.assertEqual(self.copy_rows(), (["my-porch-two"], []))


# ---------------------------------------------------------------------------------------------------------
# The cached status went stale when other code composed directly (the service now says when it changes)

class ChangeNotificationTests(StoreCase):
    def test_the_service_tells_its_owner_after_every_change_and_not_after_a_preview_or_a_refusal(self):
        calls = []
        service = PromptService(self.store, library=LIBRARY, on_change=lambda: calls.append(1))
        service.compose({"seed": 1, "remember": False})
        self.assertEqual(len(calls), 0, "a preview changes nothing")
        service.compose({"seed": 1})
        self.assertEqual(len(calls), 1)
        service.save_custom(EDIT)
        service.favorite("neo-soul", True)
        service.favorite("user:my-porch", True)
        service.favorite("neo-soul", False)
        service.remove_custom("user:my-porch")
        self.assertEqual(len(calls), 6)
        for refused in (lambda: service.favorite("nothing", True), lambda: service.save_custom({"id": "Bad Id"}),
                        lambda: service.remove_custom("user:ghost"), lambda: service.compose({"colour": 1})):
            with self.assertRaises(PromptError):
                refused()
        self.assertEqual(len(calls), 6, "a refusal changes nothing")

    def test_a_failing_status_refresh_never_turns_a_finished_change_into_a_failure(self):
        def broken():
            raise RuntimeError("status could not be rebuilt")

        service = PromptService(self.store, library=LIBRARY, on_change=broken)
        self.assertTrue(service.compose({"seed": 1})["remembered"])
        self.assertEqual(self.store.history_count(), 1)
        self.assertEqual(service.save_custom(EDIT)["id"], "user:my-porch")

    def test_the_owner_is_told_after_the_memory_lock_is_released(self):
        seen = []

        def check():
            def other_thread():
                acquired = self.store.exclusive().acquire(timeout=3)
                seen.append(acquired)
                if acquired:
                    self.store.exclusive().release()

            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(10)

        service = PromptService(self.store, library=LIBRARY, on_change=check)
        service.compose({"seed": 2})
        self.assertEqual(seen, [True], "the callback ran while still holding the lock, so another thread could not get it")

    def test_without_a_store_nothing_needs_a_lock_or_a_callback(self):
        calls = []
        service = PromptService(None, library=LIBRARY, on_change=lambda: calls.append(1))
        self.assertFalse(service.compose({"seed": 1})["remembered"])
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------------------------------------
# The documents said things the code did not do

class DocumentationTests(unittest.TestCase):
    LANE = (ROOT / "docs" / "lanes" / "prompts.md").read_text(encoding="utf-8")
    PUBLIC = (ROOT / "docs" / "PROMPTS.md").read_text(encoding="utf-8")

    def test_every_quoted_size_of_the_instrument_lexicon_is_the_real_one(self):
        quoted = set(re.findall(r"(\d+) (?:instrument )?patterns", self.LANE))
        self.assertEqual(quoted, {str(len(INSTRUMENTS))}, "the lane contract quotes the lexicon size; keep it in step")

    def test_the_syllable_claim_is_about_rhymed_lines_and_says_the_hook_keeps_its_own_length(self):
        self.assertNotIn("Every generated line has exactly that many syllables", self.PUBLIC)
        self.assertIn("The **recurring hook keeps its own length**", self.PUBLIC)
        scaffold = lyrics_scaffold("neo-soul/rondo-path", 8, form="full")
        hooks = {line.syllables for section in scaffold.sections for line in section.lines if line.rhyme == "H"}
        rhymed = {line.syllables for section in scaffold.sections for line in section.lines if line.rhyme != "H"}
        self.assertEqual(rhymed, {scaffold.syllables_per_line})
        self.assertEqual(hooks, {scaffold.motif_syllables})
        self.assertNotEqual(hooks, rhymed, "in a 4/4 style the hook really is shorter than the other lines, as the document now says")
        self.assertTrue({6, 7} >= {c for world in WORLDS for _, c, _ in world.motifs}, "the document says hooks are 6 or 7 syllables")

    def test_the_documents_state_the_limits_the_code_enforces(self):
        self.assertIn("2,281 characters", self.PUBLIC)
        peak = max(longest_style_chars(preset) for preset in LIBRARY.presets)
        self.assertEqual(peak, 2281)
        for text in (self.PUBLIC, self.LANE):
            self.assertIn("60 characters", text)
        self.assertIn("never by the catalog version", self.PUBLIC)


# ---------------------------------------------------------------------------------------------------------
# Behaviour the existing tests did not pin down: found by breaking the code on purpose and seeing nothing fail

class ComposeOptionsAreHonouredTests(StoreCase):
    def picks(self, count=200, **request):
        base = {"avoid_repeats": False, "remember": False, "include_user": False}
        return [self.service.compose({**base, "seed": seed, **request}) for seed in range(count)]

    def test_exclude_tags_removes_every_style_that_has_the_tag(self):
        everything = {r["id"].split("/")[0] for r in self.picks(tags=["jazz"])}
        self.assertIn("jazz-waltz", everything, "without the exclusion a waltz does come up, so the next check can fail")
        trimmed = {r["id"].split("/")[0] for r in self.picks(tags=["jazz"], exclude_tags=["waltz"])}
        self.assertTrue(trimmed)
        for style in trimmed:
            self.assertNotIn("waltz", LIBRARY.get_palette(style).tags, style)
        with self.assertRaisesRegex(PromptError, "No styles match"):
            self.service.compose({"seed": 1, "tags": ["jazz"], "exclude_tags": ["jazz"], "remember": False})

    def test_the_search_words_narrow_the_choice(self):
        words = "celtic fiddle"
        allowed = {p.id for p in LIBRARY.browse(query=words)}
        self.assertTrue(0 < len(allowed) < len(LIBRARY.presets) // 10)
        chosen = {r["id"] for r in self.picks(query=words)}
        self.assertTrue(chosen and chosen <= allowed, chosen - allowed)
        unrestricted = {r["id"] for r in self.picks()}
        self.assertFalse(unrestricted <= allowed, "without the words, other styles come up")
        with self.assertRaisesRegex(PromptError, "No styles match"):
            self.service.compose({"seed": 1, "query": "zzzzzz", "remember": False})

    def test_family_and_style_names_narrow_the_choice(self):
        celtic_styles = {p.id for p in LIBRARY.palettes if p.family == "celtic"}
        picked = {r["id"].split("/")[0] for r in self.picks(60, family="celtic")}
        self.assertTrue(picked and picked <= celtic_styles, picked)
        named = {r["id"].split("/")[0] for r in self.picks(60, styles=["neo-soul", "deep-house"])}
        self.assertEqual(named, {"neo-soul", "deep-house"})

    def test_the_outline_follows_the_requested_tag_style_and_form(self):
        base = {"preset": "neo-soul/rondo-path", "seed": 3, "mode": "vocal", "remember": False}
        plain = self.service.compose(base)["lyrics"]["text"]
        self.assertTrue(plain.startswith("[Verse 1]\n"))
        self.assertEqual(plain.count("["), 5)
        self.assertNotIn("[Bridge]", plain)
        lower_full = self.service.compose({**base, "tag_style": "lower", "form": "full"})["lyrics"]
        self.assertTrue(lower_full["text"].startswith("[verse]\n"))
        self.assertIn("[bridge]", lower_full["text"])
        self.assertEqual(lower_full["text"].count("["), 7)
        self.assertEqual((lower_full["scaffold"]["tag_style"], lower_full["scaffold"]["form"]), ("lower", "full"))
        long_by_default = {**base, "preset": "warm-brush-jazz/verse-and-refrain"}
        self.assertEqual(self.service.compose(long_by_default)["lyrics"]["text"].count("["), 7)
        self.assertEqual(self.service.compose({**long_by_default, "form": "short"})["lyrics"]["text"].count("["), 5)

    def test_saved_styles_are_left_out_when_asked(self):
        base = LIBRARY.get_palette("porch-blues-slide")
        self.service.save_custom({"id": "only-mine", "based_on": "porch-blues-slide/slow-tide",
                                  "palette": {"name": "Only Mine", "tags": [*base.tags, "only-mine-tag"]}})
        mine = self.service.compose({"tags": ["only-mine-tag"], "seed": 1, "remember": False})
        self.assertEqual(mine["id"], "user:only-mine")
        self.assertEqual(mine["selection"]["candidates"], 1)
        with self.assertRaisesRegex(PromptError, "No styles match"):
            self.service.compose({"tags": ["only-mine-tag"], "seed": 1, "include_user": False, "remember": False})
        explicit = self.service.compose({"preset": "user:only-mine", "seed": 1, "include_user": False, "remember": False})
        self.assertEqual(explicit["id"], "user:only-mine", "naming a style outranks the wish to leave saved styles out")


class RepeatMemoryWindowsTests(unittest.TestCase):
    """The windows in the sliders table are really what the memory applies: styles, designs and presets."""

    WINDOWS = {None: (200, 6, 3), 0: (100, 2, 1), 50: (200, 6, 3), 100: (400, 12, 5)}   # preset, style, design

    def test_nothing_inside_a_window_comes_round_while_the_pool_allows(self):
        for adventurousness, (preset_window, style_window, design_window) in self.WINDOWS.items():
            with self.subTest(adventurousness=adventurousness), tempfile.TemporaryDirectory() as directory:
                store = PromptStore.open(Path(directory) / "p.sqlite3")
                service = PromptService(store, library=LIBRARY)
                history = []
                for number in range(45):
                    request = {"seed": number, "include_user": False}
                    if adventurousness is not None:
                        request["sliders"] = {"energy": 50, "adventurousness": adventurousness, "vocals": 0}
                    result = service.compose(request)
                    preset = result["id"]
                    style, design = preset.split("/")
                    self.assertIsNone(result["selection"]["relaxed"], f"compose {number}: the pool is big enough, nothing should relax")
                    self.assertNotIn(preset, [h[0] for h in history[-preset_window:]])
                    self.assertNotIn(style, [h[1] for h in history[-style_window:]], f"compose {number}: a recent style came back")
                    self.assertNotIn(design, [h[2] for h in history[-design_window:]], f"compose {number}: a recent design came back")
                    history.append((preset, style, design))
                store.close()

    def test_the_store_reports_the_newest_distinct_designs_and_styles_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PromptStore.open(Path(directory) / "p.sqlite3")
            self.addCleanup(store.close)
            order = [("neo-soul", "arch-form"), ("deep-house", "arch-form"), ("neo-soul", "rondo-path"), ("dub-techno", "slow-tide"),
                     ("deep-house", "slow-tide"), ("ambient-garden", "evolving-cycle")]
            for style, design in order:
                store.remember(LIBRARY.generate(f"{style}/{design}", 1), style)
            presets, styles, designs = store.recent_sets(10, 10, 10)
            self.assertEqual(styles, ["ambient-garden", "deep-house", "dub-techno", "neo-soul"])
            self.assertEqual(designs, ["evolving-cycle", "slow-tide", "rondo-path", "arch-form"])
            self.assertEqual(presets[:2], ["ambient-garden/evolving-cycle", "deep-house/slow-tide"])
            _, few_styles, few_designs = store.recent_sets(10, 2, 3)
            self.assertEqual((few_styles, few_designs), (["ambient-garden", "deep-house"], ["evolving-cycle", "slow-tide", "rondo-path"]))
            self.assertEqual(store.recent_sets(2, 10, 10)[0], ["ambient-garden/evolving-cycle", "deep-house/slow-tide"])


class SavedArrangementsFollowTheDesignRulesTests(StoreCase):
    """build_user_preset checks a saved arrangement with the same rules as the shipped designs."""

    CASES = [
        ("arrangement.instrument", {"ending": "End on a solo trumpet."}, "In the ending"),
        ("arrangement.instrument", {"sections": {"simple": ["Open with a lone piano.", "Answer it.", "Return."],
                                                  "layered": ["Open.", "Answer.", "Return."], "intricate": ["Open.", "Answer.", "Return."]}},
         "section 1 of the simple version"),
        ("arrangement.tempo-meter", {"dynamics": "Slow to 60 BPM, then fade."}, "In the dynamics"),
        ("arrangement.tempo-meter", {"dynamics": "Settle into 3/4 for the close."}, "In the dynamics"),
        ("arrangement.tempo-meter", {"development": {"simple": "Use a five-beat answer for each call.",
                                                      "layered": "Answer each call with a variation.", "intricate": "Answer each call in new registers."}},
         "the simple development"),
        ("complexity.loudness", {"development": {"simple": "Answer each call with a variation.", "layered": "Answer each call with a variation.",
                                                  "intricate": "Build to a huge climax that gets louder."}}, "the intricate development"),
        ("complexity.loudness", {"dynamics": "Rise to a climax with a crescendo."}, "In the dynamics"),
        ("text.speech-markup", {"ending": "Let the announcer speak over the quiet part."}, "In the ending"),
    ]

    def test_each_rule_refuses_a_saved_arrangement_with_a_plain_message(self):
        for rule, change, place in self.CASES:
            with self.subTest(rule=rule, place=place):
                error = self.refusal({"id": "arr-test", "based_on": "neo-soul/rondo-path", "arrangement": change})
                self.assertIn("cannot be saved yet", str(error))
                self.assertIn(place, str(error))
                self.assertEqual({f.rule for f in error.findings}, {rule})
                self.assertTrue(all(f.subject == "arrangement:arr-test" for f in error.findings))
        self.assertEqual(self.store.user_presets(), ())

    def test_a_whole_new_arrangement_is_checked_too_and_a_good_one_is_saved(self):
        arrangement = arrangement_to_doc(LIBRARY.get_arrangement("canon-and-release"))
        good = self.service.save_custom({"id": "arr-good", "palette": celtic(), "arrangement": arrangement})
        self.assertEqual(good["warnings"], [])
        arrangement["ending"] = "End on a solo trumpet."
        error = self.refusal({"id": "arr-bad", "palette": celtic(), "arrangement": arrangement})
        self.assertEqual({f.rule for f in error.findings}, {"arrangement.instrument"})


if __name__ == "__main__":
    unittest.main()
