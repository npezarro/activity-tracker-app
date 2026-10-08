# Spool Schema — the cross-platform capture contract

The capture side is platform-specific (macOS: the Swift helper in
`capture/macos/capture-daemon.swift`; Windows: `activitytracker/capture_win.py`).
Everything downstream (ingest, the vision worker, summaries) is platform-agnostic
and knows the capture side **only** through the files described here. If you change
a field, update this file and **both** capture implementations.

## Transport

The capture side appends newline-delimited JSON (JSONL) to the spool folder
(`<data folder>/spool` by default; Settings can point it elsewhere). Files are named
`events-YYYY-MM-DD.jsonl` (one per local calendar day). The app **polls** these files
tracking a byte offset per file rather than relying on file-change notifications,
which are unreliable on network and VM-mounted folders.

## Event object

One JSON object per line:

```json
{ "ts": "2026-07-27T14:03:11-07:00", "source": "macos", "type": "focus", "proc": "Google Chrome", "title": "my-project · GitHub" }
{ "ts": "2026-07-27T14:03:16-07:00", "source": "macos", "type": "ocr", "proc": "Google Chrome", "title": "my-project · GitHub", "text": "Pull requests Issues ... on-screen text, scrubbed and capped ~2KB" }
{ "ts": "2026-07-27T14:07:02-07:00", "source": "macos", "type": "focus_end", "proc": "Google Chrome", "title": "my-project · GitHub", "durationSec": 231 }
{ "ts": "2026-07-27T14:07:04-07:00", "source": "macos", "type": "frame", "proc": "Slack", "title": "#general", "frame": "frame-1753639624123.jpg" }
```

| field         | required   | notes |
|---------------|------------|-------|
| `ts`          | yes        | ISO 8601 with timezone offset. |
| `source`      | yes        | Platform: `macos` \| `windows`. Stored as the DB `source`. |
| `type`        | yes        | `focus` (foreground window changed) \| `ocr` (on-screen text) \| `focus_end` (optional; the window just left, with its measured dwell) \| `frame` (optional; a JPEG was written for the vision worker). |
| `proc`        | yes        | Foreground application name (e.g. `Google Chrome`, `Terminal`). |
| `title`       | yes        | Focused window title. May be `""` if the AX/title lookup failed. |
| `text`        | ocr only   | OCR'd on-screen text, secret-scrubbed at capture, capped ~2000 chars. |
| `durationSec` | focus_end  | Whole seconds the window was actually held. |
| `frame`       | frame only | Basename of the JPEG in the frame dir. |

**Optional types are optional.** Both capture implementations emit `focus_end` and
`frame` today, but a consumer must not depend on them. Every consumer must degrade cleanly
when they are absent — never treat a missing `durationSec` as `0`, since a
measured zero and "this host does not measure" are different facts.

## Frame mode and its sidecar

In frame mode (a model is chosen to describe captures) the capture side captures the **focused
window** (`ACTIVITY_CAPTURE_SCOPE=window`, falling back to the full display when
the window grab fails), writes a downscaled JPEG to the frame dir, and writes a
sidecar JSON of the same basename:

```json
{ "ts": "...", "proc": "Google Chrome", "title": "Quarterly plan", "frame": "frame-….jpg", "text": "…window OCR, scrubbed…", "scope": "window" }
```

`text` is the window's OCR at capture time. The vision worker diffs it against the
previous capture of the same window to report what actually changed, so **progress
tracking costs no second screen grab**. It is machine-local: only the derived diff
is ever rendered downstream, and it is withheld entirely on frames the sensitivity
gate flags.

**Change detection is text-first.** A pixel hash cannot see a paragraph being typed
at any grid size, so a frame is saved when the OCR text moved by
`ACTIVITY_FRAME_TEXT_CHANGE_RATIO` (default 0.02) — with a 16x16 average hash as
the fallback signal for screens with no text on them (video, images, canvases).

## Hard invariants (do not break these)

1. **Blocklist is a capture-time guarantee, not a downstream filter.** If the
   foreground window matches `blocklist.json`, the capture side writes **zero bytes** —
   no `focus` event, no `title`, no screenshot taken. The check happens before
   any I/O. See `blocklist.json`.
2. **Images never persist.** A screenshot is captured to a temp file, OCR'd, and
   deleted in a `defer`/`finally` block so it dies even if OCR throws. A PNG left
   in the spool dir is a bug, not a cache.
3. **PAUSE flag.** If a file named `PAUSED` exists in the spool dir, the capture side
   writes nothing until it is removed (checked every loop). The file may contain
   `{"since": <epoch>, "until": <epoch or 0>}` (written by the app for the pause
   reminder and timed pauses); an empty file is still a pause.
4. **Idle / locked.** No events while the session is idle (>2 min since last
   input) or the screen is locked.
5. **Retention.** Spool files older than 48h are deleted. The spool and the database
   stay on the machine.

## Consumer

`activitytracker/ingest.py` polls the spool and stores `focus`, `focus_end` and `ocr`
events (scrubbed) in SQLite; `activitytracker/vision.py` consumes the frame folder
(JPEG + sidecar) and deletes each frame once described.
