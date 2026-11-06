"""Address validation and the bounded resolver: what the research fetcher may and may not connect to."""
import ipaddress
import random
import threading
import time
import unittest
from unittest import mock

from myradio.research.errors import FetchError
from myradio.research.netguard import Resolver, check_addresses, classify_ip, parse_target
from test_research_support import research_threads, wait_until


def kind_of(url, **kw):
    try:
        parse_target(url, **kw)
    except FetchError as error:
        return error.kind
    return None


class ParseTargetTests(unittest.TestCase):
    def test_ordinary_https_addresses_are_accepted_and_normalised(self):
        cases = {
            'https://example.org/feed?x=1': ('example.org', 443, 'https://example.org/feed?x=1'),
            'example.org/feed': ('example.org', 443, 'https://example.org/feed'),
            'feed://example.org/rss': ('example.org', 443, 'https://example.org/rss'),
            '//example.org/a': ('example.org', 443, 'https://example.org/a'),
            'https://Example.ORG./a': ('example.org', 443, 'https://example.org/a'),
            'https://example.org:443/a': ('example.org', 443, 'https://example.org/a'),
            'https://b\u00fccher.example/feed': ('xn--bcher-kva.example', 443, 'https://xn--bcher-kva.example/feed'),
            'https://example.org': ('example.org', 443, 'https://example.org/'),
            'https://example.org/a%20b?q=a%20b': ('example.org', 443, 'https://example.org/a%20b?q=a%20b'),
        }
        for url, (host, port, normalised) in cases.items():
            with self.subTest(url=url):
                target = parse_target(url)
                self.assertEqual((target.host, target.port), (host, port))
                if normalised:
                    self.assertEqual(target.url, normalised)

    def test_public_ip_literals_are_parsed_but_still_need_the_address_check(self):
        target = parse_target('https://93.184.216.34/x')
        self.assertTrue(target.is_ip)
        self.assertEqual(target.host_header, '93.184.216.34')
        v6 = parse_target('https://[2606:4700:4700::1111]/x')
        self.assertEqual((v6.is_ip, v6.host_header), (True, '[2606:4700:4700::1111]'))

    def test_only_https_on_the_standard_port(self):
        self.assertEqual(kind_of('http://example.org/'), 'not_https')
        for scheme in ('ftp', 'file', 'gopher', 'ws', 'data', 'javascript'):
            with self.subTest(scheme=scheme):
                self.assertIn(kind_of(f'{scheme}://example.org/x'), ('not_https', 'bad_url'))
        self.assertEqual(kind_of('file:///etc/passwd'), 'not_https')
        self.assertEqual(kind_of('https://example.org:8443/'), 'bad_port')
        self.assertEqual(kind_of('https://example.org:80/'), 'bad_port')
        self.assertEqual(kind_of('https://example.org:99999/'), 'bad_url')
        self.assertEqual(kind_of('https://example.org:abc/'), 'bad_url')

    def test_credentials_whitespace_controls_and_odd_delimiters_are_refused(self):
        bad = ['https://user:pw@example.org/', 'https://user@example.org/', 'https://example.org\\@evil.example/',
               'https://ex ample.org/', 'https://example.org/\r\nHost: evil', 'https://example.org/a\x00b', 'https://example.org/<x>',
               'https://example.org/"x"', 'https://example.org/a|b', '', '   ', None, 123, 'https://', 'https:///path',
               'https://example.org/' + 'a' * 3000, 'https://[fe80::1%25eth0]/', 'https://-bad.example/', 'https://a..b/',
               'https://' + 'a' * 64 + '.example/', 'https://under score!.example/']
        for url in bad:
            with self.subTest(url=str(url)[:40]):
                self.assertIsNotNone(kind_of(url))

    def test_internal_and_single_label_names_are_refused(self):
        for host in ('localhost', 'LOCALHOST', 'localhost.', 'foo.localhost', 'printer.local', 'nas.lan', 'router.home.arpa', 'db.internal',
                     'metadata.google.internal', 'intranet', 'server', 'x.corp', 'a.b.localdomain', 'abc.onion', '1.0.0.127.in-addr.arpa'):
            with self.subTest(host=host):
                self.assertIn(kind_of(f'https://{host}/feed'), ('blocked_name', 'bad_url'))

    def test_odd_spellings_of_loopback_never_get_as_far_as_resolution(self):
        # Some resolvers read these as 127.0.0.1; none of them is a canonical IP literal or a real name.
        for host in ('127.1', '2130706433', '0x7f.1', '0x7f000001', '0177.0.0.1', '017700000001', '127.0.1', '1.2.3', '0.0', '4294967295',
                     '127.0.0.0x1', '1e3.example.1'):
            with self.subTest(host=host):
                self.assertIn(kind_of(f'https://{host}/feed'), ('bad_url', 'blocked_name'))

    def test_fixture_mode_allows_loopback_http_on_any_port_but_nothing_else_new(self):
        target = parse_target('http://127.0.0.1:8080/x', fixture=True)
        self.assertEqual((target.scheme, target.port, target.host_header), ('http', 8080, '127.0.0.1:8080'))
        self.assertEqual(parse_target('http://localhost:9/x', fixture=True).host, 'localhost')
        self.assertEqual(kind_of('http://intranet/x', fixture=True), 'not_https')
        self.assertEqual(kind_of('https://intranet/x', fixture=True), 'blocked_name')
        self.assertEqual(kind_of('https://user@example.org/', fixture=True), 'bad_url')
        self.assertEqual(kind_of('http://example.org:8080/'), 'not_https')    # production mode is unaffected
        # ... and fixture mode relaxes nothing for a host that is not loopback: plain http and odd ports stay refused
        self.assertEqual(kind_of('http://example.org/feed', fixture=True), 'not_https')
        self.assertEqual(kind_of('http://93.184.216.34:8080/feed', fixture=True), 'not_https')
        self.assertEqual(kind_of('https://example.org:8443/feed', fixture=True), 'bad_port')
        self.assertEqual(kind_of('https://93.184.216.34:8443/feed', fixture=True), 'bad_port')
        self.assertIsNone(kind_of('https://example.org/feed', fixture=True))


class AddressFuzzTests(unittest.TestCase):
    def test_whatever_is_accepted_is_a_plain_https_address_on_the_standard_port(self):
        import re
        rng = random.Random(99)
        pieces = ['https://', 'http://', '//', 'feed://', 'example.org', 'a.b.c', '127.0.0.1', '[::1]', '10.0.0.1', '0x7f', '2130706433', ':443', ':80', ':99999', '@',
                  '/', '?', '#', '%00', '%2e', '\\', ' ', '\t', '.', '..', '-', '_', '\u00fc', '\u202e', 'localhost', 'x' * 70, 'a', '1']
        accepted = 0
        for _ in range(4000):
            text = ''.join(rng.choice(pieces) for _ in range(rng.randint(1, 7)))
            try:
                target = parse_target(text)
            except FetchError:
                continue
            accepted += 1
            self.assertEqual((target.scheme, target.port), ('https', 443), text)
            self.assertRegex(target.url, r'^https://[^\s\\<>"`{}|^]+$')
            self.assertNotIn('@', target.url.split('://', 1)[1].split('/', 1)[0])        # no user name or password in the host part
            self.assertTrue(target.is_ip or re.fullmatch(r'[a-z0-9_.-]+', target.host), text)
            if target.is_ip:
                self.assertIsNotNone(__import__('ipaddress').ip_address(target.host))
            else:
                self.assertIn('.', target.host)
                self.assertRegex(target.host.rsplit('.', 1)[1], r'^[a-z]')
        self.assertGreater(accepted, 20)


class ClassifyAddressTests(unittest.TestCase):
    def test_public_addresses_pass(self):
        for value in ('8.8.8.8', '1.1.1.1', '93.184.216.34', '2606:4700:4700::1111', '2001:4860:4860::8888', '::ffff:8.8.8.8',
                      '2a00:1450:4001:81b::200e'):
            with self.subTest(address=value):
                self.assertIsNone(classify_ip(value))

    def test_loopback_private_link_local_multicast_and_reserved_are_blocked(self):
        blocked = ['127.0.0.1', '127.255.255.254', '10.0.0.1', '10.255.255.255', '172.16.0.1', '172.31.255.255', '192.168.1.1', '169.254.1.1',
                   '100.64.0.1', '100.127.255.254', '0.0.0.0', '0.1.2.3', '224.0.0.1', '239.255.255.250', '240.0.0.1', '255.255.255.255',
                   '192.0.0.1', '192.0.2.1', '198.18.0.1', '198.19.255.255', '198.51.100.7', '203.0.113.9', '192.88.99.1',
                   '::1', '::', 'fe80::1', 'fc00::1', 'fd12:3456::1', 'ff02::1', '2001:db8::1', '2002::1', '2001::1', '3fff::1',
                   '64:ff9b::1', 'fec0::1', '100::1', '5f00::1', '::2']
        for value in blocked:
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value))

    def test_the_ipv6_allow_list_and_special_ranges_hold_without_help_from_the_standard_libraries_flags(self):
        """Python's own is_private / is_reserved / is_multicast flags currently cover everything outside the global range, which hides
        a missing allow-list. Defence in depth means it must stand alone: with those flags silenced, space outside 2000::/3 and the
        special-purpose blocks inside it are still refused, and real global addresses still pass."""
        flags = ('is_multicast', 'is_private', 'is_global', 'is_reserved', 'is_link_local', 'is_site_local', 'is_unspecified')
        blocked = ['fe80::1', 'fc00::1', 'fd12:3456::1', 'ff02::1', '100::1', '5f00::1', '::2', '8000::1', 'c000::1', 'e000::1', '1::1',
                   '2001:db8::1', '2002::1', '2001::1', '3fff::1', '2001:1::1']
        patches = [mock.patch.object(ipaddress.IPv6Address, name, new_callable=mock.PropertyMock, return_value=(name == 'is_global')) for name in flags]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertFalse(ipaddress.ip_address('fe80::1').is_link_local)                 # the silencing worked
        for value in blocked:
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value))
        for value in ('2606:4700:4700::1111', '2001:4860:4860::8888', '2a00:1450:4001:81b::200e'):
            with self.subTest(address=value):
                self.assertIsNone(classify_ip(value))

    def test_cloud_metadata_addresses_are_blocked(self):
        for value in ('169.254.169.254', '169.254.170.2', '100.100.100.200', '192.0.0.192', '168.63.129.16', 'fd00:ec2::254'):
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value))

    def test_ipv4_wrapped_in_ipv6_is_judged_by_the_ipv4_address(self):
        for value in ('::ffff:127.0.0.1', '::ffff:10.1.2.3', '::ffff:169.254.169.254', '::ffff:7f00:1', '0:0:0:0:0:ffff:192.168.0.1'):
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value))
        self.assertIsNone(classify_ip('::ffff:8.8.8.8'))

    def test_translation_and_tunnel_prefixes_that_can_reach_ipv4_are_blocked(self):
        for value in ('64:ff9b::7f00:1', '64:ff9b::a00:1', '2002:7f00:1::', '2002:a00:1::1', '2001:0:4136:e378:8000:63bf:3fff:fdd2', '::7f00:1', '::a00:1'):
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value))

    def test_scoped_and_unparseable_values_are_blocked(self):
        for value in ('fe80::1%eth0', '2606:4700:4700::1111%eth0', 'not-an-ip', '', '999.1.1.1', '1.2.3'):
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value))

    def test_loopback_allowance_is_only_for_loopback(self):
        self.assertIsNone(classify_ip('127.0.0.1', allow_loopback=True))
        self.assertIsNone(classify_ip('::1', allow_loopback=True))
        self.assertIsNone(classify_ip('::ffff:127.0.0.1', allow_loopback=True))
        for value in ('10.0.0.1', '169.254.169.254', '192.168.0.1', 'fe80::1', '224.0.0.1', '::'):
            with self.subTest(address=value):
                self.assertIsNotNone(classify_ip(value, allow_loopback=True))

    def test_check_addresses_refuses_the_whole_name_when_any_answer_is_not_public(self):
        with self.assertRaises(FetchError) as caught:
            check_addresses('mixed.example', ['93.184.216.34', '10.0.0.5'])
        self.assertEqual(caught.exception.kind, 'blocked_address')
        with self.assertRaises(FetchError):
            check_addresses('mixed.example', ['10.0.0.5', '93.184.216.34'])
        with self.assertRaises(FetchError):
            check_addresses('mixed.example', ['2606:4700:4700::1111', '::1'])
        self.assertEqual(check_addresses('ok.example', ['93.184.216.34', '93.184.216.34', '2606:4700:4700::1111']),
                         ['93.184.216.34', '2606:4700:4700::1111'])
        crowd = [f'93.184.216.{n}' for n in range(1, 21)]
        self.assertEqual(len(check_addresses('many.example', crowd)), 8)
        with self.assertRaises(FetchError):                                    # a private answer hidden after many public ones is still found
            check_addresses('many.example', crowd + ['10.0.0.5'])
        with self.assertRaises(FetchError) as empty:
            check_addresses('none.example', [])
        self.assertEqual(empty.exception.kind, 'dns')

    def test_error_messages_are_plain_language(self):
        with self.assertRaises(FetchError) as caught:
            check_addresses('evil.example', ['169.254.169.254'])
        text = caught.exception.message
        self.assertIn('evil.example', text)
        for jargon in ('SSRF', 'PCM', 'GGUF', 'Traceback', 'metadata', '169.254'):
            self.assertNotIn(jargon, text)


class SystemResolverTests(unittest.TestCase):
    def test_the_default_resolver_answers_with_plain_address_strings_and_they_are_judged(self):
        from myradio.research.netguard import system_resolver
        try:
            answers = system_resolver('localhost', 443)          # answered from this computer's own host table, no network needed
        except OSError:
            self.skipTest('this computer cannot look up localhost')
        self.assertTrue(answers)
        self.assertTrue(all(isinstance(a, str) for a in answers))
        with self.assertRaises(FetchError) as caught:
            check_addresses('localhost', answers)
        self.assertEqual(caught.exception.kind, 'blocked_address')
        self.assertEqual(Resolver().lookup('localhost', 443, time.monotonic() + 5, threading.Event()), answers)


class ResolverTests(unittest.TestCase):
    def tearDown(self):
        self.assertTrue(wait_until(lambda: not [t for t in research_threads() if t.name == 'myradio-research-dns'], 3))

    def lookup(self, resolver, timeout=1.0, cancel=None):
        return resolver.lookup('x.example', 443, time.monotonic() + timeout, cancel or threading.Event())

    def test_lookup_returns_addresses_and_translates_failures(self):
        self.assertEqual(self.lookup(Resolver(lambda h, p: ['93.184.216.34'])), ['93.184.216.34'])
        with self.assertRaises(FetchError) as caught:
            self.lookup(Resolver(lambda h, p: (_ for _ in ()).throw(OSError('no such host'))))
        self.assertEqual(caught.exception.kind, 'dns')
        self.assertNotIn('OSError', caught.exception.message)

    def test_a_stuck_lookup_is_abandoned_at_the_deadline_and_cleans_itself_up(self):
        release = threading.Event()
        resolver = Resolver(lambda h, p: release.wait(10) and ['93.184.216.34'], max_threads=2)
        began = time.monotonic()
        with self.assertRaises(FetchError) as caught:
            self.lookup(resolver, timeout=0.3)
        self.assertEqual(caught.exception.kind, 'dns_timeout')
        self.assertLess(time.monotonic() - began, 1.5)
        release.set()
        self.assertTrue(wait_until(lambda: not [t for t in research_threads() if t.name == 'myradio-research-dns'], 3))

    def test_stuck_lookups_cannot_pile_up_threads(self):
        release = threading.Event()
        resolver = Resolver(lambda h, p: release.wait(10) and [], max_threads=2)
        for _ in range(2):
            with self.assertRaises(FetchError):
                self.lookup(resolver, timeout=0.1)
        for _ in range(5):
            with self.assertRaises(FetchError) as caught:
                self.lookup(resolver, timeout=0.1)
            self.assertEqual(caught.exception.kind, 'dns_busy')
        self.assertLessEqual(len([t for t in research_threads() if t.name == 'myradio-research-dns']), 2)
        release.set()

    def test_cancel_stops_waiting(self):
        release, cancel = threading.Event(), threading.Event()
        resolver = Resolver(lambda h, p: release.wait(10) and [])
        threading.Timer(0.1, cancel.set).start()
        began = time.monotonic()
        with self.assertRaises(FetchError) as caught:
            self.lookup(resolver, timeout=5, cancel=cancel)
        self.assertEqual(caught.exception.kind, 'cancelled')
        self.assertLess(time.monotonic() - began, 1.5)
        release.set()


if __name__ == '__main__':
    unittest.main()
