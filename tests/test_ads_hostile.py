"""Hostile and malformed input: the parser, the operations, the routes and the event handler must refuse
cleanly, never with a raw error, and never leave half a spot behind."""
import json
import random
import string
import unittest

from myradio.ads.model import AdsError, parse_campaign
from myradio.extensions import ExtensionError

try:
    from test_ads_support import NOW, TODAY, Harness
except ImportError:  # started as "python -m unittest tests/test_ads_x.py", where the tests folder is not on the path
    from tests.test_ads_support import NOW, TODAY, Harness

GENERIC = "operation failed"
WEIRD_TEXT = ["", " ", "\x00", "\ud800", "‮", "​" * 50, "x" * 5000, "é" * 500, "<|speaker:1|>", "[laughs]", "{sponsor}", "{}", "%s", "'; DROP TABLE campaigns; --",
              "../../etc/passwd", "=1+1", "null", "true", "9" * 400, "-0", "NaN", "\n\r\t", "😀" * 200, "A" * 33, "f" * 32, "0" * 32]
WEIRD_NUMBERS = [0, -1, 1, 2 ** 31, 2 ** 70, -2 ** 70, 1.5, -0.0, 1e308, float("inf"), float("-inf"), float("nan"), True, False]


def junk(rng, depth=0):
    kind = rng.randrange(8 if depth < 3 else 5)
    if kind == 0:
        return rng.choice(WEIRD_TEXT)
    if kind == 1:
        return rng.choice(WEIRD_NUMBERS)
    if kind == 2:
        return None
    if kind == 3:
        return rng.choice([True, False])
    if kind == 4:
        return "".join(rng.choice(string.printable) for _ in range(rng.randrange(0, 40)))
    if kind == 5:
        return [junk(rng, depth + 1) for _ in range(rng.randrange(0, 5))]
    keys = ["id", "name", "advertiser", "category", "house", "priority", "pacing", "frequency", "timezone", "flights", "caps", "separation",
            "plan", "creatives", "paused", "start", "end", "days", "dayparts", "show_ids", "from", "to", "kind", "track_id", "text",
            "disclosure", "voice", "per_hour", "per_day", "per_week", "total", "campaign_minutes", "category_minutes"]
    return {rng.choice(keys + WEIRD_TEXT[:6]): junk(rng, depth + 1) for _ in range(rng.randrange(0, 6))}


def valid_campaign():
    return {"advertiser": {"name": "Corner Bike Co.", "category": "bikes"}, "name": "Spring", "priority": 6, "pacing": "even",
            "frequency": "regular", "timezone": "America/New_York", "plan": {"total": 40},
            "flights": [{"start": "2026-11-12", "end": "2026-12-31", "days": "1,2,3,4,5", "dayparts": [{"from": "07:00", "to": "19:00"}],
                         "show_ids": ["morning"]}],
            "caps": {"per_hour": 2, "per_day": 8, "per_week": 30}, "separation": {"campaign_minutes": 20, "category_minutes": 10},
            "creatives": [{"kind": "recorded", "track_id": "track-1", "title": "Tune-up"},
                          {"kind": "script", "text": "Fixing bikes since 1998.", "voice": "dex", "disclosure": "Sponsored by {sponsor}"}]}


class ParserTests(unittest.TestCase):
    def check(self, raw):
        try:
            campaign, warnings = parse_campaign(raw, today=TODAY)
        except AdsError as exc:
            message = str(exc)
            self.assertTrue(message and len(message) < 400, message)
            return None
        # Anything accepted must be fully normalized and safe to store.
        self.assertEqual(len(campaign.id), 32)
        self.assertTrue(campaign.name and campaign.advertiser and campaign.creatives)
        for text in (campaign.name, campaign.advertiser, *(c.title for c in campaign.creatives), *(c.text for c in campaign.creatives)):
            self.assertEqual(text, " ".join(text.split()))
            self.assertTrue(all(ch.isprintable() for ch in text), repr(text))
            text.encode("utf-8")  # no stray surrogates
        json.dumps(campaign.to_dict())
        return campaign

    def test_random_junk_is_refused_with_the_parsers_own_error(self):
        rng = random.Random(1105)
        for _ in range(1500):
            self.check(junk(rng))

    def test_a_valid_spot_with_one_field_replaced_by_junk(self):
        rng = random.Random(2611)
        accepted = 0
        for _ in range(1500):
            raw = valid_campaign()
            targets = ["advertiser", "name", "priority", "pacing", "frequency", "timezone", "plan", "flights", "caps", "separation", "creatives",
                       "category", "house", "paused"]
            key = rng.choice(targets)
            raw[key] = junk(rng)
            if rng.random() < 0.3 and isinstance(raw.get("flights"), list) and raw["flights"] and isinstance(raw["flights"][0], dict):
                raw["flights"][0][rng.choice(["start", "end", "days", "dayparts", "show_ids"])] = junk(rng)
            if rng.random() < 0.3 and isinstance(raw.get("creatives"), list) and raw["creatives"] and isinstance(raw["creatives"][0], dict):
                raw["creatives"][0][rng.choice(["kind", "track_id", "text", "title", "voice", "disclosure"])] = junk(rng)
            accepted += self.check(raw) is not None
        self.assertGreater(accepted, 20, "the fuzzer should also reach the accepting paths")

    def test_every_combination_of_one_wrong_type_per_field(self):
        base = valid_campaign()
        for key in list(base):
            for wrong in (None, 5, 1.5, True, [], {}, "", "x" * 300, [None], {"a": 1}):
                with self.subTest(key=key, wrong=repr(wrong)[:20]):
                    raw = json.loads(json.dumps(base))
                    raw[key] = wrong
                    self.check(raw)
        for section in ("flights", "creatives"):
            for key in list(base[section][0]):
                for wrong in (None, 5, [], {}, "", "x" * 300, [None], True, 1e400):
                    with self.subTest(section=section, key=key, wrong=repr(wrong)[:20]):
                        raw = json.loads(json.dumps(base))
                        raw[section][0][key] = wrong
                        self.check(raw)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.confirm("voice_rights", "commercial_use")
        self.track = self.h.station.add_track("Spot")

    def assertCleanRefusal(self, call):
        try:
            call()
        except ExtensionError as exc:
            self.assertNotIn(GENERIC, str(exc), "an unexpected internal error was reported instead of a plain refusal")
            self.assertTrue(str(exc))

    def test_random_campaigns_through_the_operation(self):
        rng = random.Random(77)
        for _ in range(400):
            payload = valid_campaign() if rng.random() < 0.5 else {}
            payload["creatives"] = [{"kind": "recorded", "track_id": self.track}] if rng.random() < 0.7 else payload.get("creatives")
            payload[rng.choice(["advertiser", "timezone", "flights", "creatives", "plan", "caps", "id", "frequency"])] = junk(rng)
            self.assertCleanRefusal(lambda: self.h.call("campaign_save", campaign=payload))
        for campaign in self.h.route("GET", "/campaigns").body["campaigns"]:
            self.assertEqual(campaign["id"], campaign["id"].lower())  # whatever got in is still well-formed
        self.assertEqual(self.h.ext._store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_random_arguments_to_every_other_operation(self):
        rng = random.Random(99)
        names = ["campaign_get", "campaign_pause", "campaign_resume", "campaign_remove", "break_build", "spot_speech", "spot_audio", "report"]
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "script", "text": "Buy bikes."}])
        for _ in range(600):
            name = rng.choice(names)
            args = {k: junk(rng) for k in rng.sample(["campaign", "creative", "track_id", "seconds", "show_id", "at", "seed", "record",
                                                       "ready_only", "from_date", "to_date"], rng.randrange(0, 5))}
            if rng.random() < 0.4:
                args["campaign"] = saved["id"]
            self.assertCleanRefusal(lambda: self.h.host.call("ads." + name, args))
        with self.h.ext._lock:  # whatever those calls did, the database is intact
            self.assertEqual(self.h.ext._store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_hostile_times_are_refused_plainly(self):
        self.h.save(advertiser="Acme")
        for bad in ("9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+14:00", "2026-11-05T25:00:00+00:00", "2026-02-30T00:00:00+00:00", "now",
                    "1e9", "2026-11-05", "２０２６-11-05T10:00:00+00:00", "\x00", "2026-11-05T15:00:00+99:00", "9" * 40):
            with self.subTest(at=bad), self.assertRaises(ExtensionError) as caught:
                self.h.call("break_build", seconds=60, at=bad)
            self.assertNotIn(GENERIC, str(caught.exception))
            reply = self.h.route("GET", "/break", {"at": [bad]})
            self.assertEqual(reply.status, 400)

    def test_random_queries_on_every_route(self):
        rng = random.Random(3)
        paths = ["/campaigns", "/break", "/report", "/report.csv", "/acknowledge"]
        for _ in range(500):
            path = rng.choice(paths)
            query = {k: [str(junk(rng))[:80] for _ in range(rng.randrange(0, 3))] for k in rng.sample(
                ["campaign", "seconds", "show_id", "at", "from", "to", "ready_only", "limit"], rng.randrange(0, 4))}
            reply = self.h.route("GET", path, query)
            self.assertIn(reply.status, (200, 400), (path, query, reply.body))
        for _ in range(300):
            body = junk(rng)
            reply = self.h.route("POST", "/acknowledge", body=body if isinstance(body, dict) else {"x": body})
            self.assertIn(reply.status, (200, 400), (body, reply.body))
        self.assertTrue(self.h.status()["acknowledgements"]["voice_rights"]["acknowledged"] in (True, False))

    def test_random_events_never_raise_and_never_damage_the_log(self):
        rng = random.Random(5)
        saved = self.h.save(advertiser="Acme", creatives=[{"kind": "recorded", "track_id": self.track}])
        for _ in range(800):
            event = {"type": rng.choice(["track_started", "track_finished", "transport", junk(rng)]), "at": junk(rng) if rng.random() < 0.5 else NOW + rng.random() * 3600,
                     "result": junk(rng) if rng.random() < 0.5 else rng.choice(["finished", "skipped", "stopped", "playback failed"]),
                     "track": rng.choice([{"id": self.track, "kind": "ad", "title": junk(rng), "duration": junk(rng)},
                                          {"id": self.track, "kind": rng.choice(["ad", "music", None, 5])}, junk(rng)])}
            self.h.ext.on_event(event)
        with self.h.ext._lock:
            self.assertEqual(self.h.ext._store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            rows = self.h.ext._store.conn.execute("SELECT started_at, ended_at, outcome FROM plays").fetchall()
        for row in rows:
            self.assertTrue(row["started_at"] > 0)
            self.assertIn(row["outcome"], (None, "finished", "interrupted"))
        report = self.h.route("GET", "/report", {"campaign": [saved["id"]]})
        self.assertEqual(report.status, 200)


if __name__ == "__main__":
    unittest.main()
