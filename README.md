# Clipper

Desktop app that finds the strongest moments in a long video and turns them into YouTube Shorts: 9:16 reframing, word-by-word captions, optional music, a viral-chance score, a copyright risk check, generated titles and hashtags, and scheduled uploads to YouTube.

Sources: Kick VODs, YouTube videos, or video files already on your PC (button or drag-and-drop). A file that's already Short-length is kept whole and just scored and captioned.

Video types: IRL, Gaming, Podcast / interview, and Film / TV reaction. Each type weighs moments differently; Podcast looks for quotable lines (hot takes, stories, numbers, advice) rather than loud reactions.

## Setup (Windows)

Needs Python 3.11 and Node.js. An NVIDIA GPU makes speech recognition much faster but isn't required.

```
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
npm install
```

If `npm install` reports that Electron's install script was skipped, run `node node_modules/electron/install.js`.

## Run

Double-click `Start Clipper.vbs` (it opens Clipper without a console window; make a desktop shortcut to it if you like). `Start Clipper.bat` and `npm start` also work.

The first video you process downloads a speech model: about 1.6 GB with an NVIDIA GPU, or about 150 MB plus 485 MB without one. Without an NVIDIA GPU, transcription runs on the CPU and is the slow step; the progress bar shows how far along it is and the time left.

YouTube videos are downloaded in full HD in the background while the audio is analysed (roughly 300 MB per hour of video).

If something gets stuck, `data/clipper.log` records what each job did and how long it took.

## Optional keys (all entered in Settings)

- **Virality scoring** – choose the **built-in algorithm** (free, runs on your PC) or a **connected AI model**, which reads the whole transcript to pick moments and write titles, hooks and hashtags. Supported: Claude, OpenAI, Google Gemini, Groq, DeepSeek, OpenRouter, xAI, Ollama (local, no key), or any OpenAI-compatible service. Paste a key, press "Load models", pick one. If the AI fails, the built-in algorithm is used and the job says why.
- **Google OAuth client (Desktop app)** – needed to upload and schedule to your YouTube channel.
- **AudD key** – names the exact song playing in a clip.
- **Jamendo client ID** – ranks free music by popularity.

Everything the app stores (settings, keys, downloads, renders, exports) lives in `data/`, which is not committed.

## Copyright

The risk check is an estimate, not a clearance. Clips of other people's streams, films and shows can be claimed, blocked or struck regardless of what the app reports. Only upload what you have the right to.
