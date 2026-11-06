"""Bounded PCM -> supervised FFmpeg MP3 -> Icecast PUT source.

Only the encoder runs in a subprocess. Credentials and destination stay in this
process, never in argv, environment, station snapshots, or encoder diagnostics.
Network work never runs on the station clock. Restart always requires Go live.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit


class BroadcastError(ValueError):
    pass


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {"url", "username", "password"}:
        raise BroadcastError("Provide the Icecast mount URL, source username and password.")
    url, username, password = (value[k] for k in ("url", "username", "password"))
    if not all(isinstance(v, str) for v in (url, username, password)):
        raise BroadcastError("Broadcast settings must be text.")
    if len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise BroadcastError("Use an ASCII HTTP(S) mount URL without spaces.")
    try:
        p = urlsplit(url)
        port = p.port
        if not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment:
            raise ValueError()
        if p.scheme not in ("http", "https") or not re.fullmatch(r"/[A-Za-z0-9_./~-]+", p.path) or p.path.startswith("//"):
            raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise BroadcastError("Use an HTTP(S) mount URL such as https://radio.example.org:8443/live.mp3, without credentials or query strings.") from None
    try:
        loopback = ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:
        loopback = p.hostname == "localhost"
    if p.scheme != "https" and not loopback:
        raise BroadcastError("Remote broadcast destinations require HTTPS to protect your source password. Plain HTTP is allowed only for loopback testing.")
    if not username or len(username) > 200 or ":" in username or any(ord(c) < 32 or ord(c) > 126 for c in username):
        raise BroadcastError("Use a printable ASCII source username without a colon (usually source).")
    if not password or len(password) > 1000 or any(ord(c) < 32 or ord(c) > 126 for c in password):
        raise BroadcastError("Use a nonempty printable ASCII source password (up to 1,000 characters).")
    return {"url": url, "username": username, "password": password}


class Broadcast:
    def __init__(self, directory):
        self.path = Path(directory) / "broadcast.json"
        self.lock = threading.RLock()
        self.config = None
        self.encoder = shutil.which("ffmpeg")
        self.thread = None
        self.stop_event = threading.Event()
        self.blocks = queue.Queue(maxsize=30)  # three seconds, then abandon stale audio
        self.overrun = threading.Event()
        self.state = "disconnected"
        self.error = None
        self.sent = 0
        self.connections = 0
        self.retries = 0
        self.last_sent = None
        self.retry_at = None
        self.active = False
        self.configuring = False
        self.sock = None
        self.resolution = None
        try:
            if self.path.is_symlink():
                raise BroadcastError("Broadcast settings must not be a symbolic link.")
            if self.path.exists():
                self.config = validate_config(json.loads(self.path.read_text()))
                self.path.chmod(0o600)
        except (ValueError, OSError):
            self.error = "Saved broadcast settings could not be loaded. Save them again before going live."

    def status(self):
        with self.lock:
            # Deliberately no URL/username/password: snapshots also go to agents.
            return {"configured": self.config is not None, "encoder_available": bool(self.encoder),
                    "state": self.state, "error": self.error, "active": self.active,
                    "format": "MP3 · 128 kbit/s · 44.1 kHz stereo", "bytes_sent": self.sent,
                    "connections": self.connections, "retries": self.retries,
                    "last_sent": self.last_sent, "retry_at": self.retry_at}

    def settings_summary(self):
        # Separate authenticated dashboard route; not an agent snapshot or tool.
        with self.lock:
            return {"url": self.config["url"], "username": self.config["username"]} if self.config else {}

    def configure(self, value):
        config = validate_config(value)
        with self.lock:
            if self.configuring or (self.thread and self.thread.is_alive()):
                raise BroadcastError("Disconnect the broadcast and wait for it to stop before changing settings.")
            self.configuring = True
        # Never hold the status/clock lock during disk I/O.
        staged = self.path.with_name("broadcast-" + os.urandom(8).hex() + ".tmp")
        try:
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(config, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(staged, self.path)
            with self.lock:
                self.config = config
                self.error = None
            return self.status()
        finally:
            try:
                staged.unlink(missing_ok=True)
            finally:
                with self.lock:
                    self.configuring = False

    def start(self):
        with self.lock:
            if self.configuring or (self.thread and self.thread.is_alive()):
                raise BroadcastError("Broadcast is already active or stopping. Check its status before retrying.")
            if not self.config:
                raise BroadcastError("Save an Icecast destination and source credentials in the dashboard first.")
            if not self.encoder:
                raise BroadcastError("FFmpeg is not installed. Install an FFmpeg build with libmp3lame, then restart myradio.")
            self.blocks = queue.Queue(maxsize=30)
            self.overrun.clear()
            self.stop_event.clear()
            self.sent = self.connections = self.retries = 0
            self.last_sent = self.retry_at = self.error = None
            self.active = True
            self.state = "connecting"
            self.thread = threading.Thread(target=self._run, name="myradio-broadcast", daemon=True)
            self.thread.start()
            return self.status()

    def stop(self):
        # Do not join here: transport controls must never wait for the network.
        with self.lock:
            self.active = False
            self.stop_event.set()
            self._interrupt_socket()
            if self.thread and self.thread.is_alive():
                self.state = "stopping"
            else:
                self.state = "disconnected"
            self.retry_at = None
            return self.status()

    def offer(self, block):
        # Called by the clock: bounded, no disk/network/subprocess work or waits.
        if self.active:
            try:
                self.blocks.put_nowait(block)
            except queue.Full:
                self.overrun.set()

    def close(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=8)

    def _interrupt_socket(self):
        # Shutdown wakes reads/writes immediately, including a peer drip-feeding
        # HTTP headers. Socket timeout alone is only an inactivity timeout.
        sock = self.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _resolve(self, host, port):
        # A libc resolver can ignore socket timeouts. At most one outstanding
        # lookup per Broadcast; wait is cancellable and has an absolute deadline.
        if self.resolution and not self.resolution[0].is_set() and self.resolution[2] != (host, port):
            raise OSError("previous DNS lookup still pending")
        if self.resolution is None or self.resolution[0].is_set():
            done, result = threading.Event(), []
            self.resolution = (done, result, (host, port))
            def resolve():
                try:
                    result.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
                except OSError:
                    pass
                finally:
                    done.set()
            threading.Thread(target=resolve, name="myradio-broadcast-dns", daemon=True).start()
        done, result, _ = self.resolution
        deadline = time.monotonic() + 3
        while not done.wait(0.05):
            if self.stop_event.is_set() or time.monotonic() > deadline:
                raise OSError("DNS interrupted or timed out")
        if not result:
            raise OSError("DNS failed")
        return result

    def _connect(self):
        p = urlsplit(self.config["url"])
        port = p.port or (443 if p.scheme == "https" else 80)
        addresses = self._resolve(p.hostname, port)
        connection = None
        # Absolute deadline for connect, TLS and the complete response headers.
        expired = threading.Event()
        def expire():
            expired.set()
            self._interrupt_socket()
        timer = threading.Timer(5, expire)
        timer.daemon = True
        timer.start()
        try:
            for family, kind, protocol, _, address in addresses[:4]:
                if self.stop_event.is_set() or expired.is_set():
                    raise OSError("connection interrupted")
                sock = socket.socket(family, kind, protocol)
                with self.lock:
                    self.sock = sock
                sock.settimeout(2)
                try:
                    sock.connect(address)
                    break
                except OSError:
                    sock.close()
            else:
                raise OSError("connection failed")
            if self.stop_event.is_set() or expired.is_set():
                raise OSError("connection interrupted")
            if p.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname, do_handshake_on_connect=False)
                with self.lock:
                    self.sock = sock
                sock.do_handshake()
            connection = http.client.HTTPConnection(p.hostname, port, timeout=2)
            connection.sock = sock
            connection.putrequest("PUT", p.path)
            auth = base64.b64encode((self.config["username"] + ":" + self.config["password"]).encode()).decode()
            connection.putheader("Authorization", "Basic " + auth)
            connection.putheader("Content-Type", "audio/mpeg")
            connection.putheader("Ice-Public", "0")
            connection.putheader("Ice-Name", "myradio")
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = http.client.HTTPResponse(sock)
            response.begin()
            if response.status != 200:
                code = response.status
                response.close()
                if code in (401, 403):
                    raise BroadcastError("Source authentication refused. Disconnect, check credentials and mount permissions, then try again.")
                raise BroadcastError("Icecast refused the source connection. Check the mount and server limits, then try again.")
            if self.stop_event.is_set() or expired.is_set():
                response.close()
                raise OSError("connection interrupted")
            return connection, response, sock
        except Exception:
            if connection:
                connection.close()
            with self.lock:
                if self.sock:
                    self.sock.close()
                    self.sock = None
            raise
        finally:
            timer.cancel()
            timer.join()  # no late timer may interrupt the following attempt

    def _attempt(self):
        connection = response = process = None
        try:
            connection, response, sock = self._connect()
            if self.stop_event.is_set():
                return
            with self.lock:
                self.connections += 1
                self.state = "encoding"
            # No destination, credential, shell, or remote protocol in encoder argv.
            process = subprocess.Popen([self.encoder, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
                "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "128k", "-threads", "1",
                "-f", "mp3", "-flush_packets", "1", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                close_fds=True)
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            pending = b""
            last_progress = time.monotonic()
            while not self.stop_event.is_set():
                if self.overrun.is_set():
                    raise OSError("backpressure")
                if process.poll() is not None:
                    raise BroadcastError("MP3 encoder stopped. Check that your FFmpeg build includes libmp3lame. Disconnect and restart after fixing it.")
                if time.monotonic() - last_progress > 10:
                    raise OSError("encoder stalled")
                if not pending:
                    try:
                        pending = self.blocks.get_nowait()
                    except queue.Empty:
                        pass
                readable, writable, _ = select.select([process.stdout], [process.stdin] if pending else [], [], 0.1)
                if writable:
                    try:
                        n = os.write(process.stdin.fileno(), pending)
                        pending = pending[n:]
                    except BlockingIOError:
                        pass
                if readable:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        raise OSError("encoder closed")
                    sock.sendall(data)
                    last_progress = time.monotonic()
                    with self.lock:
                        self.sent += len(data)
                        self.last_sent = time.time()
                        self.state = "streaming"
                        self.error = None
        finally:
            if process:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                process.stdin.close()
                process.stdout.close()
            if response:
                response.close()
            if connection:
                connection.close()
            with self.lock:
                self.sock = None

    def _run(self):
        try:
            while not self.stop_event.is_set():
                # Do not replay buffered audio after a disconnect.
                while True:
                    try:
                        self.blocks.get_nowait()
                    except queue.Empty:
                        break
                self.overrun.clear()
                try:
                    self._attempt()
                except BroadcastError as exc:
                    with self.lock:
                        self.error = str(exc)
                        self.state = "failed"
                    return  # Invalid auth/config/encoder: operator intervention, not a retry storm.
                except (OSError, http.client.HTTPException, ssl.SSLError):
                    if self.stop_event.is_set():
                        break
                    with self.lock:
                        self.retries += 1
                        delay = min(30, 2 ** min(self.retries, 5))
                        self.state = "retrying"
                        self.error = "Output interrupted. Check Icecast availability, TLS certificate, network and encoder. Local playout continues; reconnecting with fresh audio."
                        self.retry_at = time.time() + delay
                    if self.stop_event.wait(delay):
                        break
                    with self.lock:
                        self.state = "connecting"
                        self.retry_at = None
                except Exception:
                    with self.lock:
                        self.state = "failed"
                        self.error = "Broadcast worker failed. Disconnect and check the encoder and saved settings before trying again."
                    return
        finally:
            with self.lock:
                self.active = False
                self.retry_at = None
                if self.stop_event.is_set():
                    self.state = "disconnected"
