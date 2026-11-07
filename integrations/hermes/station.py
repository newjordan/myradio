"""Talk to a myradio station through myradio's own MCP adapter.

Each call starts the adapter (myradio_mcp.py, then python -m myradio.mcp) as a short-lived child process, sends one
MCP exchange over stdin and reads the answer. The adapter reads the station's private control.token from the data
folder; this module never sees it. The child gets a small environment (no API keys) and no shell.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

VERSION = "0.1.0"
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
            env[name] = str(value)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


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
        raise StationError(lines[-1].strip() if lines else f"myradio's adapter stopped (exit code {done.returncode}).")
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
        raise StationError(text or "The station refused that.")
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
    return f"{seconds // 60}:{seconds % 60:02d}"


def summary(status):
    """A few plain lines about the station for /radio and `hermes myradio check`."""
    station = status.get("station") or {}
    lines = [f"{station.get('name') or 'My radio'}: {'playing' if status.get('running') else 'stopped'}."]
    current = status.get("current")
    if current:
        by = f" by {current['artist']}" if current.get("artist") else ""
        lines.append(f"On air: {current.get('title') or 'untitled'}{by} ({clock(status.get('elapsed'))} of {clock(current.get('duration'))}).")
    queue = status.get("queue") or []
    if queue:
        upcoming = ", ".join(entry.get("title") or "untitled" for entry in queue[:3])
        lines.append(f"Queued: {len(queue)} ({upcoming}{', ...' if len(queue) > 3 else ''}).")
    else:
        lines.append("Queue: empty.")
    library = status.get("library") or []
    music = sum(1 for track in library if track.get("kind") == "music")
    lines.append(f"Library: {len(library)} tracks ({music} songs).")
    caps = status.get("capabilities") or {}
    makers = [label for key, label in (("music_generation", "music"), ("speech_generation", "DJ voice")) if caps.get(key)]
    lines.append(f"Makers ready: {', '.join(makers)}." if makers else "Makers ready: none (spoken carts from the library still work).")
    broadcast = status.get("broadcast") or {}
    state = broadcast.get("state", "disconnected")
    lines.append("Stream: LIVE." if state == "streaming" else
                 f"Stream: {state}{'' if broadcast.get('configured') else ', no destination saved'}.")
    health = status.get("health") or {}
    if health.get("message"):
        lines.append(health["message"])
    if status.get("error"):
        lines.append(status["error"])
    return "\n".join(lines)
