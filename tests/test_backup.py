"""Offline backup/restore round trips and malicious/corrupt archive rejection."""
import io
import datetime as dt
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import wave
import zipfile

from myradio.backup import BackupError, create_backup, restore_backup
from myradio.server import load_token
from myradio.station import FRAMES, Station


def tone():
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<h', 1000) * FRAMES * 3)
    return out.getvalue()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.archive = self.root / 'station.myradio.zip'
        self.target = self.root / 'restored'
        s = Station(self.source, start_worker=False)
        try:
            self.track = s.import_audio(tone(), 'Portable track', 'Presenter', 'music')
            s.action('station.update', {'name': 'Portable FM'})
            s.action('queue.add', {'track_id': self.track['id']})
            s.action('program.add', {'track_id': self.track['id'], 'timezone': 'America/New_York', 'local_time': '12:34', 'weekdays': '1,3,5'})
            s.action('schedule.add', {'track_id': self.track['id'], 'at': (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=7)).isoformat()})
            s.broadcast.configure({'url': 'http://127.0.0.1:1/private.mp3', 'username': 'source', 'password': 'private-broadcast-secret'})
            self.old_token = load_token(self.source)
            s.action('transport.start')
            s.tick()
            # Simulate a service shutdown with an in-progress marker, not Stop.
        finally:
            s.close()

    def tearDown(self):
        self.temp.cleanup()

    def rewrite(self, *, mutate=None, extra=None, corrupt=False, symlink=False):
        create_backup(self.source, self.archive)
        with zipfile.ZipFile(self.archive) as z:
            payload = {i.filename: z.read(i) for i in z.infolist()}
        manifest = json.loads(payload['station.json'])
        if mutate:
            mutate(manifest)
        payload['station.json'] = json.dumps(manifest).encode()
        media = next(name for name in payload if name.startswith('media/'))
        if corrupt:
            value = bytearray(payload[media]); value[-1] ^= 1; payload[media] = value
        if extra:
            payload[extra] = b'not allowed'
        with zipfile.ZipFile(self.archive, 'w') as z:
            for name, value in payload.items():
                if symlink and name == media:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, value)
                else:
                    z.writestr(name, value)

    def test_roundtrip_private_and_stopped_with_recovery(self):
        result = create_backup(self.source, self.archive)
        self.assertEqual(result['tracks'], 1)
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        with zipfile.ZipFile(self.archive) as z:
            self.assertEqual(set(z.namelist()), {'station.json', 'media/' + self.track['id'] + '.wav'})
            manifest = z.read('station.json')
            for secret in (self.old_token, 'private-broadcast-secret', 'private.mp3'):
                self.assertNotIn(secret.encode(), manifest)
        result = restore_backup(self.archive, self.target)
        self.assertEqual(result['tracks'], 1)
        self.assertEqual(result['path'], str(self.target))
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.target / 'control.token').exists())
        self.assertFalse((self.target / 'broadcast.json').exists())
        self.assertNotEqual(load_token(self.target), self.old_token)
        s = Station(self.target, start_worker=False)
        try:
            status = s.status()
            self.assertEqual(status['station']['name'], 'Portable FM')
            self.assertFalse(status['running'])
            self.assertFalse(status['broadcast']['configured'])
            self.assertEqual(status['library'][0]['id'], self.track['id'])
            self.assertEqual(status['queue'][0]['track_id'], self.track['id'])
            self.assertEqual(status['programs'][0]['weekdays'], '1,3,5')
            self.assertEqual(status['schedules'][0]['status'], 'pending')
            self.assertTrue(status['history'])
            s.action('transport.start')
            block = s.tick()
            self.assertEqual(block, struct.pack('<hh',1000,1000)*FRAMES)
            self.assertEqual(s.current['id'], self.track['id'])
        finally:
            s.close()
        self.assertEqual(list(self.root.glob('.myradio-restore-*')), [])
        self.assertEqual(list(self.root.glob('.myradio-backup-*')), [])

    def test_refuse_running_station_overwrites_and_missing_media(self):
        s = Station(self.source, start_worker=False)
        try:
            with self.assertRaisesRegex(BackupError, 'Stop the station'):
                create_backup(self.source, self.archive)
        finally:
            s.close()
        create_backup(self.source, self.archive)
        before = self.archive.read_bytes()
        with self.assertRaises(BackupError):
            create_backup(self.source, self.archive)
        self.assertEqual(self.archive.read_bytes(), before)
        self.target.mkdir()
        sentinel = self.target / 'existing'; sentinel.write_text('keep')
        with self.assertRaises(BackupError):
            restore_backup(self.archive, self.target)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.archive.unlink()
        (self.source / 'media' / (self.track['id']+'.wav')).unlink()
        with self.assertRaises(BackupError):
            create_backup(self.source, self.archive)
        self.assertFalse(self.archive.exists())

    def test_checksum_mismatch_cleans_unpublished_restore(self):
        self.rewrite(corrupt=True)
        with self.assertRaisesRegex(BackupError, 'checksum'):
            restore_backup(self.archive, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob('.myradio-restore-*')), [])

    def test_archive_path_traversal_and_symlink_are_rejected(self):
        for extra in ('../escape', '/absolute', 'media/../escape', 'control.token', 'broadcast.json'):
            with self.subTest(extra=extra):
                if self.archive.exists(): self.archive.unlink()
                self.rewrite(extra=extra)
                with self.assertRaises(BackupError):
                    restore_backup(self.archive, self.target)
                self.assertFalse(self.target.exists())
                self.assertFalse((self.root/'escape').exists())
        self.archive.unlink(); self.rewrite(symlink=True)
        with self.assertRaisesRegex(BackupError, 'symbolic'):
            restore_backup(self.archive, self.target)
        self.assertFalse(self.target.exists())

    def test_metadata_foreign_keys_sizes_and_settings_rejected(self):
        mutators = [
            lambda m: m['tables']['settings'].append({'key':'secret','value':'"x"'}),
            lambda m: m['tables']['tracks'][0].update(filename='../escape.wav'),
            lambda m: m['tables']['tracks'][0].update(duration=float('inf')),
            lambda m: m['tables']['queue'].append({'id':100,'track_id':'f'*32}),
            lambda m: m['tables']['programs'][0].update(timezone='Not/AZone'),
            lambda m: m['tables']['schedules'][0].update(due=1),
            lambda m: next(iter(m['files'].values())).update(size=9999999999),
            lambda m: m.update(format='unsupported'),
        ]
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                if self.archive.exists(): self.archive.unlink()
                self.rewrite(mutate=mutate)
                with self.assertRaises((BackupError, ValueError)):
                    restore_backup(self.archive, self.target)
                self.assertFalse(self.target.exists())
                self.assertEqual(list(self.root.glob('.myradio-restore-*')), [])

    def test_cli_roundtrip_and_invalid_archive_return_codes(self):
        import subprocess
        import sys
        made = subprocess.run([sys.executable,'-m','myradio.backup','create','--data-dir',str(self.source),str(self.archive)],capture_output=True,text=True,timeout=10)
        self.assertEqual(made.returncode,0,made.stderr)
        self.assertEqual(json.loads(made.stdout)['tracks'],1)
        restored = subprocess.run([sys.executable,'-m','myradio.backup','restore',str(self.archive),'--data-dir',str(self.target)],capture_output=True,text=True,timeout=10)
        self.assertEqual(restored.returncode,0,restored.stderr)
        self.assertIn('Broadcast credentials must be configured again',restored.stdout)
        bad=self.root/'bad.zip';bad.write_bytes(b'not a zip')
        invalid = subprocess.run([sys.executable,'-m','myradio.backup','restore',str(bad),'--data-dir',str(self.root/'invalid')],capture_output=True,text=True,timeout=10)
        self.assertEqual(invalid.returncode,1)
        self.assertNotIn('Traceback',invalid.stderr)
        self.assertFalse((self.root/'invalid').exists())
        self.assertEqual(list(self.root.glob('.myradio-restore-*')),[])

    def test_publish_race_does_not_overwrite_other_destination(self):
        from unittest.mock import patch
        from myradio.backup import publish_directory
        create_backup(self.source, self.archive)
        def race(source, destination):
            destination.mkdir()
            (destination/'keep').write_text('untouched')
            publish_directory(source,destination)
        with patch('myradio.backup.publish_directory',side_effect=race):
            with self.assertRaises(OSError):
                restore_backup(self.archive,self.target)
        self.assertEqual((self.target/'keep').read_text(),'untouched')
        self.assertEqual(list(self.root.glob('.myradio-restore-*')), [])


if __name__ == '__main__':
    unittest.main()
