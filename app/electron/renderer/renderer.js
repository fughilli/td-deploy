'use strict';
// Renderer: drives the sidecar via window.td, renders progress + log.

const $ = (id) => document.getElementById(id);
const els = {
  pick: $('pick'), toePath: $('toe-path'), watch: $('watch'), deploy: $('deploy'),
  phase: $('phase'), bar: $('bar-fill'), pi: $('pi'), target: $('target'),
  key: $('key'), pickKey: $('pick-key'), flash: $('flash'), log: $('log'),
  dot: $('status-dot'),
};

const STORE = 'td-deploy-settings';
let state = { toe: null, busy: false };

function loadSettings() {
  try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; }
}
function saveSettings() {
  const s = { pi: els.pi.value, target: els.target.value, key: els.key.value, toe: state.toe };
  localStorage.setItem(STORE, JSON.stringify(s));
  return s;
}
function pushSettings() {
  const s = saveSettings();
  window.td.send({ cmd: 'set_settings', settings: {
    pi: s.pi, target: s.target, key: s.key || null,
  }});
}

function log(line, cls) {
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = line + '\n';
  els.log.appendChild(span);
  els.log.scrollTop = els.log.scrollHeight;
}

function setBusy(busy, phase) {
  state.busy = busy;
  els.deploy.disabled = busy || !state.toe;
  els.dot.className = 'dot ' + (busy ? 'busy' : 'idle');
  els.dot.title = phase || (busy ? 'working' : 'idle');
}

function setToe(toe) {
  state.toe = toe;
  els.toePath.textContent = toe || 'No project selected';
  els.toePath.classList.toggle('muted', !toe);
  els.deploy.disabled = !toe || state.busy;
  saveSettings();
}

// --- wire controls ---
els.pick.onclick = async () => {
  const toe = await window.td.pickToe();
  if (toe) { setToe(toe); window.td.send({ cmd: 'pick_toe', toe }); }
};
els.pickKey.onclick = async () => {
  const k = await window.td.pickKey();
  if (k) { els.key.value = k; pushSettings(); }
};
els.deploy.onclick = () => {
  if (state.toe) window.td.send({ cmd: 'deploy', toe: state.toe });
};
els.watch.onchange = () => {
  window.td.send({ cmd: 'watch', enable: els.watch.checked, toe: state.toe });
};
for (const el of [els.pi, els.target, els.key]) el.onchange = pushSettings;

// --- sidecar events -> UI ---
window.td.onEvent((evt) => {
  switch (evt.type) {
    case 'ready':
      pushSettings();
      log('sidecar ready');
      break;
    case 'start':
      setBusy(true, 'start');
      els.log.textContent = '';
      log('deploy ' + evt.toe);
      els.bar.style.width = '0%';
      break;
    case 'progress':
      els.phase.textContent = evt.phase + (evt.message ? ' — ' + evt.message : '');
      els.bar.style.width = Math.round(evt.overall * 100) + '%';
      break;
    case 'log':
      log(evt.line);
      break;
    case 'done':
      setBusy(false);
      els.phase.textContent = 'Live ✓';
      els.bar.style.width = '100%';
      log('live: ' + evt.staging, 'ok');
      break;
    case 'error':
      setBusy(false);
      els.phase.textContent = 'Error';
      log('ERROR: ' + evt.message, 'err');
      break;
    case 'watch':
      els.watch.checked = !!evt.enabled;
      log('watch ' + (evt.enabled ? 'on' : 'off'));
      break;
    case 'disks':
      renderDisks(evt.disks || []);
      break;
    case 'flash_start':
      log('flash ' + evt.disk.name + ' <- ' + evt.tag);
      break;
    case 'flash_progress':
      fm.phase.textContent = (evt.stage === 'download' ? 'Downloading image' : 'Writing SD')
        + (evt.message ? ' — ' + evt.message : '');
      fm.fill.style.width = Math.round(evt.frac * 100) + '%';
      break;
    case 'flash_done':
      flashing = false;
      fm.cancel.disabled = false;
      fm.phase.textContent = 'Done ✓ — you can remove the card';
      fm.fill.style.width = '100%';
      log('flashed ' + evt.disk.name, 'ok');
      break;
    case 'flash_error':
      flashing = false;
      fm.cancel.disabled = false;
      fm.phase.textContent = 'Error';
      log('FLASH ERROR: ' + evt.message, 'err');
      break;
  }
});

// --- SD flashing modal ---
const fm = {
  modal: $('flash-modal'), tag: $('flash-tag'), refresh: $('flash-refresh'),
  list: $('disk-list'), go: $('flash-go'), cancel: $('flash-cancel'),
  progress: $('flash-progress'), phase: $('flash-phase'), fill: $('flash-fill'),
};
let selectedDisk = null;
let flashing = false;

function openFlash() {
  selectedDisk = null;
  flashing = false;
  fm.go.disabled = true;
  fm.progress.classList.add('hidden');
  fm.list.innerHTML = '<li class="muted">Scanning…</li>';
  fm.modal.classList.remove('hidden');
  window.td.send({ cmd: 'list_disks' });
}
function closeFlash() { if (!flashing) fm.modal.classList.add('hidden'); }

function renderDisks(disks) {
  fm.list.innerHTML = '';
  if (!disks.length) {
    fm.list.innerHTML = '<li class="muted">No removable disks found. Insert an SD card and Refresh.</li>';
    return;
  }
  for (const d of disks) {
    const li = document.createElement('li');
    li.textContent = `${d.name} — ${d.size_gb} GB${d.bus ? ' (' + d.bus + ')' : ''}`;
    li.onclick = () => {
      selectedDisk = d;
      fm.go.disabled = false;
      for (const c of fm.list.children) c.classList.remove('sel');
      li.classList.add('sel');
    };
    fm.list.appendChild(li);
  }
}

els.flash.onclick = openFlash;
fm.cancel.onclick = closeFlash;
fm.refresh.onclick = () => { fm.list.innerHTML = '<li class="muted">Scanning…</li>'; window.td.send({ cmd: 'list_disks' }); };
fm.go.onclick = () => {
  if (!selectedDisk) return;
  flashing = true;
  fm.go.disabled = true; fm.cancel.disabled = true;
  fm.progress.classList.remove('hidden');
  fm.fill.style.width = '0%';
  fm.phase.textContent = 'Starting…';
  window.td.send({ cmd: 'flash', disk_id: selectedDisk.id, tag: fm.tag.value || 'latest' });
};

// --- init from stored settings ---
(function init() {
  const s = loadSettings();
  if (s.pi) els.pi.value = s.pi;
  if (s.target) els.target.value = s.target;
  if (s.key) els.key.value = s.key;
  if (s.toe) setToe(s.toe);
})();
