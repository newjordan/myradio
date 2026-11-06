"""Where the station may and may not connect.

Three layers, each enforced independently:

1. ``parse_target`` accepts only well-formed ``https`` addresses on the standard port, with a real public-style
   host name (or a canonical IP literal). It rejects user names and passwords, odd IPv4 spellings such as
   ``127.1`` or ``2130706433`` (some resolvers read them as loopback), single-label and internal-looking names.
2. ``check_addresses`` classifies every address a name resolves to. If any one of them is loopback, private,
   link-local, multicast, reserved, a cloud metadata address, or an IPv4 address wrapped in IPv6, the whole name is
   refused. IPv6 is allow-listed to global unicast space (2000::/3) minus the special-purpose blocks inside it.
3. The fetcher connects to one of the checked addresses directly, never to the name, so a second DNS answer cannot
   change where the connection goes.

``fixture`` mode exists only so tests can reach servers on 127.0.0.1. It is a constructor argument of the
``Fetcher`` and is never read from configuration or operations. Even in fixture mode every non-loopback special
range stays blocked.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote, urlsplit

from .errors import fail

MAX_URL = 2048
_BAD_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f\\<>\"`{}|^]")
_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")
_TLD = re.compile(r"[a-z][a-z0-9-]{0,62}")
_INTERNAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa", ".lan", ".intranet", ".corp",
                      ".home", ".arpa", ".onion")

_V4_BLOCKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
    "168.63.129.16/32",     # Azure platform address
    "169.254.169.254/32",   # cloud instance metadata (also inside the link-local block; kept for clarity)
    "169.254.170.2/32",     # container task metadata
    "100.100.100.200/32",   # Alibaba Cloud metadata
))
_V4_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_V6_GLOBAL = ipaddress.ip_network("2000::/3")
_V6_BLOCKS = tuple(ipaddress.ip_network(n) for n in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20", "2620:4f:8000::/48",
))


def classify_ip(value: str, *, allow_loopback: bool = False) -> Optional[str]:
    """Why an address must not be contacted, or ``None`` when it is a public address."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0] if isinstance(value, str) else value)
    except ValueError:
        return "invalid"
    if isinstance(value, str) and "%" in value:
        return "scoped"
    if ip.version == 6:
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return classify_ip(str(mapped), allow_loopback=allow_loopback)
        if ip.is_loopback:
            return None if allow_loopback else "loopback"
        if ip not in _V6_GLOBAL:
            return "not-global"
        if any(ip in block for block in _V6_BLOCKS):
            return "special-purpose"
        if ip.is_multicast or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_unspecified or ip.is_site_local:
            return "special-purpose"
        return None
    if ip in _V4_LOOPBACK:
        return None if allow_loopback else "loopback"
    if any(ip in block for block in _V4_BLOCKS):
        return "private-or-reserved"
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "private-or-reserved"
    return None


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int
    request_target: str
    host_header: str
    is_ip: bool


def parse_target(url, *, fixture: bool = False) -> Target:
    """Validate an address and split it into the parts the fetcher needs. Raises ``FetchError``."""
    if not isinstance(url, str):
        raise fail("bad_url")
    raw = url.strip()
    if not raw or len(raw) > MAX_URL or _BAD_CHARS.search(raw):
        raise fail("bad_url")
    if raw.lower().startswith("feed://"):
        raw = "https://" + raw[7:]
    elif raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise fail("bad_url") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise fail("not_https")
    if "@" in parts.netloc:
        raise fail("bad_url")
    host = (parts.hostname or "")
    if not host or "%" in host:
        raise fail("bad_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    is_ip = address is not None
    loopback_literal = bool(address is not None and address.is_loopback)
    # Fixture mode relaxes the scheme and port rules for loopback targets only; every other address follows the normal rules.
    local = fixture and (loopback_literal or host == "localhost")
    if scheme == "http" and not local:
        raise fail("not_https")
    default_port = 443 if scheme == "https" else 80
    if port is None:
        port = default_port
    if not local and port != 443:
        raise fail("bad_port", host)
    if not 0 < port < 65536:
        raise fail("bad_url")
    if not is_ip:
        host = _check_name(host, fixture)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    query = quote(parts.query, safe="=&%+,;:@/?!$'()*~-._[]") if parts.query else ""
    bracket = f"[{host}]" if ":" in host else host
    host_header = bracket if port == default_port else f"{bracket}:{port}"
    return Target(url=f"{scheme}://{host_header}{path}" + (f"?{query}" if query else ""), scheme=scheme, host=host, port=port,
                  request_target=path + (f"?{query}" if query else ""), host_header=host_header, is_ip=is_ip)


def _check_name(host: str, fixture: bool) -> str:
    name = host[:-1] if host.endswith(".") and not host.endswith("..") else host
    try:
        name = name.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise fail("bad_url") from None
    if not name or len(name) > 253:
        raise fail("bad_url")
    labels = name.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise fail("bad_url")
    if fixture and (name == "localhost" or name.endswith(".localhost")):
        return name
    if len(labels) < 2:
        raise fail("blocked_name", name)
    if not _TLD.fullmatch(labels[-1]):
        raise fail("bad_url", name)        # numeric last label: an unusual spelling of an IP address
    if name == "localhost" or name.endswith(_INTERNAL_SUFFIXES) or name == "localhost.localdomain":
        raise fail("blocked_name", name)
    return name


def check_addresses(host: str, addresses, *, allow_loopback: bool = False) -> list:
    """Return the distinct, usable addresses (at most 8) or raise ``blocked_address`` if any answer, among the first 64, is not public."""
    seen, out = set(), []
    for value in list(addresses)[:64]:
        value = str(value)
        if value in seen:
            continue
        seen.add(value)
        if classify_ip(value, allow_loopback=allow_loopback) is not None:
            raise fail("blocked_address", host)
        if len(out) < 8:
            out.append(value)
    if not out:
        raise fail("dns", host)
    return out


def system_resolver(host: str, port: int) -> list:
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class Resolver:
    """Runs a (possibly slow, uncancellable) name lookup with a deadline and a hard cap on helper threads.

    A lookup that outlives its deadline is abandoned, not killed: its thread finishes on its own and releases its
    slot. At most ``max_threads`` lookups can be outstanding, so a dead DNS server cannot grow the thread count.
    """

    def __init__(self, resolve: Optional[Callable] = None, max_threads: int = 2):
        self._resolve = resolve or system_resolver
        self._slots = threading.BoundedSemaphore(max_threads)

    def lookup(self, host: str, port: int, deadline: float, cancel: threading.Event, clock=time.monotonic) -> list:
        if not self._slots.acquire(blocking=False):
            raise fail("dns_busy", host)
        box: dict = {}
        done = threading.Event()

        def work():
            try:
                box["result"] = list(self._resolve(host, port))
            except BaseException as exc:  # noqa: BLE001 - reported below as a lookup failure
                box["error"] = exc
            finally:
                self._slots.release()
                done.set()

        try:
            threading.Thread(target=work, name="myradio-research-dns", daemon=True).start()
        except RuntimeError:
            self._slots.release()
            raise fail("dns_busy", host) from None
        while not done.wait(0.05):
            if cancel.is_set():
                raise fail("cancelled", host)
            if clock() >= deadline:
                raise fail("dns_timeout", host)
        if "error" in box:
            raise fail("dns", host)
        return box["result"]
