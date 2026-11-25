"""The installed wheel has to carry the files the studio and the host library open by name.

Running from a checkout hides a missing package-data entry: the files are on disk beside the modules.
A wheel is what `pip install` and the one-line installer actually ship.
"""
import contextlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    from setuptools.build_meta import build_wheel
except ImportError:  # a bare interpreter can still run the rest of the suite
    build_wheel = None

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    *(f"myradio/hosts/data/{path.name}" for path in sorted((ROOT / "myradio/hosts/data").glob("*.json"))),
    "myradio/prompts/data/palettes.json",
    "myradio/prompts/data/arrangements.json",
    "myradio/web/index.html",
    "myradio/web/app.js",
    "myradio/web/style.css",
    "myradio/web/art/dial-sleepy.png",
    "myradio/web/art/approve-golive.png",
)


class StudioPage(unittest.TestCase):
    """The desk the browser actually loads. A missing id or a hidden volume slider is a broken studio."""

    def test_autopilot_and_monitor_volume_are_in_the_page(self):
        html = (ROOT / "myradio/web/index.html").read_text(encoding="utf-8")
        js = (ROOT / "myradio/web/app.js").read_text(encoding="utf-8")
        css = (ROOT / "myradio/web/style.css").read_text(encoding="utf-8")
        for element_id in ("autopilot", "autopilot-inline", "maker-live", "volume"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("director.configure", js)
        self.assertIn("renderAutopilot", js)
        self.assertNotIn(".volume-row,.knob{display:none}", css)
        self.assertIn(".volume-row{display:flex;margin:6px 0 0}", css)


class WheelContents(unittest.TestCase):
    @unittest.skipUnless(build_wheel, "setuptools is required to build the wheel")
    def test_wheel_ships_host_data_and_the_studio(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(ROOT)
            log = io.StringIO()
            try:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    name = build_wheel(directory)
            except Exception as exc:
                tail = log.getvalue()[-2000:]
                raise AssertionError(f"wheel build failed: {exc}\n{tail}") from None
            finally:
                os.chdir(previous)
            with zipfile.ZipFile(Path(directory) / name) as archive:
                shipped = set(archive.namelist())
        missing = [path for path in REQUIRED if path not in shipped]
        self.assertEqual(missing, [], f"wheel {name} is missing {missing}")


if __name__ == "__main__":
    unittest.main()
