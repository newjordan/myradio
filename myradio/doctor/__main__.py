"""python -m myradio.doctor: is your computer ready for myradio?

Looks at this computer and prints a short report. It only looks: nothing is installed,
downloaded, written or changed, and no password is ever asked for. Lines marked "How to fix it"
are instructions for you to follow yourself; they are never run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from .checks import DEFAULT_PORT, Settings
from .report import ReportRunner, exit_status, render_text

EXIT_USAGE = 64
DESCRIPTION = ("Looks at this computer and says whether it is ready for myradio. It only looks: nothing is installed, downloaded, "
               "written or changed, and no password is ever asked for. Lines marked \"How to fix it\" are instructions for you to "
               "follow yourself; they are never run.")
EPILOG = ("Exit status: 0 everything is fine; 1 nothing failed but something is worth a look or could not be checked; "
          "2 something must be fixed first; 64 the command was typed wrongly.")
NOT_WRITTEN = ("The report could not be written because the output is closed or the disk is full. "
               "Run the command again in a normal terminal window, or free up some space.")

# What happened when something was written to an output.
WROTE, READER_GONE, FAILED = "wrote", "reader gone", "failed"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage mistakes get their own exit status, 2 is kept for "something failed"
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: {message}\n")


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("the port must be a whole number from 0 to 65535") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("the port must be a whole number from 0 to 65535")
    return value


def build_parser() -> argparse.ArgumentParser:
    typed = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    parser = _Parser(prog="myradio-doctor" if typed == "myradio-doctor" else "python -m myradio.doctor", description=DESCRIPTION, epilog=EPILOG)
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    parser.add_argument("--data-dir", default=None, metavar="DIR",
                        help="the station folder to look at (default: MYRADIO_DATA_DIR, else ~/.myradio)")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, metavar="PORT",
                        help=f"the control page port to test (default: {DEFAULT_PORT}; 0 means any free port)")
    return parser


def _hand_over_to_null_device(stream) -> None:
    """Nobody can take what is still waiting in ``stream``, so let it go to the null device.

    Python sends whatever is left in its standard output and error one more time when the program
    ends. If the reader has gone, or the disk is full, that last attempt fails and turns the exit
    status into 120. Pointing the stream at the null device first lets the program end quietly with
    the status it earned. Only the program's own standard output and error are touched.
    """
    if stream is None or (stream is not getattr(sys, "__stdout__", None) and stream is not getattr(sys, "__stderr__", None)):
        return
    try:
        fd = stream.fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            if null != fd:
                os.dup2(null, fd)
        finally:
            if null != fd:
                os.close(null)
        stream.flush()
    except (OSError, ValueError, AttributeError):
        pass


def _attempt(stream, action) -> str:
    """Run ``action`` (a write or a flush) on ``stream`` and say what came of it. Never raises."""
    try:
        action()
        return WROTE
    except BrokenPipeError:  # the reader closed the pipe early (for example "| head")
        outcome = READER_GONE
    except (OSError, ValueError):  # a full disk, a file that was closed, a terminal that went away
        outcome = FAILED
    _hand_over_to_null_device(stream)
    return outcome


def _write(stream, text: str) -> str:
    """Write one line and flush it. Returns WROTE, READER_GONE or FAILED; never raises."""
    if stream is None:  # the program was started with this output closed
        return FAILED
    line = text + "\n"

    def go() -> None:
        try:
            stream.write(line)
        except UnicodeEncodeError:  # a terminal that cannot show some character: show it escaped instead
            stream.write(line.encode("ascii", "backslashreplace").decode("ascii"))
        stream.flush()

    return _attempt(stream, go)


def _settle(stream) -> None:
    """Send out anything argparse printed (the help text, a usage message) and cope with a closed output the same way."""
    if stream is not None:
        _attempt(stream, stream.flush)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the checks and print the report. Returns the exit status; never raises for a bad environment.

    The status describes the computer, not the printing: if the report cannot be delivered (the reader
    went away, the output is closed, the disk is full) the status is still the one the report earned.
    A reader that went away is silent, like any command in a pipe; the other cases say so on standard error.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help (0) or a usage mistake (64)
        _settle(sys.stdout)
        _settle(sys.stderr)
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    runner = ReportRunner()
    try:
        report = runner.run(Settings(data_dir=args.data_dir or None, port=args.port))
    except KeyboardInterrupt:
        _write(sys.stderr, "Stopped before the check finished.")
        return 130
    finally:
        runner.close()
    if _write(sys.stdout, json.dumps(report, indent=2) if args.json else render_text(report)) == FAILED:
        _write(sys.stderr, NOT_WRITTEN)
    return exit_status(report)


if __name__ == "__main__":
    sys.exit(main())
