"""Safe helpers for looking at the computer: bounded tool calls and text clean-up.

Everything in the doctor that starts another program goes through ``run_tool``. It
only ever takes an argument list (never a shell string), reads a bounded amount of
output, gives up after a deadline and stops the whole process group it started, so a
hung or noisy tool cannot stall the report or fill memory. Nothing here installs,
downloads or changes anything.
"""
from __future__ import annotations

import dataclasses
import math
import os
import selectors
import signal
import subprocess
import threading
import time
import unicodedata
from typing import Optional, Sequence

MAX_TOOL_OUTPUT = 256 * 1024  # bytes read from any one tool; the rest is discarded
_READ_CHUNK = 65536
_POLL_SLICE = 0.05  # how often a waiting call looks at the cancel flag
_REAP_WAIT = 1.0  # longest wait for a killed tool to be collected


@dataclasses.dataclass(frozen=True)
class ToolResult:
    """What happened when a tool was started.

    ``ran`` is False when the program never started (``reason`` says why: missing, denied,
    failed, invalid or timeout). ``output`` is the tool's combined output as text, at most
    ``max_bytes`` long.
    """

    ran: bool = False
    exit_code: Optional[int] = None
    output: str = ""
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        """The tool started, finished by itself and reported success."""
        return self.ran and self.exit_code == 0 and not (self.timed_out or self.truncated or self.cancelled)


def _kill_group(process: "subprocess.Popen") -> None:
    """Stop the tool and anything it started. The tool leads its own process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        process.kill()
    except (OSError, AttributeError):
        pass


def run_tool(args: Sequence[str], *, timeout: float, max_bytes: int = MAX_TOOL_OUTPUT,
             cancel: Optional[threading.Event] = None) -> ToolResult:
    """Run ``args`` (a list, never a shell string) and return what it printed.

    Never raises. The call returns after ``timeout`` seconds at the latest, reads at most
    ``max_bytes`` of output (stdout and stderr together), and kills the tool's whole process
    group when it times out, is cancelled or prints too much.
    """
    try:
        if not isinstance(args, (list, tuple)):
            return ToolResult(reason="invalid")  # a single string would be a shell command line, which is never accepted
        command = list(args)
        if (not command or not all(isinstance(part, str) and part and "\0" not in part for part in command)
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1):
            return ToolResult(reason="invalid")
    except TypeError:
        return ToolResult(reason="invalid")
    if cancel is not None and cancel.is_set():
        return ToolResult(cancelled=True, reason="cancelled")
    if timeout <= 0:
        return ToolResult(timed_out=True, reason="timeout")
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
    except FileNotFoundError:
        return ToolResult(reason="missing")
    except PermissionError:
        return ToolResult(reason="denied")
    except ValueError:
        return ToolResult(reason="invalid")
    except OSError:
        return ToolResult(reason="failed")

    deadline = time.monotonic() + timeout
    chunks: list = []
    size = 0
    timed_out = truncated = cancelled = ended = False
    exited = False
    selector = None
    try:
        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
        except (OSError, ValueError):
            selector = None
        while selector is not None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            events = selector.select(min(_POLL_SLICE, left))
            if events:
                try:
                    data = os.read(process.stdout.fileno(), _READ_CHUNK)
                except OSError:
                    data = b""
                if not data:
                    ended = True  # the tool closed its output
                    break
                room = max_bytes - size
                if len(data) > room:
                    chunks.append(data[:room])
                    size += room
                    truncated = True
                    break
                chunks.append(data)
                size += len(data)
            elif exited:
                break  # the tool is gone but something it started still holds the pipe: stop waiting
            else:
                exited = process.poll() is not None  # one more pass picks up anything left in the pipe
        if selector is None:
            timed_out = True  # could not watch the tool safely, so do not wait on it
        code: Optional[int] = None
        if timed_out or cancelled or truncated or not ended:
            _kill_group(process)
        try:
            code = process.wait(timeout=_REAP_WAIT if (timed_out or cancelled or truncated or not ended)
                                else max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            if not (timed_out or cancelled or truncated):
                timed_out = True  # closed its output but would not finish
            _kill_group(process)
            try:
                code = process.wait(timeout=_REAP_WAIT)
            except subprocess.TimeoutExpired:
                code = None
        text = b"".join(chunks).decode("utf-8", "replace")
        return ToolResult(ran=True, exit_code=code, output=text, timed_out=timed_out, truncated=truncated,
                          cancelled=cancelled)
    finally:
        if selector is not None:
            try:
                selector.close()
            except Exception:
                pass
        if process.poll() is None:
            _kill_group(process)
            try:
                process.wait(timeout=_REAP_WAIT)
            except Exception:
                pass
        try:
            process.stdout.close()
        except Exception:
            pass


# ---------------------------------------------------------------- text clean-up

_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_BLANKS = frozenset({"\t", "\n", "\r", "\v", "\f", "\x85", "\u2028", "\u2029"})


def clean_text(value, limit: int = 400, *, collapse: bool = True) -> str:
    """Make text from the outside world safe to show.

    Control, format (including right-to-left override) and unassigned characters become
    ``?`` so a file name or tool output can never move the cursor, recolour a terminal or
    disguise other text. With ``collapse`` (the default) runs of white space become one
    space; without it spacing is kept (used for commands the person may copy). The result
    is at most ``limit`` characters.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    out = []
    for char in value[: max(limit * 4, 64)]:
        if char in _BLANKS:
            out.append(" " if collapse else "?")
        elif unicodedata.category(char) in _DROP_CATEGORIES:
            out.append("?")
        else:
            out.append(char)
    text = "".join(out)
    if collapse:
        text = " ".join(text.split())
    else:
        text = text.strip()
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def display_path(path: str) -> str:
    """Show a path with the person's home folder written as ``~`` (shorter and less personal)."""
    try:
        home = os.path.expanduser("~")
        if home and home not in ("~", "/"):
            home = home.rstrip("/")
            if path == home:
                return "~"
            if path.startswith(home + "/"):
                return "~" + path[len(home):]
    except Exception:
        pass
    return path
