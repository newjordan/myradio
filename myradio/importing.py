"""Bounded local audio decoding. No URLs, playlists, shells or remote protocols."""
from __future__ import annotations

import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time

from ._platform import O_BINARY, WINDOWS, PipeReader


class ImportError(ValueError):
    pass


def decode_audio(data, directory, *, limit=100 * 1024 * 1024, timeout=90):
    encoder = shutil.which('ffmpeg')
    if not encoder:
        raise ImportError('This format needs FFmpeg. Install FFmpeg in PATH and retry, or import a 44.1 kHz, 16-bit PCM WAV without it.')
    # A seekable input handles M4A files with their index at the end. The random
    # private directory also prevents filenames or metadata from becoming args.
    with tempfile.TemporaryDirectory(prefix='import-', dir=directory) as staging:
        source = Path(staging) / 'source.audio'
        fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY | O_BINARY, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        command = [encoder, '-hide_banner', '-loglevel', 'error', '-nostdin',
                   '-xerror', '-max_alloc', '67108864', '-threads', '1',
                   '-protocol_whitelist', 'file,pipe',
                   '-format_whitelist', 'wav,mp3,flac,ogg,mov,aac,aiff',
                   '-probesize', '5242880', '-analyzeduration', '5000000',
                   '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
                   '-map_metadata', '-1', '-threads', '1', '-ac', '2', '-ar', '44100',
                   '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise ImportError('FFmpeg could not start. Check its installation and retry.') from None
        pcm = bytearray()
        deadline = time.monotonic() + timeout
        reader = None
        try:
            if WINDOWS:
                reader = PipeReader(process.stdout)  # select() cannot watch pipes on Windows
            else:
                os.set_blocking(process.stdout.fileno(), False)
            while True:
                if time.monotonic() >= deadline:
                    raise ImportError('Audio conversion exceeded 90 seconds. Try a shorter file or convert it to PCM WAV first.')
                if reader:
                    block = reader.read(.1)
                    if block is None:
                        continue
                else:
                    readable, _, _ = select.select([process.stdout], [], [], .1)
                    if not readable:
                        continue
                    block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    break
                if len(pcm) + len(block) > limit:
                    raise ImportError('Decoded audio exceeds the normalized 100 MiB limit (about 9 minutes 54 seconds). Split the audio into shorter tracks; nothing was imported.')
                pcm.extend(block)
            try:
                result = process.wait(timeout=max(.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                raise ImportError('Audio conversion timed out. Try a shorter file.') from None
            if result != 0 or len(pcm) < 17640 or len(pcm) % 4:
                raise ImportError('Audio could not be decoded completely. Use an intact WAV, MP3, FLAC, Ogg, AAC/M4A or AIFF file with at least 100 ms of audio; playlists and URLs are not supported.')
            return pcm
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if reader:
                reader.close()  # the encoder is gone, so its pipe has ended; wait for the reader to notice
            process.stdout.close()
