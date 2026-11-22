"""Loopback-only HTTP service shared by the dashboard and authorized agents."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from ._platform import O_BINARY, WINDOWS
from .broadcast import BroadcastError
from .chat import Chat
from .extensions import Request, builtin_extensions
from .station import MAX_UPLOAD, Station, StationError

WEB = Path(__file__).with_name("web")
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


# Signed requests: a client that holds the control token signs each request with it instead of sending it, so a
# listener that is not this studio (another account holding the port, or the far end of a tunnel while the studio
# is down) never learns the token. Each signature covers a one-time challenge that this studio issued, so a captured
# request cannot be replayed here or anywhere else. The studio signs its reply to a signed request, so the client
# can also tell that it reached this studio.
#   GET /api/challenge          -> {"challenge": "<32 hex>", "expires_in": 30}   (no token needed; single use)
#   X-Myradio-Auth: v1 <challenge> <64 hex HMAC-SHA256>
#   X-Myradio-Body-SHA256: <64 hex SHA-256 of the request body (of b"" when there is none)>
#   signed:   "myradio-v1\n{method}\n{path and query}\n{Host}\n{challenge}\n{body sha256}"
#   reply:    X-Myradio-Response-Auth: v1 <HMAC-SHA256 of "myradio-v1-response\n{challenge}\n{status}\n{body sha256}">
#             (for the live stream the body hash is the word "stream")
# Login tickets let a client that holds the token open the dashboard without handing the token to the browser:
# POST /api/login-ticket (authorized) returns a ticket that POST /api/session trades once, within 60 seconds,
# for a dashboard session (header X-Myradio-Ticket).
#
# Dashboard sessions are not cookies. Browsers send a cookie for 127.0.0.1 to every port on the host, so another
# local service the same browser visits would receive it. The session goes back in the /api/session reply, the page
# keeps it in sessionStorage (scoped to this origin, port included) and sends it as X-Myradio-Session. The <audio>
# monitor cannot send headers, so POST /api/stream-ticket gives a ticket that GET /stream.wav?ticket= accepts for
# STREAM_TICKET_TTL seconds.
STREAM_TICKET_TTL = 30
CLEAR_OLD_COOKIE = {"Set-Cookie": "myradio_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"}  # from 0.5.1 and older
TICKET_TTL = 60
TICKET_LIMIT = 16
TICKET_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
CHALLENGE_TTL = 30
CHALLENGE_LIMIT = 1024
SIGNED_AUTH = re.compile(r"v1 ([0-9a-f]{32}) ([0-9a-f]{64})")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def sign(token, *parts):
    return hmac.new(token.encode(), "\n".join(parts).encode("latin-1"), hashlib.sha256).hexdigest()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second program bind the same port and steal requests, so it stays off there.
    allow_reuse_address = not WINDOWS
    request_queue_size = 16

    def __init__(self, address, station, token, public_origin=None):
        self.public_origin = public_origin
        if address[0] != "127.0.0.1":
            raise StationError("The admin service must bind to 127.0.0.1. Use an authenticated tunnel for remote access.")
        self.station = station
        self.chat = Chat(station)
        self.token = token
        self.sessions = set()
        self.session_lock = threading.Lock()
        self.tickets = OrderedDict()      # login ticket -> monotonic expiry, oldest first
        self.stream_tickets = OrderedDict()  # monitor ticket -> monotonic expiry, oldest first
        self.challenges = OrderedDict()   # challenge -> monotonic expiry, oldest first
        self.challenge_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(32)
        self.upload_slot = threading.BoundedSemaphore(1)
        self.agent_lock = threading.Lock()
        self.agent_seen = {"seen_at": None, "last": None}
        super().__init__(address, Handler)

    def issue_challenge(self):
        """A fresh single-use challenge for one signed request. The oldest are dropped when the store is full."""
        now = time.monotonic()
        with self.challenge_lock:
            while self.challenges and next(iter(self.challenges.values())) <= now:
                self.challenges.popitem(last=False)
            while len(self.challenges) >= CHALLENGE_LIMIT:
                self.challenges.popitem(last=False)
            challenge = secrets.token_hex(16)
            self.challenges[challenge] = now + CHALLENGE_TTL
            return challenge

    def issue_ticket(self):
        now = time.monotonic()
        with self.challenge_lock:
            while self.tickets and next(iter(self.tickets.values())) <= now:
                self.tickets.popitem(last=False)
            while len(self.tickets) >= TICKET_LIMIT:
                self.tickets.popitem(last=False)
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = now + TICKET_TTL
            return ticket

    def redeem_ticket(self, ticket):
        if not TICKET_FORMAT.fullmatch(ticket or ""):
            return False
        with self.challenge_lock:
            match = next((t for t in self.tickets if hmac.compare_digest(t, ticket)), None)
            until = self.tickets.pop(match, None) if match else None
        return until is not None and until > time.monotonic()

    def issue_stream_ticket(self):
        now = time.monotonic()
        with self.challenge_lock:
            while self.stream_tickets and next(iter(self.stream_tickets.values())) <= now:
                self.stream_tickets.popitem(last=False)
            while len(self.stream_tickets) >= TICKET_LIMIT:
                self.stream_tickets.popitem(last=False)
            ticket = secrets.token_urlsafe(32)
            self.stream_tickets[ticket] = now + STREAM_TICKET_TTL
            return ticket

    def stream_ticket_valid(self, ticket):
        """A monitor ticket works until it expires, so the browser's media player may retry its first connection."""
        if not TICKET_FORMAT.fullmatch(ticket or ""):
            return False
        with self.challenge_lock:
            match = next((t for t in self.stream_tickets if hmac.compare_digest(t, ticket)), None)
            return bool(match) and self.stream_tickets[match] > time.monotonic()

    def redeem_challenge(self, challenge):
        with self.challenge_lock:
            until = self.challenges.pop(challenge, None)
        return until is not None and until > time.monotonic()

    def note_agent(self, what):
        """Remember when an external agent (the MCP adapter or the connect command) last used the station, and for what."""
        what = what if isinstance(what, str) and re.fullmatch(r"[a-z][a-z0-9_.\-/]{0,59}", what) else "request"
        with self.agent_lock:
            self.agent_seen = {"seen_at": time.time(), "last": what}

    def agent_status(self):
        with self.agent_lock:
            return dict(self.agent_seen)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "myradio"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def parse_request(self):
        self.signed = None   # (challenge, body sha256) once this request's signature checks out
        return super().parse_request()

    def log_message(self, fmt, *args):
        # Paths, chat text and launch tokens do not belong in logs.
        pass

    def headers_base(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def respond(self, status, value, *, content_type="application/json; charset=utf-8", extra=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.headers_base()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.sign_reply(status, hashlib.sha256(data).hexdigest())
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def safe_origin(self, navigation=False):
        """Host (and Origin, when sent) must be this studio's. Cross-site requests are refused, except a top-level
        navigation to the dashboard page itself (`navigation`), which holds no secrets and cannot be framed; a
        launcher opens it from a local file. Every API request stays same-origin only."""
        cross_site = self.headers.get("Sec-Fetch-Site") == "cross-site" and not navigation
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        # Opt in to exactly one HTTPS origin behind a private reverse proxy.
        # Never infer trust from attacker-controlled Forwarded headers.
        if self.server.public_origin:
            remote = urlsplit(self.server.public_origin)
            if host == remote.netloc:
                return (not origin or origin == self.server.public_origin) and not cross_site
        # Host restriction also blocks browser DNS-rebinding attacks.
        return host in allowed and (not origin or origin in {f"http://{a}" for a in allowed}) and not cross_site

    def sign_reply(self, status, body_hash):
        if self.signed:
            self.send_header("X-Myradio-Response-Auth",
                             "v1 " + sign(self.server.token, "myradio-v1-response", self.signed[0], str(status), body_hash))

    def signed_request(self):
        match = SIGNED_AUTH.fullmatch(self.headers.get("X-Myradio-Auth", ""))
        body_hash = self.headers.get("X-Myradio-Body-SHA256", "")
        if not match or not SHA256_HEX.fullmatch(body_hash):
            return False
        challenge, signature = match.groups()
        expected = sign(self.server.token, "myradio-v1", self.command, self.path, self.headers.get("Host", ""),
                        challenge, body_hash)
        # Redeem only after the signature checks out, so nobody without the token can use up others' challenges.
        if not hmac.compare_digest(expected, signature) or not self.server.redeem_challenge(challenge):
            return False
        self.signed = (challenge, body_hash)
        return True

    def authorized(self):
        if self.headers.get("X-Myradio-Auth") is not None:
            return self.signed_request()   # a signed request stands or falls by its signature alone
        token = self.headers.get("X-Myradio-Token", "")
        if token and token.isascii() and hmac.compare_digest(token, self.server.token):
            return True
        return self.has_session()   # never a cookie: see STREAM_TICKET_TTL above

    def read_body(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise StationError("Chunked request bodies are not supported.")
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise StationError("Invalid Content-Length.") from None
        if size < 0 or size > maximum:
            raise StationError(f"Request body must be no larger than {maximum} bytes.")
        data = self.rfile.read(size)
        if len(data) != size:
            raise StationError("Incomplete request body.")
        if self.signed and not hmac.compare_digest(hashlib.sha256(data).hexdigest(), self.signed[1]):
            self.close_connection = True
            raise StationError("The request body does not match its signature.")
        return data

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise StationError("Send application/json.")
        data = self.read_body(32768)
        try:
            body = json.loads(data)
        except (ValueError, UnicodeError):
            raise StationError("Invalid JSON body.") from None
        if not isinstance(body, dict):
            raise StationError("Send a JSON object.")
        return body

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            navigation = (path == "/" and self.headers.get("Sec-Fetch-Mode") == "navigate"
                          and self.headers.get("Sec-Fetch-Dest") == "document")
            if not self.safe_origin(navigation):
                return self.respond(403, {"error": "Only same-origin loopback requests are allowed."})
            if path in ("/", "/app.js", "/style.css"):
                name = "index.html" if path == "/" else path[1:]
                mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
                return self.respond(200, (WEB / name).read_bytes(), content_type=mime)
            art = re.fullmatch(r"/art/([a-z0-9-]{1,48})\.(png|webp)", path)
            if art:
                file = WEB / "art" / f"{art.group(1)}.{art.group(2)}"
                if not file.is_file():
                    return self.respond(404, {"error": "Not found."})
                return self.respond(200, file.read_bytes(), content_type="image/" + art.group(2),
                                    extra={"Cache-Control": "max-age=86400"})
            if path == "/api/challenge":
                return self.respond(200, {"challenge": self.server.issue_challenge(), "expires_in": CHALLENGE_TTL})
            asset = re.fullmatch(r"/panels/([a-z0-9_-]{1,40}\.(?:js|css))", path)
            if asset:
                return self.panel_asset(asset.group(1))
            if path == "/stream.wav" and self.server.stream_ticket_valid(parse_qs(urlsplit(self.path).query).get("ticket", [""])[0]):
                return self.stream()
            if not self.authorized():
                return self.respond(401, {"error": "Unlock with the launch code from your launcher."})
            if self.agent_client():
                self.server.note_agent("status" if path == "/api/status" else path.removeprefix("/api/"))
            if path == "/api/connect":
                return self.respond(200, self.connect_info())
            if path == "/api/operations":
                return self.respond(200, self.server.station.operations())
            if path == "/api/panels":
                return self.respond(200, self.server.station.extensions.panels())
            if path.startswith("/api/x/"):
                return self.extension_route("GET", path)
            if path == "/api/agent":
                return self.respond(200, self.server.chat.settings())
            if path == "/api/broadcast":
                return self.respond(200, self.server.station.broadcast.settings_summary())
            if path == "/api/status":
                return self.respond(200, {**self.server.station.status(), "agent_link": self.server.agent_status()})
            if path == "/stream.wav":
                return self.stream()
            return self.respond(404, {"error": "Not found."})
        except StationError as exc:
            self.respond(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def do_POST(self):
        try:
            if not self.safe_origin():
                self.close_connection = True
                return self.respond(403, {"error": "Only same-origin loopback requests are allowed."})
            path = urlsplit(self.path).path
            ticket = self.headers.get("X-Myradio-Ticket")
            if path == "/api/session" and ticket is not None:
                self.json_body()
                if self.has_session():
                    return self.respond(200, {"ok": True})
                if self.server.redeem_ticket(ticket):
                    return self.new_session()
                self.close_connection = True
                return self.respond(401, {"error": "That link has expired. Open the studio again."})
            if not self.authorized():
                self.close_connection = True
                return self.respond(401, {"error": "Unlock with the launch code from your launcher."})
            if path == "/api/shutdown":
                self.json_body()
                self.close_connection = True
                self.respond(200, {"ok": True, "message": "The studio is shutting down."})
                self.wfile.flush()
                # serve_forever returns and main() closes the station, as Ctrl+C does. Playout and broadcast stop with
                # it; a client that wants the station to stay off on the next start sends transport.stop first.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/login-ticket":
                self.json_body()
                return self.respond(200, {"ticket": self.server.issue_ticket(), "expires_in": TICKET_TTL})
            if path == "/api/stream-ticket":
                self.json_body()
                return self.respond(200, {"ticket": self.server.issue_stream_ticket(), "expires_in": STREAM_TICKET_TTL})
            if path == "/api/session":
                self.json_body()
                if self.has_session():
                    return self.respond(200, {"ok": True})
                return self.new_session()
            if path == "/api/logout":
                self.json_body()
                with self.server.session_lock:
                    self.server.sessions.discard(self.headers.get("X-Myradio-Session", ""))
                return self.respond(200, {"ok": True}, extra=CLEAR_OLD_COOKIE)
            if path == "/api/upload":
                if not self.server.upload_slot.acquire(blocking=False):
                    raise StationError("Another audio import is in progress. Try again shortly.")
                try:
                    params = parse_qs(urlsplit(self.path).query)
                    value = lambda key, default: params.get(key, [default])[0]
                    result = self.server.station.import_audio(self.read_body(MAX_UPLOAD), value("title", "Untitled"), value("artist", ""), value("kind", "music"))
                finally:
                    self.server.upload_slot.release()
                return self.respond(201, result)
            if path.startswith("/api/x/"):
                return self.extension_route("POST", path)
            body = self.json_body()
            if self.agent_client():
                self.server.note_agent(body.get("operation") if path == "/api/action" else path.removeprefix("/api/"))
            if path == "/api/agent":
                result = self.server.chat.configure(body)
            elif path == "/api/broadcast":
                try:
                    result = self.server.station.broadcast.configure(body)
                except BroadcastError as exc:
                    raise StationError(str(exc)) from None
            elif path == "/api/action":
                result = self.server.station.action(body.get("operation"), body.get("arguments", {}))
            elif path == "/api/chat":
                result = self.server.chat.message(body.get("message"))
            elif path == "/api/approve":
                result = self.server.chat.approve(body.get("id"))
            else:
                return self.respond(404, {"error": "Not found."})
            self.respond(200, result)
        except StationError as exc:
            # Do not reuse sockets with unread/invalid request bodies.
            self.close_connection = True
            self.respond(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.respond(500, {"error": "The station could not complete that request. No retry was performed; inspect status before retrying."})

    def has_session(self):
        session = self.headers.get("X-Myradio-Session", "")
        if not TICKET_FORMAT.fullmatch(session):
            return False
        with self.server.session_lock:
            return any(hmac.compare_digest(session, known) for known in self.server.sessions)

    def new_session(self):
        session = secrets.token_urlsafe(32)
        with self.server.session_lock:
            if len(self.server.sessions) >= 32:
                raise StationError("Too many sessions. Restart the station service to revoke all sessions.")
            self.server.sessions.add(session)
        return self.respond(200, {"ok": True, "session": session}, extra=CLEAR_OLD_COOKIE)

    def agent_client(self):
        return self.headers.get("X-Myradio-Client", "") in ("mcp", "connect")

    def connect_info(self):
        """What the studio's Connect your agent panel needs to write a ready-to-paste MCP setup for this computer. No secrets:
        agents read the private control code from the data folder themselves."""
        station = self.server.station
        return {"python": sys.executable, "package_dir": str(Path(__file__).resolve().parent.parent), "data_dir": str(station.directory),
                "port": self.server.server_address[1], "station": station.status()["station"].get("name", "My radio"),
                "platform": "windows" if WINDOWS else "posix",
                "agent_link": self.server.agent_status()}

    def panel_asset(self, name):
        """Serve a UI panel script/stylesheet only if a running extension declares it."""
        declared = {p["script"] for p in self.server.station.extensions.panels()}
        stem = name.rsplit(".", 1)[0]
        allowed = name in declared or (name.endswith(".css") and stem + ".js" in declared)
        file = WEB / "panels" / name
        if not allowed or file.is_symlink() or not file.is_file():
            return self.respond(404, {"error": "Not found."})
        mime = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
        return self.respond(200, file.read_bytes(), content_type=mime)

    def extension_route(self, method, path):
        station = self.server.station
        match = station.extensions.find_route(method, path)
        if match is None:
            return self.respond(404, {"error": "Not found."})
        route = match[1]
        body, parsed, slot = b"", None, None
        if method == "POST":
            if station.faulted:
                raise StationError(station.error)
            if route.max_body > 1024 * 1024:
                # Large bodies share the single import slot so memory stays bounded.
                if not self.server.upload_slot.acquire(blocking=False):
                    raise StationError("Another large upload is in progress. Try again shortly.")
                slot = self.server.upload_slot
            try:
                body = self.read_body(route.max_body)
            except BaseException:
                if slot:
                    slot.release()
                raise
            try:
                if body and self.headers.get_content_type() == "application/json":
                    try:
                        parsed = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise StationError("Invalid JSON body.") from None
                    if not isinstance(parsed, dict):
                        raise StationError("Send a JSON object.")
            except BaseException:
                if slot:
                    slot.release()
                raise
        try:
            query = parse_qs(urlsplit(self.path).query)
            reply = station.extensions.dispatch_route(match, Request(method, path, query, parsed, body))
        finally:
            if slot:
                slot.release()
        data = reply.body
        if isinstance(data, str):
            data = data.encode()
        return self.respond(reply.status, data, content_type=reply.content_type)

    def stream(self):
        subscriber = self.server.station.subscribe()
        try:
            # Non-seekable live WAV. The monitor intentionally has no rewind or duration.
            header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ", 16, 1, 2, 44100, 176400, 4, 16, b"data", 0xFFFFFFFF)
            self.send_response(200)
            self.headers_base()
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Connection", "close")
            self.sign_reply(200, "stream")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(header)
            self.wfile.flush()
            while not self.server.station.stop_event.is_set():
                try:
                    block = subscriber.get(timeout=5)
                except queue.Empty:
                    break
                if block is None:
                    break
                self.wfile.write(block)
                self.wfile.flush()
        finally:
            self.server.station.unsubscribe(subscriber)


def load_token(directory):
    path = Path(directory) / "control.token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise StationError("Control token must not be a symbolic link.")
        token = path.read_text().strip()
        if len(token) < 32 or not token.isascii() or not all(c.isalnum() or c in '-_' for c in token):
            raise StationError("Invalid control.token. Remove it to regenerate credentials.")
        path.chmod(0o600)
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


def open_dashboard(url, ticket, directory, opener=webbrowser.open):
    """Open the dashboard with a one-time login ticket. The browser is given a private page that redirects to
    `url#ticket=…`, so neither the launch code nor the ticket is on a command line. The page is removed after
    a minute; the ticket expires then anyway."""
    fd, path = tempfile.mkstemp(prefix="open-", suffix=".html", dir=directory)
    with os.fdopen(fd, "w") as page:
        page.write('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>myradio</title>'
                   f'<script>location.replace({json.dumps(url + "#ticket=" + ticket)})</script>\n')
    timer = threading.Timer(TICKET_TTL, lambda: Path(path).unlink(missing_ok=True))
    timer.daemon = True
    timer.start()
    opener(Path(path).as_uri())
    return path


def main(argv=None):
    args_in = sys.argv[1:] if argv is None else list(argv)
    if args_in[:1] == ["setup"]:
        from .installer import main as setup_main
        return setup_main(args_in[1:])
    parser = argparse.ArgumentParser(description="myradio local station and agent dashboard",
                                     epilog="Run `myradio setup` first to download the music model and engine.")
    parser.add_argument("--data-dir", default=os.environ.get("MYRADIO_DATA_DIR", str(Path.home() / ".myradio")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--public-origin", help="Exact HTTPS origin of a trusted private reverse proxy, e.g. https://host.tailnet.ts.net:8443. Service still binds loopback; unlock code still required.")
    args = parser.parse_args(argv)
    if args.public_origin:
        remote = urlsplit(args.public_origin)
        try:
            valid = remote.scheme == "https" and remote.hostname and not remote.username and not remote.password and remote.path == "" and not remote.query and not remote.fragment
            remote.port
        except ValueError:
            valid = False
        if not valid:
            parser.error("--public-origin must be an HTTPS origin without credentials, path, query or fragment")
    station = None
    server = None
    try:
        station = Station(args.data_dir, extensions=builtin_extensions())
        token = load_token(station.directory)
        server = Server(("127.0.0.1", args.port), station, token, args.public_origin)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        print(f"myradio local station: {url}\nLaunch code: {token}\nData: {station.directory}\nCtrl+C stops the service. External broadcasting requires saved Icecast settings and explicit Go live.", flush=True)
        if not args.no_browser:
            open_dashboard(url, server.issue_ticket(), station.directory)
        server.serve_forever(poll_interval=0.2)
    except (OSError, StationError) as exc:
        parser.exit(1, f"myradio: {exc}\n")
    except KeyboardInterrupt:
        print("\nStopping myradio.")
    finally:
        if station:
            station.close()
        if server:
            server.server_close()


if __name__ == "__main__":
    main()
