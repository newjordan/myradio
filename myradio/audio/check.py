"""``selfcheck``: a quick end-to-end test that the audio tools work on this computer.

Meant for the doctor checks and for support: it converts, measures, normalises and round-trips a
short synthetic tone (about a tenth of a second of work) and says in plain words whether the answers
were what they should be.
"""
from __future__ import annotations

import time

from . import synth
from .analysis import analyze, validate_generated
from .edit import fade, normalize
from .rates import to_station_format
from .wav import read_wav, write_wav

__all__ = ["selfcheck"]


def _verify(tone) -> str:
    """Return an empty string when everything is right, otherwise what went wrong."""
    station = to_station_format(tone)
    if (station.rate, station.channels, station.frame_count) != (44100, 2, 22050):
        return "converting a tone to the station format gave the wrong length"
    if read_wav(write_wav(station)) != station:
        return "saving and reloading a tone changed it"
    loudness = analyze(station).loudness_lufs
    if abs(loudness + 20.0) > 0.1:
        return f"a test tone measured {loudness:.2f} loudness units instead of -20"
    if abs(analyze(normalize(station, -16.0, -1.0)).loudness_lufs + 16.0) > 0.1:
        return "levelling a test tone missed its target"
    if validate_generated(fade(station, 5, 100), "song", 0.1, 5.0):
        return "a clean test tone was rejected by the quality check"
    return ""


def selfcheck() -> dict:
    """Run the check. Returns ``{"ok": bool, "message": str, "seconds": float}``; never raises."""
    started = time.perf_counter()
    try:
        problem = _verify(synth.sine(997.0, 0.5, 48000, 1, -20.0))
    except Exception as error:   # a doctor check must report, never raise
        problem = f"an audio tool stopped with an error ({type(error).__name__})"
    seconds = time.perf_counter() - started
    if problem:
        return {"ok": False, "seconds": seconds,
                "message": f"The audio tools are not working correctly on this computer: {problem}. Reinstall myradio, "
                           "and if this keeps happening, ask for help."}
    return {"ok": True, "seconds": seconds, "message": "The audio tools are working."}
