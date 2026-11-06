"""Sponsor messages ("spots"): campaigns, scheduling rules, break building and honest reporting.

The extension lives in ``myradio.ads.extension``; ``build()`` returns it. The pure pieces can be used
on their own: ``model`` (campaigns and the spoken-script rules), ``flights`` (calendar rules),
``engine`` (``build_break``), ``reports`` and ``words`` (plain-language wording).
"""
from .engine import Audio, BreakContext, BreakItem, BreakPlan, Play, Skip, build_break
from .model import AdsError, Campaign, Creative, Flight, parse_campaign

__all__ = ["AdsError", "Audio", "BreakContext", "BreakItem", "BreakPlan", "Campaign", "Creative", "Flight", "Play", "Skip",
           "build_break", "parse_campaign"]
