"""Transport-level tests for myradio.model_transport on real loopback sockets.

Nothing here touches DNS or any external host. Names resolve through an injected
resolver; everything else is an IP literal on an ephemeral loopback port that the test
itself opened. Deadline assertions use generous upper bounds so a loaded machine does
not make them flaky, and a strict lower bound so an early failure cannot pass as a
deadline.

The certificate tests need a certificate authority and a server certificate. They are made
when the tests start, in a temporary directory that is removed afterwards, with the
``openssl`` command-line tool (called with an argument list, never through a shell). Those
tests are skipped where the tool is missing. No key or certificate is kept or committed.
"""
import http.client
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import unittest
import urllib.error
from unittest.mock import patch

from myradio import model_transport as mt
from myradio.chat import Chat
from myradio.model_transport import (MAX_RESPONSE, ModelDeadlineError, ModelTransport,
                                     ModelTransportError, _endpoint)
from myradio.station import Station, StationError

KEY = 'transport-secret-key-1234567890'
SAFE = ('The model endpoint did not return a valid answer. No station changes were made. '
        'Check its URL, model, credentials and tool support.')
ANSWER = json.dumps({'choices': [{'message': {
    'content': 'Propose a rename.',
    'tool_calls': [{'function': {'name': 'station_update', 'arguments': '{"name":"Wire FM"}'}}],
}}]}).encode()


_system_lookups = []
_guard = None


def setUpModule():
    """Names resolve only through injected resolvers: fail loudly if anything reaches real DNS."""
    global _guard

    def forbidden(host, port):
        _system_lookups.append((host, port))
        raise AssertionError('these tests must never use the system resolver')
    _guard = patch('myradio.model_transport._system_lookup', forbidden)
    _guard.start()


def tearDownModule():
    _guard.stop()
    if _system_lookups:
        raise AssertionError(f'the system resolver was used for {sorted(set(_system_lookups))}')


def ipv6_loopback():
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(('::1', 0))
        return True
    except OSError:
        return False


HAVE_IPV6 = ipv6_loopback()


def http_ok(body=b'{"ok": true}', extra=b''):
    return (b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%sConnection: close\r\n\r\n%s'
            % (len(body), extra, body))


def until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def dns_threads():
    return [t for t in threading.enumerate() if t.name == 'myradio-model-dns']


class Peer:
    """A loopback TCP server whose behaviour is a script: script(peer, conn, head, body).

    ``tls`` is a server-side SSLContext: each connection is wrapped in it before the request is
    read. ``alpn`` then lists what each TLS client negotiated (None when it offered nothing) and
    ``handshake_errors`` the handshakes that failed, for example a client that refused the certificate."""

    def __init__(self, script, *, read=True, family=socket.AF_INET, host='127.0.0.1',
                 backlog=16, rcvbuf=None, tls=None):
        self.script, self.read, self.host, self.family, self.tls = script, read, host, family, tls
        self.stop = threading.Event()
        self.requests = []
        self.alpn = []
        self.handshake_errors = []
        self._conns = []
        self._threads = []
        self.listener = socket.socket(family, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if rcvbuf:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.listener.bind((host, 0))
        self.listener.listen(backlog)
        self.port = self.listener.getsockname()[1]
        self._accepter = threading.Thread(target=self._accept, name='test-peer-accept', daemon=True)
        self._accepter.start()

    @property
    def base(self):
        host = f'[{self.host}]' if self.family == socket.AF_INET6 else self.host
        return f"{'https' if self.tls else 'http'}://{host}:{self.port}/v1"

    def url(self, path='/chat/completions'):
        return self.base + path

    def _accept(self):
        self.listener.settimeout(.05)
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conns.append(conn)
            worker = threading.Thread(target=self._serve, args=(conn,), name='test-peer-conn', daemon=True)
            self._threads.append(worker)
            worker.start()

    def _serve(self, conn):
        try:
            if self.tls is not None:
                conn.settimeout(5)
                try:
                    conn = self.tls.wrap_socket(conn, server_side=True)
                except OSError as exc:  # ssl.SSLError is one: the client gave up during the handshake
                    self.handshake_errors.append(type(exc).__name__)
                    return
                self._conns.append(conn)  # so that close() can wake a thread blocked on it
                self.alpn.append(conn.selected_alpn_protocol())
            head = body = b''
            if self.read:
                head, body = self._read_request(conn)
                self.requests.append((head, body))
            self.script(self, conn, head, body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(conn):
        conn.settimeout(5)
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return data, b''
            data += chunk
        head, _, rest = data.partition(b'\r\n\r\n')
        length = 0
        for line in head.split(b'\r\n')[1:]:
            name, _, value = line.partition(b':')
            if name.strip().lower() == b'content-length':
                length = int(value.strip())
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            rest += chunk
        return head, rest

    def close(self):
        self.stop.set()
        for sock in list(self._conns) + [self.listener]:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a thread blocked in accept() or recv()
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        for worker in [self._accepter] + self._threads:
            worker.join(timeout=1)


def reply(raw):
    def script(peer, conn, head, body):
        conn.sendall(raw)
    return script


def hold(raw=b''):
    def script(peer, conn, head, body):
        if raw:
            conn.sendall(raw)
        peer.stop.wait(30)
    return script


def drip(raw, delay=.02, first=b''):
    """Send `first` at once, then `raw` one byte at a time."""
    def script(peer, conn, head, body):
        if first:
            conn.sendall(first)
        for byte in raw:
            conn.sendall(bytes([byte]))
            if peer.stop.wait(delay):
                return
    return script


class Resolver:
    """Injectable resolver: every name maps to loopback; chosen names block on a gate."""

    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.gate = threading.Event()
        self.calls = []
        self.active = self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, host, port):
        with self._lock:
            self.calls.append(host)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if host in self.blocked and not self.gate.wait(30):
                raise OSError('gate never opened')
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        finally:
            with self._lock:
                self.active -= 1


def error_texts(error):
    """Every string reachable from an exception, including chained ones and the traceback."""
    seen, found, stack = set(), [], [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        found += [str(current), repr(current), repr(current.args)]
        stack += [current.__cause__, current.__context__]
    found.append(''.join(traceback.format_exception(error)))
    return found


def transport_sockets(factory):
    """Patch the socket class the transport opens its connections with; the rest of `socket` stays real."""
    names = {name: getattr(socket, name) for name in dir(socket) if not name.startswith('__')}
    names['socket'] = factory
    return patch('myradio.model_transport.socket', types.SimpleNamespace(**names))


def slow_connect(delay):
    """Every connection the transport makes takes `delay` seconds longer to complete."""
    class SlowSocket(socket.socket):
        def connect(self, address):
            time.sleep(delay)
            return super().connect(address)
    return transport_sockets(SlowSocket)


def big_request(url, size=48 * 1024 * 1024):
    """Make post() send a request of `size` bytes that was built before the call. Building 48 MiB inside
    post() takes ~50 ms on an idle machine and, on a starved one, can eat a whole short budget before the
    send even starts, which then fails in the wrong phase."""
    wire = mt._payload(_endpoint(url), {'User-Agent': 'test'}, b'x' * size)
    return patch('myradio.model_transport._payload', return_value=wire)


OPENSSL = shutil.which('openssl')
_CERTIFICATE_SECTIONS = """[req]
distinguished_name = name
prompt = no
x509_extensions = authority
[name]
CN = {cn}
[authority]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[leaf]
subjectAltName = {san}
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


class Pki:
    """A throwaway certificate authority and two server certificates, made with the openssl
    command-line tool in a directory the caller owns and removes. Valid for two days, which
    is far more than a test run needs."""

    def __init__(self, directory):
        self.directory = directory
        self.ca = os.path.join(directory, 'ca.pem')
        self._run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.pem',
                  '-days', '2', '-config', self._config('ca', 'myradio test authority', ''))
        # Valid for the DNS name and for both loopback addresses.
        self.server = self._leaf('server', 'myradio test server', 'DNS:model.test,IP:127.0.0.1,IP:::1')
        # Valid for another DNS name only; its CN is the loopback address, which must not count as an IP address.
        self.dns_only = self._leaf('dnsonly', '127.0.0.1', 'DNS:onlydns.test')

    def _config(self, stem, cn, san):
        with open(os.path.join(self.directory, stem + '.cnf'), 'w') as config:
            config.write(_CERTIFICATE_SECTIONS.format(cn=cn, san=san))
        return stem + '.cnf'

    def _leaf(self, stem, cn, san):
        config = self._config(stem, cn, san)
        self._run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', stem + '.key', '-out', stem + '.csr',
                  '-config', config)
        self._run('x509', '-req', '-in', stem + '.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial',
                  '-out', stem + '.pem', '-days', '2', '-extfile', config, '-extensions', 'leaf')
        return os.path.join(self.directory, stem + '.pem'), os.path.join(self.directory, stem + '.key')

    def _run(self, *arguments):
        try:
            subprocess.run([OPENSSL, *arguments], cwd=self.directory, capture_output=True, text=True,
                           timeout=30, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f'openssl {arguments[0]} failed: {exc.stderr[-400:]}') from None


def tls_server_context(certificate, sni=None, alpn=None):
    """A server-side context for `certificate` (a (cert, key) pair). Every server name a client sends is
    appended to `sni` (None when it sent none); `alpn` is what the server is willing to speak."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*certificate)
    if alpn:
        context.set_alpn_protocols(alpn)
    if sni is not None:
        context.sni_callback = lambda sock, name, ctx: sni.append(name)
    return context


class TransportCase(unittest.TestCase):
    def peer(self, script, **kwargs):
        peer = Peer(script, **kwargs)
        self.addCleanup(peer.close)
        return peer

    def resolver(self, blocked=()):
        resolver = Resolver(blocked)
        self.addCleanup(resolver.gate.set)  # never leave the helper thread blocked after a test
        return resolver

    def stalled_listener(self):
        """A loopback address whose connect() stays pending: a listener with a full accept queue."""
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(0)
        address = listener.getsockname()
        held = []
        self.addCleanup(lambda: [s.close() for s in held])
        for _ in range(12):
            probe = socket.socket()
            held.append(probe)
            probe.setblocking(False)
            probe.connect_ex(address)
            if not select.select([], [probe], [], .1)[1]:
                return address  # this attempt is still pending, so the queue is full
        self.skipTest('this OS completes connections beyond the listen backlog')

    def post(self, url, body=b'{}', headers=None, timeout=3, transport=None):
        return (transport or ModelTransport()).post(url, body, headers or {}, timeout=timeout)

    def assertDeadline(self, call, timeout, phase, late=1.5):
        start = time.monotonic()
        with self.assertRaises(ModelDeadlineError) as caught:
            call()
        elapsed = time.monotonic() - start
        self.assertIsInstance(caught.exception, TimeoutError)
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(str(caught.exception), 'model request deadline exceeded')
        self.assertEqual(caught.exception.phase, phase)
        self.assertGreaterEqual(elapsed, timeout - .05, 'failed before the deadline')
        self.assertLess(elapsed, timeout + late, 'overran the deadline')
        return caught.exception

    def assertFails(self, call, phase=None, kind=...):
        with self.assertRaises(ModelTransportError) as caught:
            call()
        self.assertNotIsInstance(caught.exception, ModelDeadlineError)
        if phase:
            self.assertEqual(caught.exception.phase, phase)
        if kind is not ...:
            self.assertEqual(caught.exception.kind, kind)
        return caught.exception


class EndpointTests(TransportCase):
    def test_accepted_urls_are_split_for_the_wire(self):
        cases = {
            'http://127.0.0.1:8080/v1/chat/completions?x=1#frag':
                (False, '127.0.0.1', 8080, '/v1/chat/completions?x=1', '127.0.0.1:8080'),
            'https://example.test/x': (True, 'example.test', 443, '/x', 'example.test'),
            'https://example.test:443/x': (True, 'example.test', 443, '/x', 'example.test'),
            'https://example.test:8443/x': (True, 'example.test', 8443, '/x', 'example.test:8443'),
            'http://example.test': (False, 'example.test', 80, '/', 'example.test'),
            'http://example.test:80/': (False, 'example.test', 80, '/', 'example.test'),
            'http://example.test?only=query': (False, 'example.test', 80, '/?only=query', 'example.test'),
            'http://[::1]:8080/p?q=1': (False, '::1', 8080, '/p?q=1', '[::1]:8080'),
            'https://[2001:db8::1]/p': (True, '2001:db8::1', 443, '/p', '[2001:db8::1]'),
            'http://LOCALHOST:11434/v1': (False, 'localhost', 11434, '/v1', 'localhost:11434'),
            'HTTP://Example.TEST/X': (False, 'example.test', 80, '/X', 'example.test'),
            'http://under_score.test./': (False, 'under_score.test.', 80, '/', 'under_score.test.'),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(tuple(_endpoint(url)), expected)

    def test_unsafe_or_malformed_urls_are_rejected_before_any_network_use(self):
        peer = self.peer(reply(http_ok()))
        bad = ['', 'file:///etc/passwd', 'data:text/plain,x', 'javascript:alert(1)',
               'http://', 'http:///x', '//h.test/x', 'h.test/x', 'http://u:p@h.test/', 'http://u@h.test/',
               'http://@h.test/', 'http://h.test:0/', 'http://h.test:65536/', 'http://h.test:-1/',
               'http://h.test:abc/', 'http://h.test/ x', 'http://h.test/\n', 'http://h.test/\r\nX: y',
               'http://h.test/\x00', 'http://h\u00e9.test/', 'http://h%41.test/', 'http://-x.test/',
               'http://a..b/', 'http://' + 'a' * 300 + '.test/', 'http://[::1/', 'http://[zz]/',
               'http://[127.0.0.1]/', 'http://1:2:3/', 'x' * 9000, None, 5, b'http://h.test/',
               # a scoped (zone) IPv6 literal is valid for ipaddress and must still be refused
               'http://[fe80::1%25eth0]/', 'http://[fe80::1%eth0]:80/', 'http://[::1%25lo]:8080/v1',
               'http://[::1%lo]/', 'https://[fe80::1%25en0]:443/v1',
               f'ftp://127.0.0.1:{peer.port}/v1', f'http://user:pw@127.0.0.1:{peer.port}/v1',
               f'http://127.0.0.1:{peer.port}/v1\r\nX-Evil: 1']
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver)
        for url in bad:
            with self.subTest(url=url if not isinstance(url, str) else url[:40]):
                error = self.assertFails(lambda: transport.post(url, b'{}', {}, timeout=1), phase='request')
                # Our own validation rejected it: not some later failure that happens to look similar.
                self.assertEqual((str(error), error.kind), ('invalid model URL', None))
        self.assertEqual(resolver.calls, [])
        self.assertEqual(peer.requests, [])

    def test_rejections_quote_nothing_and_chain_no_library_exception(self):
        # urlsplit/ipaddress errors quote the offending text; none of it may stay reachable.
        for url in (f'http://host:{KEY}/x', f'http://[{KEY}]/x', f'http://{KEY}:{KEY}@h.test/x',
                    f'http://-{KEY}.test/', f'http://[::1/{KEY}', f'ftp://h.test/{KEY}'):
            with self.subTest(url=url[:30]):
                error = self.assertFails(lambda: ModelTransport().post(url, b'{}', {'Authorization': 'Bearer ' + KEY},
                                                                       timeout=1), phase='request')
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
                self.assertTrue(all(KEY not in text for text in error_texts(error)))

    def test_header_names_and_values_are_validated_and_never_echoed(self):
        peer = self.peer(reply(http_ok()))
        bad = [{'Authorization': 'Bearer ' + KEY + '\r\nX-Evil: 1'}, {'Authorization': 'Bearer ' + KEY + '\n'},
               {'Authorization': 'Bearer ' + KEY + '\x00'}, {'X-A\r\nB': KEY}, {'X A': KEY}, {'': KEY},
               {'X-Key': 'caf\u00e9 ' + KEY}, {'X-Key': 5}, {5: KEY}]
        for headers in bad:
            with self.subTest(headers=sorted(map(repr, headers))):
                error = self.assertFails(lambda: self.post(peer.url(), headers=headers), phase='request')
                self.assertTrue(all(KEY not in text for text in error_texts(error)))
        self.assertEqual(peer.requests, [])

    def test_call_misuse_is_a_programming_error_not_a_network_failure(self):
        transport = ModelTransport()
        for body in ('text', None, 5, [b'x']):
            with self.assertRaises(TypeError):
                transport.post('http://127.0.0.1:9/', body, {}, timeout=1)
        for timeout in (0, -1, float('nan'), float('inf'), True, False, None, '5', 10 ** 9):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                transport.post('http://127.0.0.1:9/', b'{}', {}, timeout=timeout)
        with self.assertRaises(TypeError):
            transport.post('http://127.0.0.1:9/', b'{}', {})  # timeout is required and keyword-only
        with self.assertRaises(TypeError):
            transport.post('http://127.0.0.1:9/', b'{}', {}, 5)


class WireTests(TransportCase):
    def test_request_shape_and_success(self):
        peer = self.peer(reply(http_ok(b'{"hello": 1}')))
        raw = self.post(peer.url('/chat/completions?api-version=7#ignored'), b'{"a": 1}',
                        {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
        self.assertEqual(raw, b'{"hello": 1}')
        head, body = peer.requests[0]
        lines = head.split(b'\r\n')
        self.assertEqual(lines[0], b'POST /v1/chat/completions?api-version=7 HTTP/1.1')
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(b':') for line in lines[1:])}
        self.assertEqual(headers[b'host'], b'127.0.0.1:%d' % peer.port)
        self.assertEqual(headers[b'content-length'], b'8')
        self.assertEqual(headers[b'content-type'], b'application/json')
        self.assertEqual(headers[b'authorization'], b'Bearer ' + KEY.encode())
        self.assertEqual(headers[b'connection'], b'close')
        self.assertEqual(headers[b'accept-encoding'], b'identity')
        self.assertTrue(headers[b'user-agent'].startswith(b'myradio/'))
        self.assertEqual(body, b'{"a": 1}')
        self.assertEqual(len(lines) - 1, len(headers), 'no duplicated header lines')

    def test_callers_cannot_override_request_framing_or_the_user_agent_twice(self):
        peer = self.peer(reply(http_ok()))
        self.post(peer.url(), b'{}', {'host': 'evil.test', 'Content-Length': '999', 'Connection': 'keep-alive',
                                       'Transfer-Encoding': 'chunked', 'Accept-Encoding': 'gzip',
                                       'user-agent': 'custom/1'})
        head = peer.requests[0][0].lower()
        self.assertEqual(head.count(b'host:'), 1)
        self.assertNotIn(b'evil.test', head)
        self.assertIn(b'content-length: 2\r\n', head)
        self.assertIn(b'connection: close', head)
        self.assertNotIn(b'keep-alive', head)
        self.assertNotIn(b'transfer-encoding', head)
        self.assertNotIn(b'gzip', head)
        self.assertEqual(head.count(b'user-agent:'), 1)
        self.assertIn(b'user-agent: custom/1', head)

    @unittest.skipUnless(HAVE_IPV6, 'IPv6 loopback is not available here')
    def test_ipv6_literal_round_trip_and_host_header(self):
        peer = self.peer(reply(http_ok(b'{"v6": true}')), family=socket.AF_INET6, host='::1')
        self.assertEqual(self.post(peer.url()), b'{"v6": true}')
        self.assertIn(b'Host: [::1]:%d\r\n' % peer.port, peer.requests[0][0] + b'\r\n')

    def test_localhost_and_literals_need_no_name_lookup(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver)
        for host in ('localhost', '127.0.0.1'):
            with self.subTest(host=host):
                self.assertEqual(self.post(f'http://{host}:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(resolver.calls, [])

    def test_only_status_200_is_accepted_and_nothing_is_followed(self):
        target = self.peer(reply(http_ok()))
        for status in (301, 302, 303, 307, 308, 400, 401, 404, 429, 500, 503, 201, 202, 204, 206):
            raw = (b'HTTP/1.1 %d Whatever\r\nLocation: %s\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
                   % (status, target.url().encode()))
            with self.subTest(status=status):
                peer = self.peer(reply(raw))
                self.assertFails(lambda: self.post(peer.url(), headers={'Authorization': 'Bearer ' + KEY}),
                                 phase='status')
        self.assertEqual(target.requests, [], 'a credentialed request was sent to a redirect target')

    def test_status_line_and_protocol_errors_are_normalised(self):
        cases = {
            'garbage': (b'this is not http\r\n\r\n', 'BadStatusLine'),
            'empty': (b'', 'RemoteDisconnected'),
            'http2': (b'HTTP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n', 'UnknownProtocol'),
            'long header line': (b'HTTP/1.1 200 OK\r\nX: ' + b'a' * 70000 + b'\r\n\r\n', 'LineTooLong'),
            'too many headers': (b'HTTP/1.1 200 OK\r\n' + b'X: 1\r\n' * 150 + b'\r\n', 'HTTPException'),
            # http.client reads EOF as "end of headers"; the transport insists on the blank line.
            'cut inside the headers': (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n', None),
            'cut inside a header line': (b'HTTP/1.1 200 OK\r\nContent-Le', None),
            'cut without any length': (b'HTTP/1.0 200 OK\r\nX: y\r\n', None),
        }
        for label, (raw, kind) in cases.items():
            with self.subTest(case=label):
                peer = self.peer(reply(raw))
                self.assertFails(lambda: self.post(peer.url()), phase='headers', kind=kind)

    def test_informational_responses_are_skipped(self):
        peer = self.peer(reply(b'HTTP/1.1 100 Continue\r\n\r\n' + http_ok(b'{"after": 100}')))
        self.assertEqual(self.post(peer.url()), b'{"after": 100}')

    def test_content_length_must_be_one_valid_number_matching_the_body(self):
        def serve(headers, body=b'{"a":1}'):
            return self.peer(reply(b'HTTP/1.0 200 OK\r\n' + headers + b'Connection: close\r\n\r\n' + body))
        self.assertEqual(self.post(serve(b'Content-Length: 7\r\n').url()), b'{"a":1}')
        self.assertEqual(self.post(serve(b'Content-Length: 7\r\nContent-Length: 7\r\n').url()), b'{"a":1}')
        self.assertEqual(self.post(serve(b'Content-Length: 0\r\n', b'').url()), b'')
        for label, headers in {'text': b'Content-Length: abc\r\n', 'negative': b'Content-Length: -1\r\n',
                               'plus': b'Content-Length: +7\r\n', 'list': b'Content-Length: 7, 7\r\n',
                               'space': b'Content-Length: 7 7\r\n', 'unicode digit': 'Content-Length: \u0667\r\n'.encode(),
                               'conflict': b'Content-Length: 7\r\nContent-Length: 8\r\n',
                               'huge': b'Content-Length: ' + b'9' * 40 + b'\r\n'}.items():
            with self.subTest(content_length=label):
                self.assertFails(lambda: self.post(serve(headers).url()), phase='headers')
        # A short body is a failure, not a silently truncated answer.
        self.assertFails(lambda: self.post(serve(b'Content-Length: 100\r\n').url()), phase='body')

    def test_declared_oversize_is_rejected_without_waiting_for_the_body(self):
        peer = self.peer(hold(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % (MAX_RESPONSE + 1)))
        start = time.monotonic()
        self.assertFails(lambda: self.post(peer.url(), timeout=5), phase='body')
        self.assertLess(time.monotonic() - start, 2)

    def test_body_cap_boundaries(self):
        def framed(framing, data):
            if framing == 'length':
                return b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(data) + data
            if framing == 'close':
                return b'HTTP/1.0 200 OK\r\n\r\n' + data
            return b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n' % len(data) + data + b'\r\n0\r\n\r\n'
        exact = b'x' * MAX_RESPONSE
        for framing in ('length', 'close', 'chunked'):
            with self.subTest(framing=framing, size='exactly the cap'):
                peer = self.peer(reply(framed(framing, exact)))
                self.assertEqual(self.post(peer.url(), timeout=10), exact)
            with self.subTest(framing=framing, size='one byte over'):
                peer = self.peer(reply(framed(framing, exact + b'y')))
                self.assertFails(lambda: self.post(peer.url(), timeout=10), phase='body')

    def test_many_tiny_chunks_cannot_exceed_the_cap(self):
        with patch('myradio.model_transport.MAX_RESPONSE', 2000):  # same logic, without a million Python loops
            peer = self.peer(reply(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + b'1\r\nx\r\n' * 2010))
            self.assertFails(lambda: self.post(peer.url(), timeout=20), phase='body')
            ok = self.peer(reply(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + b'1\r\nx\r\n' * 2000 + b'0\r\n\r\n'))
            self.assertEqual(self.post(ok.url()), b'x' * 2000)

    def test_chunked_bodies(self):
        head = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
        good = self.peer(reply(head + b'4\r\n{"a"\r\n' + b'4;ext=1\r\n:1}\n\r\n0\r\nTrailer: x\r\n\r\n'))
        self.assertEqual(self.post(good.url()), b'{"a":1}\n')
        for label, raw in {'not hex': b'not-a-chunk-size\r\n', 'negative size': b'-5\r\nabcde\r\n0\r\n\r\n',
                           'ends mid chunk': b'10\r\nabc', 'no terminator': b'3\r\nabc\r\n',
                           'size line cut': b'3'}.items():
            with self.subTest(chunked=label):
                peer = self.peer(reply(head + raw))
                self.assertFails(lambda: self.post(peer.url()), phase='body')

    def test_endless_trailers_or_headers_are_bounded_in_bytes_not_only_in_time(self):
        with patch('myradio.model_transport.MAX_WIRE', 100 * 1024):  # same logic, quick to reach
            def flood(peer, conn, head, body):
                conn.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n')
                line = b'X-Trailer: ' + b'y' * 1000 + b'\r\n'
                while not peer.stop.is_set():
                    conn.sendall(line)  # never ends the trailer section
            peer = self.peer(flood)
            start = time.monotonic()
            self.assertFails(lambda: self.post(peer.url(), timeout=20), phase='body')
            self.assertLess(time.monotonic() - start, 5, 'was only stopped by the deadline')
            fat = self.peer(reply(b'HTTP/1.1 200 OK\r\n' + b'X: ' + b'z' * 60000 + b'\r\n' + (b'Y: ' + b'z' * 60000 + b'\r\n') * 3))
            self.assertFails(lambda: self.post(fat.url(), timeout=20), phase='headers')

    def test_transfer_encoding_other_than_chunked_is_refused(self):
        for value in (b'gzip, chunked', b'identity', b'gzip', b'chunked, chunked'):
            with self.subTest(value=value):
                peer = self.peer(reply(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: ' + value + b'\r\n\r\n0\r\n\r\n'))
                self.assertFails(lambda: self.post(peer.url()), phase='headers')

    def test_connection_refused_and_unreachable_port(self):
        bound_only = socket.socket()
        self.addCleanup(bound_only.close)
        bound_only.bind(('127.0.0.1', 0))  # bound but not listening: refuses, and nobody else can take the port
        error = self.assertFails(lambda: self.post(f'http://127.0.0.1:{bound_only.getsockname()[1]}/v1'),
                                 phase='connect')
        self.assertEqual(error.kind, None)

    def test_next_address_is_tried_and_exhaustion_is_reported(self):
        peer = self.peer(reply(http_ok()))
        bound_only = socket.socket()
        self.addCleanup(bound_only.close)
        bound_only.bind(('127.0.0.1', 0))
        dead = ('127.0.0.1', bound_only.getsockname()[1])
        live = ('127.0.0.1', peer.port)
        entry = lambda address: (socket.AF_INET, socket.SOCK_STREAM, 6, '', address)  # noqa: E731
        transport = ModelTransport(resolver=lambda host, port: [entry(dead), entry(live)])
        self.assertEqual(self.post('http://model.test/v1', transport=transport), b'{"ok": true}')
        transport = ModelTransport(resolver=lambda host, port: [entry(dead)] * 6)
        self.assertFails(lambda: self.post('http://model.test/v1', transport=transport), phase='connect')

    def test_dead_addresses_of_one_family_cannot_use_up_every_connection_attempt(self):
        peer = self.peer(reply(http_ok()))
        bound_only = socket.socket()
        self.addCleanup(bound_only.close)
        bound_only.bind(('127.0.0.1', 0))
        dead = bound_only.getsockname()[1]  # nothing listens there, on IPv4 or on IPv6: connecting fails at once
        broken_v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', dead, 0, 0))
        good_v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', peer.port))
        # A dual-stack host whose first MAX_ADDRESSES records are dead IPv6 addresses and whose last is good IPv4.
        transport = ModelTransport(resolver=lambda host, port: [broken_v6] * mt.MAX_ADDRESSES + [good_v4])
        self.assertEqual(self.post('http://model.test/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(len(peer.requests), 1)

    def test_address_families_alternate_starting_with_the_resolvers_first_choice(self):
        v6, v4 = socket.AF_INET6, socket.AF_INET
        entry = lambda family, name: (family, socket.SOCK_STREAM, 6, '', (name, 80))  # noqa: E731
        six = [entry(v6, f'6{n}') for n in range(5)]
        four = [entry(v4, f'4{n}') for n in range(3)]
        cases = {
            'v6 first': (six[:3] + four[:1], [six[0], four[0], six[1], six[2]]),
            'v4 first': (four[:2] + six[:1], [four[0], six[0], four[1]]),
            'both lists unequal': (six + four, [six[0], four[0], six[1], four[1], six[2], four[2], six[3], six[4]]),
            'interleaved already': ([six[0], four[0], six[1], four[1]], [six[0], four[0], six[1], four[1]]),
            'one family keeps its order': (four, four),
            'nothing': ([], []),
        }
        for label, (given, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(mt._interleave(given), expected)

    def test_hostname_resolves_through_the_injected_resolver_and_keeps_its_name_in_host(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver)
        self.assertEqual(self.post(f'http://model.test:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(resolver.calls, ['model.test'])
        self.assertIn(b'Host: model.test:%d\r\n' % peer.port, peer.requests[0][0] + b'\r\n')

    def test_environment_proxies_are_ignored(self):
        proxy = self.peer(reply(http_ok(b'{"via": "proxy"}')))
        target = self.peer(reply(http_ok(b'{"via": "direct"}')))
        env = {name: proxy.base for name in ('http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY',
                                             'all_proxy', 'ALL_PROXY')}
        with patch.dict(os.environ, env):
            self.assertEqual(self.post(target.url()), b'{"via": "direct"}')
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(target.requests), 1)


class DeadlineTests(TransportCase):
    """One absolute deadline for the whole request, whichever phase is stalled."""

    def test_headers_drip_fed_one_byte_at_a_time(self):
        peer = self.peer(drip(b'HTTP/1.1 200 OK\r\nX-Slow: ' + b'x' * 400))
        self.assertDeadline(lambda: self.post(peer.url(), timeout=.3), .3, 'headers')

    def test_body_drip_fed_after_fast_headers(self):
        body = b'{"x": "' + b'y' * 300 + b'"}'
        peer = self.peer(drip(body, first=b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(body)))
        self.assertDeadline(lambda: self.post(peer.url(), timeout=.3), .3, 'body')

    def test_close_delimited_and_chunked_bodies_drip_fed(self):
        slow = b'z' * 300
        peer = self.peer(drip(slow, first=b'HTTP/1.0 200 OK\r\n\r\n'))
        self.assertDeadline(lambda: self.post(peer.url(), timeout=.3), .3, 'body')
        chunks = b''.join(b'1\r\nz\r\n' for _ in range(300))
        peer = self.peer(drip(chunks, first=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'))
        self.assertDeadline(lambda: self.post(peer.url(), timeout=.3), .3, 'body')

    def test_silent_peer_after_the_request(self):
        peer = self.peer(hold())
        self.assertDeadline(lambda: self.post(peer.url(), timeout=.3), .3, 'headers')

    def test_the_deadline_is_not_reset_by_each_received_byte(self):
        # Gaps between bytes (20 ms) are far below the budget; the total is what is bounded.
        peer = self.peer(drip(b'HTTP/1.1 200 OK\r\n' + b'X: y\r\n' * 100, delay=.01))
        start = time.monotonic()
        self.assertDeadline(lambda: self.post(peer.url(), timeout=.4), .4, 'headers')
        self.assertLess(time.monotonic() - start, 1.5)

    def test_peer_that_never_reads_a_large_request(self):
        peer = self.peer(hold(), read=False, rcvbuf=4096)
        # 48 MiB is far beyond any loopback socket buffering. It is built before the timed call: copying it
        # inside post() would count against the 0.4 s budget and, on a starved CPU, could use all of it
        # before the send starts (the failure would then be reported as "connect", not "send").
        with big_request(peer.url()):
            self.assertDeadline(lambda: self.post(peer.url(), timeout=.4), .4, 'send')

    # The next three tests are about the socket timeout being re-armed from the time left each time a
    # blocking call starts. The budget itself is checked before every call (so a drip-feed ends on time), but
    # a socket timeout that was set earlier and not lowered would let the call that is already blocked wait
    # a whole fresh budget: the request would then end later than its deadline by the time already spent.
    # Each test spends 0.7 s of a 1 s budget before the silence, so a missing re-arm ends at ~1.7 s.
    def test_header_reads_use_the_time_left_after_a_drip_and_then_silence(self):
        def drip_then_silence(peer, conn, head, body):
            conn.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
            end = time.monotonic() + .7
            while time.monotonic() < end and not peer.stop.is_set():
                conn.sendall(b'x')
                peer.stop.wait(.01)
            peer.stop.wait(30)
        peer = self.peer(drip_then_silence)
        self.assertDeadline(lambda: self.post(peer.url(), timeout=1), 1, 'headers', late=.35)

    def test_tls_handshake_timeout_counts_the_time_a_slow_connect_took(self):
        peer = self.peer(hold(), read=False)  # accepts, then never speaks: the handshake can only wait
        url = peer.base.replace('http://', 'https://')
        transport = ModelTransport(ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        with slow_connect(.7):
            self.assertDeadline(lambda: self.post(url, timeout=1, transport=transport), 1, 'tls', late=.35)

    def test_send_timeout_counts_the_time_a_slow_connect_took(self):
        peer = self.peer(hold(), read=False, rcvbuf=4096)
        with big_request(peer.url()), slow_connect(.7):
            self.assertDeadline(lambda: self.post(peer.url(), timeout=1), 1, 'send', late=.35)

    def test_name_lookup_that_never_returns(self):
        resolver = self.resolver(blocked={'stuck.test'})
        transport = ModelTransport(resolver=resolver)
        self.assertDeadline(lambda: self.post('http://stuck.test:9/v1', timeout=.3, transport=transport), .3, 'dns')

    def test_deadline_that_fires_mid_connect(self):
        address = self.stalled_listener()
        error = self.assertDeadline(lambda: self.post(f'http://127.0.0.1:{address[1]}/v1', timeout=.4), .4, 'connect')
        self.assertIsNone(error.kind)  # raised by the budget itself, not by a socket timeout

    def test_connect_slice_applies_only_while_other_addresses_remain(self):
        stalled = self.stalled_listener()
        live = self.peer(reply(http_ok()))
        entry = lambda address: (socket.AF_INET, socket.SOCK_STREAM, 6, '', address)  # noqa: E731
        with patch('myradio.model_transport.CONNECT_SLICE', .1):
            # A single address gets the whole budget, not just one slice of it.
            alone = ModelTransport(resolver=lambda host, port: [entry(stalled)])
            self.assertDeadline(lambda: self.post('http://model.test/v1', timeout=.6, transport=alone), .6, 'connect')
            # An earlier address is abandoned after its slice so that the next one can be tried.
            both = ModelTransport(resolver=lambda host, port: [entry(stalled), entry(('127.0.0.1', live.port))])
            start = time.monotonic()
            self.assertEqual(self.post('http://model.test/v1', timeout=5, transport=both), b'{"ok": true}')
            self.assertLess(time.monotonic() - start, 1.5)

    def test_tls_handshake_against_a_silent_peer(self):
        peer = self.peer(hold(), read=False)
        url = peer.base.replace('http://', 'https://')
        self.assertDeadline(lambda: self.post(url, timeout=.3), .3, 'tls')

    def test_tls_handshake_drip_fed_inside_a_record(self):
        def tls_drip(peer, conn, head, body):
            conn.sendall(b'\x16\x03\x03\x00\x40')  # a handshake record header, then its body trickled
            for _ in range(64):
                conn.sendall(b'\x00')
                if peer.stop.wait(.02):
                    return
        peer = self.peer(tls_drip, read=False)
        url = peer.base.replace('http://', 'https://')
        self.assertDeadline(lambda: self.post(url, timeout=.3), .3, 'tls')

    def test_tls_failure_is_a_failure_not_a_deadline(self):
        peer = self.peer(reply(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n'), read=False)
        url = peer.base.replace('http://', 'https://')
        start = time.monotonic()
        error = self.assertFails(lambda: self.post(url, timeout=5), phase='tls', kind='SSLError')
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(str(error), 'model request failed')

    def test_tiny_deadlines_only_ever_produce_deadline_errors(self):
        peer = self.peer(hold(), read=False)
        url = peer.base.replace('http://', 'https://')
        transport = ModelTransport()
        outcomes = set()
        for i in range(60):
            try:
                transport.post(url, b'{}', {}, timeout=.0005 + (i % 20) * .0007)
            except ModelDeadlineError as exc:
                outcomes.add(('deadline', exc.phase))
            except BaseException as exc:  # noqa: BLE001
                outcomes.add((type(exc).__name__, getattr(exc, 'phase', None)))
        self.assertLessEqual(outcomes, {('deadline', 'tls'), ('deadline', 'connect')})

class ResolverTests(TransportCase):
    def test_a_pending_lookup_is_shared_never_duplicated(self):
        resolver = self.resolver(blocked={'slow.test'})
        transport = ModelTransport(resolver=resolver)
        for _ in range(3):
            self.assertDeadline(lambda: self.post('http://slow.test:9/v1', timeout=.1, transport=transport), .1, 'dns')
        self.assertEqual(resolver.calls, ['slow.test'])
        self.assertEqual(len(dns_threads()), 1)
        self.assertTrue(all(t.daemon for t in dns_threads()), 'a stuck lookup must not keep the process alive')
        resolver.gate.set()
        self.assertTrue(until(lambda: not dns_threads()), 'the helper thread ends once the lookup returns')

    def test_callers_waiting_on_the_same_name_all_use_its_one_result(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver(blocked={'slow.test'})
        transport = ModelTransport(resolver=resolver)
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            transport.post(f'http://slow.test:{peer.port}/v1', b'{}', {}, timeout=10))) for _ in range(6)]
        for t in threads:
            t.start()
        self.assertTrue(until(lambda: resolver.calls == ['slow.test']))
        time.sleep(.2)  # let every caller reach the wait
        resolver.gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [b'{"ok": true}'] * 6)
        self.assertEqual(resolver.calls, ['slow.test'], 'waiting callers started lookups of their own')

    def test_concurrent_callers_for_one_name_share_one_lookup(self):
        resolver = self.resolver(blocked={'slow.test'})
        transport = ModelTransport(resolver=resolver)
        outcomes = []
        gate = threading.Barrier(8)

        def go():
            gate.wait()
            try:
                transport.post('http://slow.test:9/v1', b'{}', {}, timeout=.2)
            except ModelTransportError as exc:
                outcomes.append(exc.phase)
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(outcomes, ['dns'] * 8)
        self.assertEqual(resolver.calls, ['slow.test'])

    def test_a_different_name_waits_its_turn_then_runs_its_own_lookup(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver(blocked={'old.test'})
        transport = ModelTransport(resolver=resolver)
        self.assertDeadline(lambda: self.post(f'http://old.test:{peer.port}/v1', timeout=.15, transport=transport), .15, 'dns')
        # The operator switched endpoint while the old lookup is still stuck: the new name queues behind it.
        self.assertDeadline(lambda: self.post(f'http://new.test:{peer.port}/v1', timeout=.15, transport=transport), .15, 'dns')
        self.assertEqual(resolver.calls, ['old.test'])
        resolver.gate.set()
        self.assertEqual(self.post(f'http://new.test:{peer.port}/v1', timeout=3, transport=transport), b'{"ok": true}')
        self.assertEqual(resolver.calls, ['old.test', 'new.test'])
        self.assertTrue(until(lambda: not dns_threads()))

    def test_a_waiting_caller_gets_its_turn_as_soon_as_the_stuck_lookup_finishes(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver(blocked={'old.test'})
        transport = ModelTransport(resolver=resolver)
        self.assertDeadline(lambda: self.post(f'http://old.test:{peer.port}/v1', timeout=.1, transport=transport), .1, 'dns')
        threading.Timer(.2, resolver.gate.set).start()
        start = time.monotonic()
        self.assertEqual(self.post(f'http://new.test:{peer.port}/v1', timeout=5, transport=transport), b'{"ok": true}')
        self.assertLess(time.monotonic() - start, 2)

    def test_literals_and_localhost_do_not_depend_on_a_stuck_lookup(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver(blocked={'old.test'})
        transport = ModelTransport(resolver=resolver)
        self.assertDeadline(lambda: self.post('http://old.test:9/v1', timeout=.1, transport=transport), .1, 'dns')
        for host in ('127.0.0.1', 'localhost'):
            start = time.monotonic()
            self.assertEqual(self.post(f'http://{host}:{peer.port}/v1', timeout=3, transport=transport), b'{"ok": true}')
            self.assertLess(time.monotonic() - start, 1)

    def test_at_most_one_lookup_runs_at_a_time_under_racing_callers(self):
        # The window between "is a lookup pending?" and "record mine" is only a few bytecodes wide, so a
        # missing lock would rarely show. Widen it: every unlocked caller would then start its own helper.
        class SlowLookup(mt._Lookup):
            __slots__ = ()

            def __init__(self, key):
                time.sleep(.02)
                super().__init__(key)
        with patch('myradio.model_transport._Lookup', SlowLookup):
            for trial in range(3):
                resolver = Resolver(blocked={f'r{n}.test' for n in range(8)})
                transport = ModelTransport(resolver=resolver)
                gate = threading.Barrier(8)

                def go(n):
                    gate.wait()
                    try:
                        transport.post(f'http://r{n}.test:9/v1', b'{}', {}, timeout=.15)
                    except ModelTransportError:
                        pass
                threads = [threading.Thread(target=go, args=(n,)) for n in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(5)
                self.assertEqual(resolver.peak, 1, f'{resolver.peak} lookups ran at once in trial {trial}')
                self.assertEqual(len(resolver.calls), 1)
                resolver.gate.set()
                self.assertTrue(until(lambda: not dns_threads()))

    def test_resolver_failures_are_normalised_and_do_not_wedge_the_helper(self):
        peer = self.peer(reply(http_ok()))
        failures = [OSError('no such host'), socket.gaierror(-2, 'x'), UnicodeError('label too long'),
                    RuntimeError('boom ' + KEY), ValueError('bad'), MemoryError()]
        for failure in failures:
            def lookup(host, port, failure=failure):
                raise failure
            transport = ModelTransport(resolver=lookup)
            with self.subTest(failure=type(failure).__name__):
                error = self.assertFails(lambda: self.post(f'http://x.test:{peer.port}/v1', transport=transport), phase='dns')
                self.assertTrue(all(KEY not in text for text in error_texts(error)))
        for result in ([], None, [object()], [(1, 2)], 5):
            transport = ModelTransport(resolver=lambda host, port, result=result: result)
            with self.subTest(result=repr(result)):
                self.assertFails(lambda: self.post(f'http://x.test:{peer.port}/v1', transport=transport))
        # The same transport recovers once the resolver works.
        calls = []
        def flaky(host, port):
            calls.append(host)
            if len(calls) == 1:
                raise OSError('first lookup fails')
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        transport = ModelTransport(resolver=flaky)
        self.assertFails(lambda: self.post(f'http://x.test:{peer.port}/v1', transport=transport), phase='dns')
        self.assertEqual(self.post(f'http://x.test:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(len(calls), 2, 'completed lookups are not cached')

    def test_failure_to_start_the_helper_thread_does_not_wedge_the_resolver(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver)
        real_start = threading.Thread.start
        state = {'failed': False}

        def start(thread):
            if thread.name == 'myradio-model-dns' and not state['failed']:
                state['failed'] = True
                raise RuntimeError("can't start new thread")
            return real_start(thread)
        with patch.object(threading.Thread, 'start', start):
            self.assertFails(lambda: self.post(f'http://x.test:{peer.port}/v1', transport=transport))
            self.assertEqual(self.post(f'http://x.test:{peer.port}/v1', transport=transport), b'{"ok": true}')

    def test_a_helper_thread_that_cannot_start_leaves_no_lookup_recorded(self):
        # The lookup is recorded only once its thread is running, so a start that fails has nothing to clean up
        # and nothing a later request could wait on.
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver)
        real_start = threading.Thread.start

        def start(thread):
            if thread.name == 'myradio-model-dns':
                raise RuntimeError("can't start new thread")
            return real_start(thread)
        with patch.object(threading.Thread, 'start', start):
            self.assertFails(lambda: self.post('http://x.test:9/v1', transport=transport), phase='dns',
                             kind='RuntimeError')
        self.assertIsNone(transport._resolver._current)
        self.assertEqual(resolver.calls, [])

    def test_an_error_of_any_class_in_the_resolver_is_recorded_and_the_helper_thread_survives_it(self):
        peer = self.peer(reply(http_ok()))
        calls = []

        def lookup(host, port):
            calls.append(host)
            if len(calls) == 1:
                raise RuntimeError('boom ' + KEY)  # not an OSError
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        transport = ModelTransport(resolver=lookup)
        died = []
        with patch('threading.excepthook', lambda args: died.append(args.thread.name)):
            error = self.assertFails(lambda: self.post(f'http://x.test:{peer.port}/v1', transport=transport),
                                     phase='dns', kind='RuntimeError')
            self.assertTrue(until(lambda: not dns_threads()))
        # The helper reported the failure; it did not die with a traceback of its own.
        self.assertNotIn('myradio-model-dns', died)
        self.assertTrue(all(KEY not in text for text in error_texts(error)))
        self.assertEqual(self.post(f'http://x.test:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(calls, ['x.test', 'x.test'])

    def test_successful_lookups_are_not_cached(self):
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver)
        for _ in range(3):
            self.assertEqual(self.post(f'http://x.test:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(resolver.calls, ['x.test'] * 3)

    def test_old_style_numeric_hosts_are_names_for_the_lookup_helper(self):
        # Only canonical IP literals skip the lookup (the module docstring says so). Other spellings of an
        # address, which getaddrinfo itself understands, are looked up through the helper like any name.
        peer = self.peer(reply(http_ok()))
        resolver = self.resolver(blocked={'old.test'})
        transport = ModelTransport(resolver=resolver)
        hosts = ('127.1', '0x7f.1', '2130706433', '0')
        for host in hosts:
            with self.subTest(host=host):
                self.assertIsNone(mt._fixed_addresses(host, 80))
                self.assertEqual(self.post(f'http://{host}:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual(resolver.calls, list(hosts))
        # So, unlike a canonical literal, one of them waits behind a lookup that is stuck.
        self.assertDeadline(lambda: self.post('http://old.test:9/v1', timeout=.1, transport=transport), .1, 'dns')
        self.assertDeadline(lambda: self.post(f'http://127.1:{peer.port}/v1', timeout=.15, transport=transport), .15, 'dns')
        start = time.monotonic()
        self.assertEqual(self.post(f'http://127.0.0.1:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertLess(time.monotonic() - start, 1)

    def test_no_thread_is_left_behind_by_many_timeouts(self):
        before = set(threading.enumerate())
        resolver = self.resolver(blocked={'stuck.test'})
        transport = ModelTransport(resolver=resolver)
        silent = self.peer(hold())
        dripper = self.peer(drip(b'HTTP/1.1 200 OK\r\n' + b'X: y\r\n' * 200))
        tls_silent = self.peer(hold(), read=False)
        for _ in range(4):
            for url in ('http://stuck.test:9/v1', silent.url(), dripper.url(),
                        tls_silent.base.replace('http://', 'https://')):
                with self.assertRaises(ModelDeadlineError):
                    transport.post(url, b'{}', {}, timeout=.05)
        self.assertEqual(resolver.calls, ['stuck.test'])
        extra = [t for t in threading.enumerate() if t not in before and not t.name.startswith('test-peer')]
        self.assertEqual([t.name for t in extra], ['myradio-model-dns'], 'only the one bounded resolver may remain')
        resolver.gate.set()
        self.assertTrue(until(lambda: not [t for t in threading.enumerate()
                                           if t not in before and not t.name.startswith('test-peer')]))


class TlsContextTests(TransportCase):
    def test_context_is_lazy_built_once_and_verifies_certificates_and_hostnames(self):
        peer = self.peer(reply(http_ok()))
        not_tls = self.peer(reply(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n'), read=False)
        with patch('ssl.create_default_context', wraps=ssl.create_default_context) as spy:
            transport = ModelTransport()
            for _ in range(3):
                self.post(peer.url(), transport=transport)
            self.assertEqual(spy.call_count, 0, 'plain http never builds a TLS context')
            tls = not_tls.base.replace('http://', 'https://')
            for _ in range(4):
                self.assertFails(lambda: self.post(tls, transport=transport, timeout=2), phase='tls', kind='SSLError')
            self.assertEqual(spy.call_count, 1, 'one context per transport, not per request')
            racers = ModelTransport()
            contexts = []
            gate = threading.Barrier(8)

            def first_use():
                gate.wait()
                contexts.append(racers._tls_context())
            threads = [threading.Thread(target=first_use) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            self.assertEqual(spy.call_count, 2)
            self.assertEqual(len({id(c) for c in contexts}), 1)
        context = transport._tls_context()
        self.assertIs(context, transport._tls_context())
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_an_injected_context_replaces_the_default(self):
        context = ssl.create_default_context()
        with patch('ssl.create_default_context', side_effect=AssertionError('default context used')):
            transport = ModelTransport(ssl_context=context)
            self.assertIs(transport._tls_context(), context)


@unittest.skipUnless(OPENSSL, 'the openssl command-line tool is needed to make a test certificate')
class TlsWireTests(TransportCase):
    """Real certificates on loopback: what the transport verifies, and what it sends as SNI and ALPN."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.pki = Pki(directory.name)

    def secure_peer(self, certificate=None, sni=None, alpn=None, **kwargs):
        context = tls_server_context(certificate or self.pki.server, sni, alpn)
        return self.peer(reply(http_ok()), tls=context, **kwargs)

    def trusting(self):
        """A verifying client context that trusts only the test authority."""
        return ssl.create_default_context(cafile=self.pki.ca)

    def test_a_host_name_is_verified_against_the_certificate_and_sent_as_sni(self):
        sni = []
        peer = self.secure_peer(sni=sni)
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver, ssl_context=self.trusting())
        self.assertEqual(self.post(f'https://model.test:{peer.port}/v1', transport=transport), b'{"ok": true}')
        self.assertEqual((sni, resolver.calls, peer.handshake_errors), (['model.test'], ['model.test'], []))
        self.assertIn(b'Host: model.test:%d\r\n' % peer.port, peer.requests[0][0] + b'\r\n')

    def test_a_certificate_for_another_name_is_refused_in_the_tls_phase(self):
        mark = 'hostmark-q7x'
        peer = self.secure_peer()
        transport = ModelTransport(resolver=self.resolver(), ssl_context=self.trusting())
        error = self.assertFails(lambda: self.post(f'https://{mark}.test:{peer.port}/v1?k={KEY}', KEY.encode(),
                                                   {'Authorization': 'Bearer ' + KEY}, transport=transport),
                                 phase='tls', kind='SSLCertVerificationError')
        self.assertEqual(str(error), 'model request failed')
        self.assertTrue(until(lambda: peer.handshake_errors), 'the client did not abort the handshake')
        self.assertEqual(peer.requests, [], 'a request was sent although the certificate was for another name')
        self.assertIsNone(error.__context__)  # the library's message names the host; none of it stays reachable
        self.assertTrue(all(mark not in text and KEY not in text for text in error_texts(error)))

    def test_an_ip_literal_is_verified_against_the_certificates_ip_addresses_and_sends_no_sni(self):
        sni = []
        peer = self.secure_peer(sni=sni)
        transport = ModelTransport(ssl_context=self.trusting())
        self.assertEqual(self.post(peer.url(), transport=transport), b'{"ok": true}')
        self.assertEqual([name for name in sni if name is not None], [], 'an IP address was sent as a server name')
        self.assertEqual(peer.handshake_errors, [])

    @unittest.skipUnless(HAVE_IPV6, 'IPv6 loopback is not available here')
    def test_an_ipv6_literal_is_verified_against_the_certificates_ip_addresses_and_sends_no_sni(self):
        sni = []
        peer = self.secure_peer(sni=sni, family=socket.AF_INET6, host='::1')
        self.assertEqual(self.post(peer.url(), transport=ModelTransport(ssl_context=self.trusting())), b'{"ok": true}')
        self.assertEqual([name for name in sni if name is not None], [])
        self.assertIn(b'Host: [::1]:%d\r\n' % peer.port, peer.requests[0][0] + b'\r\n')

    def test_a_common_name_is_never_taken_for_an_ip_address(self):
        # This certificate's CN is 127.0.0.1 but it holds no IP address, only another DNS name.
        peer = self.secure_peer(self.pki.dns_only)
        self.assertFails(lambda: self.post(peer.url(), transport=ModelTransport(ssl_context=self.trusting())),
                         phase='tls', kind='SSLCertVerificationError')
        self.assertEqual(peer.requests, [])

    def test_the_root_dot_of_an_absolute_host_name_is_not_part_of_the_name_that_is_checked(self):
        sni = []
        peer = self.secure_peer(sni=sni)
        resolver = self.resolver()
        transport = ModelTransport(resolver=resolver, ssl_context=self.trusting())
        self.assertEqual(self.post(f'https://model.test.:{peer.port}/v1', transport=transport), b'{"ok": true}')
        # The certificate check and SNI see "model.test"; the lookup and the Host header keep the URL's spelling.
        self.assertEqual((sni, resolver.calls), (['model.test'], ['model.test.']))
        self.assertIn(b'Host: model.test.:%d\r\n' % peer.port, peer.requests[0][0] + b'\r\n')

    @unittest.skipUnless(ssl.HAS_ALPN, 'this OpenSSL has no ALPN')
    def test_the_default_context_offers_http_1_1_by_alpn_and_an_injected_context_is_left_alone(self):
        make = ssl.create_default_context

        def trusting_default(*args, **kwargs):  # the real default context, which also trusts the test authority
            context = make(*args, **kwargs)
            context.load_verify_locations(cafile=self.pki.ca)
            return context
        peer = self.secure_peer(alpn=['h2', 'http/1.1'])
        resolver = self.resolver()
        url = f'https://model.test:{peer.port}/v1'
        with patch('ssl.create_default_context', trusting_default):
            self.assertEqual(self.post(url, transport=ModelTransport(resolver=resolver)), b'{"ok": true}')
        self.assertEqual(peer.alpn, ['http/1.1'])
        # A context the caller built is used as it is: it offered nothing, so nothing was negotiated.
        self.assertEqual(self.post(url, transport=ModelTransport(resolver=resolver, ssl_context=self.trusting())),
                         b'{"ok": true}')
        self.assertEqual(peer.alpn, ['http/1.1', None])
        # A server that knows nothing about ALPN is served as before.
        plain = self.secure_peer()
        with patch('ssl.create_default_context', trusting_default):
            self.assertEqual(self.post(f'https://model.test:{plain.port}/v1', transport=ModelTransport(resolver=resolver)),
                             b'{"ok": true}')
        self.assertEqual(plain.alpn, [None])


class ConcurrencyTests(TransportCase):
    def test_parallel_posts_share_one_transport_without_crosstalk(self):
        def echo(peer, conn, head, body):
            peer.stop.wait(.15)
            conn.sendall(http_ok(b'{"len": %d, "first": %d}' % (len(body), body[0])))
        peer = self.peer(echo)
        transport = ModelTransport()
        results = {}

        def go(i):
            results[i] = transport.post(peer.url(), bytes([65 + i]) * (i + 1), {}, timeout=5)
        threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, {i: b'{"len": %d, "first": %d}' % (i + 1, 65 + i) for i in range(16)})
        # Serial execution would need 16 x 0.15 s = 2.4 s.
        self.assertLess(time.monotonic() - start, 1.2, 'requests ran one after another')

    def test_a_stalled_request_does_not_delay_others_on_the_same_transport(self):
        stalled = self.peer(hold())
        fast = self.peer(reply(http_ok()))
        transport = ModelTransport()
        outcome = []

        def stall():
            try:
                transport.post(stalled.url(), b'{}', {}, timeout=.6)
                outcome.append('returned')
            except ModelDeadlineError as exc:
                outcome.append(exc.phase)
        slow = threading.Thread(target=stall)
        slow.start()
        self.assertTrue(until(lambda: bool(stalled.requests)))
        start = time.monotonic()
        for _ in range(5):
            self.assertEqual(transport.post(fast.url(), b'{}', {}, timeout=3), b'{"ok": true}')
        self.assertLess(time.monotonic() - start, .4, 'fast requests waited for the stalled one')
        slow.join(5)
        self.assertEqual(outcome, ['headers'])


class FailureTextTests(TransportCase):
    def test_the_key_is_in_no_failure_text_for_any_failure_class(self):
        headers = {'Authorization': 'Bearer ' + KEY}
        leaky = f'?key={KEY}'
        refuse = socket.socket()
        self.addCleanup(refuse.close)
        refuse.bind(('127.0.0.1', 0))
        resolver = self.resolver(blocked={'stuck.test'})
        dns_fail = ModelTransport(resolver=lambda host, port: (_ for _ in ()).throw(OSError('lookup ' + KEY)))
        scenarios = {
            'refused': (f'http://127.0.0.1:{refuse.getsockname()[1]}/v1{leaky}', None),
            'status 401': (self.peer(reply(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 4\r\n\r\n' + KEY.encode()[:4])).url(leaky), None),
            'status 500 echoing the key': (self.peer(reply(b'HTTP/1.1 500 X\r\nContent-Length: %d\r\n\r\n%s' % (len(KEY), KEY.encode()))).url(leaky), None),
            'redirect': (self.peer(reply(b'HTTP/1.1 307 X\r\nLocation: http://127.0.0.1:1/' + KEY.encode() + b'\r\n\r\n')).url(leaky), None),
            'bad chunk': (self.peer(reply(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + KEY.encode() + b'\r\n')).url(leaky), None),
            'garbage': (self.peer(reply(KEY.encode() + b'\r\n\r\n')).url(leaky), None),
            'truncated': (self.peer(reply(b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n' + KEY.encode())).url(leaky), None),
            'oversize': (self.peer(reply(b'HTTP/1.0 200 OK\r\n\r\n' + b'k' * (MAX_RESPONSE + 1))).url(leaky), None),
            'headers drip': (self.peer(drip(b'HTTP/1.1 200 OK\r\nX: ' + b'x' * 300)).url(leaky), None),
            'silent': (self.peer(hold()).url(leaky), None),
            'tls to a plain peer': (self.peer(reply(b'HTTP/1.1 400 X\r\n\r\n'), read=False).base.replace('http://', 'https://') + leaky, None),
            'dns stuck': ('http://stuck.test:9/v1' + leaky, ModelTransport(resolver=resolver)),
            'dns error': ('http://x.test:9/v1' + leaky, dns_fail),
            'bad scheme': (f'ftp://127.0.0.1:9/v1{leaky}', None),
            'userinfo': (f'http://{KEY}:{KEY}@127.0.0.1:9/v1', None),
            'bad header': (f'http://127.0.0.1:9/v1{leaky}', None),
        }
        for label, (url, transport) in scenarios.items():
            with self.subTest(failure=label):
                call_headers = {'Authorization': 'Bearer ' + KEY + '\r\n'} if label == 'bad header' else headers
                with self.assertRaises(ModelTransportError) as caught:
                    (transport or ModelTransport()).post(url, b'{}', call_headers, timeout=.12)
                texts = error_texts(caught.exception)
                self.assertTrue(all(KEY not in text for text in texts), texts)
                self.assertTrue(all(KEY[:12] not in text for text in texts))

    def test_every_library_error_class_is_normalised_and_base_exceptions_are_not(self):
        library = [http.client.IncompleteRead(b'', 4), http.client.BadStatusLine(KEY), http.client.RemoteDisconnected(KEY),
                   http.client.LineTooLong('header line'), http.client.InvalidURL(KEY), http.client.CannotSendRequest(),
                   ssl.SSLError(KEY), ssl.SSLCertVerificationError(KEY), ssl.SSLEOFError(KEY), TimeoutError(KEY),
                   socket.timeout(KEY), ConnectionResetError(KEY), BrokenPipeError(KEY), OSError(KEY),
                   UnicodeDecodeError('utf-8', b'\xff', 0, 1, KEY), UnicodeError(KEY), ValueError(KEY), KeyError(KEY),
                   IndexError(KEY), TypeError(KEY), AttributeError(KEY), RuntimeError(KEY), RecursionError(KEY),
                   AssertionError(KEY), MemoryError(KEY), ZeroDivisionError(KEY), urllib.error.URLError(KEY)]
        transport = ModelTransport()
        for exc in library:
            with self.subTest(exc=type(exc).__name__), patch('myradio.model_transport._Exchange.run', side_effect=exc):
                with self.assertRaises(ModelTransportError) as caught:
                    transport.post('http://127.0.0.1:9/v1', b'{}', {'Authorization': 'Bearer ' + KEY}, timeout=1)
                self.assertTrue(all(KEY not in text for text in error_texts(caught.exception)))
                self.assertEqual(isinstance(caught.exception, ModelDeadlineError), isinstance(exc, TimeoutError))
                self.assertEqual(caught.exception.kind, type(exc).__name__)
        for exc in (KeyboardInterrupt(), SystemExit(3), GeneratorExit()):
            with self.subTest(exc=type(exc).__name__), patch('myradio.model_transport._Exchange.run', side_effect=exc):
                with self.assertRaises(type(exc)):
                    transport.post('http://127.0.0.1:9/v1', b'{}', {}, timeout=1)

    def test_sockets_are_closed_on_every_exit_path(self):
        plain, secure = [], []

        class Tracking(socket.socket):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                plain.append(self)

        class TrackingContext(ssl.SSLContext):
            def wrap_socket(self, *args, **kwargs):
                wrapped = super().wrap_socket(*args, **kwargs)
                secure.append(wrapped)
                return wrapped

        # Only the transport's own sockets are tracked: it sees this namespace as `socket`.
        names = {name: getattr(socket, name) for name in dir(socket) if not name.startswith('__')}
        names['socket'] = Tracking
        tracked_socket_module = types.SimpleNamespace(**names)
        transport = ModelTransport(ssl_context=TrackingContext(ssl.PROTOCOL_TLS_CLIENT))
        served = [self.peer(reply(raw)) for raw in (
            http_ok(), b'HTTP/1.1 500 X\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n', b'garbage\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort')]
        silent = self.peer(hold())
        not_tls = self.peer(reply(b'HTTP/1.1 400 X\r\n\r\n'), read=False)
        tls_silent = self.peer(hold(), read=False)
        with patch('myradio.model_transport.socket', tracked_socket_module):
            for peer in served:
                try:
                    transport.post(peer.url(), b'{}', {}, timeout=2)
                except ModelTransportError:
                    pass
            with self.assertRaises(ModelDeadlineError):
                transport.post(silent.url(), b'{}', {}, timeout=.1)
            with self.assertRaises(ModelTransportError):
                transport.post(not_tls.base.replace('http://', 'https://'), b'{}', {}, timeout=2)
            with self.assertRaises(ModelDeadlineError):
                transport.post(tls_silent.base.replace('http://', 'https://'), b'{}', {}, timeout=.1)
        self.assertEqual(len(plain), 8)
        self.assertEqual(len(secure), 2)
        self.assertEqual([s.fileno() for s in plain + secure], [-1] * 10, 'a socket was left open')

    def test_a_refused_connection_closes_its_socket(self):
        tracked = []

        class Tracking(socket.socket):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                tracked.append(self)
        refuse = socket.socket()
        self.addCleanup(refuse.close)
        refuse.bind(('127.0.0.1', 0))  # bound but not listening: connecting is refused
        dead = refuse.getsockname()
        live = self.peer(reply(http_ok()))
        entry = lambda address: (socket.AF_INET, socket.SOCK_STREAM, 6, '', address)  # noqa: E731
        with transport_sockets(Tracking):
            self.assertFails(lambda: self.post(f'http://127.0.0.1:{dead[1]}/v1'), phase='connect')
            self.assertEqual([s.fileno() for s in tracked], [-1], 'the refused attempt left its socket open')
            # An address that is refused is closed as well when the next one is then used.
            tracked.clear()
            transport = ModelTransport(resolver=lambda host, port: [entry(dead), entry(('127.0.0.1', live.port))])
            self.assertEqual(self.post('http://model.test/v1', transport=transport), b'{"ok": true}')
            self.assertEqual([s.fileno() for s in tracked], [-1, -1])

    def test_the_response_is_closed_on_every_exit_path(self):
        # A response left open is flushed again when it is collected, by which time its file may be gone: in
        # development mode Python then prints "Exception ignored ... I/O operation on closed file". Nothing
        # fails, so check directly that the transport closed every response it made.
        made = []

        class Tracking(http.client.HTTPResponse):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                made.append(self)
        scenarios = {
            'status 500': b'HTTP/1.1 500 X\r\nContent-Length: 0\r\n\r\n',
            'declared oversize': b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % (MAX_RESPONSE + 1),
            'bad content length': b'HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n',
            'truncated body': b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nshort',
            'headers cut off': b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n',
            'not http at all': b'garbage\r\n\r\n',
            'success': http_ok(),
        }
        with patch('http.client.HTTPResponse', Tracking):
            for label, raw in scenarios.items():
                with self.subTest(case=label):
                    made.clear()
                    peer = self.peer(reply(raw))
                    try:
                        self.post(peer.url())
                    except ModelTransportError:
                        pass
                    self.assertEqual(len(made), 1)
                    self.assertTrue(made[0].isclosed(), 'the response was left open')

    # Every text a ModelTransportError can carry, and every place a failure can be reported from.
    FIXED_TEXTS = frozenset({
        'invalid model URL', 'invalid request header', 'connection closed while sending',
        'unsupported transfer encoding', 'invalid content length', 'response too large', 'truncated response',
        'name lookup failed', 'could not connect', 'incomplete response headers', 'unexpected response status',
        'model request failed', 'model request deadline exceeded'})
    PHASES = frozenset({'dns', 'connect', 'tls', 'send', 'headers', 'body', 'request', 'status'})

    def test_failure_text_is_a_closed_set_and_quotes_no_peer_host_or_url_data(self):
        peer_mark, host_mark = 'peermark-q7x', 'hostmark-q7x.test'
        mark = peer_mark.encode()
        refuse = socket.socket()
        self.addCleanup(refuse.close)
        refuse.bind(('127.0.0.1', 0))
        resolver = self.resolver(blocked={'stuck-' + host_mark})  # every other name resolves to loopback
        transport = ModelTransport(resolver=resolver)
        naming_lookup = ModelTransport(resolver=lambda host, port: (_ for _ in ()).throw(OSError('no ' + host)))

        def at(port):
            return f'http://{host_mark}:{port}/v1/{peer_mark}?k={peer_mark}'

        def serving(raw, **kwargs):
            return at(self.peer(reply(raw), **kwargs).port)

        def post(url, headers=None, using=transport):
            return lambda: using.post(url, b'{}', headers or {}, timeout=.2)
        scenarios = {
            'connection refused': (post(at(refuse.getsockname()[1])), 'could not connect'),
            'status line and headers that carry the mark': (
                post(serving(b'HTTP/1.1 500 ' + mark + b'\r\nX-' + mark + b': ' + mark + b'\r\nContent-Length: 0\r\n\r\n')),
                'unexpected response status'),
            'redirect to a host with the mark': (
                post(serving(b'HTTP/1.1 307 X\r\nLocation: http://' + mark + b'.test/\r\nContent-Length: 0\r\n\r\n')),
                'unexpected response status'),
            'transfer encoding': (post(serving(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: ' + mark + b'\r\n\r\n')),
                                  'unsupported transfer encoding'),
            'content length': (post(serving(b'HTTP/1.1 200 OK\r\nContent-Length: ' + mark + b'\r\n\r\n')),
                               'invalid content length'),
            'declared oversize': (post(serving(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nX: %s\r\n\r\n'
                                               % (MAX_RESPONSE + 1, mark))), 'response too large'),
            'truncated body': (post(serving(b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n' + mark)),
                               'truncated response'),
            'headers that never end': (post(serving(b'HTTP/1.1 200 OK\r\nX: ' + mark + b'\r\n')),
                                       'incomplete response headers'),
            'not http at all': (post(serving(mark + b'\r\n\r\n')), 'model request failed'),
            'protocol version with the mark': (post(serving(b'HTTP/9.' + mark + b' 200 OK\r\n\r\n')),
                                               'model request failed'),
            'chunk size with the mark': (
                post(serving(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + mark + b'\r\n')),
                'model request failed'),
            'tls to a plain peer': (post(serving(b'HTTP/1.1 400 ' + mark + b'\r\n\r\n', read=False)
                                         .replace('http://', 'https://')), 'model request failed'),
            'silent peer': (post(at(self.peer(hold()).port)), 'model request deadline exceeded'),
            'lookup error that names the host': (post(at(9), using=naming_lookup), 'name lookup failed'),
            'lookup that never returns': (post(f'http://stuck-{host_mark}:9/v1/{peer_mark}'),
                                          'model request deadline exceeded'),
            'url that is refused': (post(f'http://user:{peer_mark}@{host_mark}/v1'), 'invalid model URL'),
            'header that is refused': (post(at(9), {'X-Key': peer_mark + '\r\n'}), 'invalid request header'),
        }
        self.assertLessEqual({expected for _, expected in scenarios.values()}, self.FIXED_TEXTS)
        for label, (call, expected) in scenarios.items():
            with self.subTest(case=label):
                with self.assertRaises(ModelTransportError) as caught:
                    call()
                error = caught.exception
                self.assertEqual(str(error), expected)
                self.assertEqual(error.args, (expected,), 'something rides along with the fixed text')
                self.assertIn(error.phase, self.PHASES)
                for text in error_texts(error) + [str(error.phase), str(error.kind)]:
                    self.assertNotIn(peer_mark, text)
                    self.assertNotIn(host_mark, text)
                self.assertNotIn('127.0.0.1', repr(error))


class ChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.station = Station(self.temp.name, start_worker=False)
        with patch.dict('os.environ', {}, clear=True):
            self.chat = Chat(self.station)
        self.chat.configure({'url': 'http://127.0.0.1:9/v1', 'model': 'fixture', 'key': KEY})
        self.peers = []

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        self.station.close()
        self.temp.cleanup()

    def serve(self, script, **kwargs):
        peer = Peer(script, **kwargs)
        self.peers.append(peer)
        return peer

    def assertSafeFailure(self):
        with self.assertRaises(StationError) as caught:
            self.chat.message('Rename the station')
        self.assertEqual(str(caught.exception), SAFE)
        # No chained exception: whatever failed, its text (URL, key, peer data) is not reachable from here.
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(all(KEY not in text for text in error_texts(caught.exception)))
        self.assertEqual(self.chat.pending, {})
        self.assertEqual(self.chat.history, [])
        self.assertEqual(self.station.status()['station']['name'], 'My radio')
        self.assertTrue(self.station.chat_lock.acquire(blocking=False), 'the chat lock was not released')
        self.station.chat_lock.release()

    @staticmethod
    def answer(message):
        return json.dumps({'choices': [{'message': message}]}).encode()

    def test_one_transport_per_chat_survives_reconfiguration(self):
        transport = self.chat._transport
        self.assertIsInstance(transport, ModelTransport)
        self.chat.configure({'url': 'http://127.0.0.1:10/v1', 'model': 'other', 'key': ''})
        self.chat.configure({'disconnect': True})
        self.assertIs(self.chat._transport, transport)
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNot(Chat(self.station)._transport, transport)

    def test_timeout_constant_is_read_when_the_request_starts(self):
        import myradio.chat as chat_module
        self.assertEqual(chat_module.MODEL_REQUEST_TIMEOUT, 45)
        for value in (.25, 3, 120):
            with patch('myradio.chat.MODEL_REQUEST_TIMEOUT', value), \
                    patch.object(ModelTransport, 'post', return_value=self.answer({'content': 'hi'})) as post:
                self.chat.message('x')
            self.assertEqual(post.call_args.kwargs, {'timeout': value})

    def test_every_failure_class_maps_to_the_one_safe_error(self):
        failures = [http.client.IncompleteRead(b'x', 5), http.client.BadStatusLine(KEY), http.client.RemoteDisconnected(KEY),
                    http.client.LineTooLong('header line'), http.client.InvalidURL(KEY), ssl.SSLError(KEY),
                    ssl.SSLCertVerificationError(KEY), TimeoutError(KEY), socket.timeout(KEY), ConnectionResetError(KEY),
                    OSError(KEY), UnicodeDecodeError('utf-8', b'\xff', 0, 1, KEY), UnicodeError(KEY), ValueError(KEY),
                    KeyError(KEY), IndexError(KEY), TypeError(KEY), AttributeError(KEY), RuntimeError(KEY),
                    RecursionError(KEY), AssertionError(KEY), MemoryError(KEY), ZeroDivisionError(KEY),
                    json.JSONDecodeError(KEY, '', 0), ModelTransportError('x', 'dns'), ModelDeadlineError('tls'),
                    StationError(KEY)]
        for exc in failures:
            with self.subTest(exc=type(exc).__name__), patch.object(ModelTransport, 'post', side_effect=exc):
                self.chat.pending['stale'] = (time.monotonic() + 120, {})
                self.assertSafeFailure()

    def test_base_exceptions_propagate_and_release_the_lock(self):
        for exc in (KeyboardInterrupt(), SystemExit(1)):
            with self.subTest(exc=type(exc).__name__), patch.object(ModelTransport, 'post', side_effect=exc):
                with self.assertRaises(type(exc)):
                    self.chat.message('x')
                self.assertEqual(self.chat.pending, {})
                self.assertEqual(self.chat.history, [])
                self.assertTrue(self.station.chat_lock.acquire(blocking=False))
                self.station.chat_lock.release()

    def test_malformed_answers_are_safe_and_mutate_nothing(self):
        call = {'function': {'name': 'station_update', 'arguments': '{"name": "A"}'}}
        bad = [b'', b'not json', b'\xff\xfe\x00\x01', b'[]', b'{}', b'null', b'"text"', b'7', b'{"choices": null}',
               b'{"choices": []}', b'{"choices": {}}', b'{"choices": [1]}', b'{"choices": ["x"]}',
               b'[' * 200000,  # json.loads raises RecursionError, which is not an OSError/ValueError
               b'[' * 200000 + b']' * 200000, self.answer([]), self.answer('text'), self.answer(None),
               self.answer({'content': 7}), self.answer({'content': ['a']}), self.answer({'tool_calls': 'x'}),
               self.answer({'tool_calls': {}}), self.answer({'tool_calls': [call] * 9}),
               self.answer({'tool_calls': [1]}), self.answer({'tool_calls': [{}]}),
               self.answer({'tool_calls': [{'function': {}}]}),
               self.answer({'tool_calls': [{'function': {'name': 'no_such_tool', 'arguments': '{}'}}]}),
               self.answer({'tool_calls': [{'function': {'name': 'station_update', 'arguments': 'not json'}}]}),
               self.answer({'tool_calls': [{'function': {'name': 'station_update', 'arguments': '[]'}}]}),
               self.answer({'tool_calls': [{'function': {'name': 'station_update', 'arguments': '{"bogus": 1}'}}]}),
               self.answer({'tool_calls': [{'function': {'name': 'station_update', 'arguments': 5}}]}),
               self.answer({'tool_calls': [{'function': {'name': ['x'], 'arguments': '{}'}}]})]
        for index, payload in enumerate(bad):
            with self.subTest(case=index), patch.object(ModelTransport, 'post', return_value=payload):
                self.chat.history.clear()  # one case must not be able to hide behind another's state
                self.assertSafeFailure()
        # A syntactically valid document that merely has no content is an acceptable answer, not an error.
        with patch.object(ModelTransport, 'post', return_value=b'{"choices": [{"message": {"choices": []}}]}'):
            self.assertEqual(self.chat.message('x')['actions'], [])

    def test_proposals_are_published_only_when_every_call_validated(self):
        good = {'function': {'name': 'station_update', 'arguments': '{"name": "Fresh FM"}'}}
        broken = {'function': {'name': 'station_update', 'arguments': '{"unknown_field": 1}'}}
        with patch.object(ModelTransport, 'post', return_value=self.answer({'tool_calls': [good, good, broken]})):
            self.assertSafeFailure()
        with patch.object(ModelTransport, 'post', return_value=self.answer({'tool_calls': [good, good]})):
            result = self.chat.message('x')
        self.assertEqual(sorted(self.chat.pending), sorted(a['id'] for a in result['actions']))
        self.assertEqual(len(result['actions']), 2)

    def test_a_later_message_invalidates_earlier_proposals_even_when_it_succeeds(self):
        call = {'function': {'name': 'station_update', 'arguments': '{"name": "Stale FM"}'}}
        proposing = self.answer({'content': 'Proposed.', 'tool_calls': [call]})
        with patch.object(ModelTransport, 'post', return_value=proposing):
            old = self.chat.message('Rename it')['actions'][0]['id']
        self.assertIn(old, self.chat.pending)
        approvable = []

        def never_mind(*args, **kwargs):
            approvable.append(sorted(self.chat.pending))  # what is still approvable while the next request runs
            return self.answer({'content': 'Never mind.'})
        with patch.object(ModelTransport, 'post', side_effect=never_mind):
            self.assertEqual(self.chat.message('Never mind')['actions'], [])
        self.assertEqual(approvable, [[]], 'the old proposal must be dropped as soon as the next message starts')
        with self.assertRaisesRegex(StationError, 'expired or was already handled'):
            self.chat.approve(old)
        self.assertEqual(self.station.status()['station']['name'], 'My radio')
        # When the new answer proposes something itself, only that one can be approved.
        with patch.object(ModelTransport, 'post', return_value=proposing):
            first = self.chat.message('Rename it')['actions'][0]['id']
            second = self.chat.message('Rename it again')['actions'][0]['id']
        with self.assertRaisesRegex(StationError, 'expired or was already handled'):
            self.chat.approve(first)
        self.chat.approve(second)
        self.assertEqual(self.station.status()['station']['name'], 'Stale FM')

    def test_proposals_are_dropped_even_if_the_next_request_is_interrupted(self):
        self.chat.pending['stale'] = (time.monotonic() + 120, {'id': 'stale', 'operation': 'station.update',
                                                               'arguments': {'name': 'Stale FM'}})
        with patch.object(ModelTransport, 'post', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.chat.message('x')
        self.assertEqual(self.chat.pending, {})
        with self.assertRaises(StationError):
            self.chat.approve('stale')
        self.assertEqual(self.station.status()['station']['name'], 'My radio')

    def test_an_interruption_between_proposals_leaves_nothing_approvable(self):
        # BaseException skips the failure handler, so atomic staging is what keeps half a batch out of `pending`.
        good = {'function': {'name': 'station_update', 'arguments': '{"name": "Half FM"}'}}
        tokens = iter(['first-token'])

        def token(nbytes):
            try:
                return next(tokens)
            except StopIteration:
                raise KeyboardInterrupt from None
        with patch.object(ModelTransport, 'post', return_value=self.answer({'tool_calls': [good, good]})), \
                patch('myradio.chat.secrets.token_urlsafe', token):
            with self.assertRaises(KeyboardInterrupt):
                self.chat.message('x')
        self.assertEqual(self.chat.pending, {})
        self.assertEqual(self.chat.history, [])
        self.assertTrue(self.station.chat_lock.acquire(blocking=False))
        self.station.chat_lock.release()
        with self.assertRaises(StationError):
            self.chat.approve('first-token')

    def test_end_to_end_through_a_real_socket_with_single_use_approval(self):
        peer = self.serve(reply(http_ok(ANSWER)))
        self.chat.configure({'url': peer.base, 'model': 'wire-fixture', 'key': KEY})
        result = self.chat.message('Rename the station')
        head, body = peer.requests[0]
        self.assertTrue(head.startswith(b'POST /v1/chat/completions HTTP/1.1\r\n'))
        self.assertIn(b'Authorization: Bearer ' + KEY.encode(), head)
        self.assertEqual(json.loads(body)['model'], 'wire-fixture')
        self.assertNotIn(KEY.encode(), body)
        self.assertEqual(self.station.status()['station']['name'], 'My radio')
        self.chat.approve(result['actions'][0]['id'])
        self.assertEqual(self.station.status()['station']['name'], 'Wire FM')
        with self.assertRaises(StationError):
            self.chat.approve(result['actions'][0]['id'])

    def test_real_socket_failures_release_the_operator_and_keep_the_key_private(self):
        stuck = self.serve(hold())
        self.chat.configure({'url': stuck.base, 'model': 'm', 'key': KEY})
        with patch('myradio.chat.MODEL_REQUEST_TIMEOUT', .2):
            start = time.monotonic()
            self.assertSafeFailure()
            self.assertLess(time.monotonic() - start, 1.5)
        bad = self.serve(reply(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n'))
        self.chat.configure({'url': bad.base, 'model': 'm', 'key': KEY})
        self.assertSafeFailure()
        refused = socket.socket()
        self.addCleanup(refused.close)
        refused.bind(('127.0.0.1', 0))
        self.chat.configure({'url': f'http://127.0.0.1:{refused.getsockname()[1]}/v1', 'model': 'm', 'key': KEY})
        self.assertSafeFailure()
        # And a good endpoint still works on the same Chat and transport afterwards.
        good = self.serve(reply(http_ok(ANSWER)))
        self.chat.configure({'url': good.base, 'model': 'm', 'key': KEY})
        self.assertEqual(len(self.chat.message('again')['actions']), 1)

    def test_switching_endpoint_while_the_old_lookup_is_still_stuck(self):
        good = self.serve(reply(http_ok(ANSWER)))
        resolver = Resolver(blocked={'old.test'})
        self.addCleanup(resolver.gate.set)
        self.chat._transport = ModelTransport(resolver=resolver)
        with patch('myradio.chat.MODEL_REQUEST_TIMEOUT', .2):
            self.chat.configure({'url': f'https://old.test:{good.port}/v1', 'model': 'm', 'key': KEY})
            self.assertSafeFailure()  # the old name's lookup is stuck: bounded failure, lock released
            self.chat.configure({'url': good.base, 'model': 'm', 'key': KEY})
            self.assertEqual(len(self.chat.message('x')['actions']), 1)  # an IP endpoint is unaffected
            self.chat.configure({'url': f'https://new.test:{good.port}/v1', 'model': 'm', 'key': KEY})
            self.assertSafeFailure()  # the new name waits behind the stuck lookup, within its own deadline
            self.assertEqual(resolver.calls, ['old.test'])
        resolver.gate.set()
        self.assertTrue(until(lambda: not dns_threads()))
        self.assertEqual(resolver.peak, 1)


if __name__ == '__main__':
    unittest.main()
