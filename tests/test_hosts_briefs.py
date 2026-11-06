"""Research briefs become sourced updates; entertainment formats are flagged and disclosed."""
import copy
import json
import os
import random
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hosts_support import BRIEF, FULL, library_with, new_service, no_network, research_brief, research_item, voice_data  # noqa: E402

from myradio.hosts import Facts, HostsError, NothingToRead, TemplateComposer, audit_speech_text, brief_from_research, grounding_lint, verbatim_extract  # noqa: E402
from myradio.hosts.briefs import HARD_MAX_SENTENCE_WORDS, MAX_ITEMS, clean_url, compose_update, iso_time, parse_brief, split_sentences  # noqa: E402
from myradio.hosts.personas import load_builtin_personas  # noqa: E402
from myradio.hosts.textsafe import MAX_SCRIPT_CHARS  # noqa: E402
from myradio.hosts.formats import FORMATS  # noqa: E402
from myradio.hosts.timing import count_words  # noqa: E402


def sentences(text):
    return [text[a:b] for a, b in split_sentences(text)]


class IsoTimeAndUrlTests(unittest.TestCase):
    def test_times_become_iso_utc_or_nothing(self):
        self.assertEqual(iso_time(1762282800), "2025-11-04T19:00:00+00:00")
        self.assertEqual(iso_time(1762282800.9), "2025-11-04T19:00:00+00:00")
        self.assertEqual(iso_time("2026-11-05T09:30:00Z"), "2026-11-05T09:30:00+00:00")
        self.assertEqual(iso_time("2026-11-05T09:30:00+02:00"), "2026-11-05T07:30:00+00:00")
        for bad in (None, True, "", "yesterday", "2026-11-05T09:30:00", "2026-13-45T00:00:00+00:00", -5, 10 ** 12, float("nan"),
                    float("inf"), [], {}, "9" * 100):
            self.assertIsNone(iso_time(bad), bad)

    def test_a_time_is_understood_the_same_way_on_every_python_version(self):
        # datetime.fromisoformat takes more forms from Python 3.11 than before; these are read by this module's own rule
        for text, expected in (("2026-11-05T09:30:00+0200", "2026-11-05T07:30:00+00:00"), ("2026-11-05T09:30:00+02", "2026-11-05T07:30:00+00:00"),
                               ("2026-11-05T09:30:00.5+02:00", "2026-11-05T07:30:00+00:00"), ("2026-11-05T09:30:00,25Z", "2026-11-05T09:30:00+00:00"),
                               ("2026-11-05 09:30:00Z", "2026-11-05T09:30:00+00:00"), ("2026-11-05T09:30+02:00", "2026-11-05T07:30:00+00:00"),
                               ("2026-11-05T09:30:00.123456789-05:30", "2026-11-05T15:00:00+00:00"), ("  2026-11-05T09:30:00z ", "2026-11-05T09:30:00+00:00")):
            with self.subTest(text=text):
                self.assertEqual(iso_time(text), expected)
        for bad in ("20261105T093000Z", "2026-11-05T09:30:00+25:00", "2026-11-05T09:30:00+02:61", "2026-11-05T24:00:00Z", "2026-02-30T09:30:00Z",
                    "2026-11-05", "2026-11-05T09:30:00+2:00", "2026-11-05T09:30:00 UTC", "2026-11-05T9:30:00Z", "2026-W45-3T09:30:00Z"):
            with self.subTest(bad=bad):
                self.assertIsNone(iso_time(bad), bad)

    def test_only_plain_web_addresses_are_kept_for_citations(self):
        self.assertEqual(clean_url("https://example.org/a?b=1"), "https://example.org/a?b=1")
        for bad in ("javascript:alert(1)", "ftp://x.org", "example.org", "https://exa mple.org", "https://x.org/<b>",
                    "https://x.org/" + "a" * 400, "", None, 5, "https://x.org/'quote"):
            self.assertIsNone(clean_url(bad), bad)


class SentenceTests(unittest.TestCase):
    def test_sentences_split_at_real_ends_only(self):
        table = {
            "One. Two. Three.": ["One.", "Two.", "Three."],
            "Dr. Smith said hello. Next, please.": ["Dr. Smith said hello.", "Next, please."],
            "Mr. and Mrs. Jones arrived. They sat down.": ["Mr. and Mrs. Jones arrived.", "They sat down."],
            "It cost 3.5 million. That is a lot.": ["It cost 3.5 million.", "That is a lot."],
            "J. K. Example wrote it. It sold well.": ["J. K. Example wrote it.", "It sold well."],
            "Is it real? Yes! Of course.": ["Is it real?", "Yes!", "Of course."],
            'She said "stop." Then she left.': ['She said "stop."', "Then she left."],
            "Wait... what happened? Nobody knows.": ["Wait... what happened?", "Nobody knows."],
            "No. 5 was the best. No. 6 was fine.": ["No. 5 was the best.", "No. 6 was fine."],
            "No full stop at the end": ["No full stop at the end"],
            "lower case after. is not a new sentence.": ["lower case after. is not a new sentence."],
        }
        for text, expected in table.items():
            with self.subTest(text=text):
                self.assertEqual(sentences(text), expected)

    def test_titles_months_weekdays_and_numbers_after_a_short_word_do_not_end_a_sentence(self):
        table = {
            "Sen. Warren said the bill would pass. Critics disagreed.": ["Sen. Warren said the bill would pass.", "Critics disagreed."],
            "The council met on Nov. 5 to vote on the budget. It passed.": ["The council met on Nov. 5 to vote on the budget.", "It passed."],
            "Oct. 31 is Halloween. Candy is cheap.": ["Oct. 31 is Halloween.", "Candy is cheap."],
            "Gov. Park and Lt. Col. Reed spoke at the hall. Nobody objected.": ["Gov. Park and Lt. Col. Reed spoke at the hall.", "Nobody objected."],
            "Doors open Sat. Dec. 4 at noon. Bring a coat.": ["Doors open Sat. Dec. 4 at noon.", "Bring a coat."],
            "Acme Corp. and Smith Bros. merged. Shares rose.": ["Acme Corp. and Smith Bros. merged.", "Shares rose."],
            "Fig. 3 shows the result. See the paper.": ["Fig. 3 shows the result.", "See the paper."],
            "Capt. Reyes, Sgt. Cole and Adm. Bell arrived. They sat down.": ["Capt. Reyes, Sgt. Cole and Adm. Bell arrived.", "They sat down."],
            "Rev. Ann Lee and Hon. Bo Cho spoke. Then it rained.": ["Rev. Ann Lee and Hon. Bo Cho spoke.", "Then it rained."],
            "Meet at 9 a.m. on Mon. Bring lunch. Thanks.": ["Meet at 9 a.m. on Mon. Bring lunch.", "Thanks."],
            "Washington, D.C. Residents voted. It was close.": ["Washington, D.C. Residents voted.", "It was close."],
            # in lower case these are ordinary words, so the sentence does end
            "He sat. Then he left.": ["He sat.", "Then he left."],
            "We watched the sun. Then it set.": ["We watched the sun.", "Then it set."],
            "She said it was the rep. Then she left.": ["She said it was the rep.", "Then she left."],
        }
        for text, expected in table.items():
            with self.subTest(text=text):
                self.assertEqual(sentences(text), expected)

    def test_sentence_spans_cover_the_text_without_changing_it(self):
        text = "First one. Second one! Third one? Fourth."
        spans = split_sentences(text)
        self.assertEqual(" ".join(text[a:b] for a, b in spans), text)

    def test_hostile_text_is_split_quickly(self):
        started = time.perf_counter()
        for text in ("a. " * 20000, "A. " * 20000, ". " * 50000, "x" * 1000000, "Dr. " * 30000):
            split_sentences(text[:20000])
        self.assertLess(time.perf_counter() - started, 2.0)


class VerbatimExtractTests(unittest.TestCase):
    def test_an_extract_is_always_a_prefix_of_whole_sentences(self):
        text = "Saturn is bright tonight. Its rings are open. Look south after dark. Bring a blanket."
        self.assertEqual(verbatim_extract(text, 1), "Saturn is bright tonight.")
        self.assertEqual(verbatim_extract(text, 9), "Saturn is bright tonight. Its rings are open.")
        self.assertEqual(verbatim_extract(text, 1000), text)
        self.assertEqual(verbatim_extract("One sentence only"), "One sentence only")

    def test_the_first_sentence_is_read_in_full_or_not_at_all(self):
        long_sentence = " ".join(["word"] * (HARD_MAX_SENTENCE_WORDS + 1)) + "."
        self.assertIsNone(verbatim_extract(long_sentence))
        fits = " ".join(["word"] * HARD_MAX_SENTENCE_WORDS) + "."
        self.assertEqual(verbatim_extract(fits, 5), fits)       # longer than the budget, but whole

    def test_a_web_address_stops_the_extract_and_is_never_read(self):
        self.assertEqual(verbatim_extract("Good news today. Read more at https://example.org/x. Thanks."), "Good news today.")
        self.assertIsNone(verbatim_extract("See example.com for details. More text here."))
        self.assertIsNone(verbatim_extract("Visit www.example.org now. Later text."))

    def test_no_extract_is_a_title_or_a_month_on_its_own(self):
        for text, first, whole in (
                ("Sen. Warren said the bill would pass. Critics disagreed.", "Sen. Warren said the bill would pass.", None),
                ("The council met on Nov. 5 to vote on the budget. It passed.", "The council met on Nov. 5 to vote on the budget.", None),
                ("Oct. 31 is Halloween. Candy is cheap.", "Oct. 31 is Halloween.", None),
                ("Brig. Gen. Smith said it was time. Nobody moved.", "Brig. Gen. Smith said it was time.", None)):
            with self.subTest(text=text):
                self.assertEqual(verbatim_extract(text, 1), first)
                self.assertTrue(verbatim_extract(text).startswith(first))
        # a reading of fewer than four words is joined to the next sentence, so it is never read alone
        self.assertEqual(verbatim_extract("Adm. Nimitz arrived at noon. Crowds cheered loudly today.", 1), "Adm. Nimitz arrived at noon.")
        self.assertEqual(verbatim_extract("Adm. Nimitz arrived. Crowds cheered loudly today.", 1), "Adm. Nimitz arrived. Crowds cheered loudly today.")
        self.assertEqual(verbatim_extract("Yes. The vote passed by a wide margin. Done.", 1), "Yes. The vote passed by a wide margin.")
        # ... unless that is not possible: then a complete short sentence is read, and a bare fragment is not
        self.assertEqual(verbatim_extract("Storm hits Texas."), "Storm hits Texas.")
        self.assertEqual(verbatim_extract("Good news today. Read more at https://example.org/x. Thanks."), "Good news today.")
        for fragment in ("Sen.", "Nov.", "12.", "Texas.", "Adm. Read more at example.org now."):
            self.assertIsNone(verbatim_extract(fragment), fragment)

    def test_no_kind_of_web_address_is_ever_part_of_an_extract(self):
        for address in ("nasa.gov/missions", "city.gov", "stanford.edu/papers/42", "astro.garden", "bit.ly/abc", "example.xyz", "radio.ai",
                        "foo.sh", "press@nasa.gov", "EXAMPLE.ORG", "https://example.org/x", "www.example.org", "example dot garden"):
            with self.subTest(address=address):
                self.assertEqual(verbatim_extract(f"The launch is on schedule. Details are at {address} for anyone curious. Thanks."),
                                 "The launch is on schedule.")
                self.assertIsNone(verbatim_extract(f"Details are at {address} for anyone curious. The launch is on schedule."))

    def test_extracts_cannot_be_made_to_crawl_and_only_the_start_of_a_long_text_is_looked_at(self):
        started = time.perf_counter()
        for text in ("a." * 20000, "a.b" * 20000, "A. " * 20000, "x" * 1000000, "Sen. " * 20000, "word " * 100000):
            verbatim_extract(text)
        self.assertLess(time.perf_counter() - started, 2.0)
        long = "The first sentence is short and clear. " + "More words follow here. " * 1000
        extract = verbatim_extract(long)
        self.assertTrue(long.startswith(extract))
        self.assertEqual(extract, "The first sentence is short and clear. More words follow here. More words follow here. More words follow here. More words follow here. More words follow here. More words follow here. More words follow here. More words follow here. More words follow here.")

    def test_degenerate_input(self):
        for value in ("", "   ", None, 5, [], "...", "?!"):
            result = verbatim_extract(value)
            self.assertTrue(result is None or isinstance(result, str))
        self.assertIsNone(verbatim_extract(""))

    def test_random_text_never_comes_back_altered_or_cut_mid_sentence(self):
        rng = random.Random(5)
        words = ["Saturn", "rings", "Dr.", "3.5", "No.", "J.", "said", "the", "bright", "meteor", "What?", "Yes!", "end.", "x.com", "café"]
        for _ in range(400):
            text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 40)))
            extract = verbatim_extract(text, rng.randint(1, 50))
            if extract is None:
                continue
            self.assertTrue(text.startswith(extract), (text, extract))
            self.assertNotEqual(extract, "")
            spans = split_sentences(text)
            ends = {b for _, b in spans}
            self.assertIn(len(extract), ends)


class ParseBriefTests(unittest.TestCase):
    def test_a_good_brief_is_read_as_given(self):
        brief = parse_brief(BRIEF)
        self.assertEqual((brief.id, brief.topic, brief.generated_at), ("brief-1", "Backyard astronomy", "2026-11-05T07:42:00+00:00"))
        self.assertEqual([i.id for i in brief.items], ["i1", "i2", "i3"])
        self.assertEqual([i.kind for i in brief.items], ["reported", "commentary", "uncertain"])
        self.assertEqual(brief.items[0].published_at, "2026-11-05T06:10:00+00:00")
        self.assertEqual(brief.items[1].published_at, "2025-11-04T19:00:00+00:00")
        self.assertEqual(brief.items[0].url, "https://example.org/meteor")
        self.assertEqual((brief.warnings, brief.dropped), ((), ()))

    def test_bad_shapes_are_plain_errors(self):
        for bad in (None, "text", 5, [], [BRIEF]):
            with self.subTest(bad=repr(bad)[:20]), self.assertRaisesRegex(HostsError, "research brief"):
                parse_brief(bad)
        for bad in ({}, {"topic": "x"}, {"items": "no"}, {"items": 5}, {"items": {"a": 1}}):
            with self.subTest(bad=bad), self.assertRaisesRegex(HostsError, "items"):
                parse_brief(bad)

    def test_items_that_cannot_be_read_with_their_source_are_dropped_with_a_reason(self):
        brief = parse_brief({"topic": "T", "items": [
            {"id": "a", "text": "Fine.", "source": "Example"}, {"id": "b", "text": "No source."}, {"id": "c", "text": "", "source": "S"},
            {"id": "d", "text": "Fine.", "source": "Example"}, "not an item", 5, None,
            {"id": "e", "text": "<|speaker:1|>", "source": "[laughs]"}]})
        self.assertEqual([i.id for i in brief.items], ["a"])
        reasons = {d["item_id"]: d["reason"] for d in brief.dropped}
        self.assertEqual(reasons["b"], "no_source")
        self.assertEqual(reasons["c"], "no_text")
        self.assertEqual(reasons["d"], "duplicate")
        self.assertEqual(reasons["e"], "no_source")
        self.assertEqual(sum(1 for d in brief.dropped if d["reason"] == "not_an_item"), 3)
        for dropped in brief.dropped:
            self.assertTrue(dropped["message"].startswith("Left out"))

    def test_odd_values_are_reported_not_trusted(self):
        brief = parse_brief({"topic": "T", "items": [
            {"id": "a", "text": "One.", "source": "S", "kind": "gossip", "url": "javascript:x", "published_at": "2026-11-05T09:30:00"}]})
        # a kind nobody recognises is a claim of unknown reliability: it is introduced as not confirmed, never as reported
        self.assertEqual(brief.items[0].kind, "uncertain")
        self.assertIsNone(brief.items[0].url)
        self.assertIsNone(brief.items[0].published_at)
        self.assertEqual(len(brief.warnings), 3)

    def test_a_kind_nobody_recognises_is_introduced_as_not_confirmed_never_as_reported(self):
        def kind_of(entry):
            brief = parse_brief({"topic": "T", "items": [dict({"text": "One thing happened.", "source": "S"}, **entry)]})
            return brief.items[0].kind, brief.warnings

        self.assertEqual(kind_of({}), ("reported", ()))                       # no kind given: the caller vouches for it
        self.assertEqual(kind_of({"kind": None}), ("reported", ()))
        self.assertEqual(kind_of({"kind": ""}), ("reported", ()))
        for given, expected in (("reported", "reported"), ("commentary", "commentary"), ("announcement", "announcement"),
                                ("uncertain", "uncertain"), ("unverified", "uncertain")):
            self.assertEqual(kind_of({"kind": given}), (expected, ()), given)
        for odd in ("gossip", "REPORTED", 5, ["reported"], {"a": 1}, True, "rumour"):
            kind, warnings = kind_of({"kind": odd})
            self.assertEqual(kind, "uncertain", odd)
            self.assertEqual(len(warnings), 1)
            self.assertIn("not confirmed", warnings[0])

    def test_text_inside_brackets_is_read_as_written_and_symbols_that_cannot_be_read_are_not_guessed_at(self):
        def one(text):
            return parse_brief({"topic": "T", "items": [{"id": "x", "text": text, "source": "S"}]})

        brief = one("He said the policy would [not] help. Critics agreed.")
        self.assertEqual(brief.items[0].text, "He said the policy would not help. Critics agreed.")
        self.assertEqual(len(brief.warnings), 1)
        self.assertIn("brackets", brief.warnings[0])
        brief = one("Profits rose 3% (down from 5% last year). Details follow.")
        self.assertEqual(brief.items[0].text, "Profits rose 3% (down from 5% last year). Details follow.")
        self.assertEqual(brief.warnings, ())
        # something nobody said aloud is removed with its brackets; the words of a quotation are not
        brief = one("The mayor <|speaker:4|> spoke [laughs] at length [sic]. <b>Bold</b> claims followed.")
        self.assertEqual(brief.items[0].text, "The mayor spoke at length sic. Bold claims followed.")
        self.assertTrue(any("Voice codes" in w for w in brief.warnings))
        brief = one("\uff1c\uff5cspeaker:3\uff5c\uff1e A meteor was seen.")
        self.assertEqual(brief.items[0].text, "A meteor was seen.")
        # a < or > that is not a tag would change the meaning if dropped, so the item is left out, with a reason
        for text in ("The vote was 5 < 7 and the margin > 2. Details follow.", "If x<y then y>z. More.", "Heart <3 everyone. More."):
            brief = one(text)
            self.assertEqual(brief.items, (), text)
            self.assertEqual([d["reason"] for d in brief.dropped], ["symbols"], text)
            self.assertIn("cannot be read aloud", brief.dropped[0]["message"])

    def test_a_source_that_is_only_a_web_address_is_not_read(self):
        for source in ("https://example.org/feed", "example.org", "www.example.org/news", "user@example.org", "Space.com", "city.gov",
                       "Example News (example.com)", "example dot com"):
            with self.subTest(source=source):
                brief = parse_brief({"topic": "T", "items": [{"id": "x", "text": "One thing happened.", "source": source}]})
                self.assertEqual(brief.items, ())
                self.assertEqual([d["reason"] for d in brief.dropped], ["source_link"])
                self.assertIn("Give the source a name", brief.dropped[0]["message"])
        brief = parse_brief({"topic": "T", "items": [{"text": "One thing happened.", "source": "Example Dot Daily"}]})
        self.assertEqual(len(brief.items), 1)

    def test_everything_is_neutralized_and_bounded(self):
        brief = parse_brief({"id": "<|speaker:1|>x", "topic": "[laughs]Space " + "t" * 300, "items": [
            {"id": "i" * 300, "text": "Hello <|speaker:2|> [whispers] world. " * 100, "source": "Src <|speaker:3|>" + "s" * 300}]})
        item = brief.items[0]
        for text in (brief.id, brief.topic, item.id, item.text, item.source):
            self.assertFalse(set("<>[]{}|\\") & set(text), text)
        self.assertLessEqual(len(item.text), 1200)
        self.assertLessEqual(len(item.source), 80)
        self.assertLessEqual(len(item.id), 64)
        self.assertLessEqual(len(brief.topic), 80)

    def test_a_huge_brief_is_capped(self):
        brief = parse_brief({"topic": "T", "items": [{"id": f"i{n}", "text": f"Item number {n}.", "source": "S"} for n in range(500)]})
        self.assertEqual(len(brief.items), MAX_ITEMS)
        self.assertEqual(sum(1 for d in brief.dropped if d["reason"] == "extra"), 480)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.service = new_service()

    def update(self, persona="marlowe", brief=None, **kw):
        kw.setdefault("seed", 3)
        kw.setdefault("dry_run", True)
        return self.service.compose_update(persona, copy.deepcopy(brief or BRIEF), kw.pop("facts", {"station": "Porchlight FM"}), **kw)

    def test_the_result_has_the_documented_shape(self):
        script = self.update(target_seconds=60)
        data = script.to_dict()
        json.dumps(data)
        self.assertEqual((data["role"], data["library_kind"], data["segment"], data["persona"], data["source"]),
                         ("update", "speech", "update", "marlowe", "template"))
        self.assertTrue(data["text"].startswith("<|speaker:3|>"))
        self.assertEqual(audit_speech_text(data["text"], 3, []), [])
        self.assertTrue(data["template"].count("+") == 1)
        self.assertEqual(len(data["citations"]), 3)
        for position, citation in enumerate(data["citations"], 1):
            self.assertEqual(set(citation), {"position", "brief_id", "item_id", "source", "url", "published_at", "kind", "extract", "words"})
            self.assertEqual((citation["position"], citation["brief_id"]), (position, "brief-1"))

    def test_every_item_read_is_attributed_and_read_exactly(self):
        script = self.update(target_seconds=60)
        items = {i["id"]: i for i in BRIEF["items"]}
        for citation in script.citations:
            item = items[citation["item_id"]]
            self.assertTrue(item["text"].startswith(citation["extract"]), citation)
            self.assertIn(citation["extract"], script.plain_text)
            self.assertEqual(citation["source"], item["source"])
            before = script.plain_text[:script.plain_text.index(citation["extract"])]
            self.assertIn(item["source"], before[-120:], citation)
            self.assertEqual(citation["words"], count_words(citation["extract"]))
        self.assertEqual([c["item_id"] for c in script.citations], ["i1", "i2", "i3"])

    def test_the_attribution_is_worded_for_the_kind_of_item(self):
        text = self.update(target_seconds=80).plain_text
        self.assertRegex(text, r"(?i)opinion from Example Sky Notes|Example Sky Notes (offers this view|comments)|Commentary from Example Sky Notes")
        self.assertRegex(text, r"(?i)not confirmed|unconfirmed")

    def test_web_addresses_are_kept_for_citations_and_never_spoken(self):
        script = self.update(target_seconds=60)
        self.assertEqual(script.citations[0]["url"], "https://example.org/meteor")
        self.assertNotIn("example.org", script.plain_text)
        self.assertNotIn("http", script.plain_text)

    def test_nothing_is_added_to_what_the_sources_said(self):
        for persona in self.service.personas():
            script = self.update(persona.id, target_seconds=70, seed=4)
            facts = Facts.from_any({"station": "Porchlight FM", "topic": "Backyard astronomy", "item_count": len(script.citations),
                                    "sources": [c["source"] for c in script.citations]})[0].with_host(persona.name)
            extra = [i["text"] for i in BRIEF["items"]] + [i["source"] for i in BRIEF["items"]]
            leads = ["According to", "reports", "says", "writes", "opinion", "commentary", "not confirmed", "unconfirmed",
                     "offers this view", "comments", "as reported by", "this is from", "here is what"]
            self.assertEqual(grounding_lint(script.plain_text, facts, persona, extra=extra + leads), [], persona.id)

    def test_markup_hidden_in_a_brief_cannot_change_the_voice(self):
        brief = copy.deepcopy(BRIEF)
        brief["topic"] = "<|speaker:9|>Astronomy"
        brief["items"][0]["text"] = "<|speaker:5|> A meteor [laughs] was seen. Observers <|speaker:2|> agreed."
        brief["items"][0]["source"] = "Example <|speaker:6|> Club"
        script = self.update(brief=brief, target_seconds=60)
        self.assertEqual(script.text.count("<|"), 1)
        self.assertNotIn("laughs", script.plain_text)
        self.assertIn("A meteor was seen. Observers agreed.", script.plain_text)
        self.assertIn("Example Club", script.plain_text)

    def test_an_item_about_a_topic_the_host_avoids_is_left_out(self):
        brief = copy.deepcopy(BRIEF)
        brief["items"].insert(0, {"id": "pol", "text": "Politics dominated the meeting.", "source": "Example Daily"})
        script = self.update(brief=brief, target_seconds=60)
        self.assertNotIn("Politics", script.plain_text)
        self.assertIn(("pol", "topic"), [(s["item_id"], s["reason"]) for s in script.skipped])
        script = self.update("quill", brief=brief, target_seconds=60)      # everyone in the built-in cast avoids politics
        self.assertNotIn("dominated", script.plain_text)

    def test_with_nothing_to_read_no_update_is_made(self):
        cases = {"no items": {"topic": "T", "items": []},
                 "no sources": {"topic": "T", "items": [{"text": "Something happened."}]},
                 "only a link": {"topic": "T", "items": [{"text": "https://example.org/only-a-link", "source": "S"}]},
                 "too long to read": {"topic": "T", "items": [{"text": " ".join(["word"] * 80) + ".", "source": "S"}]},
                 "only a blocked topic": {"topic": "T", "items": [{"text": "Politics again.", "source": "S"}]}}
        for name, brief in cases.items():
            with self.subTest(case=name), self.assertRaises(NothingToRead) as caught:
                self.update(brief=brief)
            self.assertIn("Music keeps playing", str(caught.exception))
            self.assertIsInstance(caught.exception, HostsError)

    def test_the_length_asked_for_decides_how_much_is_read(self):
        short = self.update(target_seconds=20)
        medium = self.update(target_seconds=40)
        long = self.update(target_seconds=90)
        self.assertEqual(len(short.citations), 1)
        self.assertEqual(len(long.citations), 3)
        self.assertLessEqual(len(short.citations), len(medium.citations))
        self.assertLess(short.est_seconds, long.est_seconds)
        for script in (short, medium, long):
            self.assertTrue(script.citations)
        reasons = {s["reason"] for s in short.skipped}
        self.assertIn("time", reasons)
        self.assertEqual(sum(1 for s in short.skipped if s["reason"] == "time"), 2)
        self.assertEqual(len(long.skipped), 0)

    def test_extra_time_lets_an_item_run_on_to_its_next_sentences(self):
        short = self.update(target_seconds=22)
        long = self.update(target_seconds=90)
        self.assertEqual(short.citations[0]["extract"], "A bright meteor was reported over the Pacific Northwest on Tuesday night.")
        self.assertEqual(long.citations[0]["extract"], BRIEF["items"][0]["text"])

    def test_the_opening_and_closing_agree_with_what_was_read(self):
        one = self.update(brief={"topic": "Astronomy", "items": [BRIEF["items"][0]]}, target_seconds=60)
        self.assertEqual(len(one.citations), 1)
        self.assertNotRegex(one.plain_text, r"(?i)\bthose were\b|\bhere are\b|\b(two|three) items\b")
        three = self.update(target_seconds=90)
        self.assertEqual(len(three.citations), 3)
        self.assertNotRegex(three.plain_text, r"(?i)\bone item\b")
        self.assertIn("topic", three.facts_used)
        self.assertTrue(set(three.facts_used) <= {"station", "topic", "item_count", "sources", "host", "daypart", "local_time"})

    def test_updates_are_deterministic_and_the_memory_only_moves_when_it_should(self):
        a = self.update(target_seconds=60, seed=11)
        b = new_service().compose_update("marlowe", copy.deepcopy(BRIEF), {"station": "Porchlight FM"}, seed=11, target_seconds=60, dry_run=True)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(self.service.memory.size(), 0)
        self.update(target_seconds=60, seed=11, dry_run=False)
        self.assertEqual(self.service.memory.size(), 2)       # the opening and the closing line were used
        self.assertEqual(self.service.status()["composed"]["update"], 1)

    def test_every_host_can_read_an_update_and_each_stays_in_its_own_voice(self):
        for persona in self.service.personas():
            script = self.update(persona.id, target_seconds=60)
            self.assertTrue(script.text.startswith(f"<|speaker:{persona.speaker}|>"))
            self.assertEqual(audit_speech_text(script.text, persona.speaker, persona.allowed_tags), [])
            self.assertEqual(len(script.citations), 3, persona.id)

    def test_the_target_must_be_a_number_of_seconds(self):
        for bad in (True, "30", float("nan")):
            with self.assertRaisesRegex(HostsError, "seconds"):
                self.update(target_seconds=bad)
        script = self.update(target_seconds=2000)
        self.assertEqual(script.target_seconds, 180.0)
        self.assertTrue(any("can run from" in w for w in script.warnings))

    def test_a_reported_length_miss_is_honest(self):
        script = self.update(target_seconds=170)
        self.assertLess(script.est_seconds, 100)
        self.assertTrue(any("no more items to read" in w for w in script.warnings), script.warnings)

    def test_updates_make_no_network_call(self):
        with no_network():
            self.update(target_seconds=60)

    def test_hostile_briefs_give_a_safe_update_or_a_plain_refusal_and_nothing_else(self):
        rng = random.Random(20261106)
        words = ["Sen.", "Nov.", "5", "Dr.", "said", "the", "bill", "passed", "[not]", "[laughs]", "<|speaker:3|>", "5 < 7", "nasa.gov/x",
                 "example.com", "press@x.org", "Texas.", "yes.", "No.", "(see map)", "\"quote\"", "...", "!", "?", "Mr.", "J.", "K.", "Fig.", "3",
                 "Oct.", "council", "budget", "Wed.", "Corp.", "<b>", "</b>", "{x}", "a|b", "Sat.", "https://example.org/a", "dot", "com", "It",
                 "was", "good."]
        sources = ["Example Times", "Example Gazette", "https://example.org", "user@example.org", "Space.com", "[laughs]", "<|speaker:2|>", "",
                   "Politics Weekly", "Example Daily"]
        kinds = [None, "reported", "commentary", "announcement", "uncertain", "unverified", "gossip", 5]
        hosts = [p.id for p in self.service.personas()]
        made = 0
        for _ in range(400):
            items = [{"text": " ".join(rng.choice(words) for _ in range(rng.randint(0, 40))), "source": rng.choice(sources), "kind": rng.choice(kinds)}
                     for _ in range(rng.randint(0, 6))]
            brief = {"topic": rng.choice(["", "News", "Local politics", "[laughs]", "x" * 200]), "items": items}
            host = rng.choice(hosts)
            try:
                script = self.service.compose_update(host, brief, {"station": "Radio"}, seed=rng.randint(0, 10 ** 9),
                                                     target_seconds=rng.choice([None, 8, 20, 60, 120, 180]), dry_run=True)
            except HostsError:
                continue                                    # a plain refusal (NothingToRead is one)
            made += 1
            self.assertEqual(script.text.count("<|"), 1)
            self.assertEqual(audit_speech_text(script.text, script.speaker, self.service.persona(host).allowed_tags), [])
            self.assertLessEqual(len(script.text), MAX_SCRIPT_CHARS)
            for citation in script.citations:
                self.assertIn(citation["extract"], script.plain_text)
                self.assertFalse(set("<>[]{}|") & set(citation["extract"]), citation)
        self.assertGreater(made, 30)

    # ---- titles, months and other full stops that do not end a sentence -------------------------------------
    def test_titles_and_months_in_a_brief_never_leave_a_fragment_as_the_item(self):
        texts = ["Sen. Warren said the bill would pass the chamber this week. Critics disagreed.",
                 "The council met on Nov. 5 to vote on the budget. It passed by a wide margin.",
                 "Oct. 31 is Halloween. Candy is cheap this year."]
        brief = {"topic": "City news", "items": [{"text": t, "source": f"Example {n}"} for t, n in zip(texts, ("Times", "Gazette", "Shopper"))]}
        for target in (20, 40, 60, 90):
            for persona in ("marlowe", "corin", "nova"):
                script = self.update(persona, brief=brief, facts={"station": "Porchlight FM"}, seed=4, target_seconds=target)
                self.assertTrue(script.citations)
                for citation in script.citations:
                    text = texts[int(citation["item_id"].split("-")[-1]) - 1]
                    self.assertTrue(text.startswith(citation["extract"]), citation)
                    self.assertRegex(citation["extract"], r"[a-z0-9]\.$")                      # a whole sentence, not "Sen." or "Oct."
                    self.assertNotRegex(citation["extract"], r"(?:^|\s)(?:Sen|Nov|Oct|Gov)\.$")
                    self.assertGreaterEqual(citation["words"], 4)
        script = self.update("marlowe", brief=brief, facts={"station": "Porchlight FM"}, seed=4, target_seconds=60)
        self.assertEqual([c["extract"] for c in script.citations],
                         ["Sen. Warren said the bill would pass the chamber this week. Critics disagreed.",
                          "The council met on Nov. 5 to vote on the budget. It passed by a wide margin.", "Oct. 31 is Halloween. Candy is cheap this year."])

    def test_an_item_that_is_only_an_abbreviation_is_left_out_with_a_reason(self):
        brief = {"topic": "T", "items": [{"id": "a", "text": "Sen.", "source": "Example Times"},
                                         {"id": "b", "text": "Fine words arrive here today.", "source": "Example Gazette"}]}
        script = self.update(brief=brief, target_seconds=30)
        self.assertEqual([c["item_id"] for c in script.citations], ["b"])
        self.assertIn(("a", "fragment"), [(x["item_id"], x["reason"]) for x in script.skipped])

    # ---- items that do not end like a sentence ----------------------------------------------------------------
    UNPUNCTUATED = {"headline": "Council approves new budget for roads and parks",
                    "quote": 'The mayor called it "a fair deal"', "ellipsis": "The council voted on the budget and then...",
                    "bracket": "The new park opens soon (see the map)", "colon": "The council said:"}

    def test_every_host_reads_items_that_do_not_end_like_a_sentence_and_keeps_them_apart_from_its_own_words(self):
        for persona in self.service.personas():
            for name, text in self.UNPUNCTUATED.items():
                with self.subTest(host=persona.id, item=name):
                    brief = {"topic": "City news", "items": [{"text": text, "source": "Example Gazette"}]}
                    script = self.update(persona.id, brief=brief, facts={"station": "Porchlight FM"}, seed=4)
                    self.assertEqual(script.citations[0]["extract"], text)             # the citation stays exactly as found
                    start = script.plain_text.index(text)
                    chunk = script.plain_text[start:start + len(text) + 1]
                    self.assertRegex(chunk, r"[.!?\u2026][\"'\u201d\u2019)\]]*\s?$", chunk)       # a full stop of the host's own ends it
                    self.assertFalse([w for w in script.warnings if "checked" in w or "voice codes" in w], script.warnings)

    def test_two_items_without_full_stops_do_not_run_into_each_other(self):
        brief = {"topic": "City news", "items": [{"text": "Council approves new budget", "source": "Example Gazette"},
                                                 {"text": "Library hours change on Friday", "source": "Example Times"}]}
        for persona in self.service.personas():
            script = self.update(persona.id, brief=brief, target_seconds=40)
            self.assertEqual(len(script.citations), 2, persona.id)
            self.assertIn("Council approves new budget.", script.plain_text)
            self.assertIn("Library hours change on Friday.", script.plain_text)

    # ---- addresses --------------------------------------------------------------------------------------------
    ADDRESSES = ("nasa.gov/missions", "city.gov", "stanford.edu/papers/42", "astro.garden", "bit.ly/abc", "example.xyz", "radio.ai",
                 "press@nasa.gov", "EXAMPLE.ORG", "example dot garden")

    def test_no_kind_of_address_in_an_item_is_spoken_or_cited_as_the_extract(self):
        for address in self.ADDRESSES:
            with self.subTest(address=address):
                brief = {"topic": "Space", "items": [{"id": "a", "source": "Example News",
                         "text": f"NASA says the launch is on schedule. Details are at {address} for anyone curious."}]}
                script = self.update("marlowe", brief=brief, facts={"station": "Radio"}, seed=1, target_seconds=60)
                self.assertEqual(script.citations[0]["extract"], "NASA says the launch is on schedule.")
                self.assertNotIn(address.lower(), script.plain_text.lower())
                self.assertNotIn("Details are at", script.plain_text)

    def test_an_item_whose_first_sentence_holds_an_address_is_left_out(self):
        for address in self.ADDRESSES:
            with self.subTest(address=address):
                brief = {"topic": "Space", "items": [
                    {"id": "a", "text": f"Read all about it at {address} today. More later.", "source": "Example News"},
                    {"id": "b", "text": "The launch is on schedule today.", "source": "Example Times"}]}
                script = self.update(brief=brief, target_seconds=40)
                self.assertEqual([c["item_id"] for c in script.citations], ["b"])
                self.assertIn(("a", "link"), [(x["item_id"], x["reason"]) for x in script.skipped])

    def test_a_source_that_is_only_an_address_is_never_spoken(self):
        brief = {"topic": "T", "items": [{"id": "a", "text": "One thing happened today.", "source": "https://example.org/feed"},
                                         {"id": "b", "text": "Another thing happened today.", "source": "Example Times"}]}
        script = self.update(brief=brief, target_seconds=40)
        self.assertEqual([c["item_id"] for c in script.citations], ["b"])
        self.assertNotIn("example.org", script.plain_text)
        self.assertIn(("a", "source_link"), [(x["item_id"], x["reason"]) for x in script.skipped])
        with self.assertRaises(NothingToRead):
            self.update(brief={"topic": "T", "items": [brief["items"][0]]})

    # ---- what the sources said, exactly ---------------------------------------------------------------------
    def test_brackets_and_symbols_in_an_item_never_change_what_is_said_without_a_trace(self):
        brief = {"topic": "T", "items": [
            {"id": "a", "text": "He said the policy would [not] help. Critics agreed.", "source": "Example Daily"},
            {"id": "b", "text": "Profits rose 3% (down from 5% last year). Analysts agreed.", "source": "Example Money"},
            {"id": "c", "text": "The vote was 5 < 7 and the margin > 2. Details follow.", "source": "Example Votes"}]}
        script = self.update(brief=brief, target_seconds=60)
        extracts = {c["item_id"]: c["extract"] for c in script.citations}
        self.assertEqual(extracts["a"], "He said the policy would not help. Critics agreed.")     # the words stay, the brackets go
        self.assertEqual(extracts["b"], "Profits rose 3% (down from 5% last year). Analysts agreed.")
        self.assertNotIn("c", extracts)                                                                # not read, not altered
        self.assertIn(("c", "symbols"), [(x["item_id"], x["reason"]) for x in script.skipped])
        self.assertTrue(any("brackets" in w for w in script.warnings), script.warnings)
        self.assertNotIn("5 7", script.plain_text)

    def test_the_topic_and_the_source_names_are_held_to_what_the_host_avoids(self):
        brief = {"topic": "Local politics", "items": [
            {"id": "a", "text": "The garden club meets today.", "source": "Politics Weekly"},
            {"id": "b", "text": "The garden club has new seeds.", "source": "Example Times"}]}
        for seed in range(15):
            script = self.update("marlowe", brief=brief, facts={"station": "Radio"}, seed=seed, target_seconds=40)
            self.assertNotRegex(script.plain_text, r"(?i)politic")
            self.assertEqual([c["item_id"] for c in script.citations], ["b"])
            self.assertTrue(any("topic was left out" in w for w in script.warnings))
            self.assertIn(("a", "topic"), [(x["item_id"], x["reason"]) for x in script.skipped])
            self.assertNotIn("topic", script.facts_used)
        script = self.update("marlowe", brief=dict(brief, topic="Garden news"), facts={"station": "Radio"}, seed=1, target_seconds=40)
        self.assertIn("topic", script.facts_used)

    # ---- how sure the host sounds -----------------------------------------------------------------------------
    def test_an_unrecognised_kind_is_introduced_as_not_confirmed_and_an_announcement_as_an_announcement(self):
        for kind in ("gossip", "unverified", "uncertain", 7):
            brief = {"topic": "T", "items": [{"id": "a", "text": "Something may have happened today.", "source": "Example Daily", "kind": kind}]}
            for seed in range(8):
                script = self.update("marlowe", brief=brief, seed=seed, target_seconds=30)
                self.assertRegex(script.plain_text, r"(?i)not confirmed|unconfirmed", (kind, script.plain_text))
                self.assertEqual(script.citations[0]["kind"], "uncertain")
        brief = {"topic": "T", "items": [{"id": "a", "text": "The new app is out today.", "source": "Example Software", "kind": "announcement"}]}
        for seed in range(8):
            script = self.update("marlowe", brief=brief, seed=seed, target_seconds=30)
            self.assertRegex(script.plain_text, r"(?i)announc", script.plain_text)
            self.assertEqual(script.citations[0]["kind"], "announcement")

    # ---- the limits of the speech step ---------------------------------------------------------------------
    @staticmethod
    def long_brief(items=20, words=40):
        pool = ("neighbourhood construction environmental international administration conversation organisation development "
                "responsibility transportation community opportunity infrastructure investigation communication performance "
                "information celebration programme education").split()

        def sentence(i):
            return " ".join(pool[(i * 7 + j * 3) % len(pool)] for j in range(words)).capitalize() + "."

        return {"topic": "Village life", "items": [{"id": f"i{n}", "text": f"{sentence(n)} {sentence(n + 1)}",
                                                    "source": f"Example Source {chr(97 + n % 20)}"} for n in range(items)]}

    def test_an_update_never_goes_over_what_the_speech_step_accepts(self):
        # the speech step refuses a script of more than 3,000 characters; hosts accept updates of up to 180 seconds
        self.assertEqual(MAX_SCRIPT_CHARS, 3000)
        for persona in ("corin", "marlowe", "dex", "rae"):
            script = self.update(persona, brief=self.long_brief(), facts={"station": "Radio"}, seed=2, target_seconds=180)
            self.assertLessEqual(len(script.text), MAX_SCRIPT_CHARS, persona)
            self.assertGreater(len(script.citations), 3)
            self.assertEqual(audit_speech_text(script.text, script.speaker, []), [])
            self.assertTrue(any("3,000 characters" in w for w in script.warnings), script.warnings)
            self.assertIn("time", {x["reason"] for x in script.skipped})

    def test_when_the_opening_and_closing_run_long_fewer_items_are_read_so_the_whole_fits(self):
        data = voice_data("anchor")
        greetings = ("Welcome back to the studio", "Hello again to everyone listening", "Good to have you with us", "Thank you for staying with us")
        endings = ("through the music this hour", "for another short break", "as the afternoon goes on")
        intro_tail = (" I would like to read out a few items that were sent to us, so please settle in and listen closely, because "
                      "everything that follows is read just as it was written.")
        outro_tail = (" That brings us to the end of the items we had for you today, and we hope you found them useful, so please keep "
                      "listening for more music.")
        data["segments"]["update_intro"] = [f"{g} {e}.{intro_tail}" for g in greetings for e in endings]
        data["segments"]["update_outro"] = [f"{g} {e}.{outro_tail}" for g in greetings for e in endings]
        composer = TemplateComposer(library_with("anchor", data))
        marlowe = load_builtin_personas(composer.library.voices)["marlowe"]
        pool = "river garden window summer lantern orchard bridge morning harvest quiet village market station teacher neighbour winter".split()
        items = [{"id": f"i{n}", "source": f"Example Source {chr(97 + n)}",
                  "text": " ".join(pool[(n * 5 + j) % len(pool)] for j in range(30)).capitalize() + "."} for n in range(3)]
        script = compose_update(composer, marlowe, {"topic": "Village life", "items": items}, {"station": "Radio"}, seed=3,
                                target_seconds=60, remember=False)
        self.assertEqual(len(script.citations), 2)                  # three would have run to about 76 seconds against 60 asked for
        self.assertLessEqual(script.est_seconds, 60 * 1.25)
        self.assertEqual([x["reason"] for x in script.skipped], ["time"])


class ResearchBriefTests(unittest.TestCase):
    """The research lane's own Brief (items carry *_speech fields and no 'text' or 'source') becomes an update."""

    def setUp(self):
        self.service = new_service()
        self.items = [
            research_item(1, "Bright meteor over the coast", "A bright meteor was seen over the coast on Tuesday night. Observers called it unusually slow.",
                          "Example Astronomy Club", url="https://example.org/meteor"),
            research_item(2, "Saturn watch", "", "Example Sky Notes", kind="unverified"),
            research_item(3, "Why telescopes pay off", "Telescopes are worth the money. Buy one soon.", "Example Optics", kind="commentary"),
            research_item(4, "New app released", "The app is now available for everyone. It is free.", "Example Software", kind="announcement"),
        ]
        self.brief = research_brief(self.items)

    def read(self, persona="marlowe", brief=None, **kw):
        kw.setdefault("seed", 3)
        kw.setdefault("dry_run", True)
        return self.service.compose_update(persona, copy.deepcopy(brief or self.brief), kw.pop("facts", {"station": "Porchlight FM"}), **kw)

    def test_the_fixture_is_a_valid_research_brief(self):
        from myradio.research.schema import check_brief
        self.assertEqual(check_brief(self.brief), [])
        self.assertEqual(check_brief(research_brief([], outcome="nothing_new")), [])

    def test_every_item_of_a_real_shaped_brief_is_read_with_its_source_and_kind(self):
        script = self.read(target_seconds=80)
        self.assertEqual([(c["source"], c["kind"]) for c in script.citations],
                         [("Example Astronomy Club", "reported"), ("Example Sky Notes", "uncertain"), ("Example Optics", "commentary"),
                          ("Example Software", "announcement")])
        self.assertEqual([c["item_id"] for c in script.citations], [i["id"] for i in self.items])
        self.assertEqual(script.citations[0]["url"], "https://example.org/meteor")
        self.assertEqual(script.citations[0]["published_at"], self.items[0]["published_at"])
        self.assertEqual(script.citations[0]["brief_id"], "a" * 32)
        self.assertEqual(script.skipped, ())
        self.assertNotIn("example.org", script.plain_text)
        for citation in script.citations:
            self.assertIn(citation["source"], script.plain_text)
            self.assertIn(citation["extract"], script.plain_text)

    def test_what_is_not_confirmed_is_never_introduced_as_reported(self):
        for persona in self.service.personas():
            for seed in range(6):
                script = self.read(persona.id, seed=seed, target_seconds=80)
                text, previous = script.plain_text, script.citations[0]["extract"]
                lead = text[text.index(previous) + len(previous):text.index("Saturn watch")]         # the words between the two items
                self.assertIn("Example Sky Notes", lead)
                self.assertRegex(lead, r"(?i)not confirmed|unconfirmed", (persona.id, lead))

    def test_the_headline_is_read_when_the_source_gave_only_a_title_and_the_speech_fields_are_the_ones_used(self):
        script = self.read(target_seconds=80)
        self.assertEqual(script.citations[1]["extract"], "Saturn watch")
        self.assertIn("Saturn watch.", script.plain_text)            # the host ends it, since a title has no full stop
        items = copy.deepcopy(self.items[:1])
        items[0]["extract"] = "RAW <b>markup</b> [laughs] that must not be used."
        items[0]["headline"] = "RAW HEADLINE"
        script = self.read(brief=research_brief(items), target_seconds=40)
        self.assertNotIn("RAW", script.plain_text)
        self.assertEqual(script.citations[0]["extract"], items[0]["extract_speech"])

    def test_an_extract_the_research_lane_cut_off_is_read_only_as_far_as_its_last_whole_sentence(self):
        cut = research_item(1, "Headline of the day", "The first thing happened on Monday. The second thing was still being written when the",
                            "Example Gazette", truncated=True)
        script = self.read(brief=research_brief([cut]), target_seconds=40)
        self.assertEqual(script.citations[0]["extract"], "The first thing happened on Monday.")
        only_a_stub = research_item(1, "Headline of the day", "The second thing was still being written when the", "Example Gazette", truncated=True)
        script = self.read(brief=research_brief([only_a_stub]), target_seconds=40)
        self.assertEqual(script.citations[0]["extract"], "Headline of the day")        # no whole sentence: the title is used
        whole = research_item(1, "Headline of the day", "The thing is finished now.", "Example Gazette", truncated=True)
        self.assertEqual(self.read(brief=research_brief([whole])).citations[0]["extract"], "The thing is finished now.")

    def test_the_researchers_name_is_the_topic_unless_one_is_given(self):
        text = self.read(target_seconds=60).plain_text
        self.assertIn("Sky watcher", text)
        given = self.read(brief=dict(self.brief, topic="Backyard astronomy"), target_seconds=60).plain_text
        self.assertIn("Backyard astronomy", given)
        self.assertNotIn("Sky watcher", given)
        named = self.read(facts={"station": "Porchlight FM", "topic": "Night skies"}, target_seconds=60).plain_text
        self.assertIn("Night skies", named)

    def test_a_run_that_found_nothing_has_nothing_to_read(self):
        for outcome in ("nothing_new", "error"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(NothingToRead, "Music keeps playing"):
                self.read(brief=research_brief([], outcome=outcome))
        # even if someone hands over items with an outcome that says there are none
        brief = dict(self.brief, outcome="error")
        with self.assertRaises(NothingToRead):
            self.read(brief=brief)

    def test_every_host_can_read_a_research_brief(self):
        for persona in self.service.personas():
            script = self.read(persona.id, target_seconds=70)
            self.assertEqual(len(script.citations), 4, persona.id)
            self.assertTrue(script.text.startswith(f"<|speaker:{persona.speaker}|>"))
            self.assertEqual(audit_speech_text(script.text, persona.speaker, persona.allowed_tags), [])

    def test_the_plain_shape_is_returned_untouched_and_unknown_research_kinds_are_cautious(self):
        self.assertIs(brief_from_research(BRIEF), BRIEF)
        with_outcome = dict(BRIEF, outcome="done")                      # a word of the caller's own is not the research lane's
        self.assertIs(brief_from_research(with_outcome), with_outcome)
        self.assertEqual(len(parse_brief(with_outcome).items), 3)
        mapped = brief_from_research(self.brief)
        self.assertEqual([i["kind"] for i in mapped["items"]], ["reported", "uncertain", "commentary", "announcement"])
        odd = research_item(1, "Odd one", "Something odd was seen today.", "Example Daily", kind="rumour")
        self.assertEqual(brief_from_research(research_brief([odd]))["items"][0]["kind"], "uncertain")
        parsed = parse_brief(self.brief)
        self.assertEqual((parsed.id, parsed.topic, len(parsed.items), parsed.dropped), ("a" * 32, "Sky watcher", 4, ()))
        self.assertEqual(parsed.generated_at, self.brief["created_at"])


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.service = new_service()

    def make(self, persona, fmt, facts=FULL, **kw):
        kw.setdefault("seed", 3)
        kw.setdefault("dry_run", True)
        return self.service.compose(persona, None, facts, format=fmt, **kw)

    def test_three_formats_are_listed_as_fictional_with_a_plain_notice(self):
        listed = {f["id"]: f for f in self.service.formats()}
        self.assertEqual(set(listed), {"word-salad-poem", "station-almanac", "airwave-forecast"})
        for fmt in listed.values():
            self.assertTrue(fmt["fictional"])
            self.assertTrue(fmt["disclosure"].endswith("."))
            self.assertEqual(fmt["disclosure"].count("."), 1)            # one line
            self.assertRegex(fmt["disclosure"], r"(?i)made[- ]up|make-believe|playful|made up")
            self.assertTrue(fmt["label"] and fmt["summary"])
            self.assertTrue(fmt["min_seconds"] < fmt["default_seconds"] <= fmt["max_seconds"])
        json.dumps(listed)

    def test_every_piece_opens_with_its_disclosure_and_is_flagged_fictional(self):
        for persona in self.service.personas():
            for fid, fmt in FORMATS.items():
                script = self.make(persona.id, fid)
                self.assertTrue(script.fictional)
                self.assertEqual(script.format, fid)
                self.assertEqual(script.source, "format")
                self.assertEqual((script.role, script.library_kind), ("host_break", "speech"))
                self.assertTrue(script.plain_text.startswith(fmt.disclosure), f"{persona.id}/{fid}")
                self.assertEqual(script.text.count("<|"), 1)
                self.assertEqual(script.warnings, ())

    def test_pieces_are_grounded_for_every_host_seed_and_length(self):
        from myradio.hosts.formats import VOCABULARY
        for persona in self.service.personas():
            for fid in FORMATS:
                for seed in range(6):
                    for target in (None, 14, 30, 44):
                        script = self.make(persona.id, fid, FULL, seed=seed, target_seconds=target)
                        cleaned = Facts.from_any(FULL)[0].with_host(persona.name)
                        self.assertEqual(grounding_lint(script.plain_text, cleaned, persona, extra=VOCABULARY), [])
                        self.assertNotIn("could not be checked", " ".join(script.warnings))

    def test_formats_are_deterministic_and_vary_with_the_seed(self):
        for fid in FORMATS:
            self.assertEqual(self.make("nova", fid, seed=9).to_dict(), new_service().compose("nova", None, FULL, format=fid, seed=9,
                                                                                               dry_run=True).to_dict())
            self.assertGreaterEqual(len({self.make("nova", fid, seed=s).plain_text for s in range(20)}), 10, fid)

    def test_the_poem_uses_no_facts_and_the_almanac_uses_only_the_facts_given(self):
        poem = self.make("nova", "word-salad-poem", FULL, seed=2)
        for fact in ("Porchlight FM", "Low Orbit Lullaby", "Marble Hours", "Tuesday"):
            self.assertNotIn(fact, poem.plain_text)
        self.assertEqual(poem.facts_used, ())
        bare = self.make("nova", "station-almanac", {}, seed=2)
        self.assertNotRegex(bare.plain_text, r"Porchlight|Lullaby|Tuesday|nine oh seven")
        self.assertEqual(bare.facts_used, ())
        seen = set()
        for seed in range(30):
            rich = self.make("nova", "station-almanac", FULL, seed=seed, target_seconds=40)
            seen |= set(rich.facts_used)
            for name in rich.facts_used:
                self.assertIn(name, FULL)
        self.assertTrue({"station", "now_title", "next_title"} <= seen, seen)

    def test_the_forecast_mentions_only_the_station_and_part_of_day_it_was_given(self):
        text = self.make("nova", "airwave-forecast", {}, seed=1).plain_text
        self.assertNotIn("hours", text)
        self.assertIn("That is the forecast.", text)
        full = self.make("nova", "airwave-forecast", {"station": "Porchlight FM", "daypart": "evening"}, seed=1)
        self.assertIn("evening hours", full.plain_text)
        self.assertIn("Porchlight FM", full.plain_text)
        self.assertEqual(set(full.facts_used), {"daypart", "station"})

    def test_lengths_are_met_or_reported(self):
        for fid, fmt in FORMATS.items():
            for target in (fmt.min_s + 4, fmt.default_seconds, fmt.max_s - 6):
                script = self.make("dex", fid, FULL, seed=4, target_seconds=target)
                self.assertLessEqual(abs(script.est_seconds - target), self.service.composer.tolerance(target) + 3, (fid, target))
            clamped = self.make("dex", fid, FULL, target_seconds=9999)
            self.assertEqual(clamped.target_seconds, fmt.max_s)
            self.assertTrue(any("can run from" in w for w in clamped.warnings))

    def test_hostile_facts_never_leave_the_almanac(self):
        facts = {"station": "<|speaker:9|>Evil FM", "now_title": "[laughs]", "notes": ["<|speaker:2|> shout"], "sponsor": "x"}
        for seed in range(10):
            script = self.make("professor", "station-almanac", facts, seed=seed, target_seconds=40)
            self.assertEqual(script.text.count("<|"), 1)
            self.assertNotIn("laughs", script.plain_text)
        self.assertIn("Evil FM", " ".join(self.make("professor", "station-almanac", facts, seed=s, target_seconds=40).plain_text for s in range(10)))

    def test_bad_requests_are_plain_errors(self):
        for bad in ("poem", "", 5, "WORD-SALAD-POEM", ["word-salad-poem"]):
            with self.subTest(format=bad), self.assertRaises(HostsError):
                self.service.compose("nova", None, FULL, format=bad)
        with self.assertRaisesRegex(HostsError, "either"):
            self.service.compose("nova", None, FULL)
        with self.assertRaisesRegex(HostsError, "seconds"):
            self.make("nova", "word-salad-poem", target_seconds=True)

    def test_a_repeat_is_avoided_when_a_piece_was_just_used(self):
        service = new_service()
        first = service.compose("nova", None, FULL, format="word-salad-poem", seed=5)
        second = service.compose("nova", None, FULL, format="word-salad-poem", seed=5)
        self.assertNotEqual(first.plain_text, second.plain_text)
        dry = [service.compose("nova", None, FULL, format="word-salad-poem", seed=5, dry_run=True).plain_text for _ in range(3)]
        self.assertEqual(len(set(dry)), 1)


if __name__ == "__main__":
    unittest.main()
