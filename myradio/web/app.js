'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('myradio-token') || '';
let broadcastDestination = '';
let state = null, polling = false, identityDirty = false, lastStructure = '', unlocked = false;
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
  token = fragment.get('token'); sessionStorage.setItem('myradio-token', token);
  history.replaceState(null, '', location.pathname + location.search);
}
// One page: the studio is always there. Every other section opens as a sheet over it; forms stay mounted, so drafts survive.
const viewNames = ['studio', 'library', 'schedule', 'broadcast', 'settings', 'help', 'operator', 'connect', 'trophies'];
const sheetTitles = {library: 'Library', schedule: 'Schedule', broadcast: 'Broadcast', settings: 'Settings', help: 'Guide', operator: 'Station operator', connect: 'Connect your agent', trophies: 'Trophies and Dial'};
function showView(moveFocus = false) {
  const route = location.hash.slice(1), selected = route === 'import' ? 'library' : viewNames.includes(route) ? route : 'studio';
  for (const name of viewNames.filter(name => name !== 'studio')) $('view-' + name).hidden = name !== selected;
  document.body.dataset.activeView = selected;
  for (const link of document.querySelectorAll('a[data-view]')) {
    if (link.dataset.view === selected) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  if ($('menu').open) $('menu').close();
  const sheet = $('sheet');
  if (selected === 'studio') {
    if (sheet.open) sheet.close();
  } else {
    $('sheet-title').textContent = sheetTitles[selected] || selected; sheet.dataset.view = selected;
    if (!sheet.open) sheet.showModal();
    sheet.querySelector('.sheet-body').scrollTop = 0;
  }
  if (route === 'import') $('import').open = true;
  if (selected === 'connect') setTimeout(() => loadConnect(), 0);
  if (moveFocus && selected !== 'studio') (selected === 'operator' ? $('message') : route === 'import' ? $('file') : $('sheet-title')).focus({preventScroll: true});
}
// Closing a sheet (×, Escape, or a link to the studio) always lands back on the studio; the browser returns focus to what opened it.
$('sheet').addEventListener('close', () => { if (document.body.dataset.activeView !== 'studio') location.hash = 'studio'; });
$('menu-open').addEventListener('click', () => $('menu').showModal());
for (const button of document.querySelectorAll('[data-close-menu]')) button.addEventListener('click', () => $('menu').close());
for (const link of document.querySelectorAll('#menu a')) link.addEventListener('click', () => $('menu').close());
window.addEventListener('hashchange', () => showView(true));
showView();
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function requireUnlock() {
  unlocked = false; $('connection').textContent = 'Studio locked';
  if (!$('unlock').open) $('unlock').showModal();
}
async function api(path, data, raw = false) {
  const headers = {};
  if (token) headers['X-Myradio-Token'] = token;
  const options = {headers, credentials: 'same-origin'};
  if (data !== undefined) {
    options.method = 'POST';
    if (!raw) headers['Content-Type'] = 'application/json';
    options.body = raw ? data : JSON.stringify(data);
  }
  const controller = new AbortController(); options.signal = controller.signal;
  const timer = setTimeout(() => controller.abort(), raw ? 120000 : path === '/api/chat' ? 60000 : 20000);
  try {
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401) requireUnlock();
      throw new Error(result.error || `Station returned ${response.status}`);
    }
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Request timed out. Check station status before retrying; it may have completed.');
    throw error;
  } finally { clearTimeout(timer); }
}
async function establish() {
  try {
    await api('/api/session', {});
    unlocked = true; $('unlock').close(); $('unlock-error').textContent = ''; notice('');
    broadcastDestination=(await api('/api/broadcast')).url||'';
    await loadAgentSettings();
    await refresh();
  } catch (error) { requireUnlock(); $('unlock-error').textContent = error.message; }
}
$('unlock').addEventListener('cancel', event => event.preventDefault());
$('unlock-form').addEventListener('submit', async event => {
  event.preventDefault(); token = $('token-input').value.trim();
  sessionStorage.setItem('myradio-token', token); $('token-input').value = '';
  await establish();
});
function disconnect() {
  $('audio').pause(); $('audio').removeAttribute('src'); $('audio').load();
  $('listen').hidden = false; $('disconnect').hidden = true; monitorOn = false; stopMeters(); if (state) renderConsole(state);
}
$('lock').addEventListener('click', async () => {
  $('menu').close(); disconnect(); $('agent-key').value=''; $('broadcast-password').value='';
  try { await api('/api/logout', {}); } catch (error) { notice(error.message); }
  token = ''; sessionStorage.removeItem('myradio-token'); requireUnlock();
});
function duration(value) {
  const seconds = Math.max(0, Math.floor(value || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
// --- approval cards: the studio's own confirm, with an illustration, instead of the browser's plain box
document.querySelectorAll('.approve-art').forEach(img => img.addEventListener('error', () => img.classList.add('missing')));
function approve({title, text, ok = 'Confirm', cancel = 'Keep it', art = 'approve-delete', eyebrow = 'PLEASE CONFIRM'}) {
  const dlg = $('approve');
  $('approve-title').textContent = title; $('approve-text').textContent = text; $('approve-eyebrow').textContent = eyebrow;
  $('approve-art').src = (window.MYRADIO_ART && window.MYRADIO_ART[art]) || `/art/${art}.png`;
  const okBtn = dlg.querySelector('[data-ok]'), cancelBtn = dlg.querySelector('[data-cancel]');
  okBtn.textContent = ok; cancelBtn.textContent = cancel;
  return new Promise(resolve => {
    const done = value => { dlg.close(); okBtn.onclick = cancelBtn.onclick = dlg.oncancel = dlg.onclick = null; resolve(value); };
    okBtn.onclick = () => done(true); cancelBtn.onclick = () => done(false);
    dlg.oncancel = event => { event.preventDefault(); done(false); };
    dlg.onclick = event => { if (event.target === dlg) done(false); };
    dlg.showModal(); cancelBtn.focus();
  });
}
function actionButton(label, operation, args, dangerous = false) {
  const button = node('button', label, dangerous ? 'danger' : '');
  button.type = 'button';
  button.addEventListener('click', async () => {
    if (dangerous && !await approve({title: 'Delete this audio?', text: 'It leaves your local library for good. This cannot be undone.',
                                     ok: 'Delete it', art: 'approve-delete', eyebrow: 'LIBRARY · DELETE'})) return;
    button.disabled = true;
    try { await action(operation, args); }
    finally { button.disabled = false; }
  });
  return button;
}
async function action(operation, args = {}) {
  try { await api('/api/action', {operation, arguments: args}); notice(''); await refresh(); }
  catch (error) { notice(error.message); }
}
for (const [id, op] of [['start','transport.start'],['stop','transport.stop'],['skip','transport.fade_next']]) {
  $(id).addEventListener('click', () => action(op));
}
async function loadAgentSettings() {
  const a=await api('/api/agent');
  $('agent-summary').textContent=a.error || (a.configured?`Saved operator: ${a.model} · ${a.url}. Access is checked when you send a message.`:'No AI provider connected. Local commands remain available.');
  $('agent-url').value=a.url; $('agent-model').value=a.model; $('agent-key').value=''; $('agent-consent').checked=false;
  for (const id of ['agent-url','agent-model','agent-key','agent-save','agent-disconnect','agent-consent']) $(id).disabled=a.environment_managed;
  if(a.environment_managed) $('agent-summary').textContent+=' Managed by launch environment; edit it there.';
  $('agent-disconnect').hidden=!a.configured && !a.error;
}
$('agent-form').addEventListener('submit',async event=>{
  event.preventDefault(); $('agent-save').disabled=true;
  try {
    await api('/api/agent',{url:$('agent-url').value.trim(),model:$('agent-model').value.trim(),key:$('agent-key').value});
    $('agent-key').value=''; await loadAgentSettings(); $('chat-log').replaceChildren();
    notice('Operator connection saved, not yet tested. Send a status question; proposals still need approval.'); await refresh();
  } catch(error) {notice(error.message);}
  finally { $('agent-save').disabled=false; }
});
$('agent-disconnect').addEventListener('click',async()=>{
  if(!await approve({title: 'Forget the AI connection?', text: 'The saved connection is removed and this session’s chat and proposals are cleared. Local command mode stays available.',
                     ok: 'Forget it', art: 'approve-forget', eyebrow: 'OPERATOR · DISCONNECT'}))return;
  try {await api('/api/agent',{disconnect:true});await loadAgentSettings();$('chat-log').replaceChildren();await refresh();notice('Operator disconnected; private saved settings removed.');}
  catch(error){notice(error.message);}
});
$('broadcast-form').addEventListener('submit', async event => {
  event.preventDefault(); $('broadcast-save').disabled=true;
  try {
    await api('/api/broadcast', {url:$('broadcast-url').value.trim(), username:$('broadcast-user').value, password:$('broadcast-password').value});
    broadcastDestination=$('broadcast-url').value.trim();
    $('broadcast-password').value=''; $('broadcast-url').value=''; notice('Broadcast settings saved. Review your host, then explicitly Go live when ready.'); await refresh();
  } catch (error) { notice(error.message); }
  finally { $('broadcast-save').disabled=false; }
});
$('broadcast-start').addEventListener('click', () => openGuard());
$('broadcast-stop').addEventListener('click', () => action('broadcast.stop'));
$('listen').addEventListener('click', async () => {
  try {
    const graph = ensureGraph(); if (graph) await graph.ctx.resume();
    await api('/api/session', {});
    $('audio').src = '/stream.wav';
    $('listen').hidden = true; $('disconnect').hidden = false; $('disconnect').focus();
    await $('audio').play(); monitorOn = true; startMeters(); if (state) renderConsole(state);
  } catch (error) { notice(`Monitor: ${error.message}`); }
});
$('audio').addEventListener('error', () => { if ($('audio').hasAttribute('src')) notice('The local monitor disconnected or this browser cannot play live WAV. Disconnect and reconnect; station playout continues independently.'); });
$('disconnect').addEventListener('click', disconnect);
$('identity').addEventListener('input', () => { identityDirty = true; });
$('identity').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/action', {operation:'station.update', arguments:{name:$('name-input').value, tagline:$('tagline-input').value, auto_fill:$('auto-fill').checked}});
    identityDirty = false; notice(''); await refresh();
  } catch (error) { notice(error.message); }
});
$('file').addEventListener('change', () => {
  if (!$('upload-title').value && $('file').files[0]) $('upload-title').value = $('file').files[0].name.replace(/\.(wav|mp3|flac|ogg|oga|m4a|aac|aiff|aif)$/i, '').slice(0,200);
});
$('upload').addEventListener('submit', async event => {
  event.preventDefault(); const file = $('file').files[0];
  if (!file) return;
  if (file.size > 100 * 1024 * 1024) return notice('Audio must be no larger than 100 MiB.');
  $('upload-button').disabled = true; $('upload-status').textContent = 'Uploading and converting locally… Keep this window open; playout continues.';
  const params = new URLSearchParams({title:$('upload-title').value, artist:$('upload-artist').value, kind:$('upload-kind').value});
  try {
    await api(`/api/upload?${params}`, file, true);
    $('upload').reset(); $('upload-status').textContent = 'Added to your library.'; notice(''); await refresh();
  } catch (error) { $('upload-status').textContent = 'Import failed.'; notice(error.message); }
  finally { $('upload-button').disabled = false; }
});
$('timezone').textContent = `(${Intl.DateTimeFormat().resolvedOptions().timeZone})`;
$('schedule').addEventListener('submit', async event => {
  event.preventDefault();
  const date = new Date($('schedule-at').value);
  if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now()) return notice('Choose a future time in your browser’s local timezone.');
  await action('schedule.add', {track_id:$('schedule-track').value, at:date.toISOString()});
});
$('program-zone').value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
$('program').addEventListener('submit', async event => {
  event.preventDefault();
  const weekdays = [...$('program').querySelectorAll('input[name="weekday"]:checked')].map(e => e.value).join(',');
  if (!weekdays) return notice('Choose at least one day for this program.');
  await action('program.add', {track_id:$('program-track').value, timezone:$('program-zone').value.trim(), local_time:$('program-time').value, weekdays});
});
function empty(list, message) { list.append(node('li', message, 'empty')); }
function renderStructure(s) {
  queueItems = s.queue.map(item => ({...item})); renderQueue(); renderCrate(s.library);
  const library = $('library'); library.replaceChildren();
  for (const track of s.library) {
    const row = node('li'), main = node('div', undefined, 'item-main'), buttons = node('div', undefined, 'row-actions');
    main.append(node('strong',track.title),node('small',`${track.artist || 'Unknown artist'} · ${track.kind} · ${duration(track.duration)}`));
    if (!track.available) {
        main.append(node('small', `Unavailable · ${track.playback_error}`, 'error'));
        buttons.append(actionButton('Retry repaired audio','library.retry',{track_id:track.id}));
      }
      if (track.available) buttons.append(actionButton('＋ Queue','queue.add',{track_id:track.id}));
      buttons.append(actionButton('Delete','library.delete',{track_id:track.id},true));
    row.append(main,buttons); library.append(row);
  }
  if (!s.library.length) empty(library,'Your library is waiting for its first sound.');
  for (const id of ['schedule-track','program-track']) {
  const select = $(id), selected = select.value; select.replaceChildren();
  select.append(new Option(s.library.length ? 'Choose audio…' : 'Import a track first',''));
  for (const track of s.library) select.append(new Option(`${track.title} · ${track.kind}`,track.id));
  if (s.library.some(t => t.id === selected)) select.value = selected;
  }
  const programs = $('programs'); programs.replaceChildren();
  const dayNames = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  for (const item of s.programs) {
    const row = node('li'), main = node('div', undefined, 'item-main');
    const days = item.weekdays.split(',').map(d => dayNames[Number(d)-1]).join(', ');
    main.append(node('strong', item.title), node('small', `${days} · ${item.local_time} ${item.timezone}`), node('small', `Next: ${new Date(item.next_due * 1000).toLocaleString()} (your timezone)`));
    if (item.last_status) main.append(node('small', `Last due: ${new Date(item.last_due * 1000).toLocaleString()} · ${item.last_status} (not an airing confirmation)`));
    row.append(main, actionButton('Remove','program.remove',{program_id:item.id})); programs.append(row);
  }
  if (!s.programs.length) empty(programs,'No recurring programs. Your music rotation continues as usual.');
  const schedules = $('schedules'); schedules.replaceChildren();
  for (const item of s.schedules) {
    const row=node('li'),main=node('div',undefined,'item-main');
    main.append(node('strong',item.title),node('small',`${new Date(item.at).toLocaleString()} · ${item.status}`)); row.append(main);
    if (item.status === 'pending') row.append(actionButton('Cancel','schedule.cancel',{schedule_id:item.id})); schedules.append(row);
  }
  if (!s.schedules.length) empty(schedules,'No scheduled moments yet.');
  const log=$('history'); log.replaceChildren();
  for (const item of s.history) { const row=node('li');row.append(node('span',`${item.title} · ${item.event}`),node('small',new Date(item.at).toLocaleTimeString()));log.append(row); }
  if (!s.history.length) empty(log,'Station activity will appear here.');
}
function render(s) {
  $('import-capability').textContent=s.imports.conversion_available?'FFmpeg detected: common formats convert automatically.':'FFmpeg not detected: only 44.1 kHz, 16-bit mono/stereo PCM WAV can be imported. Install FFmpeg for other formats.';
  const b=s.broadcast;
  $('broadcast-state').textContent=b.state.toUpperCase();
  $('rotation-state').textContent = s.station.auto_fill ? '↻ Music rotation on · queue plays first' : 'Music rotation off · queue only';
  $('broadcast-health').textContent=`${b.format} · ${b.bytes_sent.toLocaleString()} bytes sent · ${b.connections} connections · ${b.retries} retries` + (b.retry_at ? ` · retry after ${new Date(b.retry_at*1000).toLocaleTimeString()}` : '');
  $('broadcast-error').textContent=b.error||''; $('broadcast-error').hidden=!b.error;
  $('broadcast-saved').textContent=(b.configured?`Saved destination: ${broadcastDestination || 'configured by another session (reload to review)'}`:'No destination saved.') + (b.encoder_available?' FFmpeg detected.':' FFmpeg missing: install it and restart myradio.');
  const busy=b.active||b.state==='stopping';
  $('broadcast-start').disabled=busy||!b.configured||!b.encoder_available||!s.running;
  $('broadcast-stop').disabled=!busy;
  for (const id of ['broadcast-save','broadcast-url','broadcast-user','broadcast-password']) $(id).disabled=busy;
  state = s; $('connection').textContent = '● Local studio connected';
  $('station-name').textContent=s.station.name; $('tagline').textContent=s.station.tagline;
  $('track-title').textContent=s.current?s.current.title:'Your next sound is waiting';
  $('track-artist').textContent=s.current?(s.current.artist||kindLabel[s.current.kind]||s.current.kind):'Import audio, queue a track, and make it yours.';
  $('kind').textContent=s.current?(kindLabel[s.current.kind]||s.current.kind).toUpperCase():'—';
  $('elapsed').textContent=duration(s.elapsed); $('duration').textContent=duration(s.current?.duration);
  $('progress').max=s.current?.duration||1; $('progress').value=s.elapsed;
  $('start').disabled=s.running||!s.library.length; $('stop').disabled=!s.running; $('skip').disabled=!s.current;
  $('onboarding').hidden=!!s.library.length;
  $('library-count').textContent=`${s.library.length} tracks`;
  $('chat-mode').textContent=s.chat_mode==='model'?'Connected AI · approval required':'Local command assistant · no AI connected';
  $('monitor-count').textContent=`${s.stream.listeners} local monitors`;
  $('dropped-count').textContent=`${s.stream.dropped} slow connections dropped`;
  const healthMessage=s.error || s.health.message;
  $('playout-error').hidden=!healthMessage; $('playout-error').textContent=healthMessage;
  if (!identityDirty && !$('identity').contains(document.activeElement)) {
    $('name-input').value=s.station.name; $('tagline-input').value=s.station.tagline; $('auto-fill').checked=s.station.auto_fill;
  }
  const structure=JSON.stringify([s.library,s.queue,s.schedules,s.programs,s.history,s.station.auto_fill]);
  if (structure!==lastStructure && !arranging) {renderStructure(s);lastStructure=structure;}
  renderConsole(s);
  renderGame(s);
}
async function refresh() {
  if (polling||!unlocked) return;
  polling=true;
  try { render(await api('/api/status')); }
  catch (error) { $('connection').textContent='Connection unavailable'; notice(error.message); }
  finally { polling=false; }
}
let chatPinned = true;
$('chat-log').addEventListener('scroll', () => {
  const log = $('chat-log');
  chatPinned = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
});
new ResizeObserver(() => {
  if (chatPinned) $('chat-log').scrollTop = $('chat-log').scrollHeight;
}).observe($('chat-log'));
function chatMessage(role, content) {
  chatPinned = true;
  const article=node('article',undefined,`message ${role}`); article.append(node('span',role==='user'?'YOU':'OPERATOR'),node('p',content));
  $('chat-log').append(article);
  while ($('chat-log').children.length>60) $('chat-log').firstElementChild.remove();
  $('chat-log').scrollTop=$('chat-log').scrollHeight;return article;
}
async function sendChat(message) {
  if ($('send').disabled) return;
  chatMessage('user',message); $('send').disabled=true; $('chat-status').textContent='Operator is thinking…';
  // Invalidate all old proposal buttons when asking a new question.
  for (const button of $('chat-log').querySelectorAll('button')) button.disabled=true;
  try {
    const result=await api('/api/chat',{message}); const article=chatMessage('assistant',result.reply);
    for (const proposal of result.actions||[]) {
      const block=node('div'); block.append(node('pre',`${proposal.operation}\n${JSON.stringify(proposal.arguments,null,2)}`));
      const button=node('button','Approve this change'); button.type='button';
      const expiry=setTimeout(()=>{button.disabled=true;button.textContent='Proposal expired';},120000);
      button.addEventListener('click',async()=>{
        clearTimeout(expiry);button.disabled=true;
        try {await api('/api/approve',{id:proposal.id});button.textContent='Change applied';await refresh();}
        catch(error){button.textContent='Not applied';chatMessage('assistant',error.message);}
      });block.append(button);article.append(block);
    }
    await refresh(); $('chat-log').scrollTop=$('chat-log').scrollHeight;
  } catch(error){chatMessage('assistant',error.message);}
  finally{$('send').disabled=false;$('chat-status').textContent='Your station, under your control.';}
}
$('chat-form').addEventListener('submit',event=>{event.preventDefault();const message=$('message').value.trim();if(message){$('message').value='';sendChat(message);}});
for (const button of document.querySelectorAll('[data-message]')) button.addEventListener('click',()=>sendChat(button.dataset.message));

// ===== Console: meter bridge, cart wall, crate, bin and the control desk =====
const kindLabel = {music: 'Music', speech: 'Spoken', ad: 'Ad'};
let queueItems = [], arranging = false, picked = null, drag = null, monitorOn = false, undoAction = null, toastTimer = 0;
const announcer = node('div', '', 'sr-only'); announcer.setAttribute('aria-live', 'polite'); document.body.append(announcer);
function announce(text) { announcer.textContent = ''; setTimeout(() => { announcer.textContent = text; }, 30); }
function toast(text, undo = null) {
  $('toast-message').textContent = text; undoAction = undo; $('undo').hidden = !undo; $('toast').hidden = false; announce(text);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; undoAction = null; }, undo ? 9000 : 4000);
}
$('toast-close').addEventListener('click', () => { $('toast').hidden = true; undoAction = null; });
$('undo').addEventListener('click', () => { const fn = undoAction; undoAction = null; $('toast').hidden = true; if (fn) fn(); });

// --- the cart wall and the crate
function cartNode(item, index, crate) {
  const cart = node(crate ? 'div' : 'li', undefined, 'cart');
  cart.dataset.kind = item.kind; cart.tabIndex = 0;
  if (crate) cart.dataset.track = item.id; else cart.dataset.entry = String(item.id);
  cart.title = item.artist ? `${item.title} · ${item.artist}` : item.title;
  const kind = kindLabel[item.kind] || item.kind;
  cart.setAttribute('aria-label', `${crate ? 'Library cart' : `Queue position ${index + 1} of ${queueItems.length}`}: ${item.title}${item.artist ? ', ' + item.artist : ''}, ${kind}, ${duration(item.duration)}`);
  if (!crate) cart.setAttribute('aria-describedby', 'keyboard-instructions');
  const grip = node('button', '⠿', 'grip'); grip.type = 'button'; grip.tabIndex = -1; grip.setAttribute('aria-label', `Drag ${item.title}`);
  cart.append(grip);
  if (!crate) cart.append(node('span', String(index + 1).padStart(2, '0'), 'slot'));
  const body = node('div', undefined, 'cart-body'), meta = node('div', undefined, 'cart-meta');
  meta.append(node('span', kind, 'kind'), node('span', item.artist || ''));
  body.append(node('div', item.title, 'cart-title'), meta);
  cart.append(body, node('span', duration(item.duration), 'cart-time'));
  if (crate) {
    const add = node('button', '+', 'add'); add.type = 'button'; add.setAttribute('aria-label', `Add ${item.title} at the end of the queue`);
    add.addEventListener('click', () => addToQueue(item, null)); cart.append(add);
  } else {
    const menu = node('button', '⋯', 'cart-menu'), actions = node('div', undefined, 'cart-actions');
    menu.type = 'button'; menu.setAttribute('aria-label', `Actions for ${item.title}`); menu.setAttribute('aria-expanded', 'false');
    menu.addEventListener('click', () => menu.setAttribute('aria-expanded', String(cart.classList.toggle('tools-open'))));
    for (const [label, name, run, disabled] of [['↑', 'Move up', () => moveEntry(item, index - 1), index === 0],
                                                ['↓', 'Move down', () => moveEntry(item, index + 1), index === queueItems.length - 1],
                                                ['×', 'Remove', () => removeEntry(item), false]]) {
      const button = node('button', label); button.type = 'button'; button.disabled = disabled;
      button.setAttribute('aria-label', `${name}: ${item.title}`); button.addEventListener('click', () => { cancelPick(); run(); });
      actions.append(button);
    }
    cart.append(menu, actions);
  }
  grip.addEventListener('pointerdown', event => startDrag(event, cart, item, crate));
  cart.addEventListener('keydown', event => cartKey(event, cart, item, crate));
  return cart;
}
function renderQueue(focusEntry) {
  const list = $('queue'); list.replaceChildren();
  queueItems.forEach((item, index) => {
    const cart = cartNode(item, index, false);
    if (picked && picked.item.id === item.id) cart.classList.add('picked');
    list.append(cart);
  });
  if (!queueItems.length) {
    const empty = node('li', undefined, 'empty');
    empty.append(node('strong', 'The wall is clear.'), node('span', state && state.station.auto_fill ? 'Music rotation keeps playing. Drag a cart here from the crate, or press + on one.' : 'Drag a cart here from the crate, or press + on one.'));
    list.append(empty);
  }
  $('queue-count').textContent = `${queueItems.length} CART${queueItems.length === 1 ? '' : 'S'}`;
  if (focusEntry !== undefined) {
    const cart = list.querySelector(`[data-entry="${CSS.escape(String(focusEntry))}"]`);
    if (cart) { cart.focus({preventScroll: true}); cart.scrollIntoView({block: 'nearest'}); }
  }
  if (state) renderTape(state);
}
function renderCrate(library) {
  const crate = $('crate'); crate.replaceChildren();
  const ready = library.filter(track => track.available);
  for (const track of ready) crate.append(cartNode(track, 0, true));
  if (!ready.length) { const none = node('p', library.length ? 'No playable audio. Repair it in the Library.' : 'Your crate fills up as you import audio.', 'none'); crate.append(none); }
}

// --- queue changes: shown at once, then confirmed by the station
async function queueChange(operation, args, message, undo) {
  try { await api('/api/action', {operation, arguments: args}); notice(''); if (message) toast(message, undo); }
  catch (error) { notice(error.message); }
  finally { arranging = false; lastStructure = ''; await refresh(); }
}
function moveEntry(item, to, from = queueItems.findIndex(entry => entry.id === item.id)) {
  if (from < 0 || to < 0 || to >= queueItems.length + (from < 0 ? 1 : 0)) return;
  const [entry] = queueItems.splice(from, 1); queueItems.splice(to, 0, entry);
  arranging = true; renderQueue(entry.id);
  announce(`${entry.title} moved to position ${to + 1} of ${queueItems.length}.`);
  queueChange('queue.move', {queue_id: String(entry.id), position: to});
}
function addToQueue(track, at) {
  arranging = true;
  queueChange('queue.add', at === null ? {track_id: track.id} : {track_id: track.id, position: at},
              `${track.title} added ${at === null ? 'at the end' : `at position ${at + 1}`}.`);
}
function removeEntry(item) {
  const at = queueItems.findIndex(entry => entry.id === item.id);
  if (at < 0) return;
  queueItems.splice(at, 1); arranging = true; renderQueue();
  $('bin').classList.add('eat'); setTimeout(() => $('bin').classList.remove('eat'), 450);
  queueChange('queue.remove', {queue_id: String(item.id)}, `${item.title} removed from the queue.`,
              () => { arranging = true; queueChange('queue.add', {track_id: item.track_id, position: at}, `${item.title} is back in slot ${at + 1}.`); });
}

// --- keyboard: pick up, move, put down
function cancelPick(restore = true) {
  if (!picked) return;
  if (restore) { queueItems = picked.before; }
  const id = picked.item.id; picked = null; arranging = false; renderQueue(id);
}
function cartKey(event, cart, item, crate) {
  if (event.target !== cart) return;
  const key = event.key;
  if (crate) {
    if (key === 'Enter' || key === ' ') { event.preventDefault(); addToQueue(item, null); }
    return;
  }
  const index = queueItems.findIndex(entry => entry.id === item.id);
  if (key === ' ' || key === 'Enter') {
    event.preventDefault();
    if (!picked) {
      picked = {item, from: index, before: queueItems.slice()}; arranging = true; renderQueue(item.id);
      announce(`Picked up ${item.title}, position ${index + 1} of ${queueItems.length}. Use the arrow keys to move it, then Space to put it down, or Escape to cancel.`);
    } else {
      const from = picked.from; picked = null;
      if (index === from) { arranging = false; renderQueue(item.id); announce(`${item.title} put back in position ${index + 1}.`); }
      else { renderQueue(item.id); announce(`${item.title} put down in position ${index + 1}.`); queueChange('queue.move', {queue_id: String(item.id), position: index}); }
    }
  } else if (key === 'ArrowUp' || key === 'ArrowDown') {
    event.preventDefault();
    const to = index + (key === 'ArrowUp' ? -1 : 1);
    if (to < 0 || to >= queueItems.length) return;
    if (picked) {
      const [entry] = queueItems.splice(index, 1); queueItems.splice(to, 0, entry); renderQueue(entry.id);
      announce(`Position ${to + 1} of ${queueItems.length}.`);
    } else {
      $('queue').querySelector(`[data-entry="${CSS.escape(String(queueItems[to].id))}"]`)?.focus();
    }
  } else if (key === 'Escape' && picked) {
    event.preventDefault(); cancelPick(); announce('Move cancelled.');
  } else if ((key === 'Delete' || key === 'Backspace') && !picked) {
    event.preventDefault();
    const next = queueItems[index + 1] || queueItems[index - 1];
    removeEntry(item);
    if (next) $('queue').querySelector(`[data-entry="${CSS.escape(String(next.id))}"]`)?.focus(); else $('coming-up').focus();
  }
}

// --- pointer dragging (mouse, pen and touch, by the grip)
function startDrag(event, cart, item, crate) {
  if (event.button !== 0 || picked || drag) return;
  event.preventDefault();
  const rect = cart.getBoundingClientRect();
  drag = {item, crate, cart, pointer: event.pointerId, x: event.clientX, y: event.clientY, dx: event.clientX - rect.left, dy: event.clientY - rect.top, ghost: null, target: null};
  try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* the drag still works without capture */ }
  arranging = true;
}
function deckTop() { const deck = $('deck'); return deck.offsetParent ? deck.getBoundingClientRect().top : innerHeight; }
function dropTarget(x, y) {
  const bin = $('bin').getBoundingClientRect(), scroll = $('queue-scroll').getBoundingClientRect();
  if (y >= deckTop()) return null;                                          // over the desk: not a slot
  if (!drag.crate && x >= bin.left && x <= bin.right && y >= bin.top && y <= bin.bottom) return {bin: true};
  if (x < scroll.left || x > scroll.right || y < scroll.top - 24 || y > scroll.bottom + 24) return null;
  const others = [...$('queue').querySelectorAll('.cart')].filter(cart => cart !== drag.cart);
  let index = others.length;
  for (const [i, cart] of others.entries()) { const r = cart.getBoundingClientRect(); if (y < r.top + r.height / 2) { index = i; break; } }
  return {index, others};
}
function showMarker(target) {
  const marker = $('drop-marker');
  $('bin').classList.toggle('over', !!(target && target.bin));
  if (!target || target.bin) { marker.hidden = true; return; }
  const {index, others} = target;
  marker.style.top = `${others.length === 0 ? 2 : index < others.length ? others[index].offsetTop - 6 : others[others.length - 1].offsetTop + others[others.length - 1].offsetHeight + 2}px`;
  marker.hidden = false;
}
document.addEventListener('pointermove', event => {
  if (!drag || event.pointerId !== drag.pointer) return;
  if (!drag.ghost) {
    if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) return;
    drag.ghost = drag.cart.cloneNode(true); drag.ghost.classList.add('drag-ghost'); drag.ghost.removeAttribute('id');
    drag.ghost.setAttribute('aria-hidden', 'true'); document.body.append(drag.ghost); drag.cart.classList.add('source');
  }
  drag.ghost.style.left = `${event.clientX - drag.dx}px`; drag.ghost.style.top = `${event.clientY - drag.dy}px`;
  const scroll = $('queue-scroll'), r = scroll.getBoundingClientRect(), floor = deckTop();
  if (event.clientY < r.top + 40 && event.clientY > r.top - 30) scroll.scrollTop -= 14;
  else if (event.clientY > Math.min(r.bottom, floor) - 40 && event.clientY < r.bottom + 30) scroll.scrollTop += 14;
  if (event.clientY > floor - 48) window.scrollBy(0, 16); else if (event.clientY < 72) window.scrollBy(0, -16);   // the page scrolls too, so every slot can be reached
  drag.target = dropTarget(event.clientX, event.clientY); showMarker(drag.target);
});
function endDrag(commit) {
  if (!drag) return;
  const {item, crate, cart, ghost, target} = drag; drag = null;
  ghost?.remove(); cart.classList.remove('source'); $('drop-marker').hidden = true; $('bin').classList.remove('over');
  if (!commit || !ghost || !target) { arranging = false; return; }
  if (target.bin) removeEntry(item);
  else if (crate) addToQueue(item, target.index);
  else {
    const from = queueItems.findIndex(entry => entry.id === item.id);
    if (from === target.index) { arranging = false; return; }
    moveEntry(item, target.index, from);
  }
}
document.addEventListener('pointerup', event => { if (drag && event.pointerId === drag.pointer) endDrag(true); });
document.addEventListener('pointercancel', event => { if (drag && event.pointerId === drag.pointer) endDrag(false); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && drag) endDrag(false); });

// --- meter bridge: lamp, LCD, paper trace, real levels from the browser monitor
const LAMP = {disconnected: ['PRIVATE', '', 'Not broadcasting'], connecting: ['CONNECTING', 'busy', 'Connecting to your stream host…'],
              encoding: ['CONNECTING', 'busy', 'Starting the encoder…'], streaming: ['LIVE', 'live', 'LIVE · stream connected'],
              retrying: ['RETRYING', 'busy', 'Host unavailable · retrying'], failed: ['FAILED', 'failed', 'Output failed · see Broadcast'],
              stopping: ['STOPPING', 'busy', 'Disconnecting…']};
let tapeKey = '';
const WAVE = '<svg viewBox="0 0 250 26" preserveAspectRatio="none" aria-hidden="true"><path d="M0 13h7l3-4 4 9 4-12 3 17 4-19 4 13 4-4h8l4-7 4 17 4-19 4 12 5-4h8l4 4 4-9 4 15 4-21 4 20 4-12 4 5h9l4-8 5 13 4-17 4 19 4-12 4 4h14l4-5 4 13 4-19 4 20 4-12 4 5h14l4-9 4 16 5-20 4 19 4-12 4 6h15l4-5 4 10 4-16 4 20 4-11 4 2h11"/></svg>';
function renderTape(s) {
  const items = [s.current || {title: s.running ? 'WAITING FOR AUDIO' : 'STOPPED · READY TO ROLL', kind: 'music', idle: true}, ...queueItems.slice(0, 3)];
  const key = items.map(item => item.title + item.kind).join('|');
  if (key === tapeKey) return; tapeKey = key;
  const tape = $('tape'); tape.replaceChildren();
  for (let copy = 0; copy < 2; copy++) for (const item of items) {
    const segment = node('div', undefined, 'trace-segment');
    segment.append(node('span', item.idle ? item.title : `${(kindLabel[item.kind] || item.kind).toUpperCase()} · ${item.title}`));
    segment.insertAdjacentHTML('beforeend', WAVE); tape.append(segment);
  }
}
function renderConsole(s) {
  const b = s.broadcast, [word, tone, detail] = LAMP[b.state] || [b.state.toUpperCase(), 'busy', b.state];
  const outputBusy = b.active || b.state === 'stopping';
  $('state-word').textContent = word; $('state-lamp').className = 'state-lamp' + (tone ? ' ' + tone : '');
  $('stream-detail').textContent = detail;
  $('play-label').textContent = b.state === 'streaming' && s.current ? 'ON AIR · LIVE STREAM' : s.current ? 'PLAYING · LOCAL PLAYOUT' : s.running ? 'LOCAL PLAYOUT · WAITING FOR AUDIO' : 'LOCAL PLAYOUT · STOPPED';
  const next = queueItems[0];
  $('next-readout').textContent = next ? `UP NEXT → ${next.title}${next.artist ? ' · ' + next.artist : ''}` : s.station.auto_fill ? 'UP NEXT → music rotation' : 'UP NEXT → add a cart to the wall';
  const agentAge = s.agent_link && s.agent_link.seen_at ? Date.now() / 1000 - s.agent_link.seen_at : null;
  $('local-status').lastChild.textContent = `Playout ${s.running ? 'running' : 'stopped'} · Monitor ${monitorOn ? 'on' : 'off'}` + (agentAge !== null && agentAge < 1800 ? ` · Agent DJ seen ${ago(agentAge)}` : '');
  $('local-status').firstChild.style.color = s.running ? 'var(--accent)' : 'var(--muted)';
  $('transport-note').textContent = s.running ? (outputBusy ? 'Stop also ends the live stream.' : s.current ? `Playing ${s.current.title}` : 'Waiting for playable audio.') : s.library.length ? 'Your next sound is standing by.' : 'Import audio to start.';
  $('broadcast-note').textContent = outputBusy ? detail : b.configured ? 'Private until you say otherwise.' : 'No stream host saved yet.';
  $('guard').disabled = outputBusy; $('deck-disconnect').disabled = !outputBusy || b.state === 'stopping';
  // On air, the guard burns like a warm fire; while connecting it is just catching.
  const onAir = b.state === 'streaming', igniting = !onAir && outputBusy && b.state !== 'stopping';
  $('guard').classList.toggle('on-air', onAir); $('guard').classList.toggle('igniting', igniting);
  if (onAir !== ($('guard').dataset.live === '1')) {
    $('guard').dataset.live = onAir ? '1' : '';
    $('guard').innerHTML = onAir ? '<span><i class="ember-dot" aria-hidden="true"></i>ON AIR<span class="long"> · LIVE</span></span>' : GUARD_IDLE;
    $('guard').setAttribute('aria-label', onAir ? 'On air: your station is live' : 'Lift the GO LIVE guard');
  }
  $('deck-connect').hidden = outputBusy || b.configured; $('broadcast-note').hidden = !$('deck-connect').hidden;
  if (document.body.dataset.activeView === 'connect') renderLinks(s);
  document.body.classList.toggle('playing', !!s.current);
  renderTape(s); updateGuard(s);
}

// --- the GO LIVE guard (desk and Broadcast view)
const GUARD_IDLE = $('guard').innerHTML;
function updateGuard(s) {
  const b = s.broadcast, busy = b.active || b.state === 'stopping';
  $('go-live').disabled = busy || !s.running || !b.configured || !b.encoder_available;
  $('live-destination').textContent = b.configured ? `Destination: ${broadcastDestination || 'saved in another session (reload to review)'}` : 'No stream host saved yet.';
  $('live-warning').textContent = busy ? 'Already connected or busy. Disconnect first.' : !b.configured ? 'Set up your stream host in Broadcast first.' : !b.encoder_available ? 'FFmpeg is missing: install it and restart myradio.' : !s.running ? 'Start the station on the desk first; then the toggle unlocks.' : 'Confirm the destination and your broadcasting rights, then flip the toggle.';
}
async function openGuard() {
  try { broadcastDestination = (await api('/api/broadcast')).url || ''; } catch (error) { notice(error.message); }
  if (state) updateGuard(state);
  $('live-dialog').showModal();
}
$('guard').addEventListener('click', openGuard);
$('go-live').addEventListener('click', () => { $('live-dialog').close(); action('broadcast.start'); });
$('deck-disconnect').addEventListener('click', () => action('broadcast.stop'));
for (const element of document.querySelectorAll('#live-dialog [data-close]')) element.addEventListener('click', () => $('live-dialog').close());
$('skip').addEventListener('click', () => { if (motionOff) return; document.body.classList.add('whizz'); setTimeout(() => document.body.classList.remove('whizz'), 650); });

// --- browser monitor levels: measured from the stream this browser is playing, never invented
let graph = null, meterFrame = 0;
function ensureGraph() {
  if (graph !== null) return graph || null;
  const Context = window.AudioContext || window.webkitAudioContext;
  try {
    const ctx = new Context(), source = ctx.createMediaElementSource($('audio')), split = ctx.createChannelSplitter(2), gain = ctx.createGain();
    const left = ctx.createAnalyser(), right = ctx.createAnalyser(); left.fftSize = right.fftSize = 1024;
    source.connect(split); split.connect(left, 0); split.connect(right, 1); source.connect(gain); gain.connect(ctx.destination);
    graph = {ctx, gain, left, right, buffer: new Float32Array(1024)}; setVolume();
  } catch { graph = false; }
  return graph || null;
}
function level(analyser) {
  analyser.getFloatTimeDomainData(graph.buffer);
  let peak = 0; for (const sample of graph.buffer) peak = Math.max(peak, Math.abs(sample));
  const db = peak > 0 ? 20 * Math.log10(peak) : -Infinity;
  return Math.max(0, Math.min(100, (db + 48) / 48 * 100));
}
function startMeters() {
  cancelAnimationFrame(meterFrame);
  $('vu-label').textContent = graph ? 'MONITOR LEVEL' : 'NO LEVEL HERE';
  if (!graph) return;
  const tick = () => {
    if (!monitorOn) return;
    $('vu-left').style.setProperty('--level', level(graph.left) + '%'); $('vu-right').style.setProperty('--level', level(graph.right) + '%');
    meterFrame = requestAnimationFrame(tick);
  };
  tick();
}
function stopMeters() {
  cancelAnimationFrame(meterFrame); $('vu-label').textContent = 'MONITOR OFF';
  for (const id of ['vu-left', 'vu-right']) $(id).style.setProperty('--level', '0%');
}
function setVolume() {
  const value = Number($('volume').value);
  $('volume-out').textContent = `${value}%`; $('knob').style.setProperty('--angle', `${value * 2.7 - 135}deg`);
  if (graph) { graph.gain.gain.value = value / 100; $('audio').volume = 1; } else $('audio').volume = value / 100;
}
$('volume').addEventListener('input', setVolume); setVolume();

// ===== Connect your agent: one copy-paste so an agent can DJ the station and set up its outbound stream =====
let connectInfo = null, connectLoading = false;
function ago(seconds) {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}
const AGENT_DID = {'queue.move': 'moved a cart', 'queue.add': 'added a cart', 'queue.remove': 'removed a cart', 'transport.skip': 'skipped a track', 'transport.fade_next': 'faded to the next track',
                   'transport.start': 'started the station', 'transport.stop': 'stopped the station', status: 'checked the station', broadcast: 'saved the stream',
                   'schedule.add': 'scheduled a moment', 'program.add': 'added a weekly program', 'broadcast.start': 'went live', 'broadcast.stop': 'disconnected the stream'};
async function loadConnect() {
  if (connectLoading || !unlocked) return;
  connectLoading = true;
  try { connectInfo = await api('/api/connect'); renderKit(); if (state) renderLinks(state); }
  catch (error) { notice(error.message); }
  finally { connectLoading = false; }
}
function kitText(info, dj, stream) {
  const config = JSON.stringify({mcpServers: {myradio: {command: info.python, args: ['-m', 'myradio.mcp', '--data-dir', info.data_dir, '--port', String(info.port)], cwd: info.package_dir}}}, null, 2);
  const job = dj && stream ? 'DJ my radio station and set up its outbound stream' : dj ? 'DJ my radio station' : stream ? 'set up the outbound stream for my radio station' : 'connect to my radio station';
  const cli = `"${info.python}" -m myradio.connect --data-dir "${info.data_dir}" --port ${info.port}`;
  const lines = [`You are going to ${job}. The station is "${info.station}", running in myradio on this computer.`, '',
    '1. Connect to the station', 'Add this MCP server to your MCP settings, then reload your tools:', '', config, '',
    'The adapter reads the station’s private control code from that data folder by itself. Never ask me for it, print it or copy it anywhere.',
    'Check the connection by calling station_status. The studio’s Connect your agent panel shows when you do.'];
  let n = 2;
  if (dj) lines.push('', `${n++}. Be the DJ`,
    '- Keep 3 to 6 carts queued and plan a flow: vary energy and tempo, avoid the same artist twice in a row, and don’t repeat a song within an hour.',
    '- Your tools: station_status, queue_add (position 0 plays next), queue_move, queue_remove, transport_start, transport_fade_next (fades out and plays the next cart straight away), transport_skip, schedule_add, program_add and generation_enqueue.',
    '- You write the DJ’s words. If the station has a DJ voice (station_status shows the voice maker ready), write short, lively lines yourself: a lead-in naming the next song every 2 or 3 songs and a station ID about every 15 minutes. Ask for each with generation_enqueue (role host_break or id, priority 85 so it is made now), then queue it right before its song. Without a DJ voice, queue the station’s spoken carts (library kind "speech") instead.',
    '- Don’t delete library audio, rename the station, or call broadcast_start or broadcast_stop unless I ask. broadcast_start makes the station public.',
    '- Check station_status every few minutes. After each change, tell me in one line what you did and why.');
  if (stream) lines.push('', `${n++}. Set up the outbound stream`,
    'myradio sends 128 kbit/s MP3 to an Icecast-compatible server. A remote server needs HTTPS with a valid certificate; plain http only works to this computer, which is fine for a test.',
    '- If I don’t have a server yet, help me choose: my own Icecast or AzuraCast server, or a host that gives Icecast source details (mount URL, source user, source password). For a quick private test you can run Icecast on this computer.',
    '- Save the destination with this command. It reads the source password from MYRADIO_SOURCE_PASSWORD, so the password never appears in our chat. Set the variable from wherever the password already is (for example a file your server setup wrote), never by typing it into a command:',
    `  MYRADIO_SOURCE_PASSWORD="$(cat /path/to/source-password)" ${cli} stream --url "https://your-host:8443/live.mp3"`,
    '- If only I know the password, ask me to paste the stream address into the studio’s "Or connect a stream yourself" box instead.',
    `- Check it with: ${cli} status`,
    '- Then tell me it’s ready. Going live is my decision: I press GO LIVE in the studio, or I tell you to call broadcast_start.');
  return lines.join('\n');
}
function renderKit() { if (connectInfo) $('kit-text').textContent = kitText(connectInfo, $('kit-dj').checked, $('kit-stream').checked); }
for (const id of ['kit-dj', 'kit-stream']) $(id).addEventListener('change', () => { renderKit(); $('kit-copied').textContent = ''; });
$('kit-copy').addEventListener('click', async () => {
  if (!connectInfo) await loadConnect();
  const text = $('kit-text').textContent;
  try { await navigator.clipboard.writeText(text); $('kit-copied').textContent = 'Copied. Paste it into your agent.'; }
  catch {
    const range = document.createRange(); range.selectNodeContents($('kit-text'));
    const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    $('kit-copied').textContent = 'Selected. Press Ctrl+C (⌘C on a Mac) to copy it.';
  }
});
let destinationAsked = false;
function renderLinks(s) {
  if (!connectInfo) loadConnect();
  const link = s.agent_link || {}, age = link.seen_at ? Math.max(0, Date.now() / 1000 - link.seen_at) : null, agent = $('link-agent');
  agent.className = age === null ? 'waiting' : age < 900 ? 'ok' : 'stale';
  agent.querySelector('.ico').textContent = age === null ? '…' : '✓';
  $('link-agent-text').textContent = age === null ? 'Not seen yet. This turns green when your agent first checks the station.'
    : `Last used the station ${ago(age)}${link.last ? `: ${AGENT_DID[link.last] || link.last}` : ''}.`;
  const b = s.broadcast, stream = $('link-stream');
  if (b.configured && !broadcastDestination && !destinationAsked) { destinationAsked = true; api('/api/broadcast').then(r => { broadcastDestination = r.url || ''; }).catch(() => {}); }
  stream.className = b.state === 'streaming' ? 'ok' : b.configured ? 'ok' : 'waiting';
  stream.querySelector('.ico').textContent = b.configured ? '✓' : '…';
  $('link-stream-text').textContent = b.state === 'streaming' ? 'LIVE: connected to your stream host.'
    : b.configured ? `Saved: ${broadcastDestination || 'a destination'}. Press GO LIVE on the desk when you’re ready.` : 'No stream saved yet.';
}
function parseStream(text) {
  const raw = text.trim();
  if (!raw) return null;
  const icecast = /^icecast:\/\//i.test(raw);
  let address;
  try { address = new URL(icecast ? raw.replace(/^icecast:\/\//i, 'https://') : raw); }
  catch { return {error: 'That doesn’t look like a stream address. It starts with icecast:// or https://.'}; }
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(address.hostname);
  const scheme = icecast ? (local ? 'http:' : 'https:') : address.protocol;
  if (!['http:', 'https:'].includes(scheme)) return {error: 'Use an icecast://, https:// or (to this computer) http:// address.'};
  const password = decodeURIComponent(address.password), username = decodeURIComponent(address.username) || 'source';
  if (!password) return {error: 'Add the source password: icecast://source:PASSWORD@host:port/mount'};
  if (!address.pathname || address.pathname === '/') return {error: 'Add the mount at the end, for example /live.mp3'};
  return {url: `${scheme}//${address.host}${address.pathname}`, username, password};
}
$('stream-address').addEventListener('input', () => {
  const parsed = parseStream($('stream-address').value), out = $('stream-parsed');
  out.textContent = !parsed ? '' : parsed.error ? parsed.error : `Host ${new URL(parsed.url).host} · mount ${new URL(parsed.url).pathname} · user ${parsed.username} · password ${'•'.repeat(Math.min(12, parsed.password.length))}`;
  out.classList.toggle('error', !!(parsed && parsed.error));
});
$('stream-paste').addEventListener('submit', async event => {
  event.preventDefault();
  const parsed = parseStream($('stream-address').value), out = $('stream-parsed');
  if (!parsed || parsed.error) { out.textContent = parsed ? parsed.error : 'Paste your stream address first.'; out.classList.add('error'); return; }
  $('stream-save').disabled = true;
  try {
    await api('/api/broadcast', parsed);
    broadcastDestination = parsed.url; $('stream-address').value = '';
    out.classList.remove('error'); out.textContent = `Saved ${parsed.url}. Press GO LIVE on the desk when you’re ready.`;
    toast('Stream saved. Going live is still your call, on the desk.'); await refresh();
  } catch (error) { out.textContent = error.message; out.classList.add('error'); }
  finally { $('stream-save').disabled = false; }
});

// --- the game layer: Dial the station pet, levels, trophies and celebrations. Everything comes from real station facts:
// lifetime stats kept by the engine (status.stats), the library, what is playing, the stream and the agent link.
const LEVELS = [[0, 'Bedroom Booth'], [50, 'Garage Signal'], [150, 'Basement Broadcast'], [350, 'Rooftop Antenna'], [700, 'Pirate Frequency'],
  [1200, 'Night City FM'], [2000, 'Full Power'], [3200, 'Big Signal'], [5000, 'Legend of the Dial']];
const BADGES = [
  ['first-song', 'First song', 'Your station played its first song.', (g) => g.stats.aired_music >= 1],
  ['first-break', 'First DJ break', 'A DJ break or station ID went out between songs.', (g) => g.stats.aired_speech >= 1],
  ['first-hour', 'First hour on air', 'A full hour of airtime, all told.', (g) => g.stats.on_air_seconds >= 3600],
  ['premieres', 'Made here ×10', 'Ten songs and voices made right here by your music and voice makers.', (g) => g.made >= 10],
  ['crate-digger', 'Crate digger', 'Twenty-five tracks in your library.', (g) => g.library >= 25],
  ['night-owl', 'Night owl', 'On air between midnight and four in the morning.', (g) => g.running && new Date().getHours() < 4],
  ['agent-dj', 'Agent DJ', 'Your own agent took the decks.', (g) => g.agentSeen],
  ['went-live', 'Went live', 'Your station streamed to the world.', (g) => g.live],
];
const artUrl = (name) => (window.MYRADIO_ART && window.MYRADIO_ART[name]) || `/art/${name}.png`;
const keep = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const save = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* not kept in this browser */ } };
let earned = new Set(keep('myradio-badges', [])), lastLevel = keep('myradio-level', null), knownTracks = null, premiereAt = 0, petPoke = 0;
const xpOf = (st) => Math.floor(st.on_air_seconds / 60) + 10 * st.aired_music + 5 * st.aired_speech + st.fades;
function levelOf(xp) { let i = 0; while (i + 1 < LEVELS.length && xp >= LEVELS[i + 1][0]) i++; return i; }
function minutes(sec) { const m = Math.floor(sec / 60); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`; }
function celebrate(text) {
  toast(text);
  if (motionOff || reducedMotion.matches) return;
  const layer = $('confetti'), colors = ['#d8edac', '#ff946e', '#e9c46a', '#8fd3ff', '#f4f2ed'];
  for (let i = 0; i < 36; i++) {
    const bit = document.createElement('i');
    bit.style.left = `${8 + Math.random() * 84}%`; bit.style.background = colors[i % colors.length];
    bit.style.setProperty('--drift', `${(Math.random() - 0.5) * 240}px`); bit.style.animationDelay = `${Math.random() * 0.35}s`;
    layer.append(bit); setTimeout(() => bit.remove(), 2400);
  }
}
function petMood(s, g) {
  const cur = s.current, live = s.broadcast.state === 'streaming';
  if (!s.running) return ['sleepy', 'Zzz… press Start to wake me.'];
  if (live) return ['onair', 'We’re live! Say hi to the world.'];
  if (Date.now() - premiereAt < 90000) return ['excited', 'A brand-new song just landed!'];
  if (!cur || (!queueItems.length && !s.station.auto_fill)) return ['hungry', 'I’m hungry… tap me to queue a song.'];
  if (cur.kind === 'speech') return ['happy', 'Shh, the DJ is talking.'];
  return ['singing', `♪ ${cur.title}`];
}
function renderGame(s) {
  const st = s.stats || {on_air_seconds: 0, aired_music: 0, aired_speech: 0, aired_ad: 0, fades: 0};
  const gen = s.extensions && s.extensions.generation && s.extensions.generation.status;
  const agentAge = s.agent_link && s.agent_link.seen_at ? Date.now() / 1000 - s.agent_link.seen_at : null;
  const g = {stats: st, running: s.running, library: s.library.length, made: gen && gen.jobs ? gen.jobs.succeeded || 0 : 0,
             agentSeen: agentAge !== null && agentAge < 1800, live: s.broadcast.state === 'streaming'};
  // a new song in the library: a premiere worth celebrating (not on the first look at the library)
  const music = s.library.filter((t) => t.kind === 'music').map((t) => t.id);
  if (knownTracks) { const fresh = s.library.find((t) => t.kind === 'music' && !knownTracks.has(t.id)); if (fresh) { premiereAt = Date.now(); celebrate(`New song! “${fresh.title}” just came off the machine.`); } }
  knownTracks = new Set(music);
  // level and XP
  const xp = xpOf(st), lv = levelOf(xp), [floor, name] = LEVELS[lv], next = LEVELS[lv + 1];
  const pct = next ? Math.min(100, Math.round(((xp - floor) / (next[0] - floor)) * 100)) : 100;
  $('lv-number').textContent = `LV ${lv + 1}`; $('lv-name').textContent = name; $('xp-fill').style.width = `${pct}%`;
  $('level-title').textContent = `LV ${lv + 1} · ${name}`; $('level-xp').textContent = `${xp.toLocaleString()} XP`; $('xp-big').style.width = `${pct}%`;
  $('level-next').textContent = next ? `${(next[0] - xp).toLocaleString()} XP to LV ${lv + 2}, ${next[1]}. XP comes from minutes on air and songs and breaks played.` : 'Top level. Legend of the dial.';
  $('stat-air').textContent = minutes(st.on_air_seconds); $('stat-songs').textContent = st.aired_music.toLocaleString();
  $('stat-breaks').textContent = st.aired_speech.toLocaleString(); $('stat-fades').textContent = st.fades.toLocaleString();
  if (lastLevel !== null && lv > lastLevel) celebrate(`Level up! LV ${lv + 1} · ${name}`);
  if (lastLevel !== lv) { lastLevel = lv; save('myradio-level', lv); }
  // badges: earned once, kept in this browser; the facts that earn them are the station's own
  for (const [id, title, , test] of BADGES) if (!earned.has(id) && test(g)) { earned.add(id); save('myradio-badges', [...earned]); celebrate(`Badge unlocked: ${title}`); }
  const shelf = $('badges'); shelf.textContent = '';
  for (const [id, title, how] of BADGES) {
    const got = earned.has(id), li = node('li', undefined, 'badge-slot' + (got ? ' got' : ''));
    const img = node('img'); img.src = artUrl(`badge-${id}`); img.alt = ''; img.width = 64; img.height = 64; img.addEventListener('error', () => img.classList.add('missing'));
    li.append(img, node('strong', title), node('small', got ? how : `Locked · ${how}`)); shelf.append(li);
  }
  $('badge-count').textContent = `${earned.size} of ${BADGES.length}`;
  // Dial
  const [mood, line] = Date.now() - petPoke < 2500 ? [petMoodNow, petLine] : petMood(s, g);
  // the small masthead Dial sings without the drawn notes (it bops instead); the Trophies sheet shows the full art
  for (const [el, art] of [[$('pet-img'), mood === 'singing' ? 'happy' : mood], [$('pet-big'), mood]]) {
    if (el.dataset.mood !== art) { el.dataset.mood = art; el.src = artUrl(`dial-${art}`); }
  }
  $('pet').dataset.mood = mood; $('pet-say').textContent = line; $('pet-mood').textContent = line;
}
let petMoodNow = 'happy', petLine = '';
const PET_LINES = ['Best station on the dial.', 'Turn it up!', 'Every song here was made for you.', 'I love this one.', 'Beep boop, still on air.', 'You’re a natural at this.'];
$('pet').addEventListener('click', async () => {
  const mood = $('pet').dataset.mood;
  $('pet').classList.remove('boing'); void $('pet').offsetWidth; $('pet').classList.add('boing');
  if (mood === 'hungry' && state) {
    const pick = state.library.find((t) => t.kind === 'music' && t.available !== false);
    if (!pick) { toast('Dial is hungry, but the crate is empty. Add audio or ask your agent for a song.'); return; }
    try { await api('/api/action', {operation: 'queue.add', arguments: {track_id: pick.id}}); petMoodNow = 'excited'; petLine = `Yum! Queued “${pick.title}”.`; petPoke = Date.now(); toast(`Dial queued “${pick.title}”.`); await refresh(); }
    catch (error) { toast(error.message); }
    return;
  }
  petMoodNow = mood === 'sleepy' ? 'sleepy' : 'excited'; petLine = mood === 'sleepy' ? 'Five more minutes… (press Start)' : PET_LINES[Math.floor(Math.random() * PET_LINES.length)];
  petPoke = Date.now(); if (state) renderGame(state);
});
for (const el of [$('pet-img'), $('pet-big')]) el.addEventListener('error', () => el.classList.add('missing'));

// --- decorative motion (paper trace): pausable, and off for reduced motion
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let motionOff = false;
try { motionOff = localStorage.getItem('myradio-motion') === 'off'; } catch { /* storage may be unavailable */ }
function applyMotion() {
  const off = motionOff || reducedMotion.matches;
  document.body.classList.toggle('motion-off', off);
  $('motion').setAttribute('aria-pressed', String(off)); $('motion').disabled = reducedMotion.matches;
  $('motion').textContent = reducedMotion.matches ? 'Motion off · reduced motion' : off ? '▶ Resume motion' : 'Ⅱ Pause motion';
}
$('motion').addEventListener('click', () => {
  motionOff = !motionOff; try { localStorage.setItem('myradio-motion', motionOff ? 'off' : 'on'); } catch { /* not kept */ }
  applyMotion(); announce(motionOff ? 'Decorative motion paused.' : 'Decorative motion resumed.');
});
reducedMotion.addEventListener('change', applyMotion); applyMotion();
setInterval(()=>{if(unlocked&&!document.hidden)refresh();},2000);
if(token)establish();else requireUnlock();
