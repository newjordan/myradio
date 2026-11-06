"""Doctor checks: one class per check, each run against simulated computers.

The simulated computers are built by patching the standard-library calls the checks make
(``platform``, ``shutil.which``, ``os``, ``socket``, ``sqlite3``, ``zoneinfo``) and the doctor's own
tool runner, plus a few small real programs written to a temporary directory. Nothing real is
installed, no GPU or model is touched, and the only sockets opened are on 127.0.0.1 with port 0.

macOS and GPU scenarios use FIXTURES (see test_doctor_fixtures.py): they show how the checks react to
output of that shape, not what a real Mac prints.
"""
import collections
import datetime as dt
import errno
import importlib.util
import os
import re
import shlex
import socket
import sqlite3
import stat
import sys
import tempfile
import time
import types
import unittest
import zoneinfo
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from myradio.doctor import checks
from myradio.doctor.checks import CHECKS, DEFAULT_PORT, STATES, Settings, parse_codec_list, platform_family
from myradio.doctor.tools import ToolResult

from test_doctor_fixtures import (ARCH, FEDORA, GOOD_DECODERS, GOOD_ENCODERS, HEARTBEAT, UBUNTU, ToolTable, alive_after, banner, ffmpeg_table,
                                  guard_ports, hermetic_tools, listing, make_fake_ffmpeg, make_program, make_roots, pretend_computer, profiler_json)

KEYS = ["id", "title", "status", "detail", "hint", "fix"]
# Words a nontechnical person should never have to read in a title, detail or hint.
JARGON = (r"\bPCM\b", r"\bGGUF\b", r"\bSSRF\b", r"traceback", r"stack trace", r"\berrno\b", r"\bexception", r"\bOSError\b",
          r"\bfcntl\b", r"\bloopback\b", r"\btzdata\b", r"\bzoneinfo\b", r"\bIANA\b", r"libmp3lame", r"\bstderr\b", r"\bstdout\b",
          r"\bsubprocess\b", r"\bsegfault\b", r"\bWAL\b")
Usage = collections.namedtuple("Usage", "total used free")
GIB, MIB = 1024 ** 3, 1024 ** 2
NOW = dt.datetime(2026, 11, 5, 18, 0, tzinfo=dt.timezone.utc).timestamp()
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
HAS_TIME_ZONE_DATA = (any(os.path.exists(os.path.join(p, "America", "New_York")) for p in zoneinfo.TZPATH)
                      or importlib.util.find_spec("tzdata") is not None)
CLOCK_EARLIEST = checks.CLOCK_EARLIEST
CLOCK_LATEST = checks.CLOCK_LATEST
# What a person is told when a command would be unsafe or too long to copy whole. Written out here so that a change of wording is noticed.
GENERAL_FIX = "Use a folder just for your station and start myradio with, for example: myradio --data-dir ~/.myradio"


def usage(free):
    return Usage(free * 4, free * 3, free)


def fix_command(fix):
    """What a Copy button copies from a ``fix``: the text after the first ': ', up to a two-space note. "" when the fix is only advice.

    This is the rule the contract in docs/lanes/doctor.md gives the page. Every fix any test produces must satisfy it.
    """
    head, found, rest = fix.partition(": ")
    return rest.split("  (")[0].strip() if found else ""


class CheckCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"HOME": "/home/test-user"}))  # so ~ never depends on who runs the tests
        self.ports = guard_ports(self)  # no check may bind a fixed port such as 8765: that is where a running station listens

    def pretend(self, **kwargs):
        return pretend_computer(self.stack, **kwargs)

    def station(self, mode=0o700, name="station"):
        path = self.dir / name
        path.mkdir()
        os.chmod(path, mode)
        return path

    def well_formed(self, result, check_id):
        self.assertEqual(list(result), KEYS)
        self.assertEqual(result["id"], check_id)
        self.assertIn(result["status"], STATES)
        for key in KEYS:
            self.assertIsInstance(result[key], str)
        self.assertTrue(result["title"] and result["detail"])
        self.assertLessEqual(len(result["detail"]), 400)
        self.assertLessEqual(len(result["hint"]), 400)
        self.assertLessEqual(len(result["fix"]), 400)
        if result["status"] != "ok":
            self.assertTrue(result["hint"], f"{check_id} {result['status']} needs a hint: {result}")
        for key in ("title", "detail", "hint"):
            for pattern in JARGON:
                self.assertIsNone(re.search(pattern, result[key], re.IGNORECASE), f"jargon {pattern!r} in {check_id}.{key}: {result[key]}")
        for key in KEYS:
            self.assertFalse(re.search(r"[\x00-\x1f\x7f]", result[key]), f"control character in {check_id}.{key}")
        command = fix_command(result["fix"])
        if command:  # a command that was cut short or left a quote open could not be pasted into a terminal
            try:
                self.assertTrue(shlex.split(command))
            except ValueError:
                self.fail(f"the command in {check_id}.fix cannot be copied as one piece: {command!r}")
        return result


# ---------------------------------------------------------------------- python

class PythonCheck(CheckCase):
    def run_check(self, version, implementation="CPython"):
        return self.well_formed(checks.check_python(Settings(), version=version, implementation=implementation), "python")

    def test_supported_versions_are_ok(self):
        for version in ((3, 10, 0), (3, 11, 9), (3, 12, 3), (3, 13, 5)):
            with self.subTest(version=version):
                result = self.run_check(version)
                self.assertEqual(result["status"], "ok")
                self.assertIn(".".join(map(str, version)), result["detail"])

    def test_too_old_fails_with_a_fix_for_each_kind_of_computer(self):
        for kwargs, expected in (({"system": "Darwin", "machine": "arm64"}, "python.org/downloads/macos"),
                                 ({"system": "Linux", "os_release": UBUNTU}, "apt install python3"),
                                 ({"system": "Linux", "os_release": FEDORA}, "python.org/downloads")):
            with self.subTest(expected=expected), ExitStack() as stack:
                pretend_computer(stack, **kwargs)
                for version in ((3, 9, 18), (3, 8, 0), (2, 7, 18)):
                    result = self.run_check(version)
                    self.assertEqual(result["status"], "fail")
                    self.assertIn("too old", result["detail"])
                    self.assertIn(expected, result["fix"])

    def test_untested_versions_and_other_implementations_only_warn(self):
        newer = self.run_check((3, 14, 0))
        self.assertEqual(newer["status"], "warn")
        self.assertIn("3.10 to 3.13", newer["detail"])
        other = self.run_check((3, 11, 4), "PyPy")
        self.assertEqual(other["status"], "warn")
        self.assertIn("PyPy", other["detail"])

    def test_reads_the_running_interpreter_by_default(self):
        result = self.well_formed(checks.check_python(), "python")
        self.assertIn(platform_version(), result["detail"])


def platform_version():
    return ".".join(str(part) for part in sys.version_info[:3])


# ---------------------------------------------------------------------- system

class PlatformFamily(CheckCase):
    def family(self, **kwargs):
        with ExitStack() as stack:
            pretend_computer(stack, **kwargs)
            return platform_family()

    def test_each_kind_of_computer_gets_its_own_install_advice(self):
        cases = ((dict(system="Darwin", machine="arm64"), "macos"),
                 (dict(system="Linux", os_release={"ID": "ubuntu", "ID_LIKE": "debian"}), "debian"),
                 (dict(system="Linux", os_release={"ID": "debian"}), "debian"),
                 (dict(system="Linux", os_release={"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}), "debian"),
                 (dict(system="Linux", os_release={"ID": "pop", "ID_LIKE": "ubuntu debian"}), "debian"),
                 (dict(system="Linux", os_release={"ID": "arch"}), "arch"),
                 (dict(system="Linux", os_release={"ID": "manjaro", "ID_LIKE": "arch"}), "arch"),
                 (dict(system="Linux", os_release={"ID": "fedora"}), "linux"),
                 (dict(system="Linux", os_release={}), "linux"),
                 (dict(system="Linux", os_release=None), "linux"),
                 (dict(system="Windows"), "windows"),
                 (dict(system="FreeBSD"), "other"))
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.family(**kwargs), expected)

    def test_a_computer_that_will_not_say_what_it_is_is_just_other(self):
        with patch("platform.system", side_effect=RuntimeError("no")):
            self.assertEqual(platform_family(), "other")
        with ExitStack() as stack:
            pretend_computer(stack, system="Linux")
            with patch("platform.freedesktop_os_release", return_value={"ID": None, "ID_LIKE": 5}):
                self.assertEqual(platform_family(), "linux")


class SystemCheck(CheckCase):
    def run_check(self):
        return self.well_formed(checks.check_system(Settings()), "system")

    def test_ubuntu_on_x86(self):
        self.pretend(system="Linux", machine="x86_64", release="6.8.0-test", os_release=UBUNTU)
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("Ubuntu 24.04 LTS", result["detail"])
        self.assertIn("x86-64", result["detail"])

    def test_linux_on_arm_and_without_a_distribution_name(self):
        self.pretend(system="Linux", machine="aarch64", os_release=None)
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("ARM64", result["detail"])
        self.assertTrue(result["detail"].startswith("Linux 6.8.0-test"))

    def test_unusual_processors_and_systems_only_warn(self):
        for kwargs in ({"system": "Linux", "machine": "riscv64"}, {"system": "FreeBSD", "machine": "amd64"}):
            with self.subTest(kwargs=kwargs), ExitStack() as stack:
                pretend_computer(stack, **kwargs)
                result = self.run_check()
                self.assertEqual(result["status"], "warn")

    def test_windows_fails_plainly(self):
        self.pretend(system="Windows", machine="AMD64")
        result = self.run_check()
        self.assertEqual(result["status"], "fail")
        self.assertIn("Windows", result["detail"])
        self.assertIn("Mac or a Linux", result["fix"])

    def test_mac_with_apple_silicon_is_detected_and_not_called_verified(self):
        table = self.pretend(system="Darwin", machine="arm64", mac_version="14.5", programs=("sysctl",))
        result = self.run_check()
        self.assertEqual(result["status"], "warn", "a Mac is only detected, so the row asks for a look instead of saying it is ready")
        self.assertIn("macOS 14.5", result["detail"])
        self.assertIn("Apple silicon", result["detail"])
        self.assertIn("not been verified", result["detail"])
        self.assertIn("Nothing here needs fixing", result["hint"])
        self.assertIn("not been checked on real Macs", result["hint"])
        self.assertEqual(table.calls, [], "native Apple silicon needs no extra command")

    def test_intel_mac_is_told_apart_from_rosetta(self):
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "sysctl: unknown oid 'sysctl.proc_translated'\n", exit_code=1)
        self.pretend(system="Darwin", machine="x86_64", mac_version="12.7", programs=("sysctl",), table=table)
        result = self.run_check()
        self.assertEqual(result["status"], "warn", "a Mac is only detected, so the row asks for a look instead of saying it is ready")
        self.assertIn("Intel Mac", result["detail"])
        self.assertIn("not been verified", result["detail"])
        self.assertNotIn("Rosetta", result["detail"])

    def test_rosetta_on_apple_silicon_warns_and_explains(self):
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n")
        self.pretend(system="Darwin", machine="x86_64", mac_version="13.6", programs=("sysctl",), table=table)
        result = self.run_check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("Rosetta", result["detail"])
        self.assertIn("Apple silicon", result["detail"])
        self.assertIn("not been verified", result["detail"], "this Mac row says the same as every other Mac row")
        self.assertIn("universal2", result["fix"])
        self.assertEqual(table.calls, [[ "/usr/bin/sysctl", "-n", "sysctl.proc_translated"]])

    def test_the_rosetta_advice_does_not_promise_what_was_not_tried(self):
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n")
        self.pretend(system="Darwin", machine="x86_64", mac_version="13.6", programs=("sysctl",), table=table)
        hint = self.run_check()["hint"]
        self.assertIn("should still work", hint)
        self.assertNotIn("It works", hint)

    def test_every_mac_row_that_says_support_is_unverified_asks_for_a_look(self):
        """A row that says 'not verified' must not be 'ok' (the page would call it 'Ready'), so no Mac can read as fully ready."""
        for machine, table in (("arm64", ToolTable()),
                               ("x86_64", ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "sysctl: unknown oid 'sysctl.proc_translated'", exit_code=1)),
                               ("x86_64", ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n"))):
            with self.subTest(machine=machine, table=sorted(table.answers)), ExitStack() as stack:
                pretend_computer(stack, system="Darwin", machine=machine, mac_version="14.5", programs=("sysctl", "system_profiler"), table=table)
                for function in (checks.check_system, checks.check_accelerator):
                    result = self.well_formed(function(Settings()), function.__name__[len("check_"):])
                    if "not been verified" in result["detail"]:
                        self.assertEqual(result["status"], "warn", result)

    def test_a_mac_started_with_a_short_search_path_still_finds_its_own_tools(self):
        system = self.dir / "usr-sbin"
        system.mkdir()
        tool = system / "sysctl"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n")
        self.pretend(system="Darwin", machine="x86_64", mac_version="13.6", programs=(), table=table, system_dirs=[str(system)])
        self.assertEqual(self.run_check()["status"], "warn")
        self.assertEqual(table.calls, [[str(tool), "-n", "sysctl.proc_translated"]])
        with ExitStack() as stack:  # on Linux the fixed Mac folders are not consulted at all
            pretend_computer(stack, system="Linux", programs=(), system_dirs=[str(system)])
            self.assertIsNone(checks._system_tool("sysctl"))
        with ExitStack() as stack, patch("os.path.isfile", side_effect=OSError("odd")):
            pretend_computer(stack, system="Darwin", programs=(), system_dirs=[str(system)])
            self.assertIsNone(checks._system_tool("sysctl"))

    def test_on_a_mac_the_systems_own_program_wins_over_one_with_the_same_name_on_the_search_path(self):
        system = self.dir / "usr-sbin"
        system.mkdir()
        genuine = system / "sysctl"
        genuine.write_text("#!/bin/sh\n")
        genuine.chmod(0o755)
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n")
        # "/usr/bin/sysctl" (and system_profiler) is what the search path offers: stand-ins for a look-alike earlier on the path
        self.pretend(system="Darwin", machine="x86_64", mac_version="13.6", programs=("sysctl", "system_profiler"), table=table, system_dirs=[str(system)])
        self.assertEqual(checks._system_tool("sysctl"), str(genuine), "the fixed system folder is used first")
        self.assertEqual(self.run_check()["status"], "warn")
        self.assertEqual(table.calls, [[str(genuine), "-n", "sysctl.proc_translated"]])
        self.assertEqual(checks._system_tool("system_profiler"), "/usr/bin/system_profiler", "not in a fixed folder: the search path is the fallback")
        with patch("platform.system", side_effect=RuntimeError("no")):
            self.assertEqual(checks._system_tool("sysctl"), "/usr/bin/sysctl", "a computer that will not say what it is just uses the search path")


    def test_a_mac_that_will_not_say_is_unknown_not_guessed(self):
        self.pretend(system="Darwin", machine="x86_64", mac_version="13.6", programs=())
        self.assertEqual(self.run_check()["status"], "unknown")
        table = ToolTable().add_hang("sysctl", ["-n", "sysctl.proc_translated"])
        with ExitStack() as stack:
            pretend_computer(stack, system="Darwin", machine="x86_64", mac_version="13.6", programs=("sysctl",), table=table)
            self.assertEqual(self.run_check()["status"], "unknown")
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "banana\n")
        with ExitStack() as stack:
            pretend_computer(stack, system="Darwin", machine="x86_64", mac_version="13.6", programs=("sysctl",), table=table)
            self.assertEqual(self.run_check()["status"], "unknown")


# ---------------------------------------------------------------------- timezone data

class TimezoneDataCheck(CheckCase):
    @unittest.skipUnless(HAS_TIME_ZONE_DATA, "this computer has no timezone information")
    def test_real_data_is_ok(self):
        result = self.well_formed(checks.check_tzdata(), "tzdata")
        self.assertEqual(result["status"], "ok")

    def test_no_data_anywhere_warns_with_the_right_fix(self):
        with patch("zoneinfo.ZoneInfo", side_effect=zoneinfo.ZoneInfoNotFoundError("nothing")):
            for kwargs, expected in (({"system": "Linux", "os_release": UBUNTU}, "sudo apt install tzdata"),
                                     ({"system": "Linux", "os_release": ARCH}, "sudo pacman -S tzdata"),
                                     ({"system": "Linux", "os_release": FEDORA}, "-m pip install tzdata"),
                                     ({"system": "Darwin", "machine": "arm64"}, "-m pip install tzdata")):
                with self.subTest(kwargs=kwargs), ExitStack() as stack:
                    pretend_computer(stack, **kwargs)
                    result = self.well_formed(checks.check_tzdata(), "tzdata")
                    self.assertEqual(result["status"], "warn")
                    self.assertIn("No timezone information", result["detail"])
                    self.assertIn(expected, result["fix"])

    def test_the_pip_line_uses_the_running_python_and_is_quoted(self):
        with patch("zoneinfo.ZoneInfo", side_effect=zoneinfo.ZoneInfoNotFoundError("nothing")), patch.object(sys, "executable", "/opt/my python/bin/python3"):
            self.pretend(system="Darwin", machine="arm64")
            result = checks.check_tzdata()
        self.assertEqual(shlex.split(result["fix"].split("run: ", 1)[1]), ["/opt/my python/bin/python3", "-m", "pip", "install", "tzdata"])

    def test_some_zones_missing_names_them(self):
        def load(name):
            if name == "Asia/Tokyo":
                raise zoneinfo.ZoneInfoNotFoundError(name)
            return dt.timezone.utc

        with patch("zoneinfo.ZoneInfo", side_effect=load):
            result = self.well_formed(checks.check_tzdata(), "tzdata")
        self.assertEqual(result["status"], "warn")
        self.assertIn("Asia/Tokyo", result["detail"])

    def test_data_without_summer_time_is_not_trusted(self):
        with patch("zoneinfo.ZoneInfo", return_value=dt.timezone.utc):
            result = self.well_formed(checks.check_tzdata(), "tzdata")
        self.assertEqual(result["status"], "warn")
        self.assertIn("summer and winter", result["detail"])

    def test_data_that_breaks_when_used_and_a_missing_module(self):
        class Exploding(dt.tzinfo):
            def utcoffset(self, when):
                raise ValueError("corrupt")

        with patch("zoneinfo.ZoneInfo", return_value=Exploding()):
            self.assertEqual(self.well_formed(checks.check_tzdata(), "tzdata")["status"], "warn")
        with patch.dict(sys.modules, {"zoneinfo": None}):
            result = self.well_formed(checks.check_tzdata(), "tzdata")
        self.assertEqual(result["status"], "warn")
        self.assertIn("no timezone support", result["detail"])


# ---------------------------------------------------------------------- FFmpeg

class FFmpegCheck(CheckCase):
    def run_check(self, **settings):
        return self.well_formed(checks.check_ffmpeg(Settings(**settings)), "ffmpeg")

    def test_a_complete_install_is_ok_and_everything_runs_as_an_argument_list(self):
        table = self.pretend(programs=("ffmpeg", "ffprobe"), table=ffmpeg_table())
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hint"], "")
        for text in ("6.1.1-3ubuntu5", "MP3", "going live", "ffprobe 6.1.1"):
            self.assertIn(text, result["detail"])
        self.assertEqual(sorted(call[1:] for call in table.calls),
                         sorted([["-version"], ["-version"], ["-hide_banner", "-encoders"], ["-hide_banner", "-decoders"]]))
        self.assertTrue(all(isinstance(call, list) and call[0].startswith("/usr/bin/") for call in table.calls))

    def test_missing_ffmpeg_is_a_warning_with_the_right_install_line(self):
        for kwargs, expected in (({"system": "Darwin", "machine": "arm64"}, "brew install ffmpeg"),
                                 ({"system": "Linux", "os_release": UBUNTU}, "sudo apt install ffmpeg"),
                                 ({"system": "Linux", "os_release": ARCH}, "sudo pacman -S ffmpeg"),
                                 ({"system": "Linux", "os_release": FEDORA}, "ffmpeg.org/download")):
            with self.subTest(kwargs=kwargs), ExitStack() as stack:
                pretend_computer(stack, programs=(), **kwargs)
                result = self.run_check()
                self.assertEqual(result["status"], "warn")
                self.assertIn("not found", result["detail"])
                self.assertIn("WAV", result["hint"])
                self.assertIn(expected, result["fix"])

    def test_installed_in_a_usual_place_but_not_on_the_search_path(self):
        shelf = self.dir / "shelf"
        shelf.mkdir()
        program = shelf / "ffmpeg"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        self.pretend(programs=(), known_dirs=[str(shelf)])
        result = self.run_check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("not on this computer's program search path", result["detail"])
        self.assertIn("export PATH=", result["fix"])
        self.assertEqual(shlex.split(result["fix"].split("export PATH=", 1)[1])[0], f"{shelf}:$PATH")

    def test_a_file_named_ffmpeg_that_cannot_run_is_not_treated_as_installed(self):
        shelf = self.dir / "shelf"
        shelf.mkdir()
        (shelf / "ffmpeg").write_text("data")
        (shelf / "ffmpeg").chmod(0o644)
        self.pretend(programs=(), known_dirs=[str(shelf)])
        self.assertIn("not found", self.run_check()["detail"])

    def test_ffmpeg_without_an_mp3_encoder_says_going_live_will_not_work(self):
        self.pretend(programs=("ffmpeg", "ffprobe"), table=ffmpeg_table(encoders=["aac", "flac", "pcm_s16le", "libshine"]))
        result = self.run_check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("cannot make MP3 audio", result["detail"])
        self.assertIn("going live", result["detail"])
        self.assertIn("LAME", result["hint"])
        self.assertTrue(result["fix"])

    def test_missing_import_formats_are_named(self):
        decoders = [name for name in GOOD_DECODERS if name not in ("flac", "vorbis")]
        self.pretend(programs=("ffmpeg",), table=ffmpeg_table(decoders=decoders))
        result = self.run_check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("cannot read FLAC, Ogg files", result["detail"])
        self.assertNotIn("cannot make MP3", result["detail"])

    def test_each_accepted_decoder_alternative_counts(self):
        for decoders in (["mp3", "flac", "vorbis", "aac", "pcm_s16be"], ["mp3float", "flac", "libvorbis", "aac_fixed", "pcm_s16be"],
                         ["mp3", "flac", "vorbis", "libfdk_aac", "pcm_s16be"]):
            with self.subTest(decoders=decoders), ExitStack() as stack:
                pretend_computer(stack, programs=("ffmpeg",), table=ffmpeg_table(decoders=decoders))
                self.assertEqual(self.run_check()["status"], "ok")
        with ExitStack() as stack:
            pretend_computer(stack, programs=("ffmpeg",), table=ffmpeg_table(decoders=["flac", "vorbis", "aac", "pcm_s16be"]))
            self.assertIn("cannot read MP3", self.run_check()["detail"])

    def test_both_problems_are_reported_together(self):
        self.pretend(programs=("ffmpeg",), table=ffmpeg_table(encoders=["pcm_s16le"], decoders=["pcm_s16le"]))
        detail = self.run_check()["detail"]
        self.assertIn("cannot make MP3 audio", detail)
        self.assertIn("cannot read MP3, FLAC, Ogg, AAC and M4A, AIFF files", detail)

    def test_ffprobe_missing_or_broken_is_a_note_not_a_problem(self):
        self.pretend(programs=("ffmpeg",), table=ffmpeg_table(probe=False))
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("ffprobe was not found", result["detail"])
        with ExitStack() as stack:
            table = ffmpeg_table(probe=False).add("ffprobe", ["-version"], "garbage", exit_code=2)
            pretend_computer(stack, programs=("ffmpeg", "ffprobe"), table=table)
            result = self.run_check()
            self.assertEqual(result["status"], "ok")
            self.assertIn("did not answer", result["detail"])

    def test_a_program_that_is_not_ffmpeg_is_not_trusted(self):
        for output, code in (("Hello world\n", 0), ("", 0), (banner("ffmpeg"), 1), ("fftool version 1.0\n", 0)):
            with self.subTest(output=output[:12], code=code), ExitStack() as stack:
                pretend_computer(stack, programs=("ffmpeg",), table=ToolTable().add("ffmpeg", ["-version"], output, exit_code=code))
                result = self.run_check()
                self.assertEqual(result["status"], "warn")
                self.assertIn("did not answer like FFmpeg", result["detail"])

    def test_a_notice_printed_before_the_banner_does_not_make_a_working_ffmpeg_look_wrong(self):
        for before in ("WARNING: a library is old\n", "\n\n", "  \t\n", "first line\nsecond line\n", "x" * 5000 + "\n", "loading...\r\n"):
            with self.subTest(before=before[:20]), ExitStack() as stack:
                pretend_computer(stack, programs=("ffmpeg", "ffprobe"), table=ffmpeg_table(version=before + banner("ffmpeg")))
                result = self.run_check()
                self.assertEqual(result["status"], "ok", result)
                self.assertIn("6.1.1-3ubuntu5", result["detail"])

    def test_only_a_line_that_starts_with_the_name_counts_as_the_banner(self):
        for output in ("this is not ffmpeg version 6.1\n", "see ffmpeg version 6.1\n", "notffmpeg version 6.1\n", "ffmpeg versions 6.1\n"):
            with self.subTest(output=output[:24]), ExitStack() as stack:
                pretend_computer(stack, programs=("ffmpeg",), table=ToolTable().add("ffmpeg", ["-version"], output))
                self.assertIn("did not answer like FFmpeg", self.run_check()["detail"])
        with ExitStack() as stack:
            pretend_computer(stack, programs=("ffmpeg",), table=ffmpeg_table(version="   ffmpeg version 6.1 indented\n"))
            self.assertEqual(self.run_check()["status"], "ok")

    def test_looking_for_the_banner_stays_fast_on_a_flood_of_blank_lines(self):
        started = time.monotonic()
        for flood in ("\n" * 250000 + "x", " \n" * 120000 + "x", "\t" * 250000 + "x", ("ffmpeg version" + " " * 60) * 3000):
            checks._tool_version(flood, "ffmpeg")
        self.assertLess(time.monotonic() - started, 2.0, "searching for the banner must not slow down on odd output")
        self.assertEqual(checks._tool_version("\n" * 250000 + "ffmpeg version 7.0\n", "ffmpeg"), None, "only the first 64 KiB is searched")
        self.assertEqual(checks._tool_version("noise\nffprobe version 7.0\n", "ffprobe"), "7.0")

    def test_the_success_message_says_what_was_read_from_ffmpeg_and_not_what_was_tried(self):
        self.pretend(programs=("ffmpeg", "ffprobe"), table=ffmpeg_table())
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("Its own list says it can read MP3, FLAC, Ogg, AAC/M4A and AIFF files", result["detail"])
        self.assertIn("No real file was tried", result["detail"])
        self.assertNotIn("It can read", result["detail"])

    def test_a_program_that_cannot_start(self):
        self.pretend(programs=("ffmpeg",), table=ToolTable())  # every call comes back "missing"
        result = self.run_check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("could not be started", result["detail"])

    def test_a_hung_ffmpeg_is_unknown_not_a_failure(self):
        table = ffmpeg_table().add_hang("ffmpeg", ["-version"])
        self.pretend(programs=("ffmpeg", "ffprobe"), table=table)
        result = self.run_check(tool_timeout=3)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("did not answer within 3 seconds", result["detail"])
        self.assertTrue(result["hint"])

    def test_a_listing_that_was_cut_short_cannot_prove_anything_missing(self):
        table = ffmpeg_table()
        table.answers[("ffmpeg", ("-hide_banner", "-encoders"))] = ToolResult(ran=True, exit_code=0, output=listing("Encoders", ["pcm_s16le"]), truncated=True)
        self.pretend(programs=("ffmpeg",), table=table)
        result = self.run_check()
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("cannot make MP3", result["detail"])

    def test_a_failed_listing_is_unknown_but_a_known_problem_still_warns(self):
        table = ffmpeg_table(decoders=["pcm_s16le"])
        table.add("ffmpeg", ["-hide_banner", "-encoders"], "boom", exit_code=1)
        self.pretend(programs=("ffmpeg",), table=table)
        result = self.run_check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("could not be listed", result["detail"])

    def test_cancelled_while_waiting(self):
        table = ffmpeg_table()
        table.answers[("ffmpeg", ("-version",))] = ToolResult(ran=True, cancelled=True)
        self.pretend(programs=("ffmpeg",), table=table)
        self.assertEqual(self.run_check()["status"], "unknown")

    def test_odd_characters_in_the_version_and_path_are_cleaned(self):
        table = ffmpeg_table(version="ffmpeg version 6.1\x1b[2J\u202e Copyright\n")
        self.pretend(programs=("ffmpeg",), table=table)
        result = self.run_check()
        for char in ("\x1b", "\u202e"):
            self.assertNotIn(char, result["detail"])
        self.assertIn("6.1?[2J?", result["detail"])
        with patch("shutil.which", side_effect=lambda name, *a, **k: "/opt/ff\x1b[31mmpeg" if name == "ffmpeg" else None):
            result = self.run_check()  # the made-up path is not in the table, so it "could not be started"
        self.assertNotIn("\x1b", result["detail"])
        self.assertIn("/opt/ff?[31mmpeg", result["detail"])

    def test_the_paths_shown_use_a_tilde_for_the_home_folder(self):
        table = ToolTable().add("ffmpeg", ["-version"], "this is not ffmpeg")  # makes the check mention the path
        self.pretend(programs=("ffmpeg",), table=table)
        with patch("shutil.which", side_effect=lambda name, *a, **k: "/home/test-user/bin/ffmpeg" if name == "ffmpeg" else None):
            result = checks.check_ffmpeg(Settings())
        self.assertIn("~/bin/ffmpeg", result["detail"])
        self.assertNotIn("/home/test-user", result["detail"])


class FFmpegWithRealPrograms(CheckCase):
    """The same check through the real tool runner, using small fake executables (not FFmpeg)."""

    def test_end_to_end_with_listings_from_a_program(self):
        shelf = self.dir / "bin"
        shelf.mkdir()
        program = make_fake_ffmpeg(shelf)
        with patch("shutil.which", side_effect=lambda name, *a, **k: program if name == "ffmpeg" else None):
            result = checks.check_ffmpeg(Settings(tool_timeout=20))
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("6.1.1-3ubuntu5", result["detail"])

    def test_end_to_end_without_an_mp3_encoder(self):
        shelf = self.dir / "bin"
        shelf.mkdir()
        program = make_fake_ffmpeg(shelf, encoders=["aac", "pcm_s16le"])
        with patch("shutil.which", side_effect=lambda name, *a, **k: program if name == "ffmpeg" else None):
            result = checks.check_ffmpeg(Settings(tool_timeout=20))
        self.assertEqual(result["status"], "warn")
        self.assertIn("cannot make MP3 audio", result["detail"])

    def test_a_program_that_never_answers_ends_the_check_at_the_deadline(self):
        shelf = self.dir / "bin"
        shelf.mkdir()
        beat = self.dir / "beat"
        program = make_program(shelf, "ffmpeg", f"import sys\nsys.argv = ['x', {str(beat)!r}]\n" + HEARTBEAT)
        started = time.monotonic()
        with patch("shutil.which", side_effect=lambda name, *a, **k: program if name == "ffmpeg" else None):
            result = checks.check_ffmpeg(Settings(tool_timeout=1.0))
        took = time.monotonic() - started
        self.assertEqual(result["status"], "unknown")
        self.assertIn("did not answer", result["detail"])
        self.assertLess(took, 5.0, "all the calls wait at the same time, so one timeout is all it costs")
        self.assertFalse(alive_after(beat), "the hung program must have been stopped")


class ParsersNeverRaise(CheckCase):
    """Random and hostile text through every place the checks read text from other programs and files."""

    def test_random_text_never_raises_and_always_gives_a_well_formed_answer(self):
        import random
        rng = random.Random(20261105)
        alphabet = "abcXYZ 0123456789_.-:/=()[]{}\"'\\\n\t\x00\x1b\u202e\ufffd\U0001f3b5"
        bits = ["ffmpeg version ", "ffprobe version ", " A....D ", "libmp3lame", "Encoders:", " ------", "SPDisplaysDataType", "sppci_model",
                "MemTotal:", "kB", "{", "}", "[", "]", "null", "1", "-1", "99999999999999999999", "\n", "unknown oid"]

        def noise():
            return "".join(rng.choice(bits) if rng.random() < 0.3 else rng.choice(alphabet) for _ in range(rng.randint(0, 200)))

        for round_number in range(150):
            text = noise()
            parse_codec_list(text)
            checks._parse_profiler(text)
            checks._tool_version(text, "ffmpeg")
            table = ToolTable()
            table.add("ffmpeg", ["-version"], text if round_number % 2 else banner("ffmpeg"))
            table.add("ffmpeg", ["-hide_banner", "-encoders"], noise())
            table.add("ffmpeg", ["-hide_banner", "-decoders"], noise())
            table.add("ffprobe", ["-version"], noise())
            table.add("system_profiler", ["-json", "SPDisplaysDataType"], text)
            table.add("sysctl", ["-n", "sysctl.proc_translated"], text, exit_code=round_number % 3)
            table.add("sysctl", ["-n", "hw.memsize"], text)
            with ExitStack() as stack:
                pretend_computer(stack, system=rng.choice(["Linux", "Darwin"]), machine=rng.choice(["arm64", "x86_64"]), programs=("ffmpeg", "ffprobe", "system_profiler", "sysctl"), table=table)
                for spec in (checks.check_ffmpeg, checks.check_accelerator, checks.check_system, checks.check_memory):
                    result = spec(Settings(proc_root=str(self.dir / "none")))
                    self.assertEqual(list(result), KEYS)
                    self.assertIn(result["status"], STATES)

    def test_odd_files_in_the_system_folders_never_raise(self):
        roots = make_roots(self.dir / "odd")
        meminfo = Path(roots["proc_root"]) / "meminfo"
        for content in (b"", b"\xff\xfe\x00garbage", b"MemTotal: -5 kB\n", b"MemTotal:\n", b"x" * 200000, b"MemTotal: 1 kB\nMemAvailable: 1 kB"):
            meminfo.write_bytes(content)
            for system in ("Linux", "Darwin"):
                with ExitStack() as stack, patch("os.sysconf", side_effect=ValueError("no")):
                    pretend_computer(stack, system=system)
                    self.assertIn(checks.check_memory(Settings(**roots))["status"], STATES)
        vendor = Path(roots["sys_root"]) / "class" / "drm" / "renderD128" / "device" / "vendor"
        for content in (b"", b"\xff" * 100, b"0xZZZZ", b"0x8086" * 1000):
            vendor.write_bytes(content)
            with ExitStack() as stack:
                pretend_computer(stack, system="Linux")
                self.assertEqual(checks.check_accelerator(Settings(**roots))["status"], "ok")
        zone = Path(roots["etc_root"])
        for target in ("", "zoneinfo/", "/zoneinfo/../../etc/passwd", "/usr/share/zoneinfo/" + "A/" * 500, "/usr/share/zoneinfo/\x1b[31m/x"):
            (zone / "localtime").unlink()
            os.symlink(target or "x", zone / "localtime")
            with patch.dict(os.environ):
                os.environ.pop("TZ", None)
                result = checks.check_clock(Settings(etc_root=str(zone), now=lambda: NOW))
            self.assertIn(result["status"], STATES)
            self.assertEqual(list(result), KEYS)
        (zone / "timezone").write_bytes(b"\xff\xfe" * 300)
        (zone / "localtime").unlink()
        with patch.dict(os.environ):
            os.environ.pop("TZ", None)
            self.assertIn(checks.check_clock(Settings(etc_root=str(zone), now=lambda: NOW))["status"], STATES)

    def test_a_folder_with_endless_entries_is_read_only_up_to_a_limit(self):
        many = self.dir / "dri"
        many.mkdir()
        for index in range(300):
            (many / f"entry{index:04d}").write_text("")
        self.assertEqual(len(checks._names(str(many), 64)), 64)
        self.assertEqual(len(checks._names(str(many), 1000)), 300)
        with self.assertRaises(OSError):
            checks._names(str(self.dir / "absent"), 5)


class SmallHelpers(CheckCase):
    def test_a_failing_tool_thread_is_reported_as_a_failed_call(self):
        with patch("myradio.doctor.checks.run_tool", side_effect=RuntimeError("never raised by the real one")):
            answers = checks._run_together(Settings(), {"x": ["a"], "y": ["b"]}, 1)
        self.assertEqual({name: answer.reason for name, answer in answers.items()}, {"x": "failed", "y": "failed"})

    def test_a_finder_that_trips_over_a_folder_just_moves_on(self):
        with patch("shutil.which", return_value=None), patch("os.path.isfile", side_effect=OSError("odd")):
            self.assertEqual(checks._find_tool("ffmpeg"), (None, None))

    def test_the_nearest_existing_folder_gives_up_at_the_top(self):
        with patch("os.stat", side_effect=FileNotFoundError()):
            self.assertEqual(checks._nearest_existing("/a/b/c"), "/")
        self.assertEqual(checks._nearest_existing(str(self.dir / "x" / "y")), str(self.dir))

    def test_reasons_are_plain_words_never_raw_error_text(self):
        self.assertEqual(checks._os_reason(PermissionError(errno.EACCES, "Permission denied: '/secret/path'")), "permission was denied")
        self.assertEqual(checks._os_reason(FileNotFoundError(errno.ENOENT, "x: '/secret/path'")), "it was not found")
        self.assertEqual(checks._os_reason(OSError(errno.ENOSPC, "No space left on device")), "no space left on device")
        self.assertEqual(checks._os_reason(OSError("no number at all")), "the computer reported a problem")
        self.assertEqual(checks._os_reason(OSError(99999, "unlisted")), "unknown error 99999")
        self.assertTrue(checks._out_of_resources(OSError(errno.EMFILE, "x")))
        self.assertFalse(checks._out_of_resources(OSError(errno.EACCES, "x")))
        self.assertFalse(checks._out_of_resources(ValueError("x")))

    def test_a_home_folder_that_cannot_be_found_changes_nothing(self):
        with patch("os.path.expanduser", side_effect=RuntimeError("no home")):
            self.assertFalse(checks._too_broad("/srv/station/data"))


class CodecListParsing(unittest.TestCase):
    def test_names_come_from_the_listing_and_the_legend_is_ignored(self):
        names = parse_codec_list(listing("Encoders", GOOD_ENCODERS))
        self.assertEqual(names, set(GOOD_ENCODERS) | {"h264"})
        self.assertNotIn("=", names)

    def test_garbage_and_odd_input_never_raises(self):
        for text in ("", "\x00\x01\x02", "Encoders:\n ------\n", "A....D\n", " A....D  \n", "x" * 100000, "\n" * 50000, " A....D " + "n" * 100000):
            with self.subTest(text=text[:10]):
                self.assertIsInstance(parse_codec_list(text), set)
        self.assertEqual(parse_codec_list(None), set())


# ---------------------------------------------------------------------- disk and folder

class DiskSpaceCheck(CheckCase):
    def run_check(self, free):
        with patch("shutil.disk_usage", return_value=usage(free)):
            return self.well_formed(checks.check_disk_space(Settings(data_dir=str(self.dir))), "disk-space")

    def test_plenty_of_space_is_ok_and_says_what_it_means(self):
        result = self.run_check(50 * GIB)
        self.assertEqual(result["status"], "ok")
        self.assertIn("50.0 GB free", result["detail"])
        self.assertRegex(result["detail"], r"room for about \d+ hours of CD-quality audio")

    def test_the_hours_of_music_that_fit_are_pinned_to_cd_quality(self):
        """44.1 kHz, 2 bytes a sample, 2 channels: 635,040,000 bytes an hour. A slip in any of the three would mislead people."""
        self.assertEqual(checks.AUDIO_BYTES_PER_HOUR, 44100 * 2 * 2 * 3600)
        self.assertEqual(checks.AUDIO_BYTES_PER_HOUR, 635_040_000)
        self.assertEqual(checks._audio_time(checks.AUDIO_BYTES_PER_HOUR), "about 1.0 hours")
        self.assertEqual(checks._audio_time(checks.AUDIO_BYTES_PER_HOUR // 2), "about 30 minutes")
        self.assertEqual(checks._audio_time(10 * checks.AUDIO_BYTES_PER_HOUR), "about 10 hours")
        self.assertIn("room for about 169 hours of CD-quality audio", self.run_check(100 * GIB)["detail"])
        self.assertIn("room for about 2.5 hours of CD-quality audio", self.run_check(int(1.5 * GIB))["detail"])

    def test_a_figure_just_under_a_limit_is_never_shown_as_the_limit(self):
        warn = self.run_check(2 * GIB - 1)
        self.assertEqual(warn["status"], "warn")
        self.assertIn("Only 1.9 GB free", warn["detail"])
        self.assertNotIn("2.0 GB", warn["detail"])
        fail = self.run_check(200 * MIB - 1)
        self.assertEqual(fail["status"], "fail")
        self.assertIn("Only 199 MB free", fail["detail"])
        self.assertIn("2.0 GB free", self.run_check(2 * GIB)["detail"])
        self.assertIn("Only 200 MB free", self.run_check(200 * MIB)["detail"])

    def test_sizes_are_rounded_down_in_plain_units(self):
        for count, expected in ((0, "0 MB"), (1, "0 MB"), (MIB, "1 MB"), (GIB - 1, "1023 MB"), (GIB, "1.0 GB"), (1536 * MIB, "1.5 GB"),
                                (2 * GIB - 1, "1.9 GB"), (10.5 * GIB, "10.5 GB"), (81.49 * GIB, "81.4 GB"), (16 * GIB, "16.0 GB")):
            with self.subTest(count=count):
                self.assertEqual(checks._size(count), expected)

    def test_the_warning_and_failure_thresholds_are_exact(self):
        cases = ((2 * GIB, "ok"), (2 * GIB - 1, "warn"), (200 * MIB, "warn"), (200 * MIB - 1, "fail"), (1, "fail"), (0, "fail"))
        for free, expected in cases:
            with self.subTest(free=free):
                self.assertEqual(self.run_check(free)["status"], expected)

    def test_low_space_wording_and_help(self):
        warn = self.run_check(int(1.5 * GIB))
        self.assertIn("Only 1.5 GB free", warn["detail"])
        self.assertIn("--data-dir", warn["hint"])
        fail = self.run_check(150 * MIB)
        self.assertIn("Only 150 MB free", fail["detail"])
        self.assertIn("could stop", fail["hint"])
        self.assertIn("--data-dir", fail["fix"])
        self.assertRegex(self.run_check(100 * MIB)["detail"], r"^Only 100 MB")

    def test_a_folder_that_does_not_exist_yet_is_measured_where_it_will_be_made(self):
        wanted = self.dir / "deep" / "er" / "station"
        with patch("shutil.disk_usage", return_value=usage(10 * GIB)) as measure:
            result = checks.check_disk_space(Settings(data_dir=str(wanted)))
        self.assertEqual(result["status"], "ok")
        measure.assert_called_once_with(str(self.dir))
        self.assertFalse(wanted.exists())

    def test_when_the_drive_will_not_say_it_is_unknown(self):
        for error in (OSError(errno.EIO, "boom"), PermissionError(), ValueError("x"), AttributeError("x"), TypeError("x")):
            with self.subTest(error=type(error).__name__), patch("shutil.disk_usage", side_effect=error):
                result = self.well_formed(checks.check_disk_space(Settings(data_dir=str(self.dir))), "disk-space")
                self.assertEqual(result["status"], "unknown")
        with patch("shutil.disk_usage", return_value=types.SimpleNamespace(free=-5)):
            self.assertEqual(checks.check_disk_space(Settings(data_dir=str(self.dir)))["status"], "unknown")
        with patch("shutil.disk_usage", return_value=types.SimpleNamespace()):
            self.assertEqual(checks.check_disk_space(Settings(data_dir=str(self.dir)))["status"], "unknown")

    def test_a_nonsense_folder_setting_is_unknown_not_a_crash(self):
        self.assertEqual(self.well_formed(checks.check_disk_space(Settings(data_dir=12345)), "disk-space")["status"], "unknown")

    def test_the_real_disk_gives_a_real_answer(self):
        result = self.well_formed(checks.check_disk_space(Settings(data_dir=str(self.dir))), "disk-space")
        self.assertIn(result["status"], ("ok", "warn", "fail"))
        self.assertIn("free", result["detail"])


class DataFolderCheck(CheckCase):
    def run_check(self, path, **extra):
        return self.well_formed(checks.check_data_folder(Settings(data_dir=str(path), **extra)), "data-folder")

    def test_a_private_folder_is_ok(self):
        result = self.run_check(self.station(0o700))
        self.assertEqual(result["status"], "ok")
        self.assertIn("closed to other accounts", result["detail"])
        self.assertEqual(result["fix"], "")

    def test_a_folder_other_accounts_can_open_fails_and_the_fix_is_quoted(self):
        for mode in (0o755, 0o751, 0o777, 0o707, 0o701, 0o775, 0o705):
            with self.subTest(mode=oct(mode)):
                path = self.station(mode, name=f"s{mode:o}")
                result = self.run_check(path)
                self.assertEqual(result["status"], "fail")
                self.assertIn("other accounts", result["detail"])
                self.assertEqual(shlex.split(result["fix"].split("run: ", 1)[1]), ["chmod", "700", str(path)])

    def test_group_access_alone_is_a_gentler_warning(self):
        for mode in (0o750, 0o770, 0o710, 0o740):
            with self.subTest(mode=oct(mode)):
                path = self.station(mode, name=f"g{mode:o}")
                result = self.run_check(path)
                self.assertEqual(result["status"], "warn")
                self.assertIn("people in its group", result["detail"])
                self.assertTrue(result["fix"])

    def test_a_path_with_awkward_characters_is_quoted_so_it_cannot_become_a_second_command(self):
        path = self.dir / "my radio; rm -rf $HOME `x` 'q'"
        path.mkdir()
        os.chmod(path, 0o755)
        fix = self.run_check(path)["fix"]
        self.assertEqual(shlex.split(fix.split("run: ", 1)[1]), ["chmod", "700", str(path)])

    def test_a_path_with_control_characters_never_appears_in_a_command(self):
        path = self.dir / "bad\nname\x1b[31m"
        path.mkdir()
        os.chmod(path, 0o755)
        result = self.run_check(path)
        self.assertEqual(result["status"], "fail")
        self.assertNotIn("chmod", result["fix"])
        self.assertIn("--data-dir", result["fix"])
        for key in KEYS:
            self.assertNotIn("\n", result[key])
            self.assertNotIn("\x1b", result[key])

    def test_broad_folders_are_never_told_to_lock_down(self):
        with patch.dict(os.environ, {"HOME": str(self.dir)}):
            for path in (self.dir, "/tmp", "/", "/home", self.dir.parent):
                with self.subTest(path=str(path)):
                    self.assertTrue(checks._too_broad(str(path)))
            self.assertFalse(checks._too_broad(str(self.dir / ".myradio")))
            os.chmod(self.dir, 0o755)
            result = self.run_check(self.dir)  # "home" here: a folder far too general to lock down
            self.assertEqual(result["status"], "fail")
            self.assertNotIn("chmod", result["fix"])
            self.assertIn("--data-dir", result["fix"])

    def test_folders_under_the_home_folder_are_written_with_a_tilde_in_the_fix(self):
        with patch.dict(os.environ, {"HOME": str(self.dir)}):
            path = self.dir / ".myradio"
            path.mkdir(mode=0o755)
            os.chmod(path, 0o755)
            result = self.run_check(path)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["fix"], "Open a terminal and run: chmod 700 ~/.myradio")
            spaced = self.dir / "My Station"
            spaced.mkdir()
            os.chmod(spaced, 0o755)
            self.assertEqual(self.run_check(spaced)["fix"], "Open a terminal and run: chmod 700 ~/'My Station'")
            self.assertIn("~/.myradio", result["detail"])
            self.assertNotIn(str(self.dir), result["detail"])

    def test_group_access_on_a_broad_folder_gets_the_general_advice(self):
        with patch.dict(os.environ, {"HOME": str(self.dir)}):
            os.chmod(self.dir, 0o770)
            result = self.run_check(self.dir)
        self.assertEqual(result["status"], "warn")
        self.assertIn("--data-dir", result["fix"])
        self.assertNotIn("chmod", result["fix"])

    def test_nonsense_folder_settings_are_unknown_not_a_crash(self):
        self.assertEqual(self.well_formed(checks.check_data_folder(Settings(data_dir=12345)), "data-folder")["status"], "unknown")

    @unittest.skipIf(IS_ROOT, "root can look anywhere")
    def test_a_place_that_cannot_even_be_looked_at_fails_plainly_when_it_would_have_to_be_made(self):
        closed = self.station(0o000, name="closed2")
        self.addCleanup(os.chmod, closed, 0o700)
        with patch("myradio.doctor.checks._nearest_existing", return_value=str(closed / "inside")):
            result = self.run_check(self.dir / "newplace")
        self.assertEqual(result["status"], "fail")
        self.assertIn("cannot be opened", result["detail"])

    def test_a_folder_that_does_not_exist_yet_is_fine_when_it_can_be_made(self):
        wanted = self.dir / "later"
        result = self.run_check(wanted)
        self.assertEqual(result["status"], "ok")
        self.assertIn("does not exist yet", result["detail"])
        self.assertFalse(wanted.exists(), "the doctor must not create it")

    @unittest.skipIf(IS_ROOT, "root can write anywhere")
    def test_a_folder_that_cannot_be_made_fails(self):
        parent = self.station(0o500, name="locked")
        self.addCleanup(os.chmod, parent, 0o700)
        result = self.run_check(parent / "inside")
        self.assertEqual(result["status"], "fail")
        self.assertIn("would not be allowed to create", result["detail"])

    @unittest.skipIf(IS_ROOT, "root can read anywhere")
    def test_a_folder_behind_a_closed_door_fails_plainly(self):
        parent = self.station(0o000, name="closed")
        self.addCleanup(os.chmod, parent, 0o700)
        result = self.run_check(parent / "inside")
        self.assertEqual(result["status"], "fail")
        self.assertIn("not allowed to look", result["detail"])

    def test_a_file_where_the_folder_should_be(self):
        plain = self.dir / "plain"
        plain.write_text("x")
        result = self.run_check(plain)
        self.assertEqual(result["status"], "fail")
        self.assertIn("is a file, not a folder", result["detail"])
        result = self.run_check(plain / "inside")
        self.assertEqual(result["status"], "fail")
        self.assertIn("is a file", result["detail"])

    def test_a_folder_myradio_cannot_use_is_explained_without_advice_to_take_it_over(self):
        path = self.station()
        with patch("os.access", return_value=False):
            result = self.run_check(path)
        self.assertEqual(result["status"], "fail")
        self.assertIn("cannot read and save", result["detail"])
        self.assertIn("belongs to another account", result["hint"])
        self.assertNotIn("sudo", result["fix"])
        self.assertIn("--data-dir", result["fix"])

    def test_a_folder_left_behind_by_an_administrator_can_be_taken_back(self):
        path = self.station()
        with patch("os.access", return_value=False), patch("myradio.doctor.checks._belongs_to_administrator", return_value=True), \
                patch.dict(os.environ, {"HOME": str(self.dir)}):  # the station folder is inside the person's own home folder
            result = self.run_check(path)
        self.assertEqual(result["status"], "fail")
        self.assertIn("administrator", result["hint"])
        self.assertEqual(shlex.split(result["fix"].split("running: ", 1)[1])[:3], ["sudo", "chown", "-R"])
        self.assertEqual(shlex.split(result["fix"].split("running: ", 1)[1])[3:], ["$USER", "~/" + path.name], "inside the home folder it is written with ~")
        self.assertEqual(result["fix"], f"Take the folder back by running: sudo chown -R \"$USER\" ~/{path.name}")
        with patch("os.access", return_value=False), patch("myradio.doctor.checks._belongs_to_administrator", return_value=True), \
                patch.dict(os.environ, {"HOME": str(path)}):
            broad = self.run_check(path)  # the folder is far too general to take over
        self.assertNotIn("sudo", broad["fix"])

    def test_only_a_folder_owned_by_root_counts_as_the_administrators(self):
        root_owned, mine = types.SimpleNamespace(st_uid=0), types.SimpleNamespace(st_uid=os.getuid())
        with patch("os.geteuid", return_value=1000):
            self.assertTrue(checks._belongs_to_administrator(root_owned))
            self.assertFalse(checks._belongs_to_administrator(mine))
        with patch("os.geteuid", return_value=0):
            self.assertFalse(checks._belongs_to_administrator(root_owned), "root itself should not be told to take it back")
        with patch("os.geteuid", side_effect=AttributeError("not on this system")):
            self.assertFalse(checks._belongs_to_administrator(root_owned))
        self.assertFalse(checks._belongs_to_administrator(object()))

    @contextmanager
    def fake_folder(self, wanted, uid=0, mode=0o755):
        """For the length of the block, ``wanted`` (a path that need not exist) looks like a folder with this owner and mode
        that this account may not use. The real file system is not touched."""
        real_stat = os.stat

        def fake_stat(target, *args, **kwargs):
            if str(target) == wanted:
                return types.SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid)
            return real_stat(target, *args, **kwargs)

        with patch("os.stat", fake_stat), patch("os.access", return_value=False), patch("os.geteuid", return_value=1000):
            yield

    def test_a_system_folder_is_never_told_to_be_taken_over(self):
        """'sudo chown -R' on /usr/local or /var/lib would change a system tree, so only a folder inside the person's own home gets that advice."""
        for wanted in ("/usr/local", "/var/lib", "/etc/ssl", "/usr/share/applications", "/opt/radio"):
            with self.subTest(folder=wanted), self.fake_folder(wanted):
                result = self.run_check(wanted)
                self.assertEqual(result["status"], "fail")
                self.assertNotIn("sudo", result["fix"])
                self.assertNotIn("chown", result["fix"])
                self.assertIn("a folder of your own", result["fix"])
                self.assertNotIn("administrator", result["hint"])

    def test_the_take_back_advice_is_for_a_folder_inside_the_home_folder_and_only_when_it_fits(self):
        home = "/home/test-user"
        with self.fake_folder(f"{home}/.myradio"):
            result = self.run_check(f"{home}/.myradio")
        self.assertEqual(result["fix"], "Take the folder back by running: sudo chown -R \"$USER\" ~/.myradio")
        self.assertIn("administrator", result["hint"])
        long_folder = f"{home}/" + "a" * 200 + "/" + "b" * 200
        with self.fake_folder(long_folder):
            result = self.run_check(long_folder)
        self.assertNotIn("sudo", result["fix"], "a command that would be cut short is not shown")
        self.assertFalse(result["fix"].endswith("..."))
        for broad in (home, "/home", "/"):  # the home folder itself and anything above it
            with self.subTest(folder=broad), self.fake_folder(broad):
                self.assertNotIn("sudo", self.run_check(broad)["fix"])

    def long_station(self, length, filler="a", mode=0o755):
        """A real folder whose absolute path is exactly ``length`` characters long (three names made of ``filler``)."""
        base = str(self.dir)
        room = length - len(base) - 3  # three separators
        first = min(200, room // 3)
        second = min(200, (room - first) // 2)
        pieces = [(filler * n)[:n - 1] + "z" for n in (first, second, room - first - second)]
        path = os.path.join(base, *pieces)
        self.assertEqual(len(path), length)
        os.makedirs(path)
        os.chmod(path, mode)
        return path

    def test_a_command_is_shown_whole_or_not_at_all(self):
        """A command cut in the middle of its quoted path leaves a terminal waiting for the closing quote."""
        prefix = "Open a terminal and run: chmod 700 "
        limit = checks.MAX_TEXT - len(prefix)
        fits = self.long_station(limit)
        result = self.run_check(fits)
        self.assertEqual(len(result["fix"]), checks.MAX_TEXT, "the longest command that fits is shown in full")
        self.assertEqual(shlex.split(result["fix"].split("run: ", 1)[1]), ["chmod", "700", fits])
        for length in (limit + 1, 450, 476):
            with self.subTest(length=length):
                result = self.run_check(self.long_station(length, "b"))
                self.assertEqual(result["status"], "fail")
                self.assertFalse(result["fix"].endswith("..."), "a command must never be cut short")
                shlex.split(result["fix"])  # no quote is left open
                self.assertNotIn("chmod", result["fix"])
                self.assertEqual(result["fix"], GENERAL_FIX)
        self.assertEqual(checks.GENERAL_FOLDER_FIX, GENERAL_FIX)

    def test_the_length_rule_counts_the_quotes_too(self):
        prefix = "Open a terminal and run: chmod 700 "
        limit = checks.MAX_TEXT - len(prefix) - 2  # a path with spaces is wrapped in two quote marks
        fits = self.long_station(limit, "a b")
        self.assertIn(" ", fits)
        result = self.run_check(fits)
        self.assertEqual(len(result["fix"]), checks.MAX_TEXT)
        self.assertEqual(shlex.split(result["fix"].split("run: ", 1)[1]), ["chmod", "700", fits])
        result = self.run_check(self.long_station(limit + 1, "a b"))
        self.assertFalse(result["fix"].endswith("..."), "a command must never be cut short")
        self.assertEqual(result["fix"], GENERAL_FIX)
        group = self.run_check(self.long_station(420, "g", mode=0o750))
        self.assertFalse(group["fix"].endswith("..."))
        self.assertEqual((group["status"], group["fix"]), ("warn", GENERAL_FIX), "the group warning obeys the same rule")
        limit_group = self.long_station(checks.MAX_TEXT - len(prefix), "h", mode=0o750)
        self.assertEqual(self.run_check(limit_group)["fix"], prefix + limit_group)

    def test_a_drive_that_cannot_keep_a_folder_private_is_explained(self):
        for mode, status in ((0o755, "fail"), (0o750, "warn")):
            with self.subTest(mode=oct(mode)):
                result = self.run_check(self.station(mode, name=f"d{mode:o}"))
                self.assertEqual(result["status"], status)
                self.assertIn("USB stick or a network drive", result["hint"])
                self.assertIn("this computer's own drive", result["hint"])
        self.assertNotIn("USB stick", self.run_check(self.station(0o700, name="private"))["hint"])

    def test_a_null_byte_or_odd_error_in_the_path(self):
        self.assertEqual(self.run_check("bad\0name")["status"], "fail")
        with patch("os.stat", side_effect=OSError(errno.EIO, "disk exploded")):
            result = self.run_check(self.dir)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("exploded", result["detail"])
        with patch("os.stat", side_effect=OSError(errno.ENAMETOOLONG, "File name too long")):
            self.assertEqual(self.run_check("x" * 5000)["status"], "unknown")

    def test_a_link_to_a_private_folder_says_so(self):
        real = self.station(0o700, name="real")
        link = self.dir / "link"
        os.symlink(real, link)
        result = self.run_check(link)
        self.assertEqual(result["status"], "ok")
        self.assertIn("a link to", result["detail"])

    def test_the_default_folder_matches_the_service(self):
        with patch.dict(os.environ, {"HOME": str(self.dir)}, clear=False):
            os.environ.pop("MYRADIO_DATA_DIR", None)
            self.assertEqual(checks.resolve_data_dir(Settings()), str(self.dir / ".myradio"))
            os.environ["MYRADIO_DATA_DIR"] = str(self.dir / "chosen")
            self.assertEqual(checks.resolve_data_dir(Settings()), str(self.dir / "chosen"))
            self.assertEqual(checks.resolve_data_dir(Settings(data_dir="~/mine")), str(self.dir / "mine"))
            self.assertEqual(checks.resolve_data_dir(Settings(data_dir=str(self.dir / "x" / ".." / "y"))), str(self.dir / "y"))

    def test_nothing_on_disk_is_changed_by_looking(self):
        path = self.station(0o755)
        (path / "station.sqlite3").write_text("data")
        (path / "control.token").write_text("secret")
        before = snapshot(self.dir)
        for _ in range(2):
            checks.check_data_folder(Settings(data_dir=str(path)))
            checks.check_disk_space(Settings(data_dir=str(path)))
            checks.check_data_folder(Settings(data_dir=str(self.dir / "absent" / "deeper")))
            checks.check_disk_space(Settings(data_dir=str(self.dir / "absent" / "deeper")))
        self.assertEqual(snapshot(self.dir), before)

    def test_the_station_files_are_never_read(self):
        path = self.station()
        (path / "control.token").write_text("super-secret-launch-code")
        results = [checks.check_data_folder(Settings(data_dir=str(path))), checks.check_disk_space(Settings(data_dir=str(path)))]
        self.assertNotIn("super-secret", repr(results))
        with patch("builtins.open", side_effect=AssertionError("opened a file")) as opened:
            checks.check_data_folder(Settings(data_dir=str(path)))
            checks.check_disk_space(Settings(data_dir=str(path)))
        opened.assert_not_called()


def snapshot(root):
    """Names, sizes, modes and times of everything under root: to prove a run changed nothing."""
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(base, name)
            info = os.lstat(full)
            out[os.path.relpath(full, root)] = (info.st_size, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    return out


# ---------------------------------------------------------------------- network

class LoopbackAndPortChecks(CheckCase):
    def test_this_computer_can_keep_its_controls_private(self):
        result = self.well_formed(checks.check_loopback(Settings()), "loopback")
        self.assertEqual(result["status"], "ok")
        self.assertIn("not shared on your network", result["detail"])

    def test_a_computer_that_refuses_fails_with_a_plain_explanation(self):
        with patch("socket.socket", side_effect=OSError(errno.EACCES, "Permission denied")):
            result = self.well_formed(checks.check_loopback(Settings()), "loopback")
        self.assertEqual(result["status"], "fail")
        self.assertIn("firewall", result["hint"])

    def test_running_out_of_files_is_unknown_not_a_failed_computer(self):
        for code in (errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.ENOBUFS):
            with self.subTest(code=code), patch("socket.socket", side_effect=OSError(code, "no room")):
                result = self.well_formed(checks.check_loopback(Settings()), "loopback")
                self.assertEqual(result["status"], "unknown")
        with patch("builtins.open", side_effect=OSError(errno.EMFILE, "Too many open files")):
            self.assertEqual(self.well_formed(checks.check_fcntl(), "fcntl")["status"], "unknown")

    def test_a_free_port_is_ok(self):
        with patch("myradio.doctor.checks._bind", return_value=DEFAULT_PORT) as bind:
            result = self.well_formed(checks.check_port(Settings(port=DEFAULT_PORT)), "port")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Port 8765 is free", result["detail"])
        bind.assert_called_once_with(DEFAULT_PORT)

    def test_a_busy_port_is_found_with_a_real_socket(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        result = self.well_formed(checks.check_port(Settings(port=port)), "port")
        self.assertEqual(result["status"], "warn")
        self.assertIn(f"Port {port} is already in use", result["detail"])
        self.assertIn(f"http://127.0.0.1:{port}/", result["hint"])
        self.assertIn(f"myradio --port {port + 1}", result["fix"])
        holder.close()

    def test_the_probe_does_not_keep_the_port(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.close()
        for _ in range(2):  # looking twice must not leave anything behind
            self.assertIn(checks.check_port(Settings(port=port))["status"], ("ok", "warn"))
        again = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(again.close)
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            again.bind(("127.0.0.1", port))
        except OSError:
            self.skipTest("another program took the port in the meantime")

    def test_port_zero_means_any_free_port(self):
        result = checks.check_port(Settings(port=0))
        self.assertEqual(result["status"], "ok")
        self.assertIn("any free port", result["detail"])

    def test_ports_below_1024_and_other_errors(self):
        with patch("myradio.doctor.checks._bind", side_effect=PermissionError(errno.EACCES, "denied")):
            result = self.well_formed(checks.check_port(Settings(port=80)), "port")
        self.assertEqual(result["status"], "warn")
        self.assertIn("--port 8765", result["fix"])
        with patch("myradio.doctor.checks._bind", side_effect=OSError(errno.EADDRNOTAVAIL, "no address")):
            result = self.well_formed(checks.check_port(Settings(port=9000)), "port")
        self.assertEqual(result["status"], "unknown")

    def test_a_refusal_on_an_ordinary_port_is_not_blamed_on_the_ports_below_1024(self):
        for port, other in ((DEFAULT_PORT, DEFAULT_PORT + 1), (1024, 1025), (65535, 65534)):
            with self.subTest(port=port), patch("myradio.doctor.checks._bind", side_effect=PermissionError(errno.EACCES, "denied")):
                result = self.well_formed(checks.check_port(Settings(port=port)), "port")
                self.assertEqual(result["status"], "warn")
                self.assertIn(f"does not allow myradio to use port {port}", result["detail"])
                self.assertNotIn("1024", result["hint"])
                self.assertIn("firewall or security program", result["hint"])
                self.assertIn(f"myradio --port {other}", result["fix"])
                self.assertNotIn(f"--port {port}", result["fix"], "the advice must not name the very port that was refused")
        for port in (80, 1023):
            with self.subTest(port=port), patch("myradio.doctor.checks._bind", side_effect=PermissionError(errno.EACCES, "denied")):
                result = self.well_formed(checks.check_port(Settings(port=port)), "port")
                self.assertIn("Ports below 1024 are reserved", result["hint"])
                self.assertIn(f"myradio --port {DEFAULT_PORT}", result["fix"])

    def test_the_tests_cannot_bind_a_fixed_port(self):
        """The guard every check test runs under: a fixed port is refused and never really bound; port 0 and ports the system handed out work."""
        with self.assertRaises(PermissionError):
            checks._bind(DEFAULT_PORT)
        self.assertEqual(self.ports.refused, [("127.0.0.1", DEFAULT_PORT)])
        result = checks.check_port(Settings(port=DEFAULT_PORT))  # the check sees a refusal, never a free port
        self.assertEqual(result["status"], "warn")
        self.assertEqual(len(self.ports.refused), 2)
        self.ports.refused.clear()  # both were expected
        self.assertIsInstance(checks._bind(0), int)
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        handed_out = holder.getsockname()[1]
        holder.close()
        try:
            checks._bind(handed_out)
        except OSError:
            pass  # another program took it in the meantime; the guard still did not refuse it
        self.assertEqual(self.ports.refused, [])

    def test_nonsense_ports_are_unknown(self):
        for port in (-1, 65536, 70000, True, "8765", None, 1.5):
            with self.subTest(port=port):
                self.assertEqual(self.well_formed(checks.check_port(Settings(port=port)), "port")["status"], "unknown")

    def test_inside_the_running_station_the_port_is_not_tested(self):
        with patch("myradio.doctor.checks._bind") as bind:
            result = self.well_formed(checks.check_port(Settings(port=DEFAULT_PORT, in_service=True)), "port")
        bind.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertIn("skipped", result["detail"])

    def test_the_probe_is_a_bind_on_this_computer_only_and_never_connects_or_listens(self):
        fake = MagicMock()
        fake.getsockname.return_value = ("127.0.0.1", 8765)
        with patch("socket.socket", return_value=fake):
            checks.check_port(Settings(port=8765))
        fake.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # the same as the service
        fake.bind.assert_called_once_with(("127.0.0.1", 8765))
        fake.close.assert_called_once()
        for name in ("listen", "accept", "connect", "connect_ex", "sendto", "send"):
            getattr(fake, name).assert_not_called()


# ------------------------------------------------------------ database and locks

class SqliteCheck(CheckCase):
    def test_this_computers_database_is_fine(self):
        result = self.well_formed(checks.check_sqlite(), "sqlite")
        self.assertEqual(result["status"], "ok")
        self.assertIn(sqlite3.sqlite_version, result["detail"])

    def test_too_old_fails_at_the_exact_boundary(self):
        for info, expected in (((3, 8, 7), "fail"), ((3, 7, 17), "fail"), ((3, 8, 8), "ok"), ((3, 45, 1), "ok")):
            with self.subTest(info=info), patch("sqlite3.sqlite_version_info", info), patch("sqlite3.sqlite_version", ".".join(map(str, info))):
                result = self.well_formed(checks.check_sqlite(), "sqlite")
                self.assertEqual(result["status"], expected)
                if expected == "fail":
                    self.assertIn("3.8.8", result["hint"])

    def test_a_python_built_without_a_database(self):
        with patch.dict(sys.modules, {"sqlite3": None}):
            result = self.well_formed(checks.check_sqlite(), "sqlite")
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["fix"])

    def test_a_database_without_safe_write_mode_or_linked_tables(self):
        class Fake:
            def __init__(self, options, links):
                self.options, self.links = options, links

            def execute(self, sql):
                if sql == "PRAGMA compile_options":
                    return iter([(option,) for option in self.options])
                if sql == "PRAGMA foreign_keys=ON":
                    return iter([])
                if sql == "PRAGMA foreign_keys":
                    return types.SimpleNamespace(fetchone=lambda: self.links)
                raise AssertionError(sql)

            def close(self):
                pass

        for options, links in ((["OMIT_WAL"], (1,)), (["THREADSAFE=1"], (0,))):
            with self.subTest(options=options, links=links), patch("sqlite3.connect", return_value=Fake(options, links)):
                result = self.well_formed(checks.check_sqlite(), "sqlite")
                self.assertEqual(result["status"], "fail")

    def test_a_database_that_will_not_open_is_unknown(self):
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("nope")):
            self.assertEqual(self.well_formed(checks.check_sqlite(), "sqlite")["status"], "unknown")
        with patch("sqlite3.sqlite_version_info", ()):
            self.assertEqual(self.well_formed(checks.check_sqlite(), "sqlite")["status"], "unknown")

    def test_an_unreadable_version_number_is_unknown(self):
        with patch("sqlite3.sqlite_version_info", ("x", "y")):
            self.assertEqual(self.well_formed(checks.check_sqlite(), "sqlite")["status"], "unknown")

    def test_it_never_writes_a_database_file(self):
        # Look in a private, empty folder: the project folder is written to by other programs at the same moment.
        here = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, here)
        before = snapshot(Path.cwd())
        self.assertEqual(before, {}, "the folder this test looks in must start empty")
        checks.check_sqlite()
        self.assertEqual(snapshot(Path.cwd()), before)


class FcntlCheck(CheckCase):
    def test_file_locks_work_here(self):
        result = self.well_formed(checks.check_fcntl(), "fcntl")
        self.assertEqual(result["status"], "ok")

    def test_a_computer_without_them_fails(self):
        with patch.dict(sys.modules, {"fcntl": None}):
            result = self.well_formed(checks.check_fcntl(), "fcntl")
        self.assertEqual(result["status"], "fail")
        self.assertIn("Mac or a Linux", result["fix"])

    def test_an_incomplete_module_fails(self):
        with patch.dict(sys.modules, {"fcntl": types.ModuleType("fcntl")}):
            self.assertEqual(self.well_formed(checks.check_fcntl(), "fcntl")["status"], "fail")

    def test_locks_that_error_when_tried_fail(self):
        with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            result = self.well_formed(checks.check_fcntl(), "fcntl")
        self.assertEqual(result["status"], "fail")
        self.assertIn("no locks available", result["detail"])

    def test_it_locks_only_a_read_only_copy_of_its_own_source_never_the_stations_files(self):
        opened = []
        real_open = open

        def spy(path, *args, **kwargs):
            opened.append((str(path), args[0] if args else kwargs.get("mode", "r")))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", spy):
            checks.check_fcntl()
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][0].endswith("checks.py") and opened[0][1] == "rb")


# ---------------------------------------------------------------- memory and processor

class MemoryCheck(CheckCase):
    def run_check(self, meminfo=(16 * 1024 * 1024, 12 * 1024 * 1024), **extra):
        roots = make_roots(self.dir / f"r{len(list(self.dir.iterdir()))}", meminfo_kib=meminfo)
        return self.well_formed(checks.check_memory(Settings(**roots, **extra)), "memory")

    def test_plenty_is_ok(self):
        self.pretend(system="Linux")
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("16.0 GB of memory (12.0 GB free right now)", result["detail"])

    def test_a_small_computer_warns_without_inventing_a_requirement(self):
        self.pretend(system="Linux")
        result = self.run_check((2 * 1024 * 1024, 1024 * 1024))
        self.assertEqual(result["status"], "warn")
        self.assertIn("2.0 GB", result["detail"])
        self.assertIn("setup screens", result["hint"])
        self.assertEqual(self.run_check((4 * 1024 * 1024, 3 * 1024 * 1024))["status"], "ok")
        self.assertEqual(self.run_check((4 * 1024 * 1024 - 1, 3 * 1024 * 1024))["status"], "warn")
        self.assertIn("should run", result["hint"], "what was not tried is not stated as fact")
        self.assertNotIn("will run", result["hint"])

    def test_the_memory_limits_are_exact(self):
        """4 GiB in total and 512 MiB free right now: written out in full so a changed limit is noticed."""
        self.assertEqual(checks.MEMORY_WARN_BYTES, 4 * 1024 ** 3)
        self.assertEqual(checks.MEMORY_LOW_AVAILABLE_BYTES, 512 * 1024 ** 2)
        self.pretend(system="Linux")
        total = 16 * 1024 * 1024  # in kB, the unit the system reports
        exact = self.run_check((total, 512 * 1024))
        self.assertEqual(exact["status"], "ok", "exactly 512 MiB free is enough")
        short = self.run_check((total, 512 * 1024 - 1))
        self.assertEqual(short["status"], "warn", "one kB less is not")
        self.assertIn("only 511 MB is free", short["detail"])
        self.assertEqual(self.run_check((4 * 1024 * 1024, 512 * 1024))["status"], "ok", "exactly 4 GiB in total is enough")
        self.assertIn("3.9 GB of memory", self.run_check((4 * 1024 * 1024 - 1, 1024 * 1024))["detail"])

    def test_nearly_nothing_free_right_now_warns(self):
        self.pretend(system="Linux")
        result = self.run_check((16 * 1024 * 1024, 100 * 1024))
        self.assertEqual(result["status"], "warn")
        self.assertIn("only 100 MB is free", result["detail"])

    def test_a_computer_that_will_not_say(self):
        self.pretend(system="Linux")
        with patch("os.sysconf", side_effect=ValueError("no such name")):
            result = checks.check_memory(Settings(proc_root=str(self.dir / "nowhere")))
        self.assertEqual(self.well_formed(result, "memory")["status"], "unknown")

    def test_nonsense_numbers_are_ignored(self):
        self.pretend(system="Linux")
        roots = make_roots(self.dir / "weird", meminfo_kib=None)
        (Path(roots["proc_root"]) / "meminfo").write_text("MemTotal: lots kB\nMemAvailable: 99999999999 kB\n")
        with patch("os.sysconf", side_effect=OSError("no")):
            self.assertEqual(checks.check_memory(Settings(**roots))["status"], "unknown")
        (Path(roots["proc_root"]) / "meminfo").write_text("MemTotal: 8388608 kB\nMemAvailable: 99999999999 kB\n")
        result = checks.check_memory(Settings(**roots))
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("free right now", result["detail"])

    def test_digits_that_are_not_numbers_are_ignored(self):
        self.pretend(system="Linux")
        roots = make_roots(self.dir / "digits", meminfo_kib=None)
        (Path(roots["proc_root"]) / "meminfo").write_text("MemTotal: \u00b2 kB\n")
        with patch("os.sysconf", side_effect=ValueError("no")):
            self.assertEqual(self.well_formed(checks.check_memory(Settings(**roots)), "memory")["status"], "unknown")

    def test_a_mac_asks_the_system_when_the_usual_way_fails(self):
        table = ToolTable().add("sysctl", ["-n", "hw.memsize"], "17179869184\n")
        self.pretend(system="Darwin", machine="arm64", programs=("sysctl",), table=table)
        with patch("os.sysconf", side_effect=ValueError("not on this system")):
            result = self.well_formed(checks.check_memory(Settings()), "memory")
        self.assertEqual(result["status"], "ok")
        self.assertIn("16.0 GB", result["detail"])
        table = ToolTable().add("sysctl", ["-n", "hw.memsize"], "-1\n")
        with ExitStack() as stack, patch("os.sysconf", side_effect=ValueError("no")):
            pretend_computer(stack, system="Darwin", programs=("sysctl",), table=table)
            self.assertEqual(checks.check_memory(Settings())["status"], "unknown")


class CpuCheck(CheckCase):
    def test_counts_and_warnings(self):
        for cores, expected in ((8, "ok"), (2, "ok"), (1, "warn")):
            with self.subTest(cores=cores), patch("os.sched_getaffinity", return_value=set(range(cores)), create=True):
                result = self.well_formed(checks.check_cpu(), "cpu")
                self.assertEqual(result["status"], expected)
                self.assertIn(f"{cores} processor core", result["detail"])

    def test_falls_back_to_the_plain_count_and_then_to_unknown(self):
        with patch("os.sched_getaffinity", side_effect=OSError("no"), create=True), patch("os.cpu_count", return_value=4):
            self.assertIn("4 processor cores", checks.check_cpu()["detail"])
        with patch("os.sched_getaffinity", side_effect=AttributeError("mac"), create=True), patch("os.cpu_count", return_value=None):
            self.assertEqual(self.well_formed(checks.check_cpu(), "cpu")["status"], "unknown")
        with patch("os.sched_getaffinity", return_value=set(), create=True), patch("os.cpu_count", return_value=0):
            self.assertEqual(self.well_formed(checks.check_cpu(), "cpu")["status"], "unknown")
        with patch("os.sched_getaffinity", return_value=set(range(3)), create=True), patch("os.cpu_count", side_effect=RuntimeError("x")):
            self.assertEqual(checks.check_cpu()["status"], "ok")


# ---------------------------------------------------------------- graphics

class AcceleratorCheck(CheckCase):
    def linux(self, name="roots", **roots):
        self.pretend(system="Linux", programs=roots.pop("programs", ()), table=roots.pop("table", None))
        return Settings(**make_roots(self.dir / name, **roots))

    def test_an_intel_card_is_detected_not_certified(self):
        settings = self.linux(vendor="0x8086", driver="xe", programs=("clinfo",))
        result = self.well_formed(checks.check_accelerator(settings), "accelerator")
        self.assertEqual(result["status"], "ok")
        for text in ("Detected:", "Intel", "driver xe", "clinfo", "not run", "decided by their own setup checks"):
            self.assertIn(text, result["detail"])
        for claim in ("works", "ready", "supported", "can make"):
            self.assertNotIn(claim, result["detail"].lower())

    def test_other_makers_and_several_devices(self):
        settings = self.linux(render_nodes=("renderD128", "renderD129"), vendor="0x1002", driver="amdgpu")
        detail = checks.check_accelerator(settings)["detail"]
        self.assertEqual(detail.count("AMD (driver amdgpu)"), 2)
        settings = self.linux("other", vendor="0x1af4", driver="virtio_gpu")
        self.assertIn("a graphics device (driver virtio_gpu)", checks.check_accelerator(settings)["detail"])

    def test_nvidia_devices_without_render_nodes(self):
        settings = self.linux(render_nodes=(), nvidia=("nvidia0", "nvidia1", "nvidiactl"), programs=("nvidia-smi",))
        result = self.well_formed(checks.check_accelerator(settings), "accelerator")
        self.assertEqual(result["status"], "ok")
        self.assertIn("NVIDIA (2 devices)", result["detail"])
        self.assertIn("nvidia-smi", result["detail"])

    def test_no_graphics_card_is_a_warning_that_does_not_block_anything(self):
        result = self.well_formed(checks.check_accelerator(self.linux(render_nodes=())), "accelerator")
        self.assertEqual(result["status"], "warn")
        self.assertIn("No graphics card was detected", result["detail"])
        self.assertIn("your own audio", result["hint"])
        missing = Settings(dev_root=str(self.dir / "nope"), sys_root=str(self.dir / "nope"))
        self.assertEqual(checks.check_accelerator(missing)["status"], "warn")

    @unittest.skipIf(IS_ROOT, "root can open any device")
    def test_a_card_whose_maker_and_driver_are_not_listed_is_still_detected(self):
        settings = self.linux()
        device = Path(settings.sys_root) / "class" / "drm" / "renderD128" / "device"
        (device / "vendor").unlink()
        (device / "driver").unlink()
        result = self.well_formed(checks.check_accelerator(settings), "accelerator")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Detected: a graphics device.", result["detail"])

    @unittest.skipIf(IS_ROOT, "root can open any device")
    def test_a_card_this_account_may_not_open_is_explained(self):
        settings = self.linux(node_mode=0o000)
        result = self.well_formed(checks.check_accelerator(settings), "accelerator")
        self.assertEqual(result["status"], "warn")
        self.assertIn("not allowed to use it", result["detail"])
        self.assertIn("usermod", result["fix"])

    def test_the_tools_are_looked_up_by_name_and_never_run(self):
        table = ToolTable()
        settings = self.linux(programs=("clinfo", "sycl-ls", "nvidia-smi"), table=table)
        with patch("subprocess.Popen", side_effect=AssertionError("started a program")):
            result = checks.check_accelerator(settings)
        self.assertEqual(table.calls, [])
        self.assertIn("clinfo, sycl-ls, nvidia-smi", result["detail"])

    def test_hostile_driver_names_are_cleaned(self):
        settings = self.linux(driver="bad\x1b[31mdriver")
        result = checks.check_accelerator(settings)
        self.assertNotIn("\x1b", result["detail"])

    def test_apple_silicon_is_detected_from_the_systems_own_report(self):
        table = ToolTable().add("system_profiler", ["-json", "SPDisplaysDataType"], profiler_json("Apple M2 Pro", "19"))
        self.pretend(system="Darwin", machine="arm64", programs=("system_profiler", "sysctl"), table=table)
        result = self.well_formed(checks.check_accelerator(Settings()), "accelerator")
        self.assertEqual(result["status"], "warn", "detected, but Mac support is not verified, so it is not called ready")
        for text in ("Detected:", "Apple silicon", "Apple M2 Pro", "19 graphics cores", "not been verified", "decided by their own setup checks"):
            self.assertIn(text, result["detail"])
        self.assertIn("Nothing needs fixing", result["hint"])

    def test_a_mac_whose_profile_is_missing_garbled_or_slow_is_still_detected_by_its_chip_type(self):
        for answer in (None, "not json", "{}", '{"SPDisplaysDataType": []}', '{"SPDisplaysDataType": [1, "x"]}', "[" * 100000):
            with self.subTest(answer=str(answer)[:12]), ExitStack() as stack:
                table = ToolTable()
                if answer is not None:
                    table.add("system_profiler", ["-json", "SPDisplaysDataType"], answer)
                pretend_computer(stack, system="Darwin", machine="arm64", programs=("system_profiler",), table=table)
                result = self.well_formed(checks.check_accelerator(Settings()), "accelerator")
                self.assertEqual(result["status"], "warn")
                self.assertTrue(result["detail"].startswith("Detected: an Apple silicon Mac."))
        with ExitStack() as stack:
            table = ToolTable().add_hang("system_profiler", ["-json", "SPDisplaysDataType"])
            pretend_computer(stack, system="Darwin", machine="arm64", programs=("system_profiler",), table=table)
            self.assertIn("did not answer in time", checks.check_accelerator(Settings())["detail"])

    def test_rosetta_still_has_apple_silicon_underneath(self):
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n")
        self.pretend(system="Darwin", machine="x86_64", programs=("sysctl",), table=table)
        result = self.well_formed(checks.check_accelerator(Settings()), "accelerator")
        self.assertEqual(result["status"], "warn")
        self.assertIn("Apple silicon", result["detail"])

    def test_an_intel_mac_is_not_promised_anything(self):
        table = ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "sysctl: unknown oid 'sysctl.proc_translated'", exit_code=1)
        self.pretend(system="Darwin", machine="x86_64", programs=("sysctl", "system_profiler"), table=table)
        result = self.well_formed(checks.check_accelerator(Settings()), "accelerator")
        self.assertEqual(result["status"], "warn")
        self.assertIn("Intel Mac", result["detail"])
        self.assertIn("has not been checked", result["hint"])

    def test_a_mac_that_will_not_say_and_other_systems(self):
        self.pretend(system="Darwin", machine="x86_64", programs=())
        self.assertEqual(self.well_formed(checks.check_accelerator(Settings()), "accelerator")["status"], "unknown")
        with ExitStack() as stack:
            pretend_computer(stack, system="FreeBSD", machine="amd64")
            self.assertEqual(self.well_formed(checks.check_accelerator(Settings()), "accelerator")["status"], "unknown")


# ---------------------------------------------------------------- clock and timezone

class ClockCheck(CheckCase):
    def run_check(self, now=NOW, tz="America/Chicago", offset=-21600, **extra):
        env = {} if tz is None else {"TZ": tz}
        with patch.dict(os.environ, env), patch("myradio.doctor.checks._local_offset", return_value=offset):
            if tz is None:
                os.environ.pop("TZ", None)
            return self.well_formed(checks.check_clock(Settings(now=lambda: now, **extra)), "clock")

    def test_a_sensible_clock_and_zone_are_ok(self):
        result = self.run_check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("2026-11-05 12:00", result["detail"])
        self.assertIn("America/Chicago", result["detail"])
        self.assertIn("UTC-06:00", result["detail"])

    def test_an_impossible_date_fails_with_a_fix(self):
        for when in (0.0, 946684800.0, CLOCK_EARLIEST - 1, CLOCK_LATEST, 4.2e9):
            with self.subTest(when=when):
                result = self.run_check(now=when)
                self.assertEqual(result["status"], "fail")
                self.assertIn("cannot be right", result["detail"])
                self.assertIn("automatically", result["fix"])
        self.assertEqual(self.run_check(now=CLOCK_EARLIEST)["status"], "ok")
        self.assertEqual(self.run_check(now=CLOCK_LATEST - 1)["status"], "ok")

    def test_the_believable_window_is_exactly_2024_to_2099(self):
        utc = dt.timezone.utc
        self.assertEqual(CLOCK_EARLIEST, dt.datetime(2024, 1, 1, tzinfo=utc).timestamp())
        self.assertEqual(CLOCK_LATEST, dt.datetime(2100, 1, 1, tzinfo=utc).timestamp())
        for moment, expected in ((dt.datetime(2001, 9, 9, tzinfo=utc), "fail"), (dt.datetime(2023, 12, 31, 23, 59, 59, tzinfo=utc), "fail"),
                                 (dt.datetime(2024, 1, 1, tzinfo=utc), "ok"), (dt.datetime(2099, 12, 31, 23, 59, 59, tzinfo=utc), "ok"),
                                 (dt.datetime(2100, 1, 1, tzinfo=utc), "fail")):
            with self.subTest(moment=moment.isoformat()):
                self.assertEqual(self.run_check(now=moment.timestamp())["status"], expected)
        self.assertIn("31 December 2023", self.run_check(now=dt.datetime(2023, 12, 31, 23, 59, 59, tzinfo=utc).timestamp())["detail"])

    def test_a_clock_that_cannot_be_read_is_unknown(self):
        for bad in (float("nan"), float("inf"), "now", None, True):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(self.run_check(now=bad)["status"], "unknown")

        def explode():
            raise RuntimeError("clock stopped")

        with patch.dict(os.environ, {"TZ": "UTC"}):
            self.assertEqual(self.well_formed(checks.check_clock(Settings(now=explode)), "clock")["status"], "unknown")

    def test_a_timezone_and_clock_that_disagree_warn(self):
        result = self.run_check(offset=32400)
        self.assertEqual(result["status"], "warn")
        self.assertIn("UTC+09:00", result["detail"])
        self.assertIn("UTC-06:00", result["detail"])
        self.assertIn("disagree", result["detail"])

    def test_a_made_up_tz_setting_warns_and_says_what_is_happening(self):
        result = self.run_check(tz="Mars/Phobos", offset=0)
        self.assertEqual(result["status"], "warn")
        self.assertIn("does not exist", result["detail"])
        self.assertIn("UTC", result["hint"])

    def test_a_tz_setting_in_the_old_style_is_unknown_not_wrong(self):
        for tz in ("CST6CDT,M3.2.0,M11.1.0", "EST5", "<+03>-3"):
            with self.subTest(tz=tz):
                self.assertEqual(self.run_check(tz=tz, offset=-21600)["status"], "unknown")

    def test_the_zone_comes_from_the_system_link_or_file_when_tz_is_not_set(self):
        roots = make_roots(self.dir / "etc1", zone="Europe/London")
        result = self.run_check(tz=None, offset=0, **{k: roots[k] for k in ("etc_root",)})
        self.assertEqual(result["status"], "ok")
        self.assertIn("Europe/London", result["detail"])
        mac = self.dir / "mac"
        (mac / "etc").mkdir(parents=True)
        os.symlink("/var/db/timezone/zoneinfo/America/New_York", mac / "etc" / "localtime")
        self.assertIn("America/New_York", self.run_check(tz=None, offset=-18000, etc_root=str(mac / "etc"))["detail"])
        debian = self.dir / "deb"
        debian.mkdir()
        (debian / "timezone").write_text("Asia/Tokyo\n")
        self.assertIn("Asia/Tokyo", self.run_check(tz=None, offset=32400, etc_root=str(debian))["detail"])

    def test_no_name_or_an_unknown_name_is_unknown(self):
        empty = self.dir / "empty"
        empty.mkdir()
        result = self.run_check(tz=None, offset=0, etc_root=str(empty))
        self.assertEqual(result["status"], "unknown")
        self.assertIn("America/Chicago", result["hint"])
        fake = self.dir / "fake"
        fake.mkdir()
        os.symlink("/usr/share/zoneinfo/Fake/Place", fake / "localtime")
        self.assertEqual(self.run_check(tz=None, offset=0, etc_root=str(fake))["status"], "unknown")

    def test_odd_characters_in_the_zone_name_are_cleaned(self):
        result = self.run_check(tz="Bad\x1b[31m/Zone\n", offset=0)
        self.assertEqual(result["status"], "warn")
        for key in KEYS:
            self.assertNotIn("\x1b", result[key])

    def test_impossible_dates_are_named_plainly_and_a_computer_without_zone_support_is_handled(self):
        for when in (1e20, -1e20):
            result = self.run_check(now=when)
            self.assertEqual(result["status"], "fail")
            self.assertIn("impossible date", result["detail"])
        with patch.dict(sys.modules, {"zoneinfo": None}):
            result = self.run_check()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("could not be checked", result["detail"])

    def test_the_clocks_own_offset_can_fail_without_breaking_the_check(self):
        with patch("time.localtime", side_effect=OSError("no clock")):
            self.assertIsNone(checks._local_offset(NOW))
        with patch.dict(os.environ, {"TZ": "America/Chicago"}), patch("time.localtime", side_effect=OverflowError("far")):
            self.assertEqual(self.well_formed(checks.check_clock(Settings(now=lambda: NOW)), "clock")["status"], "ok")

    def test_the_offset_may_be_unavailable(self):
        result = self.run_check(offset=None)
        self.assertEqual(result["status"], "ok")

    def test_the_real_clock_and_zone_give_an_answer(self):
        result = self.well_formed(checks.check_clock(), "clock")
        self.assertIn(result["status"], STATES)


# ---------------------------------------------------------------- the whole set

class WholeSet(CheckCase):
    def settings(self):
        return Settings(data_dir=str(self.station()), port=0, tool_timeout=20)

    def test_the_registry_lists_every_check_once_in_a_stable_order(self):
        self.assertEqual([spec.id for spec in CHECKS],
                         ["python", "ffmpeg", "disk-space", "data-folder", "loopback", "port", "system", "sqlite", "fcntl",
                          "tzdata", "clock", "memory", "cpu", "accelerator"])
        self.assertEqual(len({spec.id for spec in CHECKS}), len(CHECKS))
        for spec in CHECKS:
            self.assertEqual(spec.title, checks.TITLES[spec.id])

    def test_every_check_is_well_formed_on_this_computer(self):
        hermetic_tools(self.stack, self.dir)  # the real checks, with only a fake FFmpeg to find
        settings = self.settings()
        for spec in CHECKS:
            with self.subTest(check=spec.id):
                self.well_formed(spec.func(settings), spec.id)

    def test_every_check_survives_being_called_with_nothing_at_all(self):
        # With no settings the port check looks at the default port, 8765, which is where a running station listens. That bind
        # is replaced here, never performed (and the guard in setUp fails the test if a real one is attempted).
        with patch("myradio.doctor.checks.run_tool", ToolTable()), patch("myradio.doctor.checks._bind", return_value=0) as bind:
            for spec in CHECKS:
                with self.subTest(check=spec.id):
                    self.well_formed(spec.func(None), spec.id)
                    self.well_formed(spec.func("not settings"), spec.id)
        self.assertIn(DEFAULT_PORT, [call.args[0] for call in bind.call_args_list], "the default port was asked about, through the stand-in only")
        self.assertEqual(self.ports.refused, [], "no real bind to a fixed port may happen")

    def test_no_check_raises_when_the_computer_misbehaves_everywhere(self):
        sabotage = [("platform.system", RuntimeError("x")), ("platform.machine", RuntimeError("x")), ("platform.release", OSError("x")),
                    ("platform.mac_ver", ValueError("x")), ("platform.python_implementation", RuntimeError("x")),
                    ("platform.freedesktop_os_release", KeyError("x")), ("shutil.which", OSError("x")),
                    ("shutil.disk_usage", RuntimeError("x")), ("os.access", RuntimeError("x")), ("os.scandir", RuntimeError("x")),
                    ("os.readlink", RuntimeError("x")), ("os.sysconf", RuntimeError("x")), ("os.cpu_count", RuntimeError("x")),
                    ("os.path.realpath", RuntimeError("x")), ("os.path.isfile", RuntimeError("x")), ("socket.socket", RuntimeError("x")),
                    ("sqlite3.connect", RuntimeError("x")), ("zoneinfo.ZoneInfo", RuntimeError("x")),
                    ("myradio.doctor.checks.run_tool", RuntimeError("x")), ("threading.Thread.start", RuntimeError("can't start new thread"))]
        settings = self.settings()
        self.stack.enter_context(patch("myradio.doctor.checks.run_tool", ToolTable()))  # tools are covered elsewhere; keep this fast
        for target, error in sabotage:
            with self.subTest(sabotage=target), patch(target, side_effect=error):
                for spec in CHECKS:
                    self.well_formed(spec.func(settings), spec.id)
        with ExitStack() as stack:
            for target, error in sabotage:
                stack.enter_context(patch(target, side_effect=error))
            for spec in CHECKS:
                self.well_formed(spec.func(settings), spec.id)

    def test_all_the_wording_is_plain_across_many_simulated_computers(self):
        scenarios = [
            dict(system="Linux", machine="x86_64", os_release=UBUNTU, programs=("ffmpeg", "ffprobe"), table=ffmpeg_table()),
            dict(system="Linux", machine="x86_64", os_release=ARCH, programs=()),
            dict(system="Linux", machine="riscv64", os_release=None, programs=("ffmpeg",), table=ffmpeg_table(encoders=[], decoders=[])),
            dict(system="Darwin", machine="arm64", mac_version="14.5", programs=("ffmpeg", "system_profiler", "sysctl"),
                 table=ffmpeg_table(encoders=["pcm_s16le"]).add("system_profiler", ["-json", "SPDisplaysDataType"], profiler_json())),
            dict(system="Darwin", machine="x86_64", mac_version="12.7", programs=("sysctl",),
                 table=ToolTable().add("sysctl", ["-n", "sysctl.proc_translated"], "1\n")),
            dict(system="Windows", machine="AMD64", programs=()),
        ]
        for scenario in scenarios:
            with self.subTest(system=scenario["system"], machine=scenario["machine"]), ExitStack() as stack:
                pretend_computer(stack, **scenario)
                roots = make_roots(self.dir / f"s{len(list(self.dir.iterdir()))}", render_nodes=())
                settings = Settings(data_dir=str(self.dir / "absent"), port=0, **roots)
                for spec in CHECKS:
                    self.well_formed(spec.func(settings), spec.id)

    def test_no_check_starts_a_program_except_through_the_tool_runner(self):
        with patch("subprocess.Popen", side_effect=AssertionError("a check started a program by itself")), \
                patch("subprocess.run", side_effect=AssertionError("a check started a program by itself")), \
                patch("os.system", side_effect=AssertionError("shell")), patch("os.popen", side_effect=AssertionError("shell")), \
                patch("myradio.doctor.checks.run_tool", ToolTable()):
            settings = self.settings()
            for spec in CHECKS:
                self.well_formed(spec.func(settings), spec.id)


if __name__ == "__main__":
    unittest.main()
