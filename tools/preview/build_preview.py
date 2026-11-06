"""Build the private design-preview artifact from the REAL web assets plus the preview layer.

Usage (from the repository folder): python3 tools/preview/build_preview.py tools/preview/preview-layer-source.html myradio/web <out html>
`preview-layer-source.html` is the published preview from 2026-11-05 (version 3). Only its preview layer is used: the
styles with the x- prefix, the preview bar, the Next-step and Autopilot cards, the planned views, the demo backend and the
planned-view script (line numbers below). Everything else comes from the current real files.
The preview = the real one-page studio (index.html, style.css, app.js) + an in-memory demo backend answering /api/* +
planned designs marked PLANNED (first-run setup, Shows, Team, Discover), opened as sheets like every other section.
Rebuild and republish to the same artifact URL after any UI change. Output has no doctype/html/head/body: the publisher adds them.
"""
import re
import sys
from pathlib import Path

old_path, web, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
old = old_path.read_text().split('\n')
L = lambda a, b: '\n'.join(old[a - 1:b])            # 1-based inclusive line range of the old preview source
index, css, js = (web / 'index.html').read_text(), (web / 'style.css').read_text(), (web / 'app.js').read_text()

def must(text, old_s, new_s):
    assert text.count(old_s) == 1, (old_s[:70], text.count(old_s))
    return text.replace(old_s, new_s)

assert old[221 - 1].startswith('<section id="x-next"') and old[226 - 1].startswith('<section id="x-auto"')
assert old[228 - 1].startswith('<section id="view-start"') and old[331 - 1].startswith('<section id="view-help"')

# ---- the real page body, with the preview pieces put in
body = index[index.index('<body>') + len('<body>'):index.index('</body>')].strip('\n')
line = old[216 - 1]; previewbar = line[line.index('<div class="x-previewbar"'):line.index('<header>')]
assert previewbar.count('<div') == previewbar.count('</div>'), 'preview bar must be a closed element'
body = must(body, '<div class="app-shell"><header class="masthead">', '<div class="app-shell">' + previewbar + '<header class="masthead">')
body = must(body, '<section id="view-studio" class="view console" aria-label="Studio">\n', '<section id="view-studio" class="view console" aria-label="Studio">\n' + old[221 - 1] + '\n')
body = must(body, '<p id="keyboard-instructions"', old[226 - 1] + '\n<p id="keyboard-instructions"')
planned_views = L(228, 330)                         # first-run setup, Shows, Team, Discover (PLANNED designs)
body = must(body, '<section id="view-operator" class="operator"', planned_views + '\n<section id="view-operator" class="operator"')
planned_links = ''.join(f'<a href="#{v}" data-view="{v}"><span aria-hidden="true">{icon}</span>{name} <span class="x-planned">◇ PLANNED</span><small>{hint}</small></a>'
                        for v, icon, name, hint in (('start', '✦', 'Set up your station', 'First run: style, sound, voice'),
                                                     ('shows', '▦', 'Shows', 'Your station’s week'),
                                                     ('team', '◍', 'On-air team', 'Hosts and spots'),
                                                     ('discover', '✧', 'Researchers', 'Sourced updates')))
body = must(body, '<small>From first sound to going live</small></a></nav>', '<small>From first sound to going live</small></a>' + planned_links + '</nav>')
body = must(body, '\n<dialog id="unlock">', '\n' + L(334, 335) + '\n<dialog id="unlock">')   # in-page confirm and the setup sheet
assert '<nav class="rail"' not in body and 'more-sheet' not in body

# ---- the demo backend learns the arranged queue (queue.move, queue.add at a position)
backend = L(341, 604)
backend = must(backend, "      case 'queue.add': track(need('track_id')); if (D.queue.length >= 1000) throw new Error('Queue is full (1,000 items).'); D.queue.push(qrow(lib(a.track_id))); break;",
"""      case 'queue.add': {
        track(need('track_id')); if (D.queue.length >= 1000) throw new Error('Queue is full (1,000 items).');
        if ('position' in a && !(Number.isInteger(a.position) && a.position >= 0)) throw new Error('Invalid position.');
        const row = qrow(lib(a.track_id)); if ('position' in a) D.queue.splice(Math.min(a.position, D.queue.length), 0, row); else D.queue.push(row); break; }
      case 'queue.move': {
        const i = D.queue.findIndex(q => String(q.id) === String(a.queue_id)); if (i < 0) throw new Error('Queue entry not found.');
        if (!(Number.isInteger(a.position) && a.position >= 0)) throw new Error('Invalid position.');
        const [row] = D.queue.splice(i, 1); D.queue.splice(Math.min(a.position, D.queue.length), 0, row); break; }""")
backend = must(backend, "    e.stopImmediatePropagation(); listen.hidden = true; document.getElementById('disconnect').hidden = false;",
"    e.stopImmediatePropagation(); listen.hidden = true; document.getElementById('disconnect').hidden = false; document.getElementById('vu-label').textContent = 'NO AUDIO HERE';")

# ---- the real app.js, with the preview's adjustments (more sections; in-page confirm, since the viewer blocks confirm())
app = js
app = must(app, "const viewNames = ['studio', 'library', 'schedule', 'broadcast', 'settings', 'help', 'operator'];",
                "const viewNames = ['studio', 'library', 'schedule', 'broadcast', 'settings', 'help', 'operator', 'start', 'shows', 'team', 'discover'];")
app = must(app, "help: 'Guide', operator: 'Station operator'};", "help: 'Guide', operator: 'Station operator', start: 'Set up your station', shows: 'Shows', team: 'On-air team', discover: 'Researchers'};")
app = must(app, "const route = location.hash.slice(1), selected",
                "const route = (h => h === 'my-station' ? 'settings' : /^start-/.test(h) ? 'start' : h)(location.hash.slice(1)), selected")
app = must(app, "if (dangerous && !confirm('Delete this audio", "if (dangerous && !await uiConfirm('Delete this audio")
app = must(app, "if(!confirm('Forget the saved AI", "if(!await uiConfirm('Forget the saved AI")
assert 'confirm(' not in app.replace('uiConfirm(', '')

# ---- the preview layer, without the old sidebar's More sheet
layer = L(940, 1311)
layer = must(layer, """  q('.rail-more').addEventListener('click', () => q('#more-sheet').showModal());
  q('#more-close').addEventListener('click', () => q('#more-sheet').close());
  qa('#more-sheet a').forEach(a => a.addEventListener('click', () => q('#more-sheet').close()));
""", '')

preview_css = """/* Preview additions for the one-page studio */
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}
.x-previewbar{margin-bottom:6px}
.menu-links .x-planned{margin-left:6px;font-size:11px}
"""
x_css = L(17, 202)
parts = [L(2, 4), css.rstrip('\n'), x_css, preview_css.rstrip('\n'), L(203, 203), body,
         L(340, 340), backend, L(605, 606), '<script>', app.rstrip('\n'), '</script>', layer]
page = '\n'.join(parts)
assert page.lstrip().startswith('<title>') and '<!doctype' not in page.lower() and '</body>' not in page
out.write_text(page)
print('built', out, len(page), 'bytes')
