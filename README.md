# Clipper

Desktop app that finds the strongest moments in a Kick stream and turns them into YouTube Shorts: 9:16 reframing, word-by-word captions, optional music, a viral-chance score, a copyright risk check, generated titles and hashtags, and scheduled uploads to YouTube.

## Setup (Windows)

Needs Python 3.11 and Node.js. An NVIDIA GPU makes speech recognition much faster but isn't required.

```
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
npm install
```

If `npm install` reports that Electron's install script was skipped, run `node node_modules/electron/install.js`.

## Run

Double-click `Start Clipper.bat`, or run `npm start`.

The first stream you process downloads the speech model (about 1.6 GB).

## Optional keys (all entered in Settings)

- **Anthropic API key** – lets Claude pick the moments and write titles, hooks and hashtags. Without it, everything is scored on your PC.
- **Google OAuth client (Desktop app)** – needed to upload and schedule to your YouTube channel.
- **AudD key** – names the exact song playing in a clip.
- **Jamendo client ID** – ranks free music by popularity.

Everything the app stores (settings, keys, downloads, renders, exports) lives in `data/`, which is not committed.

## Copyright

The risk check is an estimate, not a clearance. Clips of other people's streams, films and shows can be claimed, blocked or struck regardless of what the app reports. Only upload what you have the right to.
