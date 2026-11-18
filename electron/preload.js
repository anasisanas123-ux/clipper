// Gives the page a few desktop abilities: native file dialogs, the real path of a dropped file, and restarting.
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('clipper', {
  pickVideo: () => ipcRenderer.invoke('pick-video'),
  pickImage: () => ipcRenderer.invoke('pick-image'),
  restart: () => ipcRenderer.invoke('restart'),
  pathForFile: (file) => webUtils.getPathForFile(file),
});
