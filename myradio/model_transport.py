"""Direct model HTTP(S) POSTs bounded by ONE absolute deadline.

``ModelTransport.post`` sends one request and returns the response body. DNS, connect,
TLS handshake, request send, response headers and body all share a single monotonic
deadline taken from ``timeout``. It is a wall-clock budget for the whole exchange, not a
per-socket inactivity timeout, so a peer that keeps trickling bytes cannot extend it.

How the deadline is enforced
----------------------------
No watchdog thread and no cross-thread socket shutdown are used (those race with connect,
TLS and close, and ``SSLSocket.shutdown`` discards the TLS object under a thread that is
still using it). Instead every blocking call is individually bounded by the time left:

* name lookup runs on one shared helper thread; the caller waits on it for the time left
* connect and the TLS handshake run with their socket timeout set to the time left
  (CPython applies that timeout to the whole handshake call). While more addresses remain
  an attempt gets at most ``CONNECT_SLICE`` seconds, at most ``MAX_ADDRESSES`` are tried, and
  address families alternate (the resolver's first choice first), so a few dead IPv6 records
  cannot use up every attempt before IPv4 is tried
* the request is sent in slices, re-arming the socket timeout from the time left before each one
* every ``recv`` is issued through a reader that re-arms the timeout from the time left,
  so header and body reads (including a drip-feed of one byte at a time) end on time

Failures
--------
Every failure (DNS, refusal, TLS, malformed, truncated or oversize HTTP, non-200 status,
missed deadline, unexpected library error) is raised as ``ModelTransportError``, an
``OSError``. A missed deadline is the ``ModelDeadlineError`` subclass, also a
``TimeoutError``. Messages are fixed text: never a URL, header, credential or peer data.
``phase`` says where it failed and ``kind`` names the underlying exception class (``None``
when the transport's own checks failed, so there was no underlying exception).
``TypeError``/``ValueError`` are reserved for misuse of ``post`` itself (a body that is not
bytes, a timeout that is not a number of seconds above 0 and at most 24 hours).
``BaseException`` (for example ``KeyboardInterrupt``) is never caught; sockets are closed
on the way out.

Name lookups
------------
``getaddrinfo`` cannot be cancelled, so lookups run on at most ONE daemon helper thread
(``myradio-model-dns``). A pending lookup for the same host and port is joined, never
duplicated. A request for a different name (for example after the operator changes the
endpoint) waits for the helper to become free, within its own deadline, and then runs its
own lookup. A lookup that outlives its request is the only leftover, and it ends by itself.
Only IP literals in their canonical spelling (the ones ``ipaddress`` reads, such as
``127.0.0.1`` and ``[::1]`` in a URL) and ``localhost`` need no lookup, so only they never
depend on the helper. Every other host is a name, including the old-style numeric spellings
such as ``127.1``, ``0x7f.1`` or ``2130706433``: those are handed to ``getaddrinfo`` through
the helper like any other name, and are not rejected. Completed lookups are not cached.

TLS
---
One verifying client context (``ssl.create_default_context()``: certificate and hostname
checks on, ALPN ``http/1.1`` offered) is built lazily, once per transport, and reused by
every https request. A context passed in by the caller is used as it is. The certificate
is checked against, and SNI carries, the host from the URL without a trailing root dot
(``model.test.`` is verified as ``model.test``); an IP literal is verified against the
certificate's IP addresses and sends no SNI.

Behaviour changes from the previous urllib-based path
-----------------------------------------------------
* Proxies are never used. HTTP_PROXY, HTTPS_PROXY, ALL_PROXY and NO_PROXY, and on macOS
  the system proxy settings, are ignored: the connection is always direct, so the API key
  is only ever sent to the configured endpoint and the one deadline covers the whole
  exchange. Reaching a provider only through a proxy no longer works.
* Redirects are never followed and only status 200 is accepted; credentials never go to
  another host.
* The body is capped at 1 MiB and everything received (headers, chunk framing, trailers)
  at about 4 MiB. ``Content-Length`` must be a single valid number and the body must match
  it, the header block must end with its blank line, and ``Transfer-Encoding`` may only be
  ``chunked``.
* The URL must be http or https with a host, no userinfo, no whitespace or control
  characters, and ASCII only. The host has no ``%`` (so no IPv6 zone id such as
  ``fe80::1%eth0``), the port is 1 to 65535, and an IPv6 literal is in brackets. The query
  string is kept.
* ``Connection: close`` is always sent and the connection is never reused.

``urllib.request`` is not used at all: only ``urllib.parse.urlsplit`` reads the URL, and the
whole exchange is implemented here.
"""
from __future__ import annotations

import http.client
import io
import ipaddress
import itertools
import re
import socket
import ssl
import threading
import time
from typing import NamedTuple
from urllib.parse import urlsplit

from . import __version__

__all__ = ["MAX_RESPONSE", "ModelDeadlineError", "ModelTransport", "ModelTransportError"]

MAX_RESPONSE = 1024 * 1024      # response body cap, in bytes
MAX_WIRE = 4 * MAX_RESPONSE + 64 * 1024  # everything received (headers, framing, trailers), in bytes
MAX_TIMEOUT = 24 * 60 * 60      # sanity bound on the caller's budget, in seconds
MAX_URL = 8192
MAX_ADDRESSES = 4               # connection attempts per request
CONNECT_SLICE = 3.0             # seconds per attempt while other addresses remain
SEND_SLICE = 64 * 1024          # bytes per send call; the clock is re-read between slices

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_VALUE = re.compile(r"[\t\x20-\x7e]*")
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
_NAME = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}\.?")   # dotted labels, no empty label
_LENGTH = re.compile(r"[0-9]{1,15}")
# Set by the transport itself; a caller cannot override request framing.
_RESERVED = frozenset({"host", "content-length", "transfer-encoding", "connection", "accept-encoding"})


class ModelTransportError(OSError):
    """A model request failed. The message is fixed text with no URL, key or peer data."""

    def __init__(self, message="model request failed", phase=None, kind=None):
        super().__init__(message)
        self.phase = phase   # dns, connect, tls, send, headers, body, request, status
        self.kind = kind     # class name of the underlying error, if any


class ModelDeadlineError(ModelTransportError, TimeoutError):
    """The absolute request deadline passed."""

    def __init__(self, phase=None, kind=None):
        super().__init__("model request deadline exceeded", phase, kind)


class _Budget:
    """The one absolute deadline of a request, plus the phase it is currently in."""

    __slots__ = ("end", "phase")

    def __init__(self, seconds):
        self.end = time.monotonic() + seconds
        self.phase = "request"

    def left(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ModelDeadlineError(self.phase)
        return remaining


class _Endpoint(NamedTuple):
    tls: bool
    host: str
    port: int
    target: str       # request target: path plus query string
    host_header: str


def _ip_version(host):
    """4 or 6 for an IP literal, else 0."""
    try:
        return ipaddress.ip_address(host).version
    except ValueError:
        return 0


def _split(url):
    """(parts, port), or None when urllib cannot parse it."""
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:
        return None


def _endpoint(url):
    """Validate a model URL and split it into what the wire needs.

    Rejections are raised outside any ``except`` block so that no library exception, whose text
    may quote part of the URL, stays reachable through ``__context__``."""
    split = _split(url) if isinstance(url, str) and len(url) <= MAX_URL and url.isascii() \
        and not any(c <= " " or c == "\x7f" for c in url) else None
    parts, port = split or (None, None)
    host = parts.hostname if parts else None
    version = _ip_version(host) if host else 0
    if parts is None or parts.scheme not in ("http", "https") or not host or "@" in parts.netloc \
            or "%" in parts.netloc or port == 0 \
            or (version == 0 and (len(host) > 253 or not _NAME.fullmatch(host))) \
            or ("[" in parts.netloc) != (version == 6):  # brackets mean exactly an IPv6 literal
        raise ModelTransportError("invalid model URL", "request")
    tls = parts.scheme == "https"
    default = 443 if tls else 80
    shown = f"[{host}]" if version == 6 else host
    return _Endpoint(tls, host, default if port is None else port,
                     (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                     shown if port in (None, default) else f"{shown}:{port}")


def _payload(endpoint, headers, body):
    """Serialise the request. Header names and values are validated, never echoed."""
    lines = [f"POST {endpoint.target} HTTP/1.1", f"Host: {endpoint.host_header}",
             f"Content-Length: {len(body)}", "Connection: close", "Accept-Encoding: identity"]
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ModelTransportError("invalid request header", "request")
        if name.lower() in _RESERVED:
            continue
        if not _TOKEN.fullmatch(name) or not _VALUE.fullmatch(value):
            raise ModelTransportError("invalid request header", "request")
        lines.append(f"{name.title()}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _fixed_addresses(host, port):
    """Addresses that need no name lookup (canonical IP literals, as ``ipaddress`` reads them,
    and localhost), else None. Anything else, such as ``127.1``, is looked up like a name."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host != "localhost":
            return None
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", port, 0, 0))]
    if ip.version == 4:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (str(ip), port))]
    return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (str(ip), port, 0, 0))]


def _system_lookup(host, port):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _interleave(addresses):
    """Alternate the address families, keeping the resolver's order inside each family and
    starting with its first choice. Cutting the list at ``MAX_ADDRESSES`` without this would
    let a few dead addresses of one family (four unreachable AAAA records, say) use up every
    connection attempt before the other family is tried."""
    by_family = {}
    for entry in addresses:
        by_family.setdefault(entry[0], []).append(entry)
    return [entry for group in itertools.zip_longest(*by_family.values())
            for entry in group if entry is not None]


class _Lookup:
    __slots__ = ("key", "done", "addresses", "kind")

    def __init__(self, key):
        self.key = key
        self.done = threading.Event()
        self.addresses = None
        self.kind = None


class _Resolver:
    """Name lookups on at most one helper thread, shared by every request."""

    def __init__(self, lookup):
        self._lookup = lookup
        self._lock = threading.Lock()
        self._current = None

    def resolve(self, host, port, budget):
        fixed = _fixed_addresses(host, port)
        if fixed is not None:
            return fixed
        key = (host, port)
        while True:
            with self._lock:
                job = self._current
                if job is None or job.done.is_set():
                    job = self._begin(key)
                    break
                if job.key == key:
                    break  # join the lookup that is already pending
            # A different name owns the only helper thread: wait for it, within our deadline.
            job.done.wait(budget.left())
        if not job.done.wait(budget.left()):
            raise ModelDeadlineError("dns")
        if not job.addresses:
            raise ModelTransportError("name lookup failed", "dns", job.kind)
        return job.addresses

    def _begin(self, key):
        """Start the helper thread for a new lookup, and only then record it as the current one,
        so a thread that cannot be started leaves nothing behind that waits for a finisher.
        Called with the lock held."""
        job = _Lookup(key)
        threading.Thread(target=self._work, args=(job,), name="myradio-model-dns", daemon=True).start()
        self._current = job
        return job

    def _work(self, job):
        try:
            job.addresses = list(self._lookup(*job.key)) or None
        except Exception as exc:  # noqa: BLE001 - reported as a failed lookup, class name only
            job.kind = type(exc).__name__
        finally:
            job.done.set()


class _Reader(io.RawIOBase):
    """Raw socket reads that re-arm the socket timeout from the time left on every call."""

    def __init__(self, sock, budget):
        super().__init__()
        self._sock = sock
        self._budget = budget
        self.eof = False
        self.received = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(self._budget.left())
        count = self._sock.recv_into(buffer)
        if count == 0:
            self.eof = True
        self.received += count
        if self.received > MAX_WIRE:  # endless headers or trailers: bounded in bytes, not only in time
            raise ModelTransportError("response too large", self._budget.phase)
        return count


class _Wire:
    """The only part of a socket that ``http.client.HTTPResponse`` uses: ``makefile``."""

    def __init__(self, sock, budget):
        self._reader = _Reader(sock, budget)

    @property
    def eof(self):
        return self._reader.eof

    def makefile(self, mode="rb", *args, **kwargs):
        if mode != "rb":
            raise ValueError("read-only wire")
        return io.BufferedReader(self._reader)


def _quiet_close(thing):
    try:
        thing.close()
    except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
        pass


def _send_all(sock, data, budget):
    view = memoryview(data)
    while len(view):
        sock.settimeout(budget.left())
        sent = sock.send(view[:SEND_SLICE])
        if sent <= 0:
            raise ModelTransportError("connection closed while sending", "send")
        view = view[sent:]


def _length(response):
    """The declared body length: None when chunked or absent; strict when present."""
    encodings = response.headers.get_all("Transfer-Encoding") or []
    if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != "chunked"):
        raise ModelTransportError("unsupported transfer encoding", "headers")
    if response.chunked:
        return None
    values = {v.strip() for v in response.headers.get_all("Content-Length") or []}
    if not values:
        return None
    if len(values) != 1 or not _LENGTH.fullmatch(next(iter(values))):
        raise ModelTransportError("invalid content length", "headers")
    return int(next(iter(values)))


def _read_body(response, length):
    if length is not None and length > MAX_RESPONSE:
        raise ModelTransportError("response too large", "body")
    raw = response.read(MAX_RESPONSE + 1 if length is None else length)
    if len(raw) > MAX_RESPONSE:
        raise ModelTransportError("response too large", "body")
    if length is not None and len(raw) != length:
        raise ModelTransportError("truncated response", "body")
    return raw


class _Exchange:
    """One request: every stage is bounded by the same budget."""

    def __init__(self, transport, endpoint, payload, budget):
        self.transport, self.endpoint, self.payload, self.budget = transport, endpoint, payload, budget

    def run(self):
        endpoint, budget = self.endpoint, self.budget
        sock = response = None
        try:
            budget.phase = "dns"
            addresses = self.transport._resolver.resolve(endpoint.host, endpoint.port, budget)
            budget.phase = "connect"
            sock = self._connect(addresses)
            if endpoint.tls:
                budget.phase = "tls"
                sock = self._handshake(sock)
            budget.phase = "send"
            _send_all(sock, self.payload, budget)
            budget.phase = "headers"
            wire = _Wire(sock, budget)
            response = http.client.HTTPResponse(wire, method="POST")
            response.begin()
            if wire.eof:  # http.client takes EOF as the end of the headers; a real block ends in a blank line
                raise ModelTransportError("incomplete response headers", "headers")
            if response.status != 200:  # redirects and errors are never followed or read
                raise ModelTransportError("unexpected response status", "status")
            length = _length(response)
            budget.phase = "body"
            return _read_body(response, length)
        finally:
            if response is not None:
                _quiet_close(response)
            if sock is not None:
                _quiet_close(sock)

    def _connect(self, addresses):
        budget = self.budget
        usable = _interleave(addresses)[:MAX_ADDRESSES]
        for index, (family, kind, proto, _canon, address) in enumerate(usable):
            left = budget.left()
            sock = None
            try:
                sock = socket.socket(family, kind, proto)
                # The last address may use everything that is left; earlier ones are sliced.
                sock.settimeout(left if index == len(usable) - 1 else min(left, CONNECT_SLICE))
                sock.connect(address)
                return sock
            except OSError:
                if sock is not None:
                    _quiet_close(sock)
            except BaseException:
                if sock is not None:
                    _quiet_close(sock)
                raise
        budget.left()  # a missed deadline is reported as one
        raise ModelTransportError("could not connect", "connect")

    def _handshake(self, sock):
        budget = self.budget
        context = self.transport._tls_context()
        budget.left()
        # The certificate check and SNI want "model.test", not the absolute form "model.test.".
        name = self.endpoint.host[:-1] if self.endpoint.host.endswith(".") else self.endpoint.host
        try:
            secure = context.wrap_socket(sock, server_hostname=name, do_handshake_on_connect=False)
        except BaseException:
            _quiet_close(sock)
            raise
        try:
            secure.settimeout(budget.left())
            secure.do_handshake()  # bounded as a whole by the timeout just set
        except BaseException:
            _quiet_close(secure)
            raise
        return secure


class ModelTransport:
    """Reusable and thread-safe: holds only the resolver and the TLS context."""

    def __init__(self, resolver=None, ssl_context=None):
        """``resolver(host, port)`` returns ``socket.getaddrinfo``-style tuples (injectable
        for tests); ``ssl_context`` replaces the default verifying client context."""
        self._resolver = _Resolver(resolver or _system_lookup)
        self._tls = ssl_context
        self._tls_lock = threading.Lock()

    def _tls_context(self):
        with self._tls_lock:
            if self._tls is None:
                context = ssl.create_default_context()
                if ssl.HAS_ALPN:
                    context.set_alpn_protocols(["http/1.1"])  # say what is spoken; an injected context is left alone
                self._tls = context
            return self._tls

    def post(self, url, body, headers, *, timeout):
        """POST ``body`` (bytes) and return the response body (at most ``MAX_RESPONSE``).

        The whole exchange must finish within ``timeout`` seconds."""
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("body must be bytes")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError("timeout must be a number of seconds greater than 0")
        budget = _Budget(timeout)
        failure = None
        try:
            body = bytes(body)
            headers = dict(headers or {})
            endpoint = _endpoint(url)
            if not any(isinstance(k, str) and k.lower() == "user-agent" for k in headers):
                headers["User-Agent"] = "myradio/" + __version__
            return _Exchange(self, endpoint, _payload(endpoint, headers, body), budget).run()
        except Exception as exc:  # noqa: BLE001 - normalised below; BaseException is never caught
            failure = self._failure(exc, budget)
        raise failure  # outside the handler: no chained library exception, no peer text

    @staticmethod
    def _failure(exc, budget):
        if isinstance(exc, ModelTransportError):
            return exc
        kind = type(exc).__name__
        if isinstance(exc, TimeoutError):
            return ModelDeadlineError(budget.phase, kind)
        return ModelTransportError("model request failed", budget.phase, kind)
