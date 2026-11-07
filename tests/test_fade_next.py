"""transport.fade_next: the current track fades out over FADE_SECONDS, then the next one starts in the same block."""
import array
import io
import struct
import sys
import tempfile
import unittest
import wave

from myradio.station import ARGUMENTS, FADE_SECONDS, FRAMES, OPERATIONS, RATE, Station

LOUD = 10000


def tone(sample, seconds):
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        f.writeframes(struct.pack("<hh", sample, sample) * int(seconds * RATE))
    return out.getvalue()


def levels(block):
    """Absolute value of the left channel at every frame of a block."""
    samples = array.array("h")
    samples.frombytes(block)
    if sys.byteorder == "big":
        samples.byteswap()
    return [abs(v) for v in samples[0::2]]


class FadeNextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.station = Station(self.temp.name, start_worker=False)
        self.addCleanup(self.station.close)
        self.first = self.station.import_wav(tone(LOUD, 30), "First", "Artist", "music")
        self.second = self.station.import_wav(tone(-LOUD, 30), "Second", "Artist", "music")
        self.station.action("queue.add", {"track_id": self.first["id"]})
        self.station.action("queue.add", {"track_id": self.second["id"]})
        self.station.action("transport.start")

    def blocks(self, n):
        return [self.station.tick() for _ in range(n)]

    def test_the_operation_is_offered_to_agents(self):
        self.assertIn("transport.fade_next", OPERATIONS)
        self.assertEqual(ARGUMENTS["transport.fade_next"], {})

    def test_fades_over_two_seconds_then_the_next_track_starts_in_the_same_block(self):
        self.blocks(5)
        self.station.action("transport.fade_next")
        self.assertTrue(self.station.status()["fading"])
        fade_blocks = int(FADE_SECONDS * RATE) // FRAMES
        out = self.blocks(fade_blocks + 1)
        first_levels = levels(out[0])
        self.assertLess(first_levels[-1], first_levels[0])                      # getting quieter inside a block
        peaks = [max(levels(b)[:FRAMES // 2]) for b in out[:fade_blocks]]
        self.assertEqual(peaks, sorted(peaks, reverse=True))                    # and quieter block after block
        self.assertLess(peaks[-1], LOUD * 0.02)
        status = self.station.status()
        self.assertEqual(status["current"]["title"], "Second")                  # no gap: the next track is already on air
        self.assertFalse(status["fading"])
        self.assertTrue(all(v == LOUD for v in levels(out[-1])[-100:]))        # at full level, not faded
        self.assertEqual(sum(1 for b in out if not any(levels(b))), 0)          # never a silent block

    def test_the_log_says_the_track_was_faded(self):
        self.blocks(2)
        self.station.action("transport.fade_next")
        self.blocks(int(FADE_SECONDS * RATE) // FRAMES + 2)
        events = [h["event"] for h in self.station.status()["history"] if h["title"] == "First"]
        self.assertIn("faded to next", events)

    def test_pressing_twice_does_not_restart_the_fade(self):
        self.blocks(2)
        self.station.action("transport.fade_next")
        self.blocks(10)
        self.station.action("transport.fade_next")
        self.blocks(int(FADE_SECONDS * RATE) // FRAMES - 10 + 1)
        self.assertEqual(self.station.status()["current"]["title"], "Second")

    def test_skip_or_stop_during_a_fade_ends_it(self):
        self.blocks(2)
        self.station.action("transport.fade_next")
        self.blocks(3)
        self.station.action("transport.skip")
        self.assertFalse(self.station.status()["fading"])
        self.assertTrue(all(v == LOUD for v in levels(self.blocks(1)[0])))     # the next track is at full level

    def test_without_a_track_on_air_it_acts_like_skip(self):
        self.station.action("transport.stop")
        self.station.action("transport.fade_next")
        self.assertFalse(self.station.status()["fading"])


if __name__ == "__main__":
    unittest.main()
