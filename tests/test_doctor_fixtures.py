"""Shared fixtures for the doctor tests. This file holds no tests of its own.

Everything here is a FIXTURE OR A FAKE: the tool output below is hand-written to have the shape of
the real thing (the FFmpeg 6.1 ``-encoders`` / ``-decoders`` listing format and macOS
``system_profiler -json``). None of it was captured from a Mac, and no real GPU, FFmpeg build
or network is involved when these fakes are used.

The file is named ``test_doctor_*`` only so that it stays inside the doctor lane's owned paths.
"""
import errno
import json
import os
import shlex
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from myradio.doctor.checks import CheckSpec, Settings
from myradio.doctor.tools import ToolResult

UBUNTU = {"ID": "ubuntu", "ID_LIKE": "debian", "PRETTY_NAME": "Ubuntu 24.04 LTS"}
ARCH = {"ID": "arch", "PRETTY_NAME": "Arch Linux"}
FEDORA = {"ID": "fedora", "PRETTY_NAME": "Fedora Linux 40"}

GOOD_ENCODERS = ["aac", "flac", "libmp3lame", "libshine", "pcm_s16le"]
GOOD_DECODERS = ["aac", "aac_fixed", "alac", "flac", "mp3", "mp3float", "opus", "pcm_s16be", "pcm_s16le", "vorbis"]

LEGEND = """ V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ..S... = Slice-level multithreading
 ...X.. = Codec is experimental
 ....B. = Supports draw_horiz_band
 .....D = Supports direct rendering method 1
 ------
"""


def banner(program="ffmpeg", version="6.1.1-3ubuntu5"):
    return (f"{program} version {version} Copyright (c) 2000-2023 the FFmpeg developers\n"
            "built with gcc 13 (Ubuntu 13.2.0-23ubuntu3)\n"
            "configuration: --prefix=/usr --enable-gpl --enable-libmp3lame --enable-shared\n"
            "libavutil      58. 29.100 / 58. 29.100\n")


def listing(kind, names):
    """``Encoders:`` or ``Decoders:`` output in FFmpeg's listing format, with one video line for realism."""
    lines = [f"{kind}:\n", LEGEND, " V....D h264                 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10\n"]
    lines += [f" A....D {name:<20}{name} sample description\n" for name in names]
    return "".join(lines)


def profiler_json(model="Apple M2 Pro", cores="19"):
    return json.dumps({"SPDisplaysDataType": [{"_name": model, "spdisplays_vendor": "sppci_vendor_Apple",
                                               "sppci_cores": cores, "sppci_model": model}]}, indent=2)


class ToolTable:
    """Stands in for ``run_tool``: answers by program name and arguments, and records every call."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def add(self, program, args, output="", exit_code=0, **flags):
        self.answers[(program, tuple(args))] = ToolResult(ran=True, exit_code=exit_code, output=output, **flags)
        return self

    def add_missing(self, program, args):
        self.answers[(program, tuple(args))] = ToolResult(reason="missing")
        return self

    def add_hang(self, program, args):
        self.answers[(program, tuple(args))] = ToolResult(ran=True, exit_code=-9, timed_out=True)
        return self

    def __call__(self, args, *, timeout, max_bytes=262144, cancel=None):
        self.calls.append(list(args))
        return self.answers.get((os.path.basename(args[0]), tuple(args[1:])), ToolResult(reason="missing"))


def ffmpeg_table(table=None, *, encoders=GOOD_ENCODERS, decoders=GOOD_DECODERS, probe=True, version=None):
    table = table if table is not None else ToolTable()
    table.add("ffmpeg", ["-version"], version if version is not None else banner("ffmpeg"))
    table.add("ffmpeg", ["-hide_banner", "-encoders"], listing("Encoders", encoders))
    table.add("ffmpeg", ["-hide_banner", "-decoders"], listing("Decoders", decoders))
    if probe:
        table.add("ffprobe", ["-version"], banner("ffprobe"))
    return table


def pretend_computer(stack, *, system="Linux", machine="x86_64", release="6.8.0-test", os_release=UBUNTU, mac_version="",
                     programs=(), table=None, known_dirs=(), system_dirs=()):
    """Make this process look like another computer until ``stack`` closes.

    Patches the standard-library calls the checks make (platform, shutil.which) and the doctor's own
    ``run_tool`` so that nothing real is started. ``programs`` are the names ``shutil.which`` will find.
    """
    found = {name: f"/usr/bin/{name}" for name in programs}
    table = table if table is not None else ToolTable()
    stack.enter_context(patch("platform.system", return_value=system))
    stack.enter_context(patch("platform.machine", return_value=machine))
    stack.enter_context(patch("platform.release", return_value=release))
    stack.enter_context(patch("platform.mac_ver", return_value=(mac_version, ("", "", ""), machine)))
    if os_release is None:
        stack.enter_context(patch("platform.freedesktop_os_release", side_effect=OSError("no os-release")))
    else:
        stack.enter_context(patch("platform.freedesktop_os_release", return_value=dict(os_release)))
    stack.enter_context(patch("shutil.which", side_effect=lambda name, *a, **k: found.get(name)))
    stack.enter_context(patch("myradio.doctor.checks.run_tool", table))
    stack.enter_context(patch("myradio.doctor.checks.KNOWN_TOOL_DIRS", tuple(known_dirs)))
    stack.enter_context(patch("myradio.doctor.checks.SYSTEM_TOOL_DIRS", tuple(system_dirs)))
    return table


def make_program(directory, name, source):
    """A real executable file ``directory/name`` that runs ``source`` (Python) with this interpreter."""
    directory = Path(directory)
    script = directory / f"{name}.py"
    script.write_text(source)
    launcher = directory / name
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
    launcher.chmod(0o755)
    return str(launcher)


FAKE_FFMPEG_SOURCE = '''
import sys
args = sys.argv[1:]
if "-version" in args:
    sys.stdout.write({banner!r})
elif "-encoders" in args:
    sys.stdout.write({encoders!r})
elif "-decoders" in args:
    sys.stdout.write({decoders!r})
'''


def make_fake_ffmpeg(directory, *, encoders=GOOD_ENCODERS, decoders=GOOD_DECODERS):
    """A real executable that prints realistic listings (a FAKE: it is not FFmpeg)."""
    return make_program(directory, "ffmpeg", FAKE_FFMPEG_SOURCE.format(
        banner=banner("ffmpeg"), encoders=listing("Encoders", encoders), decoders=listing("Decoders", decoders)))


def make_fake_tools(directory):
    """A folder holding a fake ffmpeg and ffprobe (printing realistic text), for tests that run the real checks."""
    shelf = Path(directory) / "fake-bin"
    shelf.mkdir(exist_ok=True)
    make_fake_ffmpeg(shelf)
    make_program(shelf, "ffprobe", f"import sys\nsys.stdout.write({banner('ffprobe')!r})\n")
    return str(shelf)


def hermetic_tools(stack, directory):
    """Real checks, but the only FFmpeg and ffprobe they can find are the harmless fakes in ``directory``."""
    shelf = make_fake_tools(directory)
    real = shutil.which
    found = {"ffmpeg": os.path.join(shelf, "ffmpeg"), "ffprobe": os.path.join(shelf, "ffprobe")}
    stack.enter_context(patch("shutil.which", side_effect=lambda name, *a, **k: found[name] if name in found else real(name, *a, **k)))
    return shelf


def make_roots(base, *, render_nodes=("renderD128",), vendor="0x8086", driver="xe", nvidia=(), meminfo_kib=(16 * 1024 * 1024, 12 * 1024 * 1024),
               zone="America/Chicago", node_mode=0o666):
    """A throwaway /dev, /sys, /proc and /etc to point the checks at. Returns Settings keyword arguments."""
    base = Path(base)
    dev, sys_root, proc, etc = base / "dev", base / "sys", base / "proc", base / "etc"
    (dev / "dri").mkdir(parents=True)
    proc.mkdir(parents=True)
    etc.mkdir(parents=True)
    for name in render_nodes:
        node = dev / "dri" / name
        node.write_text("")
        node.chmod(node_mode)
        device = sys_root / "class" / "drm" / name / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n")
        os.symlink("../../../../bus/pci/drivers/" + driver, device / "driver")
    for name in nvidia:
        (dev / name).write_text("")
    if meminfo_kib is not None:
        total, available = meminfo_kib
        (proc / "meminfo").write_text(f"MemTotal:       {total} kB\nMemFree:         1000 kB\nMemAvailable:   {available} kB\n")
    if zone:
        os.symlink(f"/usr/share/zoneinfo/{zone}", etc / "localtime")
    return {"dev_root": str(dev), "sys_root": str(sys_root), "proc_root": str(proc), "etc_root": str(etc)}


class PortGuard:
    """Keeps a test from binding a fixed port on this computer.

    A test may bind 127.0.0.1 only with port 0 (the operating system picks a free port), or to a port
    the operating system itself handed out earlier in the same test. Any other bind is REFUSED, not
    performed, and fails the test when it ends. That keeps the tests away from 8765, the control page's
    own port, where a running station may be listening.
    """

    def __init__(self):
        self.handed_out = set()
        self.refused = []
        real_bind = socket.socket.bind
        guard = self

        def bind(sock, address, *rest):
            port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
            if isinstance(port, int) and port != 0 and port not in guard.handed_out:
                guard.refused.append(address)
                raise PermissionError(errno.EACCES, "the doctor tests may only bind port 0 or a port the system handed out")
            outcome = real_bind(sock, address, *rest)
            if port == 0:
                guard.handed_out.add(sock.getsockname()[1])
            return outcome

        self.bind = bind


def guard_ports(case):
    """Install a PortGuard for the length of one test and fail the test if it asked for a fixed port."""
    guard = PortGuard()
    patcher = patch.object(socket.socket, "bind", guard.bind)
    patcher.start()
    case.addCleanup(patcher.stop)
    case.addCleanup(lambda: case.assertEqual(guard.refused, [], "a test tried to bind a fixed port; use port 0"))
    return guard


def canned_spec(check_id, status="ok", detail="fine", hint="", fix="", title=None):
    """A check that returns a fixed answer instantly."""
    def check(settings=None):
        return {"id": check_id, "title": title or check_id.title(), "status": status, "detail": detail, "hint": hint, "fix": fix}
    return CheckSpec(check_id, title or check_id.title(), check)


def fast_specs(*statuses):
    """One canned spec per status, named a, b, c... (then c26, c27...)."""
    return tuple(canned_spec(chr(97 + index) if index < 26 else f"c{index}", status, hint="" if status == "ok" else "Do the thing.")
                 for index, status in enumerate(statuses))


def settings_for(path, **extra):
    return Settings(data_dir=str(path), port=0, **extra)


def doctor_threads():
    return [t for t in threading.enumerate() if t.name.startswith("myradio-doctor") and t.is_alive()]


class Gate:
    """Lets a test hold a fake check open and release it at the end so no thread outlives the test."""

    def __init__(self, case):
        self.event = threading.Event()
        case.addCleanup(self.finish)

    def finish(self):
        self.event.set()
        for thread in list(doctor_threads()):
            thread.join(3)

    def spec(self, check_id="stuck"):
        def check(settings=None):
            self.event.wait(30)
            return {"id": check_id, "title": "Stuck", "status": "ok", "detail": "finally", "hint": "", "fix": ""}
        return CheckSpec(check_id, "Stuck", check)


# Every fake program that must be stopped ends itself after 20 seconds (SIGALRM) even if the doctor failed to stop it.
HEARTBEAT = '''
import pathlib, signal, sys, time
signal.alarm(20)
beat = pathlib.Path(sys.argv[1])
n = 0
while True:
    n += 1
    beat.write_text(str(n))
    time.sleep(0.03)
'''


def alive_after(heartbeat, settle=0.25, window=0.2):
    """True when a program is still updating its heartbeat file, that is, it is still running.

    A heartbeat that never appeared means the program never got going, which counts as not running.
    """
    time.sleep(settle)
    try:
        first = Path(heartbeat).read_text()
    except OSError:
        return False
    time.sleep(window)
    try:
        return Path(heartbeat).read_text() != first
    except OSError:
        return False


def wait_for_files(paths, seconds=10.0):
    """Wait until every path exists (a fake program has started and written its marker)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if all(Path(path).exists() for path in paths):
            return True
        time.sleep(0.02)
    return all(Path(path).exists() for path in paths)
