'use strict';
// Renderer: drives the sidecar via window.td, renders progress + log.

const $ = (id) => document.getElementById(id);
const els = {
  pick: $('pick'), toePath: $('toe-path'), watch: $('watch'), deploy: $('deploy'),
  phase: $('phase'), bar: $('bar-fill'), pi: $('pi'), target: $('target'),
  key: $('key'), pickKey: $('pick-key'), flash: $('flash'), log: $('log'),
  dot: $('status-dot'),
  fixit: $('fixit'), fixitTitle: $('fixit-title'), fixitMsg: $('fixit-msg'),
  fixitCopy: $('fixit-copy'), fixitCopied: $('fixit-copied'),
  skipUnsupported: $('skip-unsupported'), magicChop: $('magic-chop'),
  rememberWifiPw: $('remember-wifi-pw'),
  assets: $('assets'), assetsList: $('assets-list'),
  assetsAddRoot: $('assets-addroot'), assetsRedeploy: $('assets-redeploy'),
  deployKeySelect: $('deploy-key-select'), deployKeyGen: $('deploy-key-gen'),
  deployKeyName: $('deploy-key-name'), deployKeyFp: $('deploy-key-fp'),
};

const STORE = 'td-deploy-settings';
let state = { toe: null, busy: false };
let assetRoots = [];      // extra folders to search for assets
let assetMap = {};        // original asset path/basename -> chosen replacement file
let configDir = null;     // app.getPath('userData'); where deploy keys are stored
let deployKeys = [];      // [{name, fingerprint, active}] from the sidecar

function loadSettings() {
  try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; }
}
function saveSettings() {
  const s = {
    pi: els.pi.value, target: els.target.value, key: els.key.value, toe: state.toe,
    skipUnsupported: els.skipUnsupported.checked, magicChop: els.magicChop.checked,
    rememberWifiPw: els.rememberWifiPw.checked,
    assetRoots, assetMap,
    // Flash-time per-card config, cached to prefill the modal next open. The
    // hostname and SSID are always remembered; the Wi-Fi PASSWORD is persisted
    // ONLY when the "Remember Wi-Fi password" toggle is on (plaintext at rest) —
    // otherwise it's dropped so it never touches disk.
    flashHostname: (fm.hostname && fm.hostname.value) || '',
    flashSsid: (fm.ssid && fm.ssid.value) || '',
    flashPsk: (els.rememberWifiPw.checked && fm.psk && fm.psk.value) || '',
  };
  localStorage.setItem(STORE, JSON.stringify(s));
  return s;
}
function pushSettings() {
  const s = saveSettings();
  window.td.send({ cmd: 'set_settings', settings: {
    pi: s.pi, target: s.target, key: s.key || null,
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

// --- deploy keys: generate + select the active ed25519 key ---
// The ACTIVE key's public half is written to the SD card (so the flashed Pi trusts
// it) and its private half is used to deploy. The sidecar owns the store; the
// renderer just lists/selects and asks it to generate.
function renderDeployKeys(keys, active) {
  deployKeys = keys || [];
  const sel = els.deployKeySelect;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '(none — using SSH key above)';
  sel.appendChild(none);
  for (const k of deployKeys) {
    const o = document.createElement('option');
    o.value = k.name;
    const fp = (k.fingerprint || '').replace(/^SHA256:/, '').slice(0, 12);
    o.textContent = `${k.name}${fp ? ' — ' + fp + '…' : ''}`;
    sel.appendChild(o);
  }
  const activeName = active || (deployKeys.find((k) => k.active) || {}).name || '';
  sel.value = activeName;
  const cur = deployKeys.find((k) => k.name === activeName);
  els.deployKeyFp.textContent = cur ? cur.fingerprint : '';
}

els.deployKeyGen.onclick = () => {
  // Electron's renderer has no window.prompt(), so take the name from an inline
  // field; blank -> a unique default (deploy, deploy-2, …). Chars must match the
  // sidecar's key-name rule.
  const existing = new Set(deployKeys.map((k) => k.name));
  let name = (els.deployKeyName.value || '').trim();
  if (!name) {
    name = 'deploy';
    for (let i = 2; existing.has(name); i++) name = 'deploy-' + i;
  }
  if (!/^[A-Za-z0-9._-]+$/.test(name)) {
    log('invalid deploy-key name (use letters, digits, . _ -)', 'err');
    return;
  }
  window.td.send({ cmd: 'gen_deploy_key', name });
  els.deployKeyName.value = '';
};
els.deployKeySelect.onchange = () => {
  const name = els.deployKeySelect.value;
  if (name) window.td.send({ cmd: 'select_deploy_key', name });
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
for (const el of [els.pi, els.target, els.key, els.skipUnsupported, els.magicChop,
  els.rememberWifiPw])
  el.onchange = pushSettings;

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
      renderDeployKeys(evt.keys || [], evt.active);
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
  fm.progress.classList.add('hidden');
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
fm.cancel.onclick = closeFlash;
fm.refresh.onclick = () => {
  fm.list.innerHTML = '<li class="muted">Scanning…</li>';
  window.td.send({ cmd: 'list_disks' });
  window.td.send({ cmd: 'list_releases' });
};
// Persist the flash-time fields as they're edited so they prefill next open (the
// password only when the toggle is on — see saveSettings).
for (const el of [fm.hostname, fm.ssid, fm.psk]) el.onchange = pushSettings;

fm.go.onclick = () => {
  if (!selectedDisk) return;
  const hostname = (fm.hostname.value || '').trim().toLowerCase();
  if (hostname && !validHostname(hostname)) {
    fm.phase.classList.remove('hidden');
    fm.progress.classList.remove('hidden');
    fm.phase.textContent = 'Invalid hostname — use letters, digits and hyphens (RFC1123).';
    return;
  }
  const ssid = (fm.ssid.value || '').trim();
  const networks = ssid ? [{ ssid, psk: fm.psk.value || null }] : [];
  pushSettings();  // cache the entered values (password gated by the toggle)
  flashing = true;
  fm.go.disabled = true; fm.cancel.disabled = true;
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
  if (s.key) els.key.value = s.key;
  if (s.skipUnsupported) els.skipUnsupported.checked = true;
  if (s.magicChop) els.magicChop.checked = true;
  if (s.rememberWifiPw) els.rememberWifiPw.checked = true;
  if (Array.isArray(s.assetRoots)) assetRoots = s.assetRoots;
  if (s.assetMap && typeof s.assetMap === 'object') assetMap = s.assetMap;
  // Prefill the flash modal's per-card fields. Hostname/SSID always; the password
  // only if it was remembered (i.e. the toggle was on when last saved).
  if (s.flashHostname) fm.hostname.value = s.flashHostname;
  if (s.flashSsid) fm.ssid.value = s.flashSsid;
  if (s.flashPsk && s.rememberWifiPw) fm.psk.value = s.flashPsk;
  if (s.toe) setToe(s.toe);
  // configDir is now known: push it and (re)load the key list from that dir. Safe
  // if 'ready' already fired with the default dir — this re-syncs to userData.
  if (configDir) {
    pushSettings();
    window.td.send({ cmd: 'list_deploy_keys' });
  }
})();
