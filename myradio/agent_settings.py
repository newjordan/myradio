"""Private opt-in operator connection settings; never part of agent snapshots."""
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


class AgentSettingsError(ValueError):
    pass


def validate_agent(value):
    if not isinstance(value, dict) or set(value) != {'url', 'model', 'key'}:
        raise AgentSettingsError('Provide the model base URL, model ID and API key (blank for a local server).')
    url, model, key = (value[k] for k in ('url', 'model', 'key'))
    if not all(isinstance(v, str) for v in (url, model, key)):
        raise AgentSettingsError('Operator connection settings must be text.')
    if len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise AgentSettingsError('Use an ASCII model base URL without spaces.')
    try:
        p = urlsplit(url)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment:
            raise ValueError()
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError()
    except ValueError:
        raise AgentSettingsError('Use an HTTP(S) model base URL without embedded credentials or query strings.') from None
    try:
        local = ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:
        local = p.hostname == 'localhost'
    if p.scheme != 'https' and not local:
        raise AgentSettingsError('Remote model connections require HTTPS. Plain HTTP is allowed only to loopback.')
    if not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise AgentSettingsError('Provide a model ID of 1–200 printable characters.')
    if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise AgentSettingsError('API key must be printable ASCII without spaces, or blank for a local server.')
    return {'url': url.rstrip('/'), 'model': model.strip(), 'key': key}


def load_agent(directory):
    path = Path(directory) / 'agent.json'
    if path.is_symlink():
        raise AgentSettingsError('Operator settings must not be a symbolic link.')
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise AgentSettingsError('Saved operator settings are too large. Remove agent.json and configure again.')
    try:
        value = validate_agent(json.loads(path.read_text()))
        path.chmod(0o600)
        return value
    except (ValueError, OSError):
        raise AgentSettingsError('Saved operator settings could not be loaded. Remove agent.json and configure again.') from None


def save_agent(directory, value):
    value = validate_agent(value)
    path = Path(directory) / 'agent.json'
    staged = path.with_name('agent-' + os.urandom(8).hex() + '.tmp')
    try:
        fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return value
