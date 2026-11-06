"""Extension seam: registration rules, typed operations, failure isolation, bounded events,
route safety and the private-file helpers. Host-level tests use a fake station."""
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from myradio.extensions import (Arg, Context, Extension, ExtensionError, ExtensionHost, Operation, Panel,
                                Request, Response, Route, StationFacade, migrate)


class FakeStation:
    def status(self):
        return {'ok': True}

    def import_audio(self, data, title, artist='', kind='music'):
        return {'id': 'x', 'title': title}

    def action(self, operation, arguments=None):
        return {'ok': True, 'operation': operation}


def op(name, handler=None, **arguments):
    return Operation(name, f'{name} operation', arguments, handler or (lambda ctx, args: dict(args)))


class Demo(Extension):
    name = 'demo'

    def __init__(self, ops=(), routes=(), panels=(), status=None, fail_start=False, name=None):
        if name:
            self.name = name
        self._ops, self._routes, self._panels = list(ops), list(routes), list(panels)
        self._status = status if status is not None else (lambda: {'ready': True})
        self.fail_start, self.started, self.stopped, self.ctx = fail_start, 0, 0, None

    def start(self, ctx):
        if self.fail_start:
            raise RuntimeError('boom while starting')
        self.started += 1
        self.ctx = ctx

    def stop(self):
        self.stopped += 1

    def operations(self):
        return self._ops

    def routes(self):
        return self._routes

    def panels(self):
        return self._panels

    def status(self):
        return self._status()


class HostCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = ExtensionHost(self.temp.name, StationFacade(FakeStation()))
        self.addCleanup(self.host.stop)

    def boot(self, *extensions):
        for extension in extensions:
            self.host.register(extension)
        self.host.start()
        return extensions[0] if len(extensions) == 1 else extensions


class RegistrationTests(HostCase):
    def test_extension_names_are_validated_and_unique(self):
        for bad in ('Demo', 'd', 'demo_x', '9demo', 'a' * 25):
            with self.subTest(name=bad), self.assertRaises(ExtensionError):
                self.host.register(Demo(name=bad))

        class Nameless(Demo):
            name = ''

        with self.assertRaises(ExtensionError):
            self.host.register(Nameless())
        self.host.register(Demo())
        with self.assertRaisesRegex(ExtensionError, 'already registered'):
            self.host.register(Demo())

    def test_operations_arguments_routes_and_panels_are_validated(self):
        cases = [
            Demo(ops=[op('Bad')]),
            Demo(ops=[op('same'), op('same')]),
            Demo(ops=[Operation('okname', 'd', {'x': Arg('datetime')}, lambda c, a: None)]),
            Demo(ops=[Operation('okname', 'd', {'Bad-Arg': Arg('string')}, lambda c, a: None)]),
            Demo(ops=[Operation('okname', '', {}, lambda c, a: None)]),
            Demo(routes=[Route('DELETE', '/x', lambda c, r: None)]),
            Demo(routes=[Route('GET', '/../x', lambda c, r: None)]),
            Demo(routes=[Route('GET', '/a//b', lambda c, r: None)]),
            Demo(routes=[Route('GET', '/ok', lambda c, r: None, max_body=200 * 1024 * 1024)]),
            Demo(routes=[Route('GET', '/ok', lambda c, r: None), Route('GET', '/ok', lambda c, r: None)]),
            Demo(panels=[Panel('settings', 'Settings', '⚙', 'x.js')]),
            Demo(panels=[Panel('good-one', 'Good', '◍', '../x.js')]),
            Demo(panels=[Panel('good-one', 'Good', '◍', 'x.js'), Panel('good-one', 'Dup', '◍', 'y.js')]),
        ]
        for index, extension in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ExtensionError):
                ExtensionHost(self.temp.name, StationFacade(FakeStation())).register(extension)

    def test_register_or_fail_keeps_a_visible_placeholder(self):
        class Broken(Demo):
            def routes(self):
                raise RuntimeError('boom')

        self.host.register_or_fail(Broken(name='explodes'))
        self.host.register_or_fail(Demo(name='Nope'))
        self.host.register_or_fail(Demo())
        self.host.start()
        states = {name: entry for name, entry in self.host.status().items()}
        self.assertEqual(sorted(e['state'] for e in states.values()), ['failed', 'failed', 'running'])
        self.assertTrue(all(e['error'] for e in states.values() if e['state'] == 'failed'))
        self.assertEqual(self.host.catalog(), [])

    def test_registration_closes_once_started(self):
        self.boot(Demo())
        with self.assertRaisesRegex(ExtensionError, 'before'):
            self.host.register(Demo(name='late'))


class OperationTests(HostCase):
    def test_typed_arguments_are_enforced(self):
        self.boot(Demo(ops=[Operation('typed', 'typed op', {
            's': Arg('string', max_length=5), 'b': Arg('boolean', required=False), 'i': Arg('integer', minimum=1, maximum=10, required=False),
            'n': Arg('number', required=False), 'e': Arg('string', enum=('a', 'b'), required=False),
            'o': Arg('object', required=False), 'l': Arg('array', required=False)}, lambda ctx, args: dict(args))]))
        ok = self.host.call('demo.typed', {'s': 'hello', 'b': True, 'i': 3, 'n': 1.5, 'e': 'a', 'o': {'k': 1}, 'l': [1]})
        self.assertEqual((ok['ok'], ok['operation'], ok['i']), (True, 'demo.typed', 3))
        with self.assertRaisesRegex(ExtensionError, 'too large'):
            self.host.call('demo.typed', {'s': 'x', 'o': {'k': 'y' * 70000}})
        for bad in ({}, {'s': ''}, {'s': '   '}, {'s': 'toolong'}, {'s': 'x', 'extra': 1}, {'s': 1}, {'s': 'x', 'b': 1},
                    {'s': 'x', 'i': True}, {'s': 'x', 'i': 0}, {'s': 'x', 'i': 11}, {'s': 'x', 'i': 1.5},
                    {'s': 'x', 'n': float('nan')}, {'s': 'x', 'n': float('inf')}, {'s': 'x', 'n': True},
                    {'s': 'x', 'e': 'c'}, {'s': 'x', 'o': []}, {'s': 'x', 'l': {}}, {'s': 'x', 'o': {'k': 'y' * 70000}}):
            with self.subTest(args=str(bad)[:40]), self.assertRaises(ExtensionError):
                self.host.call('demo.typed', bad)
        with self.assertRaises(ExtensionError):
            self.host.call('demo.typed', ['not', 'a', 'dict'])

    def test_unknown_and_malformed_operation_names(self):
        self.boot(Demo(ops=[op('echo')]))
        for name in ('demo.nothing', 'other.echo', 'echo', 'demo.echo.extra', '', None, 7):
            with self.subTest(name=name), self.assertRaisesRegex(ExtensionError, 'Unknown'):
                self.host.call(name, {})

    def test_handler_failures_never_leak_internals(self):
        def explode(ctx, args):
            raise RuntimeError('database password hunter2 at /home/secret/path')

        def refuse(ctx, args):
            raise ExtensionError('That show overlaps Morning Rise.')

        self.boot(Demo(ops=[op('explode', explode), op('refuse', refuse), op('scalar', lambda c, a: 5),
                            op('opaque', lambda c, a: {'x': object()})]))
        with self.assertRaises(ExtensionError) as caught:
            self.host.call('demo.explode', {})
        self.assertNotIn('hunter2', str(caught.exception))
        self.assertNotIn('/home/secret', str(caught.exception))
        with self.assertRaisesRegex(ExtensionError, 'overlaps Morning Rise'):
            self.host.call('demo.refuse', {})
        for name in ('demo.scalar', 'demo.opaque'):
            with self.subTest(name=name), self.assertRaisesRegex(ExtensionError, 'failed'):
                self.host.call(name, {})
        self.assertEqual(self.host.status()['demo']['state'], 'running')

    def test_catalog_describes_running_extensions_only(self):
        self.boot(Demo(ops=[Operation('save', 'Save a thing', {'name': Arg('string', 'The name'), 'n': Arg('integer', required=False, minimum=1)}, lambda c, a: None)]),
                  Demo(name='broken', fail_start=True, ops=[op('hidden')]))
        catalog = {entry['name']: entry for entry in self.host.catalog()}
        self.assertEqual(set(catalog), {'demo.save'})
        self.assertEqual(catalog['demo.save']['required'], ['name'])
        self.assertEqual(catalog['demo.save']['arguments']['n'], {'type': 'integer', 'minimum': 1})
        self.assertFalse(self.host.has_operation('broken.hidden'))
        self.assertTrue(self.host.has_operation('demo.save'))
        with self.assertRaisesRegex(ExtensionError, 'Unknown'):
            self.host.call('broken.hidden', {})


class LifecycleAndStatusTests(HostCase):
    def test_a_failing_extension_is_isolated_and_reported(self):
        good, bad = self.boot(Demo(), Demo(name='bad', fail_start=True))
        status = self.host.status()
        self.assertEqual(status['demo']['state'], 'running')
        self.assertEqual(status['bad']['state'], 'failed')
        self.assertIn('boom while starting', status['bad']['error'])
        self.assertEqual(good.started, 1)
        self.host.stop()
        self.host.stop()  # idempotent
        self.assertEqual((good.stopped, bad.stopped), (1, 0))
        self.assertEqual(self.host.status()['demo']['state'], 'stopped')

    def test_state_directory_is_private(self):
        extension = self.boot(Demo())
        mode = stat.S_IMODE(os.stat(extension.ctx.state_dir).st_mode)
        self.assertEqual(mode, 0o700)
        self.assertEqual(extension.ctx.state_dir, Path(self.temp.name) / 'ext' / 'demo')

    def test_status_is_sanitised_bounded_and_cycle_safe(self):
        reentrant = []

        def sneaky():
            reentrant.append(self.host.status())  # an extension misusing station.status()
            return {'ok': True}

        self.boot(Demo(status=lambda: {'x': object()}), Demo(name='huge', status=lambda: {'x': 'y' * 70000}),
                  Demo(name='raises', status=lambda: 1 / 0), Demo(name='sneaky', status=sneaky))
        status = self.host.status()
        self.assertEqual(status['demo']['status'], {})
        self.assertIn('unavailable', status['demo']['error'])
        self.assertEqual(status['huge']['status'], {'truncated': True})
        self.assertIn('unavailable', status['raises']['error'])
        self.assertEqual(reentrant, [{}])
        self.assertEqual(status['sneaky']['status'], {'ok': True})
        json.dumps(status)  # the whole aggregate always serialises

    def test_status_is_a_copy_not_the_extensions_cache(self):
        cache = {'items': [1]}
        self.boot(Demo(status=lambda: cache))
        self.host.status()['demo']['status']['items'].append(99)
        self.assertEqual(cache, {'items': [1]})

    def test_checks_are_validated_and_failures_reported(self):
        class Checker(Demo):
            def checks(self):
                return [{'id': 'ffmpeg', 'title': 'FFmpeg', 'status': 'ok', 'detail': 'found', 'hint': ''},
                        {'id': 'bad', 'title': 'Bad', 'status': 'purple'}, 'nonsense', {'id': 'x'}]

        self.boot(Checker(), Demo(name='broken', fail_start=True))
        checks = {c['id']: c for c in self.host.checks()}
        self.assertEqual(set(checks), {'demo.ffmpeg', 'broken.start'})
        self.assertEqual(checks['broken.start']['status'], 'fail')

    def test_panels_are_sorted_and_only_from_running_extensions(self):
        self.boot(Demo(panels=[Panel('zeta', 'Zeta', '◍', 'zeta.js', 70), Panel('alpha', 'Alpha', '✦', 'alpha.js', 10)]),
                  Demo(name='broken', fail_start=True, panels=[Panel('ghost', 'Ghost', '?', 'ghost.js')]))
        self.assertEqual([p['id'] for p in self.host.panels()], ['alpha', 'zeta'])

    def test_backup_files_lists_only_real_regular_files(self):
        class Persists(Demo):
            def persistent_files(self):
                return ['data.sqlite3', 'missing.json', 'link.json', '../escape', '.hidden', 'notes.txt']

        extension = self.boot(Persists())
        base = extension.ctx.state_dir
        (base / 'data.sqlite3').write_bytes(b'x')
        (base / 'notes.txt').write_text('n')
        (base / 'outside.txt').write_text('o')
        os.symlink(base / 'outside.txt', base / 'link.json')
        names = sorted(p.name for p in self.host.backup_files()['demo'])
        self.assertEqual(names, ['data.sqlite3', 'notes.txt'])


class EventTests(HostCase):
    class Listener(Demo):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.gate, self.received, self.fail_first = threading.Event(), [], False
            self.gate.set()

        def on_event(self, event):
            self.gate.wait(5)
            if self.fail_first and not self.received:
                self.received.append(None)
                raise RuntimeError('listener bug')
            self.received.append(dict(event))  # snapshot as received, before this listener mutates its own copy
            event['mutated'] = True

    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_emit_never_blocks_and_drops_the_oldest(self):
        slow = self.boot(self.Listener())
        slow.gate.clear()
        started = time.monotonic()
        for n in range(600):
            self.host.emit({'type': 'track_started', 'n': n})
        self.assertLess(time.monotonic() - started, 1.0, 'emit must be non-blocking even with a stalled listener')
        self.assertGreaterEqual(self.host.status()['demo']['events_dropped'], 340)
        slow.gate.set()
        self.assertTrue(self.wait_for(lambda: slow.received and slow.received[-1]['n'] == 599))
        self.assertLessEqual(len(slow.received), 257)

    def test_one_extension_cannot_delay_another_and_events_are_copies(self):
        fast, stalled = self.Listener(name='fast'), self.Listener(name='stalled')
        stalled.gate.clear()
        self.boot(fast, stalled)
        self.host.emit({'type': 'transport', 'state': 'started'})
        self.assertTrue(self.wait_for(lambda: len(fast.received) == 1), 'a stalled peer must not hold up delivery')
        time.sleep(0.05)  # let the fast listener finish mutating its own copy
        stalled.gate.set()
        self.assertTrue(self.wait_for(lambda: len(stalled.received) == 1))
        self.assertNotIn('mutated', stalled.received[0], 'each extension must get its own copy of an event')

    def test_listener_exceptions_do_not_kill_delivery(self):
        listener = self.Listener()
        listener.fail_first = True
        self.boot(listener)
        for n in range(3):
            self.host.emit({'n': n})
        self.assertTrue(self.wait_for(lambda: len(listener.received) == 3))
        self.assertEqual(self.host.status()['demo']['state'], 'running')

    def test_stop_joins_listener_threads(self):
        self.boot(self.Listener())
        self.host.emit({'n': 1})
        self.host.stop()
        self.assertEqual([t for t in threading.enumerate() if t.name == 'myradio-ext-demo'], [])


class RouteTests(HostCase):
    def test_route_matching_rejects_traversal_and_unknowns(self):
        self.boot(Demo(routes=[Route('GET', '/jobs', lambda c, r: {'jobs': []}), Route('POST', '/jobs/new', lambda c, r: None)]),
                  Demo(name='broken', fail_start=True, routes=[Route('GET', '/jobs', lambda c, r: {})]))
        self.assertIsNotNone(self.host.find_route('GET', '/api/x/demo/jobs'))
        self.assertIsNotNone(self.host.find_route('POST', '/api/x/demo/jobs/new'))
        for method, path in (('POST', '/api/x/demo/jobs'), ('GET', '/api/x/demo/nothing'), ('GET', '/api/x/nope/jobs'),
                             ('GET', '/api/x/demo/../jobs'), ('GET', '/api/x/demo//jobs'), ('GET', '/api/x/demo/jobs?x=1'),
                             ('GET', '/api/x/broken/jobs'), ('GET', '/api/y/demo/jobs'), ('GET', '/api/x/'), ('GET', '/api/x/DEMO/jobs')):
            with self.subTest(path=path):
                self.assertIsNone(self.host.find_route(method, path))

    def test_dispatch_normalises_results_and_hides_internals(self):
        def explode(ctx, request):
            raise RuntimeError('secret internals')

        def refuse(ctx, request):
            raise ExtensionError('Choose a show first.')

        self.boot(Demo(routes=[
            Route('GET', '/dict', lambda c, r: {'a': 1}), Route('GET', '/none', lambda c, r: None), Route('GET', '/list', lambda c, r: [1, 2]),
            Route('GET', '/raw', lambda c, r: Response(b'abc', 200, 'text/plain')), Route('GET', '/bad', lambda c, r: {'x': object()}),
            Route('GET', '/boom', explode), Route('GET', '/refuse', refuse),
            Route('POST', '/echo', lambda c, r: {'got': r.json, 'q': dict(r.query), 'n': len(r.body)})]))
        request = Request('GET', '/', {}, None, b'')
        call = lambda path, req=request, method='GET': self.host.dispatch_route(self.host.find_route(method, '/api/x/demo' + path), req)
        self.assertEqual((call('/dict').status, call('/dict').body), (200, {'a': 1}))
        self.assertEqual(call('/none').body, {'ok': True})
        self.assertEqual(call('/list').body, [1, 2])
        self.assertEqual((call('/raw').body, call('/raw').content_type), (b'abc', 'text/plain'))
        self.assertEqual(call('/refuse').status, 400)
        self.assertEqual(call('/refuse').body, {'error': 'Choose a show first.'})
        for path in ('/bad', '/boom'):
            result = call(path)
            self.assertEqual(result.status, 500)
            self.assertNotIn('secret', json.dumps(result.body))
        echoed = call('/echo', Request('POST', '/echo', {'a': ['1']}, {'k': 'v'}, b'{"k":"v"}'), 'POST')
        self.assertEqual(echoed.body, {'got': {'k': 'v'}, 'q': {'a': ['1']}, 'n': 9})


class ContextHelperTests(HostCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.boot(Demo()).ctx

    def test_open_db_is_private_wal_and_name_checked(self):
        conn = self.ctx.open_db('state.sqlite3')
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.ctx.state_dir / 'state.sqlite3').st_mode), 0o600)
        for bad in ('../x.sqlite3', 'state.db', 'a/b.sqlite3', '.sqlite3', 'UPPER.sqlite3'):
            with self.subTest(name=bad), self.assertRaises(ExtensionError):
                self.ctx.open_db(bad)
        os.symlink(self.temp.name, self.ctx.state_dir / 'link.sqlite3')
        with self.assertRaisesRegex(ExtensionError, 'symbolic'):
            self.ctx.open_db('link.sqlite3')

    def test_migrate_applies_each_step_once_and_refuses_newer_data(self):
        conn = sqlite3.connect(':memory:')
        steps = ['CREATE TABLE a(x INTEGER);', 'ALTER TABLE a ADD COLUMN y TEXT;']
        self.assertEqual(migrate(conn, steps), 2)
        self.assertEqual(migrate(conn, steps), 2)  # idempotent
        self.assertEqual([r[1] for r in conn.execute('PRAGMA table_info(a)')], ['x', 'y'])
        self.assertEqual(migrate(conn, steps + ['CREATE TABLE b(z);']), 3)
        with self.assertRaisesRegex(ExtensionError, 'newer'):
            migrate(conn, steps)

    def test_config_is_strict_about_what_it_reads(self):
        path = self.ctx.state_dir / 'config.json'
        self.assertEqual(self.ctx.config(), {})
        path.write_text('{"worker": "drama"}')
        self.assertEqual(self.ctx.config(), {'worker': 'drama'})
        path.write_text('[1]')
        with self.assertRaisesRegex(ExtensionError, 'object'):
            self.ctx.config()
        path.write_text('{nope')
        with self.assertRaisesRegex(ExtensionError, 'valid JSON'):
            self.ctx.config()
        path.write_text('{"k": "' + 'x' * 70000 + '"}')
        with self.assertRaisesRegex(ExtensionError, 'too large'):
            self.ctx.config()
        path.unlink()
        outside = Path(self.temp.name) / 'outside.json'
        outside.write_text('{"stolen": true}')
        os.symlink(outside, path)
        with self.assertRaisesRegex(ExtensionError, 'regular file'):
            self.ctx.config()

    def test_private_files_are_atomic_0600_and_symlink_safe(self):
        target = self.ctx.write_private('token.bin', b'secret')
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(self.ctx.read_private('token.bin'), b'secret')
        self.assertIsNone(self.ctx.read_private('absent.bin'))
        self.ctx.write_private('token.bin', b'newer')
        self.assertEqual(self.ctx.read_private('token.bin'), b'newer')
        self.assertEqual([p.name for p in self.ctx.state_dir.iterdir() if p.name.endswith('.tmp')], [])
        for bad in ('../x', '.hidden', 'a/b', ''):
            with self.subTest(name=bad), self.assertRaises(ExtensionError):
                self.ctx.write_private(bad, b'x')
        victim = Path(self.temp.name) / 'victim.txt'
        victim.write_text('keep')
        os.symlink(victim, self.ctx.state_dir / 'trap.bin')
        with self.assertRaisesRegex(ExtensionError, 'symbolic'):
            self.ctx.write_private('trap.bin', b'overwrite')
        with self.assertRaisesRegex(ExtensionError, 'regular'):
            self.ctx.read_private('trap.bin')
        self.assertEqual(victim.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
