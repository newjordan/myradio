"""grounding_lint: numbers, names and web addresses that the facts do not support."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hosts_support import library  # noqa: E402

from myradio.hosts import grounding_lint  # noqa: E402
from myradio.hosts.facts import Facts  # noqa: E402
from myradio.hosts.lint import LINT_LIMIT, Grounding, find_ungrounded, grounding_texts, has_address, number_words_in, numbers_in  # noqa: E402
from myradio.hosts.personas import load_builtin_personas  # noqa: E402


def kinds(text, facts=None, persona=None):
    """Sorted (kind, key) pairs the lint finds in ``text``."""
    cleaned = Facts.from_any(facts or {})[0]
    texts, numbers = grounding_texts(cleaned, persona)
    return sorted((f.kind, f.key.lower()) for f in find_ungrounded(text, Grounding(texts, numbers)))


class NumberTests(unittest.TestCase):
    def test_numbers_written_with_digits(self):
        table = [("track 7", ["7"]), ("1,000 songs", ["1000"]), ("12,34", ["12", "34"]), ("1,0000", ["1", "0"]), ("3.5 stars", ["3", "5"]), ("at 9:07", ["9", "7"]),
                 ("2026-11-05", ["2026", "11", "5"]), ("the 21st", ["21"]), ("$5 or 50%", ["5", "50"]),
                 ("007", ["7"]), ("٣ in Arabic digits", ["3"])]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(sorted(key for key, _ in numbers_in(text)), sorted(expected))

    def test_numbers_written_in_words(self):
        table = [("twenty-three", ["23"]), ("nineteen ninety-eight", ["19", "98"]), ("two thousand seven", ["2007"]),
                 ("one hundred and five", ["105"]), ("a hundred", ["100"]), ("the nineties", ["90"]),
                 ("twenty twenty", ["20", "20"]), ("three hundred sixty five", ["365"]), ("twenty-first", ["21"]),
                 ("third place", ["3"]), ("zero", ["0"]), ("a dozen", ["12"]), ("one million", ["1000000"]),
                 ("number one", ["1"]), ("top one", ["1"]), ("one hour", ["1"]), ("twenty one", ["21"]),
                 ("seven, eight", ["7", "8"]), ("eleven", ["11"]), ("ninety nine", ["99"])]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(sorted(key for key, _ in number_words_in(text)), sorted(expected))

    def test_ordinary_words_are_not_numbers(self):
        for text in ("one of my favourites", "this one is for you", "No one knows", "first up", "a second chance", "Just a second.",
                     "once more", "twice as nice", "half past", "a quarter to", "a couple of songs", "several friends",
                     "hundreds of listeners", "dozens of songs", "the second half", "oneself", "often", "stone", "tenor",
                     "bone", "plain text", "wonder", "fortunate"):
            with self.subTest(text=text):
                self.assertEqual(number_words_in(text), [], text)

    def test_hostile_number_input_neither_crashes_nor_stalls(self):
        started = time.perf_counter()
        self.assertEqual(len(numbers_in("9" * 20000)), 1)
        self.assertEqual(len(numbers_in("seven " * 20000)), 20000)
        self.assertTrue(numbers_in("one hundred " * 5000))
        self.assertLess(time.perf_counter() - started, 3.0)


class FindingTests(unittest.TestCase):
    def test_grounded_text_is_clean_and_invented_text_is_not(self):
        facts = {"now_title": "Low Orbit Lullaby", "now_artist": "The Gentle Machines", "station": "Porchlight FM"}
        self.assertEqual(kinds("That was Low Orbit Lullaby by The Gentle Machines on Porchlight FM.", facts), [])
        self.assertEqual(kinds("That was Low Orbit Lullaby.", {}), [("name", "low|orbit|lullaby")])
        self.assertEqual(kinds("Song of the year, three weeks running.", {}), [("number", "3")])

    def test_dates(self):
        self.assertEqual(kinds("On November 5th, 2026 we play.", {}),
                         [("name", "november"), ("number", "2026"), ("number", "5")])
        self.assertEqual(kinds("On November 5th, 2026 we play.", {"notes": ["November 5th, 2026"]}), [])
        self.assertIn(("number", "2026"), kinds("Back in 2026.", {}))
        self.assertEqual(kinds("2026-11-05", {"notes": ["2026-11-05"]}), [])
        self.assertTrue(kinds("It is Tuesday.", {}))
        self.assertEqual(kinds("It is Tuesday.", {"weekday": 2}), [])
        self.assertEqual(kinds("Not in December.", {}), [("name", "december")])

    def test_ordinals(self):
        self.assertEqual(kinds("the 3rd track", {}), [("number", "3")])
        self.assertEqual(kinds("the third track", {}), [("number", "3")])
        self.assertEqual(kinds("the twenty-first century", {}), [("number", "21")])
        self.assertEqual(kinds("the 3rd track", {"notes": ["Track 3"]}), [])
        self.assertEqual(kinds("the third track", {"notes": ["Track 3"]}), [])
        self.assertEqual(kinds("first up, then second thoughts", {}), [])

    def test_no_dot_one(self):
        self.assertEqual(kinds("The No. 1 song of the year", {}), [("number", "1")])
        self.assertEqual(kinds("That was No. 1 Crush", {"now_title": "No. 1 Crush"}), [])
        self.assertEqual(kinds("The number one song", {}), [("number", "1")])
        self.assertEqual(kinds("No. 5 is next", {"next_title": "No. 5"}), [])
        self.assertEqual(kinds("No, thanks. No one minds.", {}), [])

    def test_hyphenated_names(self):
        self.assertEqual(kinds("Jean-Luc Pickard and Anne-Marie.", {"notes": ["Anne Marie and Jean-Luc"]}),
                         [("name", "pickard")])
        self.assertEqual(kinds("Anne-Marie sings.", {"now_artist": "Anne-Marie Blue"}), [])
        self.assertEqual(kinds("Anne-Marie sings.", {"now_artist": "Anne Blue"}), [("name", "anne-marie")])
        self.assertEqual(kinds("A late-night set, a well-known idea.", {}), [])
        self.assertEqual(kinds("Mid-Century Modern", {}), [("name", "mid-century|modern")])

    def test_years_may_be_spoken_either_way(self):
        facts = {"sponsor_copy": "Fixing bikes since 1998."}
        self.assertEqual(kinds("Since nineteen ninety-eight.", facts), [])
        self.assertEqual(kinds("Since 1998.", facts), [])
        self.assertEqual(kinds("Since nineteen ninety-nine.", facts), [("number", "99")])
        self.assertEqual(kinds("two thousand seven", {"now_title": "Class of 2007"}), [])

    def test_the_time_may_be_spoken_from_the_local_time_fact(self):
        facts = {"local_time": "21:07"}
        for ok in ("nine oh seven", "It is 9:07", "twenty-one oh seven", "21:07", "at seven past nine"):
            with self.subTest(ok=ok):
                self.assertEqual(kinds(ok, facts), [], ok)
        self.assertEqual(kinds("nine oh eight", facts), [("number", "8")])
        self.assertEqual(kinds("a quarter to ten", {"local_time": "21:45"}), [])
        self.assertEqual(kinds("a quarter to eleven", {"local_time": "21:45"}), [("number", "11")])

    def test_names_sentence_starts_and_ordinary_capitals(self):
        self.assertEqual(kinds("Welcome back. Good evening, friends. Here's more. Let's go. I'm here and I'll stay.", {}), [])
        self.assertEqual(kinds("DJ Nova on FM and AM.", {"host": "Nova"}), [])
        self.assertEqual(kinds("UFO sightings.", {}), [("name", "ufo")])
        self.assertEqual(kinds("Marlowe here.", {"host": "Nova"}), [("name", "marlowe")])
        self.assertEqual(kinds("Say hello to Zorblax Prime.", {}), [("name", "zorblax|prime")])
        self.assertEqual(kinds("Nova's choice.", {"host": "Nova"}), [])
        self.assertEqual(kinds('He said, "Hello there."', {}), [])
        self.assertEqual(kinds("Café Müller plays.", {"now_title": "Cafe Muller"}), [])
        self.assertEqual(kinds("Mr. Brightside is next.", {"next_title": "Mr. Brightside"}), [])
        self.assertEqual(kinds("Mr. Brightside is next.", {}), [("name", "brightside")])
        self.assertEqual(kinds("Corner Bike Co. Please call us.", {"sponsor": "Corner Bike Co."}), [])
        self.assertEqual(kinds("Nine oh seven at night.", {"local_time": "21:07"}), [])

    def test_web_addresses(self):
        for text in ("visit https://example.com/now", "visit www.example.org", "visit example.com", "write to me@example.com",
                     "visit example dot com", "visit sub.domain.example.co/path?x=1"):
            with self.subTest(text=text):
                self.assertEqual([k for k, _ in kinds(text, {})].count("url"), 1, text)
        self.assertEqual(kinds("visit example.com", {"notes": ["Find us at example.com"]}), [])
        self.assertEqual(kinds("visit https://example.com/", {"notes": ["https://example.com"]}), [])
        self.assertEqual(kinds("visit www.example.com", {"notes": ["example.com is home"]}), [])
        self.assertEqual(kinds("Version 3.5 is out", {"notes": ["Version 3.5"]}), [])
        self.assertEqual(kinds("e.g. this, i.e. that, a.m. and p.m.", {}), [])

    def test_addresses_are_found_whatever_their_ending(self):
        for address in ("nasa.gov/missions", "city.gov", "stanford.edu/papers/42", "astro.garden", "bit.ly/abc", "example.xyz",
                        "radio.ai", "foo.sh", "slow-orbit.ai", "news.example.co.uk", "press@nasa.gov", "Example.Com", "EXAMPLE.ORG",
                        "shop.example.store", "my-site.example.cloud/page", "123.example.tech", "example.travel", "a.b.c.example.zone"):
            with self.subTest(address=address):
                self.assertTrue(has_address(address), address)
                found = kinds(f"Find us at {address} whenever you like.", {})
                self.assertEqual([k for k, _ in found].count("url"), 1, (address, found))
                self.assertEqual(kinds(f"Find us at {address} whenever you like.", {"notes": [f"See {address}"]}), [], address)

    def test_spoken_addresses_are_found_whatever_their_ending(self):
        for ending in ("com", "org", "net", "gov", "edu", "garden", "io", "ai", "fm", "xyz", "radio", "online", "tv", "info"):
            with self.subTest(ending=ending):
                self.assertEqual([k for k, _ in kinds(f"Find us at example dot {ending} today.", {})].count("url"), 1)
        self.assertEqual(kinds("Find us at example dot garden today.", {"notes": ["example dot garden"]}), [])

    def test_ordinary_dotted_text_is_not_an_address(self):
        for text in ("e.g. this, i.e. that, a.m. and p.m.", "Version 3.5 is out.", "FM 101.5 The Bridge.", "It ended.Then it began.",
                     "Mr. Brightside is on.", "St.Louis Blues play.", "We met at 9.30 sharp.", "The U.S. team won.", "Ph.D. students sing.",
                     "A polka dot dress and a dot in the sky.", "Hurry.It is late.", "a.k.a. the Band", "approx. five minutes", "Nov. 5 is close."):
            with self.subTest(text=text):
                self.assertFalse(has_address(text), text)
                self.assertNotIn("url", [k for k, _ in kinds(text, {})], text)

    def test_the_full_stop_after_an_address_still_ends_the_sentence(self):
        # the address used to swallow its full stop, so the next sentence's first word looked like an invented name
        facts = {"notes": ["Book now at smiles.example.co.uk/book"]}
        self.assertEqual(kinds("Book now at smiles.example.co.uk/book. Back to the music.", facts), [])
        self.assertEqual(kinds("Book now at smiles.example.co.uk/book! Back to the music.", facts), [])
        self.assertEqual(kinds("Book now at (smiles.example.co.uk/book). Back to the music.", facts), [])
        self.assertEqual(grounding_lint("Visit https://example.org/a. Thanks for listening.", {"notes": ["https://example.org/a"]}), [])
        self.assertEqual(kinds("Visit city.gov. Then Zorblax.", {"notes": ["city.gov"]}), [("name", "zorblax")])

    def test_only_the_first_part_of_a_very_long_text_is_checked_and_the_lint_says_so(self):
        quiet = "Hello there friends. "
        self.assertEqual(grounding_lint(quiet * 100, {}), [])
        at_limit = (quiet * 400)[:LINT_LIMIT]
        self.assertEqual(len(at_limit), LINT_LIMIT)
        self.assertEqual(grounding_lint(at_limit, {}), [])
        over = grounding_lint(quiet * 400, {})
        self.assertEqual(len(over), 1)
        self.assertIn("6,000 characters", over[0])
        self.assertTrue(over[0].endswith("."))
        loud = " ".join(f"Zorblax{chr(97 + i % 26) * (1 + i // 26)} {i}" for i in range(400))
        self.assertGreater(len(loud), LINT_LIMIT)
        self.assertEqual(len(grounding_lint(loud, {})), 12)                 # findings and the note together stay within the limit
        self.assertIn("6,000 characters", grounding_lint(loud, {})[-1])
        self.assertEqual(grounding_lint(None, {}), [])

    def test_speaker_markers_and_effect_tags_are_not_judged(self):
        self.assertEqual(kinds("<|speaker:3|>Hello [laughs] there.", {}), [])

    def test_the_hosts_own_name_and_phrases_are_grounded(self):
        personas = load_builtin_personas(library().voices)
        nova = personas["nova"]
        self.assertEqual(kinds("Nova here, night owls.", {}, nova), [])
        self.assertEqual(kinds("Nova here, night owls.", {}, personas["dex"]), [("name", "nova")])

    def test_warning_text_is_plain_language(self):
        warnings = grounding_lint("Zorblax plays 7 songs at example.com.", {})
        self.assertEqual(len(warnings), 3)
        for warning in warnings:
            self.assertTrue(warning.startswith("The "), warning)
            self.assertIn("not in your facts", warning)
        self.assertEqual(len(grounding_lint("Zorblax Zorblax Zorblax", {})), 1)

    def test_the_lint_accepts_facts_in_several_shapes(self):
        for shape in ({"now_title": "Seven Nation Army"}, Facts.from_any({"now_title": "Seven Nation Army"})[0],
                      ["Seven Nation Army"], "Seven Nation Army"):
            with self.subTest(shape=type(shape).__name__):
                self.assertEqual(grounding_lint("That was Seven Nation Army, seven times.", shape), [])
        self.assertEqual(len(grounding_lint("That was Seven Nation Army.", None)), 2)   # the number and the name
        self.assertEqual(grounding_lint(None, {}), [])
        self.assertEqual(grounding_lint(12345, {}), [])

    def test_a_long_script_reports_a_bounded_number_of_findings(self):
        text = " ".join(f"Zorblax{chr(97 + i % 26) * (1 + i // 26)} {i}" for i in range(400))
        self.assertLessEqual(len(grounding_lint(text, {})), 12)

    def test_ordinary_sentences_pass_and_invented_specifics_never_slip_through(self):
        import random
        from myradio.hosts.lint import COMMON_WORDS
        rng = random.Random(3)
        common = sorted(w for w in COMMON_WORDS if w.isalpha() and len(w) > 2 and w not in ("one", "first", "second"))
        for _ in range(600):
            words = [rng.choice(common) for _ in range(rng.randint(3, 14))]
            text, capital = "", True
            for word in words:
                text += (word.capitalize() if capital else word) + rng.choice([" ", " ", " ", ", ", ". ", "! "])
                capital = text.rstrip().endswith((".", "!", "?"))
            self.assertEqual(grounding_lint(text.strip(), {}), [], text)
        invented = ["Zorblax", "42", "forty-two", "example.com", "Kalamazoo Jones", "3rd", "No. 9", "November", "1,000", "www.test.org",
                    "Quentin-Blake", "seven", "twenty-one", "the nineties", "a dozen", "https://x.org/y"]
        for _ in range(600):
            words = [rng.choice(common) for _ in range(rng.randint(3, 10))]
            words.insert(rng.randint(1, len(words)), rng.choice(invented))
            text = " ".join(words)
            self.assertTrue(grounding_lint(text[0].upper() + text[1:] + ".", {}), text)

    def test_hostile_text_is_checked_quickly(self):
        started = time.perf_counter()
        for text in ("a-" * 100000, "A " * 100000, "x" * 2000000, "0" * 100000, "http://" + "a" * 100000, "@" * 100000,
                     "a." * 100000, ("one hundred and " * 20000), "٣" * 50000, "(" * 100000 + "Zorblax"):
            grounding_lint(text, {"notes": ["x"]})
        self.assertLess(time.perf_counter() - started, 6.0)

    def test_the_address_pattern_cannot_be_made_to_crawl(self):
        started = time.perf_counter()
        for text in ("a." * 8000, "a.b" * 8000, "0." * 8000, "-" * 16000, "a@" * 8000, "x.y@" * 4000, "www." * 4000, "dot " * 4000,
                     "a" * 16000 + ".", "ab." * 5000 + "c", "\u00e9." * 8000, "." * 16000):
            has_address(text)
            list(__import__("myradio.hosts.lint", fromlist=["URL"]).URL.finditer(text))
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
