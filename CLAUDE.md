# Activity Tracker

Local-first activity timeline for Windows + macOS. Python 3.12, Tk UI, pystray (Windows
tray) / NSStatusItem via pyobjc (macOS menu bar), SQLite, PyInstaller one-folder builds.
Release, update and model-provider machinery mirror LocalFlow (npezarro/localflow).

## Layout
- `activitytracker/app.py` Tk window (Today / Models / Capture & privacy / About), tray, background loop, pause reminder, updater UI. Threads talk to Tk only through `ui_q`.
- `capture_mac.py` runs the Swift helper (`capture/macos/capture-daemon.swift`, compiled by CI into the bundle) as a child process; `capture_win.py` is the in-process Windows loop (ctypes + Pillow + Windows.Media.Ocr via pywinrt).
- `ingest.py` spool -> SQLite (scrub at ingest); `vision.py` frame -> description (off/local/online/hybrid); `summary.py` sessions + summaries; `models.py` + `clis.py` providers; `pause.py` PAUSED flag + reminder logic (pure).
- `update.py` GitHub-releases updater; `selftest.py` packaged-build check (`--selftest`), `app.App._smoke` (`--smoke-ui`).
- Contract between capture and everything else: `SPOOL-SCHEMA.md`. Egress table: `PRIVACY.md`.

## Rules
- Public repo (MIT). No personal or infrastructure details in code, tests, docs or commit messages. Working notes (context.md / progress.md) are gitignored.
- Asset names are fixed (`ActivityTracker-Setup-x64.exe`, `-windows-x64.zip`, `-macos-{arm64,intel}.{zip,dmg}`): README links and the updater use `releases/latest/download/<name>`.
- Release flow: push main, wait for CI green, tag `vX.Y.Z` (bump `activitytracker/__init__.py` first). The release job publishes only after all three platforms pass.
- macOS signing: every build is signed with the same self-signed identity (secrets `MACOS_SELF_SIGN_P12` / `MACOS_SELF_SIGN_PASSWORD`, CN "Activity Tracker Self-Signed") so Screen Recording / Accessibility grants survive updates. Never fall back to ad-hoc for a release. The helper is a child of the app, so TCC attributes it to the .app.
- Privacy invariants (see SPOOL-SCHEMA.md): blocklist match => zero bytes; PAUSED halts capture; no capture idle/locked; frames deleted once described; hybrid mode fails closed (no local verdict => nothing online); `changeText` withheld on sensitive frames. Any new output that leaves the machine must be added to PRIVACY.md.
- Scrub patterns live in two places (`scrub.py`, `scrub()` in the Swift helper); keep them in sync.
- Tests: `python -m pytest -q tests`; `python -m activitytracker --selftest`; UI smoke under Xvfb on Linux: `xvfb-run -a python -m activitytracker --smoke-ui out.json`.
- Windows OCR needs an OCR language pack (normally present with the display language); without it capture still records windows, and the app says OCR is unavailable.
