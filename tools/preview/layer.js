/* Behaviour for the new preview views. Everything here is sample content and in-memory
   state; nothing is saved and nothing is sent anywhere. */
(function () {
  'use strict';
  const demo = window.__demo;
  const q = (s, r) => (r || document).querySelector(s);
  const qa = (s, r) => [...(r || document).querySelectorAll(s)];
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const mmss = sec => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`;

  /* ---------- sample content ---------- */
  const HOSTS = [
    {id: 'nova', name: 'Nova', role: 'Late-night host', blurb: 'Warm, unhurried, a little curious about everything.', chips: ['Warm', 'Calm', 'Curious'], tone: '#d8edac'},
    {id: 'dex', name: 'Dex', role: 'Morning host', blurb: 'Quick, upbeat, loves a good song intro.', chips: ['Upbeat', 'Quick', 'Playful'], tone: '#ff946e'},
    {id: 'marlowe', name: 'Marlowe', role: 'Anchor for updates', blurb: 'Clear and steady. Reads sourced updates exactly as found.', chips: ['Clear', 'Steady', 'Factual'], tone: '#7b9daa'},
    {id: 'professor', name: 'Professor Static', role: 'Dry-wit curator', blurb: 'Deadpan song notes, using only facts the station has.', chips: ['Dry', 'Witty', 'Nerdy'], tone: '#ffe9bd'},
  ];
  const hostById = id => HOSTS.find(h => h.id === id);
  const RECIPES = [
    {id: 'lofi', name: 'Late-night lo-fi', blurb: 'Dim lights, slow beats, rain on the window.', station: 'Porchlight FM', tag: 'Late-night sounds for stargazers and makers.', styles: ['Downtempo trip-hop', 'Warm brush jazz', 'Ambient garden'], host: 'nova', energy: 25, adv: 35, vocals: 10, art: ['#ff946e', '#243d49', '#ffe9bd'], words: /lo-?fi|late|night|chill|cozy|cosy|star|rain|sleep/},
    {id: 'sunny', name: 'Sunny morning drive', blurb: 'Bright, bouncy, windows down.', station: 'Sunroof Radio', tag: 'Good mornings, on repeat.', styles: ['Bossa courtyard', 'Pocket funk', 'Neo-soul'], host: 'dex', energy: 75, adv: 30, vocals: 55, art: ['#ffe9bd', '#ff946e', '#243d49'], words: /morning|sun|drive|upbeat|happy|bright|commute/},
    {id: 'maker', name: 'Maker’s workshop', blurb: 'Steady focus music for building things.', station: 'Workbench FM', tag: 'Music for people who make things.', styles: ['Chamber minimalism', 'Post-rock', 'Downtempo trip-hop'], host: 'marlowe', energy: 40, adv: 45, vocals: 5, art: ['#7b9daa', '#10131d', '#d8edac'], words: /maker|build|workshop|tinker|solder|craft|code/},
    {id: 'weird', name: 'Weird and wonderful', blurb: 'Surprising, layered, word-salad songs.', station: 'Odd Garden Radio', tag: 'Nothing here is quite what you expect.', styles: ['Progressive rock', 'Balkan chamber', 'Math rock', 'Dub reggae'], host: 'professor', energy: 55, adv: 90, vocals: 40, art: ['#d8edac', '#243d49', '#ff946e'], words: /weird|strange|odd|surreal|experimental|prog|wonder|word.?salad/},
    {id: 'cafe', name: 'Jazz café', blurb: 'Brushes, upright bass, quiet conversation.', station: 'Corner Café FM', tag: 'Small-room jazz, all day.', styles: ['Warm brush jazz', 'Chamber jazz', 'Bossa courtyard'], host: 'nova', energy: 30, adv: 30, vocals: 25, art: ['#b9d6b2', '#7b5a3c', '#ffe9bd'], words: /jazz|caf[eé]|coffee|brunch|bistro/},
    {id: 'focus', name: 'Deep focus', blurb: 'Long, slow, no interruptions.', station: 'Still Water Radio', tag: 'Quiet hours for deep work.', styles: ['Ambient garden', 'Dub techno', 'Impressionist piano'], host: null, energy: 15, adv: 30, vocals: 0, art: ['#243d49', '#7b9daa', '#ffe9bd'], words: /focus|study|quiet|calm|deep|work|read/},
  ];
  const STYLES = ['Warm brush jazz', 'Chamber jazz', 'Bossa courtyard', 'Neo-soul', 'Pocket funk', 'Downtempo trip-hop', 'Ambient garden', 'Dub techno', 'Dub reggae', 'Chamber minimalism', 'Impressionist piano', 'Post-rock', 'Math rock', 'Progressive rock', 'Balkan chamber', 'Deep house'];
  const KIND = {song: {label: 'Songs', c: '#7b9daa'}, host: {label: 'Host breaks', c: '#d8edac'}, spot: {label: 'Spots', c: '#ff946e'}, news: {label: 'Update', c: '#ffd08a'}, id: {label: 'Station ID', c: '#c9ced9'}};

  /* ---------- Studio additions ---------- */
  function listening() { return !document.getElementById('disconnect').hidden; }
  function nextStep(s) {
    if (!s.library.length) {
      if (demo.state().setup.saved) return {t: 'Add something to play', b: 'Your station is set up. Add your own audio, or set up the music maker so it can make songs for you.', link: ['#import', '＋ Add audio →']};
      return {t: 'Set up your station', b: 'Install the music maker and a DJ voice, describe your sound, and hear your first song. About ten minutes, mostly downloading.', link: ['#start', 'Start setup →']};
    }
    if (demo.state().onboarded && s.running) return {t: 'You’re on the air, privately', b: 'Your first song is playing on this computer. Make it yours next: plan your shows, pick your DJs and choose sources for short updates.', link: ['#shows', 'Customize →']};
    if (!s.running) return {t: 'Start your station', b: 'Playout begins privately on this computer. Nobody else can hear it.', btn: ['start', '▶ Start station']};
    if (!listening()) return {t: 'Hear it for yourself', b: 'Open the studio monitor and listen as your station plays.', btn: ['listen', 'Listen here']};
    if (!s.broadcast.configured) return {t: 'Ready to share it?', b: 'Going live needs a streaming host, which isn’t included. Add its details once, and after that going live takes one click.', link: ['#broadcast', 'Set up broadcast →']};
    if (!s.broadcast.active) return {t: 'Share it with the world', b: 'When you like how it sounds, go live to your streaming host. You’ll be asked to confirm first.', btn: ['broadcast-start', 'Go live…']};
    return {t: 'You’re on air', b: 'Your stream is connected to your host. Check your host’s public player to confirm that listeners can hear it.', link: ['#broadcast', 'View broadcast →']};
  }
  function renderNext(s) {
    const n = nextStep(s), link = q('#x-next-link'), btn = q('#x-next-btn');
    q('#x-next-title').textContent = n.t; q('#x-next-body').textContent = n.b;
    link.hidden = !n.link; btn.hidden = !n.btn;
    if (n.link) { link.href = n.link[0]; link.textContent = n.link[1]; }
    if (n.btn) { btn.dataset.target = n.btn[0]; btn.textContent = n.btn[1]; btn.disabled = false; }
  }
  q('#x-next-btn').addEventListener('click', e => { const t = document.getElementById(e.currentTarget.dataset.target); if (t && !t.disabled) t.click(); });

  const TIMELINE = [
    {k: 'song', t: 'Low Orbit Lullaby', s: '3:48 · ready'}, {k: 'host', t: 'Nova', s: 'Link · 0:22 · ready'}, {k: 'song', t: 'Paper Lantern Weather', s: '3:21 · ready'},
    {k: 'spot', t: 'Corner Bike Co.', s: 'Spot · 0:30'}, {k: 'song', t: 'Quartz & Rain', s: '5:02 · ready'}, {k: 'news', t: 'Astronomy update', s: 'Review first · 1:30'}, {k: 'id', t: 'Station ID', s: '0:06'}];
  const TL_LABEL = {song: 'SONG', host: 'HOST', spot: 'SPOT', news: 'UPDATE', id: 'ID'};
  let autoUser = null;
  function renderAuto(s) {
    const card = q('#x-auto'), sw = q('#x-autopilot'), D = demo.state(), ready = D.makers.music;
    card.hidden = !s.library.length;
    q('#x-auto-setup').hidden = ready; sw.disabled = !ready; q('#x-auto-meter').hidden = !ready; q('#x-auto-hint').hidden = !ready;
    if (!ready) {
      sw.checked = false; q('#x-auto-sub').textContent = 'Needs the music maker.';
      q('#x-auto-status').textContent = 'Autopilot needs the music maker. Until then your station plays your own audio on rotation.';
      q('#x-auto-time').innerHTML = ''; return;
    }
    q('#x-auto-sub').textContent = 'Makes songs and host breaks ahead of time.';
    sw.checked = autoUser === null ? true : autoUser;
    const on = sw.checked;
    let p = 1, msg = 'The next 3 hours are ready. Autopilot keeps making more as you listen.';
    if (D.warm) {
      p = Math.min(1, (Date.now() - D.warm.since) / 20000);
      if (p >= 1) D.warm = null; else msg = 'Warming up. Making your first songs and a station ID. (Simulated in this preview.)';
    }
    if (!on) { msg = 'Autopilot is off. Your station plays what’s queued, then your library.'; p = 0; }
    q('#x-auto-status').textContent = msg; q('#x-auto-meter').value = p; q('#x-auto-meter').max = 1;
    q('#x-auto-time').innerHTML = on ? TIMELINE.map(x => `<li style="--k:${KIND[x.k] ? KIND[x.k].c : '#8fb3c4'}"><b>${TL_LABEL[x.k]}</b><span>${esc(x.t)}</span><small>${esc(x.s)}</small></li>`).join('') : '';
  }
  q('#x-autopilot').addEventListener('change', e => { autoUser = e.target.checked; renderAuto(window.__lastStatus || {library: [1]}); });
  q('#x-auto-setup').addEventListener('click', () => { location.hash = '#start'; });

  /* ---------- first-run onboarding: see onboarding.js ---------- */

  /* ---------- Shows ---------- */
  const SHOWS = [
    {id: 'overnight', name: 'Overnight Drift', from: 0, to: 6, style: 'Ambient garden', host: null, talk: 'rare', energy: 15, spots: false, news: false, c: '#3b4a63'},
    {id: 'morning', name: 'Morning Rise', from: 6, to: 10, style: 'Bossa courtyard', host: 'dex', talk: 'often', energy: 70, spots: true, news: true, c: '#8a4d3a'},
    {id: 'daytime', name: 'Daytime Focus', from: 10, to: 17, style: 'Chamber minimalism', host: 'nova', talk: 'rare', energy: 35, spots: true, news: false, c: '#3f6050'},
    {id: 'golden', name: 'Golden Hour', from: 17, to: 20, style: 'Warm brush jazz', host: 'marlowe', talk: 'some', energy: 50, spots: true, news: true, c: '#7a5c26'},
    {id: 'late', name: 'Late Night', from: 20, to: 24, style: 'Downtempo trip-hop', host: 'nova', talk: 'some', energy: 30, spots: false, news: false, c: '#454a7a'},
  ];
  let sel = 'morning', showSeq = 0;
  const hourLabel = h => h === 24 || h === 0 ? '12 am' : h === 12 ? '12 pm' : h < 12 ? `${h} am` : `${h - 12} pm`;
  const hostName = id => (hostById(id) || {name: 'No host'}).name;
  q('#show-days').innerHTML = ['Every day', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d, i) => `<label class="x-chip x-tog"><input type="radio" name="show-day" value="${i}"${i === 0 ? ' checked' : ''}>${d}</label>`).join('');
  q('#se-style').innerHTML = STYLES.map(s => `<option>${esc(s)}</option>`).join('');
  q('#se-host').innerHTML = '<option value="">No host</option>' + HOSTS.map(h => `<option value="${h.id}">${esc(h.name)}</option>`).join('');
  q('#se-from').innerHTML = Array.from({length: 24}, (_, h) => `<option value="${h}">${hourLabel(h)}</option>`).join('');
  q('#se-to').innerHTML = Array.from({length: 24}, (_, h) => `<option value="${h + 1}">${hourLabel(h + 1)}</option>`).join('');

  function renderShows() {
    const cur = SHOWS.find(s => s.id === sel) || SHOWS[0]; if (cur) sel = cur.id;
    q('#show-strip').innerHTML = SHOWS.map(s => `<button type="button" class="x-block" data-id="${s.id}" aria-current="${s.id === sel}" style="left:${s.from / 24 * 100}%;width:${(s.to - s.from) / 24 * 100}%;--c:${s.c}">${esc(s.name)}<small>${hourLabel(s.from)}–${hourLabel(s.to)} · ${esc(hostName(s.host))}</small></button>`).join('');
    q('#show-list').innerHTML = SHOWS.map(s => `<li><span class="dot" style="--c:${s.c}" aria-hidden="true"></span><div class="item-main"><strong>${esc(s.name)}</strong><small>${hourLabel(s.from)}–${hourLabel(s.to)} · ${esc(s.style)} · ${esc(hostName(s.host))}</small></div><button type="button" data-id="${s.id}">Edit</button></li>`).join('') || '<li class="empty">No shows yet.</li>';
    qa('#show-strip [data-id], #show-list [data-id]').forEach(b => b.addEventListener('click', () => { sel = b.dataset.id; renderShows(); q('#show-editor').scrollIntoView({block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); }));
    q('#show-editor').hidden = !cur;
    if (!cur) { q('#clock-svg').innerHTML = ''; q('#clock-legend').innerHTML = ''; return; }
    q('#show-editor-title').textContent = 'Edit ' + cur.name; q('#show-time').textContent = `${hourLabel(cur.from)} – ${hourLabel(cur.to)}`.toUpperCase();
    q('#se-name').value = cur.name; q('#se-style').value = cur.style; q('#se-from').value = cur.from; q('#se-to').value = cur.to;
    q('#se-host').value = cur.host || ''; q(`input[name=se-talk][value=${cur.talk}]`).checked = true; q('#se-energy').value = cur.energy;
    q('#se-spots').checked = cur.spots; q('#se-news').checked = cur.news; q('#se-error').hidden = true;
    renderClock(cur);
  }
  function hourPlan(s) {
    const talk = {rare: [1, 20], some: [2, 30], often: [3, 40]}[s.talk], n = s.host ? talk[0] : 0;
    const extra = [];
    for (let i = 0; i < n; i++) extra.push({k: 'host', s: talk[1]});
    if (s.spots) extra.push({k: 'spot', s: 60});
    if (s.news) extra.push({k: 'news', s: 90});
    const used = 10 + extra.reduce((a, x) => a + x.s, 0), songs = Math.max(1, Math.floor((3600 - used) / 225)), each = (3600 - used) / songs;
    const seq = [{k: 'id', s: 10}], gap = Math.max(1, Math.floor(songs / (extra.length + 1)));
    let made = 0;
    for (let i = 0; i <= extra.length; i++) {
      const count = i === extra.length ? songs - made : Math.min(gap, songs - made);
      for (let j = 0; j < count; j++, made++) seq.push({k: 'song', s: each});
      if (i < extra.length) seq.push(extra[i]);
    }
    return seq;
  }
  function renderClock(s) {
    const seq = hourPlan(s), R = 70, C = 2 * Math.PI * R;
    let off = 0, out = '';
    for (const seg of seq) {
      const len = seg.s / 3600 * C;
      out += `<circle cx="100" cy="100" r="${R}" fill="none" stroke="${KIND[seg.k].c}" stroke-width="26" stroke-dasharray="${Math.max(0, len - 1.2).toFixed(2)} ${(C - len + 1.2).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 100 100)"/>`;
      off += len;
    }
    q('#clock-svg').innerHTML = `<title>An example hour of ${esc(s.name)}</title>${out}<text x="100" y="99" text-anchor="middle" fill="#f4f2ed" font-size="22" font-weight="700">60 min</text><text x="100" y="118" text-anchor="middle" fill="#afb6c5" font-size="10">${seq.length} moments</text>`;
    q('#clock-name').textContent = s.name;
    const tally = {};
    for (const x of seq) { (tally[x.k] = tally[x.k] || {n: 0, s: 0}); tally[x.k].n++; tally[x.k].s += x.s; }
    q('#clock-legend').innerHTML = ['id', 'song', 'host', 'spot', 'news'].filter(k => tally[k]).map(k => `<li style="--c:${KIND[k].c}"><i aria-hidden="true"></i>${KIND[k].label} ×${tally[k].n}<small>${mmss(tally[k].s)}</small></li>`).join('');
  }
  q('#se-save').addEventListener('click', () => {
    const s = SHOWS.find(x => x.id === sel), err = q('#se-error');
    const from = Number(q('#se-from').value), to = Number(q('#se-to').value), name = q('#se-name').value.trim();
    const fail = m => { err.textContent = m; err.hidden = false; };
    if (!name) return fail('Give the show a name.');
    if (to <= from) return fail('A show needs to end after it starts.');
    const clash = SHOWS.find(x => x !== s && x.from < to && from < x.to);
    if (clash) return fail(`This overlaps ${clash.name} (${hourLabel(clash.from)}–${hourLabel(clash.to)}). Shorten one of them.`);
    Object.assign(s, {name, from, to, style: q('#se-style').value, host: q('#se-host').value || null, talk: q('input[name=se-talk]:checked').value, energy: Number(q('#se-energy').value), spots: q('#se-spots').checked, news: q('#se-news').checked});
    q('#show-msg').textContent = `Saved ${name} (preview).`; renderShows();
  });
  q('#se-remove').addEventListener('click', async () => {
    const s = SHOWS.find(x => x.id === sel);
    if (s && await uiConfirm(`Remove “${s.name}”? Its hours will have no show until you add one.`, 'Remove show')) { SHOWS.splice(SHOWS.indexOf(s), 1); sel = SHOWS[0] && SHOWS[0].id; renderShows(); }
  });
  q('#show-add').addEventListener('click', () => {
    const covered = Array(24).fill(false); SHOWS.forEach(s => { for (let h = s.from; h < s.to; h++) covered[h] = true; });
    const h = covered.indexOf(false), msg = q('#show-msg');
    if (h < 0) { msg.textContent = 'Every hour already has a show. Shorten one first.'; return; }
    let to = h + 1; while (to < 24 && !covered[to] && to - h < 2) to++;
    const id = 'new' + (++showSeq);
    SHOWS.push({id, name: 'New show', from: h, to, style: 'Warm brush jazz', host: null, talk: 'some', energy: 40, spots: false, news: false, c: '#4b5a78'});
    sel = id; msg.textContent = ''; renderShows(); q('#se-name').focus(); q('#se-name').select();
  });

  /* ---------- Team ---------- */
  function renderTeam() {
    q('#team-count').textContent = `${HOSTS.length} hosts`;
    q('#team-hosts').innerHTML = HOSTS.map(h => {
      const shifts = SHOWS.filter(s => s.host === h.id).map(s => s.name);
      return `<article class="x-host"><div class="x-host-top"><span class="x-avatar" style="--tone:${h.tone}" aria-hidden="true">${esc(h.name[0])}</span><div><strong>${esc(h.name)}</strong><span class="role">${esc(h.role)}</span></div></div><p>${esc(h.blurb)}</p><div class="x-chips">${h.chips.map(c => `<span class="x-chip">${esc(c)}</span>`).join('')}</div><p class="x-hint">${shifts.length ? 'On shift: ' + esc(shifts.join(', ')) : 'Not on a show yet.'}</p><div class="x-actions"><button type="button" disabled>▶ Hear a sample</button><span class="x-hint">Needs voices set up.</span></div></article>`;
    }).join('');
  }
  q('#host-new').addEventListener('click', () => notice('Preview: with a connected AI operator, you would describe a personality in a sentence and review the draft. No AI operator is connected here.'));

  const SPOTS = [
    {who: 'Corner Bike Co.', text: 'Fixing bikes since 1998. Mention the station for a free tune-up.', freq: 'Regular', when: 'Weekdays, 7 am – 7 pm', plays: 14, st: 'live'},
    {who: 'Harbor Coffee Roasters', text: 'Small-batch beans, roasted Tuesdays.', freq: 'Light', when: 'Starts Nov 12', plays: 0, st: 'sched'},
    {who: 'Tell a friend', text: 'A promo for your own station.', freq: 'Light', when: 'Any show', plays: 6, st: 'hold'},
  ];
  const ST = {live: ['RUNNING', 'live'], sched: ['SCHEDULED', ''], hold: ['PAUSED', 'hold']};
  function renderSpots() {
    q('#team-spots').innerHTML = SPOTS.map((p, i) => `<li><div class="main"><strong>${esc(p.who)}</strong> <span class="x-status ${ST[p.st][1]}">${ST[p.st][0]}</span><div class="meta">${esc(p.freq)} · ${esc(p.when)} · ${p.plays} plays this week (sample)</div><p style="font-size:.8rem;margin-top:6px">“${esc(p.text)}” <span class="x-hint">Always read with “sponsored by”.</span></p></div><div class="x-actions" style="margin:0"><button type="button" data-i="${i}" data-act="toggle">${p.st === 'hold' ? 'Resume' : 'Pause'}</button><button type="button" class="danger" data-i="${i}" data-act="remove">Remove</button></div></li>`).join('') || '<li class="empty">No spots yet. Add one below.</li>';
    qa('#team-spots [data-act]').forEach(b => b.addEventListener('click', async () => {
      const p = SPOTS[Number(b.dataset.i)];
      if (b.dataset.act === 'toggle') { p.st = p.st === 'hold' ? 'live' : 'hold'; renderSpots(); }
      else if (await uiConfirm(`Remove the spot for ${p.who}?`, 'Remove spot')) { SPOTS.splice(SPOTS.indexOf(p), 1); renderSpots(); }
    }));
  }
  q('#spot-form').addEventListener('submit', e => {
    e.preventDefault();
    const who = q('#spot-who').value.trim(), text = q('#spot-text').value.trim(), err = q('#spot-error');
    if (!who || !text) { err.textContent = 'Add who it’s for and what it should say.'; err.hidden = false; return; }
    if (!q('#spot-rights').checked) { err.textContent = 'Confirm you’re allowed to use this voice for paid spots, or upload a recorded spot from the Library instead.'; err.hidden = false; return; }
    err.hidden = true;
    SPOTS.unshift({who, text, freq: {light: 'Light', regular: 'Regular', often: 'Often'}[q('input[name=spot-freq]:checked').value], when: 'Any show', plays: 0, st: 'sched'});
    q('#spot-form').reset(); q('#spot-disclose').checked = true; renderSpots();
  });

  /* ---------- Discover ---------- */
  const RES = [
    {name: 'Backyard astronomy', likes: ['planets', 'meteor showers', 'telescope tips'], skips: [], cadence: 'Every day at 8:00 am', anchor: 'Marlowe', review: true, st: 'live',
     brief: {at: 'Today 7:42 am', items: [['A bright meteor was reported over the Pacific Northwest on Tuesday night.', 'Example Astronomy Club newsletter', 'Nov 5, 6:10 am'], ['Saturn’s rings are close to edge-on this season, which makes them look thin.', 'Example Sky Notes', 'Nov 4, 9:00 pm'], ['A beginner’s guide to cleaning a telescope mirror.', 'Example Backyard Optics', 'Nov 3, 4:30 pm']]}},
    {name: 'Independent electronic music', likes: ['new releases', 'synth builds', 'small labels'], skips: ['celebrity news', 'national politics'], cadence: 'Weekdays at 12:00 pm', anchor: 'Marlowe', review: true, st: 'live', brief: null},
    {name: 'People building useful things', likes: ['open hardware', 'community repair'], skips: [], cadence: 'Fridays at 5:00 pm', anchor: 'Dex', review: false, st: 'hold', brief: {at: 'Last Friday 4:55 pm', items: [['A library started a free repair café on Saturdays.', 'Example Maker Weekly', 'Oct 31, 10:20 am']]}},
  ];
  const open = new Set();
  function renderRes() {
    q('#res-count').textContent = `${RES.length} researchers`;
    q('#res-list').innerHTML = RES.map((r, i) => {
      let box = '';
      if (open.has(i)) {
        box = r.brief
          ? `<div class="x-brief"><h4>LATEST BRIEF · ${esc(r.brief.at.toUpperCase())} · <span class="x-planned">SAMPLE</span></h4><ul>${r.brief.items.map(it => `<li>${esc(it[0])}<small>${esc(it[1])} · ${esc(it[2])}</small></li>`).join('')}</ul><small style="margin-top:8px">Why these: they match your interests, were published in the last 24 hours, and come from sources you chose. ${r.review ? 'Waiting for your review before it airs.' : 'Airs automatically.'}</small></div>`
          : '<div class="x-brief skip"><h4>LATEST RUN · TODAY 12:00 PM</h4>Nothing fresh enough to air. Music kept playing, and nothing was made up to fill the slot.</div>';
      }
      return `<li><div class="main"><strong>${esc(r.name)}</strong> <span class="x-status ${r.st}">${r.st === 'live' ? 'ON' : r.st === 'hold' ? 'PAUSED' : 'DRAFT'}</span><div class="meta">${esc(r.cadence)} · read by ${esc(r.anchor)}</div><div class="x-chips" style="margin-top:8px">${r.likes.map(l => `<span class="x-chip">${esc(l)}</span>`).join('')}${r.skips.map(l => `<span class="x-chip skip">skips ${esc(l)}</span>`).join('')}</div>${box}</div><div class="x-actions" style="margin:0;flex-direction:column;align-items:stretch"><label class="x-switch" style="margin:0"><input type="checkbox" data-review="${i}"${r.review ? ' checked' : ''}><span class="x-track" aria-hidden="true"></span><span>Review before airing</span></label><button type="button" data-run="${i}">${open.has(i) ? 'Hide result' : 'Run now'}</button></div></li>`;
    }).join('');
    qa('[data-run]').forEach(b => b.addEventListener('click', () => { const i = Number(b.dataset.run); open.has(i) ? open.delete(i) : open.add(i); renderRes(); }));
    qa('[data-review]').forEach(b => b.addEventListener('change', () => { RES[Number(b.dataset.review)].review = b.checked; renderRes(); }));
  }
  q('#res-draft').addEventListener('click', () => {
    const t = q('#res-new').value.trim(), msg = q('#res-msg');
    if (t.length < 8) { msg.textContent = 'Write a sentence about what you’d like to hear about.'; msg.className = 'x-error'; return; }
    const skip = /skip ([^.]+)/i.exec(t), likes = t.replace(/skip [^.]+\.?/i, '').replace(/^(a|an|some)\s+(short|quick)[^ ]*\s+(upbeat\s+)?(update|brief)\s+(on|about)\s+/i, '').replace(/[.]+$/, '').split(/,| and /).map(x => x.trim()).filter(Boolean).slice(0, 4);
    RES.unshift({name: (likes[0] || 'New researcher').replace(/^./, c => c.toUpperCase()).slice(0, 40), likes, skips: skip ? [skip[1].trim()] : [], cadence: 'Every day at 9:00 am', anchor: 'Marlowe', review: true, st: 'draft', brief: null});
    q('#res-new').value = ''; msg.className = 'x-hint'; msg.textContent = 'Draft added at the top (preview). Check the interests, then turn it on.'; renderRes();
  });

  /* ---------- preview bar, phone sheet, wiring ---------- */
  qa('.x-toggle button').forEach(b => b.addEventListener('click', () => {
    demo.reset(b.dataset.mode); if (window.__onboarding) window.__onboarding.reset();
    location.hash = b.dataset.mode === 'first' ? '#start' : '#studio';
  }));

  function onState(s) {
    window.__lastStatus = s;
    renderNext(s); renderAuto(s);
    qa('.x-toggle button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === demo.state().mode)));
  }
  const original = window.render;
  if (typeof original === 'function') window.render = function (s) { original(s); onState(s); };
  setInterval(() => { if (window.__lastStatus && demo.state().warm) renderAuto(window.__lastStatus); }, 1000);

  // Connect your agent: nothing real can connect to a preview, so let the reviewer see the connected state.
  const pretend = document.createElement('button'); pretend.type = 'button'; pretend.className = 'x-pretend';
  pretend.textContent = 'Preview only: pretend my agent connected';
  pretend.addEventListener('click', () => { demo.pretendAgent(); pretend.disabled = true; });
  const linkCard = q('#link-title') && q('#link-title').closest('.card'); if (linkCard) linkCard.append(pretend);

  renderShows(); renderTeam(); renderSpots(); renderRes();
})();
