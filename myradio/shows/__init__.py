"""Clocks, shows and the planner.

A show is a recurring part of the day with a music style, a host, a talk level and rules for spots
and updates. The week is a time zone plus a set of shows that never overlap and a default show that
fills every gap. ``plan()`` turns the week into the time-ordered plan items the director converts into
generation jobs and queue entries; ``hour_plan()`` shows what one hour of a show holds.

The extension that plugs this into the station is ``myradio.shows.extension`` (``build()``). Everything
else here is plain Python with no side effects, safe to call from any thread.
"""
from .clocks import TEMPLATES as CLOCK_TEMPLATES, ClockItem, hour_plan, summarize
from .conflicts import find_gaps, find_overlaps, report as conflict_report
from .model import DEFAULT_SHOW_ID, MAX_SHOWS, Show, ShowError, Style, default_show
from .planner import ROLES, PlanItem, plan
from .starter import WEEK_TEMPLATES
from .week import Airing, Week

__all__ = ["Airing", "CLOCK_TEMPLATES", "ClockItem", "DEFAULT_SHOW_ID", "MAX_SHOWS", "PlanItem", "ROLES", "Show",
           "ShowError", "Style", "WEEK_TEMPLATES", "Week", "conflict_report", "default_show", "find_gaps",
           "find_overlaps", "hour_plan", "plan", "summarize"]
