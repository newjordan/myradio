"""JSON shapes shown to screens and agents, with plain-language labels. Times are ISO-8601 with an offset."""
from __future__ import annotations

from typing import Mapping, Optional

from .payloads import ROLE_LABEL
from .util import iso, short

STATE_LABELS = {
    "queued": "Waiting",
    "running": "Being made now",
    "succeeded": "Done",
    "failed": "Could not be made",
    "cancelled": "Cancelled",
    "expired": "Dropped (too late)",
    "interrupted": "Interrupted, needs a retry",
}

WORKER_LABELS = {
    "checking": "Checking",
    "ready": "Ready",
    "warming": "Warming up",
    "unreachable": "Not answering",
    "missing_files": "Files missing",
    "failed": "Not working",
    "blocked": "Switched off",
    "degraded": "Having trouble",
    "misconfigured": "Settings problem",
}


# What a person can do about a finished job, by error code. Plain words, no technical terms.
FAILED_HINTS = {
    "worker_unreachable": "Check that the maker is running, then choose Try again.",
    "worker_timeout": "The maker may be too busy. Choose Try again when it is free.",
    "worker_failed": "Check the maker, then choose Try again.",
    "worker_blocked": "Remove the test maker from the settings.",
    "worker_missing": "Set up the maker in the generation settings, then choose Try again.",
    "bad_output": "Choose Try again to get a new take.",
    "no_output": "Choose Try again to get a new take.",
    "output_too_large": "Check the maker's settings, then choose Try again.",
    "silent_output": "Choose Try again to get a new take.",
    "audio_too_short": "Choose Try again to get a new take.",
    "audio_too_long": "Ask for something shorter, or choose Try again.",
    "audio_cut_off": "Choose Try again to get a new take.",
    "audio_unusable": "Choose Try again to get a new take.",
    "audio_tools_missing": "Reinstall myradio, then choose Try again.",
    "low_disk": "Free up disk space, then choose Try again.",
    "import_failed": "Check the station, then choose Try again.",
    "internal": "Choose Try again. If it keeps happening, look at the technical details.",
}


def job_hint(job: Mapping) -> str:
    state = job["state"]
    if state == "queued":
        return "It will try again by itself." if job.get("attempts") else ""
    if state == "failed":
        return FAILED_HINTS.get(job.get("error_code") or "", "Choose Try again.")
    if state == "expired":
        return "Choose Try again if you still need it."
    if state == "interrupted":
        return "Choose Try again when you are ready."
    return ""


def state_label(job: Mapping) -> str:
    if job["state"] == "queued" and job.get("attempts"):
        return "Waiting to try again"
    return STATE_LABELS.get(job["state"], job["state"])


def attempt_view(entry: Mapping) -> dict:
    return {"n": entry.get("n"), "started": iso(entry.get("started_at")), "ended": iso(entry.get("ended_at")),
            "worker": entry.get("worker"), "seed": entry.get("seed"), "outcome": entry.get("outcome"),
            "code": entry.get("code"), "message": entry.get("message")}


def job_view(job: Mapping, *, detail: bool = False, fixture: Optional[bool] = None, provenance: Optional[Mapping] = None) -> dict:
    out = {
        "id": job["id"], "seq": job.get("seq"), "role": job["role"], "role_label": ROLE_LABEL.get(job["role"], job["role"]),
        "title": job["title"], "summary": job.get("summary") or "",
        "state": job["state"], "state_label": state_label(job), "priority": job["priority"],
        "created": iso(job["created_at"]), "started": iso(job.get("started_at")), "finished": iso(job.get("finished_at")),
        "expires": iso(job.get("expires_at")),
        "next_try": iso(job["not_before"]) if job["state"] == "queued" and job.get("attempts") else None,
        "attempts": job["attempts"], "max_attempts": job["max_attempts"],
        "worker": job["worker_id"], "resource": job["resource"], "idempotency_key": job.get("idempotency_key"),
        "seed": job["seed"], "track_id": job.get("track_id"), "fixture": bool(fixture),
        "error": job.get("error"), "error_code": job.get("error_code"), "hint": job_hint(job), "note": job.get("note"),
        "cancel_requested": bool(job.get("cancel_requested")),
    }
    if detail:
        out["detail"] = short(job["detail"], 1500) if job.get("detail") else None
        out["payload"] = job["payload"]
        out["attempt_log"] = [attempt_view(a) for a in job.get("attempt_log") or []]
        out["interruptions"] = job.get("interruptions", 0)
        out["provenance"] = provenance_view(provenance) if provenance else None
    return out


def provenance_view(prov: Mapping) -> dict:
    return {
        "track_id": prov["track_id"], "job_id": prov["job_id"], "role": prov["role"], "kind": prov["kind"], "title": prov["title"],
        "worker": {"id": prov["worker_id"], "kind": prov["worker_kind"], "version": prov.get("worker_version"), "hash": prov.get("worker_hash")},
        "fixture": bool(prov["fixture"]), "prompt_id": prov.get("prompt_id"), "seed": prov.get("seed"), "params": prov.get("params"),
        "timings": prov.get("timings"), "attempts": prov.get("attempts"), "validation": prov.get("validation"),
        "audio_sha256": prov.get("audio_sha256"), "duration_s": prov["duration_s"], "created": iso(prov["created_at"]),
        "status": prov["status"], "aired": iso(prov.get("aired_at")),
    }


def compact_job(job: Mapping, fixture: bool) -> dict:
    """The few fields the status snapshot carries for a recently finished job."""
    return {"id": job["id"], "role": job["role"], "title": job["title"], "state": job["state"], "state_label": state_label(job),
            "finished": iso(job.get("finished_at")), "error": job.get("error"), "track_id": job.get("track_id"), "fixture": bool(fixture)}
