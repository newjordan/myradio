"""The doctor as a station extension.

* ``GET /api/x/doctor/report`` returns the readiness report. The answer is kept for 30 seconds;
  ``?refresh=1`` asks for a fresh one, but never more often than every 5 seconds.
* ``doctor.report`` gives the same report to the Operator and to external agents. It only reads.
* The report also folds in the checks of every other running extension
  (``station.extension_checks()``). This extension's own ``checks()`` is empty so nothing is
  counted twice.
* ``status()`` returns a tiny cached summary of the last report. It never runs a check and never
  calls the station.

The doctor keeps no files. Nothing about it needs to be backed up, and nothing it reports is secret
apart from computer facts (system version, hardware names, paths written with ``~`` for home).
No route or operation takes a path, a port or a command, so an agent cannot point it anywhere.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Optional

from myradio.extensions import Arg, Context, Extension, ExtensionError, Operation, Request, Route

from .checks import Settings
from .report import REPORT_BUDGET, ReportRunner

CACHE_SECONDS = 30.0
REFRESH_INTERVAL = 5.0
MAX_ATTENTION = 12


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class DoctorExtension(Extension):
    name = "doctor"

    def __init__(self, *, monotonic=time.monotonic, runner: Optional[ReportRunner] = None, budget: float = REPORT_BUDGET):
        self._monotonic = monotonic
        self._runner = runner
        self._budget = budget
        self._ctx: Optional[Context] = None
        self._cached = None  # (report, monotonic time it was made)
        self._snapshot: Optional[dict] = None
        self._compute_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def start(self, ctx: Context) -> None:
        # Nothing is checked here: other extensions are still starting, and asking for their
        # checks now would report them as "not running".
        self._ctx = ctx
        if self._runner is None:
            self._runner = ReportRunner(monotonic=self._monotonic, clock=ctx.clock)

    def stop(self) -> None:
        if self._runner is not None:
            self._runner.close()

    # ---- what it offers ----------------------------------------------------
    def operations(self):
        return [Operation(
            "report", "Check whether this computer is ready for myradio and list anything that needs attention. Read-only: nothing is installed or changed.",
            {"refresh": Arg("boolean", "Check again instead of using the answer from the last 30 seconds (at most once every 5 seconds).", required=False)},
            self._operation_report)]

    def routes(self):
        return [Route("GET", "/report", self._route_report)]

    def status(self) -> dict:
        snapshot = self._snapshot
        return copy.deepcopy(snapshot) if snapshot else {"checked": False}  # a few small values: cheap, and callers cannot change the original

    def checks(self):
        return []  # the report merges everyone else's checks; listing ours here too would count them twice

    def persistent_files(self):
        return ()

    # ---- the report ----------------------------------------------------------
    def _fresh_enough(self, refresh: bool) -> bool:
        cached = self._cached
        if cached is None:
            return False
        age = self._monotonic() - cached[1]
        if age < 0:
            return False  # the clock moved backwards: do not trust the stored answer
        return age < CACHE_SECONDS and not (refresh and age >= REFRESH_INTERVAL)

    def _decorate(self, cached, *, was_cached: bool) -> dict:
        report, taken = cached
        answer = copy.deepcopy(report)  # callers get their own copy, so nothing they do can change the cached answer
        answer["cached"] = was_cached
        answer["age_seconds"] = round(max(0.0, self._monotonic() - taken), 1)
        return answer

    def report(self, refresh: bool = False) -> dict:
        """The report, from the 30-second cache when it is still fresh. Only one check runs at a time."""
        if self._fresh_enough(refresh):
            return self._decorate(self._cached, was_cached=True)
        with self._compute_lock:
            if self._fresh_enough(refresh):  # another request finished a report while this one waited
                return self._decorate(self._cached, was_cached=True)
            ctx = self._ctx
            if ctx is None or self._runner is None:
                raise ExtensionError("The computer check is not running right now.")
            gather = getattr(ctx.station, "extension_checks", None)  # an older host may not offer it: then the report is just ours
            try:
                report = self._runner.run(Settings(data_dir=str(ctx.directory), in_service=True), budget=self._budget,
                                          extra=gather if callable(gather) else None)
            except Exception:
                raise ExtensionError("The computer check could not run just now. Try again in a moment.") from None
            self._cached = (report, self._monotonic())
            self._snapshot = {
                "checked": True, "overall": report["overall"], "summary": report["summary"], "counts": dict(report["counts"]),
                "checked_at": report["generated_at"],
                "attention": [item["id"] for item in report["checks"] if item["status"] in ("fail", "warn")][:MAX_ATTENTION],
            }
            return self._decorate(self._cached, was_cached=False)

    def _route_report(self, ctx: Context, request: Request) -> dict:
        try:
            refresh = _truthy((request.query.get("refresh") or [""])[0])
        except Exception:
            refresh = False  # an odd query string just means "use the cached answer"
        return self.report(refresh=refresh)

    def _operation_report(self, ctx: Context, args: dict) -> dict:
        return self.report(refresh=bool(args.get("refresh")))


def build() -> Extension:
    return DoctorExtension()
