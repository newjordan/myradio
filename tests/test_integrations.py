"""Agent plugins in integrations/: the shared launcher, the Hermes plugin's tools against a real station server,
and the omp package. No harness needs to be installed; the Hermes plugin is driven through a
stand-in for its plugin context."""
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import wave
from unittest.mock import patch

from myradio.server import Server, load_token
from myradio.station import FRAMES, Station

ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS = ROOT / "integrations"
HERMES = INTEGRATIONS / "hermes"
OMP = INTEGRATIONS / "omp"


def audio(sample):
    output = io.BytesIO()
    with wave.open(output, "wb") as f:
        f.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        f.writeframes(struct.pack("<h", sample) * FRAMES)
    return output.getvalue()


def load_hermes_plugin():
    spec = importlib.util.spec_from_file_location("hermes_myradio_plugin", HERMES / "__init__.py",
                                                  submodule_search_locations=[str(HERMES)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def manifest_list(text, key):
    """The items of a top-level YAML list in plugin.yaml (enough YAML for this test; myradio has no YAML parser)."""
    items, inside = [], False
    for line in text.splitlines():
        if line.startswith(key + ":"):
            inside = True
        elif inside and line.startswith("  - "):
            items.append(line[4:].strip())
        elif inside and line and not line.startswith(" "):
            inside = False
    return items


class OldContext:
    """The parts of Hermes 0.16's PluginContext the plugin uses: no settings, cron context or blueprints."""

    def __init__(self, settings=None):
        self.settings, self.tools, self.hooks, self.commands, self.skills, self.blueprints = settings or {}, {}, {}, {}, {}, {}
        self.cli = {}
        self.cron = None

    def register_tool(self, name, toolset, schema, handler, description="", **_):
        self.tools[name] = (toolset, schema, handler)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = handler_fn

    def register_skill(self, name, path, description=""):
        self.skills[name] = Path(path)



class FakeContext(OldContext):
    """Hermes 0.23 adds plugin settings, the cron run in progress and automation blueprints."""

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def current_cron_execution(self):
        return self.cron

    def register_automation_blueprint(self, key, **fields):
        self.blueprints[key] = fields


class StationFixture(unittest.TestCase):
    extensions = None  # a test class may run the station with extensions, e.g. the generation engine

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.station = Station(self.temp.name, start_worker=False,
                               **({"extensions": self.extensions()} if self.extensions else {}))
        self.token = load_token(self.temp.name)
        self.server = Server(("127.0.0.1", 0), self.station, self.token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {"MYRADIO_DATA_DIR": self.temp.name, "MYRADIO_PORT": str(self.port), "MYRADIO_HOME": ""}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.station.close(); self.thread.join()
        self.temp.cleanup()

    def upload(self, title, artist="Band", kind="music"):
        query = urllib.parse.urlencode({"title": title, "artist": artist, "kind": kind})
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/upload?{query}", audio(1000),
                                         {"X-Myradio-Token": self.token, "Content-Type": "audio/wav"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read())["id"]

    def launch(self, folder, messages, env=None):
        merged = {**os.environ, **self.env, **(env or {})}
        return subprocess.run([sys.executable, str(folder / "myradio_mcp.py")], capture_output=True, text=True, timeout=30,
                              input="".join(json.dumps(m) + "\n" for m in messages), env=merged, cwd=tempfile.gettempdir())



class LauncherTests(StationFixture):
    def test_copies_are_identical(self):
        copies = {folder.name: (folder / "myradio_mcp.py").read_bytes() for folder in (HERMES, OMP)}
        self.assertEqual(len(set(copies.values())), 1, "edit integrations/hermes/myradio_mcp.py, then copy it to omp/")

    def test_every_copy_reaches_the_station_from_any_directory(self):
        for folder in (HERMES, OMP):
            done = self.launch(folder, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "station_status", "arguments": {}}}])
            replies = [json.loads(line) for line in done.stdout.splitlines()]
            self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "myradio", folder.name)
            self.assertFalse(replies[1]["result"]["isError"], folder.name)
            self.assertIn("queue", json.loads(replies[1]["result"]["content"][0]["text"]))
            self.assertNotIn(self.token, done.stdout + done.stderr)

    def test_arguments_win_and_missing_station_is_explained(self):
        done = self.launch(OMP, [], env={"MYRADIO_DATA_DIR": str(Path(self.temp.name) / "nowhere")})
        self.assertEqual(done.returncode, 1)
        self.assertIn("Start myradio first", done.stderr)
        done = self.launch(OMP, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "station_status", "arguments": {}}}],
                           env={"MYRADIO_PORT": "1"})
        self.assertIn("Cannot reach myradio", done.stdout)
        done = self.launch(OMP, [], env={"MYRADIO_HOME": self.temp.name})
        self.assertIn("is not a myradio folder", done.stderr)


class HermesPluginTests(StationFixture):
    def setUp(self):
        super().setUp()
        self.plugin = load_hermes_plugin()
        self.ctx = FakeContext({"data_dir": self.temp.name, "port": self.port})
        self.plugin.register(self.ctx)

    def tool(self, name, args=None):
        return json.loads(self.ctx.tools[name][2](args or {}, task_id="t"))

    def test_registrations_match_the_manifest(self):
        manifest = (HERMES / "plugin.yaml").read_text()
        self.assertEqual(sorted(manifest_list(manifest, "provides_tools")), sorted(self.ctx.tools))
        self.assertEqual(manifest_list(manifest, "provides_hooks"), sorted(self.ctx.hooks))
        self.assertEqual({toolset for toolset, _, _ in self.ctx.tools.values()}, {"myradio"})
        for name, (_, schema, _) in self.ctx.tools.items():
            self.assertEqual(schema["name"], name)
            self.assertEqual(schema["parameters"]["type"], "object")
        self.assertEqual(list(self.ctx.commands), ["radio"])
        self.assertEqual(list(self.ctx.skills), ["dj"])
        self.assertTrue(self.ctx.skills["dj"].read_text().startswith("---\nname: dj\n"))
        shift = self.ctx.blueprints["dj-shift"]
        self.assertEqual(shift["skills"], ["myradio:dj"])
        self.assertTrue(all(slot.get("default") for slot in shift["slots"]))
        self.assertEqual(shift["schedule_template"].format(every="10"), "*/10 * * * *")

    def test_dj_tools_drive_the_station(self):
        song = self.upload("First Song")
        self.upload("Second Song", artist="Other")
        status = self.tool("myradio_status")
        self.assertEqual(len(status["library"]), 2)
        self.assertEqual(self.tool("myradio_queue_add", {"track_id": song})["operation"], "queue.add")
        self.assertEqual(self.station.status()["queue"][0]["track_id"], song)
        self.assertEqual(self.tool("myradio_start")["operation"], "transport.start")
        bad = self.tool("myradio_queue_add", {"track_id": "missing"})
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["error"])
        card = self.ctx.commands["radio"]("")  # no playout worker in tests, so the song is still queued
        self.assertIn("MY RADIO", card)
        self.assertIn("\u25cf PLAYING", card)
        self.assertIn("UP NEXT  1. First Song", card)
        self.assertNotIn("\033[", card)  # no platform given (Hermes 0.16): no colour
        self.assertIn("/radio live", self.ctx.commands["radio"]("help"))
        self.assertEqual(self.server.agent_status()["last"], "status")

    def test_owner_only_operations_are_refused_and_the_rest_runs(self):
        listing = self.tool("myradio_operations")["operations"]
        owner_only = {item["operation"] for item in listing if "owner_only" in item}
        self.assertEqual(owner_only, {"broadcast_start", "broadcast_stop", "library_delete"})
        for name in ("broadcast_start", "broadcast.start", "library_delete"):
            refused = self.tool("myradio_run", {"operation": name})
            self.assertFalse(refused["ok"])
            self.assertIn("owner", refused["error"])
        self.assertEqual(self.station.status()["broadcast"]["state"], "disconnected")
        renamed = self.tool("myradio_run", {"operation": "station.update", "arguments": {"tagline": "Night owls"}})
        self.assertTrue(renamed["ok"])
        self.assertEqual(self.station.status()["station"]["tagline"], "Night owls")
        self.assertIn("Unknown station tool", self.tool("myradio_run", {"operation": "nope"})["error"])
        self.assertTrue(self.ctx.commands["radio"]("live").startswith("myradio: "))  # the station's own refusal
        self.assertEqual(self.station.status()["broadcast"]["state"], "disconnected")

    def test_scheduled_runs_only_do_the_dj_shift(self):
        guard = self.ctx.hooks["pre_tool_call"]
        self.assertIsNone(guard(tool_name="myradio_run", args={"operation": "station_update"}, task_id="t"))
        self.ctx.cron = object()
        self.assertEqual(guard(tool_name="myradio_run", args={"operation": "station.update"}, task_id="t")["action"], "block")
        self.assertEqual(guard(tool_name="myradio_run", args={"operation": "program_remove"})["action"], "block")
        for tool, args in (("myradio_queue_add", {"track_id": "x"}), ("myradio_status", {}), ("myradio_operations", {}),
                           ("myradio_library", {"query": "x"}),
                           ("myradio_run", {"operation": "shows.plan"}), ("terminal", {"command": "ls"})):
            self.assertIsNone(guard(tool_name=tool, args=args), tool)

    def test_station_down_and_old_hermes(self):
        self.ctx.settings["port"] = 1
        down = self.tool("myradio_status")
        self.assertFalse(down["ok"])
        self.assertIn("not answering on port 1", down["error"])
        self.assertIn("python3 -m myradio.server --port 1", down["error"])
        self.assertIn("myradio: Your station is not answering", self.ctx.commands["radio"](""))
        self.ctx.settings.update(port=self.port, data_dir=str(Path(self.temp.name) / "elsewhere"))
        missing = self.tool("myradio_status")["error"]
        self.assertIn("No myradio station found in " + str(Path(self.temp.name) / "elsewhere"), missing)
        self.assertIn("settings.data_dir", missing)

        old = OldContext()
        self.plugin.register(old)
        with patch.dict(os.environ, {"MYRADIO_DATA_DIR": self.temp.name, "MYRADIO_PORT": str(self.port)}):
            self.assertIn("queue", json.loads(old.tools["myradio_status"][2]({})))
            self.assertIsNone(old.hooks["pre_tool_call"](tool_name="myradio_run", args={"operation": "station_update"}))
        self.assertEqual(old.blueprints, {})

    def test_status_for_the_model_is_compact_and_the_library_is_searchable(self):
        song = self.upload("Big Earl", artist="Tulsa")
        self.upload("Station ID", artist="", kind="speech")
        self.tool("myradio_queue_add", {"track_id": song})
        status = self.tool("myradio_status")
        self.assertEqual(status["queue"][0]["track_id"], song)
        self.assertEqual(status["queue"][0]["queue_id"], str(self.station.status()["queue"][0]["id"]))
        self.assertEqual(status["library_counts"], {"music": 1, "speech": 1})
        self.assertEqual({t["title"] for t in status["library"]}, {"Big Earl", "Station ID"})
        self.assertNotIn("extensions", status)
        moved = self.tool("myradio_queue_move", {"queue_id": status["queue"][0]["queue_id"], "position": 0})
        self.assertTrue(moved["ok"])
        found = self.tool("myradio_library", {"query": "big EARL"})
        self.assertEqual([t["track_id"] for t in found["tracks"]], [song])
        self.assertEqual(self.tool("myradio_library", {"kind": "speech"})["tracks"][0]["title"], "Station ID")
        self.assertIn("Nothing matched", self.tool("myradio_library", {"query": "polka"})["note"])
        self.assertFalse(self.tool("myradio_library", {"limit": "lots"})["ok"])

        big = {"library": [{"id": f"t{n}", "title": f"Song {n}", "kind": "music", "duration": 100} for n in range(200)]}
        compact = self.plugin.station.compact_status(big)
        self.assertIsNone(compact["library"])
        self.assertIn("myradio_library", compact["library_note"])
        tracks, total = self.plugin.station.find_tracks(big, "song 1", limit=5)
        self.assertEqual((len(tracks), total, tracks[0]["title"]), (5, sum("1" in str(n) for n in range(200)), "Song 1"))

    def test_radio_card_play_and_platforms(self):
        self.upload("Big Earl (The Pride of Tulsa)", artist="Tulsa")
        self.upload("Still Steal the Blanket")
        self.assertIn("Nothing in the library matches", self.ctx.commands["radio"]("play polka"))
        self.assertEqual(self.ctx.commands["radio"]("play  big earl"), "Up next: Big Earl (The Pride of Tulsa).")
        self.assertEqual(self.station.status()["queue"][0]["title"], "Big Earl (The Pride of Tulsa)")
        cli = self.ctx.commands["radio"]("now", platform="cli")
        self.assertIn("\033[", cli)
        self.assertIn("Big Earl", cli)
        chat = self.ctx.commands["radio"]("", platform="telegram")
        self.assertTrue(chat.startswith("```\n\u256d") and chat.endswith("\u256f\n```"))
        lines = self.ctx.commands["radio"]("", platform="tui").splitlines()
        self.assertEqual({self.plugin.station.cells(line) for line in lines}, {self.plugin.station.WIDTH})

        status = {"station": {"name": "Late Epoch"}, "running": True, "elapsed": 61,
                  "current": {"title": "A very long song title that will not fit on one line of the card at all", "duration": 150},
                  "queue": [{"title": f"Song {n}", "duration": 120, "kind": "speech" if n == 2 else "music"} for n in range(1, 6)],
                  "broadcast": {"state": "streaming", "configured": True}, "stream": {"listeners": 3}}
        card = self.plugin.station.card(status)
        self.assertIn("\u25cf LIVE", card)
        self.assertIn("1:01 / 2:30", card)
        self.assertIn("1:29 left", card)
        self.assertIn("2. Song 2 (DJ)", card)
        self.assertIn("+ 2 more, 10:00 queued in all", card)
        self.assertIn("LIVE, 3 listening", card)
        self.assertIn("\u2026", card)
        self.assertEqual({self.plugin.station.cells(line) for line in card.splitlines()}, {self.plugin.station.WIDTH})

    def test_child_gets_no_secrets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "MYRADIO_PORT": "9999"}):
            env = self.plugin.station.child_env({"data_dir": "/data", "port": 0})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual((env["MYRADIO_DATA_DIR"], env["MYRADIO_PORT"]), ("/data", "9999"))
        env = self.plugin.station.child_env({"data_dir": "~/radio-data"})
        self.assertEqual(env["MYRADIO_DATA_DIR"], str(Path.home() / "radio-data"))


class HermesSongRequestTests(StationFixture):
    """/radio request against a station running the real generation engine with no maker set up."""

    @staticmethod
    def extensions():
        from myradio.generation.extension import GenerationExtension
        return [GenerationExtension(probe_min_interval=0)]

    def setUp(self):
        super().setUp()
        self.plugin = load_hermes_plugin()
        self.ctx = FakeContext({"data_dir": self.temp.name, "port": self.port})
        self.plugin.register(self.ctx)

    def test_radio_request_writes_the_song_and_asks_the_maker(self):
        class Result:
            parsed = {"title": "Green Build", "tags": "sea shanty, accordion, stomping, rowdy male choir",
                      "lyrics": "[verse]\nHeave the tests\n\n[chorus]\nThe build is green"}

        class Llm:
            calls = []

            def complete_structured(self, **kw):
                self.calls.append(kw)
                return Result()

        self.ctx.llm = Llm()
        sent = []
        real_call = self.plugin.station.call

        def call(tool, arguments=None, settings=None):
            sent.append((tool, arguments))
            return real_call(tool, arguments, settings)

        with patch.object(self.plugin.station, "call", call):
            said = self.ctx.commands["radio"]("request a sea shanty about fixing the build")
        self.assertIn("No music maker is set up yet", said)  # the station's own words: this test station has no maker
        self.assertTrue(said.startswith("myradio: "))
        self.assertEqual(sent[-1][0], "generation_enqueue")
        self.assertEqual(sent[-1][1]["role"], "song")
        self.assertEqual(sent[-1][1]["payload"], {"tags": Result.parsed["tags"], "title": "Green Build", "lyrics": Result.parsed["lyrics"]})
        self.assertEqual(Llm.calls[0]["input"], [{"type": "text", "text": "a sea shanty about fixing the build"}])
        from myradio.generation.payloads import normalize_payload
        self.assertEqual(normalize_payload("song", sent[-1][1]["payload"]).title, "Green Build")  # what a real maker accepts

        queued = {"job": {"id": "j1", "state": "queued"}, "created": True}
        with patch.object(self.plugin.station, "call", lambda tool, arguments=None, settings=None: sent.append(arguments) or queued):
            said = self.ctx.commands["radio"]("request a sea shanty about fixing the build")
        self.assertIn('Requested "Green Build" (written by your Hermes model).', said)
        self.assertIn("Job j1 is queued", said)

        Result.parsed = dict(Result.parsed, lyrics="  Heave the tests, the build is red\n  Pull the rope till it is green")
        self.ctx.commands["radio"]("request another one")  # unlabelled lyrics get a section line the maker accepts
        self.assertTrue(sent[-1]["payload"]["lyrics"].startswith("[verse]\nHeave the tests"))
        normalize_payload("song", sent[-1]["payload"])

        old = OldContext({"data_dir": self.temp.name, "port": self.port})  # no ctx.llm: the idea itself is the style
        self.plugin.register(old)
        with patch.object(self.plugin.station, "call", lambda tool, arguments=None, settings=None: sent.append(arguments) or queued):
            said = old.commands["radio"]("request rainy day lofi")
        self.assertEqual(sent[-1]["payload"], {"tags": "rainy day lofi", "title": "rainy day lofi"})
        normalize_payload("song", sent[-1]["payload"])
        self.assertIn("instrumental", said)
        self.assertIn("Say what the song is about", old.commands["radio"]("request"))


class OmpPackageTests(unittest.TestCase):
    def test_package_layout(self):
        package = json.loads((OMP / "package.json").read_text())
        self.assertEqual(package["name"], "omp-myradio")
        self.assertIn("omp", package)  # omp skips linked packages without an omp manifest
        server = json.loads((OMP / ".mcp.json").read_text())["mcpServers"]["myradio"]
        self.assertEqual(server["args"], ["${OMP_PLUGIN_ROOT}/myradio_mcp.py"])
        self.assertTrue((OMP / "commands/radio.md").read_text().startswith("---\ndescription:"))
        self.assertIn("\ndescription: ", (OMP / "skills/myradio-dj/SKILL.md").read_text().split("---")[1])



if __name__ == "__main__":
    unittest.main()
