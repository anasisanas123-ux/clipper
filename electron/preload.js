// Gives the page two desktop abilities: a native "open video" dialog, and the real path of a dropped file.
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('clipper', {
  pickVideo: () => ipcRenderer.invoke('pick-video'),
  pathForFile: (file) => webUtils.getPathForFile(file),
});
