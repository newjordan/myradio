"""Error types for the generate-ahead engine.

Every message that can reach a person (job errors, status text, operation failures) is written
for a nontechnical reader: what happened, what it means, what to do next. Technical detail is
kept apart, in a short ``detail`` string that a screen shows only behind a disclosure.
"""
from __future__ import annotations

from ..extensions import ExtensionError


class GenerationError(ExtensionError):
    """A safe, presentable failure of an operation or route (shown to operators and agents)."""


class ConfigError(ValueError):
    """The operator-written settings have a problem. The message says which setting and how to fix it."""


class WorkerError(Exception):
    """A worker (or runner) could not make a usable result.

    ``code`` is a short stable identifier, ``message`` is plain language, ``retryable`` says whether
    another attempt could plausibly work, and ``detail`` is a short technical note.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail[:1500]


class Cancelled(Exception):
    """The job was cancelled (or put back in line) while a worker was running it."""


# Stable error codes and their plain-language wording. Workers may add context in ``detail``.
MESSAGES = {
    "worker_missing": "No maker is set up for this kind of audio yet. Add one in the generation settings.",
    "worker_unreachable": "The maker did not answer. Check that it is running, then try again.",
    "worker_timeout": "The maker took too long and was stopped.",
    "worker_failed": "The maker reported a problem and did not finish.",
    "worker_blocked": "This maker is only for tests and is switched off.",
    "bad_output": "The maker sent back something that is not a usable sound file.",
    "no_output": "The maker finished but did not produce any audio.",
    "output_too_large": "The maker produced far more data than expected and was stopped.",
    "silent_output": "The result was silent, so it was thrown away.",
    "audio_too_short": "The result was too short to use, so it was thrown away.",
    "audio_too_long": "The result ran far longer than asked for, so it was thrown away.",
    "audio_cut_off": "The result stopped abruptly, so it was thrown away.",
    "audio_unusable": "The result could not be turned into station audio, so it was thrown away.",
    "audio_tools_missing": "The audio tools are missing from this install, so nothing can be finished.",
    "low_disk": "There is not enough free disk space to make audio. Free up some space.",
    "import_failed": "The audio was made but the station could not add it to the library.",
    "expired": "This was not made in time, so it was dropped.",
    "cancelled": "This was cancelled.",
    "interrupted": "The station stopped while this was being made.",
    "internal": "Something unexpected went wrong while making this.",
}


def message_for(code: str) -> str:
    return MESSAGES.get(code, MESSAGES["internal"])
