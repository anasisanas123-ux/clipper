// Desktop shell: starts the Python engine on a free local port and shows its UI in a window.
const { app, BrowserWindow, shell, dialog, ipcMain } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const http = require('http');

const root = path.join(__dirname, '..');
let engine = null;
let engineLog = '';

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForEngine(port, timeoutMs = 60000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/api/settings', timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) resolve();
        else retry();
      });
      req.on('error', retry);
      req.on('timeout', () => req.destroy());
    };
    const retry = () => {
      if (engine === null || engine.exitCode !== null) return reject(new Error('The engine stopped.\n\n' + engineLog.slice(-1500)));
      if (Date.now() - started > timeoutMs) return reject(new Error('The engine did not start in time.\n\n' + engineLog.slice(-1500)));
      setTimeout(attempt, 400);
    };
    attempt();
  });
}

async function start() {
  const port = await freePort();
  const python = path.join(root, '.venv', 'Scripts', 'python.exe');
  engine = spawn(python, ['-m', 'backend.server', '--port', String(port)], {
    cwd: root, windowsHide: true, env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });
  engine.stdout.on('data', (d) => { engineLog += d; process.stdout.write(d); });
  engine.stderr.on('data', (d) => { engineLog += d; process.stderr.write(d); });
  engine.on('error', (e) => { engineLog += String(e); });

  const win = new BrowserWindow({
    width: 1440, height: 920, minWidth: 980, minHeight: 640,
    backgroundColor: '#0e0f13', autoHideMenuBar: true, title: 'Clipper',
    webPreferences: { contextIsolation: true, nodeIntegration: false, preload: path.join(__dirname, 'preload.js') },
  });
  ipcMain.handle('pick-video', async () => {
    const r = await dialog.showOpenDialog(win, {
      title: 'Choose a video', properties: ['openFile'],
      filters: [{ name: 'Videos', extensions: ['mp4', 'mkv', 'mov', 'webm', 'avi', 'flv', 'm4v', 'ts'] }],
    });
    return r.canceled ? null : r.filePaths[0];
  });
  // Links to licences, song pages and so on open in the normal browser, not inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  try {
    await waitForEngine(port);
    await win.loadURL(`http://127.0.0.1:${port}/`);
  } catch (e) {
    dialog.showErrorBox('Clipper could not start', String(e.message || e));
    app.quit();
  }
}

function stopEngine() {
  if (engine && engine.exitCode === null) {
    // /T also ends ffmpeg or any other child the engine started.
    spawn('taskkill', ['/pid', String(engine.pid), '/T', '/F'], { windowsHide: true });
  }
  engine = null;
}

app.whenReady().then(start);
app.on('window-all-closed', () => { stopEngine(); app.quit(); });
app.on('before-quit', stopEngine);
