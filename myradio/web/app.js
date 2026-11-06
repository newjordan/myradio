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
// Views keep forms mounted so navigation never discards a draft.
const viewNames = ['studio', 'library', 'schedule', 'broadcast', 'settings', 'help', 'operator'];
function showView(moveFocus = false) {
  const route = location.hash.slice(1), selected = route === 'import' ? 'library' : viewNames.includes(route) ? route : 'studio';
  const mainView = selected === 'operator' ? 'studio' : selected;
  for (const name of viewNames.filter(name => name !== 'operator')) $('view-' + name).hidden = name !== mainView;
  document.body.dataset.activeView = selected;
  for (const link of document.querySelectorAll('.rail a[data-view]')) {
    if (link.dataset.view === selected) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  if (route === 'import') $('import').open = true;
  if (moveFocus) {
    const target = selected === 'operator' ? $('message') : route === 'import' ? $('file') : $('main');
    target.focus({preventScroll: true});
    if (selected !== 'operator') window.scrollTo({top: 0});
  }
}
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
  $('audio').hidden = true; $('listen').hidden = false; $('disconnect').hidden = true;
}
$('lock').addEventListener('click', async () => {
  disconnect(); $('agent-key').value=''; $('broadcast-password').value='';
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
function actionButton(label, operation, args, dangerous = false) {
  const button = node('button', label, dangerous ? 'danger' : '');
  button.type = 'button';
  button.addEventListener('click', async () => {
    if (dangerous && !confirm('Delete this audio from the local library? This cannot be undone.')) return;
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
for (const [id, op] of [['start','transport.start'],['stop','transport.stop'],['skip','transport.skip']]) {
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
  if(!confirm('Forget the saved AI connection and clear this session’s chat/proposals? Local command mode will remain available.'))return;
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
$('broadcast-start').addEventListener('click', async () => {
  try {
  broadcastDestination=(await api('/api/broadcast')).url||'';
  if (confirm(`Go live to ${broadcastDestination || 'your saved Icecast destination'}? Your audio may become publicly available. Confirm the destination and your broadcasting rights before continuing.`)) action('broadcast.start');
  } catch (error) { notice(error.message); }
});
$('broadcast-stop').addEventListener('click', () => action('broadcast.stop'));
$('listen').addEventListener('click', async () => {
  try {
    await api('/api/session', {});
    $('audio').src = '/stream.wav'; $('audio').hidden = false;
    $('listen').hidden = true; $('disconnect').hidden = false;
    await $('audio').play();
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
  const queue = $('queue'); queue.replaceChildren();
  for (const [index, item] of s.queue.entries()) {
    const row = node('li'), main = node('div', undefined, 'item-main');
    main.append(node('strong', item.title), node('small', `${item.artist || item.kind} · ${duration(item.duration)}`));
    row.append(node('span', String(index + 1).padStart(2, '0'), 'queue-position'), node('span', 'mr.', 'queue-art'), main, actionButton('Remove','queue.remove',{queue_id:String(item.id)})); queue.append(row);
  }
  if (!s.queue.length) empty(queue, s.station.auto_fill ? 'Queue is clear. Music rotation is ready.' : 'Queue is clear. Add audio from your library.');
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
  const outputBusy = b.active || b.state === 'stopping';
  $('audience-state').textContent = outputBusy ? `EXTERNAL OUTPUT · ${b.state.toUpperCase()}` : 'PRIVATE · NOT BROADCASTING';
  $('audience-state').classList.toggle('external', outputBusy);
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
  $('air-state').textContent=s.running?'LOCAL PLAYOUT':'STOPPED'; $('air-state').classList.toggle('running',s.running);
  $('play-label').textContent=s.current?'NOW PLAYING':s.running?'WAITING FOR AUDIO':'NOT PLAYING';
  $('track-title').textContent=s.current?s.current.title:'Your next sound is waiting';
  $('track-artist').textContent=s.current?(s.current.artist||s.current.kind):'Import audio, queue a track, and make it yours.';
  $('kind').textContent=s.current?s.current.kind.toUpperCase():'LOCAL PLAYOUT';
  $('elapsed').textContent=duration(s.elapsed); $('duration').textContent=duration(s.current?.duration);
  $('progress').max=s.current?.duration||1; $('progress').value=s.elapsed;
  $('start').disabled=s.running||!s.library.length; $('stop').disabled=!s.running; $('skip').disabled=!s.current;
  $('onboarding').hidden=!!s.library.length; $('queue-count').textContent=String(s.queue.length);
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
  if (structure!==lastStructure) {renderStructure(s);lastStructure=structure;}
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
setInterval(()=>{if(unlocked&&!document.hidden)refresh();},2000);
if(token)establish();else requireUnlock();
