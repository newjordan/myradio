"""Shared fakes for the generation tests. Nothing here is a real model, a real server or real audio.

* ``AudioStandIn``: a minimal stand-in for ``myradio.audio``, installed ONLY when the real module is
  missing (``use_audio()``). Tests must pass against the real module too, so they only rely on the
  frozen contract (names and shapes), never on stand-in details.
* ``FakeSpeechServer``: a loopback HTTP server that can misbehave in every way the engine must survive.
* ``write_script``: fake command line programs (success, hang, ignore TERM, fail, write nothing,
  write a huge file, leave a child behind).
* ``ControlledWorker``: an in-process worker whose behaviour the test steers (length, level, silence in front, abrupt ends,
  clipping, a ready-made result, errors, delays).
* ``measure``: facts about a finished WAV (peak, silence in front, the first and last milliseconds) worked out with the standard
  library only, so a test never trusts the code it is checking to describe its own output.
* ``Harness``: an ExtensionHost with the generation extension and a fake station, in a temp directory.
"""
from __future__ import annotations

import array
import dataclasses
import io
import json
import math
import os
import socketserver
import sys
import tempfile
import textwrap
import threading
import time
import types
import uuid
import wave
from pathlib import Path

from myradio.extensions import ExtensionHost, StationFacade
from myradio.generation.extension import GenerationExtension
from myradio.generation.payloads import ROLES
from myradio.generation.workers_base import ProbeResult, Worker, WorkerOutput
from myradio.generation.errors import Cancelled

RATE = 44100


# ---------------------------------------------------------------------------------------------- audio stand-in
def tone_wav(seconds=1.0, rate=RATE, channels=2, width=2, hz=441, amplitude=0.3, fade_ms=120, silent=False, lead_s=0.0) -> bytes:
    """A WAV written with the standard library only (independent of the code under test).

    ``amplitude`` above 1 is cut off at full scale (a clipped take), ``fade_ms=0`` gives a tone that starts and stops abruptly,
    and ``lead_s`` puts that many seconds of silence in front of the tone (the tone then lasts ``seconds`` after it)."""
    lead = int(rate * lead_s)
    frames = max(1, int(rate * seconds)) + lead
    samples = array.array("h", bytes(2 * frames))
    if not silent:
        edge = max(1, rate * fade_ms // 1000)
        body = frames - lead
        for i in range(body):
            gain = min(1.0, i / edge, (body - 1 - i) / edge) ** 2  # a quadratic fade dies away fully, so the end never sounds cut off
            samples[lead + i] = max(-32768, min(32767, int(amplitude * 32767 * gain * math.sin(2 * math.pi * hz * i / rate))))
    if width == 1:
        raw = bytes(((s >> 8) + 128) & 0xFF for s in samples for _ in range(channels))
    elif width == 2:
        raw = b"".join(s.to_bytes(2, "little", signed=True) * channels for s in samples)
    else:
        raise ValueError("unsupported width")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        out.writeframes(raw)
    return buf.getvalue()


def build_audio_standin() -> types.ModuleType:
    module = types.ModuleType("myradio.audio")

    class AudioError(ValueError):
        pass

    @dataclasses.dataclass
    class Pcm:
        frames: bytes
        rate: int
        channels: int

        @property
        def frame_count(self):
            return len(self.frames) // (2 * self.channels)

        @property
        def seconds(self):
            return self.frame_count / self.rate

    @dataclasses.dataclass
    class Problem:
        code: str
        message: str

    @dataclasses.dataclass
    class Analysis:
        duration_s: float
        peak_dbfs: float
        rms_dbfs: float
        loudness_lufs: float
        silence_ratio: float
        clipped_ratio: float
        dc_offset: float
        leading_silence_s: float
        trailing_silence_s: float

    def samples_of(pcm):
        data = array.array("h")
        data.frombytes(pcm.frames[: len(pcm.frames) // 2 * 2])
        if sys.byteorder == "big":
            data.byteswap()
        return data

    def make(data, rate, channels):
        data = array.array("h", data)
        if sys.byteorder == "big":
            data.byteswap()
        return Pcm(data.tobytes(), rate, channels)

    def read_wav(source, max_bytes=104857600):
        if isinstance(source, (str, os.PathLike)):
            source = Path(source).read_bytes()
        if len(source) > max_bytes:
            raise AudioError("too large")
        try:
            with wave.open(io.BytesIO(source), "rb") as w:
                channels, width, rate, count = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
                raw = w.readframes(count)
        except (wave.Error, EOFError) as exc:
            raise AudioError(str(exc)) from None
        if channels not in (1, 2) or rate <= 0 or len(raw) != count * channels * width or count == 0:
            raise AudioError("bad or truncated wav")
        if width == 1:
            data = array.array("h", [(b - 128) << 8 for b in raw])
        elif width == 2:
            data = array.array("h")
            data.frombytes(raw)
            if sys.byteorder == "big":
                data.byteswap()
        elif width in (3, 4):
            data = array.array("h", [int.from_bytes(raw[i + width - 2:i + width], "little", signed=True) for i in range(0, len(raw), width)])
        else:
            raise AudioError("unsupported width")
        return make(data, rate, channels)

    def write_wav(pcm):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams((pcm.channels, 2, pcm.rate, 0, "NONE", "not compressed"))
            out.writeframes(pcm.frames)
        return buf.getvalue()

    def resample(pcm, rate, quality="good"):
        if rate == pcm.rate:
            return pcm
        ch, data = pcm.channels, samples_of(pcm)
        frames = len(data) // ch
        out_frames = max(1, int(frames * rate / pcm.rate))
        out = array.array("h", bytes(2 * out_frames * ch))
        for i in range(out_frames):
            pos = i * pcm.rate / rate
            lo = min(int(pos), frames - 1)
            hi = min(lo + 1, frames - 1)
            frac = pos - lo
            for c in range(ch):
                out[i * ch + c] = int(data[lo * ch + c] * (1 - frac) + data[hi * ch + c] * frac)
        return make(out, rate, ch)

    def to_station_format(pcm):
        pcm = resample(pcm, 44100)
        if pcm.channels == 1:
            data = samples_of(pcm)
            stereo = array.array("h", bytes(4 * len(data)))
            stereo[0::2] = data
            stereo[1::2] = data
            return make(stereo, 44100, 2)
        return pcm

    def analyze(pcm):
        data = samples_of(pcm)
        peak = max((abs(x) for x in data), default=0)
        rms = math.sqrt(sum(x * x for x in data) / len(data)) if data else 0.0
        db = lambda v: 20 * math.log10(v / 32768) if v > 0 else -120.0
        return Analysis(pcm.seconds, db(peak), db(rms), db(rms) - 0.7, 0.0, 0.0, 0.0, 0.0, 0.0)

    def validate_generated(pcm, role, min_s, max_s):
        problems, data = [], samples_of(pcm)
        peak = max((abs(x) for x in data), default=0)
        if peak < 33:
            return [Problem("silent", "silent")]
        if pcm.seconds < min_s:
            problems.append(Problem("too_short", "too short"))
        if pcm.seconds > max_s:
            problems.append(Problem("too_long", "too long"))
        if data and (data.count(32767) + data.count(-32768)) / len(data) > 0.001:
            problems.append(Problem("clipped", "distorted"))
        tail = data[-max(1, pcm.rate // 200) * pcm.channels:]
        if tail and max(abs(x) for x in tail) > 0.35 * peak:
            problems.append(Problem("truncated_tail", "abrupt end"))
        return problems

    def gain_of(pcm, db):
        factor = 10 ** (db / 20)
        data = samples_of(pcm)
        return make(array.array("h", [max(-32768, min(32767, int(x * factor))) for x in data]), pcm.rate, pcm.channels)

    def normalize(pcm, target_lufs=-16.0, ceiling_dbfs=-1.0):
        info = analyze(pcm)
        if info.peak_dbfs <= -90:
            return pcm
        change = min(target_lufs - info.loudness_lufs, ceiling_dbfs - info.peak_dbfs)
        return gain_of(pcm, change)

    def fade(pcm, in_ms, out_ms):
        ch, data = pcm.channels, samples_of(pcm)
        frames = len(data) // ch
        n_in, n_out = min(frames, pcm.rate * in_ms // 1000), min(frames, pcm.rate * out_ms // 1000)
        out = array.array("h", data)
        for i in range(n_in):
            for c in range(ch):
                out[i * ch + c] = int(out[i * ch + c] * i / n_in)
        for i in range(n_out):
            for c in range(ch):
                j = (frames - 1 - i) * ch + c
                out[j] = int(out[j] * i / n_out)
        return make(out, pcm.rate, ch)

    def trim_silence(pcm, threshold_dbfs=-50.0, keep_ms=60):
        ch, data = pcm.channels, samples_of(pcm)
        limit = 32768 * 10 ** (threshold_dbfs / 20)
        frames = len(data) // ch
        loud = [i for i in range(frames) if any(abs(data[i * ch + c]) > limit for c in range(ch))]
        if not loud:
            return pcm
        keep = pcm.rate * keep_ms // 1000
        lo, hi = max(0, loud[0] - keep), min(frames, loud[-1] + 1 + keep)
        return make(data[lo * ch:hi * ch], pcm.rate, ch)

    def concat(parts, crossfade_ms=0):
        parts = list(parts)
        if not parts or any((p.rate, p.channels) != (parts[0].rate, parts[0].channels) for p in parts):
            raise AudioError("parts differ")
        ch, rate = parts[0].channels, parts[0].rate
        out = array.array("h", samples_of(parts[0]))
        overlap = rate * crossfade_ms // 1000
        for part in parts[1:]:
            nxt = samples_of(part)
            n = min(overlap, len(out) // ch, len(nxt) // ch)
            for i in range(n):
                w = i / max(1, n)
                for c in range(ch):
                    j = len(out) - (n - i) * ch + c
                    out[j] = int(out[j] * (1 - w) + nxt[i * ch + c] * w)
            out.extend(nxt[n * ch:])
        return make(out, rate, ch)

    for name, value in dict(AudioError=AudioError, Pcm=Pcm, Problem=Problem, Analysis=Analysis, read_wav=read_wav, write_wav=write_wav,
                            resample=resample, to_station_format=to_station_format, analyze=analyze, validate_generated=validate_generated,
                            normalize=normalize, fade=fade, trim_silence=trim_silence, concat=concat).items():
        setattr(module, name, value)
    module.IS_STAND_IN = True
    return module


def measure(wav: bytes, quiet_dbfs: float = -50.0) -> dict:
    """Facts about a finished 16-bit WAV, worked out with the standard library only (never with the code under test).

    ``peak`` and ``head_peak`` / ``tail_peak`` (the first and last 2 and 5 milliseconds) are fractions of full scale;
    ``lead_s`` is how long it is quiet before the first sound louder than ``quiet_dbfs``."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        channels, rate, count, raw = w.getnchannels(), w.getframerate(), w.getnframes(), w.readframes(w.getnframes())
    data = array.array("h")
    data.frombytes(raw)
    if sys.byteorder == "big":
        data.byteswap()
    limit = 32768 * 10 ** (quiet_dbfs / 20)
    first = next((i for i in range(count) if any(abs(data[i * channels + c]) > limit for c in range(channels))), count)
    head, tail = rate * 2 // 1000 * channels, rate * 5 // 1000 * channels
    top = lambda part: max((abs(x) for x in part), default=0) / 32768
    return {"rate": rate, "channels": channels, "frames": count, "seconds": count / rate, "peak": top(data), "lead_s": first / rate,
            "head_peak": top(data[:head]), "tail_peak": top(data[-tail:])}


REQUIRED_AUDIO = ("AudioError", "Pcm", "read_wav", "write_wav", "to_station_format", "resample", "analyze", "validate_generated",
                  "normalize", "fade", "trim_silence", "concat")
_audio_users = 0
_audio_installed = False
_audio_saved = None
_audio_lock = threading.Lock()


def _complete(module) -> bool:
    return module is not None and all(hasattr(module, name) for name in REQUIRED_AUDIO)


def use_audio() -> bool:
    """Make ``myradio.audio`` importable with the frozen contract names.

    Returns True when the real module is in use. The stand-in is installed only when the real module is
    missing or does not have the contract names yet (it may still be being built).
    """
    global _audio_users, _audio_installed, _audio_saved
    import importlib
    with _audio_lock:
        _audio_users += 1
        if _audio_installed:
            return False
        existing = sys.modules.get("myradio.audio")
        if os.environ.get("MYRADIO_GENERATION_TEST_STANDIN") == "1":
            existing = None  # run the tests against the stand-in even when the real module exists
        elif not _complete(existing):
            try:
                existing = importlib.import_module("myradio.audio")
            except Exception:  # missing, or still being written
                existing = None
        if _complete(existing) and os.environ.get("MYRADIO_GENERATION_TEST_STANDIN") != "1":
            return True
        import myradio
        _audio_saved = (sys.modules.get("myradio.audio"), getattr(myradio, "audio", None))
        module = build_audio_standin()
        sys.modules["myradio.audio"] = module
        myradio.audio = module
        _audio_installed = True
        return False


def release_audio() -> None:
    global _audio_users, _audio_installed, _audio_saved
    with _audio_lock:
        _audio_users = max(0, _audio_users - 1)
        if _audio_users == 0 and _audio_installed:
            import myradio
            saved_module, saved_attr = _audio_saved or (None, None)
            if saved_module is not None:
                sys.modules["myradio.audio"] = saved_module
            else:
                sys.modules.pop("myradio.audio", None)
            if saved_attr is not None:
                myradio.audio = saved_attr
            elif hasattr(myradio, "audio"):
                del myradio.audio
            _audio_installed, _audio_saved = False, None


# ---------------------------------------------------------------------------------------------- fake speech server
class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        sock = self.request
        sock.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += sock.recv(65536)
            body = rest[:length]
        except (OSError, ValueError):
            return
        server.requests.append({"method": method, "path": path, "headers": headers, "body": body})
        mode = server.mode_for(method, path)
        try:
            getattr(self, "mode_" + mode)(method, path, body)
        except (OSError, ConnectionError):
            pass

    # -- helpers
    def send(self, status, headers, body=b"", reason="OK"):
        head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.request.sendall(head.encode("latin-1") + body)

    def wav_reply(self, wav, ctype="audio/wav"):
        self.send(200, {"Content-Type": ctype, "Content-Length": str(len(wav)), "Connection": "close"}, wav)

    # -- behaviours
    def mode_health(self, method, path, body):
        text = json.dumps({"data": [{"id": "drama-3"}]}).encode()
        self.send(200, {"Content-Type": "application/json", "Content-Length": str(len(text)), "Server": "fake-audiocpp/0.1", "Connection": "close"}, text)

    def mode_ok24(self, method, path, body):
        self.wav_reply(tone_wav(1.2, rate=24000, channels=1))

    def mode_ok24_long(self, method, path, body):
        self.wav_reply(tone_wav(3.0, rate=24000, channels=1))

    def mode_ok44(self, method, path, body):
        self.wav_reply(tone_wav(1.2, rate=44100, channels=2))

    def mode_ok8bit(self, method, path, body):
        self.wav_reply(tone_wav(1.2, rate=22050, channels=1, width=1))

    def mode_silence(self, method, path, body):
        self.wav_reply(tone_wav(1.2, silent=True))

    def mode_short(self, method, path, body):
        self.wav_reply(tone_wav(0.2))

    def mode_wrong_type(self, method, path, body):
        self.wav_reply(tone_wav(1.0), ctype="text/html")

    def mode_garbage(self, method, path, body):
        junk = os.urandom(2048)
        self.send(200, {"Content-Type": "audio/wav", "Content-Length": str(len(junk))}, junk)

    def mode_html(self, method, path, body):
        text = b"<html><body>Not audio</body></html>"
        self.send(200, {"Content-Type": "text/html", "Content-Length": str(len(text))}, text)

    def mode_error500(self, method, path, body):
        text = b"model exploded"
        self.send(500, {"Content-Type": "text/plain", "Content-Length": str(len(text))}, text, "Internal Server Error")

    def mode_busy503(self, method, path, body):
        self.send(503, {"Content-Length": "0"}, b"", "Service Unavailable")

    def mode_forbidden(self, method, path, body):
        self.send(403, {"Content-Length": "0"}, b"", "Forbidden")

    def mode_redirect(self, method, path, body):
        self.send(302, {"Location": "http://127.0.0.1:9/elsewhere", "Content-Length": "0"}, b"", "Found")

    def mode_truncated(self, method, path, body):
        wav = tone_wav(1.2)
        self.send(200, {"Content-Type": "audio/wav", "Content-Length": str(len(wav) + 5000)}, wav[: len(wav) // 2])

    def mode_oversize_header(self, method, path, body):
        self.send(200, {"Content-Type": "audio/wav", "Content-Length": str(900 * 1024 * 1024)}, b"RIFF")
        self.server.stop_event.wait(2)

    def mode_oversize_stream(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nConnection: close\r\n\r\n")
        block = b"\0" * 65536
        end = time.monotonic() + 8
        while time.monotonic() < end and not self.server.stop_event.is_set():
            self.request.sendall(block)

    def mode_drip_headers(self, method, path, body):
        end = time.monotonic() + 8
        for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 400:
            if time.monotonic() > end or self.server.stop_event.is_set():
                return
            self.request.sendall(bytes([byte]))
            time.sleep(0.05)

    def mode_drip_body(self, method, path, body):
        wav = tone_wav(1.2)
        self.request.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: {len(wav)}\r\n\r\n".encode())
        end = time.monotonic() + 8
        for i in range(0, len(wav), 8):
            if time.monotonic() > end or self.server.stop_event.is_set():
                return
            self.request.sendall(wav[i:i + 8])
            time.sleep(0.05)

    def mode_hang(self, method, path, body):
        self.server.stop_event.wait(8)

    def mode_chunked(self, method, path, body):
        wav = tone_wav(1.2)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        for i in range(0, len(wav), 4000):
            piece = wav[i:i + 4000]
            self.request.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        self.request.sendall(b"0\r\n\r\n")

    def mode_bad_chunk(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZZZ\r\nhello\r\n")

    def mode_no_length_ok(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nConnection: close\r\n\r\n" + tone_wav(1.0))

    def mode_not_http(self, method, path, body):
        self.request.sendall(b"\x00\x01 this is not http at all\r\n\r\n")

    def mode_header_flood(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\n")
        end = time.monotonic() + 5
        while time.monotonic() < end and not self.server.stop_event.is_set():
            self.request.sendall(b"X-Pad: " + b"a" * 500 + b"\r\n")

    def mode_dup_length(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 9\r\n\r\nhello")

    def mode_te_and_cl(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")

    def mode_gzip(self, method, path, body):
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Type: audio/wav\r\nContent-Length: 4\r\n\r\nabcd")

    def mode_interim_100(self, method, path, body):
        wav = tone_wav(1.0)
        self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        self.wav_reply(wav)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True


class FakeSpeechServer:
    """Loopback server with a default mode for POST speech requests and a health answer for GET."""

    def __init__(self, mode="ok24"):
        self.mode = mode
        self.modes = []          # optional per-request script: consumed one per speech POST, then falls back to ``mode``
        self.requests = []
        self.stop_event = threading.Event()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.requests = self.requests
        self._server.stop_event = self.stop_event
        self._server.mode_for = self._mode_for
        self.port = self._server.server_address[1]
        self.thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-speech-server", daemon=True)
        self.thread.start()
        self.health = "health"

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def _mode_for(self, method, path):
        if method == "GET":
            return self.health
        if self.modes:
            return self.modes.pop(0)
        return self.mode

    def speech_requests(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def close(self):
        self.stop_event.set()
        self._server.shutdown()
        self._server.server_close()
        self.thread.join(5)


# ---------------------------------------------------------------------------------------------- fake command line programs
SCRIPT_PRELUDE = '''
import array, json, math, os, signal, subprocess, sys, time, wave

def arg(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default

def write_tone(path, seconds=1.5, rate=44100, channels=2, hz=441):
    frames = int(rate * seconds)
    edge = int(rate * 0.12)
    data = array.array("h")
    for i in range(frames):
        gain = min(1.0, i / edge, (frames - 1 - i) / edge) ** 2
        v = int(0.3 * 32767 * gain * math.sin(2 * math.pi * hz * i / rate))
        for _ in range(channels):
            data.append(v)
    with wave.open(path, "wb") as w:
        w.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(data.tobytes())

def note(name, text):
    note_dir = arg("--notes")
    if note_dir:
        with open(os.path.join(note_dir, name), "a") as f:
            f.write(text + "\\n")
'''

SCRIPTS = {
    "ok": '''
out = arg("--out")
note("args.txt", " ".join(sys.argv[1:]))
note("argv.json", json.dumps(sys.argv[1:]))
note("env.txt", repr(sorted(os.environ)))
note("tmpdir.txt", os.environ.get("TMPDIR", ""))
note("cwd.txt", os.getcwd())
lyrics_file = arg("--lyrics-file")
if lyrics_file:
    note("lyrics.txt", open(lyrics_file).read())
print("rendering", file=sys.stderr)
write_tone(out, float(arg("--seconds", "1.5")))
''',
    "hang": '''
note("pid.txt", str(os.getpid()))
time.sleep(600)
''',
    "ignore_term": '''
signal.signal(signal.SIGTERM, signal.SIG_IGN)
note("pid.txt", str(os.getpid()))
while True:
    time.sleep(0.2)
''',
    "fail": '''
print("model could not be loaded: /secret/path", file=sys.stderr)
sys.exit(3)
''',
    "fail_noted": '''
note("runs.txt", "run")
print("error while loading shared libraries: libmodel.so", file=sys.stderr)
sys.exit(127)
''',
    "nothing": '''
print("done", file=sys.stderr)
''',
    "huge": '''
out = arg("--out")
with open(out, "wb") as f:
    f.write(b"RIFF")
    chunk = b"\\0" * (1 << 20)
    for _ in range(400):
        f.write(chunk)
        f.flush()
        time.sleep(0.01)
''',
    "child": '''
child = subprocess.Popen([sys.executable, "-c", "import time, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
note("pid.txt", str(os.getpid()) + " " + str(child.pid))
time.sleep(600)
''',
    "leave_child": '''
out = arg("--out")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], stdout=sys.stdout, stderr=sys.stderr)
note("pid.txt", str(os.getpid()) + " " + str(child.pid))
write_tone(out, 1.5)
''',
    "garbage": '''
with open(arg("--out"), "wb") as f:
    f.write(os.urandom(4096))
''',
    "silent": '''
write_tone(arg("--out"), 1.5, hz=0)
with wave.open(arg("--out"), "wb") as w:
    w.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
    w.writeframes(bytes(44100 * 4))
''',
    "plan": '''
plan = arg("--plan")
out = arg("--out")
if plan and out is None:
    with open(plan, "w") as f:
        f.write("score")
    note("plan_ran.txt", "yes")
else:
    note("main_ran.txt", "with plan=" + str(arg("--use-plan")))
    if arg("--use-plan") is not None and not os.path.exists(arg("--use-plan")):
        sys.exit(9)
    write_tone(out, 1.5)
''',
    "version": '''
note("version_ran.txt", "x")
print("fake-yue2 1.2.3 (build 77)")
''',
    "slow_ok": '''
time.sleep(float(arg("--delay", "0.5")))
write_tone(arg("--out"), 1.5)
''',
}


def write_script(directory, name):
    path = Path(directory) / f"{name}.py"
    path.write_text(SCRIPT_PRELUDE + textwrap.dedent(SCRIPTS[name]))
    return str(path)


def pid_alive(pid: int) -> bool:
    """True when the process exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        pass
    return True


def arm_watchdog(case, seconds=300):
    """If a test ever deadlocks, dump every thread's stack and stop the run instead of hanging for the whole timeout."""
    import faulthandler
    faulthandler.dump_traceback_later(seconds, exit=True)
    case.addCleanup(faulthandler.cancel_dump_traceback_later)


def wait_for(predicate, timeout=15.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------------------------- controllable worker
class Control:
    """Steers every ControlledWorker with the same id."""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.seconds = 3.0
        self.errors = []
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.probe = ProbeResult(True, "ready", "Ready.", evidence="setup_checked")
        self.probes = 0
        self.import_calls = 0
        self.delay = 0.0
        self.silent = False
        self.amplitude = 0.3     # 1.5 gives a clipped take
        self.fade_ms = 120       # 0 gives a take that starts and stops abruptly
        self.lead_s = 0.0        # silence in front of the tone (``seconds`` is the tone itself)
        self.prebuilt = None     # a ready-made Pcm to hand back, so the time to make the tone is not part of what a test measures


CONTROLS: dict = {}


class Tracker:
    """Counts every ControlledWorker call across all workers, to prove serialisation and ordering."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.order = []


GLOBAL = Tracker()


def reset_controls():
    CONTROLS.clear()
    GLOBAL.reset()


class ControlledWorker(Worker):
    kind = "controlled"
    default_roles = ROLES
    allowed_roles = ROLES
    fixture = False

    @classmethod
    def parse_options(cls, raw, where, env):
        return {}

    def control(self):
        return CONTROLS.setdefault(self.id, Control())

    def probe(self, full, cancel):
        c = self.control()
        c.probes += 1
        return c.probe

    def describe(self):
        return {"controlled": True}

    def generate(self, request):
        c = self.control()
        with GLOBAL.lock:
            GLOBAL.active += 1
            GLOBAL.max_active = max(GLOBAL.max_active, GLOBAL.active)
            GLOBAL.order.append((self.id, request.job_id))
        with c.lock:
            c.active += 1
            c.max_active = max(c.max_active, c.active)
            c.calls.append({"job": request.job_id, "role": request.role, "seed": request.seed, "attempt": request.attempt,
                            "thread": threading.current_thread().name, "payload": dict(request.payload), "at": time.monotonic(),
                            "deadline_s": request.deadline_s})
        c.entered.set()
        try:
            while not c.gate.wait(0.02):
                if request.cancel.is_set():
                    raise Cancelled()
            if c.delay:
                end = time.monotonic() + c.delay
                while time.monotonic() < end:
                    if request.cancel.is_set():
                        raise Cancelled()
                    time.sleep(0.01)
            if request.cancel.is_set():
                raise Cancelled()
            if c.errors:
                raise c.errors.pop(0)
            audio = self.ctx.audio()
            if c.prebuilt is not None:
                return WorkerOutput(c.prebuilt, {"controlled": True})
            wav = tone_wav(c.seconds, silent=c.silent, amplitude=c.amplitude, fade_ms=c.fade_ms, lead_s=c.lead_s)
            return WorkerOutput(audio.read_wav(wav), {"controlled": True})
        finally:
            with c.lock:
                c.active -= 1
            with GLOBAL.lock:
                GLOBAL.active -= 1


class ControlledFixture(ControlledWorker):
    kind = "controlled_fixture"
    fixture = True


# ---------------------------------------------------------------------------------------------- fake station and harness
class FakeStation:
    def __init__(self):
        self.imports = []
        self.wavs = {}           # track id -> the WAV bytes that were imported
        self.library = []
        self.fail_next = []
        self.lock = threading.Lock()
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {"library": [dict(t, available=True) for t in self.library]}

    def import_audio(self, data, title, artist="", kind="music"):
        if self.fail_next:
            raise self.fail_next.pop(0)
        with wave.open(io.BytesIO(data), "rb") as w:
            fmt = (w.getframerate(), w.getsampwidth(), w.getnchannels())
            frames = w.getnframes()
        track = {"id": uuid.uuid4().hex, "title": title, "artist": artist, "kind": kind, "duration": frames / fmt[0]}
        with self.lock:
            self.imports.append({**track, "format": fmt, "bytes": len(data)})
            self.wavs[track["id"]] = data
            self.library.append(track)
        return dict(track)

    def action(self, operation, arguments=None):
        return {"ok": True, "operation": operation}

    def delete_track(self, track_id):
        with self.lock:
            self.library = [t for t in self.library if t["id"] != track_id]


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


def worker_cfg(wid="w1", roles=None, resource="gpu0", **extra):
    entry = {"id": wid, "kind": "controlled", "resource": resource}
    if roles is not None:
        entry["roles"] = roles
    entry.update(extra)
    return entry


class Harness:
    """The generation extension inside a real ExtensionHost, with a fake station, in a temp directory."""

    def __init__(self, config=None, *, kinds=None, clock=None, station=None, probe_min_interval=0.0, root=None):
        self.temp = tempfile.TemporaryDirectory() if root is None else None
        self.root = Path(self.temp.name) if root is None else Path(root)
        self.state_dir = self.root / "ext" / "generation"
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if config is not None:
            (self.state_dir / "config.json").write_text(json.dumps(config))
            os.chmod(self.state_dir / "config.json", 0o600)
        self.station = station or FakeStation()
        self.clock = clock
        self.ext = GenerationExtension(kinds={"controlled": ControlledWorker, "controlled_fixture": ControlledFixture, **(kinds or {})},
                                       clock=clock, probe_min_interval=probe_min_interval)
        self.host = ExtensionHost(self.root, StationFacade(self.station))
        self.host.register(self.ext)
        self.host.start()
        self.closed = False

    @property
    def engine(self):
        return self.ext._engine

    def call(self, op, args=None):
        return self.host.call("generation." + op, args or {})

    def route(self, method, path, query=None, body=None):
        from myradio.extensions import Request
        match = self.host.find_route(method, "/api/x/generation" + path)
        assert match is not None, path
        return self.host.dispatch_route(match, Request(method, path, query or {}, body, b""))

    def status(self):
        return self.host.status()["generation"]["status"]

    def job(self, job_id):
        return self.call("job", {"job_id": job_id})["job"]

    def wait_state(self, job_id, *states, timeout=15.0):
        wait_for(lambda: self.job(job_id)["state"] in states, timeout)
        return self.job(job_id)

    DEFAULT_PAYLOADS = {
        "song": {"tags": "ambient pads and soft rain, slow, 70 BPM", "duration_s": 5},
        "host_break": {"text": "Hello there everyone, welcome back."},
        "id": {"text": "You are listening to Radio Four."},
        "spot": {"text": "This message comes from Maple Bakery."},
        "update": {"text": "Here is a short update from the garden."},
    }

    def enqueue(self, role="host_break", payload=None, **kw):
        args = {"role": role, "payload": {**self.DEFAULT_PAYLOADS[role], **(payload or {})}}
        args.update(kw)
        return self.call("enqueue", args)["job"]

    def emit(self, kind, track_id, at=None):
        self.host.emit({"type": kind, "at": at or time.time(), "track": {"id": track_id, "title": "t", "duration": 3.0, "kind": "speech"}})

    def stop(self):
        """Stop the extension cleanly but keep the directory (to start another harness on it)."""
        if not self.closed:
            self.closed = True
            self.host.stop()

    def close(self):
        self.stop()
        if self.temp is not None:
            self.temp.cleanup()
