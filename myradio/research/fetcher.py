"""A small, strict HTTPS client for feeds and web pages.

It is deliberately not ``urllib``: every limit below has to hold for the whole exchange, and every address has to
be checked *before* anything is connected.

What it guarantees
------------------
* Only ``https`` on port 443 (a loopback fixture mode exists solely as a constructor argument for tests).
* Every hop (the first request and each redirect, at most 3) is parsed, resolved, and checked again. A name that
  resolves to any loopback, private, link-local, multicast, reserved or cloud-metadata address is refused.
* The connection goes to a checked IP address, with the original name used for SNI, certificate checking and the
  ``Host`` header. The name is resolved once per hop, so DNS cannot change the target between check and connect.
* One absolute deadline covers lookup, connect, handshake, headers and body, including all redirects. Sockets are
  polled in short slices, so a slow or silent server (or a shutdown request) can never hold the caller longer. Time
  spent waiting out the polite gap between requests to one host (at most 30 seconds each) does not count against it.
* Responses are capped at 2 MiB on the wire and 2 MiB after decompression, headers at 64 KiB, and only RSS,
  Atom, JSON Feed, HTML and plain-text content types are accepted.
* No cookies, no ``Authorization`` or ``Referer``, no proxies, no HTTP/2, no connection reuse.
* A polite user agent, a minimum gap between requests to the same host (with ``Retry-After`` honoured), ETag and
  Last-Modified conditional requests, and a daily request cap.
"""
from __future__ import annotations

import collections
import datetime as dt
import email.utils
import errno
import os
import re
import selectors
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Optional

from .. import __version__
from .errors import FetchError, describe_elsewhere, fail
from .netguard import Resolver, Target, check_addresses, parse_target

MAX_BYTES = 2 * 1024 * 1024
MAX_HEAD = 64 * 1024
MAX_HEADER_LINES = 100
MAX_CHUNKS = 20_000
MAX_REDIRECTS = 3
DEFAULT_DEADLINE = 20.0
MAX_BACKOFF_WAIT = 30.0
MAX_RETRY_AFTER = 3600.0
ALLOWED_TYPES = frozenset({
    "application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/xml", "text/xml",
    "application/feed+json", "application/json", "text/html", "application/xhtml+xml", "text/plain",
})
ACCEPT = ("application/rss+xml, application/atom+xml, application/feed+json, application/xml;q=0.9, text/xml;q=0.9, "
          "application/json;q=0.8, text/html;q=0.7, text/plain;q=0.5")
_REDIRECTS = (301, 302, 303, 307, 308)
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e]{0,200}"')
_CONTACT = re.compile(r"[A-Za-z0-9@._:/+\-]{3,100}")
_CHARSET = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9_.:\-]{1,40})", re.IGNORECASE)
_STATUS = re.compile(rb"HTTP/1\.[01] (\d{3})(?: [^\r\n]*)?")
_READ, _WRITE = selectors.EVENT_READ, selectors.EVENT_WRITE


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    not_modified: bool = False
    content_type: str = ""
    charset: str = ""
    body: bytes = b""
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    server_date: Optional[float] = None
    redirects: tuple = ()
    requests: int = 0
    elapsed: float = 0.0


class RequestBudget:
    """Counts requests per UTC day. The service replaces this with a persistent one."""

    def __init__(self, limit: int = 500, wall_clock: Callable[[], float] = time.time):
        self.limit = limit
        self._clock = wall_clock
        self._day = None
        self._count = 0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            day = int(self._clock() // 86400)
            if day != self._day:
                self._day, self._count = day, 0
            if self._count >= self.limit:
                raise fail("daily_cap", cap=self.limit)
            self._count += 1

    def used(self) -> int:
        with self._lock:
            return self._count if int(self._clock() // 86400) == self._day else 0


class _Clock:
    def __init__(self, clock, seconds):
        self.clock = clock
        self.seconds = seconds
        self.at = clock() + seconds

    def remaining(self) -> float:
        return self.at - self.clock()


class _Wire:
    """One connection, driven non-blocking and polled in short slices (deadline and cancel aware)."""

    SLICE = 0.2

    def __init__(self, sock, clock: _Clock, cancel: threading.Event, host: str):
        sock.setblocking(False)
        self.sock, self.clock, self.cancel, self.host = sock, clock, cancel, host
        self._sel: Optional[selectors.BaseSelector] = None

    def _check(self):
        if self.cancel.is_set():
            raise fail("cancelled", self.host)
        if self.clock.remaining() <= 0:
            raise fail("timeout", self.host, seconds=self.clock.seconds)

    def _wait(self, events):
        self._check()
        try:
            if self._sel is None:
                self._sel = selectors.DefaultSelector()
                self._sel.register(self.sock, events)
            else:
                self._sel.modify(self.sock, events)
            self._sel.select(max(0.0, min(self.SLICE, self.clock.remaining())))
        except (OSError, ValueError, KeyError):
            raise fail("connect", self.host) from None

    def _rewrap(self, sock):
        if self._sel is not None:
            try:
                self._sel.close()
            except OSError:
                pass
            self._sel = None
        self.sock = sock

    def handshake(self, context: ssl.SSLContext, server_name: str):
        try:
            sock = context.wrap_socket(self.sock, server_hostname=server_name, do_handshake_on_connect=False)
        except (ssl.SSLError, OSError, ValueError):
            raise fail("tls", self.host) from None
        self._rewrap(sock)
        while True:
            self._check()
            try:
                self.sock.do_handshake()
                return
            except ssl.SSLWantReadError:
                self._wait(_READ)
            except ssl.SSLWantWriteError:
                self._wait(_WRITE)
            except ssl.SSLCertVerificationError:
                raise fail("certificate", self.host) from None
            except (ssl.SSLError, OSError, ValueError):
                raise fail("tls", self.host) from None

    def send_all(self, data: bytes):
        view = memoryview(data)
        while view:
            self._check()
            try:
                sent = self.sock.send(view)
                view = view[sent:]
            except (ssl.SSLWantWriteError, BlockingIOError, InterruptedError):
                self._wait(_WRITE)
            except ssl.SSLWantReadError:
                self._wait(_READ)
            except (ssl.SSLError, OSError):
                raise fail("bad_response", self.host) from None

    def recv(self, size: int = 65536) -> bytes:
        while True:
            self._check()
            try:
                return self.sock.recv(size)
            except (ssl.SSLWantReadError, BlockingIOError, InterruptedError):
                self._wait(_READ)
            except ssl.SSLWantWriteError:
                self._wait(_WRITE)
            except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                return b""
            except (ssl.SSLError, OSError):
                raise fail("bad_response", self.host) from None

    def close(self):
        for closer in (getattr(self._sel, "close", None), self.sock.close):
            try:
                if closer:
                    closer()
            except (OSError, ValueError):
                pass


class _Reader:
    """Buffered reads on top of a wire, with limits on every kind of read."""

    def __init__(self, wire: _Wire, initial: bytes = b""):
        self.wire, self.buffer, self.eof = wire, bytearray(initial), False

    def _fill(self) -> bool:
        if self.eof:
            return False
        chunk = self.wire.recv()
        if not chunk:
            self.eof = True
            return False
        self.buffer += chunk
        return True

    def read_line(self, limit: int) -> bytes:
        while True:
            index = self.buffer.find(b"\n")
            if index >= 0:
                line = bytes(self.buffer[:index + 1])
                del self.buffer[:index + 1]
                return line.rstrip(b"\r\n")
            if len(self.buffer) > limit:
                raise fail("bad_response", self.wire.host)
            if not self._fill():
                raise fail("bad_response", self.wire.host)

    def read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            if not self._fill():
                raise fail("bad_response", self.wire.host)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def read_to_end(self, cap: int) -> bytes:
        while True:
            if len(self.buffer) > cap:
                raise fail("too_large", self.wire.host)
            if not self._fill():
                break
        data = bytes(self.buffer)
        self.buffer.clear()
        if len(data) > cap:
            raise fail("too_large", self.wire.host)
        return data


def default_connect(address: str, port: int, clock: _Clock, cancel: threading.Event, host: str, attempt_seconds: float = 6.0):
    """Non-blocking connect to an already checked address, polled so that deadlines and shutdown are honoured."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    started = clock.clock()
    try:
        sock.setblocking(False)
        code = sock.connect_ex((address, port))
        if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            raise OSError(code, os.strerror(code))
        if code != 0:
            selector = selectors.DefaultSelector()
            try:
                selector.register(sock, _WRITE)
                while True:
                    if cancel.is_set():
                        raise fail("cancelled", host)
                    if clock.remaining() <= 0:
                        raise fail("timeout", host, seconds=clock.seconds)
                    if clock.clock() - started > attempt_seconds:
                        raise OSError(errno.ETIMEDOUT, "connect attempt timed out")
                    if selector.select(max(0.0, min(0.2, clock.remaining()))):
                        break
            finally:
                selector.close()
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code:
                raise OSError(code, os.strerror(code))
        return sock
    except BaseException:
        sock.close()
        raise


def http_date(value) -> Optional[float]:
    """Seconds since the epoch for an HTTP date, or ``None``. A date without a zone is taken as UTC, never as local time."""
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def sanitize_validators(etag, last_modified) -> tuple:
    """Keep only validators that are safe to send back to a server (no header injection, bounded)."""
    tag = etag.strip() if isinstance(etag, str) and _ETAG.fullmatch(etag.strip()) else None
    stamp = http_date(last_modified)
    modified = email.utils.formatdate(stamp, usegmt=True) if stamp is not None else None
    return tag, modified


class Fetcher:
    """Fetches one address at a time per call. Safe to share between threads."""

    def __init__(self, *, resolver: Optional[Callable] = None, connector: Optional[Callable] = None,
                 ssl_context: Optional[ssl.SSLContext] = None, contact: str = "", deadline_seconds: float = DEFAULT_DEADLINE,
                 host_min_interval: float = 3.0, budget: Optional[RequestBudget] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Optional[Callable[[float], None]] = None,
                 cancel: Optional[threading.Event] = None, loopback_fixture: bool = False):
        # ``loopback_fixture`` and ``connector`` are for tests only. Nothing reads them from configuration.
        self.fixture = bool(loopback_fixture)
        self.deadline_seconds = float(deadline_seconds)
        self.host_min_interval = float(host_min_interval)
        self.budget = budget or RequestBudget()
        self.cancel = cancel or threading.Event()
        self.clock = clock
        self._sleep = sleep
        self._connector = connector
        self._ssl = ssl_context
        self._resolver = Resolver(resolver)
        suffix = f"; contact {contact}" if isinstance(contact, str) and _CONTACT.fullmatch(contact) else ""
        self.user_agent = f"myradio-research/{__version__} (personal radio station feed reader{suffix})"
        self._lock = threading.Lock()
        self._ready_at: "collections.OrderedDict[str, float]" = collections.OrderedDict()
        self.requests_made = 0

    # ---- public -------------------------------------------------------------------------------------

    def check_url(self, url) -> str:
        """Validate an address without touching the network. Returns the normalised address.

        An address written as a number (``https://10.0.0.5/``) is checked here too, so it is refused when it is saved
        rather than failing on every run. Names are checked against what they resolve to when they are fetched."""
        target = parse_target(url, fixture=self.fixture)
        if target.is_ip:
            check_addresses(target.host, [target.host], allow_loopback=self.fixture)
        return target.url

    def fetch(self, url, *, validators: Optional[dict] = None, deadline_seconds: Optional[float] = None, on_behalf_of: str = "") -> FetchResult:
        """Fetch ``url``. ``validators`` is ``{"url", "etag", "last_modified"}`` from an earlier fetch; the
        conditional headers are sent only to the exact address they were recorded for.

        ``on_behalf_of`` names the operator's own source when ``url`` is an address a website chose (a discovered feed).
        Any failure after the first address, or at such an address, is reported in terms of the operator's source and
        never names the address a website picked. Raises ``FetchError``."""
        started = self.clock()
        target = parse_target(url, fixture=self.fixture)
        clock = _Clock(self.clock, float(deadline_seconds or self.deadline_seconds))
        seen = [target.url]
        count = 0
        origin = on_behalf_of or target.host
        for hop in range(MAX_REDIRECTS + 1):
            elsewhere = bool(hop or on_behalf_of)          # the address in hand was picked by a website, not by the operator
            try:
                response = self._exchange(target, validators, clock)
            except FetchError as error:
                raise (self._reworded(error, origin) if elsewhere else error) from None
            count += 1
            if response["status"] in _REDIRECTS and response["headers"].get("location"):
                if hop >= MAX_REDIRECTS:
                    raise fail("too_many_redirects", origin)
                try:
                    target = self._next_target(target, response["headers"]["location"])
                except FetchError as error:
                    raise self._reworded(error, origin) from None
                if target.url in seen:
                    raise fail("redirect_loop", origin)
                seen.append(target.url)
                continue
            try:
                return self._finish(url, target, response, seen[1:], count, started, validators)
            except FetchError as error:
                raise (self._reworded(error, origin) if elsewhere else error) from None
        raise fail("too_many_redirects", origin)

    @staticmethod
    def _reworded(error: FetchError, origin: str) -> FetchError:
        if error.kind in ("daily_cap", "cancelled", "too_many_redirects", "redirect_loop"):
            return error                                  # these messages never name an address
        return FetchError(error.kind, describe_elsewhere(error.kind, origin, status=error.status), status=error.status,
                          retry_after=error.retry_after, host=origin)

    def cancel_all(self) -> None:
        self.cancel.set()

    # ---- one request --------------------------------------------------------------------------------

    def _next_target(self, current: Target, location: str) -> Target:
        from urllib.parse import urljoin
        if len(location) > 2048:
            raise fail("bad_url")
        try:
            joined = urljoin(current.url, location.strip().replace(" ", "%20"))
        except ValueError:
            raise fail("bad_url") from None            # a Location such as "//[" or "http://[bad" is not an address
        return parse_target(joined, fixture=self.fixture)

    def _exchange(self, target: Target, validators, clock: _Clock) -> dict:
        self._wait_turn(target.host, clock)
        if self.cancel.is_set():
            raise fail("cancelled", target.host)
        addresses = [target.host] if target.is_ip else self._resolver.lookup(target.host, target.port, clock.at, self.cancel, self.clock)
        usable = check_addresses(target.host, addresses, allow_loopback=self.fixture)
        self.budget.take()
        self.requests_made += 1
        wire = self._connect(target, usable, clock)
        try:
            if target.scheme == "https":
                wire.handshake(self._context(), target.host)
            wire.send_all(self._request_bytes(target, validators))
            response = self._read_response(wire, target, clock)
        finally:
            wire.close()
        self._note_response(target.host, response)
        return response

    def _context(self) -> ssl.SSLContext:
        if self._ssl is None:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self._ssl = context
        return self._ssl

    def _connect(self, target: Target, addresses: list, clock: _Clock) -> _Wire:
        last: Optional[FetchError] = None
        for address in addresses[:4]:
            if self.cancel.is_set():
                raise fail("cancelled", target.host)
            try:
                if self._connector is not None:
                    sock = self._connector(address, target.port)
                else:
                    sock = default_connect(address, target.port, clock, self.cancel, target.host)
                return _Wire(sock, clock, self.cancel, target.host)
            except FetchError as error:
                if error.kind in ("cancelled", "timeout"):
                    raise
                last = error
            except OSError:
                last = fail("connect", target.host)
        raise last or fail("connect", target.host)

    def _request_bytes(self, target: Target, validators) -> bytes:
        lines = [f"GET {target.request_target} HTTP/1.1", f"Host: {target.host_header}", f"User-Agent: {self.user_agent}",
                 f"Accept: {ACCEPT}", "Accept-Encoding: gzip, deflate", "Connection: close"]
        if isinstance(validators, dict) and validators.get("url") == target.url:
            etag, modified = sanitize_validators(validators.get("etag"), validators.get("last_modified"))
            if etag:
                lines.append(f"If-None-Match: {etag}")
            if modified:
                lines.append(f"If-Modified-Since: {modified}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    # ---- reading the reply --------------------------------------------------------------------------

    def _read_response(self, wire: _Wire, target: Target, clock: _Clock) -> dict:
        reader = _Reader(wire)
        for _ in range(4):                       # skip any "100 Continue" style interim replies
            head = self._read_head(reader, target.host)
            if not 100 <= head["status"] < 200:
                break
        status, headers = head["status"], head["headers"]
        if status in _REDIRECTS or status == 304 or status in (204, 205) or status >= 400 or status < 200:
            return {"status": status, "headers": headers, "body": b""}
        mime = headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in ALLOWED_TYPES:
            raise fail("bad_type", target.host)
        raw = self._read_body(reader, headers, target.host)
        return {"status": status, "headers": headers, "body": self._decode(raw, headers.get("content-encoding", ""), target.host)}

    def _read_head(self, reader: _Reader, host: str) -> dict:
        lines = []
        total = 0
        first = reader.read_line(MAX_HEAD)
        while True:
            line = reader.read_line(MAX_HEAD)
            total += len(line) + 2
            if not line:
                break
            if total > MAX_HEAD or len(lines) >= MAX_HEADER_LINES:
                raise fail("bad_response", host)
            lines.append(line)
        match = _STATUS.fullmatch(first)
        if not match:
            raise fail("bad_response", host)
        headers: dict = {}
        for line in lines:
            name, sep, value = line.partition(b":")
            if not sep or not name or name != name.strip():
                if line[:1] in (b" ", b"\t"):
                    continue                     # an obsolete folded continuation line: ignored
                raise fail("bad_response", host)
            key = name.decode("latin-1").strip().lower()
            text = value.decode("latin-1").strip()
            headers[key] = f"{headers[key]}, {text}" if key in headers and key not in ("set-cookie",) else text
        return {"status": int(match.group(1)), "headers": headers}

    def _read_body(self, reader: _Reader, headers: dict, host: str) -> bytes:
        encoding = headers.get("transfer-encoding", "").strip().lower()
        if encoding:
            if encoding != "chunked":
                raise fail("unsupported_encoding", host)
            return self._read_chunked(reader, host)
        length = headers.get("content-length")
        if length is not None:
            values = {part.strip() for part in length.split(",")}
            if len(values) != 1 or not next(iter(values)).isdigit() or len(next(iter(values))) > 12:
                raise fail("bad_response", host)
            size = int(next(iter(values)))
            if size > MAX_BYTES:
                raise fail("too_large", host)
            return reader.read_exact(size)
        return reader.read_to_end(MAX_BYTES)

    def _read_chunked(self, reader: _Reader, host: str) -> bytes:
        out = bytearray()
        for _ in range(MAX_CHUNKS):
            line = reader.read_line(1024).split(b";", 1)[0].strip()
            if not line or len(line) > 8 or not re.fullmatch(rb"[0-9a-fA-F]+", line):
                raise fail("bad_response", host)
            size = int(line, 16)
            if size == 0:
                trailer = 0
                while reader.read_line(MAX_HEAD):
                    trailer += 1
                    if trailer > 50:
                        raise fail("bad_response", host)
                return bytes(out)
            if len(out) + size > MAX_BYTES:
                raise fail("too_large", host)
            out += reader.read_exact(size)
            if reader.read_exact(2) != b"\r\n":
                raise fail("bad_response", host)
        raise fail("bad_response", host)                    # an endless stream of tiny chunks is not a feed

    @staticmethod
    def _decode(raw: bytes, encoding: str, host: str) -> bytes:
        name = encoding.strip().lower()
        if name in ("", "identity"):
            return raw
        if name in ("gzip", "x-gzip"):
            wbits = 31
        elif name == "deflate":
            wbits = 15 if len(raw) > 1 and (raw[0] & 0x0F) == 8 and ((raw[0] << 8) | raw[1]) % 31 == 0 else -15
        else:
            raise fail("unsupported_encoding", host)
        decoder = zlib.decompressobj(wbits)
        try:
            out = decoder.decompress(raw, MAX_BYTES + 1)
        except zlib.error:
            raise fail("bad_response", host) from None
        if len(out) > MAX_BYTES:
            raise fail("too_large", host)
        if not decoder.eof:
            raise fail("bad_response", host)
        return out

    # ---- politeness -----------------------------------------------------------------------------------

    def _wait_turn(self, host: str, clock: _Clock) -> None:
        """Wait until the polite gap to ``host`` has passed. The wait is not part of the request deadline: the deadline is
        moved on by the time spent waiting, so a gap (or a ``Retry-After``) of up to 30 seconds cannot use up the time the
        request itself needs, and a request that waited is never reported as "took too long to answer". A wait longer than
        30 seconds is refused instead of slept."""
        began = self.clock()
        while True:
            if self.cancel.is_set():
                raise fail("cancelled", host)
            with self._lock:
                wait = self._ready_at.get(host, 0.0) - self.clock()
            if wait <= 0:
                clock.at += max(0.0, self.clock() - began)
                return
            if wait > MAX_BACKOFF_WAIT:
                raise fail("backoff", host)
            slice_ = min(wait, 0.25)
            if self._sleep is not None:
                self._sleep(slice_)
            else:
                self.cancel.wait(slice_)

    def _note_response(self, host: str, response: dict) -> None:
        gap = self.host_min_interval
        if response["status"] in (429, 503):
            retry = self._retry_after(response["headers"].get("retry-after", ""))
            if retry is not None:
                gap = max(gap, min(retry, MAX_RETRY_AFTER))
            elif response["status"] == 429:
                gap = max(gap, 60.0)
        with self._lock:
            self._ready_at[host] = self.clock() + gap
            self._ready_at.move_to_end(host)
            while len(self._ready_at) > 256:
                self._ready_at.popitem(last=False)

    @staticmethod
    def _retry_after(value: str) -> Optional[float]:
        value = value.strip()
        if value.isdigit() and len(value) < 8:
            return float(value)
        stamp = http_date(value)
        return max(0.0, stamp - time.time()) if stamp is not None else None

    def _finish(self, url, target: Target, response: dict, redirects: list, count: int, started: float, validators) -> FetchResult:
        status, headers = response["status"], response["headers"]
        host = target.host
        elapsed = self.clock() - started
        server_date = http_date(headers.get("date"))
        if status == 304:
            if not (isinstance(validators, dict) and validators.get("url") == target.url):
                raise fail("bad_response", host)
            return FetchResult(url=url, final_url=target.url, status=304, not_modified=True, redirects=tuple(redirects),
                               requests=count, elapsed=elapsed, server_date=server_date,
                               etag=validators.get("etag"), last_modified=validators.get("last_modified"))
        if status in (429, 503):
            raise fail("rate_limited", host, status=status, retry_after=self._retry_after(headers.get("retry-after", "")))
        if status != 200 and status != 203:
            if status in _REDIRECTS:
                raise fail("bad_response", host)
            raise fail("http_error", host, status=status)
        ctype = headers.get("content-type", "")
        charset = _CHARSET.search(ctype)
        etag, modified = sanitize_validators(headers.get("etag"), headers.get("last-modified"))
        return FetchResult(url=url, final_url=target.url, status=status, content_type=ctype.split(";")[0].strip().lower(),
                           charset=charset.group(1) if charset else "", body=response["body"], etag=etag, last_modified=modified,
                           server_date=server_date, redirects=tuple(redirects), requests=count, elapsed=elapsed)
