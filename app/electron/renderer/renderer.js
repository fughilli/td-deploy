'use strict';
// Renderer: drives the sidecar via window.td, renders progress + log.

const $ = (id) => document.getElementById(id);
const els = {
  pick: $('pick'), toePath: $('toe-path'), watch: $('watch'), deploy: $('deploy'),
  phase: $('phase'), bar: $('bar-fill'), pi: $('pi'), target: $('target'),
  flash: $('flash'), log: $('log'),
  dot: $('status-dot'),
  fixit: $('fixit'), fixitTitle: $('fixit-title'), fixitMsg: $('fixit-msg'),
  fixitCopy: $('fixit-copy'), fixitCopied: $('fixit-copied'),
  skipUnsupported: $('skip-unsupported'), magicChop: $('magic-chop'),
  rememberWifiPw: $('remember-wifi-pw'),
  assets: $('assets'), assetsList: $('assets-list'),
  assetsAddRoot: $('assets-addroot'), assetsRedeploy: $('assets-redeploy'),
  deployKeyList: $('deploy-key-list'), deployKeyAdd: $('deploy-key-add'),
  wifiSaved: $('wifi-saved'), wifiAdd: $('wifi-add'),
  settingsOpen: $('settings-open'), settingsModal: $('settings-modal'),
  settingsClose: $('settings-close'),
  addkeyModal: $('addkey-modal'), addkeyName: $('addkey-name'),
  addkeyGen: $('addkey-gen'), addkeyFile: $('addkey-file'), addkeyCancel: $('addkey-cancel'),
  addwifiModal: $('addwifi-modal'), addwifiSsid: $('addwifi-ssid'),
  addwifiPsk: $('addwifi-psk'), addwifiAdd: $('addwifi-add'), addwifiCancel: $('addwifi-cancel'),
  logCard: $('log-card'), logToggle: $('log-toggle'), logCaret: $('log-caret'),
  perfCard: $('perf-card'), perfToggle: $('perf-toggle'), perfCaret: $('perf-caret'),
  perfSummary: $('perf-summary'), perfCanvas: $('perf-canvas'), perfLegend: $('perf-legend'),
};

const STORE = 'td-deploy-settings';
let state = { toe: null, busy: false };
let assetRoots = [];      // extra folders to search for assets
let assetMap = {};        // original asset path/basename -> chosen replacement file
let configDir = null;     // app.getPath('userData'); where deploy keys are stored
let deployKeys = [];      // [{name, kind, path, dir, active, login, fingerprint}] from the sidecar
let wifiNetworks = [];    // [{ssid, psk}] saved Wi-Fi (psk kept only if rememberWifiPw)

function loadSettings() {
  try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; }
}
function saveSettings() {
  // Saved Wi-Fi: keep passwords only while "remember" is on (plaintext at rest).
  const nets = els.rememberWifiPw.checked
    ? wifiNetworks.map((n) => ({ ssid: n.ssid, psk: n.psk || '' }))
    : [];
  const s = {
    pi: els.pi.value, target: els.target.value, toe: state.toe,
    skipUnsupported: els.skipUnsupported.checked, magicChop: els.magicChop.checked,
    rememberWifiPw: els.rememberWifiPw.checked,
    assetRoots, assetMap, wifiNetworks: nets,
    // Per-card hostname prefill for the flash modal (not durable config).
    flashHostname: (fm.hostname && fm.hostname.value) || '',
  };
  localStorage.setItem(STORE, JSON.stringify(s));
  return s;
}
function pushSettings() {
  const s = saveSettings();
  // Note: `key` (deploy login key) is managed by the sidecar from the login key —
  // the renderer never sets it, so it isn't sent here.
  window.td.send({ cmd: 'set_settings', settings: {
    pi: s.pi, target: s.target,
    skip_unsupported: !!s.skipUnsupported, magic_chop: !!s.magicChop,
    asset_roots: assetRoots, asset_map: assetMap,
    // config_dir tells the sidecar where to store/read deploy keys (userData).
    ...(configDir ? { config_dir: configDir } : {}),
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

let fixPrompt = null;

function showFixit(evt, kind) {  // kind: 'error' | 'warning'
  fixPrompt = evt.fixPrompt || null;
  if (!fixPrompt) { els.fixit.classList.add('hidden'); return; }
  const warn = kind === 'warning';
  els.fixit.classList.toggle('warn', warn);
  const unsupported = evt.errorKind === 'unsupported_operator';
  els.fixitTitle.textContent = unsupported
    ? (warn ? 'Unsupported operators — deployed with placeholders'
            : 'Unsupported TouchDesigner operator')
    : (warn ? 'Warning' : 'Something went wrong');
  els.fixitMsg.textContent = evt.message || '';
  els.fixitCopied.classList.add('hidden');
  els.fixit.classList.remove('hidden');
}

els.fixitCopy.onclick = async () => {
  if (!fixPrompt) return;
  await window.td.copyText(fixPrompt);
  els.fixitCopied.classList.remove('hidden');
};

// --- missing assets: actionable panel (add a search root / pick a replacement) ---
function renderAssets(missing) {
  els.assetsList.innerHTML = '';
  if (!missing || !missing.length) { els.assets.classList.add('hidden'); return; }
  for (const a of missing) {
    const li = document.createElement('li');
    const info = document.createElement('div');
    info.className = 'asset-info';
    const roots = (a.searched || []).join(', ') || '(none)';
    info.innerHTML =
      `<code>${a.path}</code><span class="asset-searched">searched: ${roots}</span>`;
    const pick = document.createElement('button');
    pick.textContent = 'Choose file…';
    pick.onclick = async () => {
      const f = await window.td.pickFile();
      if (!f) return;
      assetMap[a.path] = f;                 // substitute this asset on the next deploy
      pushSettings();
      li.classList.add('resolved');
      pick.textContent = '→ ' + f.split(/[\\/]/).pop();
      pick.disabled = true;
    };
    li.append(info, pick);
    els.assetsList.appendChild(li);
  }
  els.assets.classList.remove('hidden');
}

// --- deploy keys: a list of keys. Checked (active) keys are trusted on flashed
// cards (authorized_keys); the radio marks the one that logs in for deploy. The
// sidecar owns the store; the renderer lists and issues add/activate/login/delete.
function renderDeployKeys(keys) {
  deployKeys = keys || [];
  renderDeployKeyList();
}

function renderDeployKeyList() {
  const list = els.deployKeyList;
  if (!list) return;
  list.innerHTML = '';
  if (!deployKeys.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'No deploy keys yet — add one below.';
    list.appendChild(li);
    return;
  }
  for (const k of deployKeys) {
    const li = document.createElement('li');

    const active = document.createElement('input');
    active.type = 'checkbox';
    active.checked = !!k.active;
    active.title = 'Trust this key on flashed cards (authorized_keys)';
    active.onchange = () =>
      window.td.send({ cmd: 'set_deploy_key_active', name: k.name, active: active.checked });

    const login = document.createElement('input');
    login.type = 'radio';
    login.name = 'deploy-login';
    login.checked = !!k.login;
    login.title = 'Use this key to log in for deploy';
    login.onchange = () => {
      if (login.checked) window.td.send({ cmd: 'set_deploy_login', name: k.name });
    };

    const label = document.createElement('span');
    const fp = (k.fingerprint || '').replace(/^SHA256:/, '').slice(0, 14);
    label.textContent = `${k.name} · ${k.kind}${fp ? ' · ' + fp + '…' : ''}`;
    label.title = k.path || '';

    const folder = document.createElement('button');
    folder.textContent = '📁';
    folder.title = 'Reveal in file manager';
    folder.onclick = () => window.td.revealPath(k.dir);

    const del = document.createElement('button');
    del.textContent = '🗑';
    del.className = 'danger';
    del.title = 'Delete key';
    del.onclick = () => {
      const extra = k.kind === 'sourced' ? ' (the original file is kept)' : '';
      if (confirm(`Delete deploy key "${k.name}"?${extra}`)) {
        window.td.send({ cmd: 'delete_deploy_key', name: k.name });
      }
    };

    li.appendChild(active);
    li.appendChild(login);
    li.appendChild(label);
    li.appendChild(folder);
    li.appendChild(del);
    list.appendChild(li);
  }
}

// --- saved Wi-Fi networks (only shown while "remember" is on) ---
function renderWifiSaved() {
  const list = els.wifiSaved;
  if (!list) return;
  list.innerHTML = '';
  const on = els.rememberWifiPw.checked;
  els.wifiAdd.classList.toggle('hidden', !on);  // the list "drops out" when off
  list.classList.toggle('hidden', !on);
  if (!on) return;
  if (!wifiNetworks.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'No saved networks — add one below.';
    list.appendChild(li);
    return;
  }
  wifiNetworks.forEach((n, i) => {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = n.ssid;

    const pw = document.createElement('input');
    pw.type = 'password';
    pw.value = n.psk || '';
    pw.readOnly = true;
    pw.className = 'wifi-pw';

    const eye = document.createElement('button');
    eye.textContent = '👁';
    eye.title = 'Show/hide password';
    eye.onclick = () => { pw.type = pw.type === 'password' ? 'text' : 'password'; };

    const del = document.createElement('button');
    del.textContent = '🗑';
    del.className = 'danger';
    del.title = 'Delete network';
    del.onclick = () => { wifiNetworks.splice(i, 1); pushSettings(); renderWifiSaved(); };

    li.appendChild(label);
    li.appendChild(pw);
    li.appendChild(eye);
    li.appendChild(del);
    list.appendChild(li);
  });
}

// --- settings modal + collapsible log ---
els.settingsOpen.onclick = () => {
  renderWifiSaved();
  window.td.send({ cmd: 'list_deploy_keys' });  // refresh the management list
  els.settingsModal.classList.remove('hidden');
};
els.settingsClose.onclick = () => els.settingsModal.classList.add('hidden');
els.settingsModal.onclick = (e) => {
  if (e.target === els.settingsModal) els.settingsModal.classList.add('hidden');
};
els.logToggle.onclick = () => {
  const collapsed = els.logCard.classList.toggle('collapsed');
  els.logToggle.setAttribute('aria-expanded', String(!collapsed));
  els.logCaret.textContent = collapsed ? '▸' : '▾';
};

// --- Performance pane: poll the player's /stats and plot the per-frame breakdown ---
// /stats counters are cumulative; we sample every PERF_MS and plot the delta
// (ms-per-frame per label) as stacked bars. `frame` and `present` are aggregates
// (parents of the leaves), so they're excluded from the stack and shown as lines.
const PERF_MS = 500;
const PERF_MAX = 120; // rolling window (~1 min at 500 ms)
let perfPrev = null;
let perfSamples = [];
let perfTimer = null;

function perfExcluded(k) { return k === 'frame' || k === 'present'; }
function perfColor(label) {
  let h = 0;
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 65% 55%)`;
}
async function perfPoll() {
  const host = (els.pi.value || '').trim();
  const s = await window.td.fetchStats(host, 8788);
  if (!s || typeof s !== 'object') {
    els.perfSummary.textContent = `no /stats from ${host || '(no host)'}:8788 — is the player deployed & reachable?`;
    perfPrev = null;
    return;
  }
  const now = {};
  for (const k in s) now[k] = { total: s[k].total_ms || 0, count: s[k].count || 0 };
  const pf = perfPrev && perfPrev.frame;
  if (pf && now.frame && now.frame.count > perfPrev.frame.count) {
    const dframes = now.frame.count - perfPrev.frame.count;
    const parts = {};
    for (const k in now) {
      if (perfExcluded(k)) continue;
      const p = perfPrev[k];
      if (!p) continue;
      const dt = now[k].total - p.total;
      if (dt > 0) parts[k] = dt / dframes;
    }
    const frameMs = (now.frame.total - perfPrev.frame.total) / dframes;
    perfSamples.push({ parts, frame: frameMs, fps: frameMs > 0 ? 1000 / frameMs : 0 });
    if (perfSamples.length > PERF_MAX) perfSamples.shift();
    perfDraw();
    const last = perfSamples[perfSamples.length - 1];
    els.perfSummary.textContent =
      `frame ${last.frame.toFixed(1)} ms · ${last.fps.toFixed(1)} fps  (${host})`;
  }
  perfPrev = now;
}
function perfDraw() {
  const cv = els.perfCanvas;
  if (!cv) return;
  const w = cv.clientWidth || 600;
  const h = 150;
  if (cv.width !== w) cv.width = w;
  cv.height = h;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, w, h);
  if (!perfSamples.length) return;
  let ymax = 20;
  for (const smp of perfSamples) {
    let sum = 0;
    for (const k in smp.parts) sum += smp.parts[k];
    ymax = Math.max(ymax, sum, smp.frame);
  }
  ymax *= 1.1;
  const labels = Array.from(new Set(perfSamples.flatMap((s) => Object.keys(s.parts)))).sort();
  const n = perfSamples.length;
  const bw = w / PERF_MAX;
  for (let i = 0; i < n; i++) {
    const smp = perfSamples[i];
    const x = w - (n - i) * bw;
    let yacc = h;
    for (const label of labels) {
      const v = smp.parts[label] || 0;
      if (v <= 0) continue;
      const ph = (v / ymax) * h;
      ctx.fillStyle = perfColor(label);
      ctx.fillRect(x, yacc - ph, Math.ceil(bw), ph);
      yacc -= ph;
    }
  }
  // Reference lines at 30 and 60 fps budgets.
  ctx.strokeStyle = 'rgba(255,255,255,0.25)';
  ctx.lineWidth = 1;
  for (const ms of [1000 / 30, 1000 / 60]) {
    const y = h - (ms / ymax) * h;
    if (y > 0 && y < h) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  }
  const last = perfSamples[perfSamples.length - 1];
  els.perfLegend.innerHTML = '';
  for (const label of labels) {
    const v = last.parts[label] || 0;
    const item = document.createElement('span');
    item.className = 'perf-leg';
    const sw = document.createElement('span');
    sw.className = 'perf-sw';
    sw.style.background = perfColor(label);
    item.appendChild(sw);
    item.appendChild(document.createTextNode(`${label} ${v.toFixed(1)}`));
    els.perfLegend.appendChild(item);
  }
}
els.perfToggle.onclick = () => {
  const collapsed = els.perfCard.classList.toggle('collapsed');
  els.perfToggle.setAttribute('aria-expanded', String(!collapsed));
  els.perfCaret.textContent = collapsed ? '▸' : '▾';
  if (!collapsed) {
    perfPrev = null;
    perfSamples = [];
    perfPoll();
    perfTimer = setInterval(perfPoll, PERF_MS);
  } else if (perfTimer) {
    clearInterval(perfTimer);
    perfTimer = null;
  }
};

// --- add-deploy-key modal (generate a new key, or load one from a file) ---
function uniqueKeyName(base) {
  const b = base || 'deploy';
  const existing = new Set(deployKeys.map((k) => k.name));
  let name = b;
  for (let i = 2; existing.has(name); i++) name = b + '-' + i;
  return name;
}
els.deployKeyAdd.onclick = () => {
  els.addkeyName.value = '';
  els.addkeyModal.classList.remove('hidden');
};
els.addkeyCancel.onclick = () => els.addkeyModal.classList.add('hidden');
els.addkeyGen.onclick = () => {
  const name = (els.addkeyName.value || '').trim() || uniqueKeyName('deploy');
  if (!/^[A-Za-z0-9._-]+$/.test(name)) {
    log('invalid key name (use letters, digits, . _ -)', 'err');
    return;
  }
  window.td.send({ cmd: 'add_deploy_key', name });  // path omitted -> generate
  els.addkeyModal.classList.add('hidden');
};
els.addkeyFile.onclick = async () => {
  const path = await window.td.pickFile();
  if (!path) return;
  const base = (path.split(/[\\/]/).pop() || 'key').replace(/[^A-Za-z0-9._-]/g, '-');
  const name = (els.addkeyName.value || '').trim() || uniqueKeyName(base);
  if (!/^[A-Za-z0-9._-]+$/.test(name)) {
    log('invalid key name (use letters, digits, . _ -)', 'err');
    return;
  }
  window.td.send({ cmd: 'add_deploy_key', name, path });  // path -> sourced
  els.addkeyModal.classList.add('hidden');
};

// --- add-Wi-Fi modal ---
els.wifiAdd.onclick = () => {
  els.addwifiSsid.value = '';
  els.addwifiPsk.value = '';
  els.addwifiModal.classList.remove('hidden');
};
els.addwifiCancel.onclick = () => els.addwifiModal.classList.add('hidden');
els.addwifiAdd.onclick = () => {
  const ssid = (els.addwifiSsid.value || '').trim();
  if (!ssid) { log('enter an SSID', 'err'); return; }
  wifiNetworks = wifiNetworks.filter((n) => n.ssid !== ssid);  // replace a duplicate SSID
  wifiNetworks.push({ ssid, psk: els.addwifiPsk.value || '' });
  pushSettings();
  renderWifiSaved();
  els.addwifiModal.classList.add('hidden');
};

els.assetsAddRoot.onclick = async () => {
  const d = await window.td.pickDir();
  if (!d) return;
  if (!assetRoots.includes(d)) assetRoots.push(d);
  pushSettings();
  log('asset search folder added: ' + d);
};
els.assetsRedeploy.onclick = () => {
  if (state.toe) window.td.send({ cmd: 'deploy', toe: state.toe });
};

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
els.deploy.onclick = () => {
  if (state.toe) window.td.send({ cmd: 'deploy', toe: state.toe });
};
els.watch.onchange = () => {
  window.td.send({ cmd: 'watch', enable: els.watch.checked, toe: state.toe });
};
for (const el of [els.pi, els.target, els.skipUnsupported, els.magicChop])
  el.onchange = pushSettings;
// Turning OFF "remember Wi-Fi networks" deletes any saved entries — warn first,
// and revert the toggle if the user cancels.
els.rememberWifiPw.onchange = () => {
  if (!els.rememberWifiPw.checked && wifiNetworks.length) {
    if (!confirm('Forget all saved Wi-Fi networks? Their stored passwords will be deleted.')) {
      els.rememberWifiPw.checked = true;
      return;
    }
    wifiNetworks = [];
  }
  pushSettings();
  renderWifiSaved();
};

// --- sidecar events -> UI ---
window.td.onEvent((evt) => {
  switch (evt.type) {
    case 'ready':
      // A fresh sidecar knows nothing: not the picked project, not whether Watch is
      // on. That happens on first launch AND every dev hot-restart (watchSidecarDev
      // respawns it on any engine edit), so re-push the renderer's state or Watch
      // silently stops firing while its checkbox still reads "on".
      pushSettings();
      if (state.toe) window.td.send({ cmd: 'pick_toe', toe: state.toe });
      if (els.watch.checked) window.td.send({ cmd: 'watch', enable: true, toe: state.toe });
      if (evt.settings && evt.settings.base_image_tag)
        setDefaultTag(evt.settings.base_image_tag);   // CI-stamped default
      window.td.send({ cmd: 'list_deploy_keys' });    // populate the key selector
      log('sidecar ready');
      break;
    case 'start':
      setBusy(true, 'start');
      els.log.textContent = '';
      els.fixit.classList.add('hidden');
      els.assets.classList.add('hidden');
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
      showFixit(evt, 'error');
      break;
    case 'warning':
      log('WARNING: ' + evt.message, 'err');
      showFixit(evt, 'warning');
      break;
    case 'assets':
      renderAssets(evt.missing || []);
      break;
    case 'deploy_keys':
      renderDeployKeys(evt.keys || []);
      break;
    case 'deploy_key_generated':
      log('deploy key generated: ' + evt.name + ' (' + evt.fingerprint + ')', 'ok');
      break;
    case 'watch':
      els.watch.checked = !!evt.enabled;
      log('watch ' + (evt.enabled ? 'on' : 'off'));
      break;
    case 'disks':
      renderDisks(evt.disks || []);
      break;
    case 'releases':
      renderReleases(evt.releases || []);
      if (evt.message) log(evt.message, 'err');
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
      fm.phase.textContent = 'Done ✓ — you can remove the card';
      fm.fill.style.width = '100%';
      // Turn the primary "Erase & Flash" button into a "Done" button that closes
      // the modal; the flash is finished, so Cancel is redundant — hide it.
      fm.go.textContent = 'Done';
      fm.go.dataset.mode = 'done';
      fm.go.disabled = false;
      fm.cancel.classList.add('hidden');
      log('flashed ' + evt.disk.name, 'ok');
      break;
    case 'flash_error':
      flashing = false;
      fm.cancel.disabled = false;
      fm.go.disabled = false;  // re-enable so the user can retry (not just Cancel)
      fm.phase.textContent = 'Error';
      log('FLASH ERROR: ' + evt.message, 'err');
      showFixit(evt, 'error');
      break;
  }
});

// --- SD flashing modal ---
const fm = {
  modal: $('flash-modal'), tag: $('flash-tag'), refresh: $('flash-refresh'),
  list: $('disk-list'), go: $('flash-go'), cancel: $('flash-cancel'),
  progress: $('flash-progress'), phase: $('flash-phase'), fill: $('flash-fill'),
  hostname: $('flash-hostname'), ssid: $('flash-ssid'), psk: $('flash-psk'),
  wifiInline: $('flash-wifi-inline'), wifiNote: $('flash-wifi-note'),
};

// RFC1123 label — mirrors flash_config.valid_hostname / the image's boot guard.
function validHostname(name) {
  return /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test((name || '').trim().toLowerCase());
}
let selectedDisk = null;
let flashing = false;
// The CI-stamped default (or 'latest') to select once the releases list arrives.
// Set from the sidecar 'ready' settings; kept in sync into the <select>.
let defaultTag = 'latest';

// Populate the version <select> from the sidecar's releases list, marking
// prereleases and (re)selecting the stamped default. Always keep at least the
// default option so the picker works even if the network request failed.
function renderReleases(releases) {
  const prev = fm.tag.value;
  fm.tag.innerHTML = '';
  const seen = new Set();
  const add = (tag, label) => {
    if (!tag || seen.has(tag)) return;
    seen.add(tag);
    const o = document.createElement('option');
    o.value = tag;
    o.textContent = label || tag;
    fm.tag.appendChild(o);
  };
  for (const r of releases) {
    const name = r.name && r.name !== r.tag_name ? ` — ${r.name}` : '';
    add(r.tag_name, `${r.tag_name}${name}${r.prerelease ? ' (prerelease)' : ''}`);
  }
  // Ensure the default and the prior selection remain selectable even if absent
  // from the list (e.g. a stamped tag not yet published, or an empty/failed list).
  add(defaultTag, defaultTag);
  if (prev) add(prev, prev);
  fm.tag.value = defaultTag || prev || 'latest';
}

// Remember the CI-stamped default and reflect it in the picker.
function setDefaultTag(tag) {
  if (!tag) return;
  defaultTag = tag;
  const opts = Array.from(fm.tag.options).map((o) => o.value);
  if (!opts.includes(tag)) {
    const o = document.createElement('option');
    o.value = tag;
    o.textContent = tag;
    fm.tag.appendChild(o);
  }
  fm.tag.value = tag;
}

function openFlash() {
  selectedDisk = null;
  flashing = false;
  fm.go.disabled = true;
  fm.go.textContent = 'Erase & Flash';  // reset from a prior "Done" state
  fm.go.dataset.mode = '';
  fm.cancel.classList.remove('hidden');
  fm.cancel.disabled = false;
  fm.progress.classList.add('hidden');
  // If Wi-Fi is configured in Settings, use those networks and hide the inline
  // fields; otherwise let the user enter one network inline for this flash.
  const haveSaved = wifiNetworks.length > 0;
  fm.wifiInline.classList.toggle('hidden', haveSaved);
  fm.wifiNote.classList.toggle('hidden', !haveSaved);
  if (haveSaved) {
    fm.wifiNote.textContent =
      `Using ${wifiNetworks.length} saved Wi-Fi network${wifiNetworks.length > 1 ? 's' : ''} (Settings ⚙).`;
  }
  fm.list.innerHTML = '<li class="muted">Scanning…</li>';
  fm.modal.classList.remove('hidden');
  window.td.send({ cmd: 'list_disks' });
  window.td.send({ cmd: 'list_releases' });
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
// Cancel is always available — even mid-flash. If a flash is running, ask the
// sidecar to abort it (best-effort; it stops before the raw write) and drop the
// UI back to a usable state so the user can retry instead of being trapped.
fm.cancel.onclick = () => {
  if (flashing) {
    window.td.send({ cmd: 'cancel_flash' });
    log('flash cancelled', 'err');
  }
  flashing = false;
  fm.modal.classList.add('hidden');
};
fm.refresh.onclick = () => {
  fm.list.innerHTML = '<li class="muted">Scanning…</li>';
  window.td.send({ cmd: 'list_disks' });
  window.td.send({ cmd: 'list_releases' });
};
// Persist the flash-time hostname prefill as it's edited.
fm.hostname.onchange = pushSettings;

fm.go.onclick = () => {
  if (fm.go.dataset.mode === 'done') { closeFlash(); return; }  // finished -> close
  if (!selectedDisk) return;
  const hostname = (fm.hostname.value || '').trim().toLowerCase();
  if (hostname && !validHostname(hostname)) {
    fm.phase.classList.remove('hidden');
    fm.progress.classList.remove('hidden');
    fm.phase.textContent = 'Invalid hostname — use letters, digits and hyphens (RFC1123).';
    return;
  }
  // Prefer the saved networks (Settings); fall back to the inline field when none.
  let networks = wifiNetworks.map((n) => ({ ssid: n.ssid, psk: n.psk || null }));
  if (!networks.length) {
    const ssid = (fm.ssid.value || '').trim();
    networks = ssid ? [{ ssid, psk: fm.psk.value || null }] : [];
  }
  pushSettings();
  flashing = true;
  fm.go.disabled = true;
  fm.cancel.disabled = false;  // keep Cancel usable so a stuck flash isn't a trap
  fm.progress.classList.remove('hidden');
  fm.fill.style.width = '0%';
  fm.phase.textContent = 'Starting…';
  window.td.send({
    cmd: 'flash', disk_id: selectedDisk.id, tag: fm.tag.value || 'latest',
    hostname: hostname || 'tdplayer', networks,
  });
};

// --- init from stored settings ---
(async function init() {
  // Learn the app config dir up front so the very first set_settings carries it and
  // the sidecar stores deploy keys in the OS-standard userData path.
  try { configDir = await window.td.configDir(); } catch (e) { configDir = null; }
  const s = loadSettings();
  if (s.pi) els.pi.value = s.pi;
  if (s.target) els.target.value = s.target;
  if (s.skipUnsupported) els.skipUnsupported.checked = true;
  if (s.magicChop) els.magicChop.checked = true;
  if (s.rememberWifiPw) els.rememberWifiPw.checked = true;
  if (Array.isArray(s.assetRoots)) assetRoots = s.assetRoots;
  if (s.assetMap && typeof s.assetMap === 'object') assetMap = s.assetMap;
  if (Array.isArray(s.wifiNetworks)) wifiNetworks = s.wifiNetworks;
  renderWifiSaved();
  // Prefill the flash modal's hostname (per-card, not durable config).
  if (s.flashHostname) fm.hostname.value = s.flashHostname;
  if (s.toe) setToe(s.toe);
  // configDir is now known: push it and (re)load the key list from that dir. Safe
  // if 'ready' already fired with the default dir — this re-syncs to userData.
  if (configDir) {
    pushSettings();
    window.td.send({ cmd: 'list_deploy_keys' });
  }
})();
