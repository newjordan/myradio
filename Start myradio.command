#!/bin/sh
# Local checkout launcher for Linux and macOS. No network, sudo, or model download.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'myradio needs Python 3.10 or later. Install Python, then reopen this launcher.' >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  printf '%s\n' 'Please upgrade to Python 3.10 or later.' >&2
  exit 1
fi
exec python3 -m myradio.server "$@"
