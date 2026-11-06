/* Demo backend for the design preview. The real app.js talks to /api/*; this answers
   those calls in memory with sample data, using the same response shapes as the real
   service. No network, no audio, nothing is saved. */
(function () {
  'use strict';
  const G = window;

  // app.js reads sessionStorage without a guard, and a viewer may block it.
  try { sessionStorage.setItem('myradio-token', 'preview'); }
  catch (e) {
    const mem = {};
    Object.defineProperty(G, 'sessionStorage', {configurable: true, value: {
      getItem: k => (k in mem ? mem[k] : null),
      setItem: (k, v) => { mem[k] = String(v); },
      removeItem: k => { delete mem[k]; }}});
    sessionStorage.setItem('myradio-token', 'preview');
  }
  const note = m => { document.documentElement.dataset.errors = (document.documentElement.dataset.errors || '') + ' | ' + m; };
  G.addEventListener('error', e => note(e.message));
  G.addEventListener('unhandledrejection', e => note(String(e.reason && e.reason.message || e.reason)));

  const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; } })();
  const nowS = () => Date.now() / 1000;
  const iso = ms => new Date(ms).toISOString();
  const mk = (id, title, artist, duration, kind) => ({id, title, artist, duration, kind: kind || 'music', available: true, playback_error: null});
  const SAMPLE = [
    mk('t1', 'Porch Light Parade', 'Lantern & Moss', 252),
    mk('t2', 'Low Orbit Lullaby', 'Soft Meridian', 228),
    mk('t3', 'Paper Lantern Weather', 'Otter Choir', 201),
    mk('t4', 'Quartz & Rain', 'Soft Meridian', 302),
    mk('t5', 'The Long Way Home', 'Lantern & Moss', 215),
    mk('t6', 'Moth Hours', 'Night Orchard', 280),
    mk('t7', 'Static Bloom', 'Otter Choir', 178),
    mk('t8', 'Tide Tables', 'Night Orchard', 230),
    mk('s1', 'Station ID · Porchlight FM', 'Nova', 6, 'speech'),
    mk('s2', 'Late-night link', 'Nova', 22, 'speech'),
    mk('a1', 'Spot · Corner Bike Co.', 'Sponsor read', 30, 'ad'),
  ];

  let D, seq = 100;
  const qrow = t => ({id: ++seq, track_id: t.id, title: t.title, artist: t.artist, duration: t.duration, kind: t.kind});
  const lib = id => D.library.find(t => t.id === id);
  function event(title, ev) { D.history.unshift({title, at: iso(Date.now()), event: ev}); D.history.length = Math.min(D.history.length, 200); }

  function fresh(mode) {
    const onair = mode !== 'first';
    D = {
      mode: onair ? 'onair' : 'first',
      station: onair ? {name: 'Porchlight FM', tagline: 'Late-night sounds for stargazers and makers.', auto_fill: true}
                     : {name: 'My radio', tagline: 'Your station. Your frequency.', auto_fill: true},
      running: onair, current: null, elapsed: 0, last: null,
      library: onair ? SAMPLE.map(t => ({...t})) : [],
      queue: [], schedules: [], programs: [], history: [],
      broadcast: {configured: onair, state: 'disconnected', active: false, bytes_sent: 0, connections: 0, retries: 0, retry_at: null, error: ''},
      warm: null,
      // What is installed on this computer. The on-air sample shows the fully set-up experience;
      // a first run starts with neither maker, so the dead end of "nothing to play" is visible.
      makers: {music: onair, voice: onair}, setup: {saved: onair},
    };
    if (onair) {
      D.current = lib('t1'); D.elapsed = 83; D.last = 't8';
      for (const id of ['t2', 's2', 't3', 'a1', 't4']) D.queue.push(qrow(lib(id)));
      D.schedules.push({id: 'sc1', track_id: 's1', title: 'Station ID · Porchlight FM', at: iso(Date.now() + 20 * 3600e3), status: 'pending'});
      D.programs.push(
        {id: 'p1', track_id: 's1', timezone: tz, local_time: '00:00', weekdays: '1,2,3,4,5,6,7', next_due: nowS() + 3 * 3600, last_due: null, last_status: null, title: 'Station ID · Porchlight FM'},
        {id: 'p2', track_id: 's2', timezone: tz, local_time: '22:00', weekdays: '1,2,3,4,5', next_due: nowS() + 6 * 3600, last_due: null, last_status: null, title: 'Late-night link'});
      [['Tide Tables', 'finished', 4], ['Station ID · Porchlight FM', 'finished', 5], ['Static Bloom', 'finished', 9], ['Moth Hours', 'finished', 14], ['Porch Light Parade', 'playing', 1]]
        .forEach(([title, ev, min]) => D.history.push({title, at: iso(Date.now() - min * 60e3), event: ev}));
      D.history.reverse();
    }
    return D;
  }

  function nextTrack() {
    if (D.queue.length) { const q = D.queue.shift(); return lib(q.track_id) || nextTrack(); }
    if (!D.station.auto_fill) return null;
    const music = D.library.filter(t => t.kind === 'music' && t.available);
    if (!music.length) return null;
    return music[(music.findIndex(t => t.id === D.last) + 1) % music.length];
  }
  function startNext() {
    const t = nextTrack();
    if (!t) return;
    D.current = {...t}; D.elapsed = 0; event(t.title, 'playing');
  }
  function finish(ev) { if (D.current) { D.last = D.current.id; event(D.current.title, ev); } D.current = null; D.elapsed = 0; }

  let lastTick = performance.now();
  setInterval(() => {
    const t = performance.now(), dt = Math.min(1.5, (t - lastTick) / 1000); lastTick = t;
    if (document.hidden) return;
    if (D.running && D.current) { D.elapsed += dt; if (D.elapsed >= D.current.duration) { finish('finished'); startNext(); } }
    else if (D.running) startNext();
    if (D.broadcast.state === 'streaming') D.broadcast.bytes_sent += Math.round(16000 * dt);
  }, 500);

  function status() {
    const bad = D.library.filter(t => !t.available).length;
    return {
      health: {state: !D.running ? 'stopped' : !D.current ? 'waiting' : bad ? 'degraded' : 'playing', unavailable_tracks: bad, ready_tracks: D.library.length - bad,
               message: D.running && !D.current ? 'Waiting for playable audio. Queue a ready track, import audio or repair and retry unavailable tracks. The stream carries silence while waiting.' : ''},
      station: D.station, running: D.running, current: D.current ? {...D.current} : null,
      elapsed: Math.round(D.elapsed * 100) / 100, error: null,
      queue: D.queue.map(q => ({...q})), library: D.library.map(t => ({...t})),
      schedules: D.schedules.map(s => ({...s})), programs: D.programs.map(p => ({...p})), history: D.history.slice(0, 30),
      capabilities: {music_generation: false, speech_generation: false, external_broadcast: true, agent_model: false},
      stream: {listeners: 0, dropped: 0}, chat_mode: 'commands',
      broadcast: {...D.broadcast, format: 'MP3 · 128 kbit/s · 44.1 kHz stereo', encoder_available: true, last_sent: null},
      imports: {conversion_available: true, max_upload_bytes: 104857600, max_decoded_bytes: 104857600},
    };
  }

  function nextLocal(localTime, weekdays) {
    const [hh, mm] = localTime.split(':').map(Number), days = weekdays.split(',').map(Number);
    for (let i = 0; i < 9; i++) {
      const d = new Date(); d.setDate(d.getDate() + i); d.setHours(hh, mm, 0, 0);
      const iw = d.getDay() === 0 ? 7 : d.getDay();
      if (days.includes(iw) && d.getTime() > Date.now()) return d.getTime() / 1000;
    }
    return nowS() + 86400;
  }

  function doAction(op, a) {
    const need = k => { if (typeof a[k] !== 'string' || !a[k]) throw new Error('Missing ' + k + '.'); return a[k]; };
    const track = id => { const t = lib(id); if (!t) throw new Error('That track is no longer in the library.'); return t; };
    switch (op) {
      case 'station.update':
        for (const k of ['name', 'tagline']) if (k in a) { if (!String(a[k]).trim()) throw new Error(k + ' must be 1–200 characters.'); D.station[k] = String(a[k]).trim(); }
        if ('auto_fill' in a) D.station.auto_fill = !!a.auto_fill;
        break;
      case 'transport.start':
        if (!D.library.length) throw new Error('Import at least one audio track before starting your station.');
        D.running = true; if (!D.current) startNext(); break;
      case 'transport.stop':
        if (D.broadcast.active) { D.broadcast.state = 'disconnected'; D.broadcast.active = false; D.broadcast.connections = 0; }
        D.running = false;
        if (D.current) { D.queue.unshift(qrow(D.current)); finish('stopped'); }
        break;
      case 'transport.skip': finish('skipped'); if (D.running) startNext(); break;
      case 'queue.add': {
        track(need('track_id')); if (D.queue.length >= 1000) throw new Error('Queue is full (1,000 items).');
        if ('position' in a && !(Number.isInteger(a.position) && a.position >= 0)) throw new Error('Invalid position.');
        const row = qrow(lib(a.track_id)); if ('position' in a) D.queue.splice(Math.min(a.position, D.queue.length), 0, row); else D.queue.push(row); break; }
      case 'queue.move': {
        const i = D.queue.findIndex(q => String(q.id) === String(a.queue_id)); if (i < 0) throw new Error('Queue entry not found.');
        if (!(Number.isInteger(a.position) && a.position >= 0)) throw new Error('Invalid position.');
        const [row] = D.queue.splice(i, 1); D.queue.splice(Math.min(a.position, D.queue.length), 0, row); break; }
      case 'queue.remove': { const i = D.queue.findIndex(q => String(q.id) === String(a.queue_id)); if (i < 0) throw new Error('Queue entry not found.'); D.queue.splice(i, 1); break; }
      case 'library.delete': {
        const t = track(need('track_id'));
        if ((D.current && D.current.id === t.id) || D.queue.some(q => q.track_id === t.id) || D.programs.some(p => p.track_id === t.id) || D.schedules.some(s => s.track_id === t.id && s.status === 'pending'))
          throw new Error('Remove this track from playout, queue, pending schedules and recurring programs first.');
        D.library = D.library.filter(x => x.id !== t.id); break; }
      case 'library.retry': track(need('track_id')).available = true; break;
      case 'schedule.add': {
        const t = track(need('track_id')), when = new Date(need('at'));
        if (!Number.isFinite(when.getTime())) throw new Error('Use an ISO timestamp with a timezone offset.');
        if (!(when.getTime() > Date.now() && when.getTime() <= Date.now() + 366 * 86400e3)) throw new Error('Schedule within the next 366 days.');
        D.schedules.unshift({id: 'sc' + (++seq), track_id: t.id, title: t.title, at: when.toISOString(), status: 'pending'}); break; }
      case 'schedule.cancel': { const s = D.schedules.find(x => x.id === a.schedule_id && x.status === 'pending'); if (!s) throw new Error('Pending schedule not found.'); s.status = 'cancelled'; break; }
      case 'program.add': {
        const t = track(need('track_id'));
        if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(a.local_time || '')) throw new Error('Use a 24-hour local time, HH:MM.');
        if (!/^[1-7](?:,[1-7])*$/.test(a.weekdays || '')) throw new Error('Weekdays must be comma-separated ISO days: Monday=1 … Sunday=7.');
        D.programs.push({id: 'p' + (++seq), track_id: t.id, timezone: a.timezone || tz, local_time: a.local_time, weekdays: [...new Set(a.weekdays.split(',').map(Number))].sort().join(','),
                         next_due: nextLocal(a.local_time, a.weekdays), last_due: null, last_status: null, title: t.title}); break; }
      case 'program.remove': { const i = D.programs.findIndex(p => p.id === a.program_id); if (i < 0) throw new Error('Recurring program not found.'); D.programs.splice(i, 1); break; }
      case 'broadcast.start':
        if (!D.running) throw new Error('Start local station playout before going live.');
        if (!D.broadcast.configured) throw new Error('Save your broadcast destination first.');
        D.broadcast.state = 'connecting'; D.broadcast.active = true;
        setTimeout(() => { if (D.broadcast.active) { D.broadcast.state = 'streaming'; D.broadcast.connections = 1; } }, 1200); break;
      case 'broadcast.stop':
        D.broadcast.state = 'stopping';
        setTimeout(() => { D.broadcast.state = 'disconnected'; D.broadcast.active = false; D.broadcast.connections = 0; }, 600); break;
      default: throw new Error('Unknown station operation.');
    }
    return {ok: true, operation: op};
  }

  // Same wording as the real command assistant.
  function chat(message) {
    const c = message.trim().toLowerCase().replace(/[.!?]+$/, '');
    const direct = {'start': 'transport.start', 'start station': 'transport.start', 'stop': 'transport.stop', 'stop station': 'transport.stop', 'skip': 'transport.skip', 'skip track': 'transport.skip'};
    if (direct[c]) {
      doAction(direct[c], {});
      return {reply: {'transport.start': 'Local station playout started. Click Listen to monitor it. This command does not enable an external broadcast.',
                      'transport.stop': 'Station stopped and external broadcast disconnected. The interrupted track is back at the front of your queue.',
                      'transport.skip': 'Skipped the current track.'}[direct[c]], mode: 'commands', actions: []};
    }
    if (c.startsWith('name ')) { const n = message.trim().slice(5).trim(); doAction('station.update', {name: n}); return {reply: 'Station renamed to ' + n + '.', mode: 'commands', actions: []}; }
    let reply;
    if (['status', "what's playing", 'what is playing'].includes(c)) {
      reply = (D.running ? 'Running. ' : 'Stopped. ') + (D.current ? 'Now playing: ' + D.current.title + '. ' : 'No track is playing. ') + D.queue.length + ' queued; ' + D.library.length + ' in your library.';
    } else {
      reply = "I'm the local command assistant, not a connected AI agent yet. I understand: start, stop, skip, status, and name <station name>. To have an AI operator man the station, open Connect an AI operator in this chat panel and save an OpenAI-compatible tool-calling model, or connect your existing agent using the documented MCP adapter. Model-proposed changes require your approval.";
    }
    return {reply, mode: 'commands', actions: []};
  }

  const json = (obj, status) => new Response(JSON.stringify(obj), {status: status || 200, headers: {'Content-Type': 'application/json'}});
  async function route(url, init) {
    const p = url.pathname, method = (init.method || 'GET').toUpperCase();
    const body = () => { try { return JSON.parse(init.body || '{}'); } catch (e) { return {}; } };
    if (p === '/api/status') return json(status());
    if (p === '/api/session' || p === '/api/logout') return json({ok: true});
    if (p === '/api/agent') {
      if (method === 'GET') return json({configured: false, url: '', model: '', key_saved: false, environment_managed: false, error: null});
      return json({error: 'Preview only: connecting an AI provider is turned off here. In your own studio this saves your provider privately.'}, 400);
    }
    if (p === '/api/broadcast') {
      if (method === 'GET') return json(D.broadcast.configured ? {url: 'https://radio.example.org:8443/porchlight.mp3', username: 'source'} : {});
      D.broadcast.configured = true; return json({ok: true});
    }
    if (p === '/api/action') { const b = body(); return json(doAction(b.operation, b.arguments || {})); }
    if (p === '/api/chat') return json(chat(String(body().message || '')));
    if (p === '/api/approve') return json({ok: true});
    if (p === '/api/upload') {
      const q = url.searchParams, kind = ['music', 'speech', 'ad'].includes(q.get('kind')) ? q.get('kind') : 'music';
      const t = mk('u' + (++seq), (q.get('title') || 'Untitled').slice(0, 200), (q.get('artist') || '').slice(0, 200), 180, kind);
      D.library.push(t); event(t.title, 'imported'); return json({id: t.id, title: t.title, artist: t.artist, duration: t.duration, kind: t.kind, available: true, playback_error: null}, 201);
    }
    return json({error: 'Not found.'}, 404);
  }
  const realFetch = G.fetch.bind(G);
  G.fetch = async (input, init) => {
    init = init || {};
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (!url.pathname.startsWith('/api/')) return realFetch(input, init);
    await new Promise(r => setTimeout(r, 30));
    try { return await route(url, init); } catch (e) { return json({error: (e && e.message) || 'Preview error'}, 400); }
  };

  // In-page confirmation (the viewer disables window.confirm, and a native box is not friendly anyway).
  G.uiConfirm = (message, okLabel) => new Promise(resolve => {
    const d = document.getElementById('confirm-dialog'), ok = d.querySelector('[data-ok]'), cancel = d.querySelector('[data-cancel]');
    d.querySelector('p').textContent = message; ok.textContent = okLabel || 'Confirm';
    const done = v => { d.close(); ok.onclick = cancel.onclick = d.oncancel = null; resolve(v); };
    ok.onclick = () => done(true); cancel.onclick = () => done(false); d.oncancel = e => { e.preventDefault(); done(false); };
    d.showModal(); cancel.focus();
  });

  const startMode = /^start/.test(location.hash.slice(1)) ? 'first' : 'onair';
  fresh(startMode);

  G.__demo = {
    state: () => D,
    reset(mode) { fresh(mode); if (typeof refresh === 'function') refresh(); },
    saveSetup(o) { D.station.name = o.name; D.station.tagline = o.tagline; D.setup = {saved: true, host: o.host}; refresh(); },
    // Sample songs exist only so a reviewer can walk the whole path; they are always labelled.
    addSampleSongs() {
      SAMPLE.filter(t => t.kind === 'music').slice(0, 3).forEach((t, i) => D.library.push({...t, id: 'sample' + (++seq), title: t.title + ' (sample)', artist: 'Sample artist'}));
      refresh();
    },
    setMaker(kind, ready) { D.makers[kind] = !!ready; refresh(); },
    // The mocked first-run onboarding: models "installed", the station named, the first song made and on air.
    completeOnboarding(o) {
      D.station.name = o.name; D.station.tagline = o.tagline; D.makers = {music: true, voice: o.voice !== 'none'}; D.setup = {saved: true, host: o.voice};
      const song = mk('first' + (++seq), o.title, o.name, o.seconds, 'music'); D.library.push(song);
      let intro = null;
      if (o.voice !== 'none') { intro = mk('intro' + (++seq), 'DJ intro · ' + o.name, o.voice === 'elevenlabs' ? 'Your DJ · ElevenLabs voice' : 'Your DJ · Drama 3 voice', 14, 'speech'); D.library.push(intro); }
      D.running = true; D.onboarded = Date.now();
      D.current = {...(intro || song)}; D.elapsed = 0; event(D.current.title, 'playing');
      if (intro) D.queue.unshift(qrow(song));
      refresh();
    },
    finishSetup(o) {
      D.station.name = o.name; D.station.tagline = o.tagline; D.setup = {saved: true, host: o.host};
      if (!D.library.length && D.makers.music) { G.__demo.addSampleSongs(); D.warm = {since: Date.now()}; }
      if (!D.library.length) return false;
      D.running = true; if (!D.current) startNext(); refresh(); return true;
    },
  };

  // Preview-only guards, registered before app.js so they run first.
  const listen = document.getElementById('listen'), lock = document.getElementById('lock');
  listen.addEventListener('click', e => {
    e.stopImmediatePropagation(); listen.hidden = true; document.getElementById('disconnect').hidden = false; document.getElementById('vu-label').textContent = 'NO AUDIO HERE';
    notice('Preview: your studio monitor would play the station here. No audio plays in this preview.');
  }, true);
  lock.addEventListener('click', e => { e.stopImmediatePropagation(); notice('Preview: locking is turned off here.'); }, true);
})();
