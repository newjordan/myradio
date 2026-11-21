"""Any audio.cpp voice through ``POST /v1/audio/speech``: Qwen3-TTS, Chatterbox, Kokoro, VoxCPM2 and the rest.

It works like the Drama worker (takes split at sentence ends, a warm-up line, run-on takes refused and retried) and adds
the settings other voices need:

    voice              a built-in voice id (Qwen3-TTS CustomVoice "vivian", Kokoro "am_puck") or a reference WAV path on
                       the server for voices that clone
    reference_text     what the reference WAV says, for voices that clone from it
    request_options    model options as an object, for example {"instruct": "Warm, quick news anchor"} for Qwen3-TTS
                       or {"exaggeration": 0.9} for Chatterbox
    speaker_markers    keep <|speaker:N|> markers in the text (only Drama-style models understand them; default false,
                       which removes them)
    speaker_voices     a voice per speaker slot, for example {"3": "Thaddeus_en-announcervoice_M_1", "7": "Wyatt_en-American_M_1"},
                       so each host keeps their own voice on one server; a slot not listed uses ``voice``
    fade_out_ms        ask the server to fade each take out over this many milliseconds (ZONOS3 ``fade_out_ms``), for
                       voices whose last frame can end loud enough to fail the abrupt-ending check; leave out to send nothing
    tokens_per_second  the voice's audio tokens per second, used to send max_tokens (leave out to let the model decide)

Example (Jessica, the Knight Shift Radio news voice, on Qwen3-TTS CustomVoice)::

    {"id": "news", "kind": "speech_http", "roles": ["update", "id"], "base_url": "http://127.0.0.1:8090",
     "model": "qwen3-customvoice", "voice": "vivian",
     "request_options": {"instruct": "A bright, warm young American woman reading a short radio news desk."}}
"""
from __future__ import annotations

import math
import re
from typing import Mapping

from .config import ConfigError, Env, get_bool, get_float, get_int, get_str
from .worker_drama import DramaHttpWorker

SPEAKER_MARK = re.compile(r"<\|speaker:\d+\|>\s*")
LEADING_SPEAKER = re.compile(r"\s*<\|speaker:(\d+)\|>")
OPTION_KEY = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.")


class SpeechHttpWorker(DramaHttpWorker):
    kind = "speech_http"

    @classmethod
    def parse_options(cls, raw: Mapping, where: str, env: Env) -> dict:
        options = super().parse_options(raw, where, env)
        extra = raw.get("request_options", {})
        if not isinstance(extra, dict) or len(extra) > 32:
            raise ConfigError(f"{where}: request_options must be an object of up to 32 settings.")
        clean = {}
        for key, value in extra.items():
            if not isinstance(key, str) or not key or len(key) > 60 or set(key) - OPTION_KEY:
                raise ConfigError(f"{where}: request option names use lower-case letters, digits, dots and underscores.")
            if isinstance(value, bool) or (isinstance(value, (int, float)) and math.isfinite(value)):
                clean[key] = value
            elif isinstance(value, str) and len(value) <= 2000:
                clean[key] = value
            else:
                raise ConfigError(f"{where}: request option {key} must be a short text, a number or true/false.")
        tps = raw.get("tokens_per_second")
        if tps is not None:
            tps = get_float(raw, "tokens_per_second", 0.0, 1.0, 200.0, where)
        if "warmup_text" not in raw:
            options["warmup_text"] = "Ready."
        voices = raw.get("speaker_voices", {})
        if not isinstance(voices, dict) or len(voices) > 32:
            raise ConfigError(f"{where}: speaker_voices must be an object of up to 32 speaker slots.")
        speaker_voices = {}
        for slot, name in voices.items():
            if not (isinstance(slot, str) and slot.isdigit() and 0 <= int(slot) <= 31):
                raise ConfigError(f"{where}: speaker_voices keys are speaker slots from \"0\" to \"31\".")
            if not isinstance(name, str) or not name.strip() or len(name) > 500:
                raise ConfigError(f"{where}: speaker_voices.{slot} must name a voice.")
            speaker_voices[int(slot)] = name.strip()
        fade = raw.get("fade_out_ms")
        if fade is not None:
            fade = get_int(raw, "fade_out_ms", 0, 0, 1000, where)
        options.update({
            "fade_out_ms": fade,
            "speaker_voices": speaker_voices,
            "voice": get_str(raw, "voice", None, where, max_len=500),
            "reference_text": get_str(raw, "reference_text", None, where, max_len=2000),
            "request_options": clean,
            "speaker_markers": get_bool(raw, "speaker_markers", False, where),
            "tokens_per_second": tps,
        })
        if options["model"] == "drama-3" and "model" not in raw:
            raise ConfigError(f"{where}: model must name the voice model on the server, for example \"qwen3-customvoice\".")
        return options

    def describe(self) -> dict:
        info = super().describe()
        info.update({"voice": self.options["voice"], "options": sorted(self.options["request_options"]),
                     "speaker_voices": len(self.options["speaker_voices"])})
        return info

    def _speech_body(self, text: str, seed: int, max_seconds: float) -> dict:
        o = self.options
        lead = LEADING_SPEAKER.match(text)
        voice = o["speaker_voices"].get(int(lead.group(1)), o["voice"]) if lead else o["voice"]
        if not o["speaker_markers"]:
            text = SPEAKER_MARK.sub("", text).strip()
        body = {"model": o["model"], "input": text, "response_format": "wav", "seed": int(seed)}
        if o["tokens_per_second"]:
            body["max_tokens"] = int(math.ceil(max_seconds * o["tokens_per_second"]))
        if voice:
            body["voice"] = voice
        if o["reference_text"]:
            body["reference_text"] = o["reference_text"]
        if o["fade_out_ms"]:
            body["fade_out_ms"] = o["fade_out_ms"]
        if o["request_options"]:
            body["options"] = dict(o["request_options"])
        body.update(o["generation_options"])
        return body
