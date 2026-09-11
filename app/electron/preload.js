// Safe bridge between the renderer and the main process (contextIsolation on).
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('td', {
  pickToe: () => ipcRenderer.invoke('pick-toe'),
  pickKey: () => ipcRenderer.invoke('pick-key'),
  send: (obj) => ipcRenderer.send('command', obj),
  onEvent: (cb) => ipcRenderer.on('sidecar-event', (_e, evt) => cb(evt)),
});
