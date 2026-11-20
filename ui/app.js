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
    h('div', { class: 'job-sub' }, j.status === 'live' ? h('span', { class: 'live-dot' }, '● LIVE') : '', [j.streamer, j.status === 'analyzing' ? 'working…' : j.status === 'waiting_chat' ? 'waiting for your chatbot' : j.status === 'error' ? 'failed' : j.clips + ' moments'].filter(Boolean).join(' · ')),
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

const openLogs = () => api('/api/open-folder', { body: { which: 'logs' } });

// Local video files: the "Add file" button and drag-and-drop both put the file's path in the link box.
const desktop = window.clipper;
function useFile(path) {
  if (!path) return;
  $('#url').value = path;
  $('#url').focus();
  toast('File added. Pick the video type, then press Find clips.');
}
$('#pick-file').onclick = async () => {
  if (!desktop) return toast('Paste the full path of the video file into the box instead.', true);
  useFile(await desktop.pickVideo());
};
document.addEventListener('dragover', (e) => { e.preventDefault(); document.body.classList.add('dropping'); });
document.addEventListener('dragleave', (e) => { if (!e.relatedTarget) document.body.classList.remove('dropping'); });
document.addEventListener('drop', (e) => {
  e.preventDefault();
  document.body.classList.remove('dropping');
  const file = e.dataTransfer.files[0];
  if (!file) return;
  if (!desktop) return toast('Paste the full path of the video file into the box instead.', true);
  useFile(desktop.pathForFile(file));
});

$('#new-job').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('#url').value.trim();
  if (!url) return;
  // Nothing starts until every choice for this video is made in the setup window.
  const setup = await openSetup(url);
  if (!setup) return;
  try {
    const job = await api('/api/jobs', { body: { url, setup } });
    $('#url').value = '';
    openJob(job.id);
  } catch (err) { toast(err.message, true); }
});

// ---------- marking the facecam by hand ----------
// The area is kept as shares of the frame (0-1), so it fits the video at any resolution. The backend widens or
// heightens it to the facecam panel's shape (1080 x 672) without stretching; the dashed outline shows that.
const PANEL = 1080 / 672;

function fitPanel(b, W, H) {
  let cx = (b.x + b.w / 2) * W, cy = (b.y + b.h / 2) * H, cw = b.w * W, ch = b.h * H;
  if (cw / ch < PANEL) cw = ch * PANEL; else ch = cw / PANEL;
  const s = Math.min(1, W / cw, H / ch); cw *= s; ch *= s;
  const x = Math.max(0, Math.min(W - cw, cx - cw / 2)), y = Math.max(0, Math.min(H - ch, cy - ch / 2));
  return { x, y, w: cw, h: ch };
}

// A frame of the video that you drag a box on. `onChange(box)` runs when a drag ends.
function makeMarker(onChange) {
  const img = h('img', { alt: 'A frame from the video', draggable: false });
  const fit = h('div', { class: 'mk-fit', hidden: true });
  const box = h('div', { class: 'mk-box', hidden: true });
  const wait = h('div', { class: 'mk-wait' }, 'Getting a picture from the video… (from a link this can take up to half a minute)');
  const wrap = h('div', { class: 'mk-wrap' }, img, fit, box, wait);
  const canvas = h('canvas', { class: 'mk-canvas', width: 324, height: 202 });
  const m = { el: wrap, preview: canvas, box: null, natural: null, url: '' };
  const usable = () => !!(m.box && m.box.w > 0.01 && m.box.h > 0.01);
  function draw() {
    const show = (el, r) => {
      el.hidden = !r;
      if (r) Object.assign(el.style, { left: r.x * img.clientWidth + 'px', top: r.y * img.clientHeight + 'px', width: r.w * img.clientWidth + 'px', height: r.h * img.clientHeight + 'px' });
    };
    const good = !!m.natural && usable();
    show(box, good ? m.box : null);
    const f = good ? fitPanel(m.box, m.natural[0], m.natural[1]) : null;
    show(fit, f ? { x: f.x / m.natural[0], y: f.y / m.natural[1], w: f.w / m.natural[0], h: f.h / m.natural[1] } : null);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#111'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (f) ctx.drawImage(img, f.x, f.y, f.w, f.h, 0, 0, canvas.width, canvas.height);
  }
  m.load = (url) => {
    m.url = url; m.natural = null; wait.hidden = false; wait.textContent = 'Getting a picture from the video… (from a link this can take up to half a minute)'; draw();
    img.onload = () => { wait.hidden = true; m.natural = [img.naturalWidth, img.naturalHeight]; draw(); };
    img.onerror = async () => {
      let msg = 'Couldn\'t get a picture from this video.';
      try { const r = await fetch(url); msg = (await r.json()).detail || msg; } catch {}
      wait.hidden = false; wait.textContent = msg;
    };
    img.src = url + (url.includes('?') ? '&' : '?') + 'n=' + Date.now();
  };
  m.set = (b) => { m.box = b ? { ...b } : null; draw(); };
  m.value = () => (usable() ? Object.fromEntries(Object.entries(m.box).map(([k, v]) => [k, Math.round(v * 10000) / 10000])) : null);
  let start = null;
  const at = (e) => {
    const r = img.getBoundingClientRect();
    return [Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))];
  };
  wrap.addEventListener('pointerdown', (e) => {
    if (!m.natural) return;
    start = at(e); wrap.setPointerCapture(e.pointerId); e.preventDefault();
  });
  wrap.addEventListener('pointermove', (e) => {
    if (!start) return;
    const [x, y] = at(e);
    m.box = { x: Math.min(x, start[0]), y: Math.min(y, start[1]), w: Math.abs(x - start[0]), h: Math.abs(y - start[1]) };
    draw();
  });
  wrap.addEventListener('pointerup', () => { if (start) { start = null; if (onChange) onChange(m.value()); } });
  new ResizeObserver(draw).observe(wrap);
  return m;
}

// The marker in its own window, for clips that have already been made.
const clipMarker = makeMarker(() => { $('#mk-save').disabled = !clipMarker.value(); });
$('#mk-host').append(clipMarker.el);
$('#mk-prev-host').append(clipMarker.preview);
let mkResolve = null;

function markFacecam(imgUrl, initial, showAll) {
  return new Promise((resolve) => {
    mkResolve = resolve;
    $('#mk-all-row').hidden = !showAll;
    clipMarker.set(initial || null);
    clipMarker.load(imgUrl);
    $('#mk-save').disabled = !clipMarker.value();
    $('#cam-marker').showModal();
  });
}
const mkFinish = (value) => { const r = mkResolve; mkResolve = null; $('#cam-marker').close(); if (r) r(value); };
$('#mk-save').onclick = () => mkFinish({ box: clipMarker.value(), all: $('#mk-all').checked && !$('#mk-all-row').hidden });
$('#mk-cancel').onclick = () => mkFinish(null);
$('#cam-marker').addEventListener('cancel', (e) => { e.preventDefault(); mkFinish(null); });

// ---------- setting up a new video ----------
// Asked before every new video: everything about how its clips are made, filled in with the last video's choices.
const CHOICES = {
  layout: [['split', 'Facecam top, gameplay bottom'], ['bars', 'Black bars'], ['blur', 'Blurred background (same video)'],
    ['crop', 'Centre crop (full screen)'], ['story', 'Story card (text above, video in a box)'], ['auto', 'Auto (picks per clip)']],
  cam_pos: [['auto', 'Find it automatically'], ['tl', 'Top left'], ['tr', 'Top right'], ['ml', 'Middle left'], ['mr', 'Middle right'],
    ['bl', 'Bottom left'], ['br', 'Bottom right'], ['custom', 'Drawn by hand']],
  split_game: [['fill', 'Fill the panel (crops the sides)'], ['fit', 'Show the whole game (blurred edges)']],
  caption_style: [['pop', 'Word-by-word pop'], ['edit', 'Edit captions (coloured keywords)'], ['meme', 'Meme caption (one line, whole clip)'],
    ['karaoke', 'Karaoke line'], ['clean', 'Clean'], ['none', 'No captions']],
  caption_size: [['small', 'Small'], ['medium', 'Medium'], ['large', 'Large'], ['huge', 'Huge']],
  caption_position: [['auto', 'Best spot for the framing'], ['top', 'Top'], ['middle', 'Middle'], ['bottom', 'Bottom']],
  highlight: ['yellow', 'green', 'cyan', 'pink', 'orange', 'white'].map((c) => [c, c]),
  sfx_source: [['auto', 'My sounds first (built-in when I have none)'], ['mine', 'Only my sounds'], ['builtin', 'Only built-in sounds']],
  kind: [['irl', 'IRL / chatting'], ['gaming', 'Gaming'], ['podcast', 'Podcast / interview'], ['film', 'Film / TV reaction']],
};
const SOUND_ROLE_LABELS = { intro: 'Start of the clip', hit: 'Loud hits', big: 'Biggest hit', transition: 'Transitions (whip blur)', ending: 'Ending', emoji: 'Emoji pop-ups' };
const FRAME_SPOTS = [0.35, 0.5, 0.2, 0.65, 0.8, 0.1, 0.9];

async function loadSetupInfo() {
  try { state.setup = await api('/api/setup'); } catch { state.setup = { defaults: {}, auto_songs: {}, ai_ready: false, ai_status: '' }; }
}

function songSelectOptions(select, current, withAuto) {
  const opts = [h('option', { value: '' }, 'No song')];
  if (withAuto) for (const [k, v] of Object.entries((state.setup && state.setup.auto_songs) || {})) opts.push(h('option', { value: k }, v));
  for (const t of state.tracks) opts.push(h('option', { value: t.id }, (t.source === 'mine' ? '★ ' : '') + t.title + (t.artist ? ' – ' + t.artist : '') + (t.mood ? ` (${t.mood})` : '')));
  select.replaceChildren(...opts);
  select.value = [...select.options].some((o) => o.value === current) ? current : '';
}

let setupResolve = null;
let setupCollect = null;

async function openSetup(url) {
  await loadSetupInfo();
  if (!state.fx || !Object.keys(state.fx.presets || {}).length) await loadEffects();
  try { state.tracks = (await api('/api/music')).tracks; } catch {}
  return new Promise((resolve) => {
    setupResolve = resolve;
    buildSetup({ url });
    $('#setup').showModal();
    $('#setup-form').scrollTop = 0;
  });
}

// The same window for a video that's already added: changes the look of all its clips.
async function openJobSetup(job) {
  await loadSetupInfo();
  if (!state.fx || !Object.keys(state.fx.presets || {}).length) await loadEffects();
  try { state.tracks = (await api('/api/music')).tracks; } catch {}
  let values;
  try { values = (await api(`/api/jobs/${job.id}/setup`)).values; } catch (err) { toast(err.message, true); return null; }
  return new Promise((resolve) => {
    setupResolve = resolve;
    buildSetup({ job, values });
    $('#setup').showModal();
    $('#setup-form').scrollTop = 0;
  });
}

async function changeJobSetup(job) {
  const setup = await openJobSetup(job);
  if (!setup) return;
  try {
    const r = await api(`/api/jobs/${job.id}/setup`, { body: { setup } });
    // rebuilt so every clip card shows the new settings
    state.cards.clear(); $('#j-clips').replaceChildren();
    toast('Settings changed for every clip in this video' + (r.queued ? `; ${r.queued} clip${r.queued === 1 ? '' : 's'} rendering again` : '') + '.');
    poll();
  } catch (err) { toast(err.message, true); }
}

const setupFinish = (value) => { const r = setupResolve; setupResolve = null; $('#setup').close(); if (r) r(value); };
$('#setup-cancel').onclick = () => setupFinish(null);
$('#setup').addEventListener('cancel', (e) => { e.preventDefault(); setupFinish(null); });
$('#setup-start').onclick = () => { const v = setupCollect && setupCollect(); if (v) setupFinish(v); };

function buildSetup({ url, job, values }) {
  const v = { ...(values || (state.setup && state.setup.defaults) || {}) };
  if (job) {
    $('#setup-title').textContent = 'Change this video\'s settings';
    $('#setup-source').textContent = `How every clip of "${(job.title || job.url).slice(0, 70)}" is made. Applying gives all its clips these settings, places their effects again, and renders the ones already rendered again. Titles, text and cuts are kept.`;
    $('#setup-start').textContent = 'Apply to every clip';
  } else {
    const name = url.split(/[\\/]/).pop() || url;
    $('#setup-title').textContent = 'Set up this video';
    $('#setup-source').textContent = `How the clips from "${name.length > 70 ? name.slice(0, 67) + '…' : name}" are made. Nothing starts until you press Start. Filled in with what you chose last time.`;
    $('#setup-start').textContent = 'Start';
  }
  const sel = (key, choices, value) => {
    const s = h('select', { name: key }, ...choices.map(([k, label]) => h('option', { value: k }, label)));
    s.value = value ?? choices[0][0];
    if (s.selectedIndex < 0) s.value = choices[0][0];
    return s;
  };
  const num = (key, attrs) => h('input', { type: 'number', name: key, value: v[key] ?? '', ...attrs });
  const box = (key, label, tip) => h('label', { class: 'row small', title: tip || '' },
    h('input', { type: 'checkbox', name: key, checked: v[key] ?? true }), ' ' + label);
  const fld = (label, control, extra) => h('label', { class: 'field' }, h('span', {}, label), control, extra || null);
  const section = (title, ...kids) => h('fieldset', {}, h('legend', {}, title), ...kids);
  const cat = state.fx || { presets: {}, filters: {} };

  // how the moments are picked
  const ai = state.setup || {};
  let scorer = v.scorer || 'builtin';
  if (scorer === 'ai' && !ai.ai_ready) scorer = 'builtin';
  const pick = (val, title, desc, disabled) => h('label', { class: 'pick' + (disabled ? ' off' : '') },
    h('input', { type: 'radio', name: 'scorer', value: val, checked: scorer === val, disabled }), h('span', {}, h('b', {}, title), h('span', { class: 'hint' }, desc)));
  const scorers = h('div', { class: 'pick-list' },
    pick('builtin', 'Clipper\'s own engine', 'Free and runs on this PC: the clips are made straight after transcribing.'),
    pick('chat', 'Your chatbot (ChatGPT, Claude, Gemini…)', 'Free. After transcribing, Clipper gives you a prompt to paste into a chatbot and waits for its answer.'),
    pick('ai', 'Connected AI model', ai.ai_ready ? `Uses ${ai.ai_status}.` : `Not set up (${ai.ai_status || 'no key'}). Set it up in Settings first.`, !ai.ai_ready));

  // framing, with the facecam drawn on a picture from this video
  const layout = sel('layout', CHOICES.layout, v.layout);
  const camPos = sel('cam_pos', CHOICES.cam_pos, v.cam_pos);
  const splitGame = sel('split_game', CHOICES.split_game, v.split_game);
  const zoom = num('zoom', { min: 30, max: 100 });
  let camBox = v.cam_pos === 'custom' && v.cam_box ? { ...v.cam_box } : null;
  const marker = makeMarker((b) => { if (b) { camBox = b; camPos.value = 'custom'; } });
  if (camBox) marker.set(camBox);
  let spot = 0, frameShown = false;
  const clipIds = job ? job.clips.map((c) => c.id) : [];
  const frameUrl = () => (job
    ? `/api/jobs/${job.id}/frame` + (clipIds.length ? `?clip=${clipIds[spot % clipIds.length]}` : '')
    : `/api/source-frame?url=${encodeURIComponent(url)}&pos=${FRAME_SPOTS[spot % FRAME_SPOTS.length]}`);
  const showFrame = () => { marker.load(frameUrl()); frameShown = true; };
  const another = h('button', { type: 'button', title: 'If the facecam isn\'t visible in this picture' }, 'Show another moment');
  another.onclick = () => { spot += 1; showFrame(); };
  const clear = h('button', { type: 'button' }, 'Clear the box');
  clear.onclick = () => { camBox = null; marker.set(null); if (camPos.value === 'custom') camPos.value = 'auto'; };
  camPos.addEventListener('change', () => {
    if (camPos.value === 'custom' && !camBox) toast('Drag a box around the facecam on the picture below.');
  });
  const camSection = h('div', { class: 'setup-cam' },
    h('p', { class: 'hint' }, 'Drag a box around the facecam on this picture from your video, or choose where it is above. The dashed outline is what the top panel will show.'),
    marker.el,
    h('div', { class: 'setup-cam-row' },
      h('div', { class: 'mk-preview' }, h('span', { class: 'hint' }, 'Top panel preview'), marker.preview),
      h('div', { class: 'actions left' }, another, clear)));
  const splitBox = h('div', { class: 'grid2' }, fld('Where the facecam is', camPos), fld('Gameplay (bottom half)', splitGame));
  const zoomBox = fld('Picture height % (black bars / blurred)', zoom);
  const showFraming = () => {
    const facecam = ['split', 'auto'].includes(layout.value);
    splitBox.hidden = camSection.hidden = !facecam;
    zoomBox.hidden = !['bars', 'blur'].includes(layout.value);
    if (facecam && !frameShown) showFrame();
  };
  layout.addEventListener('change', showFraming);

  // effects and sounds
  const fixed = { ...(v.sfx_fixed || {}) };
  const roleFields = Object.entries(SOUND_ROLE_LABELS).map(([role, label]) => {
    const s = h('select', {});
    soundOptions(s, fixed[role] || '');
    s.options[0].textContent = 'Auto (fits the moment)';
    s.addEventListener('change', () => { if (s.value) fixed[role] = s.value; else delete fixed[role]; });
    return fld(label, s);
  });
  const song = h('select', { name: 'song' });
  songSelectOptions(song, v.song || '', true);

  const form = $('#setup-form');
  form.replaceChildren(
    job ? null : section('The video',
      h('div', { class: 'grid2' }, fld('Type of video', sel('kind', CHOICES.kind, v.kind)),
        fld('Topic (optional)', h('input', { type: 'text', name: 'topic', placeholder: 'e.g. GTA 6', maxLength: 80, autocomplete: 'off' }))),
      h('div', { class: 'field' }, h('span', {}, 'How to pick the clips'), scorers)),
    section('Framing', fld('Framing', layout), splitBox, camSection, zoomBox),
    section('Captions',
      h('div', { class: 'grid3' }, fld('Style', sel('caption_style', CHOICES.caption_style, v.caption_style)),
        fld('Size', sel('caption_size', CHOICES.caption_size, v.caption_size)),
        fld('Position', sel('caption_position', CHOICES.caption_position, v.caption_position))),
      h('div', { class: 'grid3' }, fld('Highlight colour', sel('highlight', CHOICES.highlight, v.highlight))),
      h('div', { class: 'opts' }, box('show_hook', 'Hook text for the first seconds'), box('progress_bar', 'Progress bar'))),
    section('Effects and sounds',
      h('div', { class: 'grid3' },
        fld('Effects style', sel('effects_preset', Object.entries(cat.presets), v.effects_preset)),
        fld('Colour filter', sel('filter', [['', 'Matches the effects style'], ...Object.entries(cat.filters)], v.filter || '')),
        fld('Sounds to use', sel('sfx_source', CHOICES.sfx_source, v.sfx_source))),
      h('div', { class: 'grid3' }, fld('Sound effects volume (0 – 1.5)', num('sfx_volume', { min: 0, max: 1.5, step: 0.05 }))),
      h('details', {}, h('summary', { class: 'hint' }, 'Always use a particular sound for…'), h('div', { class: 'grid3' }, ...roleFields))),
    section('Music',
      h('div', { class: 'grid3' }, fld('Song', song), fld('Song volume (0.03 – 0.6)', num('music_volume', { min: 0.03, max: 0.6, step: 0.01 })))),
    section('Timeline',
      h('div', { class: 'opts' }, box('tighten', 'Cut pauses (jump cuts)'), box('cold_open', 'Cold open (teaser first)'),
        box('track_face', 'Follow the face'), box('beat_sync', 'Effects on the song\'s beat'))),
    job ? null : section('Clips',
      h('div', { class: 'grid3' }, fld('How many clips', num('clip_count', { min: 1, max: 30 })),
        fld('Shortest (sec)', num('min_len', { min: 5, max: 120 })), fld('Longest (sec)', num('max_len', { min: 10, max: 180 })))));
  for (const node of [...form.childNodes]) if (node.nodeType === Node.TEXT_NODE) node.remove();   // the sections left out
  showFraming();

  setupCollect = () => {
    const out = { sfx_fixed: { ...fixed } };
    for (const el of form.querySelectorAll('[name]')) {
      if (el.type === 'radio') { if (el.checked) out[el.name] = el.value; continue; }
      out[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? (el.value === '' ? null : +el.value) : el.value;
    }
    if (out.filter === '') out.filter = null;
    if (out.cam_pos === 'custom') {
      if (!camBox) { toast('Draw a box around the facecam on the picture, or choose where it is.', true); return null; }
      out.cam_box = camBox;
    }
    if (out.min_len != null && out.max_len != null && out.min_len >= out.max_len) { toast('Shortest must be less than longest', true); return null; }
    return out;
  };
}

// ---------- picking the clips with your own chatbot ----------
// Built once per video and kept while the page refreshes, so a pasted answer isn't wiped.
const chatPanels = new Map();
const CHAT_SITES = [['ChatGPT', 'https://chatgpt.com/'], ['Claude', 'https://claude.ai/new'], ['Gemini', 'https://gemini.google.com/app']];

function chatPanel(job) {
  if (chatPanels.has(job.id)) return chatPanels.get(job.id);
  const info = h('span', { class: 'hint' }, 'Loading…');
  const modeQuick = h('input', { type: 'radio', name: 'chat-mode-' + job.id, checked: true });
  const modeFull = h('input', { type: 'radio', name: 'chat-mode-' + job.id });
  const fullLabel = h('span', {}, 'The whole transcript');
  const copyRow = h('div', { class: 'chat-copy' });
  const manual = h('textarea', { rows: 4, readOnly: true, hidden: true, class: 'chat-manual' });
  const answer = h('textarea', { rows: 6, placeholder: 'Paste the chatbot\'s whole answer here: its list of clips (titles with start and end times, a table, or the JSON). If it asked questions instead, tell it "Don\'t ask questions, list the clips now" and paste its new answer.', spellcheck: false });
  const addBtn = h('button', { class: 'primary' }, 'Add these moments');
  const tally = h('span', { class: 'hint' });
  const makeBtn = h('button', { class: 'primary', disabled: true }, 'Make the clips');
  const skipBtn = h('button', {}, 'Skip: use Clipper\'s own picks');
  let data = null;

  async function copyPrompt(mode, part, btn) {
    btn.disabled = true;
    try {
      const r = await api(`/api/jobs/${job.id}/chat/prompt?mode=${mode}&part=${part}`);
      try { await navigator.clipboard.writeText(r.text); manual.hidden = true; }
      catch { manual.value = r.text; manual.hidden = false; manual.select(); }   // copy it by hand instead
      btn.textContent = btn.textContent.replace(/^(✓ )?/, '✓ ');
      toast(manual.hidden ? 'Prompt copied. Paste it into your chatbot, then paste its answer below.' : 'Select all of the text below and copy it.');
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  }
  function drawCopy() {
    if (!data) return;
    const full = modeFull.checked || data.short;
    copyRow.replaceChildren(...(full && data.parts > 1
      ? Array.from({ length: data.parts }, (_, i) => { const b = h('button', {}, `Copy part ${i + 1} of ${data.parts}`); b.onclick = () => copyPrompt('full', i + 1, b); return b; })
      : [(() => { const b = h('button', { class: 'primary' }, 'Copy the prompt'); b.onclick = () => copyPrompt(full ? 'full' : 'flagged', 1, b); return b; })()]));
  }
  function drawTally() {
    if (!data) return;
    tally.textContent = data.found ? `${data.found} moment${data.found === 1 ? '' : 's'} added so far (Clipper makes the best ${data.want}).` : 'No moments added yet.';
    makeBtn.disabled = !data.found;
  }
  modeQuick.onchange = modeFull.onchange = drawCopy;
  addBtn.onclick = async () => {
    if (!answer.value.trim()) return toast('Paste the chatbot\'s answer first', true);
    addBtn.disabled = true;
    try {
      const r = await api(`/api/jobs/${job.id}/chat/answer`, { body: { text: answer.value } });
      data.found = r.total; drawTally(); answer.value = '';
      toast(`Added ${r.added} moment${r.added === 1 ? '' : 's'}` + (r.skipped ? ` (${r.skipped} overlapped ones already added)` : '') +
        (data.parts > 1 && modeFull.checked ? '. Paste the next part\'s answer, or make the clips.' : '. Press "Make the clips".'));
    } catch (err) { toast(err.message, true); }
    addBtn.disabled = false;
  };
  const finish = async (builtin) => {
    makeBtn.disabled = skipBtn.disabled = true;
    try {
      const r = await api(`/api/jobs/${job.id}/chat/finish`, { body: { builtin } });
      chatPanels.delete(job.id);
      toast(`${r.clips} clips made. The best ones are rendering now.`);
      poll(); refreshJobs();
    } catch (err) { toast(err.message, true); makeBtn.disabled = !(data && data.found); skipBtn.disabled = false; }
  };
  makeBtn.onclick = () => finish(false);
  skipBtn.onclick = () => { if (confirm('Skip the chatbot and make the clips from Clipper\'s own picks?')) finish(true); };

  const el = h('div', { class: 'progress-card chat-card' },
    h('b', {}, 'Pick the clips with your chatbot'), info,
    h('div', { class: 'chat-step' }, h('span', { class: 'chat-num' }, '1'), h('div', {},
      h('div', {}, 'Copy the prompt and paste it into a chatbot: ',
        ...CHAT_SITES.flatMap(([n, u], i) => [i ? ', ' : '', h('a', { href: u, target: '_blank' }, n)]), ' or any other.'),
      h('div', { class: 'chat-modes' },
        h('label', { class: 'row small' }, modeQuick, h('span', {}, 'Quick: only the stretches Clipper flagged as promising (one prompt)')),
        h('label', { class: 'row small' }, modeFull, fullLabel)),
      copyRow, manual)),
    h('div', { class: 'chat-step' }, h('span', { class: 'chat-num' }, '2'), h('div', {},
      h('div', {}, 'Paste the chatbot\'s answer here. With several parts, paste each answer and press "Add" every time.'),
      answer, h('div', { class: 'actions left' }, addBtn, tally))),
    h('div', { class: 'chat-step' }, h('span', { class: 'chat-num' }, '3'), h('div', {},
      h('div', { class: 'actions left' }, makeBtn, skipBtn))));
  chatPanels.set(job.id, el);
  api(`/api/jobs/${job.id}/chat`).then((d) => {
    data = d;
    info.textContent = 'The video is transcribed. Nothing is cut until you finish these steps.';
    fullLabel.textContent = d.parts > 1 ? `The whole transcript (${d.parts} prompts, one after another; more thorough)` : 'The whole transcript (one prompt)';
    if (d.short) { modeQuick.closest('.chat-modes').hidden = true; }
    drawCopy(); drawTally();
  }).catch((err) => { info.textContent = err.message; });
  return el;
}

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
  const busy = job.status === 'analyzing' || job.status === 'live' || job.clips.some((c) => c.status === 'queued' || c.status === 'rendering');
  pollTimer = setTimeout(poll, busy ? 1200 : 5000);
}

function renderJob(job) {
  const root = $('#job');
  if (!root.firstChild) {
    root.append(
      h('div', { class: 'job-head' },
        h('div', {}, h('h2', { id: 'j-title' }), h('div', { class: 'hint', id: 'j-sub' })),
        h('div', { class: 'actions wrap' },
          h('button', { onclick: () => batch('render-all', { preview: true }, 'quick preview'), title: 'Fast low-quality previews of every clip' }, 'Preview all'),
          h('button', { onclick: () => batch('render-all', {}, 'full-quality render'), title: 'Render every clip in full quality with its current settings' }, 'Render all'),
          h('button', { onclick: exportAll, title: 'Copy every full-quality clip to the exports folder with its description' }, 'Export all'),
          h('button', { onclick: scheduleAll, title: 'Upload every rendered clip to YouTube, one per free daily slot, best score first' }, 'Schedule all to YouTube'),
          h('button', { onclick: () => changeJobSetup(state.job), title: 'Framing, facecam, captions, effects, sounds, song and timeline options for every clip of this video' }, 'Change settings…'),
          h('button', { class: 'danger', onclick: removeJob }, 'Delete'))),
      h('div', { id: 'j-status' }),
      h('div', { id: 'j-clips', class: 'clips' }));
  }
  $('#j-title').textContent = job.title || job.url;
  const kinds = { irl: 'IRL', gaming: 'Gaming', film: 'Film / TV reaction', podcast: 'Podcast' };
  const from = job.platform && job.platform !== 'local file' ? `${job.streamer} on ${job.platform}` : job.platform || job.streamer;
  const bits = [from, [kinds[job.kind], job.topic].filter(Boolean).join(': '), job.duration ? fmtTime(job.duration) + ' long' : null];
  if (job.status === 'ready') {
    if (job.ai) {
      const used = job.ai.cost_usd != null ? `about $${job.ai.cost_usd.toFixed(2)}`
        : job.ai.input_tokens ? `${Math.round((job.ai.input_tokens + (job.ai.output_tokens || 0)) / 1000)}k tokens` : '';
      bits.push(`picked by ${job.ai.provider || 'Claude'} + the built-in engine (${[job.ai.model, used].filter(Boolean).join(', ')})`);
    } else bits.push('scored by the built-in algorithm');
    if (job.engine) bits.push('speech recognition on ' + (job.engine.device === 'cuda' ? 'GPU' : 'CPU'));
  }
  $('#j-sub').textContent = bits.filter(Boolean).join(' · ');

  const status = $('#j-status');
  const parts = [];
  if (job.status === 'analyzing') {
    // The engine stamps every progress update; a long silence means a step is genuinely stuck.
    const quietMin = job.updated ? Math.floor((Date.now() / 1000 - job.updated) / 60) : 0;
    parts.push(h('div', { class: 'progress-card' },
      h('div', {}, job.stage),
      h('div', { class: 'bar' }, h('div', { style: `width:${Math.max(3, job.progress * 100)}%` })),
      quietMin >= 5
        ? h('div', { class: 'notice bad' }, `No progress for ${quietMin} minutes, so this step may be stuck. `,
          h('button', { class: 'link', onclick: openLogs }, 'Open the log folder'), ' and send clipper.log for help.')
        : h('div', { class: 'hint' }, 'Long videos take a while: the audio is downloaded and transcribed before clips appear.')));
  }
  if (job.live_mode) {
    const lv = job.live || {};
    const rec = job.status === 'live';
    const found = job.clips.length;
    parts.push(h('div', { class: 'progress-card live-card' + (rec ? ' on' : '') },
      h('div', { class: 'live-row' },
        rec ? h('span', { class: 'live-dot' }, '● LIVE') : h('span', { class: 'hint' }, 'Live recording'),
        h('b', {}, lv.state || ''),
        h('span', { class: 'hint' }, `${fmtTime(lv.recorded || 0)} recorded · ${found} clip${found === 1 ? '' : 's'} found`),
        rec ? h('button', { class: 'danger', disabled: !!lv.stopping, onclick: stopLive }, lv.stopping ? 'Stopping…' : 'Stop recording')
          : h('button', { onclick: resumeLive, title: 'Start recording again if the stream is still live' }, 'Resume recording')),
      lv.error ? h('div', { class: 'notice bad' }, lv.error) : null,
      rec ? h('div', { class: 'hint' }, 'Clipper records the stream in 2-minute pieces and checks each one for moments as it finishes, so the first clips appear a few minutes in. Only moments scoring at least ' +
        (state.settings.live_min_score || 60) + ' become clips (change it in Settings). Keep Clipper open while it records.') : null));
  }
  if (job.status === 'waiting_chat') parts.push(chatPanel(job));
  if (job.status === 'error') parts.push(h('div', { class: 'notice bad' }, 'This video could not be processed. ', job.error || '', ' ',
    h('button', { class: 'link', onclick: openLogs }, 'Open the log folder')));
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

async function stopLive() {
  if (!confirm('Stop recording? Clips already found are kept, and the last few minutes are still checked for moments.')) return;
  try { await api(`/api/jobs/${state.jobId}/live/stop`, { method: 'POST' }); poll(); } catch (err) { toast(err.message, true); }
}

async function resumeLive() {
  try { await api(`/api/jobs/${state.jobId}/live/resume`, { method: 'POST' }); toast('Recording again'); poll(); } catch (err) { toast(err.message, true); }
}

async function batch(path, body, what) {
  try {
    const r = await api(`/api/jobs/${state.jobId}/${path}`, { body });
    toast(r.queued ? `${r.queued} clip${r.queued === 1 ? '' : 's'} queued for a ${what}` : 'Nothing to do: every clip is already working');
    poll();
  } catch (err) { toast(err.message, true); }
}

async function exportAll() {
  try {
    const r = await api(`/api/jobs/${state.jobId}/export-all`, { method: 'POST' });
    if (!r.count) return toast('No full-quality clips to export yet. Render them first.', true);
    toast(`Exported ${r.count} clip${r.count === 1 ? '' : 's'}`);
    api('/api/open-folder', { body: { which: 'exports' } });
  } catch (err) { toast(err.message, true); }
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
  songSelectOptions(select, current, true);
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
    h('option', { value: 'bars' }, 'Black bars'), h('option', { value: 'auto' }, 'Auto'),
    h('option', { value: 'split' }, 'Facecam top, gameplay bottom'),
    h('option', { value: 'crop' }, 'Centre crop (full screen)'), h('option', { value: 'blur' }, 'Blurred background (same video)'),
    h('option', { value: 'story' }, 'Story card (text above, video in a box)'));
  const caps = h('select', {},
    h('option', { value: 'meme' }, 'Meme caption (one line, whole clip)'),
    h('option', { value: 'edit' }, 'Edit captions (coloured keywords, mid-screen)'),
    h('option', { value: 'pop' }, 'Word-by-word pop'), h('option', { value: 'karaoke' }, 'Karaoke line'),
    h('option', { value: 'clean' }, 'Clean'), h('option', { value: 'none' }, 'None'));
  const zoom = h('input', { type: 'range', min: 30, max: 100, step: 1, value: clip.zoom || 73 });
  const zoomLabel = h('span', {});
  const zoomField = h('label', { class: 'field' }, zoomLabel, zoom);
  const camPos = h('select', {}, ...CHOICES.cam_pos.map(([k, l]) => h('option', { value: k }, l)));
  const splitGame = h('select', {},
    h('option', { value: 'fill' }, 'Fill the panel (crops the sides)'), h('option', { value: 'fit' }, 'Show the whole game (blurred edges)'));
  let camBox = clip.cam_box || null;       // the area marked by hand, as shares of the frame
  camPos.value = clip.cam_pos === 'custom' && !camBox ? 'auto' : (clip.cam_pos || 'auto'); splitGame.value = clip.split_game || 'fill';
  let lastInfo = clip.layout_info;          // the facecam found automatically, to start the marker from
  const markBtn = h('button', { type: 'button', title: 'Draw a box around the facecam on a frame of this clip' }, 'Mark the facecam…');
  markBtn.onclick = async () => {
    let start = camBox;
    const cam = lastInfo && lastInfo.cam;
    if (!start && cam && lastInfo.width) start = { x: cam.x / lastInfo.width, y: cam.y / lastInfo.height, w: cam.w / lastInfo.width, h: cam.h / lastInfo.height };
    const r = await markFacecam(`/api/jobs/${job.id}/frame?clip=${clip.id}`, start, job.clips.length > 1);
    if (!r) return;
    camBox = r.box; camPos.value = 'custom';
    try {
      const q = await api(`/api/jobs/${job.id}/cam`, { body: { box: r.box, clip: clip.id, all: r.all } });
      toast((r.all ? 'Facecam area set for every clip in this video' + (q.queued ? `; ${q.queued} re-rendering` : '') : 'Facecam area set')
        + '. Press "Re-render with changes" (or Quick preview) to see this clip with it.');
      if (r.all) poll();
    } catch (err) { toast(err.message, true); }
  };
  camPos.addEventListener('change', () => { if (camPos.value === 'custom' && !camBox) markBtn.click(); });
  const splitFields = h('div', { class: 'grid2' },
    h('label', { class: 'field' }, h('span', {}, 'Where the facecam is in the stream'), camPos, markBtn),
    h('label', { class: 'field' }, h('span', {}, 'Gameplay (bottom)'), splitGame));
  const showZoom = () => {
    zoomLabel.textContent = `Zoom: picture fills ${zoom.value}% of the height` + (+zoom.value >= 100 ? ' (no bars)' : '');
    zoomField.hidden = !['bars', 'blur'].includes(layout.value);
    storyFields.hidden = layout.value !== 'story';
    splitFields.hidden = !['split', 'auto'].includes(layout.value);
  };
  const storyText = h('textarea', { rows: 3, maxLength: 400, value: clip.story || clip.title || '',
    placeholder: 'What is happening and why it matters. Put [square brackets] around phrases to colour them.' });
  const comment = h('input', { type: 'text', maxLength: 200, value: clip.comment || '', placeholder: 'optional: your take, shown under the video' });
  const storyFields = h('div', { class: 'controls-sub' },
    h('label', { class: 'field' }, h('span', {}, 'Story text above the video ([brackets] = coloured; channel name is set in Settings)'), storyText),
    h('label', { class: 'field' }, h('span', {}, 'Comment under the video'), comment));
  zoom.addEventListener('input', showZoom);
  layout.addEventListener('change', showZoom);
  const meme = h('input', { type: 'text', value: clip.context || clip.hook || '', maxLength: 90,
    placeholder: 'e.g. him explaining why he quit his job' });
  const memeField = h('label', { class: 'field' }, h('span', {}, 'Meme caption text (shown the whole clip; asterisks and capitals are added)'), meme);
  const hookField = h('label', { class: 'field' }, h('span', {}, 'Hook text (first 3 seconds)'), hook);
  const showCaptionFields = () => { memeField.hidden = caps.value !== 'meme'; hookField.hidden = caps.value === 'meme'; };
  caps.addEventListener('change', showCaptionFields);
  const colour = h('select', {}, ...['yellow', 'green', 'cyan', 'pink', 'orange', 'white'].map((c) => h('option', {}, c)));
  const capSize = h('select', {}, ...CHOICES.caption_size.map(([k, l]) => h('option', { value: k }, l)));
  const capPos = h('select', {}, ...CHOICES.caption_position.map(([k, l]) => h('option', { value: k }, l)));
  const showHook = h('input', { type: 'checkbox', checked: clip.show_hook !== false });
  capSize.value = clip.caption_size || 'medium'; capPos.value = clip.caption_position || 'auto';
  const song = h('select', {});
  const volume = h('input', { type: 'range', min: 0.03, max: 0.6, step: 0.01, value: clip.music_volume });
  const bar = h('input', { type: 'checkbox', checked: !!clip.progress_bar });
  layout.value = clip.layout; caps.value = clip.caption_style; colour.value = clip.highlight;
  showCaptionFields();
  showZoom();
  songOptions(song, clip.song);

  // timeline options
  const opt = (key, label, tip, dflt) => {
    const box = h('input', { type: 'checkbox', checked: clip[key] ?? dflt });
    return { box, el: h('label', { class: 'row small', title: tip }, box, ' ' + label) };
  };
  const tighten = opt('tighten', 'Cut pauses', 'Removes silent gaps between sentences (jump cuts). Loud action is never cut.', true);
  const coldOpen = opt('cold_open', 'Cold open', 'Starts with a 2-second teaser of the payoff, then jumps back to the start of the story.', false);
  const trackFace = opt('track_face', 'Follow the face', 'The crop pans smoothly to keep the person in frame when they move.', true);
  const beatSync = opt('beat_sync', 'Effects on the beat', 'With a song chosen, effects snap to its beats.', true);
  const retime = () => fx.resuggest();
  tighten.box.addEventListener('change', retime);
  coldOpen.box.addEventListener('change', retime);
  beatSync.box.addEventListener('change', retime);
  song.addEventListener('change', () => { if (beatSync.box.checked) retime(); });

  // AI ideas for the title, hook, meme caption and story text
  const ideasBox = h('div', { class: 'ideas' });
  const ideasBtn = h('button', { class: 'link', title: 'Ask the connected AI for other titles, hooks and captions' }, 'More title ideas');
  ideasBtn.onclick = async () => {
    ideasBtn.disabled = true; ideasBtn.textContent = 'Thinking…';
    try {
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/ideas`, { method: 'POST' });
      const group = (label, items, target) => items.length ? h('div', { class: 'idea-group' }, h('span', { class: 'hint' }, label),
        ...items.map((t) => h('button', { class: 'chip', title: 'Use this', onclick: () => { target.value = t; target.dispatchEvent(new Event('input')); } }, t))) : null;
      ideasBox.replaceChildren(group('Titles', r.titles, title), group('Hooks', r.hooks, hook), group('Meme captions', r.contexts, meme),
        group('Story texts', r.stories, storyText), h('button', { class: 'link', onclick: () => ideasBox.replaceChildren() }, 'Hide ideas'));
    } catch (err) { toast(err.message, true); }
    ideasBtn.disabled = false; ideasBtn.textContent = 'More title ideas';
  };

  const draft = { start: clip.start, end: clip.end };
  const range = h('span', { class: 'range' });
  const showRange = () => {
    const len = Math.round(draft.end - draft.start);
    const final = clip.final_len && Math.abs(clip.final_len - len) > 0.5 && draft.start === clip.start && draft.end === clip.end
      ? `, ${Math.round(clip.final_len)}s after edits` : '';
    range.textContent = `${fmtTime(draft.start)} → ${fmtTime(draft.end)} (${len}s${final})`;
  };
  const nudge = (key, d) => () => {
    draft[key] = Math.max(0, draft[key] + d);
    if (draft.end - draft.start < 5) draft[key] -= d;
    showRange();
  };
  showRange();

  const renderBtn = h('button', { class: 'primary', onclick: () => doRender(false) });
  const previewBtn = h('button', { onclick: () => doRender(true), title: 'Low-quality version in a fraction of the time, to check changes' }, 'Quick preview');
  const exportBtn = h('button', { onclick: doExport }, 'Export');
  const styleBtn = h('button', { class: 'link', onclick: applyStyle, title: 'Framing, captions, effects style, filter, song and timeline options' }, 'Use this style for every clip');
  const copyBox = h('div', { class: 'copy' });
  const layoutNote = h('span', { class: 'hint' });
  const badge = h('div', { class: 'badge', hidden: true }, 'Quick preview');
  const shareButtons = [
    h('button', { onclick: () => share('tiktok'), title: 'Exports the video, shows it in Explorer, copies the caption and opens TikTok upload' }, 'Post to TikTok'),
    h('button', { onclick: () => share('instagram'), title: 'Exports the video, shows it in Explorer, copies the caption and opens Instagram' }, 'Post to Reels'),
  ];

  const settingsBody = () => ({
    title: title.value, hook: hook.value, context: meme.value.trim(), zoom: +zoom.value, hashtags: parseTags(tags.value), layout: layout.value, caption_style: caps.value,
    highlight: colour.value, song: song.value, music_volume: +volume.value, progress_bar: bar.checked,
    start: draft.start, end: draft.end, story: storyText.value.trim(), comment: comment.value.trim(),
    tighten: tighten.box.checked, cold_open: coldOpen.box.checked, track_face: trackFace.box.checked, beat_sync: beatSync.box.checked,
    cam_pos: camPos.value === 'custom' && !camBox ? 'auto' : camPos.value, cam_box: camBox || undefined, split_game: splitGame.value,
    caption_size: capSize.value, caption_position: capPos.value, show_hook: showHook.checked,
    ...fx.value(),
  });
  async function doRender(preview) {
    try {
      await api(`/api/jobs/${job.id}/clips/${clip.id}/render`, { body: { ...settingsBody(), preview } });
      poll();
    } catch (err) { toast(err.message, true); }
  }
  async function applyStyle() {
    if (!confirm('Give every other clip in this video the same framing, captions, effects style, filter, song and timeline options, and re-render them? Each keeps its own text and gets effects placed on its own loud moments.')) return;
    try {
      await api(`/api/jobs/${job.id}/clips/${clip.id}/render`, { body: settingsBody() });
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/apply-style`, { method: 'POST' });
      toast(`Style applied to ${r.queued} other clip${r.queued === 1 ? '' : 's'}`);
      poll();
    } catch (err) { toast(err.message, true); }
  }
  async function share(platform) {
    try {
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/share`, { body: { platform } });
      try { await navigator.clipboard.writeText(r.caption); } catch {}
      window.open(r.url, '_blank');
      toast(`The video is selected in Explorer and the caption is copied. Drag the file into the ${platform === 'tiktok' ? 'TikTok' : 'Instagram'} page and paste the caption.`);
    } catch (err) { toast(err.message, true); }
  }
  async function doExport() {
    try {
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/export`, { method: 'POST' });
      toast('Saved to ' + r.path);
    } catch (err) { toast(err.message, true); }
  }

  const field = (label, control, extra) => h('label', { class: 'field' }, h('span', {}, label), control, extra);
  const fx = buildEffects(job, clip, () => ({ start: draft.start, end: draft.end, tighten: tighten.box.checked,
    cold_open: coldOpen.box.checked, song: song.value.startsWith('auto:') ? undefined : song.value }), video);
  const el = h('article', { class: 'clip' },
    h('div', { class: 'player' }, video, overlay, badge),
    h('div', { class: 'info' },
      h('div', { class: 'score-row' },
        h('div', { class: 'score ' + bandClass }, h('b', {}, String(clip.score)), h('small', {}, '/100')),
        h('div', {},
          h('div', { class: 'score-label' }, 'Viral chance: ' + bandText),
          h('div', { class: 'hint' }, clip.engine ? `Combined: ${clip.scored_by || 'AI'} ${clip.engine.ai} + built-in engine ${clip.engine.builtin}${clip.engine.agreement >= 0.5 ? ', both found this moment' : ''}`
            : clip.source === 'ai' || clip.source === 'claude' ? `${clip.scored_by || 'Claude'}'s estimate from the transcript and audio` : 'Built-in estimate from loudness and speech cues'))),
      h('div', { class: 'bars' }, Object.entries(clip.breakdown || {}).map(([k, v]) =>
        h('div', { class: 'mini' }, h('span', {}, k), h('div', { class: 'bar' }, h('div', { style: `width:${v}%` })), h('em', {}, String(v))))),
      h('ul', { class: 'reasons' }, (clip.reasons || []).map((r) => h('li', {}, r))),
      copyBox,
      h('div', { class: 'controls' },
        field('Title', title, ideasBtn), ideasBox, field('Hashtags (#shorts is added automatically)', tags),
        memeField, hookField,
        h('div', { class: 'grid2' }, field('Framing', layout, layoutNote), field('Captions', caps)),
        splitFields, zoomField, storyFields,
        h('div', { class: 'grid2' }, field('Caption size', capSize), field('Caption position', capPos)),
        h('label', { class: 'row small' }, showHook, ' Hook text for the first seconds'),
        h('div', { class: 'grid2' }, field('Highlight', colour), field('Song', song)),
        h('div', { class: 'grid2' }, field('Song volume', volume),
          h('label', { class: 'row' }, bar, ' Progress bar')),
        h('div', { class: 'field' }, h('span', {}, 'Cut'),
          h('div', { class: 'cut' },
            h('button', { onclick: nudge('start', -1), title: 'Start 1s earlier' }, '◀ start'),
            h('button', { onclick: nudge('start', 1), title: 'Start 1s later' }, 'start ▶'),
            range,
            h('button', { onclick: nudge('end', -1), title: 'End 1s earlier' }, '◀ end'),
            h('button', { onclick: nudge('end', 1), title: 'End 1s later' }, 'end ▶'))),
        h('div', { class: 'opts' }, tighten.el, coldOpen.el, trackFace.el, beatSync.el),
        fx.el),
      h('div', { class: 'actions' }, styleBtn, previewBtn, renderBtn, exportBtn),
      h('div', { class: 'yt' }, h('span', { class: 'hint' }, 'YouTube'), h('div', { class: 'yt-row' }, ytButtons), ytStatus,
        h('div', { class: 'yt-row' }, h('span', { class: 'hint' }, 'Other apps:'),
          ...shareButtons))));

  let shown = -1;
  let shownKey = '';
  function update(c) {
    const busy = c.status === 'queued' || c.status === 'rendering';
    const isPreview = c.showing === 'preview';
    const key = isPreview ? 'p' + c.preview_version : 'f' + c.version;
    if (c.status === 'ready' && (c.version > 0 || c.preview_version > 0) && key !== shownKey) {
      shownKey = key;
      shown = Math.max(c.version, c.preview_version || 0);
      video.src = isPreview ? `/media/${job.id}/${c.id}.mp4?kind=preview&v=${c.preview_version}` : `/media/${job.id}/${c.id}.mp4?v=${c.version}`;
    }
    badge.hidden = !(isPreview && shown >= 0);
    clip.final_len = c.final_len; showRange();
    lastInfo = c.layout_info || lastInfo;
    if (c.cam_pos === 'custom' && c.cam_box && JSON.stringify(c.cam_box) !== JSON.stringify(camBox)) {
      camBox = c.cam_box; camPos.value = 'custom';     // marked on another clip of this video
    }
    video.hidden = shown < 0;
    fx.sync(c);
    if (busy) overlay.replaceChildren(h('div', { class: 'spinner' }), h('div', {}, c.status === 'queued' ? 'Waiting in line' : (c.step || 'Working')));
    else if (c.status === 'error') overlay.replaceChildren(h('div', { class: 'bad' }, 'Render failed'), h('div', { class: 'hint' }, c.error || ''));
    else if (c.status === 'idle') overlay.replaceChildren(h('div', {}, 'Not rendered yet'), h('div', { class: 'hint' }, `${fmtTime(c.start)} in the stream`));
    else overlay.replaceChildren();
    overlay.hidden = !overlay.firstChild;
    overlay.classList.toggle('over-video', busy && shown >= 0);
    renderBtn.disabled = busy;
    renderBtn.textContent = busy ? 'Working…' : shown < 0 ? 'Render this clip' : 'Re-render with changes';
    previewBtn.disabled = busy;
    const fullReady = c.version > 0 && !isPreview;
    exportBtn.disabled = busy || !fullReady;
    exportBtn.title = fullReady ? '' : 'Render in full quality first';
    for (const b of shareButtons) b.disabled = busy || !fullReady;
    layoutNote.textContent = [c.used_layout && layout.value === 'auto' ? 'auto picked: ' + ({ split: 'facecam + gameplay', crop: 'centre crop', blur: 'blurred background', bars: 'black bars' })[c.used_layout] : '',
      c.layout_note || ''].filter(Boolean).join('. ');
    if (c.song && c.song !== song.value && !c.song.startsWith('auto:') && song.value.startsWith('auto:')) songOptions(song, c.song);   // show the song auto picked

    const yt = c.youtube;
    const ytBusy = yt && ['queued', 'uploading', 'scheduled', 'uploaded'].includes(yt.status);
    for (const b of ytButtons) b.disabled = busy || !fullReady || ytBusy;
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

// ---------- effects and sounds ----------
async function loadEffects() {
  try { state.fx = await api('/api/effects'); } catch { state.fx = null; }
}

function soundOptions(select, current) {
  const sounds = (state.fx && state.fx.sounds) || [];
  const groups = [h('option', { value: '' }, 'No sound')];
  const built = sounds.filter((s) => s.source === 'builtin'), mine = sounds.filter((s) => s.source === 'mine');
  if (built.length) groups.push(h('optgroup', { label: 'Built-in' }, built.map((s) => h('option', { value: s.id }, s.name))));
  if (mine.length) groups.push(h('optgroup', { label: 'My sounds' }, mine.map((s) => h('option', { value: s.id }, '★ ' + s.name))));
  select.replaceChildren(...groups);
  select.value = sounds.some((s) => s.id === current) ? current : '';
}

function buildEffects(job, clip, getRange, video) {
  const cat = state.fx || { presets: {}, filters: {}, visuals: {}, emojis: [], sounds: [] };
  let events = (clip.effects || []).map((e) => ({ ...e }));
  let touched = false;
  const opts = (obj) => Object.entries(obj).map(([k, v]) => h('option', { value: k }, v));
  const preset = h('select', {}, ...opts(cat.presets));
  const filter = h('select', {}, ...opts(cat.filters));
  const vol = h('input', { type: 'range', min: 0, max: 1.5, step: 0.05, value: clip.sfx_volume ?? 0.7 });
  preset.value = clip.effects_preset || 'edit';
  filter.value = clip.filter || (preset.value === 'none' ? 'none' : preset.value === 'cinematic' ? 'cinematic' : preset.value === 'edit' ? 'edit' : 'vivid');
  const list = h('div', { class: 'fx-list' });
  const note = h('p', { class: 'hint' });
  const changed = () => { touched = true; };

  // Which sounds the automatic effects use, and optional fixed sounds per kind of effect.
  const source = h('select', {}, h('option', { value: 'auto' }, 'My sounds first'), h('option', { value: 'mine' }, 'Only my sounds'),
    h('option', { value: 'builtin' }, 'Only built-in sounds'));
  source.value = clip.sfx_source || state.settings.sfx_source || 'auto';
  const ROLE_LABELS = { intro: 'Start of the clip', hit: 'Loud hits', big: 'Biggest hit', transition: 'Transitions (whip blur)', ending: 'Ending', emoji: 'Emoji pop-ups' };
  const fixed = { ...(clip.sfx_fixed || {}) };
  const roleSelects = Object.entries(ROLE_LABELS).map(([role, label]) => {
    const sel = h('select', {});
    soundOptions(sel, fixed[role] || '');
    sel.options[0].textContent = 'Auto (fits the moment)';
    sel.addEventListener('change', () => { if (sel.value) fixed[role] = sel.value; else delete fixed[role]; suggest(); });
    return h('label', { class: 'field' }, h('span', {}, label), sel);
  });
  const fixedBox = h('details', { class: 'fx-fixed' }, h('summary', { class: 'hint' }, 'Always use a particular sound for…'),
    h('div', { class: 'grid3' }, ...roleSelects));
  source.addEventListener('change', () => suggest());

  function row(e) {
    const t = h('input', { type: 'number', min: 0, step: 0.1, value: e.t, title: 'Seconds from the start of the Short' });
    t.addEventListener('change', () => { e.t = Math.max(0, +t.value || 0); changed(); });
    const go = h('button', { class: 'mini-btn', title: 'Jump the preview here', onclick: () => { if (!video.hidden) { video.currentTime = e.t; video.play(); } } }, '⏵');
    const vis = h('select', {}, ...opts(cat.visuals));
    vis.value = e.fx;
    const emoji = h('select', { class: 'emoji' }, ...cat.emojis.map((x) => h('option', { value: x }, x)));
    emoji.value = e.emoji || '💀';
    const showEmoji = () => { emoji.hidden = !['emoji', 'ending'].includes(vis.value); };
    vis.addEventListener('change', () => { e.fx = vis.value; if (!e.emoji) e.emoji = emoji.value; showEmoji(); changed(); });
    emoji.addEventListener('change', () => { e.emoji = emoji.value; changed(); });
    showEmoji();
    const snd = h('select', {});
    soundOptions(snd, e.sound);
    snd.addEventListener('change', () => { e.sound = snd.value; changed(); });
    const play = h('button', { class: 'mini-btn', title: 'Hear this sound' }, '▶');
    play.onclick = () => snd.value && preview('/api/sfx/' + snd.value.split('/').map(encodeURIComponent).join('/'), play);
    const del = h('button', { class: 'mini-btn danger', title: 'Remove', onclick: () => { events = events.filter((x) => x !== e); changed(); draw(); } }, '✕');
    return h('div', { class: 'fx-row' }, t, go, vis, emoji, snd, play, del);
  }
  function draw() {
    events.sort((a, b) => a.t - b.t);
    list.replaceChildren(...(events.length ? events.map(row)
      : [h('p', { class: 'hint' }, clip.effects == null && !touched ? 'Effects are placed on the loud moments when this clip renders, or press "Suggest" now.' : 'No effects. Add one below.')]));
  }
  async function suggest() {
    try {
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/effects/suggest`, { body: { preset: preset.value, ...getRange(), sfx_source: source.value, sfx_fixed: fixed } });
      events = r.effects; filter.value = r.filter; touched = true; draw();
    } catch (err) { toast(err.message, true); }
  }
  preset.addEventListener('change', suggest);
  const add = h('button', { onclick: () => {
    const t = video.hidden ? 0 : Math.round(video.currentTime * 10) / 10;
    events.push({ t, fx: 'punch', sound: 'builtin/hit', emoji: '💀' });
    changed(); draw();
  } }, '+ Add effect at the preview\'s current time');
  draw();
  const el = h('details', { class: 'fx' },
    h('summary', {}, 'Effects, filters and sound effects'),
    h('div', { class: 'grid3' },
      h('label', { class: 'field' }, h('span', {}, 'Style (fills in the list below)'), preset),
      h('label', { class: 'field' }, h('span', {}, 'Colour filter'), filter),
      h('label', { class: 'field' }, h('span', {}, 'Sound effects volume'), vol),
      h('label', { class: 'field' }, h('span', {}, 'Sounds to use'), source)),
    fixedBox,
    h('div', { class: 'fx-head hint' }, h('span', {}, 'Time (s)'), h('span', {}, 'Visual effect'), h('span', {}, 'Sound')),
    list,
    h('div', { class: 'fx-actions' }, add, h('button', { onclick: suggest }, 'Suggest again'),
      h('button', { class: 'link', onclick: () => api('/api/open-folder', { body: { which: 'sfx' } }) }, 'Add my own sounds (opens the folder)'),
      h('button', { class: 'link', onclick: async () => { await loadEffects(); draw(); } }, 'Refresh sounds')),
    note);
  note.textContent = 'Pause the preview where you want an effect, then add it. Changes show after "Re-render with changes".';
  return {
    el,
    resuggest: () => { if (touched || clip.effects != null) suggest(); },
    value: () => ({ effects_preset: preset.value, filter: filter.value, sfx_volume: +vol.value,
      sfx_source: source.value, sfx_fixed: { ...fixed },
      effects: touched || clip.effects != null ? events.map(({ t, fx, sound, emoji }) => ({ t, fx, sound, emoji })) : undefined }),
    sync(c) {
      if (!touched && c.effects && JSON.stringify(c.effects) !== JSON.stringify(events)) {
        events = c.effects.map((e) => ({ ...e }));
        clip.effects = c.effects;
        if (c.filter) filter.value = c.filter;
        draw();
      }
    },
  };
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
$('#open-sfx-folder').onclick = () => api('/api/open-folder', { body: { which: 'sfx' } });

// ---------- settings ----------
// Only what is set once lives here; everything about how a video's clips are made is asked in the setup window.
const FIELDS = ['custom_base_url', 'max_height', 'whisper_model', 'language', 'audd_api_key', 'jamendo_client_id',
  'schedule_times', 'youtube_privacy', 'channel_name', 'channel_handle', 'channel_avatar', 'live_min_score', 'live_auto_render',
  'encoder', 'parallel_renders'];

// Connected AI: one key and model per provider. Typed values are kept per provider while switching,
// so trying a different provider doesn't wipe what was entered for the first.
const AI_NOTES = {
  anthropic: 'Roughly $0.05–0.50 per video, depending on its length and the model.',
  ollama: 'Free and private: install Ollama, download a model in it, and keep it running. Long streams need a model with a large context window, and small local models pick weaker moments.',
  groq: 'The free tier has low per-minute token limits, so long streams may be refused.',
  openrouter: 'One key reaches many companies\' models, including some free ones.',
  custom: 'Any service that offers the OpenAI Chat Completions API. Paste its base URL above.',
};
let drafts = {};

function stashDraft() {
  const p = $('#ai-provider').dataset.current;
  if (p) drafts[p] = { key: $('#ai-key').value, model: $('#ai-model').value.trim() };
}

function showProvider(p) {
  const s = state.settings, info = s.providers[p];
  $('#ai-provider').dataset.current = p;
  const d = drafts[p] || {};
  $('#ai-key').value = d.key || '';
  $('#ai-model').value = d.model ?? (s.ai_models[p] || '');
  $('#ai-key').placeholder = s.key_hints[p] ? `saved (${s.key_hints[p]}), type to replace` : info.needs_key ? 'paste your API key' : 'optional';
  $('#ai-key-link').href = info.key_url || '#';
  $('#ai-key-link').hidden = !info.key_url;
  $('#ai-key-link').textContent = p === 'ollama' ? 'download Ollama' : 'get a key';
  $('#ai-base-row').hidden = p !== 'custom';
  $('#ai-model-list').replaceChildren();
  $('#ai-note').textContent = AI_NOTES[p] || 'Cost depends on the model you pick; see the provider\'s pricing page.';
}

function showAiState() {
  const s = state.settings;
  $('#ai-state').textContent = s.ai_ready ? `Ready: ${s.ai_status}.` : `Not ready yet: ${s.ai_status}.`;
}

$('#ai-provider').onchange = () => { stashDraft(); showProvider($('#ai-provider').value); };
$('#ai-load').onclick = async () => {
  const p = $('#ai-provider').value;
  const btn = $('#ai-load');
  btn.disabled = true; btn.textContent = 'Loading…';
  try {
    const r = await api('/api/ai/models', { body: { provider: p, key: $('#ai-key').value.trim(), base_url: $('#s-custom_base_url').value.trim() } });
    $('#ai-model-list').replaceChildren(...r.models.map((m) => h('option', { value: m })));
    if (!$('#ai-model').value && r.models.includes('claude-opus-5-5')) $('#ai-model').value = 'claude-opus-5-5';
    $('#ai-note').textContent = r.models.length
      ? `Key works: ${r.models.length} models available. Click the Model box to pick one, or type its id.`
      : 'Connected, but the provider listed no chat models.';
    $('#ai-model').focus();
  } catch (err) {
    $('#ai-note').textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Load models';
  }
};

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
  $('#s-audd_api_key').placeholder = s.has_audd_key ? 'saved – type to replace' : 'optional';
  $('#encoder-state').textContent = `Videos are encoded on: ${s.encoder_used || 'CPU'}.` +
    (s.encoder !== 'auto' && s.encoder_used && !s.encoder_used.startsWith({ nvenc: 'NVIDIA', amf: 'AMD', qsv: 'Intel', mf: 'GPU', cpu: 'CPU' }[s.encoder] || '?')
      ? ' The encoder you picked does not work on this PC, so the automatic choice is used.' : '');
  $('#gpu-note').textContent = s.gpu_note || '';
  $('#gpu-note').hidden = !s.gpu_note;
  drafts = {};
  $('#ai-provider').replaceChildren(...Object.entries(s.providers).map(([id, p]) => h('option', { value: id }, p.label)));
  $('#ai-provider').value = s.ai_provider;
  showProvider(s.ai_provider);
  showAiState();
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
  patch.parallel_renders = +patch.parallel_renders;
  stashDraft();
  const chosen = $('#ai-provider').value;
  patch.ai_provider = chosen;
  if ((drafts[chosen] || {}).key && !(drafts[chosen] || {}).model) return toast('Pick a model for the connected AI too (Load models, then choose one).', true);
  try {
    // Keys and models typed for other providers are saved too, then the chosen provider and the rest.
    for (const [p, d] of Object.entries(drafts)) {
      if (p !== chosen && (d.key || d.model)) await api('/api/settings', { body: { ai_provider: p, ai_key: d.key, ai_model: d.model } });
    }
    const mine = drafts[chosen] || {};
    const s = await api('/api/settings', { body: { ...patch, ai_key: mine.key, ai_model: mine.model } });
    fillSettings(s);
    $('#settings').close();
    toast('Settings saved');
  } catch (err) { toast(err.message, true); }
};

// ---------- profile picture, updates ----------
$('#pick-avatar').onclick = async () => {
  if (!window.clipper || !window.clipper.pickImage) return toast('Type the full path to the picture instead', true);
  const path = await window.clipper.pickImage();
  if (path) $('#s-channel_avatar').value = path;
};
$('#update-check').onclick = async () => {
  const st = $('#update-state');
  st.textContent = 'Checking…';
  $('#update-apply').hidden = true;
  try {
    const r = await api('/api/update');
    if (!r.can_update) { st.textContent = r.reason; return; }
    if (!r.behind) { st.textContent = 'Clipper is up to date.'; return; }
    st.replaceChildren(`${r.behind} update${r.behind === 1 ? '' : 's'} available:`, h('ul', {}, r.changes.map((c) => h('li', {}, c))));
    $('#update-apply').hidden = false;
  } catch (err) { st.textContent = err.message; }
};
$('#update-apply').onclick = async () => {
  const st = $('#update-state');
  $('#update-apply').disabled = true;
  st.textContent = 'Updating… (installing new packages can take a few minutes)';
  try {
    const r = await api('/api/update', { method: 'POST' });
    st.textContent = r.message;
    $('#update-apply').hidden = true;
    $('#restart-app').hidden = !r.updated;
  } catch (err) { st.textContent = err.message; }
  $('#update-apply').disabled = false;
};
$('#restart-app').onclick = () => {
  if (window.clipper && window.clipper.restart) window.clipper.restart();
  else toast('Close Clipper and open it again', true);
};

// ---------- results (real YouTube numbers) ----------
function showResults(r) {
  const kinds = { irl: 'IRL', gaming: 'Gaming', film: 'Film / TV', podcast: 'Podcast' };
  const sum = [];
  for (const [k, v] of Object.entries(r.kinds || {})) {
    if (!v.uploads) continue;
    let line = `${kinds[k]}: ${v.uploads} uploaded, ${v.settled} settled.`;
    if (v.score_vs_views != null) {
      const c = v.score_vs_views;
      line += ` Score vs views: ${c >= 0.5 ? 'strong match' : c >= 0.2 ? 'some match' : c > -0.2 ? 'no clear link yet' : 'opposite'} (${c.toFixed(2)}).`;
    }
    if (v.learned) line += ' Built-in weights tuned to your channel: ' + Object.entries(v.learned).map(([a, b]) => `${a} ${Math.round(b * 100)}%`).join(', ') + '.';
    else if (v.settled < 8) line += ` ${8 - v.settled} more settled uploads before it starts learning.`;
    sum.push(h('p', {}, line));
  }
  $('#results-summary').replaceChildren(...(sum.length ? sum : [h('p', { class: 'hint' }, r.fetched ? 'No uploads from Clipper yet.' : 'Press "Get the latest numbers".')]),
    r.fetched ? h('p', { class: 'hint' }, 'Updated ' + localTime(r.fetched)) : '');
  const rows = [...(r.rows || [])].sort((a, b) => (b.views || 0) - (a.views || 0));
  $('#results-list').replaceChildren(...rows.map((x) => h('div', { class: 'track' },
    h('div', { class: 'rank', title: 'Predicted viral chance' }, String(x.score ?? '–')),
    h('div', { class: 'grow' }, h('div', {}, x.title),
      h('div', { class: 'hint' }, x.missing ? 'Not found on YouTube (deleted or still processing)'
        : `${(x.views || 0).toLocaleString()} views · ${x.likes || 0} likes · ${x.comments || 0} comments · ${x.views_per_day}/day · ${x.privacy}`)),
    h('a', { href: 'https://youtube.com/shorts/' + x.video_id, target: '_blank' }, 'Open'))));
}
$('#open-results').onclick = async () => {
  showResults(await api('/api/performance'));
  $('#results').showModal();
};
$('#results-refresh').onclick = async () => {
  const b = $('#results-refresh');
  b.disabled = true; b.textContent = 'Fetching…';
  try { showResults(await api('/api/performance/refresh', { method: 'POST' })); } catch (err) { toast(err.message, true); }
  b.disabled = false; b.textContent = 'Get the latest numbers';
};
$('#results-reset').onclick = async () => {
  if (!confirm('Go back to the default built-in weights? Your numbers are kept, and it will learn again next time you fetch them.')) return;
  await api('/api/performance/reset', { method: 'POST' });
  toast('Back to the default weights');
};
$('#results-close').onclick = () => $('#results').close();

// ---------- boot ----------
(async () => {
  try { state.tracks = (await api('/api/music')).tracks; } catch {}
  try { state.settings = await api('/api/settings'); } catch {}
  await loadSetupInfo();
  await loadEffects();
  await refreshJobs();
})();
