"""Failures the research extension can explain to a person.

Every ``FetchError`` carries a ``kind`` (a short stable code for programs and tests) and a ``message`` written
for someone who is not technical: what happened, what it means and what to do next. Messages never contain
text taken from the remote site, only the (validated) host name the person already chose.
"""
from __future__ import annotations

from typing import Optional

KINDS = (
    "bad_url", "not_https", "bad_port", "blocked_name", "blocked_address", "dns", "dns_timeout", "dns_busy", "connect",
    "timeout", "certificate", "tls", "too_large", "bad_type", "bad_response", "http_error", "rate_limited",
    "too_many_redirects", "redirect_loop", "daily_cap", "cancelled", "backoff", "unsupported_encoding", "not_modified",
)


class FetchError(Exception):
    def __init__(self, kind: str, message: str, *, status: Optional[int] = None, retry_after: Optional[float] = None,
                 host: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.host = host


def describe(kind: str, host: str = "", *, status: Optional[int] = None, seconds: float = 0, cap: int = 0, limit_mb: int = 2) -> str:
    """The plain-language sentence for an error kind."""
    site = host or "that website"
    if kind == "bad_url":
        return "That does not look like a web address. Use one that starts with https://, for example https://example.org/feed."
    if kind == "not_https":
        return "Only secure web addresses can be used. Use the same address starting with https://."
    if kind == "bad_port":
        return "Only the standard secure web port can be used. Use an address without a port number."
    if kind == "blocked_name":
        return f"{site} is a name for a computer inside a private network, so the station will not open it. Choose a public website."
    if kind == "blocked_address":
        return f"{site} points to a private or internal address, so the station will not open it. Choose a public website."
    if kind == "dns":
        return f"The station could not find {site}. Check the spelling of the address and that your internet connection is working."
    if kind == "dns_timeout":
        return f"Looking up {site} took too long. Check that your internet connection is working; the station will try again later."
    if kind == "dns_busy":
        return "The station is still waiting on an earlier address lookup. It will try again shortly."
    if kind == "connect":
        return f"The station could not connect to {site}. The site may be down, or your internet connection may be offline."
    if kind == "timeout":
        return f"{site} took too long to answer (more than {int(seconds)} seconds), so the station gave up. It will try again later."
    if kind == "certificate":
        return (f"The station could not confirm that {site} is who it says it is (its security certificate was not accepted), "
                "so it did not connect. If this happens for every site, your computer's list of trusted certificates may be missing.")
    if kind == "tls":
        return f"The station could not make a secure connection to {site}."
    if kind == "too_large":
        return f"{site} sent far more data than a feed or page should (over {limit_mb} MB), so the station stopped reading."
    if kind == "bad_type":
        return f"{site} returned something that is not a feed or a web page, so the station skipped it."
    if kind == "bad_response":
        return f"{site} sent a reply the station could not understand."
    if kind == "http_error":
        if status == 404 or status == 410:
            return f"{site} says that page does not exist ({status}). Check the address, or look for the site's current feed address."
        if status in (401, 403):
            return f"{site} would not let the station read that address ({status}). It may need a login, or it may block automatic readers."
        if status == 429:
            return f"{site} asked the station to slow down (429). The station will wait before trying that site again."
        if status is not None and status >= 500:
            return f"{site} is having trouble right now (error {status}). The station will try again later."
        return f"{site} answered with an unexpected status ({status})."
    if kind == "rate_limited":
        return f"{site} asked the station to slow down. The station will wait before trying that site again."
    if kind == "too_many_redirects":
        return "That address sent the station around too many times (more than 3 redirects), so it stopped."
    if kind == "redirect_loop":
        return "That address keeps sending the station back to where it started, so it stopped."
    if kind == "daily_cap":
        return f"The station has used its allowance of {cap} web requests for today. Researchers will carry on tomorrow."
    if kind == "cancelled":
        return "Stopped because the station is shutting down."
    if kind == "backoff":
        return f"{site} asked the station to wait before asking again, so it will try later."
    if kind == "unsupported_encoding":
        return f"{site} sent its reply in a compressed format the station cannot read."
    return "Something went wrong while reading that address."


_ELSEWHERE = {
    "blocked_name": "It is a name for a private network, so the station stopped.",
    "blocked_address": "It is a private or internal address, so the station stopped.",
    "not_https": "It is not a secure address, so the station stopped.",
    "bad_port": "It uses a port the station does not allow, so the station stopped.",
    "bad_url": "It is not a valid web address, so the station stopped.",
    "dns": "The station could not find it.",
    "dns_timeout": "Looking it up took too long.",
    "dns_busy": "The station is still waiting on an earlier address lookup.",
    "connect": "The station could not connect to it.",
    "timeout": "It took too long to answer.",
    "certificate": "Its security certificate could not be confirmed.",
    "tls": "The station could not make a secure connection to it.",
    "too_large": "It sent far more data than a feed or page should.",
    "bad_type": "It returned something that is not a feed or a web page.",
    "bad_response": "Its reply could not be understood.",
    "rate_limited": "It asked the station to slow down.",
    "backoff": "It asked the station to wait before asking again.",
    "unsupported_encoding": "It sent its reply in a format the station cannot read.",
}


def describe_elsewhere(kind: str, origin: str, *, status: Optional[int] = None, seconds: float = 0, cap: int = 0) -> str:
    """The sentence for a failure at an address that a website chose (a redirect target or a discovered feed).

    The name of that address is deliberately left out: it was picked by the website, not by the operator, so only the
    operator's own source is ever named in a message."""
    if kind in ("too_many_redirects", "redirect_loop", "daily_cap", "cancelled"):
        return describe(kind, origin, status=status, seconds=seconds, cap=cap)
    reason = f"It answered with an error ({status})." if kind == "http_error" and status else _ELSEWHERE.get(kind, "It could not be used.")
    return f"{origin} sent the station on to another address. {reason}"


def fail(kind: str, host: str = "", *, status: Optional[int] = None, retry_after: Optional[float] = None,
         seconds: float = 0, cap: int = 0, limit_mb: int = 2) -> FetchError:
    return FetchError(kind, describe(kind, host, status=status, seconds=seconds, cap=cap, limit_mb=limit_mb),
                      status=status, retry_after=retry_after, host=host)
