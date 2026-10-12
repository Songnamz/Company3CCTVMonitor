'use strict';

const $ = (sel) => document.querySelector(sel);
const PAGE_SIZE = 48;

const state = {
  config: null,
  tab: 'live',
  events: [],          // loaded gallery events, newest first
  hasMore: false,
  loaded: false,       // gallery fetched at least once for the current filter
  loading: false,
  generation: 0,       // bumps on filter change to discard stale responses
  filter: { channel: '', date: '', type: '' },
  schedule: null,      // last saved schedule from the server
  selecting: false,    // Events tab multi-select mode
  selected: new Set(),
  lastSelected: -1,    // index of the last clicked card, for shift-click ranges
  draft: null,         // per-camera settings being edited in the Settings tab
  capture: null,       // latest per-camera capture status
  showBoxes: true,
  lbIndex: -1,
  badge: 0,
  sseOnline: false,
  status: null,
};

/* ---------- helpers ---------- */

let fmtFull, fmtTime;
function initFormatters(tz) {
  const opts = { timeZone: tz, hour12: false };
  fmtFull = new Intl.DateTimeFormat('en-GB', { ...opts, day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  fmtTime = new Intl.DateTimeFormat('en-GB', { ...opts, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
const fullTime = (ts) => fmtFull.format(new Date(ts * 1000));
const shortTime = (ts) => fmtTime.format(new Date(ts * 1000));

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

function imgOrPlaceholder(src, cls = '') {
  if (src) return el('img', { src, alt: '', loading: 'lazy', class: cls });
  return el('div', { class: `noimg ${cls}` }, 'No image');
}

/** fetch() that sends the user back to the login page when the session has expired. */
async function api(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) {
    location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
    throw new Error('login required');
  }
  return res;
}

/** Local day 'YYYY-MM-DD' (in the server's timezone) → [start, end) epoch seconds. */
function dayRange(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const start = Date.UTC(y, m - 1, d) / 1000 - state.config.utc_offset;
  return [start, start + 86400];
}

/* ---------- tabs ---------- */

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${tab}`));
  if (tab === 'live') {
    layoutGrid();
    startGrid();
  } else {
    stopGrid();
    if (tab === 'events') {
      setBadge(0);
      if (!state.loaded) loadEvents(true);
    } else if (tab === 'settings') {
      loadSchedule();
    }
  }
}

function setBadge(n) {
  state.badge = n;
  const b = $('#badge');
  b.textContent = n > 99 ? '99+' : String(n);
  b.hidden = n === 0;
}

/* ---------- live view ---------- */

let playerReady = null;

function whenPlayerReady() {
  if (!playerReady) {
    playerReady = Promise.race([
      customElements.whenDefined('video-stream'),
      new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 15000)),
    ]).catch((err) => {
      $('#live-error').hidden = false;
      playerReady = null;
      throw err;
    });
  }
  return playerReady;
}

function makeStream(name) {
  const v = document.createElement('video-stream');
  v.dataset.src = name;
  v.dataset.started = Date.now();
  v.DISCONNECT_TIMEOUT = 0; // tear down immediately when removed from the DOM
  v.mode = 'mse';           // WebRTC/UDP doesn't traverse the Cloudflare tunnel
  v.src = `/stream/api/ws?src=${encodeURIComponent(name)}`;
  return v;
}

/*
 * Stream watchdog. go2rtc reports a failed camera connection (e.g. "wrong response on
 * DESCRIBE" while the XVR restarts its encoders) as an error message but keeps the
 * websocket open, so the player never retries by itself. Replace any stream that shows an
 * error or whose video has not advanced for STALL_MS with a fresh one.
 */
const RETRY_MS = 10000;
const STALL_MS = 20000;
setInterval(() => {
  const now = Date.now();
  document.querySelectorAll('video-stream').forEach((v) => {
    if (!v.isConnected || document.hidden) return;
    const age = now - Number(v.dataset.started);
    const mode = v.querySelector('.mode')?.textContent;
    const video = v.querySelector('video');
    if (video) {
      if (video.currentTime !== Number(v.dataset.lastTime)) {
        v.dataset.lastTime = video.currentTime;
        v.dataset.lastProgress = now;
      }
    }
    const stalled = now - Number(v.dataset.lastProgress || v.dataset.started) > STALL_MS;
    if ((mode === 'error' && age > RETRY_MS) || stalled) {
      console.info(`stream ${v.dataset.src}: ${mode === 'error' ? 'error' : 'stalled'}, reconnecting`);
      v.replaceWith(makeStream(v.dataset.src));
    }
  });
}, 2000);

function buildGrid() {
  const grid = $('#grid');
  grid.innerHTML = '';
  for (const ch of state.config.channels) {
    grid.append(el('div', { class: 'tile', 'data-ch': ch.id },
      el('div', { class: 'slot' }),
      el('div', { class: 'label' }, ch.name),
      el('div', { class: 'hit', title: `Open ${ch.name} in HD`, onclick: () => openPlayer(ch) }),
    ));
  }
}

const GRID_GAP = 8;
const VIDEO_ASPECT = 16 / 9;

/** Pick the columns × rows that give the biggest 16:9 tiles inside the live view, and size the grid to it. */
function layoutGrid() {
  const view = $('#view-live');
  const grid = $('#grid');
  const n = grid.children.length;
  const W = view.clientWidth, H = view.clientHeight;
  if (!n || !W || !H) return;
  let best = null;
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    const cellW = (W - GRID_GAP * (cols - 1)) / cols;
    const cellH = (H - GRID_GAP * (rows - 1)) / rows;
    const tileW = Math.min(cellW, cellH * VIDEO_ASPECT);
    if (!best || tileW > best.tileW) best = { cols, rows, tileW };
  }
  const tileW = Math.floor(best.tileW), tileH = Math.floor(best.tileW / VIDEO_ASPECT);
  Object.assign(grid.style, {
    gridTemplateColumns: `repeat(${best.cols}, ${tileW}px)`,
    gridTemplateRows: `repeat(${best.rows}, ${tileH}px)`,
    width: `${best.cols * tileW + GRID_GAP * (best.cols - 1)}px`,
  });
}

async function startGrid() {
  if (state.tab !== 'live' || !$('#player').hidden) return;
  try { await whenPlayerReady(); } catch { return; }
  if (state.tab !== 'live' || !$('#player').hidden) return;
  for (const ch of state.config.channels) {
    const slot = document.querySelector(`.tile[data-ch="${ch.id}"] .slot`);
    if (slot && !slot.firstChild) slot.append(makeStream(ch.stream));
  }
}

/** Close a player's websocket right away (before removing it), so no video data arrives after teardown. */
function closeStreams(container) {
  container.querySelectorAll('video-stream').forEach((v) => {
    try { v.ondisconnect(); } catch { /* already closed */ }
    v.remove();
  });
}

function stopGrid() {
  closeStreams($('#grid'));
}

async function openPlayer(ch) {
  stopGrid();
  $('#player-title').textContent = `${ch.name} · HD`;
  $('#player').hidden = false;
  try { await whenPlayerReady(); } catch { return; }
  if ($('#player').hidden) return;
  const body = $('#player-body');
  closeStreams(body);
  body.append(makeStream(ch.stream_hd));
}

function closePlayer() {
  closeStreams($('#player-body'));
  $('#player').hidden = true;
  startGrid();
}

function flashTile(channel) {
  const tile = document.querySelector(`.tile[data-ch="${channel}"]`);
  if (!tile) return;
  tile.classList.remove('flash');
  void tile.offsetWidth; // restart the animation
  tile.classList.add('flash');
  clearTimeout(tile._flashTimer);
  tile._flashTimer = setTimeout(() => tile.classList.remove('flash'), 3200);
}

/* ---------- events gallery ---------- */

function channelName(id) {
  const ch = state.config.channels.find((c) => c.id === id);
  return ch ? ch.name : `CH${id}`;
}

function eventLabel(ev) {
  if (ev.shirtless) return 'No shirt';
  if (ev.code === 'Person') return ev.persons > 1 ? `${ev.persons} people` : 'Person';
  if (ev.code === 'FaceDetection') return 'Face';
  return ev.code;
}

function card(ev, isNew = false) {
  const cls = `card${isNew ? ' new' : ''}${ev.shirtless ? ' shirtless' : ''}`;
  return el('button', { class: cls + (state.selected.has(ev.id) ? ' selected' : ''), 'data-id': ev.id,
    onclick: (e) => (state.selecting ? toggleSelect(ev.id, e.shiftKey) : openLightbox(ev.id)) },
    imgOrPlaceholder(ev.thumb),
    ...(ev.shirtless ? [el('span', { class: 'flag' }, 'NO SHIRT')] : []),
    ...(ev.persons > 1 ? [el('span', { class: 'people' }, `👤 ${ev.persons}`)] : []),
    el('div', { class: 'meta' },
      el('span', { class: 'ch' }, channelName(ev.channel)),
      el('span', { class: 't' }, shortTime(ev.ts))),
  );
}

function renderGalleryFooter() {
  $('#more').hidden = !state.hasMore;
  $('#empty').hidden = !(state.loaded && state.events.length === 0);
}

async function loadEvents(reset) {
  if (state.loading && !reset) return;
  const gen = reset ? ++state.generation : state.generation;
  state.loading = true;
  const p = new URLSearchParams({ limit: PAGE_SIZE });
  if (state.filter.channel) p.set('channel', state.filter.channel);
  if (state.filter.type === 'shirtless') p.set('shirtless', 'true');
  if (state.filter.type === 'white' || state.filter.type === 'black') p.set('shirt', state.filter.type);
  if (state.filter.date) {
    const [s, e] = dayRange(state.filter.date);
    p.set('start', s);
    p.set('end', e);
  }
  if (!reset && state.events.length) p.set('before', state.events[state.events.length - 1].id);
  try {
    const res = await api(`/api/events?${p}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (gen !== state.generation) return;
    const gallery = $('#gallery');
    if (reset) {
      state.events = [];
      gallery.innerHTML = '';
      // Never keep selections the user can no longer see.
      state.selected.clear();
      state.lastSelected = -1;
      renderSelection();
    }
    const known = new Set(state.events.map((e) => e.id));
    for (const ev of data.events) {
      if (known.has(ev.id)) continue;
      state.events.push(ev);
      gallery.append(card(ev));
    }
    state.hasMore = data.has_more;
    state.loaded = true;
    renderGalleryFooter();
  } catch (err) {
    console.error('loading events failed', err);
  } finally {
    if (gen === state.generation) state.loading = false;
  }
}

function matchesFilter(ev) {
  if (state.filter.channel && Number(state.filter.channel) !== ev.channel) return false;
  if (state.filter.type === 'shirtless' && !ev.shirtless) return false;
  if ((state.filter.type === 'white' || state.filter.type === 'black')
      && !(ev.detections || []).some((d) => d.shirt === state.filter.type)) return false;
  if (state.filter.date) {
    const [s, e] = dayRange(state.filter.date);
    if (ev.ts < s || ev.ts >= e) return false;
  }
  return true;
}

function prependEvent(ev) {
  if (state.events.some((e) => e.id === ev.id)) return;
  state.events.unshift(ev);
  $('#gallery').prepend(card(ev, true));
  if (state.lbIndex >= 0) state.lbIndex += 1; // keep the lightbox on the same item
  renderGalleryFooter();
}

function updateEvent(ev) {
  const i = state.events.findIndex((e) => e.id === ev.id);
  if (i < 0) return;
  if (!matchesFilter(ev)) { removeEvent(ev.id); return; }
  state.events[i] = ev;
  document.querySelector(`.card[data-id="${ev.id}"]`)?.replaceWith(card(ev));
  if (state.lbIndex === i) showLightbox(i);
}

function removeEvent(id) {
  if (state.selected.delete(id)) renderSelection();
  const i = state.events.findIndex((e) => e.id === id);
  if (i < 0) return;
  state.events.splice(i, 1);
  document.querySelector(`.card[data-id="${id}"]`)?.remove();
  renderGalleryFooter();
  if (state.lbIndex < 0) return;
  if (i < state.lbIndex) state.lbIndex -= 1;
  else if (i === state.lbIndex) {
    if (!state.events.length) closeLightbox();
    else showLightbox(Math.min(i, state.events.length - 1));
  }
}

/* ---------- multi-select ---------- */

function setSelecting(on) {
  state.selecting = on;
  state.selected.clear();
  state.lastSelected = -1;
  $('#gallery').classList.toggle('selecting', on);
  document.querySelectorAll('.card.selected').forEach((c) => c.classList.remove('selected'));
  $('#sel-bar').hidden = !on;
  $('#sel-toggle').hidden = on;
  renderSelection();
}

/** Download a zip of events via a hidden form, so the browser streams it to disk itself. */
function downloadZip(fields) {
  const form = el('form', { method: 'POST', action: '/api/events/export', target: 'dl-frame' });
  for (const [k, v] of Object.entries(fields)) form.append(el('input', { type: 'hidden', name: k, value: v }));
  document.body.append(form);
  form.submit();
  form.remove();
}

function renderSelection() {
  const n = state.selected.size;
  $('#sel-zip').disabled = n === 0;
  $('#sel-count').textContent = `${n} selected`;
  $('#sel-delete').disabled = n === 0;
  $('#sel-delete').textContent = n ? `Delete ${n}` : 'Delete';
}

function setSelected(id, on) {
  if (on) state.selected.add(id); else state.selected.delete(id);
  document.querySelector(`.card[data-id="${id}"]`)?.classList.toggle('selected', on);
}

function toggleSelect(id, range) {
  const i = state.events.findIndex((e) => e.id === id);
  if (range && state.lastSelected >= 0 && i >= 0) {
    // Shift-click: select everything between the last clicked card and this one.
    const [a, b] = [Math.min(i, state.lastSelected), Math.max(i, state.lastSelected)];
    for (let k = a; k <= b; k++) setSelected(state.events[k].id, true);
  } else {
    setSelected(id, !state.selected.has(id));
  }
  state.lastSelected = i;
  renderSelection();
}

async function deleteSelected() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} event${ids.length > 1 ? 's' : ''} and their images? This cannot be undone.`)) return;
  const btn = $('#sel-delete');
  btn.disabled = true;
  try {
    for (let k = 0; k < ids.length; k += 1000) {
      const res = await api('/api/events/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: ids.slice(k, k + 1000) }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      (await res.json()).deleted.forEach(removeEvent);
    }
    setSelecting(false);
    if (state.events.length === 0 && state.hasMore) loadEvents(true);
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
    renderSelection();
  }
}

/* ---------- date/time range: summary, zip, delete ---------- */

const pad2 = (n) => String(n).padStart(2, '0');
/** epoch seconds -> 'YYYY-MM-DDTHH:MM' in the server's timezone (for datetime-local inputs). */
function toLocalInput(ts) {
  const d = new Date((ts + state.config.utc_offset) * 1000);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}T${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}
function fromLocalInput(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v || '');
  if (!m) return null;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 1000 - state.config.utc_offset;
}

function rangeFilters() {
  const f = { start: fromLocalInput($('#r-start').value), end: fromLocalInput($('#r-end').value) };
  if ($('#r-channel').value) f.channel = $('#r-channel').value;
  if ($('#r-type').value) f.type = $('#r-type').value;
  return f;
}

function rangeDescription(f) {
  const parts = [`${fullTime(f.start)} → ${fullTime(f.end)}`];
  if (f.channel) parts.push(channelName(Number(f.channel)));
  if (f.type) parts.push($('#r-type').selectedOptions[0].textContent.toLowerCase());
  return parts.join(', ');
}

let rangeSeq = 0;
async function updateRangeSummary() {
  const f = rangeFilters();
  const out = $('#r-summary');
  const buttons = ['#r-show', '#r-zip', '#r-delete'].map((s) => $(s));
  buttons.forEach((b) => { b.disabled = true; });
  out.classList.remove('err');
  if (f.start == null || f.end == null || f.end <= f.start) {
    out.textContent = 'Choose a start and an end time (end after start).';
    out.classList.add('err');
    return;
  }
  const seq = ++rangeSeq;
  out.textContent = 'Counting…';
  const res = await api(`/api/events/summary?${new URLSearchParams(f)}`);
  const s = await res.json();
  if (seq !== rangeSeq) return;
  if (!res.ok) { out.textContent = s.detail; out.classList.add('err'); return; }
  state.rangeCount = s.count;
  out.textContent = s.count === 0 ? 'No events in this range.'
    : `${s.count} event${s.count > 1 ? 's' : ''} · ${(s.bytes / 1048576).toFixed(1)} MB · ${shortTime(s.first)} – ${shortTime(s.last)}`;
  buttons.forEach((b) => { b.disabled = s.count === 0; });
  $('#r-delete').textContent = s.count ? `Delete ${s.count}` : 'Delete';
}

function openRange() {
  const panel = $('#range-panel');
  panel.hidden = !panel.hidden;
  if (panel.hidden) return;
  // Start from the gallery's current filters: the chosen day, or today so far.
  let start, end;
  if (state.filter.date) [start, end] = dayRange(state.filter.date);
  else {
    const now = Date.now() / 1000;
    start = Math.floor((now + state.config.utc_offset) / 86400) * 86400 - state.config.utc_offset;
    end = Math.ceil(now / 60) * 60;
  }
  $('#r-start').value = toLocalInput(start);
  $('#r-end').value = toLocalInput(end);
  $('#r-channel').value = state.filter.channel;
  $('#r-type').value = state.filter.type;
  updateRangeSummary();
}

async function deleteRange() {
  const f = rangeFilters();
  const n = state.rangeCount;
  if (!confirm(`Delete ${n} event${n > 1 ? 's' : ''} and their images?\n\n${rangeDescription(f)}\n\nThis cannot be undone.`)) return;
  const res = await api('/api/events/delete_range', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(f),
  });
  const data = await res.json();
  if (!res.ok) { alert(`Delete failed: ${data.detail}`); return; }
  data.deleted.forEach(removeEvent);
  $('#r-summary').textContent = `Deleted ${data.deleted.length} events.`;
  updateRangeSummary();
}

/** Show the range in the gallery (the gallery filters by whole days, so use the start day). */
function showRange() {
  const f = rangeFilters();
  $('#f-channel').value = f.channel || '';
  $('#f-type').value = f.type || '';
  $('#f-date').value = toLocalInput(f.start).slice(0, 10);
  state.filter = { channel: f.channel || '', type: f.type || '', date: $('#f-date').value };
  loadEvents(true);
}

/* ---------- lightbox ---------- */

function openLightbox(id) {
  const i = state.events.findIndex((e) => e.id === id);
  if (i < 0) return;
  $('#lightbox').hidden = false;
  showLightbox(i);
}

function showLightbox(i) {
  state.lbIndex = i;
  const ev = state.events[i];
  const img = $('#lb-img');
  setZoom(false);
  $('#lb-stage').hidden = !ev.image;
  $('#lb-noimg').hidden = !!ev.image;
  $('#lb-overlay').innerHTML = '';
  if (ev.image) {
    img.onload = () => drawBoxes(ev, img);
    img.src = ev.image;
    if (img.complete && img.naturalWidth) drawBoxes(ev, img);
  }
  let title = `${channelName(ev.channel)} · ${fullTime(ev.ts)} · ${eventLabel(ev)}`;
  if (ev.shirtless && ev.shirtless_score != null) title += ` (${Math.round(ev.shirtless_score * 100)}%)`;
  if (ev.reviewed) title += ' · reviewed';
  $('#lb-title').textContent = title;
  $('#lb-notshirtless').hidden = !ev.shirtless;
  $('#lb-boxes').hidden = !ev.detections?.length;
  $('#lb-prev').disabled = i === 0;
  $('#lb-next').disabled = i >= state.events.length - 1 && !state.hasMore;
}

function drawBoxes(ev, img) {
  const overlay = $('#lb-overlay');
  overlay.innerHTML = '';
  overlay.classList.toggle('hidden', !state.showBoxes);
  if (!ev.detections || !img.naturalWidth) return;
  const W = img.naturalWidth, H = img.naturalHeight;
  for (const d of ev.detections) {
    const [x1, y1, x2, y2] = d.box;
    const bad = d.shirtless && !ev.reviewed;
    let label;
    if (d.shirtless_score == null) label = 'person';
    else if (bad) label = `no shirt ${Math.round(d.shirtless_score * 100)}%`;
    else if (d.shirtless) label = 'shirt (reviewed)';
    else if (d.shirt) label = `${d.shirt} shirt`;
    else label = `shirt ${Math.round((1 - d.shirtless_score) * 100)}%`;
    const box = el('div', { class: `lb-box${bad ? ' bad' : ''}` }, el('span', {}, label));
    Object.assign(box.style, {
      left: `${(x1 / W) * 100}%`, top: `${(y1 / H) * 100}%`,
      width: `${((x2 - x1) / W) * 100}%`, height: `${((y2 - y1) / H) * 100}%`,
    });
    overlay.append(box);
  }
}

/* Click/tap the lightbox image to zoom 2.5x at that point; move to pan; click again to zoom out. */
const ZOOM = 2.5;
function stagePoint(e) {
  const stage = $('#lb-stage');
  const parent = stage.parentElement.getBoundingClientRect();
  const p = e.touches ? e.touches[0] : e;
  const x = (p.clientX - parent.left - stage.offsetLeft) / stage.offsetWidth;
  const y = (p.clientY - parent.top - stage.offsetTop) / stage.offsetHeight;
  return [Math.min(Math.max(x, 0), 1) * 100, Math.min(Math.max(y, 0), 1) * 100];
}
function setZoom(on, e) {
  const stage = $('#lb-stage');
  state.zoomed = on;
  stage.classList.toggle('zoomed', on);
  if (on) {
    const [x, y] = stagePoint(e);
    stage.style.transformOrigin = `${x}% ${y}%`;
    stage.style.transform = `scale(${ZOOM})`;
  } else {
    stage.style.transform = '';
  }
}
function panZoom(e) {
  if (!state.zoomed) return;
  const [x, y] = stagePoint(e);
  $('#lb-stage').style.transformOrigin = `${x}% ${y}%`;
}

function toggleBoxes() {
  state.showBoxes = !state.showBoxes;
  $('#lb-overlay').classList.toggle('hidden', !state.showBoxes);
}

async function lbNotShirtless() {
  const ev = state.events[state.lbIndex];
  if (!ev) return;
  const res = await api(`/api/events/${ev.id}/not-shirtless`, { method: 'POST' });
  if (res.ok) updateEvent(await res.json());
}

function closeLightbox() {
  setZoom(false);
  $('#lightbox').hidden = true;
  $('#lb-img').removeAttribute('src');
  state.lbIndex = -1;
}

async function lbStep(delta) {
  if (state.lbIndex < 0) return;
  let i = state.lbIndex + delta;
  if (i < 0) return;
  if (i >= state.events.length) {
    if (!state.hasMore) return;
    await loadEvents(false);
    if (i >= state.events.length) return;
  }
  showLightbox(i);
}

async function lbDelete() {
  const ev = state.events[state.lbIndex];
  if (!ev || !confirm(`Delete this ${channelName(ev.channel)} event from ${fullTime(ev.ts)}?`)) return;
  const res = await api(`/api/events/${ev.id}`, { method: 'DELETE' });
  if (res.ok || res.status === 404) removeEvent(ev.id);
  else alert(`Delete failed (HTTP ${res.status})`);
}

/* ---------- toasts ---------- */

function toast(ev) {
  const box = $('#toasts');
  const t = el('div', {
    class: ev.shirtless ? 'toast alert' : 'toast',
    onclick: () => {
      t.remove();
      setTab('events');
      if (state.events.some((e) => e.id === ev.id)) openLightbox(ev.id);
    },
  },
  imgOrPlaceholder(ev.thumb),
  el('div', {},
    el('div', { class: 'tt' }, `${eventLabel(ev)} · ${channelName(ev.channel)}`),
    el('div', { class: 'ts' }, fullTime(ev.ts))));
  box.prepend(t);
  while (box.children.length > 4) box.lastChild.remove();
  const ttl = ev.shirtless ? 20000 : 6000;
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, ttl);
}

/* ---------- status + SSE ---------- */

function renderStatus() {
  const box = $('#status');
  const s = state.status;
  box.classList.remove('ok', 'err', 'offline');
  if (!state.sseOnline) {
    box.classList.add('offline');
    box.title = 'Lost connection to the CCTV server – reconnecting…';
  } else if (s && s.connected) {
    box.classList.add('ok');
    box.title = `Connected to XVR event stream (${s.event_codes.join(', ')})`;
  } else {
    box.classList.add('err');
    box.title = `XVR event stream disconnected${s && s.last_error ? `: ${s.last_error}` : ''}`;
  }
  if (s) {
    $('#today').textContent = s.today_count;
    const sh = $('#today-shirtless');
    sh.hidden = !s.today_shirtless;
    sh.textContent = `${s.today_shirtless} no shirt`;
    const ai = $('#ai-status');
    const d = s.detector || {};
    ai.className = `ai-chip ${d.online ? 'ok' : 'err'}`;
    const chans = Object.entries(d.channels || {})
      .map(([c, v]) => `CH${c}: ${v.idle ? 'not analysed' : v.error ? `error – ${v.error}` : 'ok'}`).join('\n');
    renderCapture(s.capture);
    ai.title = d.online
      ? `AI detector running${d.inference_ms ? ` (${d.inference_ms} ms/frame)` : ''}\n${chans}`
      : 'AI detector offline – people / no-shirt detection is not running';
  }
}

/* ---------- capture schedule (per camera) ---------- */

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const FOREVER = 10 * 365 * 86400;
const clone = (o) => JSON.parse(JSON.stringify(o));

function reasonText(cam) {
  if (cam.reason === 'paused') {
    return cam.paused_until - Date.now() / 1000 > FOREVER / 2 ? 'paused' : `paused until ${shortTime(cam.paused_until)}`;
  }
  return cam.reason === 'outside schedule' ? 'outside its schedule' : cam.reason;
}

function renderCapture(cap) {
  if (!cap) return;
  state.capture = cap;
  const all = state.config.channels.map((c) => c.id);
  const off = all.filter((c) => !cap.cameras[c]?.active);
  const chip = $('#capture-chip');
  chip.hidden = off.length === 0;
  chip.textContent = off.length === all.length ? 'Capture off' : `Capture off: ${off.map(channelName).join(', ')}`;
  chip.title = off.map((c) => `${channelName(c)}: ${reasonText(cap.cameras[c])}`).join('\n') + '\nSee the Settings tab.';
  document.querySelectorAll('.tile').forEach((t) => {
    const ch = Number(t.dataset.ch);
    let badge = t.querySelector('.nocap');
    if (!off.includes(ch)) { badge?.remove(); return; }
    if (!badge) t.append(badge = el('div', { class: 'nocap' }));
    badge.textContent = `not capturing · ${reasonText(cap.cameras[ch])}`;
  });
  const st = $('#cap-state');
  st.classList.toggle('off', off.length > 0);
  st.textContent = off.length === 0 ? 'Capturing on all cameras.'
    : off.length === all.length ? 'Not capturing on any camera.'
    : `Not capturing on ${off.map(channelName).join(', ')}.`;
  // Per-camera status lines in the Settings tab
  for (const c of all) {
    const pill = document.querySelector(`.cam-panel[data-ch="${c}"] .pill`);
    if (!pill) continue;
    const cam = cap.cameras[c];
    pill.classList.toggle('off', !cam.active);
    pill.textContent = cam.active ? (cam.reason === 'always' ? 'Capturing 24/7' : 'Capturing (in schedule)')
      : `Not capturing · ${reasonText(cam)}`;
    document.querySelector(`.cam-panel[data-ch="${c}"] .resume`).hidden = !cam.paused_until;
  }
}

async function loadSchedule() {
  const sch = await (await api('/api/schedule')).json();
  state.schedule = sch;
  state.draft = clone(sch.cameras);
  renderSchedule();
  renderCapture(sch.capture);
  setSchMsg('');
}

function setSchMsg(text, err = false) {
  const m = $('#sch-msg');
  m.textContent = text;
  m.classList.toggle('err', err);
}

function changed() {
  setSchMsg('Unsaved changes');
}

const DETECT_ITEMS = [
  ['person', 'Person', 'anyone, including people too far away to tell their clothing', null],
  ['white', 'White shirt', '', '#f2f2f2'],
  ['black', 'Black shirt', '', '#111'],
  ['none', 'No shirt', 'also turns on the red no-shirt alert', '#d9a07a'],
];

function detectEditor(cam) {
  const box = el('div', { class: 'detect' });
  for (const [key, label, title, colour] of DETECT_ITEMS) {
    const input = el('input', { type: 'checkbox' });
    input.checked = cam.detect[key];
    input.addEventListener('change', () => { cam.detect[key] = input.checked; renderSchedule(); changed(); });
    // White/black are already covered while "Person" is ticked.
    const implied = cam.detect.person && (key === 'white' || key === 'black');
    box.append(el('label', { class: implied ? 'implied' : '', title: implied ? 'Included in Person' : title },
      input, ...(colour ? [el('span', { class: 'swatch', style: `background:${colour}` })] : []), label));
  }
  const parts = [];
  const any = Object.values(cam.detect).some(Boolean);
  if (!any) parts.push(el('span', { class: 'warn-text' }, 'Nothing ticked: this camera will not capture anything.'));
  else if (cam.detect.person) parts.push('Captures everyone' + (cam.detect.none ? ' and alerts on no shirt.' : ' (no-shirt alerts off).'));
  else {
    const kinds = DETECT_ITEMS.filter(([k]) => k !== 'person' && cam.detect[k]).map(([, l]) => l.toLowerCase());
    parts.push(`Captures only people with: ${kinds.join(', ')}. People too far away to judge clothing are skipped.`);
  }
  return el('div', {}, box, el('p', { class: 'hint' }, ...parts));
}

function ruleEditor(cam, rule, i) {
  const days = el('div', { class: 'rule-line' }, el('span', { class: 'lbl' }, 'Days'),
    ...DAYS.map((d, n) => el('button', {
      class: `toggle${rule.days.includes(n) ? ' on' : ''}`,
      onclick: () => {
        const at = rule.days.indexOf(n);
        if (at >= 0) rule.days.splice(at, 1); else rule.days.push(n);
        renderSchedule(); changed();
      },
    }, d)));
  const time = (key) => {
    const input = el('input', { type: 'time', value: rule[key], step: 60 });
    input.addEventListener('change', () => { rule[key] = input.value; changed(); });
    return input;
  };
  return el('div', { class: 'rule' }, days,
    el('div', { class: 'rule-line' }, el('span', { class: 'lbl' }, 'Time'), time('start'), el('span', {}, '→'), time('end'),
      el('button', { class: 'btn ghost remove', onclick: () => { cam.rules.splice(i, 1); renderSchedule(); changed(); } }, 'Remove')));
}

/* ---------- detection areas (per camera) ---------- */

const SVGNS = 'http://www.w3.org/2000/svg';
const frames = {};      // channel -> Promise of an object URL of a current frame
const zoneDrawing = {}; // channel -> points of the area being drawn (survives re-renders)

function svgEl(tag, attrs = {}) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function loadFrame(ch, refresh = false) {
  if (!frames[ch] || refresh) {
    const old = frames[ch];
    frames[ch] = api(`/api/cameras/${ch}/frame.jpg`).then(async (res) => {
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      return URL.createObjectURL(await res.blob());
    });
    frames[ch].catch(() => { delete frames[ch]; });
    old?.then((url) => URL.revokeObjectURL(url), () => {});
  }
  return frames[ch];
}

/** Draw polygons on a still frame; only people standing (feet) inside them are detected. */
function zoneEditor(ch, cam) {
  let W = 1920, H = 1080;
  let drag = null; // {zone, point} vertex being moved
  const img = el('img', { alt: '', class: 'zone-img' });
  const status = el('div', { class: 'zone-status' }, 'Loading picture…');
  const overlay = svgEl('svg', { class: 'zone-svg', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none' });
  const stage = el('div', { class: 'zone-stage' }, img, overlay, status);
  const tools = el('div', { class: 'row zone-tools' });
  const list = el('div', { class: 'zone-list' });

  const showFrame = (refresh) => {
    status.hidden = false;
    status.textContent = 'Loading picture…';
    loadFrame(ch, refresh).then((url) => { img.src = url; }, (err) => { status.textContent = `No picture: ${err.message}`; });
  };
  img.addEventListener('load', () => {
    status.hidden = true;
    W = img.naturalWidth; H = img.naturalHeight;
    overlay.setAttribute('viewBox', `0 0 ${W} ${H}`);
    stage.style.aspectRatio = `${W} / ${H}`;
    render();
  });

  const toPoint = (e) => {
    const r = overlay.getBoundingClientRect();
    const c = (v) => Math.round(Math.min(Math.max(v, 0), 1) * 10000) / 10000;
    return [c((e.clientX - r.left) / r.width), c((e.clientY - r.top) / r.height)];
  };
  const pts = (poly) => poly.map(([x, y]) => `${x * W},${y * H}`).join(' ');

  function render() {
    const drawing = zoneDrawing[ch];
    const r = W / Math.max(overlay.getBoundingClientRect().width, 1); // image px per screen px
    overlay.innerHTML = '';
    overlay.classList.toggle('drawing', !!drawing);
    if (cam.zones.length) {
      // Darken everything outside the areas: that part of the view is ignored.
      const mask = svgEl('mask', { id: `zmask-${ch}` });
      mask.append(svgEl('rect', { width: W, height: H, fill: '#fff' }));
      for (const z of cam.zones) mask.append(svgEl('polygon', { points: pts(z), fill: '#000' }));
      overlay.append(svgEl('defs'), svgEl('rect', { width: W, height: H, class: 'zone-shade', mask: `url(#zmask-${ch})` }));
      overlay.firstChild.append(mask);
    }
    cam.zones.forEach((z, zi) => {
      overlay.append(svgEl('polygon', { points: pts(z), class: 'zone-poly' }));
      const [cx, cy] = z.reduce(([a, b], [x, y]) => [a + x / z.length, b + y / z.length], [0, 0]);
      const label = svgEl('text', { x: cx * W, y: cy * H, class: 'zone-label', 'font-size': 16 * r });
      label.textContent = `Area ${zi + 1}`;
      overlay.append(label);
      z.forEach(([x, y], pi) => overlay.append(svgEl('circle', {
        cx: x * W, cy: y * H, r: 7 * r, class: 'zone-handle', 'data-z': zi, 'data-p': pi,
      })));
    });
    if (drawing) {
      overlay.append(svgEl('polyline', { points: pts(drawing), class: 'zone-draft' }));
      drawing.forEach(([x, y], i) => overlay.append(svgEl('circle', {
        cx: x * W, cy: y * H, r: (i === 0 && drawing.length >= 3 ? 10 : 6) * r, class: `zone-dot${i === 0 ? ' first' : ''}`,
      })));
    }
    renderTools();
  }

  function renderTools() {
    const drawing = zoneDrawing[ch];
    tools.innerHTML = '';
    list.innerHTML = '';
    if (drawing) {
      tools.append(
        el('button', { class: 'btn primary', ...(drawing.length < 3 ? { disabled: '' } : {}), onclick: finish }, 'Finish area'),
        el('button', { class: 'btn', ...(drawing.length ? {} : { disabled: '' }), onclick: () => { drawing.pop(); render(); } }, 'Undo point'),
        el('button', { class: 'btn ghost', onclick: () => { delete zoneDrawing[ch]; render(); } }, 'Cancel'),
        el('span', { class: 'hint' }, drawing.length < 3
          ? 'Click/tap the picture to add corners.' : 'Tap the first corner or press Finish to close the area.'));
      return;
    }
    tools.append(
      el('button', { class: 'btn', onclick: () => { zoneDrawing[ch] = []; render(); } },
        cam.zones.length ? '+ Draw another area' : 'Draw area'),
      el('button', { class: 'btn ghost', onclick: () => showFrame(true) }, 'Refresh picture'));
    if (cam.zones.length) {
      tools.append(el('button', { class: 'btn ghost', onclick: () => { cam.zones = []; render(); changed(); } }, 'Clear (whole view)'));
      cam.zones.forEach((z, zi) => list.append(el('div', { class: 'zone-item' },
        `Area ${zi + 1} · ${z.length} corners`,
        el('button', { class: 'btn ghost', onclick: () => { cam.zones.splice(zi, 1); render(); changed(); } }, 'Delete'))));
    }
  }

  function finish() {
    const drawing = zoneDrawing[ch];
    if (!drawing || drawing.length < 3) return;
    cam.zones.push(drawing);
    delete zoneDrawing[ch];
    render(); changed();
  }

  overlay.addEventListener('pointerdown', (e) => {
    const drawing = zoneDrawing[ch];
    const h = e.target.closest('.zone-handle');
    if (h && !drawing) {
      drag = { zone: Number(h.dataset.z), point: Number(h.dataset.p) };
      overlay.setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    if (!drawing) return;
    const p = toPoint(e);
    const first = drawing[0];
    const r = overlay.getBoundingClientRect();
    if (first && drawing.length >= 3 && Math.hypot((p[0] - first[0]) * r.width, (p[1] - first[1]) * r.height) < 14) {
      finish();
      return;
    }
    drawing.push(p);
    render();
  });
  overlay.addEventListener('pointermove', (e) => {
    if (!drag) return;
    cam.zones[drag.zone][drag.point] = toPoint(e);
    render();
  });
  const endDrag = () => { if (drag) { drag = null; changed(); } };
  overlay.addEventListener('pointerup', endDrag);
  overlay.addEventListener('pointercancel', endDrag);
  new ResizeObserver(() => render()).observe(stage);

  showFrame(false);
  render();
  return el('div', {}, stage, tools, list,
    el('p', { class: 'hint' }, cam.zones.length
      ? 'Only people with at least half of their body inside the areas are detected. The darkened part is ignored. Drag a corner to adjust.'
      : 'No areas: the whole view is used. Draw an area around where people appear, e.g. the stairs or a doorway, to detect only them.'));
}

function renderSchedule() {
  const box = $('#cam-scheds');
  box.innerHTML = '';
  for (const ch of state.config.channels) {
    const cam = state.draft[ch.id];
    const enabled = el('input', { type: 'checkbox' });
    enabled.checked = cam.enabled;
    enabled.addEventListener('change', () => { cam.enabled = enabled.checked; renderSchedule(); changed(); });
    const rules = el('div', { class: `rules${cam.enabled ? '' : ' disabled'}` });
    if (!cam.rules.length) rules.append(el('p', { class: 'hint' }, 'No time ranges yet.'));
    cam.rules.forEach((r, i) => rules.append(ruleEditor(cam, r, i)));
    box.append(el('div', { class: 'panel cam-panel', 'data-ch': ch.id },
      el('div', { class: 'cam-head' }, el('h2', {}, ch.name), el('span', { class: 'pill' }, '…')),
      el('h3', {}, 'Detect'),
      detectEditor(cam),
      el('h3', {}, 'Detection area'),
      zoneEditor(ch.id, cam),
      el('h3', {}, 'When'),
      el('div', { class: 'row' },
        el('button', { class: 'btn', onclick: () => pauseCameras([ch.id], 3600) }, 'Pause 1 hour'),
        el('button', { class: 'btn', onclick: () => pauseCameras([ch.id], 28800) }, 'Pause 8 hours'),
        el('button', { class: 'btn', onclick: () => pauseCameras([ch.id], FOREVER) }, 'Pause until I resume'),
        el('button', { class: 'btn primary resume', onclick: () => pauseCameras([ch.id], null) }, 'Resume')),
      el('label', { class: 'check' }, enabled, 'Only capture during these times (otherwise 24/7)'),
      rules,
      el('div', { class: 'row' },
        el('button', { class: 'btn ghost', onclick: () => {
          cam.rules.push({ days: [0, 1, 2, 3, 4], start: '06:00', end: '18:00' });
          cam.enabled = true; renderSchedule(); changed();
        } }, '+ Add time range'),
        el('button', { class: 'btn ghost', title: 'Use this camera\'s detect and schedule settings for every camera', onclick: () => {
          for (const other of state.config.channels) {
            if (other.id === ch.id) continue;
            state.draft[other.id].enabled = cam.enabled;
            state.draft[other.id].rules = clone(cam.rules);
            state.draft[other.id].detect = clone(cam.detect);
          }
          renderSchedule(); changed();
        } }, 'Copy to all cameras')),
    ));
  }
  if (state.capture) renderCapture(state.capture);
}

async function putSchedule(cameras) {
  const res = await api('/api/schedule', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cameras }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  state.schedule = data;
  renderCapture(data.capture);
  return data;
}

async function saveSchedule() {
  try {
    await putSchedule(state.draft);
    state.draft = clone(state.schedule.cameras);
    renderSchedule(); // the editors must edit the new draft, not the one just saved
    setSchMsg('Saved ✓');
  } catch (err) {
    setSchMsg(err.message, true);
  }
}

/** Pause/resume takes effect immediately and leaves unsaved schedule edits alone. */
async function pauseCameras(ids, seconds) {
  const cameras = clone(state.schedule.cameras);
  const until = seconds ? Date.now() / 1000 + seconds : null;
  for (const id of ids) {
    cameras[id].paused_until = until;
    state.draft[id].paused_until = until;
  }
  try { await putSchedule(cameras); } catch (err) { setSchMsg(err.message, true); }
}

function onNewEvent(ev) {
  toast(ev);
  flashTile(ev.channel);
  const inGallery = state.loaded && matchesFilter(ev);
  if (inGallery) prependEvent(ev);
  if (state.tab !== 'events' || !inGallery) setBadge(state.badge + 1);
}

function connectSSE() {
  const es = new EventSource('/api/live');
  let wasOffline = false;
  es.addEventListener('open', () => {
    state.sseOnline = true;
    renderStatus();
    // Catch up on anything missed while disconnected.
    if (wasOffline && state.loaded) loadEvents(true);
    wasOffline = false;
  });
  es.addEventListener('error', () => {
    state.sseOnline = false;
    wasOffline = true;
    renderStatus();
    // EventSource gives up for good on a non-200 reply (e.g. expired session): check and reconnect.
    if (es.readyState === EventSource.CLOSED) {
      api('/api/status').then(() => setTimeout(connectSSE, 3000)).catch(() => {});
    }
  });
  es.addEventListener('status', (e) => { state.status = JSON.parse(e.data); renderStatus(); });
  es.addEventListener('event', (e) => onNewEvent(JSON.parse(e.data)));
  es.addEventListener('deleted', (e) => removeEvent(JSON.parse(e.data).id));
  es.addEventListener('updated', (e) => updateEvent(JSON.parse(e.data)));
  es.addEventListener('deleted_many', (e) => JSON.parse(e.data).ids.forEach(removeEvent));
}

/* ---------- init ---------- */

async function init() {
  state.config = await (await api('/api/config')).json();
  initFormatters(state.config.tz);

  const sel = $('#f-channel');
  for (const ch of state.config.channels) sel.append(el('option', { value: ch.id }, ch.name));

  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
  sel.addEventListener('change', () => { state.filter.channel = sel.value; loadEvents(true); });
  $('#f-type').addEventListener('change', (e) => { state.filter.type = e.target.value; loadEvents(true); });
  $('#f-date').addEventListener('change', (e) => { state.filter.date = e.target.value; loadEvents(true); });
  $('#f-clear').addEventListener('click', () => {
    sel.value = '';
    $('#f-date').value = '';
    $('#f-type').value = '';
    state.filter = { channel: '', date: '', type: '' };
    loadEvents(true);
  });
  $('#more').addEventListener('click', () => loadEvents(false));
  $('#sel-toggle').addEventListener('click', () => setSelecting(true));
  $('#sel-done').addEventListener('click', () => setSelecting(false));
  $('#sel-all').addEventListener('click', () => { state.events.forEach((e) => setSelected(e.id, true)); renderSelection(); });
  $('#sel-none').addEventListener('click', () => { [...state.selected].forEach((id) => setSelected(id, false)); renderSelection(); });
  $('#sel-delete').addEventListener('click', deleteSelected);
  $('#sel-zip').addEventListener('click', () => downloadZip({ ids: [...state.selected].join(',') }));
  for (const ch of state.config.channels) $('#r-channel').append(el('option', { value: ch.id }, ch.name));
  $('#range-toggle').addEventListener('click', openRange);
  $('#r-close').addEventListener('click', () => { $('#range-panel').hidden = true; });
  ['#r-start', '#r-end', '#r-channel', '#r-type'].forEach((s) => $(s).addEventListener('change', updateRangeSummary));
  $('#r-zip').addEventListener('click', () => downloadZip(rangeFilters()));
  $('#r-delete').addEventListener('click', deleteRange);
  $('#r-show').addEventListener('click', showRange);

  $('#player-close').addEventListener('click', closePlayer);
  $('#lb-close').addEventListener('click', closeLightbox);
  $('#lb-prev').addEventListener('click', () => lbStep(-1));
  $('#lb-next').addEventListener('click', () => lbStep(1));
  $('#lb-delete').addEventListener('click', lbDelete);
  $('#lb-boxes').addEventListener('click', toggleBoxes);
  const stage = $('#lb-stage');
  stage.addEventListener('click', (e) => setZoom(!state.zoomed, e));
  stage.addEventListener('mousemove', panZoom);
  stage.addEventListener('touchmove', (e) => { if (state.zoomed) { e.preventDefault(); panZoom(e); } }, { passive: false });

  $('#sch-save').addEventListener('click', saveSchedule);
  document.querySelectorAll('[data-pause-all]').forEach((b) => b.addEventListener('click', () => {
    const v = b.dataset.pauseAll;
    const ids = state.config.channels.map((c) => c.id);
    pauseCameras(ids, v === 'resume' ? null : v === 'forever' ? FOREVER : Number(v));
  }));
  $('#lb-notshirtless').addEventListener('click', lbNotShirtless);

  document.addEventListener('keydown', (e) => {
    if (!$('#lightbox').hidden) {
      if (e.key === 'ArrowLeft') lbStep(-1);
      else if (e.key === 'ArrowRight') lbStep(1);
      else if (e.key === 'Escape') closeLightbox();
      else if (e.key === 'Delete') lbDelete();
      else if (e.key === 'b' || e.key === 'B') toggleBoxes();
    } else if (!$('#player').hidden && e.key === 'Escape') {
      closePlayer();
    } else if (state.selecting && e.key === 'Escape') {
      setSelecting(false);
    }
  });

  // Basic swipe for the lightbox on touch screens.
  let touchX = null;
  const lb = $('#lightbox');
  lb.addEventListener('touchstart', (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  lb.addEventListener('touchend', (e) => {
    if (touchX === null || state.zoomed) { touchX = null; return; }
    const dx = e.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(dx) > 60) lbStep(dx > 0 ? -1 : 1);
  });

  buildGrid();
  new ResizeObserver(layoutGrid).observe($('#view-live'));
  renderStatus();
  connectSSE();
  setTab('live');
}

init().catch((err) => {
  console.error(err);
  document.querySelector('main').prepend(el('p', { class: 'notice' }, `Failed to load: ${err.message}`));
});
