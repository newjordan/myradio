"""Run the checks together, safely, and shape the report.

Every check runs on its own short-lived daemon thread and the report waits only until a shared
deadline. A check that raises, hangs (for example on a network drive that has gone away) or
returns nonsense turns into an ``unknown`` row with a plain explanation; it can never raise
into the caller or hold the report up. A check that is still stuck from an earlier report is not
started again, so repeated reports cannot pile up threads.
"""
from __future__ import annotations

import dataclasses
import logging
import textwrap
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .checks import CHECKS, STATES, CheckSpec, Settings
from .tools import clean_text

SCHEMA = 1
REPORT_BUDGET = 6.0  # seconds the whole report may take, however many tools hang
MAX_FOREIGN_CHECKS = 100  # checks accepted from other parts of myradio
LOG = logging.getLogger("myradio.doctor")

DEFAULT_HINTS = {
    "warn": "Read the details above, then run the check again.",
    "fail": "This needs fixing before myradio can work properly. Fix it, then run the check again.",
    "unknown": "Run the check again in a moment.",
}
LABELS = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "unknown": "UNKNOWN"}
EXIT_CODES = {"ok": 0, "unknown": 1, "warn": 1, "fail": 2}


def _version() -> str:
    try:
        from myradio import __version__
        return clean_text(__version__, 30)
    except Exception:
        return ""


def iso_utc(epoch) -> str:
    """ISO-8601 with an offset, for the edges of the API."""
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError, TypeError):
        return "1970-01-01T00:00:00+00:00"


def normalize_check(raw, *, check_id: Optional[str] = None, title: Optional[str] = None) -> Optional[dict]:
    """A clean check with exactly the keys id, title, status, detail, hint and fix; None if ``raw`` is unusable.

    Text is cleaned and bounded, and anything that is not ``ok`` always carries a hint.
    """
    if not isinstance(raw, dict):
        return None
    cid = check_id if check_id is not None else raw.get("id")
    name = title if title is not None else raw.get("title")
    status = raw.get("status")
    if not isinstance(cid, str) or not isinstance(name, str) or not isinstance(status, str) or status not in STATES:
        return None
    cid, name = clean_text(cid, 80), clean_text(name, 120)
    if not cid or not name:
        return None
    detail = clean_text(raw.get("detail", ""), 400)
    hint = clean_text(raw.get("hint", ""), 400)
    fix = clean_text(raw.get("fix", ""), 400, collapse=False)
    if status != "ok":
        detail = detail or "This check gave no details."
        hint = hint or DEFAULT_HINTS[status]
    return {"id": cid, "title": name, "status": status, "detail": detail, "hint": hint, "fix": fix}


def _placeholder(check_id: str, title: str, detail: str, hint: str = "") -> dict:
    return {"id": check_id, "title": title, "status": "unknown", "detail": detail,
            "hint": hint or DEFAULT_HINTS["unknown"], "fix": ""}


def summarize(items: Iterable[dict]) -> dict:
    """Overall state, counts (always in the order ok, warn, fail, unknown) and one plain sentence."""
    counts = {state: 0 for state in STATES}
    for item in items:
        counts[item["status"]] += 1
    total = sum(counts.values())
    if counts["fail"]:
        text = f"Your computer is not ready yet: {counts['fail']} {'thing needs' if counts['fail'] == 1 else 'things need'} fixing first."
        if counts["warn"]:
            text += f" {counts['warn']} more {'is' if counts['warn'] == 1 else 'are'} worth a look."
        overall = "fail"
    elif counts["warn"]:
        text = f"Your computer can run myradio. {counts['warn']} {'thing is' if counts['warn'] == 1 else 'things are'} worth a look."
        overall = "warn"
    elif counts["unknown"]:
        text = (f"Nothing is wrong that we could see, but {counts['unknown']} "
                f"{'thing' if counts['unknown'] == 1 else 'things'} could not be checked.")
        overall = "unknown"
    elif total:
        text, overall = "Your computer is ready for myradio.", "ok"
    else:
        text, overall = "No checks were run.", "unknown"
    return {"overall": overall, "summary": text, "counts": counts}


class _Job:
    def __init__(self, key: str):
        self.key = key
        self.done = threading.Event()
        self.value = None
        self.error: Optional[str] = None
        self.thread: Optional[threading.Thread] = None
        self.busy = False  # an earlier attempt at this check is still stuck, so this one was not started

    def run(self, fn: Callable[[], object]) -> None:
        try:
            self.value = fn()
        except BaseException as exc:  # a check must never take anything down with it
            self.error = type(exc).__name__
            LOG.debug("doctor check %s failed", self.key, exc_info=True)
        finally:
            self.done.set()


class ReportRunner:
    """Builds reports. Keep one for as long as the doctor lives; ``close`` stops anything still running."""

    def __init__(self, specs: Optional[Iterable[CheckSpec]] = None, *, monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], float] = time.time):
        self._specs = tuple(specs) if specs is not None else CHECKS
        self._monotonic = monotonic
        self._clock = clock
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()  # one report at a time: callers that overlap wait their turn
        self._jobs: dict = {}
        self._cancel = threading.Event()
        self._closed = False

    # ---- running -------------------------------------------------------
    def _launch(self, key: str, fn: Callable[[], object]) -> _Job:
        job = _Job(key)
        with self._lock:
            earlier = self._jobs.get(key)
            if earlier is not None and earlier.thread is not None and earlier.thread.is_alive():
                job.busy = True
                job.done.set()
                return job
            thread = threading.Thread(target=job.run, args=(fn,), name=f"myradio-doctor-{key}", daemon=True)
            try:
                thread.start()
            except RuntimeError:  # the computer would not give us another thread
                job.error = "ThreadError"
                job.done.set()
            else:
                job.thread = thread
                self._jobs[key] = job
        return job

    def _collect(self, key: str, title: str, job: _Job):
        """(check, finished_in_time)."""
        if job.busy:
            return _placeholder(key, title, "An earlier attempt at this check is still waiting for your computer to answer.",
                                "Your computer is slow to answer this one, for example a drive that is not responding. Check it, then try again."), False
        if not job.done.is_set():
            return _placeholder(key, title, "This check did not finish in time.",
                                "Your computer was slow to answer. Run the check again in a moment."), False
        if job.error is not None:
            return _placeholder(key, title, "This check hit an unexpected problem and was skipped.",
                                "Run the check again. If it keeps happening, copy this report when you ask for help."), True
        item = normalize_check(job.value, check_id=key, title=title)
        if item is None:
            return _placeholder(key, title, "This check did not give a usable answer.",
                                "Run the check again. If it keeps happening, copy this report when you ask for help."), True
        return item, True

    def run(self, settings: Optional[Settings] = None, *, budget: float = REPORT_BUDGET,
            extra: Optional[Callable[[], list]] = None) -> dict:
        """Run every check and return the report. Never raises.

        ``extra`` is called on its own thread and returns checks from other parts of myradio
        (the doctor extension passes ``station.extension_checks``); they are cleaned and appended.
        """
        try:
            with self._run_lock:
                return self._run(settings, budget, extra)
        except Exception:  # only reachable through a malformed check list: still answer, plainly
            LOG.debug("doctor report failed", exc_info=True)
            item = _placeholder("doctor", "Computer check", "The check could not be run.", "Run the check again. If it keeps happening, restart myradio.")
            report = {"schema": SCHEMA, "myradio_version": _version(), "generated_at": iso_utc(self._clock()), "duration_ms": 0}
            report.update(summarize([item]))
            report["partial"] = True
            report["checks"] = [item]
            return report

    def _run(self, settings, budget, extra) -> dict:
        base = settings if isinstance(settings, Settings) else Settings()
        s = dataclasses.replace(base, cancel=self._cancel)
        started = self._monotonic()
        deadline = started + max(0.0, budget)
        if self._closed:
            jobs = [(spec, None) for spec in self._specs]
            extra_job = None
        else:
            jobs = [(spec, self._launch(spec.id, lambda spec=spec: spec.func(s))) for spec in self._specs]
            extra_job = self._launch("extensions", extra) if extra is not None else None
        for _, job in jobs:
            if job is not None:
                job.done.wait(max(0.0, deadline - self._monotonic()))
        if extra_job is not None:
            extra_job.done.wait(max(0.0, deadline - self._monotonic()))

        items, partial = [], False
        for spec, job in jobs:
            if job is None:
                item, finished = _placeholder(spec.id, spec.title, "The check was stopped before it started."), False
            else:
                item, finished = self._collect(spec.id, spec.title, job)
            partial = partial or not finished
            items.append(item)
        if extra_job is not None:
            taken = {item["id"] for item in items}
            folded, finished = self._foreign(extra_job, taken)
            partial = partial or not finished
            items.extend(folded)
        elapsed = max(0.0, self._monotonic() - started)
        report = {"schema": SCHEMA, "myradio_version": _version(), "generated_at": iso_utc(self._clock()),
                  "duration_ms": int(round(elapsed * 1000))}
        report.update(summarize(items))
        report["partial"] = partial
        report["checks"] = items
        return report

    def _foreign(self, job: _Job, taken: set):
        """Checks from other parts of myradio, cleaned and bounded."""
        if job.busy or not job.done.is_set():
            return [_placeholder("extensions", "Other parts of myradio", "The other parts of myradio did not report back in time.")], False
        if job.error is not None or not isinstance(job.value, (list, tuple)):
            return [_placeholder("extensions", "Other parts of myradio", "The other parts of myradio could not be checked.",
                                 "Run the check again. If it keeps happening, restart myradio.")], True
        folded = []
        for raw in list(job.value)[: MAX_FOREIGN_CHECKS * 2]:
            item = normalize_check(raw)
            if item is None or item["id"] in taken or item["id"].startswith("doctor."):
                continue
            taken.add(item["id"])
            folded.append(item)
            if len(folded) >= MAX_FOREIGN_CHECKS:
                break
        return folded, True

    # ---- shutting down -------------------------------------------------
    def close(self, wait: float = 1.0) -> None:
        """Stop starting work, tell running tools to stop and give the threads a moment to finish."""
        self._closed = True
        self._cancel.set()
        end = self._monotonic() + max(0.0, wait)
        with self._lock:
            threads = [job.thread for job in self._jobs.values() if job.thread is not None]
        for thread in threads:
            thread.join(max(0.0, end - self._monotonic()))


def run_report(settings: Optional[Settings] = None, *, budget: float = REPORT_BUDGET,
               extra: Optional[Callable[[], list]] = None, specs: Optional[Iterable[CheckSpec]] = None) -> dict:
    """One-shot report: build, run, clean up. Never raises."""
    runner = ReportRunner(specs)
    try:
        return runner.run(settings, budget=budget, extra=extra)
    finally:
        runner.close()


def exit_status(report: dict) -> int:
    """0 when every check is ok; 1 when nothing failed but something warned or could not be checked; 2 when a check failed."""
    try:
        return EXIT_CODES.get(summarize(report["checks"])["overall"], 1)
    except Exception:
        return 1


def _fix_line(fix: str, width: int, pad: str) -> str:
    """The "How to fix it" line.

    A fix that holds a command (instruction text, then ``: ``, then the command) is kept on one line,
    however long: a command broken across lines is easy to copy wrongly. The terminal wraps it on screen,
    and a line copied from there is still one line. A fix that is only advice is wrapped like the rest.
    """
    text = "How to fix it: " + fix
    if ": " in fix:
        return pad + text
    return textwrap.fill(text, width, initial_indent=pad, subsequent_indent=pad + "   ", break_long_words=False, break_on_hyphens=False)


def render_text(report: dict, width: int = 78) -> str:
    """The readable report for a terminal. Plain text only; status is always written as a word."""
    pad = " " * 8
    lines = ["myradio doctor: is your computer ready?",
             textwrap.fill("This only looks at your computer. It does not install, download or change anything.", width), ""]
    for item in report.get("checks", []):
        lines.append(f"{LABELS.get(item['status'], 'UNKNOWN'):<8}{item['title']}")
        lines.append(textwrap.fill(item["detail"], width, initial_indent=pad, subsequent_indent=pad, break_long_words=False, break_on_hyphens=False)
                     if item["detail"] else pad.rstrip())
        if item["status"] != "ok":
            lines.append(textwrap.fill("What to do: " + item["hint"], width, initial_indent=pad, subsequent_indent=pad + "   ",
                                       break_long_words=False, break_on_hyphens=False))
            if item.get("fix"):
                lines.append(_fix_line(item["fix"], width, pad))
    counts = report.get("counts", {})
    total = sum(counts.values()) if counts else len(report.get("checks", []))
    lines += ["", report.get("summary", ""),
              f"Looked at {total} {'thing' if total == 1 else 'things'} in {report.get('duration_ms', 0) / 1000:.1f} seconds."]
    if report.get("partial"):
        lines.append("Some checks did not finish in time. Run the check again for a complete answer.")
    return "\n".join(lines)
