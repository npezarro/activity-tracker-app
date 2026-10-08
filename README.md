# Activity Tracker

A private, local-first record of what you worked on, for Windows and macOS.

Activity Tracker watches the window you're working in, reads what's on it, and keeps a
timeline on your own computer: which app and document, for how long, and what changed.
Optionally, a model describes each capture in plain words and writes hourly and daily
summaries. You choose the models: **local** (nothing leaves your computer), **online**,
or a **mix** where a local model screens every capture and only non-sensitive ones go
online.

## Download

| | |
|---|---|
| **Windows** installer | [ActivityTracker-Setup-x64.exe](https://github.com/npezarro/activity-tracker-app/releases/latest/download/ActivityTracker-Setup-x64.exe) |
| Windows portable (unzip and run) | [ActivityTracker-windows-x64.zip](https://github.com/npezarro/activity-tracker-app/releases/latest/download/ActivityTracker-windows-x64.zip) |
| **macOS** Apple Silicon | [ActivityTracker-macos-arm64.dmg](https://github.com/npezarro/activity-tracker-app/releases/latest/download/ActivityTracker-macos-arm64.dmg) |
| macOS Intel | [ActivityTracker-macos-intel.dmg](https://github.com/npezarro/activity-tracker-app/releases/latest/download/ActivityTracker-macos-intel.dmg) |

All versions: [Releases](https://github.com/npezarro/activity-tracker-app/releases).
The app checks for a newer release once a day (Settings > About & updates), downloads it,
verifies its checksum and restarts into it. Your data and settings are kept.

**macOS first run:** the app isn't notarized by Apple yet. Open it once, then System
Settings > Privacy & Security > "Open Anyway". Allow **Screen Recording** and
**Accessibility** for Activity Tracker when asked, then press *Restart capture*. Releases
are signed with the same certificate every time, so you only grant these once.

**Windows:** nothing to grant. SmartScreen may warn about an unrecognized app on first run
(More info > Run anyway).

## What it does

- **Timeline** (Today tab): each window you worked in, how long you held it, and what you
  were doing there. Search everything it has kept.
- **Summaries**: every hour (adjustable) and at the end of the day, saved as Markdown in
  the `summaries` folder.
- **Pause** from the tray / menu-bar icon: for 15 minutes, 1 hour, 4 hours or until you
  resume. Timed pauses resume by themselves.
- **Pause reminder**: if recording has been paused for 15 minutes (adjustable, 0 = never),
  a reminder offers to resume, and repeats every 15 minutes while you stay paused.
- **Start at login**, **in-app updates**, portable mode (the zip keeps everything in its
  own `data` folder).

## Choosing models

Settings > Models has two choices.

**Describe each capture with**

| Option | What happens | What leaves your computer |
|---|---|---|
| Off | The timeline keeps the on-screen text, read by the OS's own OCR (Windows OCR / Apple Vision). | Nothing |
| Local model | [Ollama](https://ollama.com) describes each capture. Run `ollama pull qwen2.5vl:3b` (fast) or `qwen2.5vl:7b` (better). | Nothing |
| Online model | An online model describes each capture. | A screenshot of every captured window |
| Mix | The local model sees every capture first and flags sensitive ones (passwords, bank details, private messages...). Those stay local; the rest are also described online, and the local model merges the two. If the local model isn't running, nothing is sent. | Screenshots of windows judged not sensitive |

Online providers: **OpenAI**, **Google Gemini**, **Groq** (API keys, stored in the system
keychain), any **OpenAI-compatible server** (LM Studio, vLLM...), or **Codex CLI** on your
ChatGPT login.

**Write summaries with**: no model (a plain summary from the timeline), the local model,
the online provider, **Claude Code** (your Claude login) or **Codex CLI** (your ChatGPT
login). Summaries send the text timeline, never screenshots.

The same provider set and key handling as [LocalFlow](https://github.com/npezarro/localflow).

## Privacy

- Everything is stored on your computer: a SQLite database, a spool folder and the
  summaries, in `%LOCALAPPDATA%\ActivityTracker` / `~/Library/Application Support/ActivityTracker`
  (or the portable `data` folder).
- **Blocklist**: matching apps and windows are never captured at all (no title, no text, no
  screenshot). The default covers password managers, sign-in and 2FA pages, banks and
  private/incognito windows. Edit it from Settings > Capture & privacy.
- Secret-shaped text (API keys, tokens, `password=...`) is redacted before anything is
  stored. That can't catch plain-English sensitive content; the blocklist is the real control.
- Screenshots are kept only until they are described, then deleted.
- No capture while you're idle (2 minutes by default) or the screen is locked.
- Screen text and descriptions are deleted after 3 days, the rest of the timeline after 30
  (both adjustable).

Full list of where data can go: [PRIVACY.md](PRIVACY.md).

## Building from source

```bash
pip install -r requirements.txt
python -m activitytracker                 # run
python -m pytest -q tests                 # tests
python -m activitytracker --selftest      # the packaged-build check CI runs
```

On macOS, build the capture helper first: `swiftc -O -o build/activity-capture capture/macos/capture-daemon.swift`.
Releases are built by GitHub Actions (`.github/workflows/build.yml`); pushing a `v*` tag
publishes one after every platform's build passes its tests.

MIT License.
