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
  }
});

// --- init from stored settings ---
(function init() {
  const s = loadSettings();
  if (s.pi) els.pi.value = s.pi;
  if (s.target) els.target.value = s.target;
  if (s.key) els.key.value = s.key;
  if (s.toe) setToe(s.toe);
})();
