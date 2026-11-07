#!/usr/bin/env python3
"""Start myradio's own MCP adapter (myradio.mcp) for an agent harness.

Every plugin in integrations/ ships an identical copy of this file, so each folder can be installed on its
own. It only finds myradio and hands over; the adapter itself reads the station's private control.token
from the data folder. Nothing here reads, prints or stores the token.

Where it looks for myradio, first match wins:
  1. MYRADIO_HOME: a myradio source folder (the one with myradio/mcp.py in it)
  2. the myradio source folder this file sits in (a git checkout with integrations/ inside)
  3. the myradio-mcp command on PATH (the installer puts it there)
  4. ~/myradio
  5. a myradio package the running Python can already import

Station settings: MYRADIO_DATA_DIR (default ~/.myradio) and MYRADIO_PORT (default 8765). Arguments given to
this script are passed on to the adapter and win over both, e.g. --port 8766.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def is_source(folder):
    return folder is not None and (Path(folder) / "myradio" / "mcp.py").is_file()


def find_source():
    """A myradio source folder to import from, or None when only the installed command or package is available."""
    home = os.environ.get("MYRADIO_HOME", "").strip()
    if home:
        home = Path(home).expanduser()
        if not is_source(home):
            sys.exit(f"myradio: MYRADIO_HOME={home} is not a myradio folder (no myradio/mcp.py inside).")
        return home
    for parent in Path(__file__).resolve().parents:
        if is_source(parent) and (parent / "integrations").is_dir():
            return parent
    return None


def station_args(extra):
    data_dir = os.environ.get("MYRADIO_DATA_DIR", "").strip() or str(Path.home() / ".myradio")
    port = os.environ.get("MYRADIO_PORT", "").strip() or "8765"
    return ["--data-dir", str(Path(data_dir).expanduser()), "--port", port, *extra]


def main(argv=None):
    extra = sys.argv[1:] if argv is None else list(argv)
    args = station_args(extra)
    source = find_source()
    if source is None:
        command = shutil.which("myradio-mcp")
        if command:
            if os.name == "nt":
                return subprocess.call([command, *args])
            os.execv(command, [command, *args])
        fallback = Path.home() / "myradio"
        if is_source(fallback):
            source = fallback
        elif importlib.util.find_spec("myradio") is None:
            sys.exit("myradio: could not find myradio. Install it (see https://myradio.garden), "
                     "or set MYRADIO_HOME to your myradio folder.")
    if source is not None:
        sys.path.insert(0, str(source))
    from myradio.mcp import main as adapter_main
    return adapter_main(args)


if __name__ == "__main__":
    sys.exit(main())
