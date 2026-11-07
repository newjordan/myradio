"""``myradio setup``: get a computer ready to make music, in one command.

It finds out what kind of machine this is, downloads the music model and the runtime that plays it (resuming
interrupted downloads and checking every file against its published SHA-256), writes the station's generation
settings, and makes a short test song. Run it again at any time: finished downloads are checked and kept.

Machines:

* ``linux-cuda``: Linux x86-64 with an NVIDIA graphics card. Uses the published CUDA build of audio.cpp and NVIDIA's
  CUDA libraries from PyPI, so only the NVIDIA driver is needed (no CUDA toolkit).
* ``spark``: NVIDIA DGX Spark (Linux aarch64, GB10). Builds audio.cpp from the published source with the CUDA toolkit
  that ships with DGX OS.
* ``mac-metal``: a Mac with Apple silicon. Uses the published Metal build of audio.cpp.

Only the standard library is used. Nothing here needs administrator rights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

HF = "https://huggingface.co/Frosty40/YuE2-Turbo-Radio/resolve/main/"
USER_AGENT = "myradio-setup"


@dataclass(frozen=True)
class Artifact:
    name: str
    url: str
    sha256: str
    size: int
    dest: str  # relative to the setup folder
    executable: bool = False


MODELS = [
    Artifact("YuE2 Turbo Radio music model (Q8_0)", HF + "models/Yue2-3B-GGUF/yue2-3b-q8_0.gguf",
             "52be345b5155bf9ef0b2411e7df8d1d2b8e13b6d977a2f70c35612ec69b1f591", 4264195616, "models/Yue2-3B-GGUF/yue2-3b-q8_0.gguf"),
    Artifact("YuE2 audio decoder (F16 VAE)", HF + "models/Yue2-3B-GGUF/yue2-vae-f16.gguf",
             "d4f4a05d8f291ae820cd1e43609da3fa91b56465810091a2b08c3350b751719d", 265218656, "models/Yue2-3B-GGUF/yue2-vae-f16.gguf"),
]
# The small files the engine reads beside the weights, and the model's licence and notices (they travel together).
MODELS += [Artifact(f"YuE2 {name}", HF + "models/Yue2-3B-GGUF/" + name, sha, size, "models/Yue2-3B-GGUF/" + name) for name, sha, size in [
    ("sidecars/yue2-model-config.json", "ad3477bbef890bf98ae196c1e4b44779494a6231c4ab66f32708eabadf265329", 959),
    ("sidecars/yue2-generation-config.json", "203830cebde6e3644eb291925362990d198e66c3b6006cd11f1aaa21904bcc61", 466),
    ("sidecars/yue2-vae-config.json", "f0191bb9694009956de44e0c361a6f1334760be4c8f848e599bde242a54a0970", 1378),
    ("sidecars/yue2-qwen.tiktoken", "b2b1b8dfb5cc5f024bafc373121c6aba3f66f9a5a0269e243470a1de16a33186", 2561218),
    ("LICENSE.txt", "060985741d20e70613b4c189c7de106cabd3fb2109fbbfb6d705c9d619417dd0", 20309),
    ("original-model-notices/THIRD_PARTY_NOTICES.md", "14d3fd9f6fee86b4260b69b0979735b99ffec8c6b4db254567047a4215cd9af3", 793),
    ("original-model-notices/licenses/SnakeBeta-NVIDIA-MIT.txt", "da9858d516047d82096d01c112a61bd67f26d289039464d668a1d45f91738ecc", 1076),
    ("original-model-notices/licenses/stable-audio-tools-MIT.txt", "a1fac33b7bcd791b74fb33aeb439f825e7277e239fc119fb7d2ab6f084a0c101", 1069),
]]
LINUX_CUDA_BINARY = Artifact("audio.cpp, CUDA build for Linux x86-64", HF + "apollo/runtime/full-song/audiocpp_cli",
                             "54242a0ba914fb8d21883b832d4e28f3efa2c2f1061bab57e3db9d70c16e8f23", 0, "runtime/bin/audiocpp_cli", True)
SOURCE = Artifact("audio.cpp source (YuE2 Turbo Radio)", HF + "apollo/source-full-song.tar.gz",
                  "173bb8de185e6ae45cacd3f892a1e690ad648251aaad677fc2ee419811e0d025", 0, "runtime/source-full-song.tar.gz")
MAC_METAL_BINARY = Artifact("audio.cpp, Metal build for Apple silicon", HF + "mac/runtime/audiocpp_cli",
                            "8e442b2eb39e7d269294e97c11f363423bbae1f492c747992473dc36bdb7000e", 32933360, "runtime/bin/audiocpp_cli", True)
CUDA_WHEELS = ["nvidia-cublas>=13,<14", "nvidia-cuda-runtime>=13,<14", "nvidia-cufft>=12,<13"]

# The measured settings from the published runs (docs/runs/): 8 steps, guidance 1.0, full chain of thought.
YUE2_ENV = {"YUE2_NAR_WINDOW_FRAMES": "1024", "YUE2_NAR_OVERLAP_FRAMES": "128", "YUE2_NAR_PREFIX_CACHE": "1"}
NEEDED_GB = {"linux-cuda": 6.5, "spark": 6.0, "mac-metal": 5.0}


class SetupError(Exception):
    """A problem the person can act on; the message says what to do."""


# ----------------------------------------------------------------------------------------------- reporting

class Reporter:
    def __init__(self, as_json: bool):
        self.as_json = as_json
        self._last = 0.0

    def emit(self, event: str, **fields):
        if self.as_json:
            print(json.dumps({"event": event, **fields}), flush=True)

    def step(self, text: str):
        self.emit("step", text=text)
        if not self.as_json:
            print(f"\n▶ {text}", flush=True)

    def info(self, text: str):
        self.emit("info", text=text)
        if not self.as_json:
            print(f"  {text}", flush=True)

    def progress(self, name: str, done: int, total: int):
        if total and total < 50_000_000:           # small files finish too fast to be worth a bar
            return
        now = time.monotonic()
        if now - self._last < 0.5 and done < total:
            return
        self._last = now
        self.emit("progress", name=name, done=done, total=total)
        if not self.as_json and total:
            pct = done * 100 // total
            if not sys.stdout.isatty():           # a log or a pipe: a line at each quarter, no bar
                quarter = pct // 25
                if quarter != getattr(self, "_quarter", None) or done >= total:
                    self._quarter = quarter
                    print(f"  {pct:3d}%  {done / 1e9:5.2f} / {total / 1e9:.2f} GB", flush=True)
                return
            bar = "█" * (pct // 4) + "·" * (25 - pct // 4)
            end = "\n" if done >= total else ""
            print(f"\r  {bar} {pct:3d}%  {done / 1e9:5.2f} / {total / 1e9:.2f} GB", end=end, flush=True)


# ----------------------------------------------------------------------------------------------- the machine

@dataclass
class Machine:
    kind: str                      # linux-cuda | spark | mac-metal | unsupported
    summary: str
    gpu: str = ""
    memory_gb: float = 0.0
    notes: list = field(default_factory=list)


def _run(cmd, timeout=20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def detect() -> Machine:
    system, arch = platform.system(), platform.machine().lower()
    if system == "Darwin":
        if arch != "arm64":
            return Machine("unsupported", "a Mac with an Intel processor; myradio needs Apple silicon (M1 or newer)")
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple silicon"
        mem = int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30
        return Machine("mac-metal", f"{chip}, {mem:.0f} GB", chip, mem)
    if system != "Linux":
        return Machine("unsupported", f"{system} is not supported yet (Windows support is being tested); myradio runs on macOS and Linux today")
    smi = shutil.which("nvidia-smi")
    if not smi:
        return Machine("unsupported", "Linux without an NVIDIA driver (nvidia-smi not found); install the NVIDIA driver first")
    line = _run([smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"]).splitlines()
    name, mem_mb, driver = ([p.strip() for p in line[0].split(",")] + ["", "", ""])[:3] if line else ("", "0", "")
    if arch in ("aarch64", "arm64"):
        if "GB10" in name or Path("/etc/dgx-release").exists():
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
            return Machine("spark", f"NVIDIA DGX Spark ({name or 'GB10'}), {total:.0f} GB unified memory, driver {driver}", name, total)
        return Machine("unsupported", f"Linux on ARM with {name or 'an unknown GPU'}; only the DGX Spark is supported on ARM")
    if arch not in ("x86_64", "amd64"):
        return Machine("unsupported", f"Linux on {arch} is not supported")
    try:
        mem_gb = float(mem_mb) / 1024
    except ValueError:
        mem_gb = 0.0
    m = Machine("linux-cuda", f"{name}, {mem_gb:.0f} GB, driver {driver}", name, mem_gb)
    if mem_gb and mem_gb < 11.5:
        m.notes.append("This card has under 12 GB of graphics memory; the music model may not fit. The tested card has 16 GB.")
    try:
        if driver and int(driver.split(".")[0]) < 580:
            m.notes.append(f"Driver {driver} may be too old for CUDA 13; update the NVIDIA driver if the test song fails.")
    except ValueError:
        pass
    return m


# ----------------------------------------------------------------------------------------------- downloads

def sha256_of(path: Path, report: Optional[Callable[[int], None]] = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
            if report:
                report(len(chunk))
    return h.hexdigest()


def fetch(art: Artifact, root: Path, rep: Reporter, retries: int = 6) -> Path:
    """Download ``art`` into ``root`` once: resume a partial file, verify the SHA-256, keep a verified file."""
    dest = root / art.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamp = dest.with_name(dest.name + ".verified")
    if dest.exists() and stamp.exists() and stamp.read_text().strip() == art.sha256 and (not art.size or dest.stat().st_size == art.size):
        rep.info(f"{art.name}: already downloaded and verified")
        return dest
    if dest.exists() and art.sha256:
        rep.info(f"{art.name}: checking the file already here")
        if sha256_of(dest) == art.sha256:
            stamp.write_text(art.sha256)
            return dest
        dest.unlink()
    part = dest.with_name(dest.name + ".part")
    attempt = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(art.url, headers={"User-Agent": USER_AGENT, **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if have and resp.status != 206:      # the server ignored the range: start over
                    have = 0
                    part.unlink(missing_ok=True)
                length = resp.headers.get("Content-Length")
                total = have + int(length) if length else art.size
                with open(part, "ab") as out:
                    done = have
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                        done += len(chunk)
                        rep.progress(art.name, done, total or done)
                if total and done < total:          # the server closed early: resume from here
                    raise ConnectionError(f"connection closed at {done} of {total} bytes")
            break
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:              # already complete
                break
            if e.code == 404:
                raise SetupError(f"{art.name} is not published yet at {art.url}.") from None
            attempt += 1
            if attempt > retries:
                raise SetupError(f"Could not download {art.name} ({e}). Check your connection and run `myradio setup` again; "
                                 "it picks up where it stopped.") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            attempt += 1
            if attempt > retries:
                raise SetupError(f"Could not download {art.name} ({e}). Check your connection and run `myradio setup` again; "
                                 "it picks up where it stopped.") from None
            rep.info(f"connection dropped, resuming ({attempt}/{retries})")
            time.sleep(min(30, 2 ** attempt))
    if art.size > 50_000_000:
        rep.info(f"{art.name}: checking the download")
    if art.sha256:
        got = sha256_of(part)
        if got != art.sha256:
            part.unlink(missing_ok=True)
            raise SetupError(f"{art.name} did not match its published checksum (got {got[:12]}…). "
                             "The partial file was removed; run `myradio setup` again.")
    part.replace(dest)
    if art.executable:
        dest.chmod(0o755)
    stamp.write_text(art.sha256)
    return dest


# ----------------------------------------------------------------------------------------------- runtimes

def runtime_linux_cuda(root: Path, rep: Reporter) -> tuple[Path, dict]:
    binary = fetch(LINUX_CUDA_BINARY, root, rep)
    libdir = root / "runtime" / "cuda-libs"
    marker = libdir / ".installed"
    if not marker.exists():
        rep.info("installing NVIDIA's CUDA libraries from PyPI (about 1 GB, no CUDA toolkit needed)")
        cmd = _pip_install_cmd(libdir)
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise SetupError("Could not install the CUDA libraries:\n" + (res.stderr or res.stdout)[-800:])
        marker.write_text(" ".join(CUDA_WHEELS))
    found = sorted({str(p.parent) for p in libdir.rglob("libcudart.so.13")} | {str(p.parent) for p in libdir.rglob("libcublas.so.13")}
                   | {str(p.parent) for p in libdir.rglob("libcufft.so.12")})
    if not found:
        raise SetupError(f"The CUDA libraries installed, but libcudart.so.13 was not found under {libdir}.")
    return binary, {"LD_LIBRARY_PATH": ":".join(found)}


def _pip_install_cmd(target: Path) -> list:
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--quiet", "--python", sys.executable, "--target", str(target), *CUDA_WHEELS]
    return [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", str(target), *CUDA_WHEELS]


def runtime_spark(root: Path, rep: Reporter) -> tuple[Path, dict]:
    built = root / "runtime" / "build" / "cuda" / "bin" / "audiocpp_cli"
    if built.exists():
        rep.info("audio.cpp is already built for this Spark")
        return built, {}
    nvcc = shutil.which("nvcc") or ("/usr/local/cuda/bin/nvcc" if Path("/usr/local/cuda/bin/nvcc").exists() else None)
    missing = [n for n, ok in (("nvcc (CUDA toolkit)", nvcc), ("cmake", shutil.which("cmake")), ("a C++ compiler", shutil.which("c++") or shutil.which("g++")))
               if not ok]
    if missing:
        raise SetupError("Building on the Spark needs " + ", ".join(missing) + ". DGX OS includes them; "
                         "on a fresh install run `sudo apt install cmake build-essential` and make sure /usr/local/cuda/bin is on PATH.")
    tarball = fetch(SOURCE, root, rep)
    src = root / "runtime" / "source"
    if not (src / "CMakeLists.txt").exists():
        rep.info("unpacking the source")
        with tarfile.open(tarball) as tf:
            tf.extractall(root / "runtime", filter="data")
        if not (src / "CMakeLists.txt").exists():
            raise SetupError("The source archive did not contain source/CMakeLists.txt.")
    _placeholder_demo_voices(src / "webui" / "native" / "demo_voices")
    rep.info("building audio.cpp for the GB10 (about 10 minutes, once)")
    cuda_home = str(Path(nvcc).resolve().parent.parent)
    gen = ["-G", "Ninja"] if shutil.which("ninja") else []
    env = {**os.environ, "PATH": f"{cuda_home}/bin:" + os.environ.get("PATH", "")}
    configure = ["cmake", "-S", str(src), "-B", str(root / "runtime" / "build" / "cuda"), *gen, "-DCMAKE_BUILD_TYPE=Release",
                 "-DENGINE_ENABLE_CUDA=ON", "-DENGINE_ENABLE_LLAMAFILE=OFF", "-DCMAKE_CUDA_ARCHITECTURES=121",
                 f"-DCMAKE_CUDA_COMPILER={nvcc}", f"-DCUDAToolkit_ROOT={cuda_home}", "-DAUDIOCPP_DEPLOYMENT_BUILD=ON"]
    jobs = str(max(2, min(8, (os.cpu_count() or 4) // 2)))
    for cmd in (configure, ["cmake", "--build", str(root / "runtime" / "build" / "cuda"), "--target", "audiocpp_cli", "-j", jobs]):
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            log = root / "runtime" / "build.log"
            log.write_text(res.stdout + "\n" + res.stderr)
            raise SetupError(f"The build stopped. The log is at {log}.")
    if not built.exists():
        raise SetupError(f"The build finished but {built} is missing.")
    return built, {}


def _placeholder_demo_voices(folder: Path) -> None:
    """The published source leaves out the web UI's demo voice recordings, which the build embeds. Music needs none of
    them, so a missing one is replaced by half a second of silence."""
    import wave
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("demo_1_man.wav", "demo_2_man.wav", "demo_3_woman.wav", "demo_4_woman.wav"):
        path = folder / name
        if path.exists():
            continue
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 8000)


def runtime_mac(root: Path, rep: Reporter) -> tuple[Path, dict]:
    if not MAC_METAL_BINARY.sha256:
        raise SetupError("The Mac build of audio.cpp is not published yet. It will appear at "
                         f"{MAC_METAL_BINARY.url}; until then build it from source (docs/runs/2026-11-06-mac-mini-hour.md).")
    return fetch(MAC_METAL_BINARY, root, rep), {}


# ----------------------------------------------------------------------------------------------- settings

def worker_config(machine: Machine, binary: Path, model_dir: Path, env: dict, artist: str) -> dict:
    backend = "metal" if machine.kind == "mac-metal" else "cuda"
    command = [str(binary), "--task", "gen", "--family", "yue2", "--model", str(model_dir), "--backend", backend,
               "--threads", "8", "--lyrics", "{lyrics}", "--request-option", "style={tags}", "--request-option", "cot=full",
               "--request-option", "guidance_scale=1.0", "--request-option", "num_inference_steps={steps}",
               "--request-option", "semantic_max_tokens=4500", "--session-option", "yue2.vae_tile_frames=1024",
               "--seed", "{seed}", "--out", "{output_file}"]
    if backend == "cuda":
        command[-4:-4] = ["--session-option", "yue2.packed_projections=true", "--session-option", "yue2.kv_cache_alignment=256"]
    return {
        "artist": artist,
        "resources": {"gpu0": {"max_concurrent": 1}},
        "workers": [{
            "id": "music", "kind": "yue2_cli", "resource": "gpu0", "command": command,
            "env": {**YUE2_ENV, **env}, "timeout_s": 2400, "smoke_test": False,
            "model_files": [str(model_dir / "yue2-3b-q8_0.gguf"), str(model_dir / "yue2-vae-f16.gguf")],
        }],
        "buffer": {"song": {"low": 1800, "high": 3600}},
        "limits": {"max_song_seconds": 240},
    }


def write_config(data_dir: Path, config: dict, rep: Reporter, force: bool) -> Path:
    path = data_dir / "ext" / "generation" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except ValueError:
            current = None
        if current == config:
            rep.info(f"generation settings already up to date: {path}")
            return path
        if not force:
            backup = path.with_name(f"config.{time.strftime('%Y%m%d-%H%M%S')}.json")
            shutil.copy2(path, backup)
            rep.info(f"kept your previous settings as {backup.name}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    rep.info(f"wrote {path}")
    return path


def test_song(config: dict, out: Path, rep: Reporter) -> float:
    worker = config["workers"][0]
    values = {"lyrics": "[verse]\nThe lamp is on, the dial is warm\nYour very first song is being born",
              "tags": "warm lofi, soft female vocal, mellow piano", "steps": "8", "seed": "7", "output_file": str(out)}
    cmd = [part.format(**values) if "{" in part else part for part in worker["command"]]
    cmd = [c.replace("semantic_max_tokens=4500", "semantic_max_tokens=500") for c in cmd]
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update(worker["env"])
    start = time.monotonic()
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
    wall = time.monotonic() - start
    if res.returncode != 0 or not out.exists() or out.stat().st_size < 10000:
        log = out.with_suffix(".log")
        log.write_text(res.stdout + "\n" + res.stderr)
        raise SetupError(f"The test song did not come out (exit {res.returncode}). The log is at {log}.")
    return wall


# ----------------------------------------------------------------------------------------------- main

def setup(data_dir: Path, rep: Reporter, force: bool = False, skip_test: bool = False, artist: str = "myradio") -> dict:
    rep.step("Looking at this computer")
    machine = detect()
    rep.emit("machine", kind=machine.kind, summary=machine.summary)
    rep.info(machine.summary)
    for note in machine.notes:
        rep.info("note: " + note)
    if machine.kind == "unsupported":
        raise SetupError(f"This computer can't make music yet: {machine.summary}.")

    root = data_dir / "setup"
    root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(root).free / 1e9
    have = sum((root / a.dest).stat().st_size for a in MODELS if (root / a.dest).exists()) / 1e9
    if free_gb + have < NEEDED_GB[machine.kind]:
        raise SetupError(f"Setup needs about {NEEDED_GB[machine.kind]:.1f} GB of free disk space in {root}; "
                         f"{free_gb:.1f} GB is free.")

    rep.step("Downloading the music model (4.5 GB, once)")
    for art in MODELS:
        fetch(art, root, rep)

    rep.step("Getting the music engine ready")
    binary, env = {"linux-cuda": runtime_linux_cuda, "spark": runtime_spark, "mac-metal": runtime_mac}[machine.kind](root, rep)

    rep.step("Hooking it up to your station")
    config = worker_config(machine, binary, root / "models" / "Yue2-3B-GGUF", env, artist)
    cfg_path = write_config(data_dir, config, rep, force)

    result = {"machine": machine.kind, "summary": machine.summary, "config": str(cfg_path), "binary": str(binary)}
    if not skip_test:
        rep.step("Making a test song")
        out = root / "test-song.wav"
        out.unlink(missing_ok=True)
        wall = test_song(config, out, rep)
        rep.info(f"made {out} in {wall:.1f} s")
        result.update(test_song=str(out), test_wall_s=round(wall, 1))
    (root / "setup.json").write_text(json.dumps({**result, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n")
    rep.emit("done", **result)
    if not rep.as_json:
        print("\n✓ Your station can make music. Run `myradio` to open your studio.\n", flush=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="myradio setup", description="Download the music model and engine, and make a test song.")
    parser.add_argument("--data-dir", default=os.environ.get("MYRADIO_DATA_DIR", str(Path.home() / ".myradio")))
    parser.add_argument("--json", action="store_true", help="print progress as JSON lines (for the studio)")
    parser.add_argument("--force", action="store_true", help="replace the generation settings without keeping a backup")
    parser.add_argument("--skip-test", action="store_true", help="do not make the test song")
    parser.add_argument("--check", action="store_true", help="only report what kind of machine this is")
    args = parser.parse_args(argv)
    rep = Reporter(args.json)
    if args.check:
        m = detect()
        print(json.dumps({"kind": m.kind, "summary": m.summary, "notes": m.notes}) if args.json else f"{m.kind}: {m.summary}")
        return 0 if m.kind != "unsupported" else 1
    try:
        setup(Path(args.data_dir).expanduser(), rep, force=args.force, skip_test=args.skip_test)
    except SetupError as e:
        rep.emit("error", text=str(e))
        if not args.json:
            print(f"\n✗ {e}\n", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Run `myradio setup` again to pick up where it stopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
