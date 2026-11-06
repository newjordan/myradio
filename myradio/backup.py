"""Offline portable backups: allowlisted data, no credentials, no SQL from archives."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import sys
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import wave
import zipfile

from .scheduling import recurrence
from .station import SCHEMA, MAX_UPLOAD, RATE, WIDTH, CHANNELS, FRAMES

TABLES = {
    'settings': ('key', 'value'),
    'tracks': ('id', 'title', 'artist', 'duration', 'kind', 'filename'),
    'queue': ('id', 'track_id'),
    'schedules': ('id', 'track_id', 'at', 'due', 'status'),
    'history': ('id', 'title', 'at', 'event'),
    'programs': ('id', 'track_id', 'timezone', 'local_time', 'weekdays', 'next_due', 'last_due', 'last_status'),
}
MAX_MANIFEST = 16 * 1024 * 1024
MAX_ROWS = 100000
MAX_TRACKS = 10000
MAX_TOTAL = 20 * 1024 ** 3


class BackupError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def string(value, limit, *, empty=False):
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))


def validate(data):
    """Type-check data before binding it into a schema created by our own code."""
    if not isinstance(data, dict) or set(data) != set(TABLES):
        raise BackupError('Backup has unsupported station tables.')
    for table, columns in TABLES.items():
        rows = data[table]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise BackupError('Backup table is too large or invalid.')
        if any(not isinstance(row, dict) or set(row) != set(columns) for row in rows):
            raise BackupError('Backup rows do not match the supported schema.')
    settings = {}
    for row in data['settings']:
        if row['key'] not in ('name', 'tagline', 'auto_fill', 'playing') or row['key'] in settings:
            raise BackupError('Unknown or duplicate station setting.')
        if not string(row['value'], 4096):
            raise BackupError('Invalid station setting.')
        try:
            settings[row['key']] = json.loads(row['value'])
        except ValueError:
            raise BackupError('Invalid station setting JSON.') from None
    if not string(settings.get('name'), 200) or not string(settings.get('tagline'), 200) or type(settings.get('auto_fill')) is not bool:
        raise BackupError('Required station identity settings are missing or invalid.')
    tracks = {}
    if len(data['tracks']) > MAX_TRACKS:
        raise BackupError('Backup has too many tracks.')
    for row in data['tracks']:
        track_id = row['id']
        if not isinstance(track_id, str) or not re.fullmatch('[a-f0-9]{32}', track_id) or track_id in tracks:
            raise BackupError('Invalid or duplicate track ID.')
        if row['filename'] != track_id + '.wav' or not string(row['title'], 200) or not string(row['artist'], 200, empty=True):
            raise BackupError('Invalid track metadata or media path.')
        if row['kind'] not in ('music', 'speech', 'ad') or not finite(row['duration']) or not .1 <= row['duration'] <= MAX_UPLOAD / (RATE * WIDTH * CHANNELS):
            raise BackupError('Invalid track kind or duration.')
        tracks[track_id] = row
    if settings.get('playing') is not None and settings['playing'] not in tracks:
        raise BackupError('Playing marker refers to missing audio.')
    for table in ('queue', 'schedules', 'programs', 'history'):
        ids = set()
        for row in data[table]:
            key = row['id']
            if table in ('queue', 'history'):
                good_id = type(key) is int and -(2**63) < key < 2**63
            else:
                good_id = isinstance(key, str) and re.fullmatch('[a-f0-9]{32}', key)
            if not good_id or key in ids:
                raise BackupError('Invalid or duplicate row ID.')
            ids.add(key)
            if table != 'history' and row['track_id'] not in tracks:
                raise BackupError('A queue or schedule references missing audio.')
            if table == 'schedules':
                if not finite(row['due']) or row['status'] not in ('pending', 'queued', 'missed', 'cancelled') or not string(row['at'], 100):
                    raise BackupError('Invalid one-shot schedule.')
                try:
                    at = dt.datetime.fromisoformat(row['at'])
                    if at.tzinfo is None or abs(at.timestamp() - row['due']) > 1:
                        raise ValueError()
                except (ValueError, OverflowError, OSError):
                    raise BackupError('Invalid schedule time.') from None
            elif table == 'programs':
                try:
                    recurrence(row['timezone'], row['local_time'], row['weekdays'])
                    if not finite(row['next_due']) or (row['last_due'] is not None and not finite(row['last_due'])) or row['last_status'] not in (None, 'queued', 'missed'):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise BackupError('Invalid recurring program.') from None
            elif table == 'history':
                if not string(row['title'], 200) or not string(row['at'], 100) or not string(row['event'], 100):
                    raise BackupError('Invalid history row.')
    return tracks


def validate_media(path, track):
    if path.is_symlink() or not path.is_file() or not 44 <= path.stat().st_size <= MAX_UPLOAD + 4096:
        raise BackupError('A referenced media file is missing or outside size limits.')
    try:
        with wave.open(str(path), 'rb') as f:
            frames = f.getnframes()
            if (f.getframerate(), f.getsampwidth(), f.getnchannels(), f.getcomptype()) != (RATE, WIDTH, CHANNELS, 'NONE') or frames < FRAMES or frames * WIDTH * CHANNELS > MAX_UPLOAD:
                raise BackupError('Backup contains unsupported media.')
            if abs(frames / RATE - track['duration']) > 1 / RATE:
                raise BackupError('Media duration does not match station metadata.')
            remaining = frames * WIDTH * CHANNELS
            while remaining:
                block = f.readframes(min(16384, remaining // 4))
                if not block:
                    raise BackupError('Backup contains truncated audio.')
                remaining -= len(block)
    except (wave.Error, EOFError):
        raise BackupError('Backup contains invalid WAV audio.') from None


@contextlib.contextmanager
def offline_lock(directory):
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir() or not (directory / 'station.sqlite3').is_file():
        raise BackupError('Choose an existing station data directory.')
    lock = directory / 'station.lock'
    if lock.is_symlink() or (directory / 'station.sqlite3').is_symlink() or (directory / 'media').is_symlink():
        raise BackupError('Station data paths must not be symbolic links.')
    with lock.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('Stop the station service before making a backup.') from None
        try:
            yield directory
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def create_backup(directory, destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError('Backup destination already exists; nothing was overwritten.')
    with offline_lock(directory) as directory:
        with contextlib.closing(sqlite3.connect((directory / 'station.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                raise BackupError('Station database failed integrity checks.')
            data = {table: [dict(row) for row in db.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + ' LIMIT ' + str(MAX_ROWS + 1))] for table, columns in TABLES.items()}
        tracks = validate(data)
        files = {}
        total = 0
        for row in tracks.values():
            path = directory / 'media' / row['filename']
            validate_media(path, row)
            size = path.stat().st_size
            total += size
            if total > MAX_TOTAL:
                raise BackupError('Station exceeds the 20 GiB portable-backup limit.')
            files['media/' + row['filename']] = {'size': size, 'sha256': digest(path)}
        manifest = json.dumps({'format': 'myradio-backup/v1', 'tables': data, 'files': files}, allow_nan=False).encode()
        if len(manifest) > MAX_MANIFEST:
            raise BackupError('Station metadata exceeds the backup limit.')
        fd, temporary = tempfile.mkstemp(prefix='.myradio-backup-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'w+b') as stream:
                with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr('station.json', manifest)
                    for name in files:
                        archive.write(directory / name, name)
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic no-clobber publication; never replace an older backup.
            os.link(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {'tracks': len(tracks), 'media_bytes': total, 'path': str(destination)}


def publish_directory(source, destination):
    """Atomic no-clobber directory rename on the supported POSIX platforms."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        result = libc.renamex_np(os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise BackupError('This platform lacks a supported atomic no-clobber directory rename.')
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def restore_backup(archive_path, destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError('Restore needs a new, nonexistent data directory. Existing data is never replaced.')
    final_destination = destination
    destination = Path(tempfile.mkdtemp(prefix='.myradio-restore-', dir=destination.parent))
    published = False
    try:
        with (destination / 'station.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_TRACKS + 1 or len({i.filename for i in entries}) != len(entries):
                    raise BackupError('Too many or duplicate archive entries.')
                by_name = {i.filename: i for i in entries}
                entry = by_name.get('station.json')
                if not entry or entry.file_size > MAX_MANIFEST or entry.flag_bits & 1 or ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                    raise BackupError('Missing, oversized or encrypted station manifest.')
                def reject_constant(value):
                    raise BackupError('Nonfinite number in backup.')
                manifest = json.loads(archive.read(entry), parse_constant=reject_constant)
                if not isinstance(manifest, dict) or set(manifest) != {'format', 'tables', 'files'} or manifest['format'] != 'myradio-backup/v1':
                    raise BackupError('Unsupported backup format.')
                tracks = validate(manifest['tables'])
                files = manifest['files']
                wanted = {'media/' + row['filename'] for row in tracks.values()}
                if not isinstance(files, dict) or set(files) != wanted or set(by_name) != wanted | {'station.json'}:
                    raise BackupError('Archive entries do not match the manifest; extra paths are not allowed.')
                total = 0
                for name, meta in files.items():
                    info = by_name[name]
                    if not isinstance(meta, dict) or set(meta) != {'size', 'sha256'} or type(meta['size']) is not int or not 44 <= meta['size'] <= MAX_UPLOAD + 4096 or info.file_size != meta['size']:
                        raise BackupError('Invalid media size in backup.')
                    if not isinstance(meta['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', meta['sha256']):
                        raise BackupError('Invalid checksum in backup.')
                    if info.flag_bits & 1 or ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        raise BackupError('Encrypted or symbolic-link archive entries are forbidden.')
                    total += meta['size']
                    if total > MAX_TOTAL:
                        raise BackupError('Backup exceeds the 20 GiB restore limit.')
                (destination / 'media').mkdir(mode=0o700)
                for row in tracks.values():
                    name = 'media/' + row['filename']
                    path = destination / name
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    h = hashlib.sha256()
                    size = 0
                    with os.fdopen(fd, 'wb') as target, archive.open(name) as source:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            size += len(block)
                            if size > files[name]['size']:
                                raise BackupError('Expanded media exceeds its declared size.')
                            h.update(block)
                            target.write(block)
                        target.flush()
                        os.fsync(target.fileno())
                    if size != files[name]['size'] or h.hexdigest() != files[name]['sha256']:
                        raise BackupError('Media checksum mismatch; restore aborted.')
                    validate_media(path, row)
            database = destination / 'station.sqlite3'
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.executescript(SCHEMA)
                db.execute('BEGIN')
                for table, columns in TABLES.items():
                    statement = 'INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                    db.executemany(statement, ([row[c] for c in columns] for row in manifest['tables'][table]))
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise BackupError('Restored references failed integrity checks.')
                db.commit()
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise BackupError('Restored database failed integrity checks.')
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            database.chmod(0o600)
            publish_directory(destination, final_destination)
            published = True
        return {'tracks': len(tracks), 'media_bytes': total, 'path': str(final_destination)}
    finally:
        if not published:
            import shutil
            shutil.rmtree(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline myradio backup/restore. Credentials are never included. Stop the service first.')
    actions = parser.add_subparsers(dest='action', required=True)
    backup = actions.add_parser('create', help='Save a portable backup to a new archive')
    backup.add_argument('--data-dir', required=True)
    backup.add_argument('archive')
    restore = actions.add_parser('restore', help='Restore into a new nonexistent data directory')
    restore.add_argument('archive')
    restore.add_argument('--data-dir', required=True)
    args = parser.parse_args(argv)
    try:
        result = create_backup(args.data_dir, args.archive) if args.action == 'create' else restore_backup(args.archive, args.data_dir)
    except (BackupError, OSError, ValueError, TypeError, sqlite3.Error, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        parser.exit(1, f'myradio backup: {exc}\n')
    print(json.dumps(result))
    if args.action == 'restore':
        print('Restored offline. Launch with this data directory; unlock credentials will be regenerated. Broadcast credentials must be configured again. Playout and broadcast stay stopped.')


if __name__ == '__main__':
    main()
