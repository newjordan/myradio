"""Lifetime station stats (levels and badges in the studio) and the pixel art route."""
import io
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

from myradio.server import WEB, Server, load_token
from myradio.station import FADE_SECONDS, FRAMES, RATE, Station

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def tone(seconds, sample=3000):
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        f.writeframes(struct.pack("<hh", sample, sample) * int(seconds * RATE))
    return out.getvalue()


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.station = Station(self.temp.name, start_worker=False)
        self.addCleanup(self.station.close)

    def stats(self):
        return self.station.status()["stats"]

    def test_a_new_station_starts_at_zero(self):
        self.assertEqual(self.stats(), {"on_air_seconds": 0.0, "aired_music": 0, "aired_speech": 0, "aired_ad": 0, "fades": 0})

    def test_airtime_and_plays_are_counted_by_kind_and_survive_a_restart(self):
        song = self.station.import_wav(tone(2.0), "Song", "Artist", "music")
        link = self.station.import_wav(tone(1.5), "Link", "Host", "speech")
        for track in (song, link):
            self.station.action("queue.add", {"track_id": track["id"]})
        self.station.action("transport.start")
        for _ in range(int(3.5 * RATE / FRAMES) + 2):
            self.station.tick()
        stats = self.stats()
        self.assertEqual((stats["aired_music"], stats["aired_speech"]), (1, 1))
        self.assertGreaterEqual(stats["on_air_seconds"], 3.4)
        self.station.close()
        self.station = Station(self.temp.name, start_worker=False)
        self.addCleanup(self.station.close)
        self.assertEqual(self.stats()["aired_music"], 1)

    def test_the_track_on_air_counts_toward_airtime_and_fades_are_counted(self):
        first = self.station.import_wav(tone(20.0), "Long", "Artist", "music")
        second = self.station.import_wav(tone(20.0), "Next", "Artist", "music")
        for track in (first, second):
            self.station.action("queue.add", {"track_id": track["id"]})
        self.station.action("transport.start")
        for _ in range(10):
            self.station.tick()
        self.assertGreaterEqual(self.stats()["on_air_seconds"], 0.9)
        self.station.action("transport.fade_next")
        for _ in range(int(FADE_SECONDS * RATE / FRAMES) + 2):
            self.station.tick()
        self.assertEqual(self.stats()["fades"], 1)
        self.assertEqual(self.stats()["aired_music"], 1)


class ArtRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.station = Station(self.temp.name)
        self.addCleanup(self.station.close)
        self.server = Server(("127.0.0.1", 0), self.station, load_token(self.temp.name))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        art = WEB / "art"
        art.mkdir(exist_ok=True)
        self.file = art / "zz-test-only.png"
        self.assertFalse(self.file.exists())
        self.file.write_bytes(PNG)
        self.addCleanup(self.file.unlink)

    def get(self, path):
        return self.http.open(self.base + path, timeout=3)

    def test_art_is_served_as_an_image_without_the_launch_code(self):
        with self.get("/art/zz-test-only.png") as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), PNG)

    def test_only_simple_names_inside_the_art_folder(self):
        for path in ("/art/missing.png", "/art/..%2fapp.js", "/art/../app.js", "/art/Zz.png", "/art/zz-test-only.gif"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            self.assertIn(caught.exception.code, (401, 404))


if __name__ == "__main__":
    unittest.main()
