"""myradio.audio: a pure-Python PCM toolkit to validate, convert and polish audio.

Python standard library only (no ``audioop``, which Python 3.13 removed). All functions take and
return :class:`Pcm`, an immutable block of interleaved signed 16-bit little-endian samples, and raise
:class:`AudioError` (a ``ValueError``) with a plain-language message when something cannot be done.

The frozen contract used by the generation pipeline:

* ``read_wav(source, max_bytes=104857600) -> Pcm`` and ``write_wav(pcm) -> bytes``
* ``to_station_format(pcm)`` (44.1 kHz stereo) and ``resample(pcm, rate, quality="good")``
* ``analyze(pcm) -> Analysis`` and ``validate_generated(pcm, role, min_s, max_s) -> [Problem]``
* ``normalize(pcm, target_lufs=-16.0, ceiling_dbfs=-1.0)``, ``fade(pcm, in_ms, out_ms)``,
  ``trim_silence(pcm, threshold_dbfs=-50.0, keep_ms=60)`` and ``concat(parts, crossfade_ms=0)``

Details, limits, timings and test results are in docs/lanes/audio.md.
"""
from .analysis import PROBLEM_CODES, ROLES, Analysis, Problem, analyze, validate_generated
from .check import selfcheck
from .edit import (MAX_BOOST_DB, NormalizeReport, apply_gain, concat, fade, normalize, normalize_with_report,
                   trim_silence)
from .loudness import loudness_lufs
from .pcm import (FLOOR_DB, MAX_PCM_BYTES, MAX_RATE, MIN_RATE, STATION_CHANNELS, STATION_RATE, AudioError, Pcm)
from .rates import QUALITIES, resample, to_station_format
from .wav import MAX_WAV_BYTES, read_wav, write_wav, write_wav_file

__all__ = [
    # frozen contract
    "AudioError", "Pcm", "Analysis", "Problem", "read_wav", "write_wav", "to_station_format", "resample",
    "analyze", "validate_generated", "normalize", "fade", "trim_silence", "concat",
    # additions
    "write_wav_file", "apply_gain", "normalize_with_report", "NormalizeReport", "loudness_lufs", "selfcheck",
    "PROBLEM_CODES", "ROLES", "QUALITIES", "STATION_RATE", "STATION_CHANNELS", "MIN_RATE", "MAX_RATE",
    "MAX_PCM_BYTES", "MAX_WAV_BYTES", "MAX_BOOST_DB", "FLOOR_DB",
]
