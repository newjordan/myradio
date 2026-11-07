"""Talk to a myradio station through myradio's own MCP adapter.

Each call starts the adapter (myradio_mcp.py, then python -m myradio.mcp) as a short-lived child process, sends one
MCP exchange over stdin and reads the answer. The adapter reads the station's private control.token from the data
folder; this module never sees it. The child gets a small environment (no API keys) and no shell.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import textwrap

VERSION = "0.2.0"
LAUNCHER = Path(__file__).resolve().parent / "myradio_mcp.py"
PROTOCOL = "2025-06-18"
TIMEOUT_S = 30
PASS_ENV = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "APPDATA", "LOCALAPPDATA",
            "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP")
SETTING_ENV = {"myradio_home": "MYRADIO_HOME", "data_dir": "MYRADIO_DATA_DIR", "port": "MYRADIO_PORT"}


class StationError(Exception):
    """Something the user should read: the station's own words, or why it could not be reached."""


def child_env(settings=None):
    """The adapter's environment: the basics a Python process needs plus the three myradio settings.
    A plugin setting wins over the same MYRADIO_* variable in Hermes's environment."""
    env = {name: os.environ[name] for name in PASS_ENV if name in os.environ}
    for key, name in SETTING_ENV.items():
        value = (settings or {}).get(key)
        if value in (None, "", 0):
            value = os.environ.get(name, "")
        if value not in (None, ""):
            value = str(value).strip()
            if key in ("myradio_home", "data_dir"):  # the child runs in the plugin folder, so make paths absolute
                value = str(Path(value).expanduser().absolute())
            env[name] = value
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def where(settings=None):
    """The data folder and port the adapter will use, for messages."""
    env = child_env(settings)
    return env.get("MYRADIO_DATA_DIR") or str(Path.home() / ".myradio"), env.get("MYRADIO_PORT") or "8765"


def plain(message, settings=None):
    """Turn the adapter's short errors into what to do next, with the exact command."""
    message = str(message or "").strip()
    data_dir, port = where(settings)
    if message.startswith("Start myradio first"):
        return (f"No myradio station found in {data_dir} (it has no control.token yet). Start your station with "
                f"`myradio` (or `python3 -m myradio.server` from your myradio folder) and try again. If your station keeps "
                f"its data somewhere else: hermes config set plugins.entries.myradio.settings.data_dir /path/to/data")
    if message.startswith("Cannot reach myradio"):
        return (f"Your station is not answering on port {port}. Start it with `myradio` (or `python3 -m myradio.server "
                f"--port {port}` from your myradio folder) and try again. If it runs on another port: "
                f"hermes config set plugins.entries.myradio.settings.port <port>")
    if message.startswith("Unlock with the launch code"):
        return (f"The station on port {port} did not accept the control code in {data_dir}, so it is probably a different "
                f"station from that folder. Point the plugin at the right one: hermes config set "
                f"plugins.entries.myradio.settings.data_dir /path/to/data (and settings.port if it differs)")
    return message


def exchange(requests, settings=None, timeout=TIMEOUT_S):
    """Initialize a fresh adapter, send `requests` as (method, params) pairs, and return their results in order."""
    messages = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                            "clientInfo": {"name": "hermes-myradio", "version": VERSION}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    messages += [{"jsonrpc": "2.0", "id": n, "method": method, "params": params}
                 for n, (method, params) in enumerate(requests, 1)]
    try:
        done = subprocess.run([sys.executable, str(LAUNCHER)], input="".join(json.dumps(m) + "\n" for m in messages),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              env=child_env(settings), cwd=str(LAUNCHER.parent))
    except subprocess.TimeoutExpired:
        raise StationError(f"The station did not answer within {timeout} seconds.") from None
    except OSError as exc:
        raise StationError(f"Could not start myradio's adapter: {exc}") from None
    replies = {}
    for line in done.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and "id" in message:
            replies[message["id"]] = message
    if 0 not in replies:
        lines = [line for line in done.stderr.splitlines() if line.strip()]
        raise StationError(plain(lines[-1] if lines else f"myradio's adapter stopped (exit code {done.returncode}).", settings))
    results = []
    for n in range(1, len(requests) + 1):
        message = replies.get(n)
        if message is None:
            raise StationError("myradio's adapter stopped before it answered.")
        if "error" in message:
            raise StationError(str(message["error"].get("message") or "The adapter refused the request."))
        results.append(message["result"])
    return results


def call(tool, arguments=None, settings=None):
    """Run one station tool (an MCP tool name such as queue_add) and return the station's reply, parsed."""
    (result,) = exchange([("tools/call", {"name": tool, "arguments": arguments or {}})], settings)
    text = "".join(part.get("text", "") for part in result.get("content", []) if isinstance(part, dict))
    if result.get("isError"):
        raise StationError(plain(text, settings) or "The station refused that.")
    try:
        return json.loads(text)
    except ValueError:
        return text


def catalog(settings=None):
    """Every tool the running station offers, as MCP tool entries (name, description, inputSchema)."""
    (result,) = exchange([("tools/list", {})], settings)
    return result.get("tools", [])


def clock(seconds):
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


# ---------------------------------------------------------------- what the model reads

SMALL_LIBRARY = 60     # up to this many tracks, myradio_status lists the whole library
HISTORY_KEEP = 15


def track_view(track):
    """A library track, queue entry or on-air track as the model sees it (queue entries carry their own track_id)."""
    view = {"track_id": track.get("track_id") or track.get("id"), "title": track.get("title") or "untitled",
            "artist": track.get("artist") or "", "kind": track.get("kind") or "music",
            "seconds": round(float(track.get("duration") or 0), 1)}
    if track.get("available") is False:
        view["available"] = False
    return view


def compact_status(status):
    """The station status trimmed for a model: everything a DJ needs, without the parts that grow with the library."""
    if not isinstance(status, dict):
        return status
    library = status.get("library") or []
    counts = {}
    for track in library:
        counts[track.get("kind") or "music"] = counts.get(track.get("kind") or "music", 0) + 1
    broadcast = status.get("broadcast") or {}
    out = {
        "station": status.get("station"),
        "playing": bool(status.get("running")),
        "on_air": dict(track_view(status["current"]), elapsed=round(float(status.get("elapsed") or 0), 1))
                  if status.get("current") else None,
        "queue": [dict(track_view(entry), queue_id=str(entry.get("id"))) for entry in status.get("queue") or []],
        "schedules": status.get("schedules") or [],
        "programs": status.get("programs") or [],
        "recent_history": (status.get("history") or [])[:HISTORY_KEEP],
        "makers_ready": {"music": bool((status.get("capabilities") or {}).get("music_generation")),
                         "dj_voice": bool((status.get("capabilities") or {}).get("speech_generation"))},
        "stream": {"state": broadcast.get("state", "disconnected"), "destination_saved": bool(broadcast.get("configured")),
                   "listeners": (status.get("stream") or {}).get("listeners", 0)},
        "health": status.get("health"),
        "library_counts": counts,
    }
    notes = {name: (ext.get("status") or {}).get("headline") for name, ext in (status.get("extensions") or {}).items()
             if isinstance(ext, dict) and (ext.get("status") or {}).get("headline")}
    if notes:
        out["notes"] = notes
    if status.get("error"):
        out["error"] = status["error"]
    if len(library) <= SMALL_LIBRARY:
        out["library"] = [track_view(track) for track in library]
    else:
        out["library"] = None
        out["library_note"] = f"{len(library)} tracks: find them with myradio_library (search by words, kind)."
    return out


def find_tracks(status, query="", kind="", limit=30):
    """Library tracks whose title or artist contain every word of `query`, best matches first."""
    words = [w for w in str(query or "").lower().split() if w]
    found = []
    for track in (status or {}).get("library") or []:
        if kind and (track.get("kind") or "music") != kind:
            continue
        text = f"{track.get('title') or ''} {track.get('artist') or ''}".lower()
        if all(w in text for w in words):
            title = (track.get("title") or "").lower()
            rank = (0 if title == " ".join(words) else 1 if title.startswith(" ".join(words)) else 2, title)
            found.append((rank, track))
    found.sort(key=lambda item: item[0])
    return [track_view(track) for _, track in found[:max(1, min(int(limit or 30), 100))]], len(found)


# ---------------------------------------------------------------- the /radio card

WIDTH = 58
CORAL, CREAM, DIM, GREEN, RED, BOLD, RESET = ("\033[38;5;209m", "\033[38;5;230m", "\033[38;5;245m", "\033[38;5;114m",
                                              "\033[38;5;203m", "\033[1m", "\033[0m")


def cells(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1 for ch in text)


def fit(text, width):
    text = " ".join(str(text or "").split())
    if cells(text) <= width:
        return text + " " * (width - cells(text))
    out = ""
    for ch in text:
        if cells(out + ch) > width - 1:
            break
        out += ch
    return out + "\u2026" + " " * (width - 1 - cells(out))


def card(status, color=False):
    """What is on air, time left, up next and the stream, as a boxed card for a terminal."""
    paint = (lambda code, text: f"{code}{text}{RESET}") if color else (lambda code, text: text)
    inner = WIDTH - 4
    rows = []

    def row(text="", painted=None):
        rows.append("\u2502 " + (painted if painted is not None else fit(text, inner)) + " \u2502")

    def rule():
        rows.append("\u251c" + "\u2500" * (WIDTH - 2) + "\u2524")

    station = status.get("station") or {}
    broadcast = status.get("broadcast") or {}
    live = broadcast.get("state") == "streaming"
    state = "LIVE" if live else "PLAYING" if status.get("running") else "STOPPED"
    badge = f"\u25cf {state}"
    name = fit((station.get("name") or "My radio").upper(), inner - cells(badge) - 1)
    row(painted=paint(BOLD + CREAM, name) + " " + paint(RED if live else GREEN if status.get("running") else DIM, badge))
    if station.get("tagline"):
        row(painted=paint(DIM, fit(station["tagline"], inner)))
    rule()

    current = status.get("current")
    label = 9
    if current:
        title = current.get("title") or "untitled"
        if current.get("artist"):
            title += f" \u2014 {current['artist']}"
        row(painted=paint(CORAL, "ON AIR".ljust(label)) + paint(BOLD + CREAM, fit(title, inner - label)))
        duration = float(current.get("duration") or 0)
        elapsed = min(float(status.get("elapsed") or 0), duration) if duration else 0
        times = f"  {clock(elapsed)} / {clock(duration)}"
        bar_cells = inner - label - cells(times)
        done = int(round(bar_cells * (elapsed / duration))) if duration else 0
        bar = paint(CORAL, "\u2501" * done) + paint(DIM, "\u2500" * (bar_cells - done))
        row(painted=" " * label + bar + paint(DIM, times))
        row(painted=" " * label + paint(DIM, fit(f"{clock(math.ceil(duration - elapsed))} left", inner - label)))
    else:
        row(painted=paint(CORAL, "ON AIR".ljust(label)) + paint(DIM, fit("nothing playing", inner - label)))
    rule()

    queue = status.get("queue") or []
    if queue:
        for n, entry in enumerate(queue[:3], 1):
            lead = ("UP NEXT" if n == 1 else "").ljust(label)
            kind = entry.get("kind") or "music"
            extra = "" if kind == "music" else f" ({'DJ' if kind == 'speech' else kind})"
            length = clock(entry.get("duration"))
            text = fit(f"{n}. {entry.get('title') or 'untitled'}{extra}", inner - label - len(length) - 1)
            row(painted=paint(CORAL, lead) + text + " " + paint(DIM, length))
        total = sum(float(entry.get("duration") or 0) for entry in queue)
        more = f"+ {len(queue) - 3} more, " if len(queue) > 3 else ""
        row(painted=" " * label + paint(DIM, fit(f"{more}{clock(total)} queued in all", inner - label)))
    else:
        filler = "auto-fill picks from the library" if station.get("auto_fill", True) else "add songs with /radio play"
        row(painted=paint(CORAL, "UP NEXT".ljust(label)) + paint(DIM, fit(f"queue empty, {filler}", inner - label)))
    rule()

    if live:
        stream = f"LIVE, {(status.get('stream') or {}).get('listeners', 0)} listening"
    elif broadcast.get("configured"):
        stream = f"not live ({broadcast.get('state', 'disconnected')}); /radio live to go public"
    else:
        stream = "not live, no destination saved"
    caps = status.get("capabilities") or {}
    makers = [label_ for key, label_ in (("music_generation", "music"), ("speech_generation", "DJ voice")) if caps.get(key)]
    library = status.get("library") or []
    songs = sum(1 for track in library if track.get("kind") == "music")
    for head, text in (("STREAM", stream),
                       ("MAKERS", ", ".join(makers) + " ready" if makers else "none set up; the library still plays"),
                       ("LIBRARY", f"{len(library)} tracks, {songs} songs")):
        row(painted=paint(CORAL, head.ljust(label)) + fit(text, inner - label))
    message = (status.get("health") or {}).get("message") or status.get("error")
    if message:
        rule()
        for line in textwrap.wrap(message, inner)[:3]:
            row(painted=paint(DIM, fit(line, inner)))
    top = "\u256d" + "\u2500" * (WIDTH - 2) + "\u256e"
    bottom = "\u2570" + "\u2500" * (WIDTH - 2) + "\u256f"
    return "\n".join([top, *rows, bottom])


def summary(status):
    """The card without colour (kept for `hermes myradio check` and older callers)."""
    return card(status)
