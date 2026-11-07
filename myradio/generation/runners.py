"""The only two ways the engine reaches a model worker, both with hard deadlines.

``SubprocessRunner`` starts a program (never through a shell) in its own process group, with a scrubbed
environment and a private working directory, captures the tail of its output, and guarantees that when
it returns nothing it started is still running: TERM first, KILL after a grace period.

``HttpRunner`` makes one HTTP/1.1 request over a plain socket with an *absolute* deadline over the whole
exchange (a server that drips bytes cannot stretch it), a response size cap, no redirects and, unless the
worker opted in, loopback addresses only.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import selectors
import signal
import socket
import subprocess
import threading
import time
from typing import Mapping, Optional, Sequence

from .._platform import NEW_GROUP_FLAGS, WINDOWS, PipeReader, kill_tree
from .errors import Cancelled, WorkerError


# ---- subprocesses ---------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class RunResult:
    returncode: Optional[int]
    reason: str          # "exit", "timeout", "cancelled" or "too_large"
    stdout: str          # the last few KB, decoded leniently
    stderr: str
    elapsed_s: float
    killed: bool         # the group needed SIGKILL after SIGTERM
    leaked: bool         # something in the group could not be stopped


class _Ring:
    def __init__(self, limit: int):
        self.limit, self.buf = limit, bytearray()

    def add(self, data: bytes) -> None:
        self.buf += data
        if len(self.buf) > self.limit * 2:
            del self.buf[: len(self.buf) - self.limit]

    def text(self) -> str:
        return bytes(self.buf[-self.limit:]).decode("utf-8", "replace")


def tree_bytes(path, stop_after: int, max_entries: int = 5000) -> int:
    """Total apparent size of files under ``path`` (symlinks not followed), stopping early past ``stop_after``."""
    total, seen, stack = 0, 0, [os.fspath(path)]
    while stack:
        try:
            iterator = os.scandir(stack.pop())
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                seen += 1
                if seen > max_entries:
                    return stop_after + 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if total > stop_after:
                    return total
    return total


def build_env(passthrough: Sequence[str], extra: Mapping[str, str], private_dir, environ: Optional[Mapping[str, str]] = None) -> dict:
    """A scrubbed environment: only the named variables, the operator's fixed values, and a private temp directory."""
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in passthrough if name in source}
    env.update(extra)
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = os.fspath(private_dir)
    return env


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class SubprocessRunner:
    """Run one program to completion or to its deadline. Stateless and safe to share between threads."""

    def __init__(self, *, poll_s: float = 0.05, ring_bytes: int = 8192):
        self.poll_s = poll_s
        self.ring_bytes = ring_bytes

    def run(self, argv: Sequence[str], *, cwd, env: Mapping[str, str], deadline_s: float, cancel: Optional[threading.Event] = None,
            max_dir_bytes: Optional[int] = None, grace_s: float = 5.0, drain_s: float = 0.5) -> RunResult:
        if (not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv)):
            raise WorkerError("worker_failed", "The command is not valid.", retryable=False, detail="argv must be a non-empty list of strings")
        started = time.monotonic()
        deadline = started + deadline_s
        if WINDOWS:
            return self._run_windows(argv, cwd=cwd, env=env, deadline=deadline, started=started, cancel=cancel,
                                     max_dir_bytes=max_dir_bytes, grace_s=grace_s, drain_s=drain_s)
        try:
            proc = subprocess.Popen(list(argv), cwd=os.fspath(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, shell=False, close_fds=True, start_new_session=True)
        except (OSError, ValueError) as exc:
            raise WorkerError("worker_failed", "The program could not be started. Check that it is installed and allowed to run.",
                              detail=f"{type(exc).__name__}: {exc}") from None
        pgid = proc.pid
        selector = selectors.DefaultSelector()
        rings = {"out": _Ring(self.ring_bytes), "err": _Ring(self.ring_bytes)}
        reason = None
        try:
            for stream, name in ((proc.stdout, "out"), (proc.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            exited_at: Optional[float] = None
            next_size_check = started
            while True:
                now = time.monotonic()
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                elif now >= deadline:
                    reason = "timeout"
                elif max_dir_bytes and now >= next_size_check:
                    next_size_check = now + 0.2
                    if tree_bytes(cwd, max_dir_bytes) > max_dir_bytes:
                        reason = "too_large"
                if reason:
                    break
                if exited_at is None and proc.poll() is not None:
                    exited_at = now
                if exited_at is not None and (not selector.get_map() or now - exited_at >= drain_s):
                    break
                for key, _ in selector.select(min(self.poll_s, max(0.0, deadline - now))):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if chunk:
                        rings[key.data].add(chunk)
                    else:
                        selector.unregister(key.fileobj)
            reason = reason or "exit"
            killed, leaked = self._stop_group(proc, pgid, grace_s if reason != "exit" else min(grace_s, 1.0))
            self._drain(selector, rings)
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            selector.close()
            if proc.poll() is None:
                _signal_group(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        return RunResult(proc.returncode, reason, rings["out"].text(), rings["err"].text(), time.monotonic() - started, killed, leaked)

    @staticmethod
    def _stop_group(proc: subprocess.Popen, pgid: int, grace_s: float) -> tuple:
        """Make sure no process of the group is left. Returns (needed_kill, still_alive)."""
        proc.poll()  # reap the leader if it already exited, so it is not counted as a live group member
        if not _group_alive(pgid):
            return False, False
        _signal_group(pgid, signal.SIGTERM)
        end = time.monotonic() + max(0.0, grace_s)
        while time.monotonic() < end:
            proc.poll()
            if not _group_alive(pgid):
                return False, False
            time.sleep(0.02)
        _signal_group(pgid, signal.SIGKILL)
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            proc.poll()
            if not _group_alive(pgid):
                return True, False
            time.sleep(0.02)
        return True, True

    def _run_windows(self, argv: Sequence[str], *, cwd, env: Mapping[str, str], deadline: float, started: float,
                     cancel: Optional[threading.Event], max_dir_bytes: Optional[int], grace_s: float, drain_s: float) -> RunResult:
        """The same contract on Windows, which has no process groups to signal and cannot select() on pipes.

        Helper threads read the two pipes. Stopping uses ``taskkill /T /F``, which ends the program and the
        programs it started while it is still running; there is no polite TERM step for console programs.
        A program that started others and then exited by itself cannot be traced afterwards.
        """
        try:
            proc = subprocess.Popen(list(argv), cwd=os.fspath(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, shell=False, close_fds=True, creationflags=NEW_GROUP_FLAGS)
        except (OSError, ValueError) as exc:
            raise WorkerError("worker_failed", "The program could not be started. Check that it is installed and allowed to run.",
                              detail=f"{type(exc).__name__}: {exc}") from None
        rings = {"out": _Ring(self.ring_bytes), "err": _Ring(self.ring_bytes)}
        readers = {"out": PipeReader(proc.stdout), "err": PipeReader(proc.stderr)}
        live = set(readers)
        reason = None
        killed = leaked = False
        try:
            exited_at: Optional[float] = None
            next_size_check = started
            while True:
                now = time.monotonic()
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                elif now >= deadline:
                    reason = "timeout"
                elif max_dir_bytes and now >= next_size_check:
                    next_size_check = now + 0.2
                    if tree_bytes(cwd, max_dir_bytes) > max_dir_bytes:
                        reason = "too_large"
                if reason:
                    break
                if exited_at is None and proc.poll() is not None:
                    exited_at = now
                if exited_at is not None and (not live or now - exited_at >= drain_s):
                    break
                got = False
                for name in sorted(live):
                    chunk = readers[name].read(0)
                    if chunk is None:
                        continue
                    got = True
                    if chunk:
                        rings[name].add(chunk)
                    else:
                        live.discard(name)
                if not got:
                    time.sleep(min(self.poll_s, max(0.0, deadline - now)))
            reason = reason or "exit"
            if proc.poll() is None:
                kill_tree(proc)
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
                killed, leaked = True, proc.poll() is None
            for name in sorted(live):  # what is already sitting in the pipes
                for _ in range(32):
                    chunk = readers[name].read(0.05)
                    if not chunk:
                        break
                    rings[name].add(chunk)
        finally:
            if proc.poll() is None:
                kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for reader in readers.values():
                reader.close(wait=0.5)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        return RunResult(proc.returncode, reason, rings["out"].text(), rings["err"].text(), time.monotonic() - started, killed, leaked)

    @staticmethod
    def _drain(selector: selectors.BaseSelector, rings: Mapping[str, _Ring]) -> None:
        """Collect what is already sitting in the pipes after the group is gone."""
        for key in list(selector.get_map().values()):
            for _ in range(32):
                try:
                    chunk = os.read(key.fd, 65536)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    break
                rings[key.data].add(chunk)


# ---- HTTP -----------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class HttpResponse:
    status: int
    reason: str
    headers: dict
    body: bytes
    elapsed_s: float


_HEADER_NAME_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_STATUS_RE = re.compile(rb"HTTP/1\.[01] (\d{3})(?: ([^\r\n]*))?")
_RESOLVERS = threading.BoundedSemaphore(2)
USER_AGENT = "myradio-generation/1"


def is_loopback_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


class HttpRunner:
    def __init__(self, *, max_header_bytes: int = 32768):
        self.max_header_bytes = max_header_bytes

    # -- address handling ---
    @staticmethod
    def _resolve(host: str, port: int, deadline: float, cancel: Optional[threading.Event]) -> list:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET, (host, port))]
        if not _RESOLVERS.acquire(blocking=False):
            raise WorkerError("worker_unreachable", "The computer's name lookup is stuck, so the maker could not be reached.",
                              detail="too many unfinished name lookups")
        box: dict = {}

        def lookup() -> None:
            try:
                box["found"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                box["error"] = exc
            finally:
                _RESOLVERS.release()

        thread = threading.Thread(target=lookup, name="myradio-gen-resolve", daemon=True)
        thread.start()
        while thread.is_alive():
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker_timeout", "The maker's address could not be looked up in time.", detail=f"lookup of {host}")
            thread.join(min(0.1, remaining))
        if "error" in box or not box.get("found"):
            raise WorkerError("worker_unreachable", "The maker's address could not be found. Check the address in your settings.",
                              detail=f"lookup of {host} failed")
        return [(family, sockaddr) for family, _, _, _, sockaddr in box["found"] if family in (socket.AF_INET, socket.AF_INET6)]

    def _connect(self, host: str, port: int, deadline: float, cancel: Optional[threading.Event], allow_remote: bool) -> socket.socket:
        addresses = self._resolve(host, port, deadline, cancel)
        if not allow_remote:
            addresses = [a for a in addresses if is_loopback_address(a[1][0])]
            if not addresses:
                raise WorkerError("worker_blocked", "That address is not on this computer, and only this computer is allowed.",
                                  retryable=False, detail=f"{host} is not a loopback address")
        last: Optional[Exception] = None
        for family, sockaddr in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker_timeout", "The maker did not answer in time.", detail="connect deadline")
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(min(remaining, 10.0))
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise WorkerError("worker_unreachable", "The maker did not answer. Check that it is running, then try again.",
                          detail=f"{type(last).__name__}: {last}" if last else "no address")

    # -- reading ---
    @staticmethod
    def _recv(sock: socket.socket, deadline: float, cancel: Optional[threading.Event], size: int = 65536) -> bytes:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker_timeout", "The maker took too long to answer and was given up on.", detail="response deadline")
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            sock.settimeout(min(remaining, 0.25))
            try:
                return sock.recv(size)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                raise WorkerError("worker_unreachable", "The connection to the maker was lost.", detail=f"{type(exc).__name__}: {exc}") from None

    def _read_head(self, sock, deadline, cancel) -> tuple:
        """Return (status, reason, headers, leftover_body_bytes), skipping interim 1xx answers."""
        buffer = bytearray()
        while True:
            while True:
                crlf, lf = buffer.find(b"\r\n\r\n"), buffer.find(b"\n\n")
                cut = min((i for i in (crlf, lf) if i >= 0), default=-1)
                if cut >= 0:
                    width = 4 if cut == crlf else 2
                    head, rest = bytes(buffer[:cut]), bytes(buffer[cut + width:])
                    break
                if len(buffer) > self.max_header_bytes:
                    raise WorkerError("bad_output", "The maker sent back something that is not a normal web answer.", detail="header block too large")
                data = self._recv(sock, deadline, cancel)
                if not data:
                    raise WorkerError("bad_output", "The maker closed the connection before answering.", detail="closed before headers")
                buffer += data
            lines = head.replace(b"\r\n", b"\n").split(b"\n")
            match = _STATUS_RE.fullmatch(lines[0])
            if not match:
                raise WorkerError("bad_output", "The maker sent back something that is not a normal web answer.", detail=lines[0][:60].decode("latin-1"))
            status = int(match.group(1))
            headers: dict = {}
            if len(lines) > 101:
                raise WorkerError("bad_output", "The maker sent back too many headers.", detail="more than 100 headers")
            for line in lines[1:]:
                name, sep, value = line.partition(b":")
                if not sep or not _HEADER_NAME_RE.fullmatch(name):
                    raise WorkerError("bad_output", "The maker sent back a malformed answer.", detail="bad header line")
                key = name.decode("ascii").lower()
                text = value.strip().decode("latin-1")
                if key in headers and key in ("content-length", "transfer-encoding", "content-type") and headers[key] != text:
                    raise WorkerError("bad_output", "The maker sent back a confusing answer.", detail=f"conflicting {key}")
                headers[key] = text
            if 100 <= status < 200 and status != 101:
                buffer = bytearray(rest)
                continue
            return status, (match.group(2) or b"").decode("latin-1"), headers, rest

    def _read_body(self, sock, headers, first: bytes, limit: int, deadline, cancel, status: int, head_only: bool) -> bytes:
        if head_only or status in (204, 304) or 100 <= status < 200:
            return b""
        encoding = headers.get("transfer-encoding", "").lower().strip()
        length = headers.get("content-length")
        if encoding and length is not None:
            raise WorkerError("bad_output", "The maker sent back a confusing answer.", detail="both chunked and content-length")
        if encoding:
            if encoding != "chunked":
                raise WorkerError("bad_output", "The maker sent back audio in a form that cannot be read.", detail=f"transfer-encoding {encoding[:30]}")
            return self._read_chunked(sock, first, limit, deadline, cancel)
        if headers.get("content-encoding", "identity").lower() not in ("identity", ""):
            raise WorkerError("bad_output", "The maker sent back compressed data that cannot be read.", detail="content-encoding")
        if length is not None:
            if not re.fullmatch(r"\d{1,12}", length):
                raise WorkerError("bad_output", "The maker sent back a confusing answer.", detail="bad content-length")
            expected = int(length)
            if expected > limit:
                raise WorkerError("output_too_large", "The maker sent back far more data than expected, so it was ignored.",
                                  retryable=False, detail=f"content-length {expected} exceeds {limit}")
            body = bytearray(first[:expected])
            while len(body) < expected:
                data = self._recv(sock, deadline, cancel, min(65536, expected - len(body)))
                if not data:
                    raise WorkerError("bad_output", "The maker's answer was cut off before it finished.",
                                      detail=f"got {len(body)} of {expected} bytes")
                body += data
            return bytes(body)
        body = bytearray(first)
        while True:
            if len(body) > limit:
                raise WorkerError("output_too_large", "The maker sent back far more data than expected, so it was ignored.",
                                  retryable=False, detail=f"body exceeds {limit}")
            data = self._recv(sock, deadline, cancel)
            if not data:
                return bytes(body)
            body += data

    def _read_chunked(self, sock, first: bytes, limit: int, deadline, cancel) -> bytes:
        pending = bytearray(first)
        body = bytearray()

        def fill() -> None:
            data = self._recv(sock, deadline, cancel)
            if not data:
                raise WorkerError("bad_output", "The maker's answer was cut off before it finished.", detail="chunked body ended early")
            pending.extend(data)

        def line() -> bytes:
            while True:
                end = pending.find(b"\n")
                if end >= 0:
                    value = bytes(pending[:end]).rstrip(b"\r")
                    del pending[: end + 1]
                    return value
                if len(pending) > 4096:
                    raise WorkerError("bad_output", "The maker sent back a malformed answer.", detail="chunk header too long")
                fill()

        while True:
            size_line = line().split(b";", 1)[0].strip()
            if not re.fullmatch(rb"[0-9A-Fa-f]{1,10}", size_line):
                raise WorkerError("bad_output", "The maker sent back a malformed answer.", detail="bad chunk size")
            size = int(size_line, 16)
            if size == 0:
                for _ in range(64):
                    if not line():
                        return bytes(body)
                raise WorkerError("bad_output", "The maker sent back a malformed answer.", detail="too many trailers")
            if len(body) + size > limit:
                raise WorkerError("output_too_large", "The maker sent back far more data than expected, so it was ignored.",
                                  retryable=False, detail=f"chunked body exceeds {limit}")
            while len(pending) < size + 1:
                fill()
            body += pending[:size]
            del pending[:size]
            if line():
                raise WorkerError("bad_output", "The maker sent back a malformed answer.", detail="missing chunk terminator")

    # -- public ---
    def request(self, method: str, host: str, port: int, path: str, *, body: bytes = b"", headers: Optional[Mapping[str, str]] = None,
                deadline_s: float, max_response_bytes: int, cancel: Optional[threading.Event] = None,
                allow_remote: bool = False) -> HttpResponse:
        """One request, one answer. No redirects are followed; a 3xx is returned like any other status."""
        started = time.monotonic()
        deadline = started + deadline_s
        if method not in ("GET", "POST") or not path.startswith("/") or re.search(r"[\s\x00-\x1f\x7f]", path):
            raise WorkerError("worker_failed", "The request is not valid.", retryable=False, detail="bad method or path")
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host if ':' not in host else '[' + host + ']'}:{port}", f"User-Agent: {USER_AGENT}",
                 "Accept: */*", "Accept-Encoding: identity", "Connection: close"]
        for name, value in (headers or {}).items():
            if not _HEADER_NAME_RE.fullmatch(name.encode("ascii", "replace")) or re.search(r"[\x00-\x1f\x7f]", value):
                raise WorkerError("worker_failed", "The request is not valid.", retryable=False, detail="bad header")
            lines.append(f"{name}: {value}")
        if method == "POST":
            lines.append(f"Content-Length: {len(body)}")
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", "replace") + (body if method == "POST" else b"")
        sock = self._connect(host, port, deadline, cancel, allow_remote)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker_timeout", "The maker did not answer in time.", detail="send deadline")
            sock.settimeout(remaining)
            try:
                sock.sendall(payload)
            except (socket.timeout, TimeoutError):
                raise WorkerError("worker_timeout", "The maker did not accept the request in time.", detail="send timed out") from None
            except OSError as exc:
                raise WorkerError("worker_unreachable", "The connection to the maker was lost.", detail=f"{type(exc).__name__}: {exc}") from None
            status, reason, resp_headers, first = self._read_head(sock, deadline, cancel)
            data = self._read_body(sock, resp_headers, first, max_response_bytes, deadline, cancel, status, False)
            return HttpResponse(status, reason, resp_headers, data, time.monotonic() - started)
        finally:
            try:
                sock.close()
            except OSError:
                pass
