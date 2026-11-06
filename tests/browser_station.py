"""Optional real-browser acceptance test (Playwright is a test-only dependency).

Start an isolated myradio service first. Set MYRADIO_TEST_URL and MYRADIO_TEST_TOKEN.
Use a disposable, empty data directory: this test imports audio and changes playout.
"""
import asyncio
import io
import os
from pathlib import Path
import struct
import subprocess
import wave
from playwright.async_api import async_playwright


async def main():
    base = os.environ.get('MYRADIO_TEST_URL', 'http://127.0.0.1:18765')
    token = os.environ['MYRADIO_TEST_TOKEN']
    async with async_playwright() as p:
        options = {'headless': True, 'args': ['--no-sandbox']}
        if os.environ.get('MYRADIO_TEST_BROWSER'):
            options['executable_path'] = os.environ['MYRADIO_TEST_BROWSER']
        browser = await p.chromium.launch(**options)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(base + '/#token=' + token)
        await page.wait_for_function("() => document.getElementById('connection').textContent.includes('connected')")
        assert await page.locator('#unlock').evaluate('(el)=>!el.open')
        assert '#token=' not in page.url
        # Navigation is a real view switch, keyboard operable, and never loses drafts.
        await page.locator('#message').fill('Keep this draft while I explore')
        for view in ('library', 'schedule', 'broadcast', 'settings', 'help', 'studio'):
            await page.locator(f'.rail a[data-view={view}]').focus()
            await page.keyboard.press('Enter')
            await page.wait_for_function('(view) => document.body.dataset.activeView === view', arg=view)
            await page.locator(f'#view-{view}').wait_for(state='visible')
            assert await page.locator(f'.rail a[data-view={view}]').get_attribute('aria-current') == 'page'
            assert await page.locator('#message').input_value() == 'Keep this draft while I explore'
        await page.locator('#message').fill('')
        await page.locator('[data-view=settings]').click()
        await page.locator('#name-input').fill('Draft station identity')
        await page.locator('[data-view=studio]').click()
        await page.wait_for_timeout(1100)
        await page.locator('[data-view=settings]').click()
        assert await page.locator('#name-input').input_value() == 'Draft station identity'
        await page.locator('#name-input').fill('Browser Studio')
        await page.locator('#identity button').click()
        await page.wait_for_function("() => document.getElementById('station-name').textContent==='Browser Studio'")
        print('PASS: keyboard navigation across every desktop view, active states, mounted chat and identity drafts, identity save.')
        output = io.BytesIO()
        with wave.open(output, 'wb') as f:
            f.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
            f.writeframes(struct.pack('<h', 500) * 44100 * 3)
        payload=output.getvalue(); suffix='wav'; mime='audio/wav'
        if os.environ.get('MYRADIO_TEST_IMPORT_FORMAT')=='mp3':
            encoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','wav','-i','pipe:0',
                                    '-c:a','libmp3lame','-f','mp3','pipe:1'],input=payload,capture_output=True,timeout=20)
            assert encoded.returncode==0,encoded.stderr
            payload=encoded.stdout;suffix='mp3';mime='audio/mpeg'
        await page.locator('[data-view=library]').click()
        await page.locator('#file').set_input_files({'name': f'Browser check.{suffix}', 'mimeType': mime, 'buffer': payload})
        await page.locator('#upload-button').click()
        await page.wait_for_function("() => document.getElementById('library-count').textContent==='1 tracks'")
        await page.locator('[data-view=schedule]').click()
        await page.locator('#program-track').select_option(label='Browser check · music')
        await page.locator('#program-time').fill('12:34')
        await page.locator('#program-zone').fill('America/New_York')
        await page.locator('#program button[type="submit"]').click()
        await page.wait_for_function("() => document.getElementById('programs').textContent.includes('12:34 America/New_York')")
        assert 'Mon, Tue, Wed, Thu, Fri, Sat, Sun' in await page.locator('#programs').inner_text()
        await page.reload()
        await page.wait_for_function("() => document.getElementById('programs').textContent.includes('12:34 America/New_York')")
        if os.environ.get('MYRADIO_TEST_AGENT_SETUP'):
            await page.locator('.agent-help summary').click()
            await page.locator('#agent-url').fill('http://127.0.0.1:1/v1')
            await page.locator('#agent-model').fill('setup-browser-test')
            await page.locator('#agent-key').fill('private-browser-agent-key')
            await page.locator('#agent-consent').check()
            await page.locator('#agent-save').click()
            await page.wait_for_function("() => document.getElementById('chat-mode').textContent.includes('Connected AI')")
            assert await page.locator('#agent-key').input_value()==''
            await page.reload()
            await page.wait_for_function("() => document.getElementById('agent-summary').textContent.includes('setup-browser-test')")
            await page.locator('.agent-help summary').click()
            assert await page.locator('#agent-key').input_value()==''
            await page.locator('#message').fill('What is playing?')
            await page.locator('#send').click()
            await page.wait_for_function("() => document.getElementById('chat-log').textContent.includes('No station changes were made')")
            async def accept_disconnect(dialog):
                await dialog.accept()
            page.once('dialog',accept_disconnect)
            await page.locator('#agent-disconnect').click()
            await page.wait_for_function("() => document.getElementById('chat-mode').textContent.includes('Local command')")
            await page.locator('.agent-help summary').click()
            print('PASS: private operator setup, consent, cleared API key, reload persistence, failed endpoint without mutation, disconnect/forget to command mode. No real model certification claimed.')
        await page.locator('[data-view=studio]').click()
        await page.locator('#start').click()
        await page.wait_for_function("() => document.getElementById('track-title').textContent==='Browser check'")
        await page.locator('#listen').click()
        await page.wait_for_function("() => document.getElementById('audio').currentTime > 0", timeout=15000)
        await page.locator('#message').fill('status')
        await page.locator('#send').click()
        await page.wait_for_function("() => document.getElementById('chat-log').textContent.includes('Now playing: Browser check')")
        destination=os.environ.get('MYRADIO_TEST_BROADCAST_URL')
        if destination:
            await page.locator('[data-view=broadcast]').click()
            await page.locator('#broadcast-url').fill(destination)
            await page.locator('#broadcast-password').fill(os.environ['MYRADIO_TEST_BROADCAST_PASSWORD'])
            await page.locator('#broadcast-save').click()
            await page.wait_for_function("() => document.getElementById('broadcast-saved').textContent.includes('Saved destination:')")
            assert await page.locator('#broadcast-password').input_value()==''
            await page.reload()
            await page.wait_for_function("() => document.getElementById('broadcast-saved').textContent.includes('http://127.0.0.1:')")
            assert await page.locator('#broadcast-password').input_value()==''
            assert 'DISCONNECTED' == await page.locator('#broadcast-state').inner_text()
            await page.locator('[data-view=studio]').click()
            await page.locator('#listen').click()
            await page.wait_for_function("() => document.getElementById('audio').currentTime > 0", timeout=15000)
            await page.locator('#message').fill('status')
            await page.locator('#send').click()
            await page.wait_for_function("() => document.getElementById('chat-log').textContent.includes('Now playing: Browser check')")
            await page.locator('[data-view=broadcast]').click()
            # Go live opens the guard dialog (shared with the control desk); closing it changes nothing.
            await page.locator('#broadcast-start').click()
            await page.locator('#live-dialog').wait_for(state='visible')
            assert destination in await page.locator('#live-destination').inner_text()
            await page.locator('#live-dialog [data-close]').first.click()
            assert 'DISCONNECTED' == await page.locator('#broadcast-state').inner_text()
            await page.locator('#broadcast-start').click()
            await page.locator('#go-live').click()
            await page.wait_for_function("() => document.getElementById('broadcast-state').textContent==='STREAMING'",timeout=15000)
            assert await page.locator('#broadcast-save').is_disabled()
            await page.wait_for_function("() => document.getElementById('state-word').textContent==='LIVE'")
            assert os.environ['MYRADIO_TEST_BROADCAST_PASSWORD'] not in await page.content()
        await page.locator('[data-view=studio]').click()
        Path('docs/screenshots').mkdir(parents=True, exist_ok=True)
        await page.screenshot(path='docs/screenshots/midnight-studio-desktop.png', full_page=True)
        await page.locator('#disconnect').click()
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.screenshot(path='docs/screenshots/midnight-studio-mobile.png', full_page=True)
        await page.locator('[data-view=operator]').click()
        await page.wait_for_function("() => { const log = document.getElementById('chat-log'); return log.scrollHeight - log.scrollTop - log.clientHeight < 40; }")
        await page.screenshot(path='docs/screenshots/midnight-operator-mobile.png', full_page=True)
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
        if destination:
            await page.locator('[data-view=broadcast]').click()
            await page.locator('#broadcast-stop').click()
            await page.wait_for_function("() => document.getElementById('broadcast-state').textContent==='DISCONNECTED'")
            await page.wait_for_function("() => document.getElementById('state-word').textContent==='PRIVATE'")
            assert 'LOCAL PLAYOUT' in await page.locator('#play-label').inner_text()
            await page.locator('#broadcast-start').click()
            await page.locator('#go-live').click()
            await page.wait_for_function("() => document.getElementById('broadcast-state').textContent==='STREAMING'",timeout=15000)
        # Every mobile view remains reachable at 320 CSS pixels, without clipping.
        await page.set_viewport_size({'width': 320, 'height': 740})
        for view in ('studio', 'library', 'schedule', 'broadcast', 'operator', 'settings', 'help'):
            await page.locator(f'.rail a[data-view={view}]').click()
            await page.locator(f'#view-{view}').wait_for(state='visible')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), view
        await page.set_viewport_size({'width': 390, 'height': 844})
        print('PASS: all seven mobile views reachable at 320px, including dedicated operator; no horizontal overflow.')
        await page.locator('[data-view=schedule]').click()
        await page.locator('#programs button').click()
        await page.wait_for_function("() => document.getElementById('programs').textContent.includes('No recurring programs')")
        if os.environ.get('MYRADIO_TEST_DATA_DIR'):
            # Fault injection is opt-in and only uses this disposable service's media.
            directory = Path(os.environ['MYRADIO_TEST_DATA_DIR'])
            media = next((directory / 'media').glob('*.wav'))
            original = media.read_bytes()
            media.unlink()
            await page.wait_for_function("() => document.getElementById('library').textContent.includes('Unavailable')", timeout=15000)
            await page.wait_for_function("() => document.getElementById('playout-error').textContent.includes('Waiting for playable audio')")
            assert await page.locator('#play-label').inner_text() == 'LOCAL PLAYOUT · WAITING FOR AUDIO'
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.locator('[data-view=studio]').click()
            await page.screenshot(path='docs/screenshots/midnight-unavailable-mobile.png', full_page=True)
            await page.locator('[data-view=operator]').click()
            await page.locator('#message').fill('status')
            await page.locator('#send').click()
            await page.wait_for_function("() => document.getElementById('chat-log').textContent.includes('Waiting for playable audio')")
            media.write_bytes(original)
            await page.locator('[data-view=library]').click()
            await page.get_by_role('button', name='Retry repaired audio').click()
            await page.wait_for_function("() => !document.getElementById('library').textContent.includes('Unavailable') && document.getElementById('track-title').textContent==='Browser check'")
            await page.locator('[data-view=studio]').click()
            await page.locator('#listen').click()
            await page.wait_for_function("() => document.getElementById('audio').currentTime > 0", timeout=15000)
            await page.locator('#disconnect').click()
            print('PASS: real missing-file quarantine, waiting health warning, mobile layout, chat diagnosis, repair, explicit retry, resumed browser audio decode.')
        await page.locator('[data-view=studio]').click()
        await page.locator('#stop').click()
        await page.wait_for_function("() => document.getElementById('play-label').textContent==='LOCAL PLAYOUT · STOPPED'")
        if destination:
            await page.wait_for_function("() => document.getElementById('broadcast-state').textContent==='DISCONNECTED'")
            print('PASS: broadcast save, private password cleared, reload destination and disconnected state, cancelled go-live, confirmed streaming, locked config while live, disconnect independent of playout, reconnect, stop disconnects output.')
        await page.locator('#lock').click()
        await page.wait_for_function("() => document.getElementById('unlock').open")
        assert await page.locator('#unlock').evaluate('(el)=>el.open')
        assert not errors, errors
        print(f'PASS: unlock, fragment erasure, {suffix.upper()} import, playout, live WAV browser decode, command chat, recurring program create/reload/remove, desktop/mobile layout, stop, lock; no JS errors.')
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
