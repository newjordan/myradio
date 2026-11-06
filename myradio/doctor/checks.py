"""Read-only checks that answer "Is your computer ready for myradio?".

Each ``check_*`` function looks at one thing and returns a plain dictionary::

    {"id", "title", "status", "detail", "hint", "fix"}

``status`` is one of ``ok``, ``warn``, ``fail`` or ``unknown``. ``detail`` says what was found,
``hint`` says what it means and what to do next (always filled for anything that is not ``ok``),
and ``fix`` is optional instruction text for this kind of computer, for example the Homebrew
line on a Mac or the apt line on Debian-based Linux. A ``fix`` is only ever shown to the
person. It is never run.

The checks only look. They never install, download, write, delete, change permissions, ask for a
password or reach the internet. Other programs are started only through ``tools.run_tool`` (an
argument list, a short deadline, a bounded read). A check reports what it detects, not what it
assumes: a graphics device that is found is "detected", never "music generation works".

The checks are written to be called through ``report.ReportRunner``, which also guarantees that
nothing here can raise into the caller or hold the report up. Each function still tries to
handle its own failures and turn them into an ``unknown`` result with a plain explanation.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import errno
import functools
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import sys
import threading
import time
from typing import Callable, Optional

from .tools import ToolResult, clean_text, display_path, run_tool

LOG = logging.getLogger("myradio.doctor")
STATES = ("ok", "warn", "fail", "unknown")
DEFAULT_PORT = 8765
MAX_TEXT = 400

MIN_PYTHON = (3, 10)
TESTED_PYTHON_MAX = (3, 13)
SQLITE_MIN = (3, 8, 8)  # WAL, foreign key checks and truncating checkpoints, which the station and backup code use
DISK_WARN_BYTES = 2 * 1024 ** 3
DISK_FAIL_BYTES = 200 * 1024 ** 2
AUDIO_BYTES_PER_HOUR = 44100 * 2 * 2 * 3600  # 44.1 kHz, 16-bit, stereo: what the station stores
MEMORY_WARN_BYTES = 4 * 1024 ** 3
MEMORY_LOW_AVAILABLE_BYTES = 512 * 1024 ** 2
TOOL_TIMEOUT = 3.0
PROFILER_TIMEOUT = 4.0
CLOCK_EARLIEST = 1704067200.0  # 2024-01-01 UTC: before this software existed, so a clock earlier than this is wrong
CLOCK_LATEST = 4102444800.0  # 2100-01-01 UTC
SAMPLE_ZONES = ("UTC", "America/New_York", "Europe/London", "Asia/Tokyo")
# Where well-known installers put FFmpeg. Only used to explain "installed, but not on the search path".
KNOWN_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/snap/bin")
# Where macOS keeps its own programs, for when a program was started with a short search path (for example from the Finder).
SYSTEM_TOOL_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")

# Said on every Mac row. A Mac is detected, never certified, so those rows ask for a look (warn) instead of saying "ready".
MAC_NOTE = "myradio can detect this Mac, but Mac support has not been verified yet."
MAC_HINT = ("Nothing here needs fixing. myradio has not been checked on real Macs yet, so this only shows what was detected and cannot "
            "promise that everything works. If something does not work later, say which Mac and which macOS you have when you ask for help.")

# Shown instead of a command when a command would be unsafe or too long to copy whole (see ``_command_fix``).
GENERAL_FOLDER_FIX = "Use a folder just for your station and start myradio with, for example: myradio --data-dir ~/.myradio"
# Folder privacy is judged from permission bits, and some drives (USB sticks, network drives) cannot keep any.
DRIVE_CANNOT_BE_PRIVATE = ("If the folder is on a USB stick or a network drive, that drive may not be able to keep it private. "
                           "Choose a folder on this computer's own drive instead.")

# What myradio's import and going-live paths need from FFmpeg (see importing.py and broadcast.py).
MP3_ENCODER = "libmp3lame"
IMPORT_DECODERS = (
    ("MP3", frozenset({"mp3", "mp3float"})),
    ("FLAC", frozenset({"flac"})),
    ("Ogg", frozenset({"vorbis", "libvorbis"})),
    ("AAC and M4A", frozenset({"aac", "aac_fixed", "libfdk_aac"})),
    ("AIFF", frozenset({"pcm_s16be"})),
)

TITLES = {
    "python": "Audio engine",
    "ffmpeg": "FFmpeg",
    "disk-space": "Disk space",
    "data-folder": "Station folder",
    "loopback": "Private by default",
    "port": "Control page port",
    "system": "Operating system",
    "sqlite": "Station database",
    "fcntl": "One copy at a time",
    "tzdata": "Timezone names",
    "clock": "Clock and timezone",
    "memory": "Memory",
    "cpu": "Processor",
    "accelerator": "Graphics hardware",
}


@dataclasses.dataclass(frozen=True)
class Settings:
    """What to look at. The defaults describe the real computer; tests point the roots elsewhere."""

    data_dir: Optional[str] = None  # None: MYRADIO_DATA_DIR, else ~/.myradio (the same default the service uses)
    port: int = DEFAULT_PORT
    in_service: bool = False  # True when the doctor runs inside the running station
    tool_timeout: float = TOOL_TIMEOUT
    profiler_timeout: float = PROFILER_TIMEOUT
    dev_root: str = "/dev"
    sys_root: str = "/sys"
    proc_root: str = "/proc"
    etc_root: str = "/etc"
    now: Callable[[], float] = time.time
    cancel: Optional[threading.Event] = None


@dataclasses.dataclass(frozen=True)
class CheckSpec:
    id: str
    title: str
    func: Callable[[Optional[Settings]], dict]


def _result(check_id: str, status: str, detail: str, hint: str = "", fix: str = "") -> dict:
    return {"id": check_id, "title": TITLES[check_id], "status": status,
            "detail": clean_text(detail, MAX_TEXT), "hint": clean_text(hint, MAX_TEXT),
            "fix": clean_text(fix, MAX_TEXT, collapse=False)}


def _guarded(check_id: str):
    """A check that hits something unexpected reports ``unknown`` instead of raising."""
    def wrap(func):
        @functools.wraps(func)
        def inner(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                LOG.debug("doctor check %s failed", check_id, exc_info=True)
                return _result(check_id, "unknown", "This check hit an unexpected problem and was skipped.",
                               "Run the check again. If it keeps happening, copy this report when you ask for help.")
        return inner
    return wrap


def _size(count: float) -> str:
    """A size in plain words, always rounded down.

    Rounding down means a figure just under a limit can never be shown as the limit itself ("Only 2.0 GB
    free" next to a limit of 2 GB). Here 1 GB is 1,024 MB, as on Linux; the Finder on a Mac counts 1,000.
    """
    if count >= 1024 ** 3:
        tenths = int(count * 10 // 1024 ** 3)
        return f"{tenths // 10}.{tenths % 10} GB"
    return f"{int(count // 1024 ** 2)} MB"


def _audio_time(free_bytes: float) -> str:
    hours = free_bytes / AUDIO_BYTES_PER_HOUR
    if hours >= 10:
        return f"about {hours:.0f} hours"
    if hours >= 1:
        return f"about {hours:.1f} hours"
    return f"about {max(0, round(hours * 60))} minutes"


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _settings(settings: Optional[Settings]) -> Settings:
    return settings if isinstance(settings, Settings) else Settings()


def _tool(s: Settings, args, *, timeout: Optional[float] = None, max_bytes: Optional[int] = None) -> ToolResult:
    kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
    return run_tool(args, timeout=s.tool_timeout if timeout is None else timeout, cancel=s.cancel, **kwargs)


def _run_together(s: Settings, calls: dict, timeout: float) -> dict:
    """Run several tool calls at the same time, so the slowest one sets the wait. ``{name: [args]}`` in, ``{name: ToolResult}`` out.

    Every call is bounded by ``run_tool`` itself, so these helper threads always end on their own.
    """
    results: dict = {}

    def work(name, args):
        try:
            results[name] = run_tool(args, timeout=timeout, cancel=s.cancel)
        except Exception:  # run_tool never raises; this only guards a stand-in that does
            results[name] = ToolResult(reason="failed")

    threads = []
    for name, args in calls.items():
        thread = threading.Thread(target=work, args=(name, args), name="myradio-doctor-tool", daemon=True)
        try:
            thread.start()
            threads.append(thread)
        except RuntimeError:  # no thread to spare: do this one here instead
            work(name, args)
    for thread in threads:
        thread.join(timeout + 3.0)
    return {name: results.get(name, ToolResult(timed_out=True, reason="timeout")) for name in calls}


# ------------------------------------------------------------------ platform

def _os_release() -> dict:
    try:
        return dict(platform.freedesktop_os_release())
    except Exception:
        return {}


def platform_family() -> str:
    """"macos", "debian" (including Ubuntu and Mint), "arch", "linux" (any other), "windows" or "other"."""
    try:
        system = platform.system()
    except Exception:
        return "other"
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    if system == "Linux":
        release = _os_release()
        ids = {str(release.get("ID", "")).lower(), *str(release.get("ID_LIKE", "")).lower().split()}
        if ids & {"debian", "ubuntu"}:
            return "debian"
        if "arch" in ids:
            return "arch"
        return "linux"
    return "other"


def resolve_data_dir(settings: Optional[Settings] = None) -> str:
    """The station folder the checks look at (same default as the service: ``~/.myradio``)."""
    s = _settings(settings)
    raw = s.data_dir or os.environ.get("MYRADIO_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".myradio")
    return os.path.abspath(os.path.expanduser(raw))


def _too_broad(path: str) -> bool:
    """A folder that is too general to tell someone to lock down (the home folder, "/", "/tmp"...)."""
    normal = os.path.normpath(path)
    if len([part for part in normal.split(os.sep) if part]) <= 1:
        return True
    try:
        home = os.path.normpath(os.path.expanduser("~"))
    except Exception:
        return False
    return home not in (".", "~") and (normal == home or home.startswith(normal + os.sep))


def _shell_path(path: str) -> str:
    """The path as it should be written in a command the person copies (home folder kept as ~)."""
    home = os.path.expanduser("~")
    if home not in ("~", "/") and path.startswith(home.rstrip("/") + "/"):
        return "~/" + shlex.quote(path[len(home.rstrip("/")) + 1:])
    return shlex.quote(path)


def _safe_for_command(path: str) -> bool:
    """False when showing the path in a command would have to change it (control characters)."""
    return clean_text(path, 4096, collapse=False) == path


def _inside_home(path: str) -> bool:
    """True when the path lies inside the person's own home folder (not the home folder itself)."""
    try:
        home = os.path.normpath(os.path.expanduser("~"))
    except Exception:
        return False
    return home not in (".", "~", os.sep) and os.path.normpath(path).startswith(home.rstrip(os.sep) + os.sep)


def _command_fix(prefix: str, path: str) -> str:
    """``prefix`` followed by the path quoted for a shell, or "" when that is not safe to hand to a person.

    A command is only ever shown whole: never changed (control characters), never aimed at a folder that is
    too general to touch, and never cut short. A command cut in the middle of a quoted path would leave the
    terminal waiting for the closing quote, so a command that does not fit in the text limit is dropped and
    the caller shows a general sentence instead.
    """
    if not _safe_for_command(path) or _too_broad(path):
        return ""
    command = prefix + _shell_path(path)
    return command if len(command) <= MAX_TEXT else ""


# ------------------------------------------------------------------- python

@_guarded("python")
def check_python(settings: Optional[Settings] = None, *, version=None, implementation=None) -> dict:
    """Python version and implementation (``version`` and ``implementation`` exist for tests)."""
    version = tuple(version if version is not None else sys.version_info[:3])
    implementation = implementation or platform.python_implementation()
    text = ".".join(str(part) for part in version[:3])
    if version[:2] < MIN_PYTHON:
        family = platform_family()
        if family == "macos":
            fix = "Download the macOS installer from https://www.python.org/downloads/macos/ and run it, then start myradio with the new Python."
        elif family == "debian":
            fix = "Open a terminal and run: sudo apt install python3  (if that is still older than 3.10, use the installer from https://www.python.org/downloads/)"
        else:
            fix = "Download a current Python from https://www.python.org/downloads/ and use it to start myradio."
        return _result("python", "fail", f"Python {text} is too old.",
                       "myradio needs Python 3.10 or newer. Install a newer Python, then start myradio again.", fix)
    if implementation != "CPython":
        return _result("python", "warn", f"This is Python {text} from {implementation}, not the standard CPython.",
                       "myradio has only been tried with the standard Python from python.org. It may work. If you see odd problems, switch to CPython 3.10 to 3.13.")
    if version[:2] > TESTED_PYTHON_MAX:
        return _result("python", "warn", f"Python {text} is newer than the versions myradio has been tried with (3.10 to 3.13).",
                       "It will probably work. If something misbehaves, install Python 3.12 and start myradio with it.")
    return _result("python", "ok", f"Python {text} (CPython) is installed and ready.")


# ------------------------------------------------------------------- system

def _system_tool(name: str) -> Optional[str]:
    """A system program by name.

    On a Mac the fixed system folders are tried first, so a program of the same name that happens to sit
    earlier on the search path is never run in place of the real one; the search path is the fallback.
    """
    try:
        on_a_mac = platform.system() == "Darwin"
    except Exception:
        on_a_mac = False
    if on_a_mac:
        for folder in SYSTEM_TOOL_DIRS:
            candidate = os.path.join(folder, name)
            try:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
            except Exception:
                continue
    return shutil.which(name)


def _sysctl_translated(s: Settings):
    """The command that tells Rosetta from an Intel Mac, or None when it is not needed or not available."""
    if platform.machine().lower() not in ("x86_64", "amd64", "i386"):
        return None
    tool = _system_tool("sysctl")
    return [tool, "-n", "sysctl.proc_translated"] if tool else None


def _mac_kind(s: Settings, translated: Optional[ToolResult] = None) -> str:
    """"arm" (native Apple silicon), "rosetta" (Intel Python on Apple silicon), "intel" or "unknown".

    ``translated`` is the answer from the command in ``_sysctl_translated`` (run it first when it is needed).
    """
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm"
    if machine in ("x86_64", "amd64", "i386") and translated is not None and translated.ran and not translated.timed_out:
        answer = translated.output.strip()
        if translated.exit_code == 0 and answer == "1":
            return "rosetta"
        if (translated.exit_code == 0 and answer == "0") or "unknown oid" in answer.lower():
            return "intel"
    return "unknown"


def _linux_name() -> str:
    release = _os_release()
    name = str(release.get("PRETTY_NAME") or release.get("NAME") or "").strip()
    return name or "Linux"


@_guarded("system")
def check_system(settings: Optional[Settings] = None) -> dict:
    """Operating system, processor type, Apple silicon and Rosetta."""
    s = _settings(settings)
    system = platform.system()
    machine = platform.machine().lower()
    arch = {"x86_64": "x86-64", "amd64": "x86-64", "aarch64": "ARM64", "arm64": "ARM64"}.get(machine, machine or "an unknown processor")
    if system == "Windows":
        return _result("system", "fail", "This is Windows, which myradio does not support.",
                       "myradio runs on macOS and Linux. It needs file-locking features that Windows does not have.",
                       "Run myradio on a Mac or a Linux computer.")
    if system == "Darwin":
        version = platform.mac_ver()[0] or "(unknown version)"
        note = MAC_NOTE
        command = _sysctl_translated(s)
        kind = _mac_kind(s, run_tool(command, timeout=min(s.tool_timeout, 2.0), cancel=s.cancel) if command else None)
        # A Mac is never "ready" here: it is detected, and detection is not proof that myradio runs on it.
        if kind == "arm":
            return _result("system", "warn", f"macOS {version} on an Apple silicon Mac. {note}", MAC_HINT)
        if kind == "rosetta":
            return _result("system", "warn",
                           f"macOS {version} on an Apple silicon Mac, but this copy of Python is the Intel version running through Rosetta (Apple's translation layer). {note}",
                           "It should still work but will run slower, and tools that look for your Mac's chip may not see it. Install the Apple silicon version of Python and set myradio up with it again.",
                           "Download the 'universal2' macOS installer from https://www.python.org/downloads/macos/ , which runs natively on Apple silicon.")
        if kind == "intel":
            return _result("system", "warn", f"macOS {version} on an Intel Mac. {note}", MAC_HINT)
        return _result("system", "unknown", f"macOS {version} ({arch}). It could not be worked out whether this is an Intel Mac or an Apple silicon Mac. {note}",
                       "This does not stop myradio from running. Run the check again in a moment.")
    if system == "Linux":
        name, kernel = _linux_name(), clean_text(platform.release(), 60)
        label = f"Linux {kernel} on {arch}" if name == "Linux" else f"{name} (Linux {kernel}) on {arch}"
        if machine in ("x86_64", "amd64", "aarch64", "arm64"):
            return _result("system", "ok", f"{label}.")
        return _result("system", "warn", f"{label}.",
                       "myradio is meant for 64-bit Intel/AMD and ARM computers. Other kinds have not been tried and may not work.")
    return _result("system", "warn", f"{clean_text(system, 40) or 'This operating system'} on {arch}.",
                   "myradio is only set up for macOS and Linux. This system has not been tried and may not work.")


# ----------------------------------------------------------------- timezone

def _install_tzdata_fix() -> str:
    family = platform_family()
    pip = f"{_shell_path(sys.executable or 'python3')} -m pip install tzdata"
    if family == "debian":
        return "Open a terminal and run: sudo apt install tzdata"
    if family == "arch":
        return "Open a terminal and run: sudo pacman -S tzdata"
    return f"Open a terminal and run: {pip}"


@_guarded("tzdata")
def check_tzdata(settings: Optional[Settings] = None) -> dict:
    """Do timezone names such as America/New_York load (needed for schedules in local time)?"""
    try:
        import zoneinfo
    except ImportError:
        return _result("tzdata", "warn", "This copy of Python has no timezone support.",
                       "Schedules at local times will not work. Install a standard Python from python.org.",
                       "Download a current Python from https://www.python.org/downloads/ and use it to start myradio.")
    loaded, missing = [], []
    for name in SAMPLE_ZONES:
        try:
            zoneinfo.ZoneInfo(name)
            loaded.append(name)
        except Exception:
            missing.append(name)
    if not loaded:
        return _result("tzdata", "warn", "No timezone information was found on this computer.",
                       "Schedules at a local time need it (UTC schedules need it too). Install it, then restart myradio.",
                       _install_tzdata_fix())
    if missing:
        return _result("tzdata", "warn", f"Some timezone names could not be loaded: {', '.join(missing)}.",
                       "Schedules in those timezones will not work. Reinstall the timezone information, then restart myradio.",
                       _install_tzdata_fix())
    try:
        zone = zoneinfo.ZoneInfo("America/New_York")
        winter = dt.datetime(2026, 1, 15, 12, tzinfo=zone).utcoffset()
        summer = dt.datetime(2026, 7, 15, 12, tzinfo=zone).utcoffset()
        if winter == summer:
            return _result("tzdata", "warn", "Timezone information loaded, but it does not show summer and winter time changes.",
                           "Schedules could fire an hour early or late after a clock change. Reinstall the timezone information, then restart myradio.",
                           _install_tzdata_fix())
    except Exception:
        return _result("tzdata", "warn", "Timezone information loaded, but it could not be read properly.",
                       "Reinstall the timezone information, then restart myradio.", _install_tzdata_fix())
    return _result("tzdata", "ok", "Timezone names such as America/New_York load correctly, so schedules can be set in local time.")


# ------------------------------------------------------------------- FFmpeg

def _find_tool(name: str):
    """(path, None) when on the search path, (None, path) when installed in a usual place only, else (None, None)."""
    found = shutil.which(name)
    if found:
        return found, None
    for folder in KNOWN_TOOL_DIRS:
        candidate = os.path.join(folder, name)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return None, candidate
        except Exception:
            continue
    return None, None


def _ffmpeg_fix(*, reinstall: bool = False) -> str:
    family = platform_family()
    if family == "macos":
        return ("Open Terminal and run: brew reinstall ffmpeg" if reinstall else
                "Open Terminal and run: brew install ffmpeg  (Homebrew is a free installer from https://brew.sh )")
    if family == "debian":
        return "Open a terminal and run: sudo apt install ffmpeg"
    if family == "arch":
        return "Open a terminal and run: sudo pacman -S ffmpeg"
    return "Install the full version of FFmpeg with your system's package manager, or get it from https://ffmpeg.org/download.html"


_CODEC_LINE = re.compile(r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_]+)(?:\s|$)")


def parse_codec_list(text: str) -> set:
    """Names from ``ffmpeg -encoders`` or ``-decoders`` output (the key legend lines are ignored)."""
    names = set()
    for line in str(text).splitlines()[:20000]:
        found = _CODEC_LINE.match(line)
        if found:
            names.add(found.group(1))
    return names


def _tool_version(output: str, program: str) -> Optional[str]:
    """The version a program names in its banner: the first line that begins "<program> version X".

    Some systems print a warning or notice before the banner, so every line is tried, not only the first.
    Only blanks and tabs may come before the name on a line (white space that spans lines could make the
    search slow on a flood of empty lines), and only the first 64 KiB of the output is searched.
    """
    found = re.search(rf"^[ \t\r]*{re.escape(program)} version (\S+)", str(output)[:65536], re.MULTILINE)
    return clean_text(found.group(1), 40) if found else None


@_guarded("ffmpeg")
def check_ffmpeg(settings: Optional[Settings] = None) -> dict:
    """FFmpeg and ffprobe: present, version, an MP3 encoder (going live) and decoders (importing)."""
    s = _settings(settings)
    path, elsewhere = _find_tool("ffmpeg")
    if path is None:
        if elsewhere:
            folder = os.path.dirname(elsewhere)
            return _result("ffmpeg", "warn",
                           f"FFmpeg is installed at {display_path(elsewhere)}, but myradio cannot find it because that folder is not on this computer's program search path.",
                           "Start myradio from a Terminal window where the 'ffmpeg' command works, or add that folder to your PATH, then restart myradio.",
                           f"Add this line to your shell's startup file, then open a new Terminal: export PATH={shlex.quote(folder)}:\"$PATH\"")
        return _result("ffmpeg", "warn", "FFmpeg was not found on this computer.",
                       "FFmpeg is a free tool that lets myradio read MP3, FLAC and other audio files and send your station to a streaming host. "
                       "Without it you can still add WAV files and play your station here. Install it, then restart myradio.",
                       _ffmpeg_fix())
    shown = display_path(path)
    calls = {"version": [path, "-version"], "encoders": [path, "-hide_banner", "-encoders"], "decoders": [path, "-hide_banner", "-decoders"]}
    probe_path = shutil.which("ffprobe")
    if probe_path:
        calls["probe"] = [probe_path, "-version"]
    found = _run_together(s, calls, s.tool_timeout)
    first = found["version"]
    if first.cancelled:
        return _result("ffmpeg", "unknown", "The check was stopped before FFmpeg answered.", "Run the check again.")
    if first.timed_out:
        return _result("ffmpeg", "unknown", f"FFmpeg is installed at {shown} but did not answer within {s.tool_timeout:g} seconds.",
                       "Your computer may be busy, or a security program may be slowing FFmpeg down. Run the check again in a minute.")
    if not first.ran:
        return _result("ffmpeg", "warn", f"FFmpeg is installed at {shown} but could not be started.",
                       "The file may be damaged or not allowed to run. Reinstall FFmpeg, then restart myradio.", _ffmpeg_fix(reinstall=True))
    version = _tool_version(first.output, "ffmpeg")
    if first.exit_code != 0 or version is None:
        return _result("ffmpeg", "warn", f"The program called ffmpeg at {shown} did not answer like FFmpeg.",
                       "Another program may be using the name ffmpeg. Reinstall FFmpeg, then restart myradio.", _ffmpeg_fix(reinstall=True))

    if probe_path:
        probe = found["probe"]
        probe_version = _tool_version(probe.output, "ffprobe") if probe.ran and probe.exit_code == 0 else None
        probe_note = (f" Its companion ffprobe {probe_version} is there too." if probe_version else
                      " Its companion tool ffprobe is installed but did not answer; myradio does not need it today.")
    else:
        probe_note = " Its companion tool ffprobe was not found; myradio does not need it today."

    def listing(call: ToolResult):
        """(names, complete). A list that was cut short can prove something is present, never that it is absent."""
        if call.ran and call.exit_code == 0 and not call.timed_out and not call.cancelled:
            return parse_codec_list(call.output), not call.truncated
        return None, False

    encoders, encoders_complete = listing(found["encoders"])
    decoders, decoders_complete = listing(found["decoders"])
    problems = []
    if encoders_complete and MP3_ENCODER not in encoders:
        problems.append("cannot make MP3 audio, which going live needs")
    missing = [label for label, names in IMPORT_DECODERS if decoders_complete and not (names & decoders)]
    if missing:
        problems.append("cannot read " + ", ".join(missing) + " files")
    unclear = not (encoders_complete and decoders_complete)
    if problems:
        extra = " The rest of its abilities could not be listed." if unclear else ""
        return _result("ffmpeg", "warn", f"FFmpeg {version} is installed at {shown}, but it {' and it '.join(problems)}.{extra}{probe_note}",
                       "Install a full build of FFmpeg, one that includes MP3 support (called LAME) and the common audio formats, then restart myradio. "
                       "Until then, going live and some imports will not work. You can still add WAV files.",
                       _ffmpeg_fix(reinstall=True))
    if unclear:
        return _result("ffmpeg", "unknown", f"FFmpeg {version} is installed at {shown}, but the list of audio formats it can handle could not be read.{probe_note}",
                       "Your computer may be busy. Run the check again in a minute.")
    return _result("ffmpeg", "ok", f"FFmpeg {version} is installed. Its own list says it can read MP3, FLAC, Ogg, AAC/M4A and AIFF files "
                                   f"and make MP3 audio for going live. No real file was tried.{probe_note}")


# -------------------------------------------------------- station folder, disk

def _nearest_existing(path: str) -> str:
    current = path
    for _ in range(512):
        try:
            os.stat(current)
            return current
        except (FileNotFoundError, NotADirectoryError):
            parent = os.path.dirname(current)
            if parent == current:
                return current
            current = parent
    return current


def _out_of_resources(exc: BaseException) -> bool:
    """True for "this program has run out of files or memory right now": a reason to say unknown, not that the computer is wrong."""
    return getattr(exc, "errno", None) in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM)


def _os_reason(exc: BaseException) -> str:
    """A short plain reason for an operating-system error (never the raw error text)."""
    code = getattr(exc, "errno", None)
    if isinstance(exc, PermissionError):
        return "permission was denied"
    if isinstance(exc, FileNotFoundError):
        return "it was not found"
    if code is not None:
        try:
            return clean_text(os.strerror(code), 80).lower() or "the computer reported a problem"
        except Exception:
            pass
    return "the computer reported a problem"


@_guarded("disk-space")
def check_disk_space(settings: Optional[Settings] = None) -> dict:
    """Free space where the station keeps its audio (warn under 2 GB, fail under 200 MB)."""
    s = _settings(settings)
    try:
        path = resolve_data_dir(s)
    except Exception:
        return _result("disk-space", "unknown", "The station folder could not be worked out.", "Start myradio with --data-dir and a folder you choose, then run the check again.")
    try:
        where = _nearest_existing(path)
        usage = shutil.disk_usage(where)
        free = int(usage.free)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        return _result("disk-space", "unknown", f"The free space for {display_path(path)} could not be measured ({_os_reason(exc)}).",
                       "Check that the drive is connected and that you may open it, then run the check again.")
    if free < 0:
        return _result("disk-space", "unknown", "The free space could not be measured.", "Run the check again.")
    shown = display_path(os.path.normpath(where))
    room = f"{_size(free)} free where myradio keeps your station ({shown}), room for {_audio_time(free)} of CD-quality audio."
    advice = "To use a roomier drive, start myradio with --data-dir and a folder on that drive."
    if free < DISK_FAIL_BYTES:
        return _result("disk-space", "fail", f"Only {_size(free)} free where myradio keeps your station ({shown}).",
                       "myradio may not be able to save new audio, and the station could stop. Free up space now by emptying the trash or removing big files, or move to a roomier drive. " + advice,
                       "Free up space, or start myradio with: myradio --data-dir <a folder on a roomier drive>")
    if free < DISK_WARN_BYTES:
        return _result("disk-space", "warn", f"Only {_size(free)} free where myradio keeps your station ({shown}), room for {_audio_time(free)} of CD-quality audio.",
                       "That is enough to start, but your library will fill it quickly. Free up space soon. " + advice)
    return _result("disk-space", "ok", room)


@_guarded("data-folder")
def check_data_folder(settings: Optional[Settings] = None) -> dict:
    """The station folder: can myradio use it, and is it private to this account?"""
    s = _settings(settings)
    try:
        path = resolve_data_dir(s)
    except Exception:
        return _result("data-folder", "unknown", "The station folder could not be worked out.", "Start myradio with --data-dir and a folder you choose, then run the check again.")
    shown = display_path(path)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return _missing_folder(path, shown)
    except NotADirectoryError:
        return _result("data-folder", "fail", f"Part of the path {shown} is a file, not a folder.",
                       "myradio cannot make its station folder there. Choose a different folder with --data-dir.")
    except PermissionError:
        return _result("data-folder", "fail", f"This account is not allowed to look at {shown}.",
                       "myradio cannot use a folder it may not open. Choose a folder in your own home with --data-dir.")
    except ValueError:
        return _result("data-folder", "fail", "That is not a usable folder name.", "Choose a different folder with --data-dir.")
    except OSError as exc:
        return _result("data-folder", "unknown", f"The folder {shown} could not be examined ({_os_reason(exc)}).",
                       "Check that the drive is connected, then run the check again.")
    if not stat.S_ISDIR(info.st_mode):
        return _result("data-folder", "fail", f"{shown} is a file, not a folder.", "myradio needs a folder for your station. Choose a different one with --data-dir.")
    try:
        usable = os.access(path, os.R_OK | os.W_OK | os.X_OK)
    except Exception:
        usable = False
    if not usable:
        # Taking a folder over with sudo is offered only for a folder inside the person's own home (the usual case: ~/.myradio
        # made once by running myradio with sudo). A system folder such as /usr/local gets no such advice.
        take_back = (_command_fix("Take the folder back by running: sudo chown -R \"$USER\" ", path)
                     if _belongs_to_administrator(info) and _inside_home(path) else "")
        if take_back:
            return _result("data-folder", "fail", f"myradio cannot read and save in {shown}.",
                           "The folder belongs to the administrator account, probably because myradio was once run as an administrator. "
                           "Take it back, or choose a different folder with --data-dir.", take_back)
        return _result("data-folder", "fail", f"myradio cannot read and save in {shown}.",
                       "The folder belongs to another account, or its settings do not let this account save in it. Choose a different folder with --data-dir.",
                       "Start myradio with a folder of your own, for example: myradio --data-dir ~/.myradio")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007:
        return _result("data-folder", "fail", f"The station folder {shown} can be opened by other accounts on this computer.",
                       "It holds your library and settings, so it should be private. Make it so only your account can open it. " + DRIVE_CANNOT_BE_PRIVATE,
                       _command_fix("Open a terminal and run: chmod 700 ", path) or GENERAL_FOLDER_FIX)
    if mode & 0o070:
        return _result("data-folder", "warn", f"The station folder {shown} can also be opened by other people in its group.",
                       "If you share this computer with other people, make the folder so only your account can open it. " + DRIVE_CANNOT_BE_PRIVATE,
                       _command_fix("Open a terminal and run: chmod 700 ", path) or GENERAL_FOLDER_FIX)
    note = ""
    try:
        real = os.path.realpath(path)
        if real != path:
            note = f" (a link to {display_path(real)})"
    except Exception:
        pass
    return _result("data-folder", "ok", f"Your station folder {shown}{note} is closed to other accounts on this computer, and myradio can save in it.")


def _belongs_to_administrator(info) -> bool:
    """True when the folder is owned by root and this program is not: the usual result of running myradio with sudo once."""
    try:
        return info.st_uid == 0 and os.geteuid() != 0
    except (AttributeError, OSError):
        return False


def _missing_folder(path: str, shown: str) -> dict:
    try:
        parent = _nearest_existing(path)
        info = os.stat(parent)
    except OSError as exc:
        return _result("data-folder", "fail", f"The place where {shown} would be made cannot be opened ({_os_reason(exc)}).",
                       "Choose a folder in your own home with --data-dir.")
    if not stat.S_ISDIR(info.st_mode) or not os.access(parent, os.W_OK | os.X_OK):
        return _result("data-folder", "fail", f"myradio would not be allowed to create {shown}.",
                       "Choose a folder in your own home with --data-dir, or fix who owns the place where it would be made.")
    return _result("data-folder", "ok", f"Your station folder {shown} does not exist yet. myradio makes it, closed to other accounts, the first time it starts.")


# --------------------------------------------------------- private by default

def _bind(port: int) -> int:
    """Bind a TCP socket on this computer only, the way the service does, and let go of it at once."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return sock.getsockname()[1]
    finally:
        sock.close()


@_guarded("loopback")
def check_loopback(settings: Optional[Settings] = None) -> dict:
    """Can myradio open its private control page (a connection only this computer can use)?"""
    try:
        _bind(0)
    except OSError as exc:
        if _out_of_resources(exc):
            return _result("loopback", "unknown", f"The private connection could not be tried because this program has run out of room to open more ({_os_reason(exc)}).",
                           "Close some programs and run the check again.")
        return _result("loopback", "fail", f"This computer would not let myradio open a private connection to itself ({_os_reason(exc)}).",
                       "myradio keeps its controls on this computer only, so it needs this. A firewall or security program is probably blocking it. "
                       "Allow Python to make connections on this computer, then run the check again.")
    return _result("loopback", "ok", "This computer lets myradio keep its control page on this computer only, so it is not shared on your network.")


@_guarded("port")
def check_port(settings: Optional[Settings] = None) -> dict:
    """Is the control page's port free (skipped, with an explanation, while myradio itself is running)?"""
    s = _settings(settings)
    port = s.port
    if s.in_service:
        return _result("port", "ok", "myradio is running right now, so its control page port is expected to be in use by myradio itself. This check was skipped.")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        return _result("port", "unknown", "That is not a valid port number.", "Use a number from 1 to 65535, for example 8765.")
    if port == 0:
        return _result("port", "ok", "myradio will pick any free port on its own.")
    other = port + 1 if port < 65535 else port - 1
    try:
        _bind(port)
    except PermissionError:
        if port < 1024:
            return _result("port", "warn", f"This computer does not allow myradio to use port {port}.",
                           "Ports below 1024 are reserved. Start myradio on a higher one.",
                           f"Start myradio with another port, for example: myradio --port {DEFAULT_PORT}")
        return _result("port", "warn", f"This computer does not allow myradio to use port {port}.",
                       "A firewall or security program may be blocking that port. Allow Python to use it, or start myradio on a different one.",
                       f"Start myradio with another port, for example: myradio --port {other}")
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EADDRINUSE:
            return _result("port", "warn", f"Port {port} is already in use.",
                           f"If myradio is already running, just open http://127.0.0.1:{port}/ in your browser. Otherwise another program is using the port: start myradio on a different one.",
                           f"Start myradio with another port, for example: myradio --port {other}")
        return _result("port", "unknown", f"Port {port} could not be tested ({_os_reason(exc)}).", "Run the check again. If it keeps happening, start myradio on a different port.")
    return _result("port", "ok", f"Port {port} is free, so myradio can open its control page there.")


# ------------------------------------------------------------ database, locks

@_guarded("sqlite")
def check_sqlite(settings: Optional[Settings] = None) -> dict:
    """The built-in database: new enough, with the safe-write mode (WAL) and linked tables the station uses."""
    try:
        import sqlite3
    except ImportError:
        return _result("sqlite", "fail", "This copy of Python was built without its built-in database.",
                       "myradio keeps your station in it, so it cannot run. Install Python from python.org or from your system's package manager.",
                       "Download a current Python from https://www.python.org/downloads/ and use it to start myradio.")
    version = clean_text(getattr(sqlite3, "sqlite_version", "unknown"), 30)
    try:
        info = tuple(int(part) for part in sqlite3.sqlite_version_info)
    except Exception:
        info = ()
    if not info:
        return _result("sqlite", "unknown", "The version of the built-in database could not be read.", "Run the check again.")
    needed = ".".join(str(part) for part in SQLITE_MIN)
    if info < SQLITE_MIN:
        return _result("sqlite", "fail", f"The built-in database (SQLite {version}) is too old.",
                       f"myradio needs SQLite {needed} or newer to keep your station safe if the power goes out. A newer Python brings a newer one.",
                       "Install a current Python from https://www.python.org/downloads/ and use it to start myradio.")
    try:
        connection = sqlite3.connect(":memory:")
        try:
            options = {str(row[0]) for row in connection.execute("PRAGMA compile_options")}
            connection.execute("PRAGMA foreign_keys=ON")
            links = connection.execute("PRAGMA foreign_keys").fetchone()
        finally:
            connection.close()
    except Exception:
        return _result("sqlite", "unknown", f"The built-in database (SQLite {version}) could not be tried out.", "Run the check again.")
    if "OMIT_WAL" in options or not links or links[0] != 1:
        return _result("sqlite", "fail", f"The built-in database (SQLite {version}) was built without features myradio needs.",
                       "myradio needs the safe-write mode and linked tables to keep your station safe. Install Python from python.org or your system's package manager.",
                       "Download a current Python from https://www.python.org/downloads/ and use it to start myradio.")
    return _result("sqlite", "ok", f"The built-in database (SQLite {version}) is new enough and has the safe-write mode and linked tables the station uses.")


@_guarded("fcntl")
def check_fcntl(settings: Optional[Settings] = None) -> dict:
    """File locks, which stop two copies of myradio from using one station folder at once."""
    try:
        import fcntl
    except ImportError:
        return _result("fcntl", "fail", "This computer does not have the file locks myradio relies on.",
                       "myradio uses them so two copies never write to one station folder. It needs macOS or Linux.",
                       "Run myradio on a Mac or a Linux computer.")
    if not all(hasattr(fcntl, name) for name in ("flock", "LOCK_SH", "LOCK_EX", "LOCK_NB", "LOCK_UN")):
        return _result("fcntl", "fail", "This computer's file locks are incomplete.",
                       "myradio uses them so two copies never write to one station folder. It needs macOS or Linux.", "Run myradio on a Mac or a Linux computer.")
    try:
        try:
            handle = open(globals().get("__file__") or os.devnull, "rb")
        except OSError:
            handle = open(os.devnull, "rb")
        with handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as exc:
        if _out_of_resources(exc):
            return _result("fcntl", "unknown", f"File locks could not be tried because this program has run out of room to open more files ({_os_reason(exc)}).",
                           "Close some programs and run the check again.")
        return _result("fcntl", "fail", f"File locking did not work when tried ({_os_reason(exc)}).",
                       "myradio refuses to start without it, so two copies cannot damage one station. Try a different Python, or run myradio on a local drive.")
    return _result("fcntl", "ok", "This computer supports the file locks myradio uses to keep two copies from sharing one station folder.")


# ------------------------------------------------------------ memory and CPU

def _memory_bytes(s: Settings):
    """(total, available) in bytes. Either can be None when this computer does not say."""
    total = available = None
    system = platform.system()
    if system == "Linux":
        try:
            with open(os.path.join(s.proc_root, "meminfo"), "rb") as handle:
                text = handle.read(65536).decode("ascii", "replace")
            for line in text.splitlines():
                key, _, rest = line.partition(":")
                words = rest.split()
                if words and words[0].isdigit():
                    if key == "MemTotal":
                        total = int(words[0]) * 1024
                    elif key == "MemAvailable":
                        available = int(words[0]) * 1024
        except (OSError, ValueError):
            total = available = None
    if total is None:
        try:
            total = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (ValueError, OSError, AttributeError, TypeError):
            total = None
    if total is None and system == "Darwin":
        tool = _system_tool("sysctl")
        if tool:
            found = _tool(s, [tool, "-n", "hw.memsize"], timeout=min(s.tool_timeout, 2.0))
            word = found.output.strip()
            if found.ran and found.exit_code == 0 and word.isdigit():
                total = int(word)
    if total is not None and total <= 0:
        total = None
    if available is not None and (available < 0 or (total is not None and available > total)):
        available = None
    return total, available


@_guarded("memory")
def check_memory(settings: Optional[Settings] = None) -> dict:
    """How much memory the computer has."""
    s = _settings(settings)
    total, available = _memory_bytes(s)
    if total is None:
        return _result("memory", "unknown", "This computer did not say how much memory it has.",
                       "That is fine for playing your own audio. Music and voice setup will tell you what they need.")
    now = f" ({_size(available)} free right now)" if available is not None else ""
    if total < MEMORY_WARN_BYTES:
        return _result("memory", "warn", f"This computer has {_size(total)} of memory{now}.",
                       "myradio itself should run, but the music and voice models are large and usually need much more. Their own setup screens say exactly how much.")
    if available is not None and available < MEMORY_LOW_AVAILABLE_BYTES:
        return _result("memory", "warn", f"This computer has {_size(total)} of memory, but only {_size(available)} is free right now.",
                       "Close programs you are not using, such as extra browser tabs, so the station has room to work.")
    return _result("memory", "ok", f"This computer has {_size(total)} of memory{now}.")


@_guarded("cpu")
def check_cpu(settings: Optional[Settings] = None) -> dict:
    """How many processor cores myradio may use."""
    count = None
    try:
        count = len(os.sched_getaffinity(0))  # respects limits set on the program, unlike cpu_count
    except (AttributeError, OSError, TypeError):
        pass
    if not count:
        try:
            count = os.cpu_count()
        except Exception:
            count = None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return _result("cpu", "unknown", "This computer did not say how many processor cores it has.",
                       "That is fine for playing your own audio.")
    if count == 1:
        return _result("cpu", "warn", "This computer has 1 processor core.",
                       "Playing, sharing and making audio at the same time may stutter. Close other programs while the station runs.")
    return _result("cpu", "ok", f"This computer has {count} processor cores.")


# ----------------------------------------------------------------- graphics

_GPU_VENDORS = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD", "0x1022": "AMD"}
_DETECT_ONLY = ("This only shows what the computer reports. Whether music and voices actually run is decided by their own setup checks.")


def _names(path: str, limit: int) -> list:
    """At most ``limit`` entry names of a folder, sorted (a folder with endless entries cannot use endless memory)."""
    found = []
    with os.scandir(path) as entries:
        for entry in entries:
            found.append(entry.name)
            if len(found) >= limit:
                break
    return sorted(found)


def _read_small(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(64).decode("ascii", "replace").strip()
    except OSError:
        return ""


def _linux_graphics(s: Settings) -> dict:
    nodes, nvidia = [], []
    try:
        names = _names(os.path.join(s.dev_root, "dri"), 64)
    except OSError:
        names = []
    for name in names:
        if re.fullmatch(r"renderD\d{1,3}", name):
            base = os.path.join(s.sys_root, "class", "drm", name, "device")
            vendor = _GPU_VENDORS.get(_read_small(os.path.join(base, "vendor")).lower(), "")
            try:
                driver = os.path.basename(os.readlink(os.path.join(base, "driver")))
            except OSError:
                driver = ""
            try:
                usable = os.access(os.path.join(s.dev_root, "dri", name), os.R_OK | os.W_OK)
            except Exception:
                usable = False
            nodes.append({"name": name, "vendor": vendor, "driver": clean_text(driver, 30), "usable": usable})
    try:
        nvidia = [n for n in _names(s.dev_root, 2048) if re.fullmatch(r"nvidia\d{1,2}", n)]
    except OSError:
        nvidia = []
    tools = [name for name in ("clinfo", "sycl-ls", "nvidia-smi") if shutil.which(name)]  # looked up by name, never run
    return {"nodes": nodes, "nvidia": nvidia, "tools": tools}


def _parse_profiler(text: str):
    """(chip name, graphics cores) from ``system_profiler -json SPDisplaysDataType`` output, or None."""
    try:
        data = json.loads(text)
        entries = data["SPDisplaysDataType"]
        for entry in entries[:4]:
            if isinstance(entry, dict):
                chip = clean_text(entry.get("sppci_model") or entry.get("_name") or "", 60)
                cores = clean_text(entry.get("sppci_cores") or "", 6)
                if chip:
                    return chip, (cores if cores.isdigit() else "")
    except Exception:
        pass
    return None


@_guarded("accelerator")
def check_accelerator(settings: Optional[Settings] = None) -> dict:
    """Graphics and accelerator hints. Detection only: nothing here is called a working capability."""
    s = _settings(settings)
    system = platform.system()
    if system == "Darwin":
        calls = {}
        command = _sysctl_translated(s)
        if command:
            calls["translated"] = command
        program = _system_tool("system_profiler")
        if program:
            calls["profiler"] = [program, "-json", "SPDisplaysDataType"]
        answers = _run_together(s, calls, s.profiler_timeout) if calls else {}
        kind = _mac_kind(s, answers.get("translated"))
        if kind in ("arm", "rosetta"):
            detail = "Detected: an Apple silicon Mac"
            found = answers.get("profiler")
            if found is not None:
                chip = _parse_profiler(found.output) if found.ran and found.exit_code == 0 and not found.truncated else None
                if chip:
                    detail += f" ({chip[0]}" + (f", {chip[1]} graphics cores" if chip[1] else "") + ")"
                elif found.timed_out:
                    detail += " (macOS did not answer in time with more details)"
            # Detected, not proven: like every Mac row this asks for a look instead of saying "ready".
            return _result("accelerator", "warn", f"{detail}. {_DETECT_ONLY} Mac support has not been verified yet.",
                           "Nothing needs fixing here. Music and voice setup will tell you whether this Mac can run them. You can still play your own audio.")
        if kind == "intel":
            return _result("accelerator", "warn", "This is an Intel Mac, with no Apple silicon chip.",
                           "Whether music and voices can run on an Intel Mac has not been checked, and Mac support is only detected for now. You can still play your own audio.")
        return _result("accelerator", "unknown", "It could not be worked out what chip this Mac has.", "You can still play your own audio. Run the check again in a moment.")
    if system != "Linux":
        return _result("accelerator", "unknown", "Graphics hardware is only looked for on Linux and macOS.", "You can still play your own audio.")
    found = _linux_graphics(s)
    nodes, nvidia, tools = found["nodes"], found["nvidia"], found["tools"]
    if not nodes and not nvidia:
        look = f" Tools found by name: {', '.join(tools)}." if tools else ""
        return _result("accelerator", "warn", f"No graphics card was detected.{look}",
                       "Music and voices are made by models that normally need a graphics card. You can still play your own audio. "
                       "If your models run on another computer, ignore this.")
    parts = []
    for node in nodes[:4]:
        maker = node["vendor"] or "a graphics device"
        parts.append(f"{maker}" + (f" (driver {node['driver']})" if node["driver"] else ""))
    if nvidia:
        parts.append(f"NVIDIA ({len(nvidia)} {_plural(len(nvidia), 'device', 'devices')})")
    look = f" Tools found by name (not run): {', '.join(tools)}." if tools else ""
    detail = f"Detected: {'; '.join(parts)}.{look} {_DETECT_ONLY}"
    blocked = [n["name"] for n in nodes if not n["usable"]]
    if nodes and not any(n["usable"] for n in nodes) and not nvidia:
        return _result("accelerator", "warn", f"Detected a graphics device, but this account is not allowed to use it ({', '.join(blocked)}).",
                       "Add your account to the group that may use graphics devices, then log out and back in. " + _DETECT_ONLY,
                       "Open a terminal and run: sudo usermod -aG render \"$USER\"  (then log out and back in)")
    return _result("accelerator", "ok", detail)


# ------------------------------------------------------------ clock and zone

def _local_zone_name(s: Settings):
    """(name, where it came from). Name is "" when it cannot be worked out."""
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz.lstrip(":"), "TZ"
    try:
        target = os.path.realpath(os.path.join(s.etc_root, "localtime"))
        found = re.search(r"zoneinfo(?:\.default)?/(.+)$", target)
        if found:
            return found.group(1), "localtime"
    except Exception:
        pass
    try:
        with open(os.path.join(s.etc_root, "timezone"), "rb") as handle:
            line = handle.read(200).decode("ascii", "replace").strip().splitlines()
        if line and line[0].strip():
            return line[0].strip(), "timezone"
    except OSError:
        pass
    return "", ""


def _local_offset(now: float) -> Optional[int]:
    """Seconds east of UTC that this computer's own clock settings use at ``now`` (None if it will not say)."""
    try:
        return int(time.localtime(now).tm_gmtoff)
    except Exception:
        return None


def _utc_text(seconds: int) -> str:
    return f"UTC{'+' if seconds >= 0 else '-'}{abs(seconds) // 3600:02d}:{abs(seconds) % 3600 // 60:02d}"


@_guarded("clock")
def check_clock(settings: Optional[Settings] = None) -> dict:
    """Is the clock believable, and does the local timezone name make sense?"""
    s = _settings(settings)
    try:
        now = s.now()
    except Exception:
        now = None
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        return _result("clock", "unknown", "This computer's clock could not be read.", "Run the check again.")
    fix = "Open your system's Date & Time settings and turn on setting the date and time automatically."
    if not CLOCK_EARLIEST <= now < CLOCK_LATEST:
        try:
            shown = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%d %B %Y")
        except (OverflowError, OSError, ValueError):
            shown = "an impossible date"
        return _result("clock", "fail", f"This computer's clock says it is {shown}, which cannot be right.",
                       "Schedules and secure connections do not work with the wrong date. Turn on automatic date and time in your system settings.", fix)
    try:
        import zoneinfo
        zone_module = zoneinfo
    except ImportError:
        zone_module = None
    name, source = _local_zone_name(s)
    offset = _local_offset(now)
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    if offset is not None:
        stamp = stamp.astimezone(dt.timezone(dt.timedelta(seconds=offset)))
    when = stamp.strftime("%Y-%m-%d %H:%M")
    utc = "" if offset is None else _utc_text(offset)
    if not name:
        return _result("clock", "unknown", f"The clock reads {when} ({utc or 'timezone unknown'}), but the timezone's name could not be worked out.",
                       "You can still choose a timezone by name, for example America/Chicago, when you schedule shows.")
    zone = None
    if zone_module is not None and re.fullmatch(r"[A-Za-z0-9_+\-/.]{1,64}", name):
        try:
            zone = zone_module.ZoneInfo(name)
        except Exception:
            zone = None
    if zone is None:
        if zone_module is not None and source == "TZ" and "/" in name:
            return _result("clock", "warn", f"The TZ setting names a timezone that does not exist: {clean_text(name, 60)}.",
                           "Your computer is quietly using UTC instead. Fix or remove the TZ setting, then restart myradio.",
                           "Set TZ to a real name such as America/Chicago, or remove it from your shell's startup file.")
        return _result("clock", "unknown", f"The clock reads {when} ({utc or 'timezone unknown'}). The timezone name {clean_text(name, 60)} could not be checked against the timezone list.",
                       "You can still choose a timezone by name, for example America/Chicago, when you schedule shows.")
    expected = dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone(zone).utcoffset()
    if offset is not None and expected is not None and int(expected.total_seconds()) != offset:
        return _result("clock", "warn", f"The clock reads {when} ({utc}), but the timezone {clean_text(name, 60)} should be {_utc_text(int(expected.total_seconds()))} right now. They disagree.",
                       "Schedules may fire at the wrong time. Check your system's timezone setting and restart myradio.", fix)
    return _result("clock", "ok", f"The clock reads {when} in {clean_text(name, 60)}{f' ({utc})' if utc else ''}.")


# ----------------------------------------------------------------- registry

CHECKS = (
    CheckSpec("python", TITLES["python"], check_python),
    CheckSpec("ffmpeg", TITLES["ffmpeg"], check_ffmpeg),
    CheckSpec("disk-space", TITLES["disk-space"], check_disk_space),
    CheckSpec("data-folder", TITLES["data-folder"], check_data_folder),
    CheckSpec("loopback", TITLES["loopback"], check_loopback),
    CheckSpec("port", TITLES["port"], check_port),
    CheckSpec("system", TITLES["system"], check_system),
    CheckSpec("sqlite", TITLES["sqlite"], check_sqlite),
    CheckSpec("fcntl", TITLES["fcntl"], check_fcntl),
    CheckSpec("tzdata", TITLES["tzdata"], check_tzdata),
    CheckSpec("clock", TITLES["clock"], check_clock),
    CheckSpec("memory", TITLES["memory"], check_memory),
    CheckSpec("cpu", TITLES["cpu"], check_cpu),
    CheckSpec("accelerator", TITLES["accelerator"], check_accelerator),
)
