"""Settings, stored as data/config.json. Unknown keys are dropped on load, so a
setting removed in a later version can't linger and confuse anyone."""
import json
import os

from . import paths

DEFAULTS = {
    # ---- Capture ----
    "capture_enabled": True,
    "spool_dir": "",  # blank = data/spool (the capture helper's drop folder)
    "frame_interval_s": 30,  # with a vision model: describe the focused window at most this often
    "ocr_interval_s": 2,  # without one: re-read the same window's text at most this often
    "idle_seconds": 120,  # no capture after this long without keyboard/mouse input
    "capture_scope": "window",  # "window" (focused window) or "display" (whole screen)
    # ---- Pause ----
    "pause_until": 0,  # epoch seconds; 0 = not a timed pause
    "nudge_after_min": 15,  # remind me to resume after this long paused (0 = never)
    "nudge_repeat_min": 15,  # and again this often while still paused
    "nudge_timed_pauses": False,  # also nudge during "pause for 1 hour" style pauses
    # ---- Models: what describes each screenshot ----
    # "off"    = no model; the timeline keeps on-screen text read on this computer (OCR)
    # "local"  = a model on this computer (Ollama); nothing leaves the machine
    # "online" = an online model (API key or ChatGPT/Codex login) describes every capture
    # "hybrid" = the local model sees every capture first; only captures it judges
    #            NOT sensitive also go to the online model, and the two are merged locally
    "vision_mode": "off",
    "local_endpoint": "http://127.0.0.1:11434",
    "local_vision_model": "qwen2.5vl:3b",
    "local_text_model": "",  # blank = reuse the vision model for text (summaries, merging)
    "online_provider": "openai",  # openai | gemini | groq | custom | codex
    "online_base_url": "",  # custom provider only
    "online_model": "",  # blank = provider default
    "codex_model": "",
    "codex_path": "",
    "sensitive_keywords": [],  # [] = built-in list (passwords, bank details, medical...)
    "diff_include_text": True,  # quote the lines that appeared on screen in the timeline
    # ---- Models: what writes the summaries ----
    # "off" (plain timeline summary, no model) | "local" | "online" | "claude" | "codex"
    "summary_engine": "off",
    "summary_interval_min": 60,
    "daily_summary_hour": 23,
    "claude_model": "haiku",
    "claude_path": "",
    # ---- Storage ----
    "retention_days": 30,
    "text_retention_days": 3,  # on-screen text and descriptions are the bulky, sensitive rows
    # ---- App ----
    "start_at_login": False,
    "auto_update_check": True,
    "last_update_check": 0,
    "setup_seen": False,
}

VISION_MODES = ("off", "local", "online", "hybrid")
SUMMARY_ENGINES = ("off", "local", "online", "claude", "codex")


def config_path():
    return os.path.join(paths.data_dir(), "config.json")


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except FileNotFoundError:
        save(cfg)
    except (OSError, ValueError):
        pass  # corrupt config: run on defaults, don't overwrite the user's file
    return cfg


def save(cfg):
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, config_path())


def spool_dir(cfg):
    path = cfg.get("spool_dir") or os.path.join(paths.data_dir(), "spool")
    os.makedirs(path, exist_ok=True)
    return path


def frame_dir(cfg):
    path = os.path.join(spool_dir(cfg), "frames")
    os.makedirs(path, exist_ok=True)
    return path
