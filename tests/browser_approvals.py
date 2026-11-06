"""Focused real-browser contract for the redesigned operator.

Uses an explicit loopback fake OpenAI-compatible provider: this verifies the
approval UI, not any real model's quality or availability. Run only against a
disposable service; this changes its identity and saves/forgets an agent config.
Environment and Playwright setup are the same as browser_station.py.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from playwright.async_api import async_playwright


async def go(page, view):
    """Open a section the way the studio does: the studio is the page and every other section is a sheet over it."""
    await page.evaluate('(view) => { location.hash = view; }', view)
    if view == 'studio':
        await page.wait_for_function("() => !document.getElementById('sheet').open")
    else:
        await page.locator(f'#view-{view}').wait_for(state='visible')


class Provider(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        assert self.path == '/v1/chat/completions', self.path
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert request['model'] == 'approval-ui-fixture'
        Provider.calls += 1
        result = {'choices': [{'message': {
            'content': 'Rename only after your approval. <img src=x onerror="alert(1)">',
            'tool_calls': [{'function': {'name': 'station_update',
                                        'arguments': json.dumps({'name': 'Approved Night Studio'})}}],
        }}]}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def main():
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            options = {'headless': True, 'args': ['--no-sandbox']}
            if os.environ.get('MYRADIO_TEST_BROWSER'):
                options['executable_path'] = os.environ['MYRADIO_TEST_BROWSER']
            browser = await p.chromium.launch(**options)
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(os.environ['MYRADIO_TEST_URL'] + '/#token=' + os.environ['MYRADIO_TEST_TOKEN'])
            await page.wait_for_function("() => document.getElementById('connection').textContent.includes('connected')")
            before = await page.locator('#station-name').inner_text()
            await go(page, 'operator')
            await page.locator('.agent-help summary').click()
            await page.locator('#agent-url').fill(f'http://127.0.0.1:{fixture.server_port}/v1')
            await page.locator('#agent-model').fill('approval-ui-fixture')
            await page.locator('#agent-consent').check()
            await page.locator('#agent-save').click()
            await page.wait_for_function("() => document.getElementById('chat-mode').textContent.includes('approval required')")
            await page.locator('.agent-help summary').click()
            await page.locator('#message').fill('Propose a station rename, please.')
            await page.locator('#send').click()
            approve = page.get_by_role('button', name='Approve this change', exact=True)
            await approve.wait_for(state='visible')
            assert Provider.calls == 1
            assert await page.locator('#station-name').inner_text() == before
            assert await page.locator('#chat-log img').count() == 0, 'Model output must remain text'
            assert '<img src=x' in await page.locator('#chat-log').inner_text()
            await page.set_viewport_size({'width': 390, 'height': 844})
            await go(page, 'operator')
            await approve.wait_for(state='visible')
            await go(page, 'library')
            await go(page, 'operator')
            await approve.click()
            applied = page.get_by_role('button', name='Change applied', exact=True)
            await applied.wait_for(state='visible')
            assert await applied.is_disabled()
            await page.wait_for_function("() => document.getElementById('station-name').textContent==='Approved Night Studio'")
            Path('docs/screenshots').mkdir(parents=True, exist_ok=True)
            await page.screenshot(path='docs/screenshots/midnight-approval-mobile.png', full_page=True)
            print('PASS: real UI through loopback model fixture; no rename until approval, proposal survives mobile navigation, one-shot disabled applied button, model HTML rendered as inert text.')
            # A new request invalidates an earlier still-pending approval card.
            await page.locator('#message').fill('Propose another rename.')
            await page.locator('#send').click()
            await approve.wait_for(state='visible')
            await page.locator('#message').fill('Replace that proposal.')
            await page.locator('#send').click()
            await page.wait_for_function("() => [...document.querySelectorAll('#chat-log button')].filter(b=>b.textContent==='Approve this change').length===2")
            pending = page.get_by_role('button', name='Approve this change', exact=True)
            assert await pending.nth(0).is_disabled()
            assert await pending.nth(1).is_enabled()
            await page.locator('.agent-help summary').click()
            page.once('dialog', lambda dialog: dialog.accept())
            await page.locator('#agent-disconnect').click()
            await page.wait_for_function("() => document.getElementById('chat-mode').textContent.includes('Local command assistant')")
            assert await page.locator('#chat-log button').count() == 0
            print('PASS: new question disables stale proposal, disconnect clears proposals and returns to command mode.')
            # Every section sheet fits at phone and tablet widths, and the studio's last line scrolls clear of the fixed desk.
            for width in (320, 390, 768):
                await page.set_viewport_size({'width': width, 'height': 844})
                for view in ('library', 'schedule', 'broadcast', 'operator', 'settings', 'help', 'studio'):
                    await go(page, view)
                    assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), (width, view)
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                footer_bottom = await page.locator('footer').evaluate('(el) => el.getBoundingClientRect().bottom')
                deck_top = await page.locator('#deck').evaluate('(el) => el.getBoundingClientRect().top')
                assert footer_bottom <= deck_top, (width, footer_bottom, deck_top)
            print('PASS: every section sheet fits at 320/390/768px; the studio scrolls clear of the fixed desk.')
            assert not errors, errors
            await browser.close()
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join()


if __name__ == '__main__':
    asyncio.run(main())
