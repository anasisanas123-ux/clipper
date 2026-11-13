'use strict';
const $ = (s, el = document) => el.querySelector(s);

// Small DOM builder. Text always goes in as text nodes, so titles from transcripts can't inject markup.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k in el && k !== 'list') el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? 'POST' : 'GET'),
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

let toastTimer;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = bad ? 'bad' : '';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

const fmtTime = (s) => {
  s = Math.max(0, Math.round(s));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return (hh ? hh + ':' + String(mm).padStart(2, '0') : mm) + ':' + String(ss).padStart(2, '0');
};

const state = { jobId: null, job: null, cards: new Map(), tracks: [], settings: {} };

// ---------- streams list ----------
async function refreshJobs() {
  const jobs = await api('/api/jobs');
  const list = $('#job-list');
  list.replaceChildren(...jobs.map((j) => h('div', {
    class: 'job-item' + (j.id === state.jobId ? ' active' : ''), onclick: () => openJob(j.id),
  },
    h('div', { class: 'job-title' }, j.title || j.url),
    h('div', { class: 'job-sub' }, [j.streamer, j.status === 'analyzing' ? 'working…' : j.status === 'error' ? 'failed' : j.clips + ' moments'].filter(Boolean).join(' · ')),
  )));
  if (!jobs.length) list.append(h('p', { class: 'hint' }, 'Nothing yet.'));
}

function openJob(id) {
  state.jobId = id;
  state.job = null;
  state.cards.clear();
  $('#job').replaceChildren();
  $('#empty').hidden = true;
  $('#job').hidden = false;
  refreshJobs();
  poll();
}

$('#new-job').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('#url').value.trim();
  if (!url) return;
  try {
    const job = await api('/api/jobs', { body: { url, kind: $('#kind').value, topic: $('#topic').value.trim() } });
    $('#url').value = '';
    openJob(job.id);
  } catch (err) { toast(err.message, true); }
});

// ---------- job view ----------
let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  if (!state.jobId) return;
  let job;
  try { job = await api('/api/jobs/' + state.jobId); } catch { return; }
  if (job.id !== state.jobId) return;
  const statusChanged = !state.job || state.job.status !== job.status;
  state.job = job;
  renderJob(job);
  if (statusChanged) refreshJobs();
  const busy = job.status === 'analyzing' || job.clips.some((c) => c.status === 'queued' || c.status === 'rendering');
  pollTimer = setTimeout(poll, busy ? 1200 : 5000);
}

function renderJob(job) {
  const root = $('#job');
  if (!root.firstChild) {
    root.append(
      h('div', { class: 'job-head' },
        h('div', {}, h('h2', { id: 'j-title' }), h('div', { class: 'hint', id: 'j-sub' })),
        h('div', { class: 'actions' },
          h('button', { onclick: scheduleAll, title: 'Upload every rendered clip to YouTube, one per free daily slot, best score first' }, 'Schedule all to YouTube'),
          h('button', { class: 'danger', onclick: removeJob }, 'Delete'))),
      h('div', { id: 'j-status' }),
      h('div', { id: 'j-clips', class: 'clips' }));
  }
  $('#j-title').textContent = job.title || job.url;
  const kinds = { irl: 'IRL', gaming: 'Gaming', film: 'Film / TV reaction' };
  const bits = [job.streamer, [kinds[job.kind], job.topic].filter(Boolean).join(': '), job.duration ? fmtTime(job.duration) + ' long' : null];
  if (job.status === 'ready') {
    bits.push(job.ai ? `scored by Claude (${job.ai.model}, about $${job.ai.cost_usd.toFixed(2)})` : 'scored on this PC, without Claude');
    if (job.engine) bits.push('speech recognition on ' + (job.engine.device === 'cuda' ? 'GPU' : 'CPU'));
  }
  $('#j-sub').textContent = bits.filter(Boolean).join(' · ');

  const status = $('#j-status');
  const parts = [];
  if (job.status === 'analyzing') {
    parts.push(h('div', { class: 'progress-card' },
      h('div', {}, job.stage + (job.progress > 0 && job.progress < 1 ? ` (${Math.round(job.progress * 100)}%)` : '')),
      h('div', { class: 'bar' }, h('div', { style: `width:${Math.max(3, job.progress * 100)}%` })),
      h('div', { class: 'hint' }, 'A multi-hour stream takes a while: the audio is downloaded and transcribed before clips appear.')));
  }
  if (job.status === 'error') parts.push(h('div', { class: 'notice bad' }, 'This stream could not be processed. ', job.error || ''));
  for (const w of job.warnings || []) parts.push(h('div', { class: 'notice' }, w));
  status.replaceChildren(...parts);

  const wrap = $('#j-clips');
  for (const clip of job.clips) {
    let card = state.cards.get(clip.id);
    if (!card) {
      card = buildCard(job, clip);
      state.cards.set(clip.id, card);
      wrap.append(card.el);
    }
    card.update(clip);
  }
}

async function scheduleAll() {
  const ready = state.job.clips.filter((c) => c.status === 'ready' && !(c.youtube && c.youtube.status !== 'error')).length;
  if (!ready) return toast('No rendered clips waiting to be scheduled', true);
  if (!confirm(`Upload ${ready} clip${ready > 1 ? 's' : ''} to your YouTube channel and schedule them in the next free daily slots? They will go public at those times.`)) return;
  try {
    const r = await api(`/api/jobs/${state.jobId}/youtube-all`, { method: 'POST' });
    toast(`${r.queued} clip${r.queued === 1 ? '' : 's'} queued for YouTube`);
    poll();
  } catch (err) { toast(err.message, true); }
}

const parseTags = (text) => [...new Set(text.toLowerCase().split(/[\s,#]+/).map((t) => t.replace(/[^a-z0-9_]/g, '')).filter((t) => t && t !== 'shorts'))];
const localTime = (iso) => new Date(iso).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

async function removeJob() {
  if (!confirm('Delete this stream and its rendered clips? Exported files are kept.')) return;
  await api('/api/jobs/' + state.jobId, { method: 'DELETE' });
  state.jobId = null;
  $('#job').hidden = true;
  $('#empty').hidden = false;
  refreshJobs();
}

const band = (s) => s >= 80 ? ['Very high', 'vh'] : s >= 65 ? ['High', 'hi'] : s >= 50 ? ['Decent', 'mid'] : s >= 35 ? ['Low', 'lo'] : ['Very low', 'vl'];
const RISK = { low: 'Low copyright risk', medium: 'Medium copyright risk', high: 'High copyright risk' };

function songOptions(select, current) {
  const opts = [h('option', { value: '' }, 'No song')];
  for (const t of state.tracks) {
    opts.push(h('option', { value: t.id }, (t.source === 'mine' ? '★ ' : '') + t.title + (t.artist ? ' – ' + t.artist : '') + (t.mood ? ` (${t.mood})` : '')));
  }
  select.replaceChildren(...opts);
  select.value = state.tracks.some((t) => t.id === current) ? current : '';
}

function buildCard(job, clip) {
  const video = h('video', { controls: true, loop: true, playsInline: true, preload: 'metadata' });
  const overlay = h('div', { class: 'overlay' });
  const [bandText, bandClass] = band(clip.score);
  const title = h('input', { type: 'text', value: clip.title, maxLength: 100 });
  const hook = h('input', { type: 'text', value: clip.hook, maxLength: 60 });
  const tags = h('input', { type: 'text', value: (clip.hashtags || []).map((t) => '#' + t).join(' '), placeholder: '#gta6 #funny' });
  const when = h('input', { type: 'datetime-local' });
  const ytStatus = h('div', { class: 'yt-status hint' });
  const ytButtons = [
    h('button', { onclick: () => doUpload('next'), title: 'Uses the next free daily upload time from Settings' }, 'Schedule next slot'),
    h('button', { onclick: () => doUpload('at') }, 'Schedule at'), when,
    h('button', { onclick: () => doUpload('now') }, 'Upload now'),
  ];
  async function doUpload(mode) {
    if (mode === 'at' && !when.value) return toast('Pick a date and time first', true);
    try {
      await api(`/api/jobs/${job.id}/clips/${clip.id}/youtube`, { body: { mode, at: when.value, title: title.value, hashtags: parseTags(tags.value) } });
      poll();
    } catch (err) { toast(err.message, true); }
  }
  const layout = h('select', {},
    h('option', { value: 'auto' }, 'Auto'), h('option', { value: 'split' }, 'Facecam top, gameplay bottom'),
    h('option', { value: 'crop' }, 'Centre crop'), h('option', { value: 'blur' }, 'Blurred background'));
  const caps = h('select', {},
    h('option', { value: 'pop' }, 'Word-by-word pop'), h('option', { value: 'karaoke' }, 'Karaoke line'),
    h('option', { value: 'clean' }, 'Clean'), h('option', { value: 'none' }, 'None'));
  const colour = h('select', {}, ...['yellow', 'green', 'cyan', 'pink', 'orange', 'white'].map((c) => h('option', {}, c)));
  const song = h('select', {});
  const volume = h('input', { type: 'range', min: 0.03, max: 0.6, step: 0.01, value: clip.music_volume });
  const bar = h('input', { type: 'checkbox', checked: !!clip.progress_bar });
  layout.value = clip.layout; caps.value = clip.caption_style; colour.value = clip.highlight;
  songOptions(song, clip.song);

  const draft = { start: clip.start, end: clip.end };
  const range = h('span', { class: 'range' });
  const showRange = () => { range.textContent = `${fmtTime(draft.start)} → ${fmtTime(draft.end)} (${Math.round(draft.end - draft.start)}s)`; };
  const nudge = (key, d) => () => {
    draft[key] = Math.max(0, draft[key] + d);
    if (draft.end - draft.start < 5) draft[key] -= d;
    showRange();
  };
  showRange();

  const renderBtn = h('button', { class: 'primary', onclick: doRender });
  const exportBtn = h('button', { onclick: doExport }, 'Export');
  const copyBox = h('div', { class: 'copy' });
  const layoutNote = h('span', { class: 'hint' });

  async function doRender() {
    try {
      await api(`/api/jobs/${job.id}/clips/${clip.id}/render`, { body: {
        title: title.value, hook: hook.value, hashtags: parseTags(tags.value), layout: layout.value, caption_style: caps.value,
        highlight: colour.value, song: song.value, music_volume: +volume.value, progress_bar: bar.checked,
        start: draft.start, end: draft.end,
      } });
      poll();
    } catch (err) { toast(err.message, true); }
  }
  async function doExport() {
    try {
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/export`, { method: 'POST' });
      toast('Saved to ' + r.path);
    } catch (err) { toast(err.message, true); }
  }

  const field = (label, control, extra) => h('label', { class: 'field' }, h('span', {}, label), control, extra);
  const el = h('article', { class: 'clip' },
    h('div', { class: 'player' }, video, overlay),
    h('div', { class: 'info' },
      h('div', { class: 'score-row' },
        h('div', { class: 'score ' + bandClass }, h('b', {}, String(clip.score)), h('small', {}, '/100')),
        h('div', {},
          h('div', { class: 'score-label' }, 'Viral chance: ' + bandText),
          h('div', { class: 'hint' }, clip.source === 'claude' ? 'Claude\'s estimate from the transcript and audio' : 'Estimate from loudness and speech cues'))),
      h('div', { class: 'bars' }, Object.entries(clip.breakdown || {}).map(([k, v]) =>
        h('div', { class: 'mini' }, h('span', {}, k), h('div', { class: 'bar' }, h('div', { style: `width:${v}%` })), h('em', {}, String(v))))),
      h('ul', { class: 'reasons' }, (clip.reasons || []).map((r) => h('li', {}, r))),
      copyBox,
      h('div', { class: 'controls' },
        field('Title', title), field('Hashtags (#shorts is added automatically)', tags),
        field('Hook text (first 3 seconds)', hook),
        h('div', { class: 'grid2' }, field('Framing', layout, layoutNote), field('Captions', caps)),
        h('div', { class: 'grid2' }, field('Highlight', colour), field('Song', song)),
        h('div', { class: 'grid2' }, field('Song volume', volume),
          h('label', { class: 'row' }, bar, ' Progress bar')),
        h('div', { class: 'field' }, h('span', {}, 'Cut'),
          h('div', { class: 'cut' },
            h('button', { onclick: nudge('start', -1), title: 'Start 1s earlier' }, '◀ start'),
            h('button', { onclick: nudge('start', 1), title: 'Start 1s later' }, 'start ▶'),
            range,
            h('button', { onclick: nudge('end', -1), title: 'End 1s earlier' }, '◀ end'),
            h('button', { onclick: nudge('end', 1), title: 'End 1s later' }, 'end ▶')))),
      h('div', { class: 'actions' }, renderBtn, exportBtn),
      h('div', { class: 'yt' }, h('span', { class: 'hint' }, 'YouTube'), h('div', { class: 'yt-row' }, ytButtons), ytStatus)));

  let shown = -1;
  function update(c) {
    const busy = c.status === 'queued' || c.status === 'rendering';
    if (c.status === 'ready' && c.version !== shown) {
      shown = c.version;
      video.src = `/media/${job.id}/${c.id}.mp4?v=${c.version}`;
    }
    video.hidden = shown < 0;
    if (busy) overlay.replaceChildren(h('div', { class: 'spinner' }), h('div', {}, c.status === 'queued' ? 'Waiting in line' : (c.step || 'Working')));
    else if (c.status === 'error') overlay.replaceChildren(h('div', { class: 'bad' }, 'Render failed'), h('div', { class: 'hint' }, c.error || ''));
    else if (c.status === 'idle') overlay.replaceChildren(h('div', {}, 'Not rendered yet'), h('div', { class: 'hint' }, `${fmtTime(c.start)} in the stream`));
    else overlay.replaceChildren();
    overlay.hidden = !overlay.firstChild;
    overlay.classList.toggle('over-video', busy && shown >= 0);
    renderBtn.disabled = busy;
    renderBtn.textContent = busy ? 'Working…' : shown < 0 ? 'Render this clip' : 'Re-render with changes';
    exportBtn.disabled = busy || shown < 0;
    layoutNote.textContent = c.used_layout && layout.value === 'auto' ? 'auto picked: ' + ({ split: 'facecam + gameplay', crop: 'centre crop', blur: 'blurred background' })[c.used_layout] : '';

    const yt = c.youtube;
    const ytBusy = yt && ['queued', 'uploading', 'scheduled', 'uploaded'].includes(yt.status);
    for (const b of ytButtons) b.disabled = busy || shown < 0 || ytBusy;
    ytStatus.className = 'yt-status hint' + (yt && yt.status === 'error' ? ' bad' : yt && yt.video_id ? ' ok' : '');
    if (!yt) ytStatus.replaceChildren();
    else if (yt.status === 'queued' || yt.status === 'uploading') ytStatus.replaceChildren(yt.status === 'queued' ? 'Waiting to upload…' : 'Uploading to YouTube…');
    else if (yt.status === 'error') ytStatus.replaceChildren('Upload failed: ' + (yt.error || ''));
    else ytStatus.replaceChildren(
      yt.status === 'scheduled' ? `Uploaded. Goes public ${localTime(yt.publish_at)}. ` : 'Uploaded. ',
      h('a', { href: 'https://youtube.com/shorts/' + yt.video_id, target: '_blank' }, 'Open on YouTube'));

    const cr = c.copyright;
    if (cr) {
      copyBox.replaceChildren(h('details', { class: 'risk ' + cr.overall },
        h('summary', {}, RISK[cr.overall]),
        h('ul', {}, cr.items.map((i) => h('li', { class: i.level }, h('b', {}, i.label + '. '), i.detail))),
        h('p', { class: 'hint' }, cr.note)));
    } else {
      copyBox.replaceChildren();
    }
  }
  return { el, update, refreshSongs: () => songOptions(song, song.value) };
}

// ---------- music ----------
const MOODS = ['hype', 'funny', 'tense', 'chill', 'emotional', 'rage'];
let player = null;
function preview(url, btn) {
  if (player) { player.pause(); const was = player._btn; player._btn.textContent = '▶'; player = null; if (was === btn) return; }
  player = new Audio(url); player._btn = btn; btn.textContent = '■';
  player.play().catch(() => toast('Could not play that track', true));
  player.onended = () => { btn.textContent = '▶'; player = null; };
}

async function loadTracks() {
  const r = await api('/api/music');
  state.tracks = r.tracks;
  for (const card of state.cards.values()) card.refreshSongs();
  $('#lib-list').replaceChildren(...(r.tracks.length ? r.tracks.map((t) => {
    const b = h('button', { class: 'play' }, '▶');
    b.onclick = () => preview('/api/music/file/' + t.id.split('/').map(encodeURIComponent).join('/'), b);
    return h('div', { class: 'track' }, b,
      h('div', { class: 'grow' }, h('div', {}, t.title + (t.artist ? ' – ' + t.artist : '')),
        h('div', { class: 'hint' }, t.source === 'mine' ? 'Your file (licence not checked)' : `${t.license}${t.mood ? ' · ' + t.mood : ''}`)));
  }) : [h('p', { class: 'hint' }, 'No tracks yet. Download some under "Find free music" or add your own files.')]));
}

async function findMusic(mood, q) {
  const list = $('#find-list');
  list.replaceChildren(h('p', { class: 'hint' }, 'Searching…'));
  try {
    const r = await api(`/api/music/search?mood=${encodeURIComponent(mood)}&q=${encodeURIComponent(q || '')}`);
    const rows = r.results.map((t) => {
      const play = h('button', { class: 'play' }, '▶');
      play.onclick = () => preview(t.url, play);
      const get = h('button', {}, 'Download');
      get.onclick = async () => {
        get.disabled = true; get.textContent = 'Downloading…';
        try { await api('/api/music/download', { body: { track: t, mood } }); get.textContent = 'In library'; loadTracks(); }
        catch (err) { get.disabled = false; get.textContent = 'Download'; toast(err.message, true); }
      };
      return h('div', { class: 'track' }, play,
        h('div', { class: 'grow' }, h('div', {}, t.title + (t.artist ? ' – ' + t.artist : '')),
          h('div', { class: 'hint' }, `${fmtTime(t.duration)} · `, t.license_url ? h('a', { href: t.license_url, target: '_blank' }, t.license) : t.license,
            t.page ? [' · ', h('a', { href: t.page, target: '_blank' }, 'source')] : null)),
        get);
    });
    list.replaceChildren(...(rows.length ? rows : [h('p', { class: 'hint' }, 'No usable tracks found for "' + r.query + '".')]),
      ...r.errors.map((e) => h('p', { class: 'notice bad' }, e)));
  } catch (err) { list.replaceChildren(h('p', { class: 'notice bad' }, err.message)); }
}

async function loadTrending() {
  const list = $('#trend-list');
  list.replaceChildren(h('p', { class: 'hint' }, 'Loading…'));
  const r = await api('/api/music/trending');
  list.replaceChildren(...(r.items.length
    ? r.items.map((t, i) => h('div', { class: 'track' }, h('span', { class: 'rank' }, String(i + 1)), h('div', { class: 'grow' }, t.title, h('div', { class: 'hint' }, t.artist))))
    : [h('p', { class: 'notice bad' }, 'Could not load the chart. ' + (r.error || ''))]));
}

let mood = 'hype';
$('#moods').replaceChildren(...MOODS.map((m) => h('button', { class: m === mood ? 'active' : '', onclick: (e) => {
  mood = m;
  for (const b of $('#moods').children) b.classList.toggle('active', b === e.target);
  $('#music-q').value = '';
  findMusic(m, '');
} }, m)));
$('#music-search').addEventListener('submit', (e) => { e.preventDefault(); findMusic(mood, $('#music-q').value); });
for (const tab of document.querySelectorAll('.tabs button')) {
  tab.onclick = () => {
    for (const t of document.querySelectorAll('.tabs button')) t.classList.toggle('active', t === tab);
    for (const p of document.querySelectorAll('[data-pane]')) p.hidden = p.dataset.pane !== tab.dataset.tab;
    if (tab.dataset.tab === 'find' && !$('#find-list').firstChild) findMusic(mood, '');
    if (tab.dataset.tab === 'trend' && !$('#trend-list').firstChild) loadTrending();
  };
}
$('#open-music').onclick = () => { loadTracks(); $('#music').showModal(); };
$('#music-close').onclick = () => { if (player) { player.pause(); player = null; } $('#music').close(); };
$('#open-music-folder').onclick = () => api('/api/open-folder', { body: { which: 'music' } });

// ---------- settings ----------
const FIELDS = ['use_claude', 'anthropic_api_key', 'claude_model', 'clip_count', 'min_len', 'max_len', 'caption_style',
  'highlight', 'max_height', 'progress_bar', 'whisper_model', 'language', 'audd_api_key', 'jamendo_client_id',
  'schedule_times', 'youtube_privacy'];

let ytPoll;
function showYouTube(y) {
  $('#yt-state').textContent = y.connecting ? 'Waiting for you to finish signing in in your browser…'
    : y.connected ? `Connected to: ${y.channel || 'your channel'}`
      : y.has_client ? 'Client file saved. Press Connect and sign in with the Google account that owns your channel.'
        : 'Not set up yet. Open "One-time setup" below.';
  if (y.error) $('#yt-state').textContent += ' ' + y.error;
  $('#yt-connect').disabled = !y.has_client || y.connecting;
  $('#yt-connect').textContent = y.connected ? 'Reconnect' : 'Connect YouTube channel';
  $('#yt-disconnect').hidden = !y.connected;
  $('#yt-setup').open = !y.has_client;
  clearTimeout(ytPoll);
  if (y.connecting) ytPoll = setTimeout(async () => showYouTube(await api('/api/youtube')), 1500);
}
$('#yt-save-client').onclick = async () => {
  try { showYouTube(await api('/api/youtube/client', { body: { json: $('#yt-client').value } })); $('#yt-client').value = ''; toast('Client file saved'); }
  catch (err) { toast(err.message, true); }
};
$('#yt-connect').onclick = async () => {
  try { showYouTube(await api('/api/youtube/connect', { method: 'POST' })); } catch (err) { toast(err.message, true); }
};
$('#yt-disconnect').onclick = async () => showYouTube(await api('/api/youtube/disconnect', { method: 'POST' }));

function fillSettings(s) {
  state.settings = s;
  for (const f of FIELDS) {
    const el = $('#s-' + f);
    if (el.type === 'checkbox') el.checked = !!s[f];
    else if (el.type === 'password') el.value = '';
    else el.value = s[f] ?? '';
  }
  $('#s-anthropic_api_key').placeholder = s.has_api_key ? `saved (${s.key_hint}) – type to replace` : 'sk-ant-…';
  $('#s-audd_api_key').placeholder = s.has_audd_key ? 'saved – type to replace' : 'optional';
  $('#s-claude-state').textContent = s.claude_active
    ? 'On. Each stream sends its transcript to Claude and costs roughly $0.05–0.50 depending on length and model.'
    : s.has_api_key ? 'Off. Moments are picked and scored on this PC for free.'
      : 'No API key saved, so moments are picked and scored on this PC for free. Add a key to turn Claude on.';
}

$('#open-settings').onclick = async () => {
  fillSettings(await api('/api/settings'));
  showYouTube(await api('/api/youtube'));
  $('#settings').showModal();
};
$('#settings-cancel').onclick = () => $('#settings').close();
$('#settings-save').onclick = async () => {
  const patch = {};
  for (const f of FIELDS) {
    const el = $('#s-' + f);
    patch[f] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? +el.value : el.value.trim();
  }
  patch.max_height = +patch.max_height;
  if (patch.min_len >= patch.max_len) return toast('Shortest must be less than longest', true);
  try { fillSettings(await api('/api/settings', { body: patch })); $('#settings').close(); toast('Settings saved'); }
  catch (err) { toast(err.message, true); }
};

// ---------- boot ----------
(async () => {
  try { state.tracks = (await api('/api/music')).tracks; } catch {}
  await refreshJobs();
})();
