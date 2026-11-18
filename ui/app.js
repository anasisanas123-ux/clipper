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
        h('div', { class: 'actions wrap' },
          h('button', { onclick: () => batch('render-all', { preview: true }, 'quick preview'), title: 'Fast low-quality previews of every clip' }, 'Preview all'),
          h('button', { onclick: () => batch('render-all', {}, 'full-quality render'), title: 'Render every clip in full quality with its current settings' }, 'Render all'),
          h('button', { onclick: exportAll, title: 'Copy every full-quality clip to the exports folder with its description' }, 'Export all'),
          h('button', { onclick: scheduleAll, title: 'Upload every rendered clip to YouTube, one per free daily slot, best score first' }, 'Schedule all to YouTube'),
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
      bits.push(`scored by ${job.ai.provider || 'Claude'} (${[job.ai.model, used].filter(Boolean).join(', ')})`);
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
  const showZoom = () => {
    zoomLabel.textContent = `Zoom: picture fills ${zoom.value}% of the height` + (+zoom.value >= 100 ? ' (no bars)' : '');
    zoomField.hidden = !['bars', 'blur'].includes(layout.value);
    storyFields.hidden = layout.value !== 'story';
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
    cold_open: coldOpen.box.checked, song: song.value }), video);
  const el = h('article', { class: 'clip' },
    h('div', { class: 'player' }, video, overlay, badge),
    h('div', { class: 'info' },
      h('div', { class: 'score-row' },
        h('div', { class: 'score ' + bandClass }, h('b', {}, String(clip.score)), h('small', {}, '/100')),
        h('div', {},
          h('div', { class: 'score-label' }, 'Viral chance: ' + bandText),
          h('div', { class: 'hint' }, clip.source === 'ai' || clip.source === 'claude' ? `${clip.scored_by || 'Claude'}'s estimate from the transcript and audio` : 'Built-in estimate from loudness and speech cues'))),
      h('div', { class: 'bars' }, Object.entries(clip.breakdown || {}).map(([k, v]) =>
        h('div', { class: 'mini' }, h('span', {}, k), h('div', { class: 'bar' }, h('div', { style: `width:${v}%` })), h('em', {}, String(v))))),
      h('ul', { class: 'reasons' }, (clip.reasons || []).map((r) => h('li', {}, r))),
      copyBox,
      h('div', { class: 'controls' },
        field('Title', title, ideasBtn), ideasBox, field('Hashtags (#shorts is added automatically)', tags),
        memeField, hookField,
        h('div', { class: 'grid2' }, field('Framing', layout, layoutNote), field('Captions', caps)),
        zoomField, storyFields,
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
    layoutNote.textContent = c.used_layout && layout.value === 'auto' ? 'auto picked: ' + ({ split: 'facecam + gameplay', crop: 'centre crop', blur: 'blurred background', bars: 'black bars' })[c.used_layout] : '';

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
      const r = await api(`/api/jobs/${job.id}/clips/${clip.id}/effects/suggest`, { body: { preset: preset.value, ...getRange() } });
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
      h('label', { class: 'field' }, h('span', {}, 'Sound effects volume'), vol)),
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
const FIELDS = ['clip_count', 'min_len', 'max_len', 'default_layout', 'default_zoom', 'caption_style', 'custom_base_url',
  'highlight', 'max_height', 'progress_bar', 'whisper_model', 'language', 'audd_api_key', 'jamendo_client_id',
  'schedule_times', 'youtube_privacy', 'effects_preset', 'sfx_volume', 'channel_name', 'channel_handle', 'channel_avatar', 'tighten', 'cold_open'];

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
const aiScorer = () => document.querySelector('input[name=scorer]:checked')?.value || 'builtin';

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

function showScorer() {
  $('#ai-box').hidden = aiScorer() !== 'ai';
  const s = state.settings;
  $('#ai-state').textContent = aiScorer() === 'builtin' ? 'Using the built-in algorithm.'
    : s.ai_ready ? `Currently saved: ${s.ai_status}.` : `Not ready yet: ${s.ai_status}. Until it's set up, the built-in algorithm is used.`;
}

for (const r of document.querySelectorAll('input[name=scorer]')) r.onchange = showScorer;
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
  $('#gpu-note').textContent = s.gpu_note || '';
  $('#gpu-note').hidden = !s.gpu_note;
  drafts = {};
  $('#ai-provider').replaceChildren(...Object.entries(s.providers).map(([id, p]) => h('option', { value: id }, p.label)));
  $('#ai-provider').value = s.ai_provider;
  document.querySelector(`input[name=scorer][value=${s.scorer}]`).checked = true;
  showProvider(s.ai_provider);
  showScorer();
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
  stashDraft();
  const chosen = $('#ai-provider').value;
  patch.scorer = aiScorer();
  patch.ai_provider = chosen;
  if (patch.scorer === 'ai' && !(drafts[chosen] || {}).model) return toast('Pick a model for the connected AI first (Load models, then choose one).', true);
  try {
    // Keys and models typed for other providers are saved too, then the chosen provider and the rest.
    for (const [p, d] of Object.entries(drafts)) {
      if (p !== chosen && (d.key || d.model)) await api('/api/settings', { body: { ai_provider: p, ai_key: d.key, ai_model: d.model } });
    }
    const mine = drafts[chosen] || {};
    const s = await api('/api/settings', { body: { ...patch, ai_key: mine.key, ai_model: mine.model } });
    fillSettings(s);
    $('#settings').close();
    toast(s.scorer === 'ai' && !s.ai_ready ? `Saved, but the connected AI isn't ready: ${s.ai_status}.` : 'Settings saved', s.scorer === 'ai' && !s.ai_ready);
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
  await loadEffects();
  await refreshJobs();
})();
