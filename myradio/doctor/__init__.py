"""The myradio doctor: read-only checks that answer "Is your computer ready?".

Use it three ways: ``python -m myradio.doctor`` (or the ``myradio-doctor`` command), the
``run_report`` function, or the ``doctor`` extension inside the running station. It only looks.
It never installs, downloads or changes anything.
"""
from .checks import CHECKS, DEFAULT_PORT, Settings
from .report import ReportRunner, exit_status, render_text, run_report

__all__ = ["CHECKS", "DEFAULT_PORT", "ReportRunner", "Settings", "exit_status", "render_text", "run_report"]
