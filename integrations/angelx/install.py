#!/usr/bin/env python3
"""Install the myradio plugin into angelX: the station's MCP server and the myradio-dj skill.

    python3 install.py              add myradio to ~/.angelX/mcp.json and link the skill into ~/.angelX/skills
    python3 install.py --check      also start the adapter once and ask the station for its status
    python3 install.py --uninstall  take both out again
    python3 install.py --dry-run    show what would change

angelX reads MCP servers from ANGEL_MCP_CONFIG (default ~/.angelX/mcp.json) and user skills from ANGEL_SKILLS_DIR
(default ~/.angelX/skills). Other servers in mcp.json are kept; the old file is saved as mcp.json.bak.
No token is written anywhere: the adapter reads the station's control code from its data folder when it starts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "myradio_mcp.py"
SKILL = HERE / "skills" / "myradio-dj"
SERVER = "myradio"


def angel_path(variable, default):
    value = os.environ.get(variable, "").strip()
    return Path(value).expanduser() if value else Path.home() / ".angelX" / default


def server_entry(data_dir, port):
    env = {}
    if data_dir:
        env["MYRADIO_DATA_DIR"] = str(Path(data_dir).expanduser().resolve())
    if port:
        env["MYRADIO_PORT"] = str(port)
    entry = {"command": sys.executable, "args": [str(LAUNCHER)]}
    if env:
        entry["env"] = env
    return entry


def read_config(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        sys.exit(f"{path} is not valid JSON. Fix or move it, then run this again.")
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
        sys.exit(f"{path} does not look like an MCP config ({{\"mcpServers\": {{...}}}}). Leaving it alone.")
    return value


def write_config(path, value, dry_run):
    text = json.dumps(value, indent=2) + "\n"
    if dry_run:
        print(f"would write {path}:\n{text}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(text, encoding="utf-8")
    os.chmod(staged, 0o600)
    os.replace(staged, path)
    print(f"updated {path}")


def link_skill(target, copy, dry_run):
    if target.is_symlink() or target.exists():
        if target.is_symlink() and target.resolve() == SKILL.resolve():
            print(f"skill already linked: {target}")
            return
        if not target.is_symlink():
            sys.exit(f"{target} already exists and is not this plugin's link. Remove it first, then run this again.")
        if not dry_run:
            target.unlink()
    if dry_run:
        print(f"would {'copy' if copy else 'link'} {SKILL} -> {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if copy:
            raise OSError("copy requested")
        target.symlink_to(SKILL, target_is_directory=True)
        print(f"linked skill {target}")
    except OSError:
        shutil.copytree(SKILL, target)
        print(f"copied skill to {target}")


def unlink_skill(target, dry_run):
    if target.is_symlink() and target.resolve() == SKILL.resolve():
        if dry_run:
            print(f"would remove {target}")
        else:
            target.unlink()
            print(f"removed {target}")
    elif target.exists():
        print(f"left {target} in place (not this plugin's link; remove it yourself if it is a copy)")


def check(entry):
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "station_status", "arguments": {}}}]
    env = {**os.environ, **entry.get("env", {})}
    done = subprocess.run([entry["command"], *entry["args"]], input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, timeout=30, env=env)
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip().startswith("{")]
    if len(replies) < 2:
        print(f"Not connected: {(done.stderr.strip().splitlines() or ['the adapter did not answer'])[-1]}")
        return 1
    result = replies[1].get("result", {})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    if result.get("isError"):
        print(f"Not connected: {text}")
        return 1
    status = json.loads(text)
    print(f"Connected to \"{status['station']['name']}\": {'playing' if status.get('running') else 'stopped'}, "
          f"{len(status.get('queue') or [])} queued, {len(status.get('library') or [])} tracks in the library.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the myradio plugin into angelX.")
    parser.add_argument("--data-dir", help="The station's data folder, if not ~/.myradio (or MYRADIO_DATA_DIR)")
    parser.add_argument("--port", type=int, help="The station's port, if not 8765 (or MYRADIO_PORT)")
    parser.add_argument("--copy", action="store_true", help="Copy the skill instead of linking it")
    parser.add_argument("--uninstall", action="store_true", help="Remove the server entry and the skill link")
    parser.add_argument("--check", action="store_true", help="After installing, ask the station for its status")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change and change nothing")
    args = parser.parse_args(argv)
    config_path = angel_path("ANGEL_MCP_CONFIG", "mcp.json")
    skill_target = angel_path("ANGEL_SKILLS_DIR", "skills") / SKILL.name
    config = read_config(config_path)
    servers = config.setdefault("mcpServers", {})
    if args.uninstall:
        if SERVER in servers:
            del servers[SERVER]
            write_config(config_path, config, args.dry_run)
        unlink_skill(skill_target, args.dry_run)
        return 0
    entry = server_entry(args.data_dir or os.environ.get("MYRADIO_DATA_DIR"), args.port or os.environ.get("MYRADIO_PORT"))
    if servers.get(SERVER) != entry:
        servers[SERVER] = entry
        write_config(config_path, config, args.dry_run)
    else:
        print(f"{config_path} already has the myradio server")
    link_skill(skill_target, args.copy, args.dry_run)
    if not args.dry_run:
        print("Restart angelX to load the myradio tools.")
    return check(entry) if args.check and not args.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
