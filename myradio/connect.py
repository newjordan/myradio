"""Connect a running myradio station to an outbound stream, from the command line or from an agent.

    MYRADIO_SOURCE_PASSWORD=... python3 -m myradio.connect stream --url https://radio.example.org:8443/live.mp3
    python3 -m myradio.connect status

The source password comes from an environment variable (MYRADIO_SOURCE_PASSWORD by default) or a file, never from the
command line, and it is never printed. So an agent can save the destination without the password appearing in its chat,
as long as it sets the variable from where the password already is (a file its server setup wrote, for example) instead of
typing it into a command. The station's private control code is read from the data folder, as the MCP adapter does.
Saving a destination never goes live: that is GO LIVE in the studio, or the broadcast_start tool.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from .mcp import Client


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m myradio.connect", description="Connect a running myradio station to an outbound stream.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=os.environ.get("MYRADIO_DATA_DIR", str(Path.home() / ".myradio")))
    commands = parser.add_subparsers(dest="command", required=True)
    stream = commands.add_parser("stream", help="Save the Icecast destination: mount URL, source user and source password.")
    stream.add_argument("--url", required=True, help="Mount URL without credentials, e.g. https://radio.example.org:8443/live.mp3")
    stream.add_argument("--username", default="source", help="Source user (usually source)")
    secret = stream.add_mutually_exclusive_group()
    secret.add_argument("--password-env", default="MYRADIO_SOURCE_PASSWORD", help="Environment variable that holds the source password")
    secret.add_argument("--password-file", help="File whose first line is the source password")
    commands.add_parser("status", help="Show the saved stream destination and whether the station is live.")
    args = parser.parse_args(argv)
    try:
        token = (Path(args.data_dir).expanduser() / "control.token").read_text().strip()
    except OSError:
        parser.exit(1, "myradio: start the station first; this needs its private control.token file in the data folder.\n")
    client = Client(args.port, token, client="connect")
    try:
        if args.command == "status":
            saved = client.request("/api/broadcast").get("url") or "not saved"
            state = client.request("/api/status")["broadcast"]["state"]
            print(f"Stream destination: {saved}. Output: {state}.")
            return 0
        if urlsplit(args.url).username or urlsplit(args.url).password:
            parser.exit(2, "myradio: leave the user and password out of --url; use --username and the password variable or file.\n")
        if args.password_file:
            try:
                password = Path(args.password_file).expanduser().read_text().splitlines()[0].strip()
            except (OSError, IndexError):
                parser.exit(2, "myradio: could not read a password from --password-file.\n")
        else:
            password = os.environ.get(args.password_env, "")
        if not password:
            parser.exit(2, f"myradio: set {args.password_env} to the source password first, from wherever it already is "
                           "(never by typing it into a command), or use --password-file.\n")
        client.request("/api/broadcast", {"url": args.url, "username": args.username, "password": password})
    except ValueError as exc:
        parser.exit(1, f"myradio: {exc}\n")
    print(f"Saved the stream destination {args.url} (user {args.username}; password not shown). "
          "Going live is a separate step: GO LIVE in the studio, or the broadcast_start tool when the owner asks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
