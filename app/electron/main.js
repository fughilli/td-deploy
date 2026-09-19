// td-deploy Studio — Electron main process.
// Spawns the Python deploy sidecar, bridges its JSON-lines protocol to the
// renderer, and provides the native .toe file dialog.
'use strict';

const { app, BrowserWindow, ipcMain, dialog, clipboard } = require('electron');
const { spawn } = require('child_process');
const readline = require('readline');
const path = require('path');
const fs = require('fs');

let win = null;
let sidecar = null;

// The sidecar is a frozen binary next to the packaged app; in dev it's the repo's
// python sidecar (run `TOXC_PYTHON=./nix/dev.sh ... ` is not needed on a real
// machine — python3 with Pillow/PyAV suffices).
function sidecarCommand() {
  const exe = process.platform === 'win32' ? 'td-deploy-sidecar.exe' : 'td-deploy-sidecar';
  const bundled = path.join(process.resourcesPath || '', 'sidecar', exe);
  if (fs.existsSync(bundled)) return { cmd: bundled, args: [] };
  const repo = path.resolve(__dirname, '..', '..'); // app/electron -> repo root
  const py = process.env.TOXC_PYTHON || 'python3';
  return { cmd: py, args: [path.join(repo, 'app', 'sidecar.py')] };
}

function startSidecar() {
  const { cmd, args } = sidecarCommand();
  sidecar = spawn(cmd, args, { stdio: ['pipe', 'pipe', 'inherit'] });
  const rl = readline.createInterface({ input: sidecar.stdout });
  rl.on('line', (line) => {
    line = line.trim();
    if (!line) return;
    let evt;
    try { evt = JSON.parse(line); } catch (e) { return; } // ignore stray non-JSON
    if (win && !win.isDestroyed()) win.webContents.send('sidecar-event', evt);
  });
  sidecar.on('exit', (code) => {
    if (win && !win.isDestroyed())
      win.webContents.send('sidecar-event', { type: 'error', message: `sidecar exited (${code})` });
  });
  sidecar.on('error', (err) => {
    if (win && !win.isDestroyed())
      win.webContents.send('sidecar-event', { type: 'error', message: `sidecar: ${err.message}` });
  });
}

function toSidecar(obj) {
  if (sidecar && sidecar.stdin.writable) sidecar.stdin.write(JSON.stringify(obj) + '\n');
}

// --- dev live-reload of the Python sidecar (guarded by TOXC_DEV) ---------------
// electronmon reloads/restarts the Electron side (main/preload/renderer) on edit;
// this restarts the sidecar subprocess when the Python engine changes, so the whole
// app hot-reloads with the window still open. No effect in a packaged build.
function restartSidecar() {
  if (sidecar) {
    try {
      sidecar.removeAllListeners(); // don't fire the "sidecar exited" error toast
      sidecar.kill();
    } catch (e) {
      /* already gone */
    }
    sidecar = null;
  }
  startSidecar();
  if (win && !win.isDestroyed())
    win.webContents.send('sidecar-event', { type: 'log', line: '[dev] sidecar reloaded' });
}

function watchSidecarDev() {
  const repo = path.resolve(__dirname, '..', '..'); // app/electron -> repo root
  // Restart the sidecar on edits to the deploy engine AND the toxc compiler libs
  // it imports (compile.py pulls in ir/importer/passes/lowering/compiler/runtime),
  // so a compiler change hot-reloads without a manual //app:dev restart.
  const targets = [
    path.join(repo, 'app', 'sidecar.py'),
    path.join(repo, 'app', 'deploy_engine'),
    ...['ir', 'importer', 'passes', 'lowering', 'compiler', 'runtime', 'runtime_expr'].map((d) =>
      path.join(repo, d)
    ),
  ];
  let timer = null;
  const bounce = () => {
    clearTimeout(timer);
    timer = setTimeout(restartSidecar, 200); // debounce editor save bursts
  };
  for (const t of targets) {
    try {
      fs.watch(t, { recursive: true }, bounce);
    } catch (e) {
      /* target missing: skip */
    }
  }
}

// The app icon (electron-builder bakes build/icon.png into the packaged app; this is for
// the window/dock in `npm start` / bazel run //app:dev, where there's no bundle icon).
const APP_ICON = path.join(__dirname, 'build', 'icon.png');

function createWindow() {
  win = new BrowserWindow({
    width: 760, height: 620, minWidth: 620, minHeight: 480,
    title: 'td-deploy Studio',
    icon: fs.existsSync(APP_ICON) ? APP_ICON : undefined, // Windows/Linux window icon
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  if (process.platform === 'darwin' && app.dock && fs.existsSync(APP_ICON)) {
    try { app.dock.setIcon(APP_ICON); } catch (e) { /* dev-only nicety */ }
  }
  createWindow();
  startSidecar();
  if (process.env.TOXC_DEV) watchSidecarDev();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => {
  if (sidecar) sidecar.kill();
  app.quit();
});

ipcMain.handle('pick-toe', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose a TouchDesigner project',
    properties: ['openFile'],
    filters: [{ name: 'TouchDesigner', extensions: ['toe', 'tox'] }],
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});

ipcMain.handle('pick-key', async () => {
  const r = await dialog.showOpenDialog(win, { title: 'SSH deploy key', properties: ['openFile'] });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});

ipcMain.on('command', (_e, obj) => toSidecar(obj));

// The app's per-user config dir (deploy keys are stored under it by the sidecar).
// The renderer includes this in set_settings so keys live in the OS-standard spot.
ipcMain.handle('config-dir', () => app.getPath('userData'));

// Copy the fix-it prompt to the clipboard (renderer file:// isn't a secure context, so
// navigator.clipboard is unavailable — go through the main-process clipboard).
ipcMain.handle('copy-text', (_e, text) => {
  clipboard.writeText(String(text ?? ''));
  return true;
});

// Missing-asset resolution: pick a replacement file, or a folder to add to the search roots.
ipcMain.handle('pick-file', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose a replacement file',
    properties: ['openFile'],
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});
ipcMain.handle('pick-dir', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Add a folder to search for assets',
    properties: ['openDirectory'],
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});
