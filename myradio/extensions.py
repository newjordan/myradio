"""Extension seam: optional station subsystems (generation, shows, ads, research, hosts, ...)
plug in here instead of editing core playout.

Contract
--------
* Core playout never calls into an extension. Station events are copied into a bounded
  per-extension mailbox (oldest dropped when full) and delivered on that extension's own
  thread, so a slow or broken extension cannot delay the audio clock or another extension.
* An extension keeps its state in its own files under ``<data>/ext/<name>/`` (see
  ``Context.open_db``), never in the station database, so its locks and disk activity are
  independent of playout.
* ``Extension.status()`` runs inside every status request. It must be O(1), lock-free and
  must not call ``station.status()``; return a cached snapshot.
* Operations are typed and bounded. The host validates arguments before a handler runs.
  Handler failures other than ``ExtensionError`` are reported generically (no internals) and
  never propagate into the station.
* Commands, endpoints and credentials for model workers come from files the operator writes
  (``Context.config()``, ``Context.read_private()``), never from operation arguments, so a
  chat model or an external agent cannot make the station execute an arbitrary command.
* A failing extension disables itself and reports its error; the station keeps running.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
import queue
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ._platform import O_BINARY

NAME_RE = re.compile(r"[a-z][a-z0-9]{1,23}")
OP_RE = re.compile(r"[a-z][a-z0-9_]{1,39}")
ARG_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
PANEL_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
SCRIPT_RE = re.compile(r"[a-z0-9_-]{1,40}\.js")
DB_RE = re.compile(r"[a-z0-9_-]{1,40}\.sqlite3")
FILE_RE = re.compile(r"[A-Za-z0-9_.-]{1,60}")
ROUTE_RE = re.compile(r"/[A-Za-z0-9_\-/.]*")
URL_RE = re.compile(r"/api/x/([a-z][a-z0-9]{1,23})(/[A-Za-z0-9_\-/.]*)?")
ARG_TYPES = ("string", "boolean", "integer", "number", "object", "array")
CHECK_STATES = ("ok", "warn", "fail", "unknown")
# Hash routes that the host page already uses or that claude.ai reserves.
RESERVED_PANELS = frozenset({"studio", "library", "schedule", "broadcast", "settings", "help", "operator",
                             "start", "import", "my-station", "sidebar", "customize", "upgrade", "downloads"})
MAX_JSON_ARG = 65536
MAX_STATUS = 65536
MAX_CONFIG = 65536
MAILBOX = 256


class ExtensionError(ValueError):
    """A safe, presentable failure. The message may be shown to operators and agents."""


@dataclasses.dataclass(frozen=True)
class Arg:
    type: str
    description: str = ""
    required: bool = True
    enum: tuple = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: int = 4000

    def schema(self) -> dict:
        value: dict = {"type": self.type}
        if self.description:
            value["description"] = self.description
        if self.enum:
            value["enum"] = list(self.enum)
        if self.minimum is not None:
            value["minimum"] = self.minimum
        if self.maximum is not None:
            value["maximum"] = self.maximum
        if self.type == "string":
            value["maxLength"] = self.max_length
        return value

    def check(self, name: str, value: Any) -> Any:
        kind = self.type
        if kind == "string":
            ok = isinstance(value, str)
            if ok and (len(value) > self.max_length or (self.required and not value.strip())):
                raise ExtensionError(f"{name} must be 1–{self.max_length} characters." if self.required
                                     else f"{name} must be at most {self.max_length} characters.")
        elif kind == "boolean":
            ok = isinstance(value, bool)
        elif kind == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        elif kind == "object":
            ok = isinstance(value, dict)
        else:
            ok = isinstance(value, list)
        if not ok:
            raise ExtensionError(f"Invalid {name}.")
        if kind in ("object", "array"):
            try:
                size = len(json.dumps(value))
            except (TypeError, ValueError):
                raise ExtensionError(f"Invalid {name}.") from None
            if size > MAX_JSON_ARG:
                raise ExtensionError(f"{name} is too large.")
        if self.enum and value not in self.enum:
            raise ExtensionError(f"{name} must be one of: {', '.join(map(str, self.enum))}.")
        if kind in ("integer", "number"):
            if self.minimum is not None and value < self.minimum:
                raise ExtensionError(f"{name} must be at least {self.minimum:g}.")
            if self.maximum is not None and value > self.maximum:
                raise ExtensionError(f"{name} must be at most {self.maximum:g}.")
        return value


@dataclasses.dataclass(frozen=True)
class Operation:
    name: str
    description: str
    arguments: Mapping[str, Arg]
    handler: Callable[["Context", dict], Optional[dict]]


@dataclasses.dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, list]
    json: Optional[dict]
    body: bytes


@dataclasses.dataclass(frozen=True)
class Response:
    body: Any = None
    status: int = 200
    content_type: str = "application/json; charset=utf-8"


@dataclasses.dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: Callable[["Context", Request], Any]
    max_body: int = 32768


@dataclasses.dataclass(frozen=True)
class Panel:
    id: str
    title: str
    icon: str
    script: str
    order: int = 50


class Extension:
    """Base class. Override only what the subsystem needs."""

    name = ""

    def start(self, ctx: "Context") -> None: ...
    def stop(self) -> None: ...
    def operations(self) -> Sequence[Operation]: return ()
    def routes(self) -> Sequence[Route]: return ()
    def panels(self) -> Sequence[Panel]: return ()
    def status(self) -> dict: return {}
    def checks(self) -> Sequence[dict]: return ()
    def persistent_files(self) -> Sequence[str]: return ()
    def on_event(self, event: dict) -> None: ...


class StationFacade:
    """The narrow view of the station that extensions receive (thread-safe)."""

    def __init__(self, station):
        self._station = station

    def status(self) -> dict:
        return self._station.status()

    def import_audio(self, data: bytes, title: str, artist: str = "", kind: str = "music") -> dict:
        return self._station.import_audio(data, title, artist, kind)

    def action(self, operation: str, arguments: Optional[dict] = None) -> dict:
        return self._station.action(operation, arguments)

    def extension_checks(self) -> list:
        """Doctor checks from every running extension (the doctor extension merges these into its report)."""
        return self._station.extensions.checks()


def migrate(conn: sqlite3.Connection, steps: Sequence[str]) -> int:
    """Apply ordered SQL scripts exactly once each, tracked by ``PRAGMA user_version``."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(steps):
        raise ExtensionError("This data was written by a newer myradio. Update before opening it.")
    for index in range(current, len(steps)):
        conn.executescript(steps[index])
        conn.execute(f"PRAGMA user_version={index + 1}")
        conn.commit()
    return len(steps)


@dataclasses.dataclass
class Context:
    name: str
    directory: Path
    state_dir: Path
    station: Any
    clock: Callable[[], float] = time.time

    def open_db(self, filename: str) -> sqlite3.Connection:
        """A private, WAL-mode connection to this extension's own database file."""
        if not DB_RE.fullmatch(filename):
            raise ExtensionError("Invalid database name.")
        path = self.state_dir / filename
        if path.is_symlink():
            raise ExtensionError("Database path must not be a symbolic link.")
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600))
        except FileExistsError:
            pass  # reopening an existing database keeps its mode
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def config(self) -> dict:
        """Operator-written, non-secret settings (``config.json``). Empty when absent."""
        return self._read_json("config.json")

    def _read_json(self, filename: str) -> dict:
        path = self.state_dir / filename
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return {}
        if not stat.S_ISREG(info.st_mode):
            raise ExtensionError(f"{filename} must be a regular file.")
        if info.st_size > MAX_CONFIG:
            raise ExtensionError(f"{filename} is too large.")
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise ExtensionError(f"{filename} is not valid JSON.") from None
        if not isinstance(value, dict):
            raise ExtensionError(f"{filename} must contain a JSON object.")
        return value

    def write_private(self, filename: str, data: bytes) -> Path:
        """Atomically write a 0600 file inside this extension's directory."""
        if not FILE_RE.fullmatch(filename) or filename.startswith("."):
            raise ExtensionError("Invalid file name.")
        target = self.state_dir / filename
        if target.is_symlink():
            raise ExtensionError("Refusing to write through a symbolic link.")
        staged = self.state_dir / f".{filename}.{os.urandom(6).hex()}.tmp"
        try:
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(staged, target)
        finally:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
        return target

    def read_private(self, filename: str) -> Optional[bytes]:
        if not FILE_RE.fullmatch(filename):
            raise ExtensionError("Invalid file name.")
        path = self.state_dir / filename
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ExtensionError("Refusing to read anything but a regular file.")
        return path.read_bytes()


class _Mailbox:
    """Bounded, drop-oldest event delivery on a dedicated thread."""

    _STOP = object()

    def __init__(self, slot: "_Slot"):
        self.slot = slot
        self.queue: queue.Queue = queue.Queue(MAILBOX)
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, name=f"myradio-ext-{slot.name}", daemon=True)
        self.thread.start()

    def put(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.dropped += 1
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            if event is self._STOP:
                return
            try:
                self.slot.ext.on_event(event)
            except Exception:
                self.slot.errors += 1

    def stop(self) -> None:
        while True:
            try:
                self.queue.put_nowait(self._STOP)
                break
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
        self.thread.join(timeout=2)


class _Placeholder(Extension):
    """Stands in for an extension that could not be registered, so it shows up as failed."""

    def __init__(self, name: str):
        self.name = name


class _Slot:
    def __init__(self, ext: Extension):
        self.ext = ext
        self.name = ext.name
        self.state = "registered"
        self.error: Optional[str] = None
        self.errors = 0
        self.ctx: Optional[Context] = None
        self.mailbox: Optional[_Mailbox] = None
        self.ops: dict = {}
        self.routes: dict = {}
        self.panels: list = []


class ExtensionHost:
    def __init__(self, directory, facade, *, clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.facade = facade
        self.clock = clock
        self._slots: dict = {}
        self._lock = threading.RLock()
        self._busy = threading.local()
        self._started = False

    # ---- registration -------------------------------------------------
    def register(self, ext: Extension) -> None:
        name = getattr(ext, "name", "")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ExtensionError("Extension names are 2–24 lowercase letters or digits, starting with a letter.")
        with self._lock:
            if name in self._slots:
                raise ExtensionError(f"Extension {name} is already registered.")
            if self._started:
                raise ExtensionError("Register extensions before the station starts them.")
            slot = _Slot(ext)
            try:
                operations, routes, panels = list(ext.operations()), list(ext.routes()), list(ext.panels())
            except Exception as exc:  # a buggy describe method must not escape as a raw exception
                raise ExtensionError(f"Extension {name} could not describe itself ({type(exc).__name__}).") from None
            for op in operations:
                if not OP_RE.fullmatch(op.name) or op.name in slot.ops:
                    raise ExtensionError(f"Invalid or duplicate operation name: {op.name!r}.")
                if not callable(op.handler) or not 0 < len(op.description) <= 400:
                    raise ExtensionError(f"Operation {op.name} needs a handler and a short description.")
                for key, arg in op.arguments.items():
                    if not ARG_RE.fullmatch(key) or arg.type not in ARG_TYPES:
                        raise ExtensionError(f"Operation {op.name} has an invalid argument {key!r}.")
                slot.ops[op.name] = op
            for route in routes:
                if (route.method not in ("GET", "POST") or not ROUTE_RE.fullmatch(route.path) or ".." in route.path.split("/")
                        or "//" in route.path or not callable(route.handler) or not 0 < route.max_body <= 100 * 1024 * 1024):
                    raise ExtensionError(f"Invalid route {route.method} {route.path!r}.")
                if (route.method, route.path) in slot.routes:
                    raise ExtensionError(f"Duplicate route {route.method} {route.path}.")
                slot.routes[(route.method, route.path)] = route
            taken = {p["id"] for s in self._slots.values() for p in s.panels}
            for panel in panels:
                if (not PANEL_RE.fullmatch(panel.id) or panel.id in RESERVED_PANELS or panel.id in taken
                        or not SCRIPT_RE.fullmatch(panel.script) or not panel.title or len(panel.title) > 40):
                    raise ExtensionError(f"Invalid or reserved panel {panel.id!r}.")
                taken.add(panel.id)
                slot.panels.append({"id": panel.id, "title": panel.title, "icon": panel.icon[:2], "script": panel.script,
                                    "order": int(panel.order), "extension": name})
            self._slots[name] = slot

    def register_or_fail(self, ext: Extension) -> None:
        """Register, or keep a failed placeholder so the station still starts and the problem is visible."""
        try:
            self.register(ext)
        except ExtensionError as exc:
            name = getattr(ext, "name", "")
            with self._lock:
                if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in self._slots:
                    name = f"broken{len(self._slots) + 1}"
                slot = _Slot(_Placeholder(name))
                slot.state, slot.error = "failed", str(exc)[:200]
                self._slots[name] = slot

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._started = True
            slots = list(self._slots.values())
        for slot in slots:
            if slot.state == "failed":
                continue
            state_dir = self.directory / "ext" / slot.name
            try:
                state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                slot.ctx = Context(slot.name, self.directory, state_dir, self.facade, self.clock)
                slot.ext.start(slot.ctx)
            except Exception as exc:
                slot.state, slot.error = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
                continue
            slot.state = "running"
            if type(slot.ext).on_event is not Extension.on_event:
                slot.mailbox = _Mailbox(slot)

    def stop(self) -> None:
        with self._lock:
            slots = list(self._slots.values())
        for slot in reversed(slots):
            if slot.state == "running":
                try:
                    slot.ext.stop()
                except Exception:
                    slot.errors += 1
            if slot.mailbox:
                slot.mailbox.stop()
            if slot.state == "running":
                slot.state = "stopped"

    # ---- operations -------------------------------------------------------
    def has_operation(self, full_name: str) -> bool:
        found = self._find_op(full_name)
        return found is not None and found[0].state == "running"

    def _find_op(self, full_name: str):
        if not isinstance(full_name, str) or full_name.count(".") != 1:
            return None
        ext_name, local = full_name.split(".")
        slot = self._slots.get(ext_name)
        if slot is None:
            return None
        op = slot.ops.get(local)
        return (slot, op) if op else None

    def catalog(self) -> list:
        """Operations of running extensions, in a JSON-schema-like shape."""
        out = []
        for slot in self._slots.values():
            if slot.state != "running":
                continue
            for op in slot.ops.values():
                out.append({"name": f"{slot.name}.{op.name}", "description": op.description,
                            "arguments": {k: a.schema() for k, a in op.arguments.items()},
                            "required": [k for k, a in op.arguments.items() if a.required]})
        return out

    def call(self, full_name: str, arguments: Optional[dict]) -> dict:
        found = self._find_op(full_name)
        if found is None or found[0].state != "running":
            raise ExtensionError("Unknown station operation.")
        slot, op = found
        args = {} if arguments is None else arguments
        if not isinstance(args, dict) or set(args) - set(op.arguments):
            raise ExtensionError("Invalid operation arguments.")
        clean = {}
        for key, spec in op.arguments.items():
            if key not in args:
                if spec.required:
                    raise ExtensionError(f"Missing {key}.")
                continue
            clean[key] = spec.check(key, args[key])
        try:
            result = op.handler(slot.ctx, clean)
            payload = {"ok": True, "operation": full_name}
            if result is not None:
                if not isinstance(result, dict):
                    raise TypeError("operation result must be an object")
                payload.update(result)
            return json.loads(json.dumps(payload))
        except ExtensionError:
            raise
        except Exception:
            slot.errors += 1
            raise ExtensionError(f"The {slot.name} operation failed. No retry was performed; inspect status before retrying.") from None

    # ---- status, checks, panels, backups ----------------------------------
    def status(self) -> dict:
        if getattr(self._busy, "active", False):
            return {}  # an extension called station.status() from status(): break the cycle
        self._busy.active = True
        try:
            out = {}
            for name, slot in self._slots.items():
                entry: dict = {"state": slot.state, "error": slot.error}
                if slot.state == "running":
                    try:
                        raw = json.dumps(slot.ext.status())
                        entry["status"] = {"truncated": True} if len(raw) > MAX_STATUS else json.loads(raw)
                    except Exception:
                        slot.errors += 1
                        entry["status"], entry["error"] = {}, "Status is unavailable."
                if slot.mailbox and slot.mailbox.dropped:
                    entry["events_dropped"] = slot.mailbox.dropped
                out[name] = entry
            return out
        finally:
            self._busy.active = False

    def panels(self) -> list:
        items = [p for s in self._slots.values() if s.state == "running" for p in s.panels]
        return sorted(copy.deepcopy(items), key=lambda p: (p["order"], p["id"]))

    def checks(self) -> list:
        out = []
        for slot in self._slots.values():
            if slot.state != "running":
                out.append({"id": f"{slot.name}.start", "title": f"{slot.name} extension", "status": "fail",
                            "detail": slot.error or "Not running.", "hint": "Check the extension's setup, then restart myradio."})
                continue
            try:
                items = list(slot.ext.checks())
            except Exception:
                slot.errors += 1
                continue
            for item in items:
                if (isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("title"), str)
                        and item.get("status") in CHECK_STATES):
                    out.append({"id": f"{slot.name}.{item['id']}"[:80], "title": item["title"][:120], "status": item["status"],
                                "detail": str(item.get("detail", ""))[:400], "hint": str(item.get("hint", ""))[:400]})
        return out

    def backup_files(self) -> dict:
        """Existing files each extension asks to include in portable backups (credentials excluded by contract)."""
        out = {}
        for slot in self._slots.values():
            try:
                names = list(slot.ext.persistent_files()) if slot.state in ("running", "stopped") else []
            except Exception:
                continue
            base = self.directory / "ext" / slot.name
            files = []
            for rel in names:
                if isinstance(rel, str) and FILE_RE.fullmatch(rel) and not rel.startswith("."):
                    path = base / rel
                    if path.is_file() and not path.is_symlink():
                        files.append(path)
            out[slot.name] = files
        return out

    # ---- routes -------------------------------------------------------------
    def find_route(self, method: str, path: str):
        match = URL_RE.fullmatch(path)
        if not match:
            return None
        rest = match.group(2) or "/"
        if ".." in rest.split("/") or "//" in rest:
            return None
        slot = self._slots.get(match.group(1))
        if slot is None or slot.state != "running":
            return None
        route = slot.routes.get((method, rest))
        return (slot, route, rest) if route else None

    def dispatch_route(self, match, request: Request) -> Response:
        slot, route, _ = match
        try:
            value = route.handler(slot.ctx, request)
            if isinstance(value, Response):
                response = value
            elif value is None:
                response = Response({"ok": True})
            else:
                response = Response(value)
            if response.content_type.startswith("application/json"):
                json.dumps(response.body)
            elif not isinstance(response.body, (bytes, str)):
                raise TypeError("non-JSON responses must be bytes or text")
            return response
        except ExtensionError as exc:
            return Response({"error": str(exc)}, 400)
        except Exception:
            slot.errors += 1
            return Response({"error": f"The {slot.name} service could not complete that request."}, 500)

    # ---- events ---------------------------------------------------------------
    def emit(self, event: dict) -> None:
        """Never blocks. Called from the playout path."""
        for slot in tuple(self._slots.values()):
            if slot.mailbox is not None and slot.state == "running":
                slot.mailbox.put(copy.deepcopy(event))


# Subsystems that ship with myradio. Each lives in ``myradio/<name>/extension.py`` and exposes ``build()``.
BUILTIN = ("generation", "shows", "ads", "research", "hosts", "prompts", "doctor")


class _LoadFailure(Extension):
    """Stands in for a built-in extension that could not load, so the failure is visible in
    status and doctor output instead of silently missing."""

    def __init__(self, name: str, error: str):
        self.name, self.error = name, error

    def start(self, ctx: Context) -> None:
        raise ExtensionError(self.error)


def builtin_extensions() -> list:
    import importlib
    found = []
    for name in BUILTIN:
        module_name = f"myradio.{name}.extension"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in (module_name, f"myradio.{name}"):
                continue  # not part of this build
            found.append(_LoadFailure(name, f"Could not import {module_name}: missing {exc.name}"[:200]))
            continue
        except Exception as exc:
            found.append(_LoadFailure(name, f"Could not import {module_name}: {type(exc).__name__}"))
            continue
        try:
            found.append(module.build())
        except Exception as exc:
            found.append(_LoadFailure(name, f"{name} setup failed: {type(exc).__name__}"))
    return found
