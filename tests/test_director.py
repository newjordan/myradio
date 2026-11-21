"""The director (autopilot DJ): what it asks for, where it puts each clip, and what it never does."""
import unittest

from myradio.director.extension import DEFAULTS, Director, clean_settings, speakable_news, topic_query
from myradio.extensions import ExtensionError


class FakeStation:
    """Records every operation; generation jobs finish when the test says so."""

    def __init__(self, songs=6, roles=("host_break", "id", "update"), running=True):
        self.queue = [{"id": 100 + n, "track_id": f"t{n}", "title": f"Song {n}", "artist": "Band", "kind": "music"} for n in range(songs)]
        self.current = {"id": "now", "title": "Now playing", "artist": "Band", "kind": "music"}
        self.running = running
        self.roles = set(roles)
        self.calls = []
        self.jobs = {}
        self.briefs = [{"id": "b" * 32, "topic": "Weather", "items": [{"text": "Clear skies tonight.", "source": "Met office"}]}]
        self.released = []

    def status(self):
        return {"running": self.running, "station": {"name": "Trance", "tagline": "Instrumental trance"}, "current": self.current,
                "queue": [dict(e) for e in self.queue],
                "extensions": {"generation": {"status": {"capabilities": {"roles": {r: True for r in self.roles}}}}}}

    def action(self, op, args=None):
        args = args or {}
        self.calls.append((op, args))
        if op == "hosts.list":
            return {"hosts": [{"id": "marlowe", "hidden": False}, {"id": "dex", "hidden": False}]}
        if op == "hosts.compose":
            return {"text": f"<|speaker:3|>{args['segment']} for {args['facts'].get('next_title', 'the station')}.", "speaker": 3}
        if op == "hosts.compose_update":
            return {"text": "<|speaker:3|>Here is the news.", "speaker": 3}
        if op == "research.save":
            self.saved = args["researcher"]
            return {"researcher": {"id": args["researcher"].get("id") or "r" * 32}}
        if op == "research.run_now":
            return {"state": "done", "outcome": "ready" if self.briefs else "nothing_new",
                    "message": None if self.briefs else "Nothing new and interesting this time. (looked at 2 posts: 2 older than you asked for)"}
        if op == "research.claim":
            return {"brief": self.briefs.pop(0) if self.briefs else None}
        if op == "research.release":
            self.released.append(args["brief_id"])
            return {"released": True}
        if op == "generation.enqueue":
            job_id = f"j{len(self.jobs)}"
            self.jobs[job_id] = {"id": job_id, "state": "queued", "track_id": None, "args": args}
            return {"job": {"id": job_id, "state": "queued"}}
        if op == "generation.job":
            return {"job": self.jobs[args["job_id"]]}
        if op == "queue.add":
            self.queue.insert(args["position"], {"id": 900 + len(self.calls), "track_id": args["track_id"], "title": "clip", "kind": "speech"})
            return {"ok": True}
        if op == "library.delete":
            return {"ok": True}
        raise AssertionError(op)

    def finish(self, job_id, ok=True):
        self.jobs[job_id].update(state="succeeded" if ok else "failed", track_id=f"clip-{job_id}" if ok else None)

    def asked(self, op):
        return [a for o, a in self.calls if o == op]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def director(station, clock=None, **settings):
    return Director(station, clean_settings({"enabled": True, **settings}), clock or Clock())


class DirectorTests(unittest.TestCase):
    def test_it_does_nothing_until_switched_on_and_an_idle_pass_reads_nothing(self):
        st = FakeStation()
        st.status = lambda: self.fail("an idle director must not read the station (status holds the playout lock)")
        Director(st, dict(DEFAULTS), Clock()).tick()
        self.assertEqual(st.calls, [])

    def test_every_third_song_gets_an_intro_naming_it_at_a_background_priority(self):
        st = FakeStation(songs=6)
        d = director(st, station_id_minutes=0, news_minutes=0)
        d.tick()
        intros = st.asked("hosts.compose")
        self.assertEqual([a["facts"]["next_title"] for a in intros], ["Song 2", "Song 5"])
        self.assertEqual(intros[0]["facts"]["now_title"], "Song 1", "the song before it")
        enq = st.asked("generation.enqueue")
        self.assertEqual([(e["role"], e["priority"], e["payload"]["speaker"]) for e in enq], [("host_break", 40, 3)] * 2)
        d.tick()
        self.assertEqual(len(st.asked("generation.enqueue")), 2, "each song is decided once")

    def test_a_ready_intro_goes_right_in_front_of_its_song(self):
        st = FakeStation(songs=6)
        d = director(st, station_id_minutes=0, news_minutes=0)
        d.tick()
        st.finish("j0")
        st.queue.pop(0)   # a song aired meanwhile, so "Song 2" moved up
        d.tick()
        titles = [e["title"] for e in st.queue]
        self.assertEqual(titles[:3], ["Song 1", "clip", "Song 2"])
        self.assertEqual(d.placed, 1)

    def test_an_intro_whose_song_already_played_is_deleted_not_aired(self):
        st = FakeStation(songs=3)
        d = director(st, station_id_minutes=0, news_minutes=0)
        d.tick()
        st.finish("j0")
        st.queue = [e for e in st.queue if e["title"] != "Song 2"]
        d.tick()
        self.assertEqual(st.asked("queue.add"), [])
        self.assertEqual(st.asked("library.delete"), [{"track_id": "clip-j0"}])
        self.assertEqual(d.dropped, 1)

    def test_station_ids_come_on_their_cadence_after_the_next_song(self):
        st = FakeStation(songs=2)
        clock = Clock()
        d = director(st, clock, every_songs=20, news_minutes=0, station_id_minutes=20)
        d.tick()
        self.assertEqual([a["segment"] for a in st.asked("hosts.compose")], ["station_id"])
        self.assertEqual(st.asked("generation.enqueue")[0]["role"], "id")
        st.finish("j0")
        d.tick()
        self.assertEqual(st.asked("queue.add")[-1]["position"], 1)
        clock.t += 10 * 60
        d.tick()
        self.assertEqual(len(st.asked("hosts.compose")), 1, "not again before 20 minutes")
        clock.t += 11 * 60
        d.tick()
        self.assertEqual(len(st.asked("hosts.compose")), 2)

    def test_news_reads_an_approved_update_and_gives_it_back_if_the_speech_fails(self):
        st = FakeStation(songs=1)
        d = director(st, every_songs=20, station_id_minutes=0, news_minutes=30, news_host="marlowe")
        d.tick()
        self.assertEqual(st.asked("hosts.compose_update")[0]["persona"], "marlowe")
        self.assertEqual(st.asked("generation.enqueue")[0]["role"], "update")
        st.finish("j0", ok=False)
        d.tick()
        self.assertEqual(st.released, ["b" * 32])
        self.assertEqual(st.asked("queue.add"), [])

    def test_no_news_means_no_speech(self):
        st = FakeStation(songs=1)
        st.briefs = []
        d = director(st, every_songs=20, station_id_minutes=0)
        d.tick()
        self.assertEqual(st.asked("generation.enqueue"), [])

    def test_it_waits_for_a_voice_and_for_the_station_to_be_on_air(self):
        for st in (FakeStation(roles=()), FakeStation(running=False)):
            director(st).tick()
            self.assertEqual(st.asked("generation.enqueue"), [])

    def test_it_never_has_more_than_ahead_clips_waiting(self):
        st = FakeStation(songs=30)
        d = director(st, every_songs=1, station_id_minutes=0, news_minutes=0, ahead=3)
        d.tick()
        self.assertEqual(len(st.asked("generation.enqueue")), 3)
        st.finish("j0")
        d.tick()
        self.assertEqual(len(st.asked("generation.enqueue")), 4, "one placed, one more asked for")

    def test_a_news_lookup_reads_the_newest_sourced_brief_next_even_with_the_autopilot_off(self):
        st = FakeStation(songs=3)
        st.briefs = [{"id": "o" * 32, "items": [{"headline_speech": "An older topic"}]},
                     {"id": "n" * 32, "items": [{"headline_speech": "Cerebras opens its hackathon"}]}]
        d = Director(st, dict(DEFAULTS), Clock())
        reply = d.ask_news("get me news on the Cerebras hackathon and its winners", "marlowe")
        d.tick()
        self.assertEqual(st.saved["interests"], ["Cerebras", "hackathon", "winners"])
        self.assertIn("q=Cerebras+hackathon+winners", st.saved["sources"][0]["url"])
        self.assertTrue(st.saved["paused"])
        update = st.asked("hosts.compose_update")[0]
        self.assertEqual((update["persona"], update["brief"]["id"]), ("marlowe", "n" * 32), "the newest brief is this request's")
        enq = st.asked("generation.enqueue")[0]
        self.assertEqual((enq["role"], enq["priority"]), ("update", 85))
        self.assertEqual(d.requests[0]["state"], "recording")
        st.finish("j0")
        d.tick()
        self.assertEqual(st.asked("queue.add")[-1]["position"], 0, "a request plays next")
        self.assertEqual((d.requests[0]["key"], d.requests[0]["state"], d.requests[0]["track_id"]), (reply["key"], "queued", "clip-j0"))
        d.ask_news("Something else entirely", None)
        d.tick()
        self.assertEqual(st.saved.get("id"), "r" * 32, "the same researcher is reused")

    def test_a_lookup_with_nothing_found_says_so_and_a_failed_recording_says_why(self):
        st = FakeStation(songs=1)
        st.briefs = []
        d = Director(st, dict(DEFAULTS), Clock())
        d.ask_news("Nothing at all", None)
        d.tick()
        self.assertEqual(d.requests[0]["state"], "nothing")
        self.assertIn("older than you asked for", d.requests[0]["message"], "the research service's own reason is passed on")
        self.assertEqual(st.asked("generation.enqueue"), [])
        st.briefs = [{"id": "x" * 32, "items": []}]
        d.ask_news("Something real", None)
        d.tick()
        st.finish("j0", ok=False)
        d.tick()
        self.assertEqual(d.requests[1]["state"], "failed")
        self.assertEqual(st.released, ["x" * 32])

    def test_google_news_headlines_are_read_with_the_publisher_as_the_source(self):
        brief = {"id": "z" * 32, "items": [
            {"headline_speech": "NASA’s Artemis III Mission to Test Moon Lander Operations - NASA (.gov)",
             "extract_speech": "NASA’s Artemis III Mission to Test Moon Lander Operations NASA (.gov)", "extract_truncated": True,
             "source_name_speech": "Google News", "source_url": "https://news.google.com/rss/search?q=Artemis"},
            {"headline_speech": "Local - a hyphenated name stays", "extract_speech": "Body text.", "source_name_speech": "Town paper",
             "source_url": "https://town.example/feed"}]}
        spoken = speakable_news(brief, "Artemis moon mission")
        first, other = spoken["items"]
        self.assertEqual((first["headline_speech"], first["extract_speech"], first["source_name_speech"]),
                         ("NASA’s Artemis III Mission to Test Moon Lander Operations",) * 2 + ("NASA",))
        self.assertEqual(other, brief["items"][1], "other sources are left alone")
        self.assertEqual(spoken["topic"], "Artemis moon mission")
        self.assertEqual(brief["items"][0]["source_name_speech"], "Google News", "the original brief is not changed")

    def test_lookups_are_checked(self):
        d = Director(FakeStation(), dict(DEFAULTS), Clock())
        for topic, host in (("no", None), ("x" * 201, None), ("Fine topic", "Bad Host!")):
            with self.subTest(topic=topic[:10], host=host), self.assertRaises(ExtensionError):
                d.ask_news(topic, host)
        self.assertEqual(topic_query("what's the latest on the Artemis launch?")[1], ["Artemis", "launch"])

    def test_settings_are_checked(self):
        for bad in ({"every_songs": 0}, {"priority": 90}, {"enabled": "yes"}, {"hosts": "marlowe"}, {"volume": 3}):
            with self.subTest(bad=bad), self.assertRaises(ExtensionError):
                clean_settings(bad)
        self.assertEqual(clean_settings({"hosts": ["dex"]})["hosts"], ["dex"])

    def test_its_state_survives_a_restart(self):
        st = FakeStation(songs=6)
        d = director(st, station_id_minutes=0, news_minutes=0)
        d.tick()
        again = Director(st, d.settings, Clock(), d.state())
        again.tick()
        self.assertEqual(len(st.asked("generation.enqueue")), 2, "no double intros after a restart")


if __name__ == "__main__":
    unittest.main()
