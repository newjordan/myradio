"""The compatibility rules: every rule is shown to fire on a deliberately broken style, and to stay
quiet on the legitimate cases that look similar. The shipped catalog must pass them all.

Passing these rules means a style does not contradict itself in text. It says nothing about how a
music model will play it."""
import copy
import json
import re
import unittest
from importlib.resources import files
from pathlib import Path

from myradio.prompts import RULES, check_arrangement, check_library, check_palette, load_library
from myradio.prompts.compat import ERROR, WARNING, check_preset, errors
from myradio.prompts.instruments import BASS_ROLE_WORDS, INSTRUMENTS, TRADITIONS, find_instruments
from myradio.prompts.library import Arrangement, Palette

DATA = files("myradio.prompts").joinpath("data")
DOCS = Path(__file__).resolve().parents[1] / "docs" / "PROMPTS.md"


def doc(name, kind="palettes"):
    return json.loads(DATA.joinpath(name).read_text(encoding="utf-8"))[kind]


BASE = next(p for p in doc("palettes-1.1.0.json") if p["id"] == "celtic-session-reels")
ARRANGEMENT = doc("arrangements-1.1.0.json", "arrangements")[0]
# The seven warnings the original 1.0.0 catalog carries: one instrument serves as lead and as support.
LEGACY_WARNINGS = {
    ("instrument.duplicate", "palette:soul-jazz-organ", "supports[1]"),
    ("instrument.duplicate", "palette:neo-soul", "supports[0]"),
    ("instrument.duplicate", "palette:broken-beat", "supports[0]"),
    ("instrument.duplicate", "palette:synthwave-horizon", "supports[0]"),
    ("instrument.duplicate", "palette:impressionist-piano", "supports[0]"),
    ("instrument.duplicate", "palette:string-quartet", "supports[1]"),
    ("instrument.duplicate", "palette:desert-guitar", "supports[1]"),
}


def rules_for(mutate, *, base=BASE, arrangement=False):
    data = copy.deepcopy(ARRANGEMENT if arrangement else base)
    mutate(data)
    parsed = Arrangement.parse(data, "x") if arrangement else Palette.parse(data, "x")
    found = (check_arrangement if arrangement else check_palette)(parsed)
    return {item.rule for item in found}


def set_text(path, value):
    def apply(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return apply


class ShippedCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = load_library()
        cls.legacy_ids = ({p.id for p in cls.library.through("1.0.0").palettes}
                          | {a.id for a in cls.library.through("1.0.0").arrangements})

    def test_the_whole_catalog_passes_every_rule(self):
        report = check_library(self.library)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["errors"], [])
        self.assertEqual((report["palettes"], report["arrangements"]), (71, 18))
        self.assertEqual(report["rules"], len(RULES))

    def test_new_content_has_no_findings_at_all(self):
        findings = [f for p in self.library.palettes if p.id not in self.legacy_ids for f in check_palette(p)]
        findings += [f for a in self.library.arrangements if a.id not in self.legacy_ids for f in check_arrangement(a)]
        self.assertEqual([f.to_dict() for f in findings], [])

    def test_original_content_has_no_errors_and_only_the_known_warnings(self):
        legacy = self.library.through("1.0.0")
        findings = [f for p in legacy.palettes for f in check_palette(p)] + [f for a in legacy.arrangements for f in check_arrangement(a)]
        self.assertEqual([f for f in findings if f.severity == ERROR], [])
        self.assertEqual({(f.rule, f.subject, f.field) for f in findings}, LEGACY_WARNINGS)

    def test_every_instrument_named_in_a_role_is_recognised(self):
        for palette in self.library.palettes:
            for text in (*palette.leads, *palette.supports):
                self.assertTrue(find_instruments(text), f"{palette.id}: {text}")
            self.assertTrue(find_instruments(palette.bass) or BASS_ROLE_WORDS.search(palette.bass), palette.id)


class RuleCoverageTests(unittest.TestCase):
    def test_every_rule_is_documented_and_described(self):
        text = DOCS.read_text(encoding="utf-8")
        for rule, (severity, description) in RULES.items():
            with self.subTest(rule=rule):
                self.assertIn(severity, (ERROR, WARNING))
                self.assertTrue(description.endswith("."))
                self.assertIn(f"| `{rule}` | {severity} | {description} |", text,
                              "docs/PROMPTS.md must list every rule with its severity and exact description")

    def test_the_documented_rule_list_has_no_stale_entries(self):
        documented = set(re.findall(r"`((?:meter|tempo|instrument|energy|complexity|text|arrangement)\.[a-z-]+)`", DOCS.read_text(encoding="utf-8")))
        self.assertEqual(documented - set(RULES), set())

    def test_the_unbroken_base_passes_and_each_rule_below_is_a_real_rule(self):
        self.assertEqual(rules_for(lambda d: None), set())
        self.assertEqual(rules_for(lambda d: None, arrangement=True), set())


# (rule that must fire, how to break the base palette). The base is celtic-session-reels: 4/4, driving, 112 to 126.
PALETTE_BREAKS = [
    ("meter.format", lambda d: d.update(meter="four/four")),
    ("meter.format", lambda d: d.update(meter="7/8 (2+2+3")),
    ("meter.grouping-sum", lambda d: d.update(meter="7/8 (2+2+2)")),
    ("meter.grouping-sum", lambda d: d.update(meter="6/8 (3+2)")),
    ("meter.mention", set_text(("rhythm", "simple"), "A steady pulse in 5/4 with even eighth notes.")),
    ("meter.mention", set_text(("rhythm", "simple"), "A steady pulse in 12/8 with even eighth notes.")),
    ("meter.beat-count", set_text(("rhythm", "simple"), "A steady five-beat cycle with even eighth notes.")),
    ("meter.beat-count", set_text(("rhythm", "simple"), "A steady 7-beat cycle with even eighth notes.")),
    ("meter.grouping-mention", set_text(("rhythm", "layered"), "Group the notes 3+3+3 for one part.")),
    ("meter.grouping-mention", lambda d: (d.update(meter="7/8 (2+2+3)"), d["rhythm"].update(layered="Play 3+3+1 for one part."))),
    ("meter.tag", lambda d: d.update(tags=d["tags"] + ["waltz"])),
    ("meter.tag", lambda d: d.update(tags=d["tags"] + ["odd-meter"])),
    ("tempo.range", lambda d: d.update(bpm=[60, 120])),
    ("tempo.number", set_text(("rhythm", "simple"), "A steady pulse at 80 BPM with even eighth notes.")),
    ("tempo.word", lambda d: (d.update(bpm=[130, 150]), d["rhythm"].update(simple="A slow groove with even eighth notes."))),
    ("tempo.word", lambda d: (d.update(bpm=[70, 90], energy="steady"), d["rhythm"].update(simple="A brisk tempo with even eighth notes."))),
    ("tempo.energy", lambda d: d.update(energy="calm", bpm=[130, 140])),
    ("tempo.energy", lambda d: d.update(energy="steady", bpm=[40, 70])),
    ("tempo.energy", lambda d: d.update(energy="driving", bpm=[60, 80])),
    ("instrument.lead-melodic", lambda d: d.update(leads=["a steady conga pattern", "wooden flute with rolls"])),
    ("instrument.bass-slot", lambda d: d.update(bass="bright flute figures")),
    ("instrument.bass-only-elsewhere", lambda d: d.update(leads=["upright bass solo", "wooden flute with rolls"])),
    ("instrument.bass-only-elsewhere", lambda d: d.update(supports=["electric bass fills", "bodhran with tipper accents"])),
    ("instrument.tradition-mix", lambda d: d.update(leads=["sitar with sliding bends", "wooden flute with rolls"])),
    ("instrument.tradition-mix", set_text(("production",), "Warm room sound with a tabla and a koto added.")),
    ("instrument.purity", lambda d: d.update(supports=["a second synthesizer pad", "bodhran with tipper accents"])),
    ("instrument.purity", lambda d: d.update(tags=["chiptune", "retro", "playful"], leads=["pulse-wave lead", "wooden flute with rolls"])),
    ("instrument.microtonal", lambda d: (d["harmony"].update(simple="Stay in one maqam around a steady tonic."),
                                         d.update(supports=["piano chords", "bodhran with tipper accents"]))),
    ("instrument.negation", set_text(("production",), "Live-room warmth; no bodhran and no fiddle.")),
    ("instrument.negation", lambda d: (d.update(supports=["electric piano chords", "bodhran with tipper accents"],
                                               tags=["celtic-inspired", "reel", "dance"]),
                                       d.update(production="Warm and dry; no electric instruments."))),
    ("instrument.stacking", lambda d: d.update(leads=["trumpet, saxophone, clarinet and flute together", "wooden flute with rolls"])),
    ("instrument.stacking", lambda d: d.update(leads=["trumpet", "wooden flute"],
                                               supports=["accordion, piano and harp", "bodhran with tipper accents"],
                                               bass="upright bass, cello and tuba together")),
    ("energy.intense-instrument", lambda d: d.update(energy="calm", bpm=[100, 110], leads=["distorted electric guitar", "wooden flute with rolls"])),
    ("energy.loud-claim", lambda d: (d.update(energy="calm", bpm=[100, 110]), d.update(production="Crushing, aggressive low end."))),
    ("complexity.loudness", set_text(("harmony", "intricate"), "Rise to a climax that gets louder with every pass.")),
    ("complexity.loudness", set_text(("rhythm", "intricate"), "Add a bass drop before the final chorus.")),
    ("text.speech-markup", lambda d: d.update(moods=["a DJ announcing the next song", "warm evening"])),
    ("text.speech-markup", lambda d: d.update(motifs=["a rising line [laughs] and a fall", "a held note"])),
    ("text.speech-markup", lambda d: d.update(production="Warm room. <|speaker:1|> hello.")),
    ("text.punctuation", lambda d: d.update(bass="a low-tuned guitar playing open bass notes.")),
    ("text.punctuation", set_text(("production",), "Live-room warmth, dry and bright")),
    # a bar must have at least one beat, written in plain digits (a zero-length bar once made the lyric outline loop forever)
    ("meter.format", lambda d: d.update(meter="0/4")),
    ("meter.format", lambda d: d.update(meter="00/8")),
    ("meter.format", lambda d: d.update(meter="01/4")),
    ("meter.format", lambda d: d.update(meter=chr(0x660) + "/4")),
    ("meter.format", lambda d: d.update(meter="7/8 (2+0+5)")),
    # wording that belongs to a host or announcer, beyond the words DJ and announcer themselves
    ("text.speech-markup", set_text(("production",), "Warm room. Hello listeners, welcome back to the show.")),
    ("text.speech-markup", lambda d: d.update(moods=["you are listening to the best", "warm evening"])),
    ("text.speech-markup", lambda d: d.update(name="Welcome Back Reels")),
]
# duplicate warning: the same instrument as lead and support without saying "second"
DUPLICATE = ("instrument.duplicate", lambda d: d.update(supports=["fiddle chord answers", "bodhran with tipper accents"]))
UNKNOWN = ("instrument.unrecognized", lambda d: d.update(supports=["a mystery contraption", "bodhran with tipper accents"]))


class PaletteRuleTests(unittest.TestCase):
    def test_each_rule_fires_on_a_broken_palette(self):
        seen = set()
        for rule, mutate in PALETTE_BREAKS + [DUPLICATE, UNKNOWN]:
            with self.subTest(rule=rule, case=str(mutate)[-40:]):
                self.assertIn(rule, rules_for(mutate))
                seen.add(rule)
        palette_rules = {r for r in RULES if not r.startswith("arrangement.")}
        self.assertEqual(palette_rules - seen, set(), "every palette rule needs a failing example")

    def test_messages_are_plain_language_with_no_internal_names(self):
        for rule, mutate in PALETTE_BREAKS[:12]:
            data = copy.deepcopy(BASE)
            mutate(data)
            for finding in check_palette(Palette.parse(data, "x")):
                self.assertTrue(finding.message.endswith((".", ")")), finding.message)
                for jargon in ("regex", "Traceback", "None", "palettes[", "NoneType", "ValueError"):
                    self.assertNotIn(jargon, finding.message)

    def test_severity_matches_the_rule_table_and_findings_sort_errors_first(self):
        data = copy.deepcopy(BASE)
        data["supports"] = ["a mystery contraption", "bodhran with tipper accents"]
        data["meter"] = "four/four"
        found = check_palette(Palette.parse(data, "x"))
        self.assertEqual([f.severity for f in found], sorted((f.severity for f in found), key=lambda s: s != ERROR))
        for item in found:
            self.assertEqual(item.severity, RULES[item.rule][0])
            self.assertEqual(item.subject, "palette:celtic-session-reels")

    def test_legitimate_cases_that_look_similar_stay_quiet(self):
        cases = {
            "a deliberate fusion may mix traditions": lambda d: d.update(tags=d["tags"] + ["fusion"], leads=["sitar with sliding bends", "wooden flute with rolls"]),
            "half-time lets a calm style run fast": lambda d: d.update(energy="calm", bpm=[130, 140], tags=d["tags"] + ["half-time"]),
            "a cross-rhythm of the same bar length": set_text(("rhythm", "simple"), "A steady 4/4 pulse against a felt 2/2 lilt over even eighth notes."),
            "a beat count named as a cross-rhythm": set_text(("rhythm", "simple"), "Place a five-beat phrase over the four-beat bar."),
            "a rearranged grouping": set_text(("rhythm", "layered"), "Rotate the accent to 2+3+3 for one part, then back."),
            "a negated word is not a claim": set_text(("production",), "Dry and bright; no aggressive peaks and no louder build."),
            "no piano is fine when none is named": set_text(("production",), "Warm and dry; no piano."),
            "without crowding the guitar is about an action": set_text(("bass",), "a low-tuned guitar playing open bass notes without crowding the guitar"),
            "a second instrument may share a family": lambda d: d.update(supports=["a second fiddle in harmony", "bodhran with tipper accents"]),
            "a retuned fixed-pitch instrument": lambda d: (d["harmony"].update(simple="Stay in one maqam around a steady tonic."),
                                                           d.update(supports=["retuned accordion chords", "bodhran with tipper accents"])),
            "a three-instrument part": lambda d: d.update(leads=["fiddle, flute and whistle in unison", "wooden flute with rolls"]),
            "studio, radio and tuning are everyday musical words": set_text(("production",), "Warm studio room sound; the players tune in to each other and nothing is radio-polished."),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assertEqual(rules_for(mutate), set())

    def test_microtonal_claims_only_matter_for_fixed_pitch_instruments(self):
        quiet = lambda d: d["harmony"].update(simple="Stay in one maqam around a steady tonic.")
        self.assertEqual(rules_for(quiet), set())
        loud = lambda d: (quiet(d), d.update(supports=["upright piano chords", "bodhran with tipper accents"]))
        self.assertEqual(rules_for(loud), {"instrument.microtonal"})

    def test_the_classic_clashing_stacks_are_caught(self):
        bagpipes = lambda d: d.update(leads=["sitar with sliding bends", "highland bagpipes with a steady drone"])
        self.assertIn("instrument.tradition-mix", rules_for(bagpipes))
        whistle = lambda d: d.update(leads=["koto melody with plucked notes", "tin whistle with rolls"])
        self.assertIn("instrument.tradition-mix", rules_for(whistle))
        self.assertEqual(rules_for(lambda d: d.update(leads=["highland bagpipes with a steady drone", "wooden flute with rolls"])), set())
        named_waltz = lambda d: d.update(name="Evening Waltz", id="evening-waltz")
        self.assertIn("meter.tag", rules_for(named_waltz))
        self.assertNotIn("meter.tag", rules_for(lambda d: (named_waltz(d), d.update(meter="3/4", bpm=[100, 118]))))

    def test_a_chiptune_or_synthetic_style_may_not_name_acoustic_instruments(self):
        base = {"tags": ["chiptune", "retro", "playful"], "leads": ["pulse-wave lead with fast vibrato", "second pulse-wave lead"],
                "supports": ["quick arpeggios on one channel", "noise-channel percussion"], "bass": "triangle-wave bass playing octave jumps"}
        self.assertEqual(rules_for(lambda d: d.update(base)), set())
        self.assertEqual(rules_for(lambda d: d.update(base, supports=["a violin pad", "noise-channel percussion"])), {"instrument.purity"})

    def test_preset_check_joins_palette_and_arrangement_rules(self):
        palette = Palette.parse(copy.deepcopy(BASE), "x")
        arrangement = Arrangement.parse(copy.deepcopy(ARRANGEMENT), "x")
        self.assertEqual(check_preset(palette, arrangement), [])
        bad = copy.deepcopy(ARRANGEMENT)
        bad["ending"] = "End on a trumpet solo."
        found = check_preset(palette, Arrangement.parse(bad, "x"))
        self.assertEqual([f.rule for f in errors(found)], ["arrangement.instrument"])


class ArrangementRuleTests(unittest.TestCase):
    def test_each_arrangement_rule_fires(self):
        cases = [
            ("arrangement.instrument", set_text(("dynamics",), "Let the trumpet lead and the drums drop out gently.")),
            ("arrangement.instrument", lambda d: d["sections"]["simple"].__setitem__(0, "Start with a lone piano melody.")),
            ("arrangement.tempo-meter", set_text(("ending",), "Slow to 60 BPM and fade out.")),
            ("arrangement.tempo-meter", set_text(("ending",), "Settle into 3/4 and fade out.")),
            ("arrangement.tempo-meter", lambda d: d["development"].update(simple="Use a five-beat answer for each call.")),
            ("complexity.loudness", set_text(("dynamics",), "Build to a climax that gets louder.")),
            ("text.speech-markup", set_text(("dynamics",), "Let the announcer speak over the quiet part.")),
            ("text.punctuation", set_text(("ending",), "Let the sound decay naturally")),
            ("text.speech-markup", set_text(("ending",), "Say welcome back to the listeners and fade out.")),
        ]
        for rule, mutate in cases:
            with self.subTest(rule=rule, case=str(mutate)[-30:]):
                self.assertIn(rule, rules_for(mutate, arrangement=True))

    def test_roles_and_shapes_are_fine_and_so_is_negated_loudness(self):
        quiet = set_text(("dynamics",), "Keep the lead, the answer and the foundation balanced, with no rise in volume or any drop.")
        self.assertEqual(rules_for(quiet, arrangement=True), set())


class LexiconTests(unittest.TestCase):
    def test_longer_matches_win_so_one_word_is_not_two_instruments(self):
        keys = [i.key for i in find_instruments("warm bass clarinet and a bass drum")]
        self.assertEqual(keys, ["bass-clarinet", "bass-drum"])
        self.assertEqual([i.key for i in find_instruments("brass-like synth chords")], ["synthesizer"])
        self.assertEqual([i.key for i in find_instruments("a 17-string bass koto")], ["bass-koto"])
        self.assertEqual([i.key for i in find_instruments("nylon-string guitar and steel-string guitar")], ["guitar"])
        self.assertEqual(find_instruments("Synthesize the variations in a clear reprise."), [])

    def test_the_table_is_consistent(self):
        keys = [i.key for i in INSTRUMENTS]
        self.assertEqual(len(keys), len(set(keys)))
        for item in INSTRUMENTS:
            self.assertTrue(set(item.flags) <= set("mbBpefxni"), item.key)
            if item.has("B"):
                self.assertTrue(item.has("b"), "a bass-only instrument can fill the bass slot")
            self.assertFalse(item.pattern.search(""), "a pattern must never match empty text")
        self.assertIn("near-east", TRADITIONS)
        self.assertGreaterEqual(len(TRADITIONS), 15)


if __name__ == "__main__":
    unittest.main()
