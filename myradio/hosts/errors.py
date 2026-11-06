"""Errors for the hosts package. They subclass ExtensionError so their messages (always plain
language, written for the person running the station) reach the operator and agents unchanged."""
from __future__ import annotations

from myradio.extensions import ExtensionError


class HostsError(ExtensionError):
    """An expected, presentable failure."""


class NothingToRead(HostsError):
    """A research brief had no item that can be read aloud with its source. Nothing is invented
    to fill the gap; the right response is to keep playing music."""
