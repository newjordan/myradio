"""Connect the host script composer to a language model through an OpenAI-compatible chat endpoint.

This lives at the station level, beside chat.py and model_transport.py: the hosts package itself never
touches the network; its extension hands this connector to ``HostsService.set_writer``.

Opt-in only: nothing here runs unless the hosts ``config.json`` has a ``writer`` object, for example::

    "writer": {"base_url": "http://127.0.0.1:8001", "model": "treebeard", "timeout_s": 20}

The model's answer is untrusted text. ``LlmComposer`` (llm.py) checks every answer and falls back to the
built-in templates when the model is slow, unreachable or writes something it may not say, so a
writer can never put an unchecked word on air.

Settings (all optional except ``base_url`` and ``model``):

    base_url          http(s) address of the server, for example http://127.0.0.1:8001
    model             model name the server expects (1 to 120 characters)
    path              chat route, default /v1/chat/completions
    timeout_s         whole-request deadline in seconds, 1 to 120, default 20
    max_tokens        16 to 2048, default 400
    temperature       0 to 2, default 0.8
    disable_thinking  default true: asks reasoning models to answer directly (chat_template_kwargs)
    api_key_file      a file holding a bearer key; the key is never logged or shown
    allow_remote      default false: only a loopback address is accepted unless this is true
"""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from myradio.model_transport import ModelTransport, ModelTransportError

DEFAULTS = {"path": "/v1/chat/completions", "timeout_s": 20.0, "max_tokens": 400, "temperature": 0.8,
            "disable_thinking": True}
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_MODEL = re.compile(r"[A-Za-z0-9._:/@+-]{1,120}")
_PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,200}")
_KNOWN = frozenset(("base_url", "model", "path", "timeout_s", "max_tokens", "temperature", "disable_thinking",
                    "api_key_file", "allow_remote"))
MAX_KEY_BYTES = 4096


class WriterError(Exception):
    """The writer settings cannot be used; the message is safe to show the operator."""


def _number(raw: Mapping, key: str, low: float, high: float, integer: bool = False):
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise WriterError(f"writer.{key} must be a number from {low:g} to {high:g}.")
    if integer and value != int(value):
        raise WriterError(f"writer.{key} must be a whole number.")
    return int(value) if integer else float(value)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_writer(raw: Any) -> Tuple[dict, List[str]]:
    """Check the ``writer`` settings. Returns (settings, notes); raises WriterError when unusable."""
    if not isinstance(raw, Mapping):
        raise WriterError("The writer setting in config.json must be an object.")
    notes = [f"The writer setting {str(key)[:20]!r} is not known and was ignored." for key in raw if key not in _KNOWN]
    base = raw.get("base_url")
    if not isinstance(base, str) or len(base) > 300:
        raise WriterError("writer.base_url must be an http or https address.")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise WriterError("writer.base_url must be a plain http or https address such as http://127.0.0.1:8001.")
    allow_remote = raw.get("allow_remote", False)
    if not isinstance(allow_remote, bool):
        raise WriterError("writer.allow_remote must be true or false.")
    if not allow_remote and not _is_loopback(parts.hostname):
        raise WriterError("writer.base_url must be on this computer (127.0.0.1) unless writer.allow_remote is true.")
    model = raw.get("model")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise WriterError("writer.model must be the model name the server expects.")
    path = raw.get("path", DEFAULTS["path"])
    if not isinstance(path, str) or not _PATH.fullmatch(path) or ".." in path:
        raise WriterError("writer.path must be a route such as /v1/chat/completions.")
    thinking = raw.get("disable_thinking", DEFAULTS["disable_thinking"])
    if not isinstance(thinking, bool):
        raise WriterError("writer.disable_thinking must be true or false.")
    key_file = raw.get("api_key_file")
    if key_file is not None and (not isinstance(key_file, str) or not key_file or len(key_file) > 1024):
        raise WriterError("writer.api_key_file must be the path of a file holding the key.")
    settings = {"url": base.rstrip("/") + path, "model": model,
                "timeout_s": _number(raw, "timeout_s", 1, 120),
                "max_tokens": _number(raw, "max_tokens", 16, 2048, integer=True),
                "temperature": _number(raw, "temperature", 0, 2),
                "disable_thinking": thinking, "api_key_file": key_file}
    return settings, notes


def _read_key(path: str) -> str:
    try:
        data = Path(path).expanduser().read_bytes()[:MAX_KEY_BYTES + 1]
    except OSError:
        raise WriterError("The writer key file cannot be read.") from None
    key = data.decode("utf-8", "replace").strip()
    if not key or len(data) > MAX_KEY_BYTES or any(c in key for c in "\r\n"):
        raise WriterError("The writer key file must hold one key on one line.")
    return key


def answer_text(raw: bytes) -> str:
    """The assistant's words from a chat-completions response, without any reasoning block."""
    try:
        data = json.loads(raw)
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("The writer's answer was not a chat completion.") from None
    if not isinstance(text, str):
        raise ValueError("The writer's answer had no text.")
    return _THINK.sub("", text).strip()


def make_complete(settings: Mapping, transport: Optional[ModelTransport] = None) -> Callable[[str, str], str]:
    """``complete(system, user) -> str`` for ``HostsService.set_writer``. Raises on any failure, which
    the composer turns into a template fallback."""
    transport = transport or ModelTransport()
    key = _read_key(settings["api_key_file"]) if settings.get("api_key_file") else None

    def complete(system: str, user: str) -> str:
        body = {"model": settings["model"], "stream": False, "max_tokens": settings["max_tokens"],
                "temperature": settings["temperature"],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if settings["disable_thinking"]:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        try:
            raw = transport.post(settings["url"], json.dumps(body).encode(), headers, timeout=settings["timeout_s"])
        except ModelTransportError as exc:
            raise ValueError(f"The writer could not be reached ({exc.phase or 'request'}).") from None
        return answer_text(raw)

    return complete
