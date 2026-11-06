"""The lyric scaffold: a separate, generated outline for sung music. Never the operator's own lyrics,
never DJ or announcer speech, never speech markup.

Syllable counts are checked against an independent heuristic counter and rhymes against the
spelling of their sound, so a typo in the word bank cannot pass silently."""
import unittest

from myradio.prompts import PromptService, load_library, lyrics_scaffold
from myradio.prompts.lyric_words import PREPOSITIONS, PRONOUNS, RHYME_GROUPS, VERBS_I, VERBS_T, WORLDS
from myradio.prompts.lyrics import (
    FORMS, MAX_SCAFFOLD_CHARS, TAG_STYLES, _FRAMES, _LITERALS, _solutions, contains_dj_speech, count_syllables,
    line_syllables_for_meter, validate_scaffold_text,
)

TAILS = {  # how each rhyme group is spelled: a typo that puts a word in the wrong group fails here
    "im": r"im$", "ide": r"ide$", "ore": r"(ore|oor|oar)$", "ain": r"(ain|ane)$", "ay": r"(ay|eigh)$",
    "oon": r"(oon|une)$", "un": r"(un|one)$", "ome": r"(ome|omb|oam)$", "ight": r"(ight|ite)$", "old": r"old$",
    "ee": r"(ee|ea|ey)$", "ing": r"ing$", "y": r"(y|igh|ie|ye)$", "ow": r"ow$", "own": r"own$", "ar": r"ar$",
    "eam": r"eam$", "ill": r"ill$", "ove": r"ove$", "ake": r"(ake|eak)$", "ong": r"ong$", "ound": r"ound$", "ine": r"(ine|ign)$",
}
WORD_TO_GROUP = {end.word: end.group for group in RHYME_GROUPS.values() for end in group}


class WordBankTests(unittest.TestCase):
    def test_declared_syllable_counts_agree_with_an_independent_counter(self):
        for world in WORLDS:
            for kind, table in (("noun", world.nouns), ("adjective", world.adjectives)):
                for syllables, words in table.items():
                    for word in words:
                        self.assertEqual(count_syllables(word), syllables, f"{world.id} {kind} {word}")
        for table, kind in ((VERBS_T, "transitive"), (VERBS_I, "intransitive")):
            for syllables, pairs in table.items():
                for base, third in pairs:
                    self.assertEqual(count_syllables(base), syllables, f"{kind} {base}")
                    self.assertEqual(count_syllables(third), syllables, f"{kind} {third}: the -s form keeps the syllable count")
        for syllables, phrases in PREPOSITIONS.items():
            for phrase in phrases:
                self.assertEqual(sum(count_syllables(w) for w in phrase.split()), syllables, phrase)
        for group in RHYME_GROUPS.values():
            for end in group:
                self.assertEqual(count_syllables(end.word), end.syllables, end.word)
        for phrase, count in _LITERALS.items():
            self.assertEqual(sum(count_syllables(w) for w in phrase.split()), count, phrase)
        for world in WORLDS:
            for phrase, count, group in world.motifs:
                self.assertEqual(sum(count_syllables(w) for w in phrase.split()), count, phrase)

    def test_every_rhyme_group_word_is_spelled_like_its_group(self):
        self.assertEqual(set(TAILS), set(RHYME_GROUPS))
        for name, group in RHYME_GROUPS.items():
            self.assertGreaterEqual(len({e.word for e in group}), 4, name)
            for end in group:
                self.assertRegex(end.word, TAILS[name], f"{end.word} does not belong in {name}")

    def test_every_hook_ends_on_a_word_of_its_rhyme_group_with_room_for_three_more(self):
        for world in WORLDS:
            self.assertGreaterEqual(len(world.motifs), 5)
            for phrase, count, group in world.motifs:
                last = phrase.rsplit(" ", 1)[-1]
                self.assertIn(last, {e.word for e in RHYME_GROUPS[group]}, phrase)
                self.assertGreaterEqual(len({e.word for e in RHYME_GROUPS[group]}) - 1, 4, f"{group} needs words for the chorus and outro")
                self.assertTrue(5 <= count <= 8)

    def test_nothing_in_the_bank_sounds_like_a_host_an_announcer_or_a_station(self):
        words = set()
        for world in WORLDS:
            words.update(w for table in (world.nouns, world.adjectives) for group in table.values() for w in group)
            words.update(phrase for phrase, _, _ in world.motifs)
        for table in (VERBS_T, VERBS_I):
            words.update(w for pairs in table.values() for pair in pairs for w in pair)
        words.update(PRONOUNS)
        words.update(p for group in PREPOSITIONS.values() for p in group)
        words.update(e.word for group in RHYME_GROUPS.values() for e in group)
        words.update(_LITERALS)
        for word in words:
            self.assertEqual(contains_dj_speech(word), [], word)
        # and no pair of neighbouring words in a frame forms such a phrase
        self.assertEqual(contains_dj_speech(" ".join(sorted(words))), [])

    def test_every_frame_uses_only_known_slots_and_the_agreement_matches_its_verbs(self):
        slots = {"P", "D", "SA?", "OA?", "SN", "ON", "VT", "VI", "PT", "E"} | set(_LITERALS)
        for pos, frames in _FRAMES.items():
            self.assertIn(pos, "nav")
            for agreement, frame in frames:
                self.assertIn(agreement, ("pron", "np", "imp"))
                self.assertEqual(frame[-1], "E")
                self.assertTrue(set(frame) <= slots, frame)

    def test_every_end_word_can_end_a_line_of_every_length_the_meters_ask_for(self):
        for group in RHYME_GROUPS.values():
            for end in group:
                for target in range(6, 11):
                    found = any(next(_solutions(frame, target, end), None) is not None for _, frame in _FRAMES[end.pos])
                    self.assertTrue(found, f"no wording for {end.word} at {target} syllables")


class GuardTests(unittest.TestCase):
    def test_the_guard_finds_host_wording_and_ignores_ordinary_lines(self):
        for bad in ("Stay tuned for more", "you're listening to the best", "This is the station", "A DJ on the air",
                    "welcome back to the show", "tune in tonight", "coming up next: a song"):
            with self.subTest(bad=bad):
                self.assertTrue(contains_dj_speech(bad), bad)
        for fine in ("The lantern learns to swim", "right afternoon", "the studious moon", "We sing to the sea", "an announcement of rain"):
            with self.subTest(fine=fine):
                self.assertEqual(contains_dj_speech(fine), [], fine)

    def test_validation_rejects_markup_unknown_tags_and_oversize_text(self):
        good = "[Verse 1]\nThe moon sings\n\n[Chorus]\nA candle in the bay"
        self.assertEqual(validate_scaffold_text(good), [])
        for bad in ("", "x" * (MAX_SCAFFOLD_CHARS + 1), "[Verse 1]\n<|speaker:1|> hello", "[Intro]\nla", "[Verse 1]\nwhat [laughs] now",
                    "[Chorus]\nYou are listening to the radio", "[verse 1]\nlower-case numbered tag"):
            with self.subTest(bad=bad[:30]):
                self.assertTrue(validate_scaffold_text(bad), bad[:30])


class ScaffoldStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = load_library()

    def test_a_short_and_a_full_form_with_the_expected_tags(self):
        short = lyrics_scaffold("warm-brush-jazz/theme-and-return", 1)
        self.assertEqual((short.form, [s.tag for s in short.sections]),
                         ("short", ["[Verse 1]", "[Chorus]", "[Verse 2]", "[Chorus]", "[Outro]"]))
        full = lyrics_scaffold("warm-brush-jazz/verse-and-refrain", 1)  # an arrangement with five or more sections
        self.assertEqual((full.form, [s.tag for s in full.sections]),
                         ("full", ["[Verse 1]", "[Chorus]", "[Verse 2]", "[Chorus]", "[Bridge]", "[Chorus]", "[Outro]"]))
        forced = lyrics_scaffold("warm-brush-jazz/theme-and-return", 1, form="full")
        self.assertEqual(len(forced.sections), 7)
        lower = lyrics_scaffold("warm-brush-jazz/theme-and-return", 1, tag_style="lower")
        self.assertEqual([s.tag for s in lower.sections], ["[verse]", "[chorus]", "[verse]", "[chorus]", "[outro]"])

    def test_the_text_is_in_the_format_the_music_model_is_given(self):
        scaffold = lyrics_scaffold("kora-river-ostinato/slow-tide", 4, form="full")
        text = scaffold.text
        blocks = text.split("\n\n")
        self.assertEqual(len(blocks), 7)
        for block, section in zip(blocks, scaffold.sections):
            lines = block.split("\n")
            self.assertEqual(lines[0], section.tag)
            self.assertRegex(lines[0], r"^\[[^\]\n]{2,60}\]$")  # a tag on its own line, as the reference station requires
            self.assertEqual(len(lines) - 1, len(section.lines))
            self.assertTrue(all(line.strip() == line and line for line in lines))
        self.assertTrue(20 <= len(text) <= MAX_SCAFFOLD_CHARS)
        self.assertNotRegex(text, r"[<>|]")
        self.assertEqual(validate_scaffold_text(text), [])
        self.assertFalse(text.endswith("\n"))

    def test_every_line_has_exactly_the_planned_number_of_syllables(self):
        for preset_id, seed in (("warm-brush-jazz/theme-and-return", 1), ("kopanitsa-village-dance/verse-and-refrain", 2),
                                ("celesta-lullaby/slow-tide", 3), ("raga-inspired-evening/gradual-bloom", 4),
                                ("kora-river-ostinato/canon-and-release", 5), ("choro-roda-afternoon/expanding-phrases", 6)):
            scaffold = lyrics_scaffold(preset_id, seed)
            for section in scaffold.sections:
                for line in section.lines:
                    counted = sum(count_syllables(word) for word in line.text.split())
                    self.assertEqual(counted, line.syllables, f"{preset_id}: {line.text}")
                    if line.rhyme != "H":
                        self.assertEqual(line.syllables, scaffold.syllables_per_line)
                    else:
                        self.assertEqual(line.syllables, scaffold.motif_syllables)

    def test_syllables_per_line_follow_the_meter(self):
        table = {"4/4": 8, "3/4": 6, "6/8": 6, "9/8 (3+3+3)": 9, "12/8 (3+3+3+3)": 6, "7/8 (2+2+3)": 7, "11/8 (2+2+3+2+2)": 6,
                 "5/4 (3+2)": 10, "10/8 (2+3+2+3)": 10, "2/4": 8, "8/4 (4+2+2)": 8, "3/8": 6, "6/4": 6}
        for meter, expected in table.items():
            with self.subTest(meter=meter):
                self.assertEqual(line_syllables_for_meter(meter), expected)
        for preset in self.library.presets[::13]:
            self.assertEqual(lyrics_scaffold(preset, 3).syllables_per_line, line_syllables_for_meter(preset.palette.meter))
        with self.assertRaises(ValueError):
            line_syllables_for_meter("free time")

    def test_rhyme_plan_lines_with_one_letter_rhyme_and_different_letters_do_not(self):
        for seed in range(30):
            scaffold = lyrics_scaffold(self.library.presets[seed * 41].id, seed, form="full")
            hook_last = scaffold.motif.rsplit(" ", 1)[-1]
            hook_group = WORD_TO_GROUP[hook_last]
            ends_used = []
            seen_chorus = False
            for section in scaffold.sections:
                self.assertEqual(section.scheme, "".join(line.rhyme for line in section.lines))
                if section.kind == "chorus":
                    if seen_chorus:
                        continue  # the chorus is sung the same way each time, so its end words are not new
                    seen_chorus = True
                groups = {}
                for line in section.lines:
                    last = line.text.rsplit(" ", 1)[-1].lower()
                    if line.rhyme == "H":
                        self.assertEqual(line.text.lower(), scaffold.motif)
                        continue
                    self.assertIn(last, WORD_TO_GROUP, f"{line.text}: the last word must come from the rhyme bank")
                    groups.setdefault(line.rhyme, set()).add(WORD_TO_GROUP[last])
                    ends_used.append(last)
                self.assertTrue(all(len(g) == 1 for g in groups.values()), f"one letter, one sound: {groups}")
                sounds = [next(iter(g)) for g in groups.values()]
                self.assertEqual(len(sounds), len(set(sounds)), "different letters use different sounds")
                if section.kind in ("chorus", "outro"):
                    self.assertEqual(sounds, [hook_group], "chorus and outro answer the hook's own rhyme")
            # a person hears no end word twice, except the hook's own
            self.assertEqual(len(ends_used), len(set(ends_used)))
            self.assertNotIn(hook_last, ends_used)

    def test_one_motif_returns_in_the_chorus_the_bridge_and_the_outro(self):
        scaffold = lyrics_scaffold("sunset-city-pop/arch-form", 12, form="full")
        homes = [s.tag for s in scaffold.sections if any(line.text.lower() == scaffold.motif for line in s.lines)]
        self.assertEqual(sorted(set(homes)), ["[Bridge]", "[Chorus]", "[Outro]"])
        self.assertGreaterEqual(scaffold.text.lower().count(scaffold.motif), 7)
        self.assertEqual(scaffold.to_dict()["motif"]["returns_in"], homes)
        choruses = [s for s in scaffold.sections if s.kind == "chorus"]
        self.assertTrue(all(c.lines == choruses[0].lines for c in choruses), "the chorus is sung the same way each time")

    def test_the_whole_catalog_scaffolds_cleanly(self):
        for preset in self.library.presets:
            scaffold = lyrics_scaffold(preset, 7)
            self.assertEqual(validate_scaffold_text(scaffold.text), [], preset.id)
            self.assertEqual(scaffold.source, "scaffold")
        for seed in (0, 1, 2, 99, 2**63 - 1):
            for preset in self.library.presets[::29]:
                self.assertEqual(validate_scaffold_text(lyrics_scaffold(preset, seed).text), [])

    def test_no_scaffold_in_a_large_sample_contains_host_wording_or_markup(self):
        for index, preset in enumerate(self.library.presets[::3]):
            for seed in (index, index + 1000):
                text = lyrics_scaffold(preset, seed, tag_style=TAG_STYLES[seed % 2], form=FORMS[index % 2]).text
                self.assertEqual(contains_dj_speech(text), [], text)
                self.assertNotRegex(text, r"[<>|]|\[(?:laughs|whispers|excited|sighs)\]")

    def test_the_same_inputs_give_the_same_outline_and_other_inputs_differ(self):
        first = lyrics_scaffold("neo-soul/rondo-path", 8)
        self.assertEqual(first, lyrics_scaffold("neo-soul/rondo-path", 8))
        self.assertEqual(first.text, lyrics_scaffold(self.library.get("neo-soul/rondo-path"), 8).text)
        texts = {lyrics_scaffold("neo-soul/rondo-path", seed).text for seed in range(60)}
        self.assertGreaterEqual(len(texts), 58)
        self.assertNotEqual(first.text, lyrics_scaffold("deep-house/rondo-path", 8).text)
        self.assertEqual(len({lyrics_scaffold("neo-soul/rondo-path", s).world for s in range(60)}), len(WORLDS))

    def test_a_style_leans_toward_a_world_that_fits_it(self):
        counts = {}
        for seed in range(120):
            world = lyrics_scaffold("ambient-garden/slow-tide", seed).world
            counts[world] = counts.get(world, 0) + 1
        self.assertEqual(max(counts, key=counts.get), "harbor-night", counts)

    def test_arguments_are_checked(self):
        for kwargs in ({"tag_style": "shout"}, {"form": "epic"}):
            with self.assertRaises(ValueError):
                lyrics_scaffold("neo-soul/rondo-path", 1, **kwargs)
        for seed in (-1, 2**63, 1.5, "1", None, True):
            with self.assertRaises(ValueError):
                lyrics_scaffold("neo-soul/rondo-path", seed)
        with self.assertRaises(ValueError):
            lyrics_scaffold(12345, 1)
        with self.assertRaises(ValueError):
            lyrics_scaffold("not/a-preset", 1)

    def test_to_dict_is_a_complete_record_that_says_it_is_a_scaffold(self):
        record = lyrics_scaffold("neo-soul/rondo-path", 8).to_dict()
        self.assertEqual((record["kind"], record["source"], record["speech"]), ("lyrics-scaffold", "scaffold", False))
        self.assertEqual(record["target_model"], "Frosty40/YuE2-Turbo-Radio")
        self.assertEqual(record["lyrics"], lyrics_scaffold("neo-soul/rondo-path", 8).text)
        self.assertEqual(len(record["plan"]), 5)
        self.assertEqual(set(record["plan"][0]), {"tag", "kind", "scheme", "syllables", "rhymes"})


class SeparationFromSuppliedLyricsTests(unittest.TestCase):
    """A scaffold is generated text; supplied lyrics are the operator's own and are never rewritten."""

    def setUp(self):
        self.service = PromptService(None)

    def test_the_library_still_refuses_to_invent_lyrics(self):
        library = load_library()
        with self.assertRaises(ValueError):
            library.generate("neo-soul/rondo-path", 1, mode="vocal")

    def test_supplied_lyrics_pass_through_untouched_and_are_labelled(self):
        mine = "[Verse]\nDes étoiles dans nos mains\n\n[Chorus]\n明日は明るい"
        result = self.service.compose({"preset": "neo-soul/rondo-path", "seed": 3, "lyrics": mine, "remember": False})
        self.assertEqual(result["lyrics"], {"source": "supplied", "text": mine, "scaffold": None})
        self.assertEqual(result["prompt"]["lyrics"], mine)
        self.assertEqual(result["provenance"]["lyrics_source"], "supplied")

    def test_a_vocal_request_without_words_gets_a_labelled_outline(self):
        result = self.service.compose({"preset": "neo-soul/rondo-path", "seed": 3, "mode": "vocal", "remember": False})
        self.assertEqual(result["lyrics"]["source"], "scaffold")
        self.assertEqual(result["prompt"]["lyrics"], result["lyrics"]["text"])
        self.assertEqual(result["lyrics"]["scaffold"]["source"], "scaffold")
        self.assertEqual(result["prompt"]["lyrics"], lyrics_scaffold("neo-soul/rondo-path", 3).text)
        self.assertIn("Sing the supplied lyrics", result["prompt"]["style"])
        self.assertFalse(result["prompt"]["speech"])

    def test_the_two_paths_cannot_be_mixed_and_instrumentals_never_get_words(self):
        base = {"preset": "neo-soul/rondo-path", "seed": 3, "remember": False}
        for extra in ({"lyrics": "[Verse]\nhi", "scaffold": True}, {"mode": "instrumental", "lyrics": "[Verse]\nhi"},
                      {"mode": "instrumental", "scaffold": True}, {"mode": "vocal", "scaffold": False}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.service.compose({**base, **extra})
        plain = self.service.compose(base)
        self.assertEqual((plain["lyrics"]["source"], plain["prompt"]["lyrics"], plain["treatment"]["mode"]), ("none", None, "instrumental"))

    def test_supplied_lyrics_with_speech_markers_or_no_tags_are_flagged_not_rewritten(self):
        result = self.service.compose({"preset": "neo-soul/rondo-path", "seed": 3, "remember": False,
                                       "lyrics": "<|speaker:1|> Welcome back, you're listening to the radio [laughs]"})
        self.assertEqual(len(result["warnings"]), 3)
        self.assertIn("<|speaker:1|>", result["prompt"]["lyrics"])


if __name__ == "__main__":
    unittest.main()
