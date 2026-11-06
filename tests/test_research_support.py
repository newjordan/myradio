"""Shared fixtures for the research tests. This module contains no tests.

Everything here is a local fixture: servers bind to 127.0.0.1 port 0 and nothing leaves the machine.
"""
import contextlib
import datetime as dt
import email.utils
import gzip
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import zlib
from xml.sax.saxutils import escape

from myradio.research.fetcher import Fetcher, RequestBudget

# A throwaway self-signed certificate and key made only for these tests (valid 2020 to 2125 for the fixture names
# below). It protects nothing and is trusted by nothing outside the test client contexts that load it.
FIXTURE_CERT = """-----BEGIN CERTIFICATE-----
MIICDTCCAbKgAwIBAgIBATAKBggqhkjOPQQDAjAoMSYwJAYDVQQDDB1teXJhZGlv
IHJlc2VhcmNoIHRlc3QgZml4dHVyZTAgFw0yMDAxMDEwMDAwMDBaGA8yMTI1MDEw
MTAwMDAwMFowKDEmMCQGA1UEAwwdbXlyYWRpbyByZXNlYXJjaCB0ZXN0IGZpeHR1
cmUwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATf6i7245/MQbhsZ4SshsZMtWRr
o5KEXw+JxmtNUCoAD/RFsGcP+hO1tYsw9mwcWHeXnzCinxWdzWma33S0ptL/o4HK
MIHHMGQGA1UdEQRdMFuCEmZlZWRzLmV4YW1wbGUudGVzdIIOYS5leGFtcGxlLnRl
c3SCDmIuZXhhbXBsZS50ZXN0gg5jLmV4YW1wbGUudGVzdIIJbG9jYWxob3N0hwR/
AAABhwRduNgiMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgKEMB0GA1Ud
DgQWBBSImC3kKUnyaQxCzmac7IR1u8JMZjAfBgNVHSMEGDAWgBSImC3kKUnyaQxC
zmac7IR1u8JMZjAKBggqhkjOPQQDAgNJADBGAiEAiN0HZq1aYDG6wnZPIMs+6LTt
h4de5UtFi5ynQLpUufECIQCqeWzWcB7QMhd7u5IAM6PI6BObW0kVt5iqRyM8fyyk
/A==
-----END CERTIFICATE-----
"""
FIXTURE_KEY = """-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIAUGsOYB7uP1jDxUQHNdUVf93nnwTfm7BMqb0D+l6teLoAoGCCqGSM49
AwEHoUQDQgAE3+ou9uOfzEG4bGeErIbGTLVka6OShF8PicZrTVAqAA/0RbBnD/oT
tbWLMPZsHFh3l58wop8Vnc1pmt90tKbS/w==
-----END EC PRIVATE KEY-----
"""
PUBLIC_A, PUBLIC_B, PUBLIC_C = "93.184.216.34", "93.184.216.35", "93.184.216.36"   # documentation-style stand-ins that classify as public
NOW = 1_762_300_800.0          # 2025-11-05 00:00:00 UTC, the fixed "now" used by pipeline tests


def client_context():
    """A TLS client context that trusts only the fixture certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=FIXTURE_CERT)
    return context


class Request:
    def __init__(self, method, target, headers, raw):
        self.method, self.target, self.headers, self.raw = method, target, headers, raw

    @property
    def path(self):
        return self.target.split("?", 1)[0]


class Conn:
    """The server side of one connection, handed to route handlers."""

    def __init__(self, sock, server):
        self.sock, self.server = sock, server

    def send(self, data: bytes):
        self.sock.sendall(data)

    def sleep(self, seconds: float) -> bool:
        """Sleep, returning early (False) when the server is shutting down."""
        return not self.server.stopping.wait(seconds)

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()


def response(status=200, body=b"", *, content_type="application/rss+xml; charset=utf-8", headers=None, compress=None,
             chunked=False, length=True, reason="OK"):
    """Bytes of a complete HTTP/1.1 reply."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    extra = dict(headers or {})
    if compress == "gzip":
        body = gzip.compress(body)
        extra["Content-Encoding"] = "gzip"
    elif compress == "deflate":
        body = zlib.compress(body)
        extra["Content-Encoding"] = "deflate"
    elif compress == "raw-deflate":
        packer = zlib.compressobj(wbits=-15)
        body = packer.compress(body) + packer.flush()
        extra["Content-Encoding"] = "deflate"
    lines = [f"HTTP/1.1 {status} {reason}"]
    if content_type:
        lines.append(f"Content-Type: {content_type}")
    lines.append("Connection: close")
    for key, value in extra.items():
        lines.append(f"{key}: {value}")
    if chunked:
        lines.append("Transfer-Encoding: chunked")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        parts = [body[i:i + 700] for i in range(0, len(body), 700)] or [b""]
        framed = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in parts if p) + b"0\r\n\r\n"
        return head + framed
    if length:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def redirect(location, status=302):
    return response(status, b"", content_type="", headers={"Location": location}, reason="Found")


class FixtureServer:
    """A tiny scriptable server. Routes map a path to ``handler(request, conn)``; unknown paths get 404."""

    def __init__(self, tls=False):
        self.tls = tls
        self.routes = {}
        self.requests = []
        self.sni = []
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._clients = []
        self._threads = []
        self._dir = None
        self._context = None
        if tls:
            self._dir = tempfile.TemporaryDirectory()
            cert, key = os.path.join(self._dir.name, "c.pem"), os.path.join(self._dir.name, "k.pem")
            with open(cert, "w") as f:
                f.write(FIXTURE_CERT)
            with open(key, "w") as f:
                f.write(FIXTURE_KEY)
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(cert, key)
            self._context.sni_callback = self._on_sni
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self.port = self._listener.getsockname()[1]
        self._accept = threading.Thread(target=self._accept_loop, name="fixture-accept", daemon=True)
        self._accept.start()

    # -- scripting --
    def route(self, path, handler):
        self.routes[path] = handler

    def serve(self, path, body=b"", **kwargs):
        data = response(body=body, **kwargs)
        self.routes[path] = lambda req, conn: conn.send(data)

    def hits(self, path=None):
        with self._lock:
            return [r for r in self.requests if path is None or r.path == path]

    @property
    def base(self):
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    # -- internals --
    def _on_sni(self, sock, name, context):
        with self._lock:
            self.sni.append(name)

    def _accept_loop(self):
        self._listener.settimeout(0.02)         # closing a listener does not wake a blocked accept on every platform
        while not self.stopping.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._serve_one, args=(client,), name="fixture-conn", daemon=True)
            with self._lock:
                self._clients.append(client)
                self._threads.append(thread)
            thread.start()

    def _serve_one(self, client):
        conn = Conn(client, self)
        try:
            client.settimeout(5)
            if self._context is not None:
                client = self._context.wrap_socket(client, server_side=True)
                conn.sock = client
                with self._lock:
                    self._clients.append(client)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            first, *lines = head.split("\r\n")
            method, target, _ = first.split(" ", 2)
            headers = {}
            for line in lines:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            request = Request(method, target, headers, head)
            with self._lock:
                self.requests.append(request)
            handler = self.routes.get(request.path)
            if handler is None:
                conn.send(response(404, b"not found", content_type="text/plain", reason="Not Found"))
            else:
                handler(request, conn)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            conn.close()

    def close(self):
        self.stopping.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        with self._lock:
            clients, threads = list(self._clients), list(self._threads)
        for client in clients:
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                client.close()
        self._accept.join(2)
        for thread in threads:
            thread.join(2)
        if self._dir is not None:
            self._dir.cleanup()


class StaticResolver:
    """Stand-in for DNS. ``mapping`` values may be a list or a callable ``(call_number) -> list``."""

    def __init__(self, mapping):
        self.mapping, self.calls = dict(mapping), []

    def __call__(self, host, port):
        self.calls.append(host)
        value = self.mapping.get(host)
        if value is None:
            raise socket.gaierror(socket.EAI_NONAME, "no such host (test fixture)")
        return value(len([h for h in self.calls if h == host])) if callable(value) else list(value)


class RedirectingConnector:
    """Pretends to connect to a 'public' address but really dials the local fixture server."""

    def __init__(self, server):
        self.server, self.dialed = server, []

    def __call__(self, address, port):
        self.dialed.append((address, port))
        return socket.create_connection(("127.0.0.1", self.server.port), timeout=5)


DEFAULT_HOSTS = {"feeds.example.test": [PUBLIC_A], "a.example.test": [PUBLIC_B], "b.example.test": [PUBLIC_C],
                 "c.example.test": [PUBLIC_A]}


def tls_fetcher(server, hosts=None, **kwargs):
    """A production-mode fetcher (https only, address checks on) whose connections end up at ``server``.
    Returns ``(fetcher, resolver, connector)``."""
    resolver = StaticResolver(hosts or DEFAULT_HOSTS)
    connector = RedirectingConnector(server)
    options = dict(resolver=resolver, connector=connector, ssl_context=client_context(), host_min_interval=0.0,
                   budget=RequestBudget(10_000), deadline_seconds=8.0)
    options.update(kwargs)
    return Fetcher(**options), resolver, connector


def loopback_fetcher(**kwargs):
    """A fixture-mode fetcher for plain http://127.0.0.1:PORT servers."""
    options = dict(loopback_fixture=True, host_min_interval=0.0, budget=RequestBudget(10_000), deadline_seconds=8.0)
    options.update(kwargs)
    return Fetcher(**options)


class FakeClock:
    def __init__(self, start=NOW):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


# ---- feed builders ------------------------------------------------------------------------------------

def rfc822(ts):
    return email.utils.formatdate(ts, usegmt=True)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rss(items, title="Example Sky Notes", link="https://sky.example/"):
    """``items``: dicts with title, link, desc (raw XML text, already escaped or CDATA), ts, guid, cats."""
    body = []
    for item in items:
        parts = [f"<title>{escape(item.get('title', ''))}</title>"]
        if item.get("link"):
            parts.append(f"<link>{escape(item['link'])}</link>")
        if item.get("guid"):
            parts.append(f'<guid isPermaLink="false">{escape(item["guid"])}</guid>')
        if item.get("ts") is not None:
            parts.append(f"<pubDate>{rfc822(item['ts'])}</pubDate>")
        if item.get("desc") is not None:
            parts.append(f"<description>{item['desc']}</description>")
        for cat in item.get("cats", ()):
            parts.append(f"<category>{escape(cat)}</category>")
        body.append("<item>" + "".join(parts) + "</item>")
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>{escape(title)}</title><link>{escape(link)}</link><description>feed</description>" + "".join(body)
            + "</channel></rss>")


def atom(entries, title="Example Maker Weekly", link="https://maker.example/"):
    body = []
    for entry in entries:
        parts = [f"<title>{escape(entry.get('title', ''))}</title>"]
        if entry.get("link"):
            parts.append(f'<link rel="alternate" type="text/html" href="{escape(entry["link"])}"/>')
        parts.append(f"<id>{escape(entry.get('id', entry.get('link', 'urn:x')))}</id>")
        if entry.get("ts") is not None:
            parts.append(f"<published>{iso(entry['ts'])}</published><updated>{iso(entry['ts'])}</updated>")
        if entry.get("summary") is not None:
            parts.append(f"<summary>{escape(entry['summary'])}</summary>")
        body.append("<entry>" + "".join(parts) + "</entry>")
    return ('<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            f"<title>{escape(title)}</title><link href=\"{escape(link)}\"/>" + "".join(body) + "</feed>")


def jsonfeed(items, title="Example JSON Feed"):
    out = []
    for item in items:
        entry = {"id": item.get("id", item.get("link", "1")), "title": item.get("title", "")}
        if item.get("link"):
            entry["url"] = item["link"]
        if item.get("text") is not None:
            entry["content_text"] = item["text"]
        if item.get("ts") is not None:
            entry["date_published"] = iso(item["ts"])
        out.append(entry)
    return json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": title, "items": out})


def wait_until(predicate, timeout=5.0, interval=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def research_threads():
    return [t for t in threading.enumerate() if t.name.startswith("myradio-research")]


# ---- a whole extension inside a real ExtensionHost -------------------------------------------------

class FakeStation:
    def status(self):
        return {}

    def import_audio(self, data, title, artist='', kind='music'):
        return {'id': 'x', 'title': title}

    def action(self, operation, arguments=None):
        return {'ok': True, 'operation': operation}


class Harness:
    """The research extension running in a real ExtensionHost, with a fixture fetcher and a fake wall clock.

    ``autostart=False`` (the default) leaves the scheduler thread off so tests drive ``service.tick()`` themselves."""

    def __init__(self, directory, *, fetcher=None, clock=None, autostart=False, **options):
        from myradio.extensions import ExtensionHost, StationFacade
        from myradio.research.extension import ResearchExtension
        import random
        self.clock = clock or FakeClock()
        self.fetcher = fetcher or loopback_fetcher()
        options.setdefault('rng', random.Random(1))
        options.setdefault('run_now_wait', 0.0)
        self.ext = ResearchExtension(fetcher=self.fetcher, autostart=autostart, **options)
        self.host = ExtensionHost(directory, StationFacade(FakeStation()), clock=self.clock)
        self.host.register(self.ext)
        self.host.start()
        self.service = self.ext.service

    def close(self):
        self.host.stop()

    def call(self, name, arguments=None):
        return self.host.call(f'research.{name}', arguments)

    def save(self, **spec):
        return self.service.save(spec)['researcher']

    def run(self, rid):
        """A manual run, carried out by this thread, and the brief it made."""
        self.service.run_now(rid)
        self.service.tick()
        return self.service.briefs(researcher=rid, limit=1)[0]

    def due_run(self, rid):
        """Let the researcher's schedule come due, run it, and return the newest brief."""
        self.clock.advance(self.service.store.get(rid)['next_run_ts'] - self.clock() + 1)
        self.service.tick()
        return self.service.briefs(researcher=rid, limit=1)[0]


def researcher(url, **kw):
    """A valid researcher specification reading ``url``."""
    spec = {'name': 'Backyard astronomy', 'interests': ['meteor showers', 'planets'], 'sources': [url], 'freshness_hours': 24,
            'cadence': {'type': 'every_hours', 'hours': 6}}
    spec.update(kw)
    return spec


def item(title, desc, hours_ago=1.0, link=None, **kw):
    """An RSS item with a plain-text description, dated ``hours_ago`` before NOW."""
    from xml.sax.saxutils import escape
    return {'title': title, 'desc': escape(desc), 'ts': NOW - hours_ago * 3600, 'link': link or f'https://sky.example/{abs(hash(title)) % 10**8}', **kw}
