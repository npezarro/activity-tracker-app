# Where your data can go

Activity Tracker reads your screen, so every place data leaves the computer is listed
here. Anything not listed stays on your computer.

| Data | Destination | When | Guard |
|---|---|---|---|
| Window titles, on-screen text, timeline | Your computer only (`activity.db`, `spool/`) | Always | Blocklist (zero bytes for blocked windows), secret scrub, retention |
| Screenshots of the focused window | Your computer, until described, then deleted | A model is chosen | Blocklist, idle/lock/pause gates |
| Screenshots | **Online provider** (OpenAI / Gemini / Groq / your server / OpenAI via Codex) | Describe = **Online** | Blocklist only |
| Screenshots | **Online provider** | Describe = **Mix**, and the local model judged the window not sensitive | Local sensitivity check (model verdict + keyword list), fails closed, plus the blocklist |
| Text timeline (titles, descriptions, scrubbed changed lines) | **Online provider**, or Anthropic via Claude Code, or OpenAI via Codex | Summaries = online / Claude Code / Codex | Secret scrub; the prompt forbids repeating credentials |
| Update check | api.github.com / github.com | Once a day if enabled, or when you ask | Sends nothing but the request |

Notes:

- The sensitivity check in Mix mode is a model's judgement plus a keyword scan. It is
  better than nothing, not a guarantee. Put anything you never want captured in the
  blocklist.
- The secret scrub removes strings shaped like keys and passwords. It can't recognise a
  bank balance or a medical note written in plain words.
- API keys are kept in Windows Credential Manager / the macOS Keychain. If neither is
  available they are saved in `secrets.json` in the data folder, and the app tells you.
