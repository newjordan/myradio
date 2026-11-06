"""The operator-written settings (``config.json``) and the checks that keep them safe.

Everything that decides what the station may run or connect to comes from this file and nowhere
else: never from an operation argument, never from a chat model. Loading is forgiving about *which*
part is wrong (one bad worker does not switch off the others, and every problem is reported in plain
language) but strict about *what* is accepted:

* a command is a list of arguments (never one shell string), its program is a full path to an
  existing executable or a bare name found on the PATH, and it does not run a typed-in script
  (``bash -c ...``). That last check catches the usual ways of writing one, including combined letters
  (``bash -lc``) and a script runner behind ``env``, ``nice``, ``timeout`` and similar; it is a guard against
  mistakes in a file the operator writes and trusts, not a security boundary;
* a worker address must be ``http://`` and point at this computer unless the worker explicitly
  says ``"allow_remote": true``;
* timeouts, sizes and counts must be inside sensible bounds.
"""
from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import os
import re
import shutil
import urllib.parse
from typing import Any, Callable, Mapping, Optional, Sequence

from .errors import ConfigError
from .payloads import ROLES
from .util import finite_number

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,60}")
_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([a-z_]{1,24})\}|\{|\}")
EVAL_WORDS = frozenset({"--eval", "-command", "-encodedcommand", "/c", "/k", "-exec", "--exec"})
INTERPRETERS = frozenset({
    "sh", "bash", "dash", "zsh", "ksh", "fish", "csh", "tcsh", "ash", "busybox", "cmd", "cmd.exe", "powershell",
    "powershell.exe", "pwsh", "python", "python2", "python3", "perl", "ruby", "node", "nodejs", "php", "lua",
    "osascript", "env", "awk", "gawk", "sed", "expect", "tclsh",
})
# Programs that start another program named in their own arguments ("nice -n 5 bash -c ...").
WRAPPERS = frozenset({"env", "nice", "ionice", "timeout", "xargs", "nohup", "setsid", "sudo", "doas", "stdbuf", "taskset", "chrt", "time",
                      "command", "exec", "flock", "unbuffer", "ssh"})
# For each interpreter: the one-letter options that make it run text typed on the command line (``-c`` for a shell or
# python, ``-e`` for perl), and the letters that take the rest of the word as their value (so ``-mcalendar`` names a
# module and is not a bundle containing ``c``). A program not listed is treated as running text for ``-c`` and ``-e``.
EVAL_LETTERS = {"sh": "c", "bash": "c", "dash": "c", "zsh": "c", "ksh": "c", "fish": "c", "csh": "c", "tcsh": "c", "ash": "c",
                "python": "c", "python2": "c", "python3": "c", "perl": "eE", "ruby": "e", "node": "ep", "nodejs": "ep", "php": "r",
                "lua": "e", "awk": "e", "gawk": "e", "sed": "e", "osascript": "e"}
VALUE_LETTERS = {"python": "mWXQ", "python2": "mWXQ", "python3": "mWXQ", "perl": "IMmiFCxdD", "ruby": "IrKCx", "node": "r", "nodejs": "r"}
LONE_ONLY = frozenset({"powershell", "powershell.exe", "pwsh", "cmd", "cmd.exe"})  # their options are whole words, not letters
DEFAULT_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LD_LIBRARY_PATH", "TZ")
TOP_LEVEL = frozenset({"version", "artist", "resources", "workers", "buffer", "retry", "finishing", "limits"})

SONG_BUFFER_MAX = (7200, 14400)      # seconds
ITEM_BUFFER_MAX = (50, 100)          # items
DEFAULT_BUFFER = {
    "song": (600, 1200),
    "host_break": (2, 4),
    "id": (1, 2),
    "spot": (1, 3),
    "update": (1, 2),
}


@dataclasses.dataclass(frozen=True)
class Env:
    """Things the checks look at, replaceable in tests."""
    which: Callable[[str], Optional[str]] = shutil.which
    environ: Mapping[str, str] = dataclasses.field(default_factory=lambda: os.environ)
    isfile: Callable[[str], bool] = os.path.isfile
    access: Callable[..., bool] = os.access


@dataclasses.dataclass(frozen=True)
class ResourceSpec:
    name: str
    max_concurrent: int = 1
    switch_after_s: float = 120.0


@dataclasses.dataclass(frozen=True)
class WorkerSpec:
    id: str
    kind: str
    roles: tuple
    resource: str
    options: Mapping[str, Any]
    fixture: bool = False


@dataclasses.dataclass(frozen=True)
class RetrySpec:
    max_attempts: int = 3
    base_s: float = 10.0
    cap_s: float = 300.0
    max_requeues: int = 2


@dataclasses.dataclass(frozen=True)
class FinishSpec:
    target_lufs: float = -16.0
    ceiling_dbfs: float = -1.0
    strict_clipping: bool = False   # reject results the checker calls distorted (default: note them and keep them)
    strict_tail: bool = True        # reject results that stop abruptly (set false if a maker's results always end loud)


@dataclasses.dataclass(frozen=True)
class LimitSpec:
    max_queued: int = 500
    max_song_seconds: int = 420
    min_free_disk_mb: int = 500
    probe_interval_s: float = 60.0


@dataclasses.dataclass(frozen=True)
class Rejected:
    """A worker entry that could not be loaded, kept so status can say so plainly."""
    id: str
    kind: str
    message: str


@dataclasses.dataclass(frozen=True)
class Disabled:
    """A worker entry the operator switched off with ``"enabled": false``. It is kept so the screen can say so, and so
    that waiting jobs for the kinds it covers stay in line instead of being thrown away."""
    id: str
    kind: str
    roles: tuple


@dataclasses.dataclass(frozen=True)
class LoadedConfig:
    artist: str
    resources: Mapping[str, ResourceSpec]
    workers: Mapping[str, WorkerSpec]
    rejected: tuple
    buffer: Mapping[str, tuple]
    urgent_priority: int
    retry: RetrySpec
    finish: FinishSpec
    limits: LimitSpec
    problems: tuple
    fingerprint: str
    present: bool          # a settings file existed
    disabled: tuple = ()   # makers switched off with "enabled": false (not a problem, just not running)

    def worker_for(self, role: str) -> Optional[WorkerSpec]:
        for spec in self.workers.values():
            if role in spec.roles:
                return spec
        return None


# ---- value helpers -------------------------------------------------------------------------------------------

def get_int(raw: Mapping, key: str, default: int, lo: int, hi: int, where: str) -> int:
    value = raw.get(key, default)
    if not finite_number(value) or value != int(value):
        raise ConfigError(f"{where}: {key} must be a whole number from {lo} to {hi}.")
    value = int(value)
    if not lo <= value <= hi:
        raise ConfigError(f"{where}: {key} must be from {lo} to {hi}.")
    return value


def get_float(raw: Mapping, key: str, default: float, lo: float, hi: float, where: str, unit: str = "") -> float:
    value = raw.get(key, default)
    if not finite_number(value):
        raise ConfigError(f"{where}: {key} must be a number from {lo:g} to {hi:g}{unit}.")
    if not lo <= value <= hi:
        raise ConfigError(f"{where}: {key} must be from {lo:g} to {hi:g}{unit}. That is the range that makes sense.")
    return float(value)


def get_bool(raw: Mapping, key: str, default: bool, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false.")
    return value


def get_str(raw: Mapping, key: str, default: Optional[str], where: str, *, max_len: int = 200) -> Optional[str]:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_len or "\x00" in value:
        raise ConfigError(f"{where}: {key} must be text of 1 to {max_len} characters.")
    return value.strip()


def get_file_name(raw: Mapping, key: str, where: str) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not FILE_NAME_RE.fullmatch(value) or value.startswith("."):
        raise ConfigError(f"{where}: {key} must be a plain file name kept next to the settings file (letters, digits, dot, dash, underscore).")
    return value


# ---- commands --------------------------------------------------------------------------------------------------

def template_names(arg: str, where: str) -> set:
    """The placeholder names used in one argument. Literal braces are written ``{{`` and ``}}``."""
    names = set()
    for match in _TOKEN_RE.finditer(arg):
        token = match.group(0)
        if match.group(1):
            names.add(match.group(1))
        elif token in ("{", "}"):
            raise ConfigError(f"{where}: a stray brace in {arg[:60]!r}. Write {{{{ and }}}} for literal braces.")
    return names


def render_arg(arg: str, values: Mapping[str, str]) -> str:
    def swap(match: "re.Match") -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return values[match.group(1)]

    return _TOKEN_RE.sub(swap, arg)


def _family(program: str) -> str:
    """The lower-case name of a program without its folder or version digits (``/usr/bin/python3.12`` is ``python``)."""
    base = os.path.basename(program).lower()
    return base if base in INTERPRETERS or base in WRAPPERS else base.rstrip("0123456789.")


def _runs_text(family: str, arg: str) -> bool:
    """True when ``arg`` makes the interpreter ``family`` run text typed on the command line: ``-c`` or ``-e`` as the
    program expects it, those letters inside a word of combined one-letter options (``-lc``, ``-Sc``, ``-le``), or one of
    the long spellings (``--eval``, ``/c``)."""
    if arg.lower() in EVAL_WORDS:
        return True
    if not re.fullmatch(r"-[A-Za-z0-9]+", arg) or (family in LONE_ONLY and len(arg) > 2):
        return False
    runs_text, takes_value = EVAL_LETTERS.get(family, "ce"), VALUE_LETTERS.get(family, "")
    for letter in arg[1:]:
        if letter in runs_text:
            return True
        if letter in takes_value:
            return False  # the rest of the word is this option's value, such as the module name in -mcalendar
    return False


def typed_in_script(program: str, args: Sequence[str]) -> Optional[str]:
    """``"bash -c"`` style text naming the option that would run a script typed into the command, or None.

    Looks at a shell or interpreter directly and, behind ``env``, ``nice``, ``timeout``, ``xargs`` and similar
    wrappers, at the program they start. Only the usual ways of writing a script on the command line are caught:
    this guards against mistakes in a file the operator writes, and is not a security boundary."""
    family = _family(program)
    interpreter, wrapper = family in INTERPRETERS, family in WRAPPERS
    for index, arg in enumerate(args):
        if interpreter and _runs_text(family, arg):
            return f"{os.path.basename(program)} {arg}"
        if wrapper and not arg.startswith("-") and _family(arg) in INTERPRETERS:
            found = typed_in_script(arg, args[index + 1:])
            if found:
                return found
    return None


def check_command(value: Any, field: str, where: str, allowed: frozenset, env: Env, *, required: Sequence = ()) -> tuple:
    """Validate a command template and return it with the program replaced by its full path."""
    if isinstance(value, str):
        raise ConfigError(f"{where}: {field} must be a list of separate arguments, not one line of text "
                          f'(for example ["/opt/tool/run", "--out", "{{output_file}}"]). One line of text would need a shell, and none is used.')
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ConfigError(f"{where}: {field} must be a list of 1 to 64 arguments.")
    args = []
    for item in value:
        if not isinstance(item, str) or "\x00" in item or len(item) > 4096:
            raise ConfigError(f"{where}: every item in {field} must be text (up to 4096 characters).")
        args.append(item)
    program = args[0]
    if not program.strip() or program != program.strip():
        raise ConfigError(f"{where}: the first item of {field} must be the program to run.")
    if template_names(program, where):
        raise ConfigError(f"{where}: the program in {field} cannot contain placeholders.")
    if program.startswith("~"):
        raise ConfigError(f"{where}: write the full path to the program in {field}; ~ is not expanded.")
    if os.path.isabs(program):
        if not env.isfile(program) or not env.access(program, os.X_OK):
            raise ConfigError(f"{where}: the program {program} in {field} was not found or cannot be run.")
        resolved = program
    elif os.sep in program or (os.altsep and os.altsep in program):
        raise ConfigError(f"{where}: {program!r} in {field} is a relative path. Use the full path (starting with /) "
                          f"or just the program name if it is installed on the PATH.")
    else:
        resolved = env.which(program)
        if not resolved:
            raise ConfigError(f"{where}: the program {program!r} in {field} was not found on the PATH. "
                              f"Install it or write its full path.")
        resolved = os.path.abspath(resolved)  # a PATH entry such as "." must not leave a path that only works from one folder
    names = set()
    for arg in args[1:]:
        names |= template_names(arg, where)
    found = typed_in_script(resolved, args[1:])
    if found:
        raise ConfigError(f"{where}: {field} runs a typed-in script ({found}). Put the steps in a script file "
                          f"and name that file instead.")
    unknown = sorted(names - allowed)
    if unknown:
        raise ConfigError(f"{where}: {field} uses unknown placeholder(s) {', '.join('{' + n + '}' for n in unknown)}. "
                          f"Available: {', '.join('{' + n + '}' for n in sorted(allowed))}.")
    for needed in required:
        if needed not in names:
            raise ConfigError(f"{where}: {field} must include {{{needed}}} so the result can be found.")
    return (resolved, *args[1:])


def check_env(raw: Mapping, where: str) -> tuple:
    """(``env`` values to set, ``env_passthrough`` names to copy) for a worker's scrubbed environment."""
    env = raw.get("env", {})
    if not isinstance(env, dict) or len(env) > 40:
        raise ConfigError(f"{where}: env must be an object of up to 40 name/value pairs.")
    clean = {}
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_NAME_RE.fullmatch(key) or not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
            raise ConfigError(f"{where}: env entries need a plain name (letters, digits, underscore) and a text value.")
        clean[key] = value
    names = raw.get("env_passthrough", [])
    if not isinstance(names, list) or len(names) > 60 or not all(isinstance(n, str) and ENV_NAME_RE.fullmatch(n) for n in names):
        raise ConfigError(f"{where}: env_passthrough must be a list of environment variable names.")
    return clean, tuple(dict.fromkeys(DEFAULT_PASSTHROUGH + tuple(names)))


def check_paths(raw: Mapping, key: str, where: str, limit: int = 20) -> tuple:
    value = raw.get(key, [])
    if not isinstance(value, list) or len(value) > limit:
        raise ConfigError(f"{where}: {key} must be a list of up to {limit} full file paths.")
    out = []
    for item in value:
        if not isinstance(item, str) or "\x00" in item or not os.path.isabs(item) or len(item) > 1024:
            raise ConfigError(f"{where}: every path in {key} must be a full path starting with /.")
        out.append(item)
    return tuple(out)


# ---- addresses -----------------------------------------------------------------------------------------------

def is_loopback_name(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base_url(value: Any, where: str, allow_remote: bool) -> dict:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ConfigError(f"{where}: base_url must be an address such as http://127.0.0.1:8080.")
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ConfigError(f"{where}: base_url is not a valid address.") from None
    if parts.scheme != "http":
        raise ConfigError(f"{where}: base_url must start with http://. Secure remote connections are not supported; "
                          f"use an address on this computer or a private tunnel.")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment or not host:
        raise ConfigError(f"{where}: base_url must be a plain address such as http://127.0.0.1:8080 (no user name, query or fragment).")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"{where}: the port in base_url must be from 1 to 65535.")
    path = parts.path.rstrip("/")
    if path and (not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", path) or any(seg in (".", "..") for seg in path.split("/"))):
        raise ConfigError(f"{where}: the path in base_url may only contain letters, digits and - . _ ~ / (and no . or .. parts)")
    if not is_loopback_name(host) and not allow_remote:
        raise ConfigError(f'{where}: base_url points to another computer ({host}). By default only addresses on this computer '
                          f'are allowed. If you really mean it, add "allow_remote": true to this worker.')
    return {"host": host, "port": port or 80, "path": path, "base_url": f"http://{host if ':' not in host else '[' + host + ']'}:{port or 80}{path}"}


def check_url_path(raw: Mapping, key: str, default: str, where: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9._~/-]{0,120}", value) or ".." in value.split("/") or "//" in value:
        raise ConfigError(f"{where}: {key} must be a path such as {default}.")
    return value


# ---- whole file ----------------------------------------------------------------------------------------------

def _canonical(raw: Any) -> str:
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(raw: Any) -> str:
    return hashlib.sha256(_canonical(raw).encode()).hexdigest()


def _resources(raw: Any, problems: list) -> dict:
    default = {"gpu0": ResourceSpec("gpu0")}
    if raw is None:
        return default
    if not isinstance(raw, dict) or not raw or len(raw) > 4:
        problems.append("resources must be an object naming 1 to 4 resource groups, for example {\"gpu0\": {\"max_concurrent\": 1}}. Using one group, gpu0.")
        return default
    out = {}
    for name, entry in raw.items():
        where = f"Resource group {name!r}"
        try:
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                raise ConfigError(f"{where}: use a short lowercase name such as gpu0.")
            entry = {} if entry is None else entry
            if not isinstance(entry, dict):
                raise ConfigError(f"{where}: settings must be an object.")
            out[name] = ResourceSpec(name, get_int(entry, "max_concurrent", 1, 1, 4, where),
                                     get_float(entry, "switch_after_s", 120.0, 5.0, 3600.0, where, " seconds"))
        except ConfigError as exc:
            problems.append(str(exc))
    return out or default


def _buffer(raw: Any, problems: list) -> tuple:
    marks = dict(DEFAULT_BUFFER)
    urgent = 80
    if raw is None:
        return marks, urgent
    if not isinstance(raw, dict):
        problems.append("buffer must be an object. Using the default targets.")
        return marks, urgent
    for key, entry in raw.items():
        if key == "urgent_priority":
            try:
                urgent = get_int({"urgent_priority": entry}, "urgent_priority", 80, 1, 100, "Buffer")
            except ConfigError as exc:
                problems.append(str(exc))
            continue
        if key not in ROLES:
            problems.append(f"Buffer: {key!r} is not a kind of audio ({', '.join(ROLES)}). It was ignored.")
            continue
        where = f"Buffer for {key}"
        try:
            if not isinstance(entry, dict):
                raise ConfigError(f"{where}: use an object like {{\"low\": 1, \"high\": 3}}.")
            lo_max, hi_max = SONG_BUFFER_MAX if key == "song" else ITEM_BUFFER_MAX
            low = get_int(entry, "low", marks[key][0], 0, lo_max, where)
            high = get_int(entry, "high", marks[key][1], 1, hi_max, where)
            if low > high:
                raise ConfigError(f"{where}: low ({low}) cannot be above high ({high}).")
            marks[key] = (low, high)
        except ConfigError as exc:
            problems.append(str(exc))
    return marks, urgent


def _small_section(raw: Any, name: str, problems: list, build: Callable):
    if raw is None:
        return build({})
    try:
        if not isinstance(raw, dict):
            raise ConfigError(f"{name} must be an object.")
        return build(raw)
    except ConfigError as exc:
        problems.append(f"{exc} Using the defaults for {name}.")
        return build({})


def parse_config(raw: Optional[Mapping], kinds: Mapping[str, Any], env: Env = Env(), *, present: bool = True) -> LoadedConfig:
    """Turn the settings object into a :class:`LoadedConfig`. ``kinds`` maps a worker ``kind`` to its class."""
    problems: list = []
    if raw is None:
        raw, present = {}, False
    elif not isinstance(raw, Mapping):
        raw = {}
        problems.append("The settings must be an object. Nothing was loaded.")
    for key in raw:
        if key not in TOP_LEVEL:
            problems.append(f"Unknown setting {str(key)[:40]!r} was ignored. Check the spelling.")
    artist = "myradio"
    if "artist" in raw:
        try:
            artist = get_str(raw, "artist", "myradio", "Settings", max_len=80) or "myradio"
        except ConfigError as exc:
            problems.append(str(exc))
    resources = _resources(raw.get("resources"), problems)
    marks, urgent = _buffer(raw.get("buffer"), problems)
    retry = _small_section(raw.get("retry"), "retry", problems, lambda r: RetrySpec(
        get_int(r, "max_attempts", 3, 1, 6, "Retry"), get_float(r, "base_s", 10.0, 1.0, 3600.0, "Retry", " seconds"),
        get_float(r, "cap_s", 300.0, 1.0, 86400.0, "Retry", " seconds"), get_int(r, "max_requeues", 2, 0, 10, "Retry")))
    if retry.cap_s < retry.base_s:
        problems.append("Retry: cap_s cannot be shorter than base_s. Using the defaults.")
        retry = RetrySpec()
    finish = _small_section(raw.get("finishing"), "finishing", problems, lambda r: FinishSpec(
        get_float(r, "target_lufs", -16.0, -30.0, -8.0, "Finishing", " LUFS"),
        get_float(r, "ceiling_dbfs", -1.0, -12.0, 0.0, "Finishing", " dBFS"), get_bool(r, "strict_clipping", False, "Finishing"),
        get_bool(r, "strict_tail", True, "Finishing")))
    limits = _small_section(raw.get("limits"), "limits", problems, lambda r: LimitSpec(
        get_int(r, "max_queued", 500, 10, 2000, "Limits"), get_int(r, "max_song_seconds", 420, 30, 580, "Limits"),
        get_int(r, "min_free_disk_mb", 500, 0, 1000000, "Limits"), get_float(r, "probe_interval_s", 60.0, 10.0, 3600.0, "Limits", " seconds")))
    workers: dict = {}
    rejected: list = []
    disabled: list = []
    claimed: dict = {}
    entries = raw.get("workers", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list) or len(entries) > 8:
        problems.append("workers must be a list of up to 8 makers.")
        entries = []
    first_resource = next(iter(resources))
    for position, entry in enumerate(entries, start=1):
        wid = entry.get("id") if isinstance(entry, dict) and isinstance(entry.get("id"), str) else f"worker{position}"
        kind = entry.get("kind") if isinstance(entry, dict) and isinstance(entry.get("kind"), str) else "?"
        where = f"Worker {wid!r}"
        try:
            if not isinstance(entry, dict):
                raise ConfigError(f"Worker number {position} must be an object.")
            if not NAME_RE.fullmatch(wid):
                raise ConfigError(f"{where}: id must be a short lowercase name such as music or voice.")
            if wid in workers or any(r.id == wid for r in rejected):
                raise ConfigError(f"{where}: this id is used twice.")
            cls = kinds.get(kind)
            if cls is None:
                raise ConfigError(f"{where}: kind {kind!r} is not one of {', '.join(sorted(kinds))}.")
            if not get_bool(entry, "enabled", True, where):
                listed = entry.get("roles")
                roles_off = tuple(dict.fromkeys(r for r in listed if isinstance(r, str) and r in cls.allowed_roles)) if isinstance(listed, list) else ()
                disabled.append(Disabled(wid, kind, roles_off or tuple(cls.default_roles)))
                continue
            resource = entry.get("resource", first_resource)
            if not isinstance(resource, str) or resource not in resources:
                raise ConfigError(f"{where}: resource must be one of {', '.join(resources)}.")
            roles = entry.get("roles", list(cls.default_roles))
            if (not isinstance(roles, list) or not roles or len(roles) > len(ROLES)
                    or not all(isinstance(r, str) and r in ROLES for r in roles)):
                raise ConfigError(f"{where}: roles must be a list chosen from {', '.join(cls.allowed_roles)}.")
            bad = [r for r in roles if r not in cls.allowed_roles]
            if bad:
                what = "Drama 3 only speaks; it never makes music." if kind == "drama_http" and "song" in bad else \
                    "The music maker only makes songs." if kind == "yue2_cli" else ""
                raise ConfigError(f"{where}: a {kind} maker cannot make {', '.join(bad)}. {what}".strip())
            roles = tuple(dict.fromkeys(roles))
            taken = [r for r in roles if r in claimed]
            if taken:
                raise ConfigError(f"{where}: {', '.join(taken)} is already handled by {claimed[taken[0]]!r}. Only one maker per kind of audio.")
            options = cls.parse_options(entry, where, env)
            spec = WorkerSpec(wid, kind, roles, resource, options, fixture=bool(cls.fixture))
            workers[wid] = spec
            for role in roles:
                claimed[role] = wid
        except ConfigError as exc:
            rejected.append(Rejected(wid, kind, str(exc)))
            problems.append(str(exc))
    return LoadedConfig(artist, resources, workers, tuple(rejected), marks, urgent, retry, finish, limits, tuple(problems),
                        fingerprint(raw), present, tuple(disabled))


EXAMPLE_CONFIG = {
    "artist": "My Radio",
    "resources": {"gpu0": {"max_concurrent": 1, "switch_after_s": 120}},
    "workers": [
        {"id": "voice", "kind": "drama_http", "resource": "gpu0", "base_url": "http://127.0.0.1:8080", "model": "drama-3",
         "request_timeout_s": 90, "warmup_timeout_s": 180, "max_take_chars": 380},
        {"id": "music", "kind": "yue2_cli", "resource": "gpu0",
         "command": ["/opt/yue2/bin/audiocpp_cli", "--task", "gen", "--family", "yue2", "--model", "/opt/yue2/models/Yue2-3B-GGUF",
                     "--backend", "sycl", "--threads", "8", "--lyrics", "{lyrics}", "--request-option", "style={tags}",
                     "--request-option", "cot=off", "--request-option", "guidance_scale=1.0", "--seed", "{seed}", "--out", "{output_file}"],
         "timeout_s": 1800,
         "model_files": ["/opt/yue2/models/Yue2-3B-GGUF/yue2-3b-q8_0.gguf"],
         "env": {"ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"},
         "env_passthrough": ["ONEAPI_ROOT"]},
    ],
    "buffer": {"song": {"low": 600, "high": 1200}, "host_break": {"low": 2, "high": 4}},
    "retry": {"max_attempts": 3, "base_s": 10, "cap_s": 300, "max_requeues": 2},
    "finishing": {"target_lufs": -16.0, "ceiling_dbfs": -1.0},
    "limits": {"max_queued": 500, "max_song_seconds": 420, "min_free_disk_mb": 500, "probe_interval_s": 60},
}
