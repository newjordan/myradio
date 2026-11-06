"""Build the private design preview of the studio: the REAL one-page studio plus the preview layer in this folder.

Usage, from the repository folder:
    python3 tools/preview/build_preview.py myradio/web <out.html>

The real studio (myradio/web/index.html, style.css, app.js) runs on an in-memory demo backend (backend.js) with sample data;
planned designs are marked PLANNED: the first-run onboarding (onboarding.html, onboarding.js), Shows, Team and Discover
(planned-views.html, layer.js), the Next-step and Autopilot cards. Every section opens as a sheet over the studio, like the
real app. The output has no doctype, html, head or body tags: the artifact publisher adds them. Rebuild and republish to the
same artifact URL after any change to the studio or to these files.
"""
import base64
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
web, out = Path(sys.argv[1]), Path(sys.argv[2])
read = lambda name: (here / name).read_text().strip('\n')
index, css, js = (web / 'index.html').read_text(), (web / 'style.css').read_text(), (web / 'app.js').read_text()


def must(text, old, new):
    assert text.count(old) == 1, (old[:70], text.count(old))
    return text.replace(old, new)


# ---- the real page body, with the preview pieces put in
body = index[index.index('<body>') + len('<body>'):index.index('</body>')].strip('\n')
body = must(body, '<div class="app-shell"><header class="masthead">', '<div class="app-shell">' + read('previewbar.html') + '<header class="masthead">')
body = must(body, '<section id="view-studio" class="view console" aria-label="Studio">\n', '<section id="view-studio" class="view console" aria-label="Studio">\n' + read('next-step.html') + '\n')
body = must(body, '<p id="keyboard-instructions"', read('autopilot.html') + '\n<p id="keyboard-instructions"')
body = must(body, '<section id="view-operator" class="operator"', read('onboarding.html') + '\n' + read('planned-views.html') + '\n<section id="view-operator" class="operator"')
planned = ''.join(f'<a href="#{v}" data-view="{v}"><span aria-hidden="true">{icon}</span>{name} <span class="x-planned">◇ PLANNED</span><small>{hint}</small></a>'
                  for v, icon, name, hint in (('start', '✦', 'Set up your station', 'First run: install, hook up, first song'),
                                               ('shows', '▦', 'Shows', 'Your station’s week'),
                                               ('team', '◍', 'On-air team', 'Hosts and spots'),
                                               ('discover', '✧', 'Researchers', 'Sourced updates')))
body = must(body, '<small>From first sound to going live</small></a></nav>', '<small>From first sound to going live</small></a>' + planned + '</nav>')
body = must(body, '\n<dialog id="unlock">', '\n' + read('dialogs.html') + '\n<dialog id="unlock">')

# ---- the real app.js, with the preview's adjustments (more sections; in-page confirm, since the viewer blocks confirm())
app = js
app = must(app, "const viewNames = ['studio', 'library', 'schedule', 'broadcast', 'settings', 'help', 'operator', 'connect'];",
                "const viewNames = ['studio', 'library', 'schedule', 'broadcast', 'settings', 'help', 'operator', 'connect', 'start', 'shows', 'team', 'discover'];")
app = must(app, "connect: 'Connect your agent'};", "connect: 'Connect your agent', start: 'Set up your station', shows: 'Shows', team: 'On-air team', discover: 'Researchers'};")
app = must(app, "const route = location.hash.slice(1), selected",
                "const route = (h => h === 'my-station' ? 'settings' : /^start-/.test(h) ? 'start' : h)(location.hash.slice(1)), selected")
app = must(app, "if (dangerous && !confirm('Delete this audio", "if (dangerous && !await uiConfirm('Delete this audio")
app = must(app, "if(!confirm('Forget the saved AI", "if(!await uiConfirm('Forget the saved AI")
assert 'confirm(' not in app.replace('uiConfirm(', '')

head = '<title>myradio Studio</title>\n<meta name="claude:viewport-fit-sides" content="cover">'
preview_css = """/* Preview additions for the one-page studio */
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}
.x-previewbar{margin-bottom:6px}
.menu-links .x-planned{margin-left:6px;font-size:11px}"""
# ---- the night-time radio room behind the studio (backdrop.webp, made from the late-night-station art)
backdrop = base64.b64encode((here / 'backdrop.webp').read_bytes()).decode()
preview_css += ('\n/* Backdrop: an empty radio room at night, kept dark and soft so the desk reads on top */\n'
                'body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;image-rendering:pixelated;'
                'background:linear-gradient(90deg,#0b1019f0 0%,#0b1019c4 40%,#0b101966 100%),'
                'url(data:image/webp;base64,' + backdrop + ') 70% 50%/cover no-repeat}')
parts = [head, '<style>', css.rstrip('\n'), read('preview.css'), preview_css, '</style>', body,
         '<script>', read('backend.js'), '</script>', '<script>', app.rstrip('\n'), '</script>',
         '<script>', read('layer.js'), '</script>', '<script>', read('onboarding.js'), '</script>']
page = '\n'.join(parts) + '\n'
assert '<!doctype' not in page.lower() and '</body>' not in page and 'preview-layer-source' not in page
out.write_text(page)
print('built', out, len(page), 'bytes')
