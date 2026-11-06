/* First-run onboarding, as a walk-through (PLANNED design, simulated): install the models, hook them up, one prompt,
   the first song, then the station is on the air and the person customizes it from there. Nothing downloads, nothing is
   installed, nothing is sent anywhere. Sizes and speeds are the real models' published figures; waits are shortened. */
(function () {
  'use strict';
  const demo = window.__demo;
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const STEPS = ['install', 'hookup', 'prompt', 'first'];
  const MUSIC = {label: 'Music maker · YuE2 Turbo Radio', bytes: 4.53e9};
  const VOICE = {
    elevenlabs: {label: 'DJ voice · ElevenLabs', bytes: 0, check: ['DJ voice connected', 'ElevenLabs accepted the key and read a test line.']},
    qwen: {label: 'DJ voice · Qwen3-TTS (Jessica)', bytes: 2.82e9, check: ['DJ voice warmed up', 'Qwen3-TTS on this computer read a test line in Jessica’s voice.']},
    drama: {label: 'DJ voice · Drama 3 B70 Turbo', bytes: 24.3e9, check: ['DJ voice warmed up', 'Drama 3 on this computer: first audio in 0.3 s (the model card’s figure).']},
    none: {label: 'No DJ', bytes: 0, check: ['No DJ voice yet', 'Music only for now. Add a voice any time from the menu.']},
  };
  const IDEAS = [
    {label: 'Late-night lo-fi', vibe: 'Late-night lo-fi and slow beats for stargazers and people who build things.', name: 'Porchlight FM', tagline: 'Late-night sounds for stargazers and makers.', title: 'Porch Light Parade', seconds: 192, words: /lo-?fi|late|night|chill|cozy|star|rain|sleep/},
    {label: 'Sunny morning drive', vibe: 'Bright, bouncy morning music with the windows down.', name: 'Sunroof Radio', tagline: 'Good mornings, on repeat.', title: 'Windows Down', seconds: 204, words: /morning|sun|drive|upbeat|happy|bright|summer/},
    {label: 'Jazz café', vibe: 'Small-room jazz: brushes, upright bass and quiet conversation.', name: 'Corner Café FM', tagline: 'Small-room jazz, all day.', title: 'Second Cup', seconds: 236, words: /jazz|caf[eé]|coffee|brunch|swing/},
    {label: 'Deep focus', vibe: 'Long, slow ambient music for deep work, with no surprises.', name: 'Still Water Radio', tagline: 'Quiet hours for deep work.', title: 'Still Water', seconds: 258, words: /focus|study|quiet|calm|ambient|deep|work/},
    {label: 'Weird and wonderful', vibe: 'Surprising, layered songs that never sound like a playlist.', name: 'Odd Garden Radio', tagline: 'Nothing here is quite what you expect.', title: 'The Gardener Hums in Prime Numbers', seconds: 221, words: /weird|odd|strange|surreal|experimental|prog|wonder/},
  ];
  const gb = bytes => (bytes / 1e9).toFixed(2) + ' GB';
  const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  const realMinutes = bytes => Math.max(1, Math.round(bytes * 8 / 100e6 / 60));   // on a 100 Mbit/s connection
  let st, timers = [];
  const later = (fn, ms) => { const t = setTimeout(fn, ms); timers.push(t); return t; };
  const every = (fn, ms) => { const t = setInterval(fn, ms); timers.push(t); return t; };
  const stopAll = () => { timers.forEach(t => { clearTimeout(t); clearInterval(t); }); timers = []; };
  const say = text => { const a = q('#ob-announce'); a.textContent = ''; later(() => { a.textContent = text; }, 30); };

  function show(step, focus = true) {
    st.step = step;
    qa('#view-start .ob-panel').forEach(p => { p.hidden = p.dataset.panel !== step; });
    const at = STEPS.indexOf(step);
    qa('#view-start .ob-steps li').forEach((li, i) => {
      li.classList.toggle('done', i < at);
      if (i === at) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      li.querySelector('span').textContent = i < at ? '✓' : String(i + 1);
    });
    if (focus && !q('#view-start').hidden) q(`#ob-h-${step}`).focus({preventScroll: true});
    const body = q('#sheet .sheet-body'); if (body) body.scrollTop = 0;
  }

  // ---- 1. install
  function voice() { return (q('input[name=ob-voice]:checked') || {}).value || 'elevenlabs'; }
  function updateInstall() {
    const v = voice(), bytes = MUSIC.bytes + VOICE[v].bytes;
    q('#ob-key-row').hidden = v !== 'elevenlabs';
    q('#ob-install-hint').textContent = `${gb(bytes)} to download: about ${realMinutes(bytes)} minutes on a 100 Mbit/s connection.`;
    q('#ob-install-error').hidden = true;
  }
  qa('input[name=ob-voice]').forEach(r => r.addEventListener('change', updateInstall));
  q('#ob-install').addEventListener('click', () => {
    const v = voice(), err = q('#ob-install-error');
    if (v === 'elevenlabs' && !q('#ob-key').value.trim()) {
      err.textContent = 'Paste your ElevenLabs API key, or choose another voice. (In this preview any text works; nothing is sent.)';
      err.hidden = false; q('#ob-key').focus(); return;
    }
    err.hidden = true;
    st.voice = v;
    qa('#view-start [data-panel=install] input, #ob-install').forEach(el => { el.disabled = true; });
    const items = [MUSIC].concat(VOICE[v].bytes ? [VOICE[v]] : []);
    const box = q('#ob-downloads'); box.hidden = false;
    box.innerHTML = items.map((it, i) => `<div class="ob-dl"><div class="ob-dl-top"><strong>${it.label}</strong><span id="ob-dl-text-${i}">waiting</span></div><progress id="ob-dl-bar-${i}" max="1" value="0" aria-label="${it.label} download"></progress></div>`).join('') +
      (v === 'elevenlabs' ? '<p class="x-hint">ElevenLabs runs in the cloud, so there is nothing to download for your DJ. Your key is saved privately on this computer.</p>' : '') +
      '<div class="ob-actions"><button id="ob-skip-dl" type="button">Skip the wait</button><span class="x-hint">Simulated download.</span></div>';
    const SIM_BPS = 0.55e9;   // shortened: a few seconds per gigabyte so the walk-through keeps moving
    let i = 0, done = 0;
    const tick = every(() => {
      const it = items[i]; if (!it) return;
      done = Math.min(it.bytes, done + SIM_BPS * 0.25);
      q(`#ob-dl-bar-${i}`).value = done / it.bytes;
      q(`#ob-dl-text-${i}`).textContent = `${gb(done)} of ${gb(it.bytes)}`;
      if (done >= it.bytes) { q(`#ob-dl-text-${i}`).textContent = `${gb(it.bytes)} · installed ✓`; i += 1; done = 0; if (i >= items.length) finish(); }
    }, 250);
    function finish() {
      clearInterval(tick);
      items.forEach((it, k) => { q(`#ob-dl-bar-${k}`).value = 1; q(`#ob-dl-text-${k}`).textContent = `${gb(it.bytes)} · installed ✓`; });
      q('#ob-skip-dl').disabled = true;
      say('Models installed. Hooking them up.');
      later(startHookup, 700);
    }
    q('#ob-skip-dl').addEventListener('click', finish);
    say('Installing.');
  });

  // ---- 2. hook up
  function startHookup() {
    show('hookup');
    const v = VOICE[st.voice];
    const checks = [
      ['Graphics card found', 'Intel Arc Pro B70 · 32 GB (sample reading; myradio reads your real card).'],
      ['Music maker loaded', 'YuE2 Turbo Radio · 4.53 GB, ready on the graphics card.'],
      ['Test clip made', '8 seconds of music came back clean: not silent, not clipped, correct length.'],
      v.check,
      ['Everything is linked up', 'Your station can make songs' + (st.voice === 'none' ? '.' : ' and talk between them.')],
    ];
    const list = q('#ob-checks'); list.innerHTML = '';
    checks.forEach(([title, detail], k) => later(() => {
      const prev = list.lastElementChild; if (prev) { prev.classList.remove('busy'); prev.classList.add('ok'); prev.querySelector('.ico').textContent = '✓'; }
      const li = document.createElement('li'); li.className = 'busy';
      li.innerHTML = '<span class="ico" aria-hidden="true">…</span><span class="txt"><strong></strong><small></small></span>';
      li.querySelector('strong').textContent = title; li.querySelector('small').textContent = detail;
      list.append(li);
      if (k === checks.length - 1) later(() => { li.classList.remove('busy'); li.classList.add('ok'); li.querySelector('.ico').textContent = '✓'; q('#ob-hookup-next').disabled = false; q('#ob-hookup-next').focus(); say('Everything is linked up.'); }, 700);
    }, 300 + k * 1100));
  }
  q('#ob-hookup-next').addEventListener('click', () => { show('prompt'); });

  // ---- 3. one prompt
  q('#ob-ideas').innerHTML = IDEAS.map((it, k) => `<button type="button" class="x-chip ob-idea" data-k="${k}">${it.label}</button>`).join('');
  function pick(it, fillVibe) {
    st.idea = it;
    if (fillVibe) q('#ob-vibe').value = it.vibe;
    if (!q('#ob-name').dataset.touched) q('#ob-name').value = it.name;
    if (!q('#ob-tagline').dataset.touched) q('#ob-tagline').value = it.tagline;
    qa('.ob-idea').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.k) === IDEAS.indexOf(it))));
  }
  qa('.ob-idea').forEach(b => b.addEventListener('click', () => { pick(IDEAS[Number(b.dataset.k)], true); q('#ob-prompt-error').hidden = true; }));
  q('#ob-vibe').addEventListener('input', () => {
    const text = q('#ob-vibe').value.toLowerCase(), match = IDEAS.find(it => it.words.test(text));
    if (match) pick(match, false);
  });
  ['#ob-name', '#ob-tagline'].forEach(id => q(id).addEventListener('input', e => { e.target.dataset.touched = '1'; }));
  q('#ob-make').addEventListener('click', () => {
    const vibe = q('#ob-vibe').value.trim(), err = q('#ob-prompt-error');
    if (vibe.length < 8) { err.textContent = 'Write a sentence about the sound you want, or tap one of the ideas.'; err.hidden = false; q('#ob-vibe').focus(); return; }
    err.hidden = true;
    const it = st.idea || IDEAS[0];
    st.final = {name: q('#ob-name').value.trim() || it.name, tagline: q('#ob-tagline').value.trim() || it.tagline, title: it.title, seconds: it.seconds, vibe, voice: st.voice};
    startFirstSong();
  });

  // ---- 4. the first song
  function startFirstSong() {
    show('first');
    const f = st.final, dj = st.voice !== 'none';
    q('#ob-first-lead').textContent = `“${f.vibe}” · ${f.name}`;
    const stages = ['Writing the lyrics and the arrangement', `Composing “${f.title}”`, 'Rendering the audio', 'Mastering it to station level'].concat(dj ? ['Recording your DJ’s first hello'] : []);
    q('#ob-stages').innerHTML = stages.map(s => `<li>${s.replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]))}</li>`).join('');
    const total = 11000 + (dj ? 1500 : 0), started = Date.now(), share = 1 / stages.length;
    q('#ob-skip-render').disabled = false;
    const tick = every(() => {
      const p = Math.min(1, (Date.now() - started) / total), stage = Math.min(stages.length - 1, Math.floor(p / share));
      q('#ob-render').value = p;
      q('#ob-render-clock').textContent = `${mmss(Math.min(f.seconds, p * f.seconds * 1.15))} of ${mmss(f.seconds)} made`;
      qa('#ob-stages li').forEach((li, k) => { li.className = k < stage ? 'ok' : k === stage ? 'busy' : ''; });
      if (p >= 1) done();
    }, 200);
    function done() {
      clearInterval(tick); stopAll();
      qa('#ob-stages li').forEach(li => { li.className = 'ok'; });
      q('#ob-render').value = 1; q('#ob-render-clock').textContent = `${mmss(f.seconds)} of ${mmss(f.seconds)} made`;
      q('#ob-h-first').textContent = 'Your first song is ready';
      say('Your first song is ready. Going on the air.');
      later(() => {
        demo.completeOnboarding(f);
        location.hash = '#studio';
        later(() => { if (typeof toast === 'function') toast(`${f.name} is on the air, privately on this computer. Customize it any time from the menu.`); }, 400);
      }, 900);
    }
    q('#ob-skip-render').onclick = done;
  }

  function reset() {
    stopAll();
    st = {step: 'install', voice: 'elevenlabs', idea: null, final: null};
    qa('#view-start input, #view-start textarea, #view-start button').forEach(el => { el.disabled = false; });
    q('input[name=ob-voice][value=elevenlabs]').checked = true; q('#ob-key').value = '';
    ['#ob-vibe', '#ob-name', '#ob-tagline'].forEach(id => { q(id).value = ''; delete q(id).dataset.touched; });
    qa('.ob-idea').forEach(b => b.setAttribute('aria-pressed', 'false'));
    q('#ob-downloads').hidden = true; q('#ob-downloads').innerHTML = ''; q('#ob-checks').innerHTML = '';
    q('#ob-hookup-next').disabled = true; q('#ob-h-first').textContent = 'Making your first song';
    q('#ob-render').value = 0; q('#ob-stages').innerHTML = '';
    ['#ob-install-error', '#ob-prompt-error'].forEach(id => { q(id).hidden = true; });
    updateInstall(); show('install', false);
  }
  window.__onboarding = {reset};
  reset();
})();
