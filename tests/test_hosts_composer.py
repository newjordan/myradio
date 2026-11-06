"""The template composer: the script library, grounding, repetition, length, determinism and effects."""
import dataclasses
import json
import os
import random
import re
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hosts_support import FACT_SETS, FULL, library, new_service, no_network  # noqa: E402
from test_hosts_support import voice_data  # noqa: E402

from myradio.hosts import Facts, HostsError, TemplateComposer, audit_speech_text, estimate_seconds, grounding_lint  # noqa: E402
from myradio.hosts.composer import ComposerConfig, blocked_by_topic, join_names  # noqa: E402
from myradio.hosts.memory import Memory  # noqa: E402
from myradio.hosts.personas import load_builtin_personas, validate_persona  # noqa: E402
from myradio.hosts.rng import Rng  # noqa: E402
from myradio.hosts.segments import LEXICON_KEYS, SEGMENT_IDS, SEGMENTS  # noqa: E402
from myradio.hosts.templates import MIN_TEMPLATES, TemplateLibrary, load_voice, slot_names  # noqa: E402
from myradio.hosts.textsafe import EVENT_TAGS  # noqa: E402
from myradio.hosts.timing import count_words, estimate_raw  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "myradio", "hosts", "data")


def warm_data():
    with open(os.path.join(DATA, "voice-warm.json"), encoding="utf-8") as handle:
        return json.load(handle)


def library_with(voice, data):
    return TemplateLibrary({**library().voices, voice: load_voice(data, f"{voice}-test.json")})


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.lib = library()

    def test_eight_script_styles_with_a_dozen_templates_for_every_kind(self):
        self.assertGreaterEqual(len(self.lib.voices), 8)
        for voice, counts in self.lib.counts().items():
            for segment in SEGMENT_IDS:
                self.assertGreaterEqual(counts[segment], MIN_TEMPLATES, f"{voice}/{segment}")

    def test_every_host_has_at_least_twelve_templates_for_each_kind_of_script(self):
        for persona in load_builtin_personas(self.lib.voices).values():
            for segment in SEGMENT_IDS:
                self.assertGreaterEqual(len(self.lib.templates(persona.voice, segment)), 12, f"{persona.id}/{segment}")

    def test_templates_are_distinct_and_carry_stable_ids(self):
        for voice, pack in self.lib.voices.items():
            for segment in SEGMENT_IDS:
                templates = pack.templates[segment]
                fingerprints = {" ".join(b.source.lower() for b in t.beats) for t in templates}
                self.assertEqual(len(fingerprints), len(templates), f"{voice}/{segment}")
                self.assertEqual([t.id for t in templates], [f"{voice}.{segment}.{i + 1:02d}" for i in range(len(templates))])
                openings = {t.beats[0].source.lower() for t in templates}
                self.assertEqual(len(openings), len(templates), f"{voice}/{segment}: two templates open with the same line")

    def test_extendable_kinds_have_plenty_of_extra_lines_for_long_scripts(self):
        for voice, pack in self.lib.voices.items():
            for segment, spec in SEGMENTS.items():
                if spec.extendable:
                    extras = pack.extensions.get(segment, ())
                    self.assertGreaterEqual(len(extras), 10, f"{voice}/{segment}")
                    self.assertEqual(len({e.beat.source for e in extras}), len(extras), f"{voice}/{segment} repeats an extra line")
                else:
                    self.assertEqual(pack.extensions.get(segment, ()), ())

    def test_the_song_is_named_by_at_least_nine_templates_where_it_matters(self):
        for segment, spec in SEGMENTS.items():
            for key in spec.key:
                for voice, pack in self.lib.voices.items():
                    users = [t for t in pack.templates[segment] if key in t.slots]
                    self.assertGreaterEqual(len(users), 9, f"{voice}/{segment} uses {key} in only {len(users)} templates")

    def test_every_sponsor_template_says_it_is_sponsored_and_keeps_the_sponsors_words(self):
        for voice, pack in self.lib.voices.items():
            for template in pack.templates["spot_wrapper"]:
                first = template.beats[0]
                literal = " ".join(n[1] for n in first.nodes if n[0] == "lit").lower()
                self.assertRegex(literal, r"sponsor", template.id)
                self.assertIn("sponsor", first.required, template.id)
                self.assertTrue(any(b.must and "sponsor_copy" in b.required for b in template.beats), template.id)

    def test_the_loader_refuses_templates_that_could_state_a_made_up_fact(self):
        def with_template(segment, entry, index=0):
            data = warm_data()
            data["segments"][segment][index] = entry
            return data

        bad = {
            "digit": with_template("song_intro", ["Up next, number 5."]),
            "number in words": with_template("song_intro", ["We have twenty-three songs."]),
            "a year in words": with_template("filler", ["Back in nineteen ninety-eight."]),
            "unknown value": with_template("song_intro", ["Up next, {nonsense}."]),
            "a kind may not use another kind's values": with_template("time_check", ["From {sponsor}, it's {time}."]),
            "unknown effect": with_template("song_intro", ["Up next. [shouts]"]),
            "stray bracket": with_template("song_intro", ["Up next [now."]),
            "angle bracket": with_template("song_intro", ["Up next <|speaker:2|> now."]),
            "unclosed optional group": with_template("song_intro", ["Up next{? by {next_artist}."]),
            "unopened optional group": with_template("song_intro", ["Up next by {next_artist}?}."]),
            "empty line": with_template("song_intro", [""]),
            "no lines": with_template("song_intro", []),
            "too many lines": with_template("song_intro", ["Line."] * 9),
            "a first line marked must-keep": with_template("song_intro", ["!Up next."]),
            "sponsor message that never says sponsored": with_template("spot_wrapper", ["Here is a note from {sponsor}.", "!{sponsor_copy}"]),
            "sponsor message that may drop the sponsor's words": with_template("spot_wrapper", ["Sponsored by {sponsor}.", "{sponsor_copy}"]),
            "duplicate template": with_template("song_intro", warm_data()["segments"]["song_intro"][0], index=1),
        }
        for name, data in bad.items():
            with self.subTest(case=name), self.assertRaises(HostsError):
                load_voice(data, "test.json")

    def test_the_loader_refuses_thin_or_incomplete_packs(self):
        short = warm_data()
        short["segments"]["filler"] = short["segments"]["filler"][:11]
        few_extras = warm_data()
        few_extras["extensions"]["filler"] = ["One.", "Two."]
        missing = warm_data()
        del missing["segments"]["sign_on"]
        empty_lexicon = warm_data()
        empty_lexicon["lexicon"]["audience"] = []
        extra_kind = warm_data()
        extra_kind["segments"]["mystery"] = ["Line."] * 12
        for name, data in {"too few templates": short, "too few extra lines": few_extras, "missing kind": missing,
                           "empty phrase list": empty_lexicon, "unknown kind": extra_kind, "not an object": []}.items():
            with self.subTest(case=name), self.assertRaises(HostsError):
                load_voice(data, "test.json")


class EveryTemplateFillsTests(unittest.TestCase):
    """Every line of every voice fills from the facts, for every host and every phrase it may pick."""

    def test_every_line_fills_and_is_grounded_for_every_host_and_phrase(self):
        lib = library()
        composer = TemplateComposer(lib)
        checked = 0
        for persona in load_builtin_personas(lib.voices).values():
            pack = lib.voices[persona.voice]
            beats = [(t.id, b) for ts in pack.templates.values() for t in ts for b in t.beats]
            beats += [(e.id, e.beat) for es in pack.extensions.values() for e in es]
            beat_slots = {id(b): slot_names(b.nodes) for _, b in beats}
            seen = set()
            for name in ("full", "thin", "morning_single"):
                facts = Facts.from_any(FACT_SETS[name])[0].with_host(persona.name)
                base = composer.slot_values(facts, persona, Rng(1))
                variants = [(None, None)] + [(key, phrase) for key in LEXICON_KEYS for phrase in persona.lexicon[key]]
                for key, phrase in variants:
                    values = dict(base) if key is None else dict(base, **{key: phrase})
                    for ident, beat in beats:
                        if key is not None and key not in beat_slots[id(beat)]:
                            continue          # this line does not use the phrase being varied
                        rendered = beat.render(values)
                        if rendered is None or (persona.id, rendered.text) in seen:
                            continue
                        seen.add((persona.id, rendered.text))
                        checked += 1
                        self.assertEqual(grounding_lint(rendered.text, facts, persona), [], f"{ident}: {rendered.text}")
                        self.assertRegex(rendered.text, r"[.!?…][\"')\]]*$|\]$", rendered.text)
                        self.assertNotRegex(rendered.text, r"[{}<>|\\]|  | [,.;:!?]|\.\.(?!\.)|\?\?|!!|,\.")
                        self.assertFalse(rendered.text[0].islower() or rendered.text[0] in ",.;:", rendered.text)
                        self.assertTrue(set(rendered.tags) <= set(EVENT_TAGS))
        self.assertGreater(checked, 5000)

    def test_every_templates_core_line_fills_whenever_its_values_are_present(self):
        lib = library()
        composer = TemplateComposer(lib)
        for persona in load_builtin_personas(lib.voices).values():
            facts = Facts.from_any(FULL)[0].with_host(persona.name)
            values = composer.slot_values(facts, persona, Rng(3))
            for segment in SEGMENT_IDS:
                for template in lib.templates(persona.voice, segment):
                    self.assertLessEqual(template.needs, set(values), f"{template.id} needs {sorted(template.needs - set(values))}")
                    self.assertIsNotNone(template.beats[0].render(values), template.id)

    def test_whole_scripts_are_clean_for_every_host_kind_and_fact_set(self):
        service = new_service()
        for persona in service.personas():
            for segment in SEGMENT_IDS:
                for name, raw in FACT_SETS.items():
                    for seed in range(2):
                        with self.subTest(host=persona.id, segment=segment, facts=name, seed=seed):
                            try:
                                script = service.compose(persona.id, segment, raw, seed=seed, dry_run=True)
                            except HostsError:
                                # only kinds that need facts the set lacks may refuse
                                self.assertIn(segment, ("station_id", "time_check", "spot_wrapper"))
                                continue
                            self.assertEqual(script.source, "template", script.warnings)
                            cleaned = Facts.from_any(raw)[0].with_host(persona.name)
                            self.assertEqual(grounding_lint(script.plain_text, cleaned, persona), [])
                            self.assertEqual(audit_speech_text(script.text, persona.speaker, persona.allowed_tags), [])
                            self.assertNotRegex(script.plain_text, r"[{}<>|\\\[\]]")


class ShapeAndDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.service = new_service()

    def test_the_result_has_the_documented_shape(self):
        script = self.service.compose("nova", "song_intro", FULL, seed=7, dry_run=True)
        data = script.to_dict()
        json.dumps(data)
        for key in ("text", "plain_text", "speaker", "role", "library_kind", "segment", "persona", "est_seconds", "target_seconds",
                    "facts_used", "warnings", "source", "template", "seed", "fictional", "format", "citations", "skipped", "variants"):
            self.assertIn(key, data)
        self.assertEqual((data["speaker"], data["role"], data["library_kind"], data["segment"], data["persona"], data["seed"]),
                         (1, "host_break", "speech", "song_intro", "nova", 7))
        self.assertTrue(data["text"].startswith("<|speaker:1|>"))
        self.assertEqual(data["text"][len("<|speaker:1|>"):].count("<|"), 0)
        self.assertEqual(data["est_seconds"], estimate_seconds(data["text"], self.service.persona("nova").wps))
        self.assertIn("next_title", data["facts_used"])
        self.assertIsInstance(data["facts_used"], list)
        self.assertFalse(data["fictional"])
        self.assertIsNone(data["format"])
        self.assertEqual(data["source"], "template")
        self.assertRegex(data["template"], r"^warm\.song_intro\.\d\d$")

    def test_roles_and_library_kinds_follow_the_shared_vocabulary(self):
        expected = {"station_id": ("id", "speech"), "song_intro": ("host_break", "speech"), "song_back_announce": ("host_break", "speech"),
                    "time_check": ("host_break", "speech"), "sign_on": ("host_break", "speech"), "sign_off": ("host_break", "speech"),
                    "spot_wrapper": ("spot", "ad"), "update_intro": ("update", "speech"), "update_outro": ("update", "speech"),
                    "filler": ("host_break", "speech")}
        for segment, (role, kind) in expected.items():
            script = self.service.compose("dex", segment, FULL, seed=1, dry_run=True)
            self.assertEqual((script.role, script.library_kind), (role, kind), segment)

    def test_the_same_seed_gives_the_same_script_everywhere(self):
        for persona in ("nova", "dex", "marlowe", "quill", "fuzz"):
            for segment in SEGMENT_IDS:
                first = new_service().compose(persona, segment, FULL, seed=424242, dry_run=True)
                second = new_service().compose(persona, segment, FULL, seed=424242, dry_run=True)
                self.assertEqual(first.to_dict(), second.to_dict(), f"{persona}/{segment}")

    def test_the_stream_of_random_choices_does_not_depend_on_the_python_version(self):
        rng = Rng(7, "pin")
        self.assertEqual([rng.below(1000) for _ in range(6)], [276, 735, 20, 559, 532, 524])
        self.assertEqual(Rng(9).shuffled(range(10)), [7, 3, 6, 5, 2, 1, 0, 4, 9, 8])
        self.assertEqual(round(Rng("x", 1).random(), 12), 0.950550180228)

    def test_scripts_do_not_depend_on_hash_randomization_or_the_process(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import json, sys; sys.path.insert(0, %r)\n"
                "from myradio.hosts import HostsService\n"
                "facts = %r\n"
                "service = HostsService()\n"
                "out = [service.compose(p, k, facts, seed=99, dry_run=True).to_dict() for p in ('nova', 'dex', 'marlowe', 'quill')\n"
                "       for k in ('station_id', 'song_intro', 'song_back_announce', 'time_check', 'sign_on', 'sign_off', 'spot_wrapper', 'filler')]\n"
                "out += [service.compose('nova', None, facts, format=f, seed=99, dry_run=True).to_dict()\n"
                "        for f in ('word-salad-poem', 'station-almanac', 'airwave-forecast')]\n"
                "print(json.dumps(out, sort_keys=True))\n") % (root, FULL)
        results = []
        for hashseed in ("1", "424242"):
            run = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120,
                                 env={"PYTHONHASHSEED": hashseed, "PATH": os.environ.get("PATH", "")})
            self.assertEqual(run.returncode, 0, run.stderr[-500:])
            results.append(run.stdout)
        self.assertEqual(results[0], results[1])
        self.assertGreater(len(json.loads(results[0])), 30)

    def test_different_seeds_give_varied_scripts(self):
        for segment in ("song_intro", "sign_on", "filler", "song_back_announce"):
            texts = {self.service.compose("nova", segment, FULL, seed=s, dry_run=True).plain_text for s in range(30)}
            self.assertGreaterEqual(len(texts), 20, segment)

    def test_no_seed_means_a_fresh_random_one_that_is_reported(self):
        first = self.service.compose("nova", "song_intro", FULL, dry_run=True)
        again = self.service.compose("nova", "song_intro", FULL, seed=first.seed, dry_run=True)
        self.assertEqual(first.text, again.text)
        seeds = {self.service.compose("nova", "song_intro", FULL, dry_run=True).seed for _ in range(20)}
        self.assertGreater(len(seeds), 15)

    def test_seeds_the_station_makes_up_survive_a_trip_through_javascript(self):
        # JSON numbers are doubles in a browser: a seed above 2**53 comes back changed, and the script cannot be repeated
        seeds = {self.service.compose("nova", "song_intro", FULL, dry_run=True).seed for _ in range(300)}
        self.assertGreater(len(seeds), 250)
        for seed in seeds:
            self.assertLess(seed, 2 ** 53)
            self.assertEqual(int(float(seed)), seed)
            self.assertEqual(json.loads(json.dumps({"seed": seed}))["seed"], seed)

    def test_seeds_are_validated(self):
        for bad in (-1, 2 ** 63, 1.5, "7", True, [], float("nan")):
            with self.subTest(seed=bad), self.assertRaisesRegex(HostsError, "seed"):
                self.service.compose("nova", "song_intro", FULL, seed=bad, dry_run=True)
        for good in (0, 1, 2 ** 63 - 1):
            self.assertTrue(self.service.compose("nova", "song_intro", FULL, seed=good, dry_run=True).text)

    def test_unknown_kinds_hosts_and_choices_are_plain_errors(self):
        for segment in ("poem", "", 5, "SONG_INTRO", ["song_intro"]):
            with self.subTest(segment=segment), self.assertRaisesRegex(HostsError, "Choose|either"):
                self.service.compose("nova", segment, FULL, dry_run=True)
        with self.assertRaisesRegex(HostsError, "no host"):
            self.service.compose("nobody", "song_intro", FULL, dry_run=True)
        with self.assertRaisesRegex(HostsError, "either"):
            self.service.compose("nova", None, FULL, dry_run=True)
        with self.assertRaisesRegex(HostsError, "either"):
            self.service.compose("nova", "song_intro", FULL, format="word-salad-poem", dry_run=True)

    def test_a_language_the_templates_do_not_cover_is_refused(self):
        persona, _ = validate_persona({"name": "Zed", "role": "r", "blurb": "b", "voice": "warm"}, library().voices)
        with self.assertRaisesRegex(HostsError, "English"):
            self.service.composer.compose(dataclasses.replace(persona, language="fr"), "song_intro", FULL)


class SegmentBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.service = new_service()

    def test_a_script_that_cannot_be_honest_without_a_fact_says_so_plainly(self):
        cases = {("station_id", "station"): "station", ("time_check", "local_time"): "time", ("spot_wrapper", "sponsor"): "sponsor",
                 ("spot_wrapper", "sponsor_copy"): "words"}
        for (segment, missing), word in cases.items():
            facts = {k: v for k, v in FULL.items() if k != missing}
            with self.subTest(segment=segment, missing=missing), self.assertRaises(HostsError) as caught:
                self.service.compose("nova", segment, facts, seed=1, dry_run=True)
            self.assertIn(word, str(caught.exception))

    def test_a_time_check_says_the_time_in_words(self):
        for persona in self.service.personas():
            for seed in range(10):
                script = self.service.compose(persona.id, "time_check", {"local_time": "21:07"}, seed=seed, dry_run=True)
                self.assertIn("nine oh seven at night", script.plain_text.lower(), persona.id)
        self.assertIn("midnight", self.service.compose("nova", "time_check", {"local_time": "00:00"}, seed=1, dry_run=True).plain_text.lower())
        self.assertIn("a quarter to seven in the morning",
                      self.service.compose("nova", "time_check", {"local_time": "6:45"}, seed=1, dry_run=True).plain_text.lower())

    def test_a_station_id_always_names_the_station(self):
        for persona in self.service.personas():
            for seed in range(10):
                script = self.service.compose(persona.id, "station_id", {"station": "Porchlight FM"}, seed=seed, dry_run=True)
                self.assertIn("Porchlight FM", script.plain_text)
                self.assertIn("station", script.facts_used)

    def test_songs_are_named_whenever_the_caller_supplied_them(self):
        for persona in self.service.personas():
            for seed in range(8):
                intro = self.service.compose(persona.id, "song_intro", FULL, seed=seed, dry_run=True)
                self.assertIn("Paper Lantern Weather", intro.plain_text, persona.id)
                self.assertIn("next_title", intro.facts_used)
                back = self.service.compose(persona.id, "song_back_announce", FULL, seed=seed, dry_run=True)
                self.assertIn("Low Orbit Lullaby", back.plain_text, persona.id)
                self.assertIn("now_title", back.facts_used)

    def test_without_track_facts_the_host_speaks_in_general_terms(self):
        for persona in ("nova", "dex", "marlowe", "quill"):
            for seed in range(10):
                script = self.service.compose(persona, "song_intro", {"station": "Radio"}, seed=seed, dry_run=True)
                self.assertEqual((script.warnings, script.source), ((), "template"))
                self.assertLessEqual(set(script.facts_used), {"station"})

    def test_a_thin_fact_set_still_makes_every_script_that_needs_nothing_more(self):
        for segment in ("song_intro", "song_back_announce", "sign_on", "sign_off", "filler", "update_intro", "update_outro"):
            for persona in ("nova", "marlowe", "quill"):
                script = self.service.compose(persona, segment, {}, seed=2, dry_run=True)
                self.assertTrue(script.plain_text)
                self.assertEqual(script.source, "template", f"{persona}/{segment}: {script.warnings}")

    def test_update_openings_and_closings_agree_with_the_item_count(self):
        for count in (1, 2, 3):
            for persona in ("nova", "dex", "marlowe", "professor", "quill", "juniper"):
                for segment in ("update_intro", "update_outro"):
                    for seed in range(12):
                        text = self.service.compose(persona, segment, dict(FULL, item_count=count), seed=seed, dry_run=True).plain_text
                        if count == 1:
                            self.assertNotRegex(text, r"(?i)\b(those|them|here are|were the|are the)\b|\b(two|three) items\b", text)
                        else:
                            self.assertNotRegex(text, r"(?i)\bone item\b|\b(two|three) item\b|\bthat was (one|two|three)\b", text)

    def test_notes_are_spoken_whole(self):
        facts = dict(FULL, notes=["The studio closes early on Friday"])
        texts = [self.service.compose("nova", "filler", facts, seed=s, dry_run=True).plain_text for s in range(300)]
        self.assertTrue(any("The studio closes early on Friday." in t for t in texts))

    def test_a_note_about_something_the_host_avoids_is_left_out(self):
        facts = dict(FULL, notes=["Politics night is back"])
        for seed in range(15):
            script = self.service.compose("nova", "sign_on", facts, seed=seed, dry_run=True)
            self.assertNotIn("Politics", script.plain_text)
        self.assertTrue(any("does not talk about politics" in w for w in script.warnings))
        script = self.service.compose("professor", "sign_on", dict(FULL, notes=["Politicians are discussed"]), seed=1, dry_run=True)
        self.assertNotIn("Politicians", script.plain_text)

    def test_topics_to_avoid_match_the_words_that_start_like_them(self):
        topics = ("politics", "religion", "medical advice", "rumors", "gossip", "speculation")
        for text, hit in (("Politicians are discussed", "politics"), ("A political debate tonight", "politics"), ("politics again", "politics"),
                          ("Religious holidays ahead", "religion"), ("Many religions meet here", "religion"),
                          ("Medication advice is not given", "medical advice"), ("Medical advice for gardeners", "medical advice"),
                          ("Rumours are going round", "rumors"), ("A rumor mill", "rumors"), ("Stop gossiping", "gossip"),
                          ("Pure speculation", "speculation"), ("They speculate about it", "speculation"), ("Speculative fiction", "speculation")):
            with self.subTest(text=text):
                self.assertEqual(blocked_by_topic(text, topics), hit)
        for text in ("Please be polite", "A policy change", "The medical team is here", "Advice for gardeners", "Nothing to avoid here",
                     "", "Spec sheets"):
            with self.subTest(text=text):
                self.assertIsNone(blocked_by_topic(text, topics))
        self.assertIsNone(blocked_by_topic(None, topics))
        self.assertEqual(blocked_by_topic("C++ news", ("c++",)), "c++")             # a topic with symbols is matched as written
        self.assertIsNone(blocked_by_topic("Anything", ("", "   ")))

    def test_a_note_about_something_the_host_avoids_is_never_spoken_whatever_the_seed(self):
        # Many seeds, because a single lucky seed proves nothing: the note is spoken often when it is allowed.
        service = new_service()
        harmless = "The studio closes early on Friday"
        spoken = sum(harmless in service.compose("professor", kind, {"notes": [harmless]}, seed=seed, dry_run=True).plain_text
                     for kind in ("filler", "sign_on") for seed in range(300))
        self.assertGreater(spoken, 10)
        for note in ("Politicians are discussed", "A political debate tonight", "Politics night is back"):
            for kind in ("filler", "sign_on", "song_intro", "sign_off"):
                for seed in range(150):
                    script = service.compose("professor", kind, {"notes": [note, "Gardeners meet on Monday"]}, seed=seed, dry_run=True)
                    self.assertNotRegex(script.plain_text, r"(?i)politic")
                    self.assertTrue(any("does not talk about politics" in w for w in script.warnings), (note, kind, seed))
        script = service.compose("professor", "sign_on", {"notes": ["Religious services moved"]}, seed=1, dry_run=True)
        self.assertNotRegex(script.plain_text, r"(?i)religio")

    def test_a_topic_the_host_avoids_is_not_named_in_an_update_opening(self):
        service = new_service()
        for seed in range(40):
            script = service.compose("marlowe", "update_intro", {"topic": "Local politics", "item_count": 2}, seed=seed, dry_run=True)
            self.assertNotIn("politics", script.plain_text.lower())
            self.assertNotIn("topic", script.facts_used)
            self.assertTrue(any("topic was left out" in w for w in script.warnings))
        named = [service.compose("marlowe", "update_intro", {"topic": "Local gardens", "item_count": 2}, seed=s, dry_run=True) for s in range(40)]
        self.assertTrue(any("Local gardens" in n.plain_text for n in named))

    def test_the_host_name_defaults_to_the_persona_and_can_be_overridden(self):
        plain = [self.service.compose("nova", "station_id", {"station": "Radio"}, seed=s, dry_run=True) for s in range(30)]
        self.assertTrue(any("Nova" in s.plain_text for s in plain))
        self.assertTrue(all("host" not in s.facts_used for s in plain))
        given = [self.service.compose("nova", "station_id", {"station": "Radio", "host": "Mx Moonbeam"}, seed=s, dry_run=True)
                 for s in range(30)]
        self.assertTrue(any("Mx Moonbeam" in s.plain_text for s in given))
        self.assertTrue(all(s.speaker == 1 for s in given))      # the voice is still Nova's

    def test_names_that_carry_digits_and_capitals_are_read_as_given(self):
        facts = {"station": "FM 101.5 The Bridge", "next_title": "7 Rings (Live) [2019 Remaster]", "next_artist": "B-52s & Friends",
                 "local_time": "07:00"}
        for persona in ("nova", "dex", "marlowe"):
            for seed in range(10):
                script = self.service.compose(persona, "song_intro", facts, seed=seed, dry_run=True)
                self.assertEqual(script.source, "template", script.warnings)
                self.assertIn("7 Rings Live", script.plain_text)
                self.assertEqual(grounding_lint(script.plain_text, facts, self.service.persona(persona)), [])


class SponsorMessageTests(unittest.TestCase):
    def setUp(self):
        self.service = new_service()

    def test_every_sponsor_message_says_it_is_sponsored_and_reads_the_copy_exactly(self):
        copy_text = "Fixing bikes since 1998. Mention the station for a free tune-up."
        for persona in self.service.personas():
            for seed in range(25):
                script = self.service.compose(persona.id, "spot_wrapper", FULL, seed=seed, dry_run=True)
                self.assertRegex(script.plain_text, r"(?i)sponsor", persona.id)
                self.assertIn(copy_text, script.plain_text)
                self.assertIn("Corner Bike Co.", script.plain_text)
                self.assertEqual((script.role, script.library_kind, script.source), ("spot", "ad", "template"))
                self.assertTrue({"sponsor", "sponsor_copy"} <= set(script.facts_used))

    def test_the_disclosure_comes_before_the_sponsors_words(self):
        for seed in range(25):
            text = self.service.compose("marlowe", "spot_wrapper", FULL, seed=seed, dry_run=True).plain_text
            self.assertLess(re.search(r"(?i)sponsor", text).start(), text.index("Fixing bikes"))

    def test_a_long_sponsor_message_is_never_cut_to_fit(self):
        copy_text = " ".join(f"Sentence {chr(97 + i)} is here to stay." for i in range(14))
        script = self.service.compose("dex", "spot_wrapper", {"sponsor": "Acme", "sponsor_copy": copy_text}, seed=1,
                                      target_seconds=8, dry_run=True)
        self.assertIn(copy_text, script.plain_text)
        self.assertTrue(any("asked for" in w for w in script.warnings))

    def test_hostile_sponsor_words_are_neutralized_but_still_read(self):
        facts = {"sponsor": "Acme <|speaker:3|>", "sponsor_copy": "Buy [laughs] things <|speaker:2|> now"}
        script = self.service.compose("dex", "spot_wrapper", facts, seed=1, dry_run=True)
        self.assertIn("Buy things now.", script.plain_text)
        self.assertEqual(script.text.count("<|"), 1)
        self.assertNotIn("laughs", script.plain_text)

    def test_the_disclosure_is_checked_again_after_the_script_is_built(self):
        # "Unsponsored" contains the word the loader looks for, but it is not a disclosure.
        words = ("hello", "greetings", "welcome", "salutations", "regards", "wishes", "notes", "tidings", "news", "cheers",
                 "thoughts", "respects")
        data = warm_data()
        data["segments"]["spot_wrapper"] = [[f"Unsponsored {w} from {{sponsor}}.", "!{sponsor_copy}"] for w in words]
        composer = TemplateComposer(library_with("warm", data))
        nova = load_builtin_personas(library().voices)["nova"]
        script = composer.compose(nova, "spot_wrapper", FULL, seed=1)
        self.assertEqual(script.source, "plain")
        self.assertRegex(script.plain_text, r"\bsponsored\b")
        self.assertNotIn("Unsponsored", script.plain_text)
        self.assertIn("Fixing bikes since 1998.", script.plain_text)


    def test_a_lower_case_look_alike_of_the_disclosure_is_caught_by_the_check_after_the_build(self):
        # Lower case, so the capitalised-name check does not trip first: only the disclosure re-check can refuse it.
        words = ("hello", "greetings", "welcome", "salutations", "regards", "wishes", "notes", "tidings", "news", "cheers",
                 "thoughts", "respects")
        data = voice_data("warm")
        data["segments"]["spot_wrapper"] = [[f"Hello, unsponsored {w} from {{sponsor}}.", "!{sponsor_copy}"] for w in words]
        composer = TemplateComposer(library_with("warm", data))
        nova = load_builtin_personas(library().voices)["nova"]
        for seed in range(6):
            script = composer.compose(nova, "spot_wrapper", FULL, seed=seed)
            self.assertEqual(script.source, "plain")
            self.assertRegex(script.plain_text, r"\bsponsored\b")
            self.assertNotRegex(script.plain_text, r"unsponsored")
            self.assertIn("Fixing bikes since 1998.", script.plain_text)
            self.assertTrue(any("could not be matched" in w and "sponsored" in w for w in script.warnings), script.warnings)

    def test_copy_that_ends_with_a_web_address_still_gets_its_wrapper(self):
        facts = {"sponsor": "Maple Bakery & Co.", "sponsor_copy": "Dr. Smith's Dental: because every smile matters... Book now at smiles.example.co.uk/book"}
        service = new_service()
        for persona in service.personas():
            for seed in range(8):
                with self.subTest(host=persona.id, seed=seed):
                    script = service.compose(persona.id, "spot_wrapper", facts, seed=seed, dry_run=True)
                    self.assertEqual((script.source, script.warnings), ("template", ()))
                    self.assertIn("smiles.example.co.uk/book.", script.plain_text)
                    self.assertRegex(script.plain_text, r"(?i)sponsor")


class RepetitionTests(unittest.TestCase):
    TARGETS = {"station_id": 3.5, "song_intro": 10, "song_back_announce": 10, "time_check": 3.5, "sign_on": 12, "sign_off": 12,
               "update_intro": 5.5, "update_outro": 5, "filler": 10}

    def test_no_line_repeats_inside_the_window_across_two_hundred_scripts(self):
        service = new_service()
        for persona, segments in (("nova", tuple(self.TARGETS)), ("marlowe", ("song_intro", "song_back_announce", "update_intro", "sign_off"))):
            for segment in segments:
                target = self.TARGETS[segment]
                history = []
                for i in range(200):
                    script = service.compose(persona, segment, FULL, seed=i, target_seconds=target)
                    window = min(8, script.variants - 1)
                    recent = history[-window:] if window else []
                    self.assertNotIn(script.template, [t for t, _ in recent], f"{persona}/{segment} #{i}")
                    self.assertNotIn(script.plain_text, [p for _, p in recent], f"{persona}/{segment} #{i}")
                    self.assertGreaterEqual(window, 6, f"{persona}/{segment} #{i} had only {script.variants} lines to choose from")
                    history.append((script.template, script.plain_text))
                self.assertGreaterEqual(len({t for t, _ in history}), 6, f"{persona}/{segment}")

    def test_the_rule_holds_for_every_host_over_a_shorter_run(self):
        service = new_service()
        for persona in service.personas():
            for segment in ("song_intro", "filler"):
                history = []
                for i in range(40):
                    script = service.compose(persona.id, segment, FULL, seed=i, target_seconds=self.TARGETS[segment])
                    window = min(8, script.variants - 1)
                    recent = [t for t, _ in history[-window:]] if window > 0 else []
                    self.assertNotIn(script.template, recent, f"{persona.id}/{segment} #{i}")
                    self.assertGreaterEqual(script.variants, 2, f"{persona.id}/{segment}")
                    history.append((script.template, script.plain_text))

    def test_the_full_window_of_eight_holds_for_a_host_with_plenty_to_choose_from(self):
        service = new_service()
        for segment in ("song_intro", "sign_off", "filler", "station_id"):
            last, gaps = {}, []
            for i in range(200):
                script = service.compose("nova", segment, FULL, seed=i, target_seconds=self.TARGETS[segment])
                if script.template in last:
                    gaps.append(i - last[script.template])
                last[script.template] = i
            self.assertGreaterEqual(min(gaps), 9, segment)

    def test_extra_lines_used_a_moment_ago_come_last_in_line(self):
        service = new_service()
        previous, overlaps = None, 0
        for i in range(60):
            script = service.compose("nova", "song_intro", FULL, seed=i, target_seconds=14)
            used = set(service.memory.recent("nova", "song_intro", 1)[0].units[1:])
            if previous is not None:
                overlaps += bool(used & previous)
            previous = used
        self.assertLess(overlaps, 12)

    def test_a_preview_does_not_use_up_the_hosts_lines(self):
        service = new_service()
        for _ in range(10):
            service.compose("nova", "song_intro", FULL, seed=5, target_seconds=10, dry_run=True)
        self.assertEqual(service.memory.size(), 0)
        service.compose("nova", "song_intro", FULL, seed=5, target_seconds=10)
        self.assertEqual(service.memory.size(), 1)

    def test_memory_belongs_to_one_host_and_one_kind_of_script(self):
        service = new_service()
        for i in range(12):
            service.compose("nova", "song_intro", FULL, seed=i, target_seconds=10)
        # another host, and another kind of script, are untouched by everything Nova has said
        self.assertEqual(service.compose("hazel", "song_intro", FULL, seed=5, target_seconds=10, dry_run=True).to_dict(),
                         new_service().compose("hazel", "song_intro", FULL, seed=5, target_seconds=10, dry_run=True).to_dict())
        self.assertEqual(service.compose("nova", "song_back_announce", FULL, seed=5, target_seconds=10, dry_run=True).to_dict(),
                         new_service().compose("nova", "song_back_announce", FULL, seed=5, target_seconds=10, dry_run=True).to_dict())
        self.assertNotEqual(service.compose("nova", "song_intro", FULL, seed=5, target_seconds=10, dry_run=True).to_dict(),
                            new_service().compose("nova", "song_intro", FULL, seed=5, target_seconds=10, dry_run=True).to_dict())

    def test_a_host_with_few_fitting_lines_still_avoids_back_to_back_repeats(self):
        service = new_service()
        previous = None
        for i in range(60):
            script = service.compose("nova", "song_back_announce", {"station": "Radio"}, seed=i, target_seconds=8)
            self.assertGreaterEqual(script.variants, 3)
            self.assertNotEqual(script.template, previous)
            previous = script.template

    def test_the_window_of_eight_holds_at_default_lengths_for_every_host_and_kind(self):
        # The favourable runs above fix the length. At the default lengths several hosts have only five to eight lines
        # that fit, and the window used to shrink to that number, so a line came round again within eight scripts.
        service = new_service()
        for persona in service.personas():
            for segment in SEGMENT_IDS:
                if segment == "spot_wrapper":
                    continue
                last = {}
                for i in range(22):
                    script = service.compose(persona.id, segment, FULL, seed=i)
                    if script.template in last:
                        self.assertGreaterEqual(i - last[script.template], 9, f"{persona.id}/{segment} #{i} repeated {script.template}")
                    last[script.template] = i

    def test_a_line_borrowed_to_avoid_a_repeat_says_why_it_misses_the_length(self):
        service = new_service()
        notes, history = 0, []
        for i in range(30):
            script = service.compose("nova", "time_check", FULL, seed=i)
            self.assertNotIn(script.template, history[-8:])
            history.append(script.template)
            if any("used a moment ago" in w for w in script.warnings):
                notes += 1
                self.assertGreater(abs(script.est_seconds - script.target_seconds), service.composer.tolerance(script.target_seconds))
                self.assertLess(script.variants, 9)
        self.assertGreater(notes, 0)
        self.assertLess(notes, 8)       # it is the exception, not the rule

    def test_lines_borrowed_to_avoid_a_repeat_are_the_nearest_in_length_never_the_farthest(self):
        # Three lines fit a ten-second break; the other nine are 5 to 30 seconds long. Once the three have been used the
        # window of eight asks for more, and the ones that are borrowed must be the closest to ten seconds.
        pool = ("music friends night radio listening quiet evening kitchen window garden river morning coffee blanket lantern "
                "weather harbor orchard bridge village market station teacher neighbour winter summer").split()
        wps = 2.1

        def template(seconds, n):
            words = max(3, round((seconds - 0.3 * (1 + (seconds > 20))) * wps))      # one sentence, or two for the long ones
            parts = [words] if seconds <= 20 else [words // 2, words - words // 2]
            sentences = [" ".join(pool[(n * 3 + j + 7 * k) % len(pool)] for j in range(count)).capitalize() + "." for k, count in enumerate(parts)]
            return [sentences[0]] + ["!" + extra for extra in sentences[1:]]        # '!' keeps a line, so the length is fixed

        lengths = [10, 10.2, 9.8, 5, 6, 7, 13, 14, 15, 22, 25, 30]
        data = voice_data("warm")
        data["segments"]["filler"] = [template(seconds, n) for n, seconds in enumerate(lengths)]
        composer = TemplateComposer(library_with("warm", data))
        nova = load_builtin_personas(library().voices)["nova"]
        by_id = {t.id: seconds for t, seconds in zip(composer.library.templates("warm", "filler"), lengths)}
        used, misses = [], []
        for i in range(60):
            script = composer.compose(nova, "filler", {}, seed=i, target_seconds=10)
            self.assertNotIn(script.template, used[-8:], f"#{i}")
            used.append(script.template)
            misses.append(abs(by_id[script.template] - 10))
        self.assertLessEqual(max(misses), 5, "a line 12 seconds or more away was borrowed while closer ones existed")
        self.assertGreater(len({t for t in used}), 8)           # and the nearest nine are all used in turn

    def test_with_few_facts_every_usable_line_is_used_before_any_is_said_again(self):
        facts = {"station": "Radio", "local_time": "21:07"}
        service = new_service()
        for persona in service.personas():
            cleaned = Facts.from_any(facts)[0].with_host(persona.name)
            values = service.composer.slot_values(cleaned, persona, Rng(1))
            for segment in SEGMENT_IDS:
                if segment == "spot_wrapper":
                    continue
                usable = len([t for t in service.library.templates(persona.voice, segment) if t.needs <= set(values)])
                if not usable:
                    continue
                last, previous = {}, None
                for i in range(min(30, 3 * usable + 4)):
                    script = service.compose(persona.id, segment, facts, seed=i)
                    self.assertNotEqual(script.template, previous, f"{persona.id}/{segment} #{i}: the same line twice in a row")
                    previous = script.template
                    if script.template in last:
                        self.assertGreaterEqual(i - last[script.template], min(9, usable), f"{persona.id}/{segment} #{i}")
                    last[script.template] = i

    def test_the_memory_is_bounded(self):
        memory = Memory(limit=10)
        for i in range(500):
            memory.remember("nova", "song_intro", [f"t{i}"], f"d{i}", float(i))
        records = memory.recent("nova", "song_intro", 100)
        self.assertEqual(len(records), 10)
        self.assertEqual(records[0].units, ("t499",))
        self.assertEqual(memory.recent("nova", "song_intro", 0), [])
        memory.forget("nova")
        self.assertEqual(memory.size(), 0)

    def test_scripts_written_at_the_same_time_from_many_threads_stay_consistent(self):
        service = new_service()
        results, lock = [], threading.Lock()

        def worker(n):
            for i in range(25):
                script = service.compose("nova", "filler", FULL, seed=n * 1000 + i, target_seconds=10)
                with lock:
                    results.append(script)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(results), 100)
        self.assertTrue(all(r.text.startswith("<|speaker:1|>") and r.source == "template" for r in results))
        self.assertEqual(service.memory.size(), 30)       # bounded, whatever the traffic


class LengthControlTests(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        self.composer = self.service.composer

    def test_default_lengths_are_met_for_every_host_and_kind(self):
        for persona in self.service.personas():
            for segment in SEGMENT_IDS:
                if segment == "spot_wrapper":
                    continue
                for seed in range(8):
                    script = self.service.compose(persona.id, segment, FULL, seed=seed, dry_run=True)
                    self.assertIsNotNone(script.target_seconds)
                    self.assertLessEqual(abs(script.est_seconds - script.target_seconds), self.composer.tolerance(script.target_seconds),
                                         f"{persona.id}/{segment}/{seed}: {script.est_seconds} vs {script.target_seconds}")
                    self.assertFalse([w for w in script.warnings if "asked for" in w])

    def test_asked_for_lengths_are_met_within_the_stated_tolerance(self):
        table = {"song_intro": (6, 10, 14), "song_back_announce": (6, 10, 13), "sign_on": (8, 12, 16), "sign_off": (8, 12, 16),
                 "filler": (6, 10, 14), "station_id": (3, 4.5), "time_check": (3, 4.5), "update_intro": (4, 6), "update_outro": (3.5, 5)}
        for persona in ("nova", "marlowe", "professor", "juniper", "sable", "wren"):
            for segment, targets in table.items():
                for target in targets:
                    for seed in range(10):
                        script = self.service.compose(persona, segment, FULL, seed=seed, target_seconds=target, dry_run=True)
                        self.assertLessEqual(abs(script.est_seconds - target), self.composer.tolerance(target),
                                             f"{persona}/{segment} {target}s seed {seed}: {script.est_seconds} ({script.plain_text})")
                        self.assertEqual(script.target_seconds, float(target))

    def test_the_tolerance_is_the_larger_of_a_share_and_a_floor(self):
        self.assertEqual(self.composer.tolerance(4), 1.5)
        self.assertAlmostEqual(self.composer.tolerance(20), 3.0)
        tight = TemplateComposer(library(), config=ComposerConfig(tolerance=0.05, min_tolerance=0.5))
        self.assertAlmostEqual(tight.tolerance(20), 1.0)

    def test_the_estimate_is_an_explicit_words_per_second_calculation(self):
        text = "One two three four five six seven eight nine ten."          # ten words, one sentence
        self.assertEqual(count_words(text), 10)
        self.assertAlmostEqual(estimate_raw(text, 2.5), 10 / 2.5 + 0.30)
        self.assertAlmostEqual(estimate_raw("Hello, there. Friends.", 2.0), 3 / 2.0 + 2 * 0.30 + 0.12)
        self.assertAlmostEqual(estimate_raw("Hello [laughs] there.", 2.0), 2 / 2.0 + 0.30 + 0.40)
        self.assertAlmostEqual(estimate_raw("<|speaker:2|>Hello there.", 2.0), estimate_raw("Hello there.", 2.0))
        self.assertGreater(estimate_raw(text, 2.1), estimate_raw(text, 2.9))
        for degenerate in ("", "   ", None, 5):
            self.assertEqual(estimate_seconds(degenerate, 2.5), 0.0)
        self.assertEqual(estimate_seconds("word", 0), 0.0)

    def test_estimates_add_up_so_the_composer_can_choose_lines_by_arithmetic(self):
        a, b = "Up next, Paper Lantern Weather by Marble Hours.", "Settle in, night owls."
        self.assertAlmostEqual(estimate_raw(a + " " + b, 2.1), estimate_raw(a, 2.1) + estimate_raw(b, 2.1))

    def test_the_reported_estimate_matches_the_text_and_the_hosts_pace(self):
        for persona in self.service.personas():
            script = self.service.compose(persona.id, "song_intro", FULL, seed=3, dry_run=True)
            self.assertEqual(script.est_seconds, estimate_seconds(script.text, persona.wps))
        self.assertEqual(self.service.persona("nova").pace, "slow")
        self.assertLess(self.service.persona("nova").wps, self.service.persona("dex").wps)

    def test_a_chattier_host_talks_longer_by_default(self):
        low = [self.service.compose("quill", "song_intro", FULL, seed=s, dry_run=True).target_seconds for s in range(60)]
        high = [self.service.compose("fuzz", "song_intro", FULL, seed=s, dry_run=True).target_seconds for s in range(60)]
        self.assertLess(max(low), min(high))
        self.assertGreater(len(set(high)), 1)

    def test_lengths_outside_what_a_kind_allows_are_clamped_with_a_note(self):
        script = self.service.compose("nova", "station_id", FULL, seed=1, target_seconds=500, dry_run=True)
        self.assertEqual(script.target_seconds, SEGMENTS["station_id"].max_s)
        self.assertTrue(any("can run from" in w for w in script.warnings))
        script = self.service.compose("nova", "song_intro", FULL, seed=1, target_seconds=0.1, dry_run=True)
        self.assertEqual(script.target_seconds, SEGMENTS["song_intro"].min_s)

    def test_a_length_a_host_cannot_reach_is_reported_honestly(self):
        script = self.service.compose("quill", "song_intro", FULL, seed=1, target_seconds=30, dry_run=True)
        self.assertLess(script.est_seconds, 25)
        self.assertTrue(any("shorter than the 30 asked for" in w for w in script.warnings), script.warnings)
        self.assertEqual(script.est_seconds, estimate_seconds(script.plain_text, 2.1))

    def test_lengths_that_are_not_numbers_are_refused(self):
        for bad in (True, "10", [10], float("nan"), float("inf"), -float("inf")):
            with self.subTest(length=bad), self.assertRaisesRegex(HostsError, "seconds"):
                self.service.compose("nova", "song_intro", FULL, target_seconds=bad, dry_run=True)


class FactsOnlyTests(unittest.TestCase):
    def test_random_facts_never_produce_an_unsupported_number_name_or_address(self):
        service = new_service()
        rng = random.Random(77)
        words = ["Zorblax", "seven", "7", "Moon-River", "No. 1", "Dr. Who", "1,000", "x.com", "Anne-Marie", "O'Brien", "2026-11-05",
                 "twenty-first", "café", "FM", "UFO", "the", "of", "a", "Sunday", "3.5", "[laughs]", "<|speaker:2|>"]
        checked = 0
        for _ in range(150):
            def phrase():
                return " ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))
            facts = {"station": phrase(), "tagline": phrase(), "now_title": phrase(), "now_artist": phrase(),
                     "next_title": phrase(), "next_artist": phrase(), "notes": [phrase(), phrase()],
                     "local_time": rng.choice(["00:00", "07:30", "12:00", "18:45", "23:59"])}
            persona = service.persona(rng.choice(["nova", "dex", "marlowe", "professor", "quill", "juniper", "rae", "comet"]))
            segment = rng.choice(["song_intro", "song_back_announce", "sign_on", "sign_off", "filler", "station_id", "time_check"])
            try:
                script = service.compose(persona.id, segment, facts, seed=rng.randint(0, 10 ** 9), dry_run=True)
            except HostsError:
                continue
            checked += 1
            cleaned = Facts.from_any(facts)[0].with_host(persona.name)
            self.assertEqual(grounding_lint(script.plain_text, cleaned, persona), [], script.plain_text)
            self.assertEqual(audit_speech_text(script.text, script.speaker, persona.allowed_tags), [])
        self.assertGreater(checked, 100)

    def test_a_script_that_fails_the_check_is_replaced_by_a_plain_one(self):
        lines = ["Welcome to the House of Zorblax.", "Zorblax greets you.", "Visit Zorblax now.", "Zorblax is calling.",
                 "Come to Zorblax.", "Zorblax awaits.", "Zorblax again.", "Praise Zorblax.", "Hello from Zorblax.",
                 "Zorblax says hi.", "Zorblax Prime is here.", "Greetings, Zorblax."]
        data = warm_data()
        data["segments"]["song_intro"] = [[line, "Settle in."] for line in lines]
        composer = TemplateComposer(library_with("warm", data))
        nova = load_builtin_personas(library().voices)["nova"]
        facts = {"next_title": "Paper Lantern Weather", "next_artist": "Marble Hours"}
        script = composer.compose(nova, "song_intro", facts, seed=1)
        self.assertEqual(script.source, "plain")
        self.assertIsNone(script.template)
        self.assertNotIn("Zorblax", script.plain_text)
        self.assertEqual(script.plain_text, "Up next, Paper Lantern Weather by Marble Hours.")
        self.assertTrue(any("could not be matched to your facts" in w and "not in your facts" in w for w in script.warnings),
                        script.warnings)
        self.assertEqual(grounding_lint(script.plain_text, facts, nova), [])
        self.assertEqual(audit_speech_text(script.text, 1, nova.allowed_tags), [])
        self.assertEqual(composer.memory.size(), 1)

    def test_a_script_with_an_invented_web_address_or_number_is_also_replaced(self):
        data = warm_data()
        data["segments"]["filler"] = [[f"Visit {w} dot com today.", "Settle in."] for w in
                                      ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
                                       "kilo", "lima")]
        composer = TemplateComposer(library_with("warm", data))
        nova = load_builtin_personas(library().voices)["nova"]
        script = composer.compose(nova, "filler", {"station": "Radio"}, seed=1)
        self.assertEqual((script.source, script.plain_text), ("plain", "You're listening to Radio."))
        self.assertEqual(numbers_found(script.plain_text), [])

    def test_a_plain_script_is_used_when_no_ready_made_line_fits_the_facts(self):
        data = warm_data()
        data["segments"]["song_intro"] = [[f"Up next, {{next_title}} for {w}.", "Settle in."] for w in
                                          ("friends", "dreamers", "wanderers", "listeners", "neighbours", "travellers", "sleepers",
                                           "walkers", "thinkers", "makers", "readers", "builders")]
        composer = TemplateComposer(library_with("warm", data))
        nova = load_builtin_personas(library().voices)["nova"]
        script = composer.compose(nova, "song_intro", {"station": "Radio"}, seed=1)
        self.assertEqual((script.source, script.plain_text), ("plain", "Here is the next song."))
        self.assertTrue(any("No ready-made line fitted" in w for w in script.warnings))

    def test_if_even_the_plain_script_is_refused_the_host_says_only_general_words(self):
        service = new_service()
        with mock.patch("myradio.hosts.composer.grounding_lint", return_value=["The name “X” is not in your facts."]):
            script = service.compose("nova", "song_intro", FULL, seed=1, dry_run=True)
        self.assertEqual(script.plain_text, "Here is the next song.")
        self.assertEqual(script.facts_used, ())
        self.assertTrue(any("short, general line" in w for w in script.warnings))
        with mock.patch("myradio.hosts.composer.grounding_lint", return_value=["The name “X” is not in your facts."]):
            with self.assertRaisesRegex(HostsError, "could not be prepared safely"):
                service.compose("nova", "spot_wrapper", FULL, seed=1, dry_run=True)    # a sponsor's words are never dropped


def numbers_found(text):
    from myradio.hosts.lint import numbers_in
    return numbers_in(text)


class EffectTests(unittest.TestCase):
    def test_a_host_only_uses_the_effects_it_may(self):
        service = new_service()
        for persona in service.personas():
            for segment in ("song_intro", "song_back_announce", "sign_on", "filler", "sign_off", "spot_wrapper"):
                for seed in range(12):
                    script = service.compose(persona.id, segment, FULL, seed=seed, dry_run=True)
                    tags = re.findall(r"\[([a-z ]+)\]", script.text)
                    self.assertLessEqual(len(tags), 2, script.text)
                    self.assertTrue(set(tags) <= set(persona.allowed_tags), f"{persona.id}: {tags}")
        used = {t for s in range(60) for t in re.findall(r"\[([a-z ]+)\]", service.compose("dex", "song_intro", FULL, seed=s, dry_run=True).text)}
        self.assertTrue(used)      # the feature is real: at least one host does use an effect

    def test_a_custom_host_with_no_effects_never_gets_one(self):
        service = new_service()
        service.save_persona({"name": "Plain Pat", "role": "Host", "blurb": "Plain.", "voice": "bright", "allowed_tags": []})
        for segment in ("song_intro", "filler", "sign_on", "song_back_announce"):
            for seed in range(60):
                self.assertNotIn("[", service.compose("plain-pat", segment, FULL, seed=seed, dry_run=True).text)

    def test_the_operator_can_rename_or_switch_off_an_effect(self):
        lib = library()
        cheery = validate_persona({"name": "Cheery", "role": "Host", "blurb": "Cheery.", "voice": "bright",
                                   "allowed_tags": ["excited", "laughs"]}, lib.voices)[0]
        renamed = TemplateComposer(lib, tag_map={"excited": "very excited", "laughs": None})
        texts = [renamed.compose(cheery, seg, FULL, seed=s, remember=False).text for s in range(80)
                 for seg in ("song_intro", "song_back_announce", "filler")]
        joined = " ".join(texts)
        self.assertIn("[very excited]", joined)
        self.assertNotIn("[excited]", joined)
        self.assertNotIn("[laughs]", joined)
        for text in texts:
            self.assertEqual(audit_speech_text(text, cheery.speaker, ["very excited"]), [])
            self.assertNotRegex(text, r"\[\s|\s\]|  ")

    def test_every_effect_a_built_in_host_may_use_is_in_its_script_style(self):
        lib = library()
        for persona in load_builtin_personas(lib.voices).values():
            pack = lib.voices[persona.voice]
            written = {tag for ts in pack.templates.values() for t in ts for b in t.beats for tag in b.tags}
            written |= {tag for es in pack.extensions.values() for e in es for tag in e.beat.tags}
            for tag in persona.allowed_tags:
                self.assertIn(tag, written, f"{persona.id} may use [{tag}] but no line of the {persona.voice} style has it")

    def test_every_built_in_host_really_uses_each_effect_it_may(self):
        service = new_service()
        for persona in service.personas():
            for tag in persona.allowed_tags:
                found = any(f"[{tag}]" in service.compose(persona.id, kind, FULL, seed=seed, dry_run=True).text
                            for seed in range(300) for kind in ("song_intro", "song_back_announce", "filler"))
                self.assertTrue(found, f"{persona.id} never said [{tag}]")

    def test_lines_with_an_effect_the_host_may_not_use_do_not_throw_the_length_off(self):
        # the tag is removed from the finished script, so the pause it would have taken must not be counted either
        service = new_service()
        for persona_id in ("hazel", "nova", "wren", "dusty", "sable"):
            for kind in ("song_intro", "song_back_announce", "filler"):
                for target in (6, 9, 12):
                    for seed in range(40):
                        script = service.compose(persona_id, kind, FULL, seed=seed, target_seconds=target, dry_run=True)
                        # judged on the unrounded estimate, the one the lines were chosen by
                        miss = abs(estimate_raw(script.text, service.persona(persona_id).wps) - target)
                        self.assertLessEqual(miss, service.composer.tolerance(target) + 1e-9,
                                             f"{persona_id}/{kind} {target}s #{seed}: {script.est_seconds} ({script.plain_text})")

    def test_effects_add_to_the_time_estimate(self):
        service = new_service()
        for seed in range(80):
            script = service.compose("dex", "song_intro", FULL, seed=seed, target_seconds=10, dry_run=True)
            if "[" in script.text:
                self.assertEqual(script.est_seconds, estimate_seconds(script.text, 2.9))
                self.assertGreater(estimate_raw(script.text, 2.9), estimate_raw(script.plain_text, 2.9))
                return
        self.fail("no script used an effect")


class MiscTests(unittest.TestCase):
    def test_joining_names(self):
        self.assertEqual(join_names([]), "")
        self.assertEqual(join_names(["A"]), "A")
        self.assertEqual(join_names(["A", "B"]), "A and B")
        self.assertEqual(join_names(["A", "B", "C"]), "A, B and C")
        self.assertEqual(join_names(["A", "B", "C", "D", "E"]), "A, B, C and others")

    def test_the_composer_makes_no_network_call(self):
        service = new_service()
        with no_network():
            for segment in SEGMENT_IDS:
                service.compose("nova", segment, FULL, seed=1)
            for fmt in ("word-salad-poem", "station-almanac", "airwave-forecast"):
                service.compose("nova", None, FULL, format=fmt, seed=1)


if __name__ == "__main__":
    unittest.main()
