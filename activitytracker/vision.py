"""Turns captured screenshots into short descriptions of what you were doing.

The capture helper saves a downscaled JPEG of the focused window whenever it
changed, with a sidecar JSON holding the window's on-screen text. This worker takes
the oldest frame, asks the chosen model(s) to describe it, diffs the text against
the previous capture of the same window, stores a `vision` event, and deletes the
frame. Frames never outlive their description.

Modes (config "vision_mode"):
  local   the local model describes every frame. Nothing leaves the computer.
  online  the online model describes every frame (the screenshot is uploaded).
  hybrid  the local model sees every frame first and judges whether it is sensitive.
          Sensitive frames stay on this computer; the rest are also described online
          and the two descriptions are merged by the local model. Fails closed: if the
          local model can't run, the frame is skipped, never sent online.
"""
import json
import logging
import os
import sys
import threading
import time

from . import config, diff, models, store
from .scrub import scrub

log = logging.getLogger(__name__)
SOURCE = "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux"

PROMPT = ("Analyze this screenshot of one window on a computer to understand what the user is doing. "
          "In 2-3 sentences, describe the meaningful content and activity: what app or site, what they are "
          "reading, writing or watching, specific topics and names. Ignore browser chrome, tabs, bookmarks, "
          "menus and operating-system UI.")

DEFAULT_SENSITIVE_KEYWORDS = [
    "password", "api key", "apikey", "secret key", "private key", "seed phrase", "routing number",
    "account number", "social security", "ssn", "credit card", "card number", "cvv", "one-time code",
    "one time code", "2fa", "bank statement", "tax return", "medical record",
]


def sensitivity_prompt(with_change=False):
    return ("Analyze this screenshot of one window. Respond with ONLY a JSON object with %s keys: "
            '"description" (2-3 sentences on the meaningful content and activity: what app or site, what the '
            "user is reading, writing or watching, specific topics and names; ignore browser chrome, tabs, "
            'menus and OS UI), "sensitive" (true ONLY if the screen shows private information that should '
            "not leave this device: a password, API key or secret, a financial account, card or routing "
            "number, an SSN, banking or tax details, medical records, or a private personal message; "
            'otherwise false), "reason" (a short phrase for why it is sensitive, or "")%s.'
            % ("four" if with_change else "three",
               ', and "change" (ONE short sentence naming concretely what the user did or what progressed '
               "since the earlier state given below, or \"\" if nothing meaningful changed; never restate "
               "the description)" if with_change else ""))


def with_change_context(prompt, prev_description, d):
    if not prev_description:
        return prompt
    added = [l for l in (d or {}).get("added", []) if len(l) >= 8][:12]
    return (prompt + '\n\nEarlier state of this same window: "%s"\n\n' % prev_description
            + ("Text that appeared on screen since then:\n" + "\n".join(added) if added
               else "No new text appeared on screen since then."))


def parse_local_eval(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        sv = data.get("sensitive")
        return {"description": str(data.get("description") or "").strip(),
                "sensitive": sv is True or str(sv).lower() == "true",
                "reason": str(data.get("reason") or "").strip(),
                "change": str(data.get("change") or "").strip()}
    return {"description": str(raw or "").strip(), "sensitive": False, "reason": "", "change": ""}


def classify_sensitive(description, model_flag, keywords=None):
    text = (description or "").lower()
    hit = next((k for k in (keywords or DEFAULT_SENSITIVE_KEYWORDS) if k and k.lower() in text), None)
    sensitive = model_flag is True or bool(hit)
    return sensitive, ("keyword:%s" % hit if hit else "model-flagged" if model_flag is True else "")


def parse_synthesis(raw, with_change):
    text = (raw or "").strip()
    if not with_change:
        return {"summary": text, "change": ""}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {"summary": str(data.get("summary") or "").strip(), "change": str(data.get("change") or "").strip()}
    except ValueError:
        pass
    return {"summary": text, "change": ""}


class VisionWorker:
    MEMORY_MAX, MEMORY_TTL = 40, 2 * 3600

    def __init__(self, get_cfg, get_key, on_event=None):
        self.get_cfg, self.get_key, self.on_event = get_cfg, get_key, on_event or (lambda e: None)
        self.memory = {}  # window key -> (text, description, at)
        self.status = ""  # last problem, shown in the app
        self._stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="vision", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self.process_next():
                    self._stop.wait(3)
            except Exception:
                log.exception("vision worker")
                self._stop.wait(10)

    # ---- one frame ----
    def process_next(self, max_backlog=5):
        cfg = self.get_cfg()
        frame_dir = config.frame_dir(cfg)
        frames = sorted(f for f in os.listdir(frame_dir) if f.endswith(".jpg"))
        if cfg["vision_mode"] == "off":
            for f in frames:  # switched off with frames waiting: they must not linger
                _remove(frame_dir, f)
            return False
        if not frames:
            return False
        while len(frames) > max_backlog:  # stay near real time rather than describe stale frames
            _remove(frame_dir, frames.pop(0))
        frame = frames[0]
        try:
            self.handle(cfg, os.path.join(frame_dir, frame), _read_meta(frame_dir, frame))
        finally:
            _remove(frame_dir, frame)
        return True

    def handle(self, cfg, jpg, meta):
        now = time.time()
        key = diff.window_key(meta.get("proc"), meta.get("title"))
        prev = self.memory.get(key)
        if prev and now - prev[2] > self.MEMORY_TTL:
            prev = None
        has_text = isinstance(meta.get("text"), str)
        d = diff.diff_text(prev[0] if prev else None, meta.get("text") if has_text else None)
        text, vmeta = self.describe(cfg, jpg, prev[1] if prev else "", d)
        desc = scrub(text)
        if not desc:
            return None
        event_meta = {"proc": meta.get("proc") or "Unknown", "title": scrub(meta.get("title") or ""),
                      "ts": meta.get("ts")}
        event_meta.update(vmeta)
        event_meta.update(diff_meta(d, vmeta, cfg))
        store.insert(SOURCE, "vision", desc, event_meta, ts=meta.get("ts"))
        self.memory[key] = (meta.get("text") if has_text else None, desc, now)
        while len(self.memory) > self.MEMORY_MAX:
            self.memory.pop(next(iter(self.memory)))
        self.on_event({"content": desc, "meta": event_meta})
        return desc

    def describe(self, cfg, jpg, prev_description, d):
        mode = cfg["vision_mode"]
        try:
            if mode == "local":
                if prev_description:
                    local = parse_local_eval(models.ollama_generate(
                        cfg, with_change_context(sensitivity_prompt(True), prev_description, d),
                        image=jpg, json_mode=True))
                    out = (local["description"], {"visionSource": "local"})
                    if local["change"]:
                        out[1]["delta"] = local["change"]
                else:
                    out = (models.ollama_generate(cfg, PROMPT, image=jpg), {"visionSource": "local"})
            elif mode == "online":
                out = (models.online_chat(cfg, self.get_key, PROMPT, "Describe this screenshot.", image=jpg),
                       {"visionSource": "online"})
            else:
                out = self.describe_hybrid(cfg, jpg, prev_description, d)
            self.status = ""
            return out
        except Exception as exc:
            self.status = "%s model failed: %s" % (mode, str(exc)[:160])
            log.warning(self.status)
            return "", {}

    def describe_hybrid(self, cfg, jpg, prev_description, d):
        try:
            local = parse_local_eval(models.ollama_generate(
                cfg, with_change_context(sensitivity_prompt(bool(prev_description)), prev_description, d),
                image=jpg, json_mode=True))
        except Exception as exc:  # fail CLOSED: no local verdict, nothing goes online
            self.status = "local model unavailable, frame skipped (not sent online): %s" % str(exc)[:120]
            log.warning(self.status)
            return "", {"visionSource": "local-failed"}
        delta = {"delta": local["change"]} if local["change"] else {}
        sensitive, reason = classify_sensitive(local["description"], local["sensitive"],
                                               cfg.get("sensitive_keywords") or None)
        if sensitive:
            return (local["description"] or "(sensitive screen)",
                    dict(visionSource="local-sensitive", sensitive=True, sensitiveReason=reason, **delta))
        try:
            online = models.online_chat(cfg, self.get_key, PROMPT, "Describe this screenshot.", image=jpg)
        except Exception as exc:
            log.warning("hybrid online step failed, keeping the local description: %s", str(exc)[:160])
            online = ""
        if not online:
            return local["description"], dict(visionSource="local-only", sensitive=False, onlineFailed=True, **delta)
        with_change = bool(prev_description)
        base = "Two AI systems each described the same screenshot. "
        if with_change:
            prompt = with_change_context(
                base + 'Respond with ONLY a JSON object with two keys: "summary" (ONE concise 2-3 sentence '
                "summary of what the user is doing, combining the accurate specifics from both and favoring "
                'the more specific detail; do not mention there were two descriptions) and "change" (ONE '
                "short sentence naming what progressed since the earlier state below, or \"\").",
                prev_description, d)
        else:
            prompt = (base + "Write ONE concise 2-3 sentence summary of what the user is doing, combining the "
                      "accurate specifics from both and favoring the more specific detail. Do not mention "
                      "there were two descriptions.")
        prompt += "\n\nDescription A: %s\n\nDescription B: %s" % (local["description"], online)
        try:
            merged = parse_synthesis(models.ollama_generate(cfg, prompt, model=models.local_text_model(cfg),
                                                            json_mode=with_change), with_change)
        except Exception as exc:
            log.warning("hybrid merge failed, using the online description: %s", str(exc)[:160])
            merged = {"summary": "", "change": ""}
        change = merged["change"] or local["change"]
        return (merged["summary"] or online,
                dict(visionSource="hybrid" if merged["summary"] else "online", sensitive=False,
                     **({"delta": change} if change else {})))


def diff_meta(d, vmeta, cfg):
    """Diff fields for the stored event. `changeText` quotes the screen verbatim, so it
    is scrubbed, and withheld entirely on frames the sensitivity check flagged."""
    if not d or d["first_seen"]:
        return {"firstSeen": True}
    out = {"addedCount": len(d["added"]), "removedCount": len(d["removed"]),
           "changeRatio": round(d["change_ratio"], 2)}
    if vmeta.get("sensitive") is not True and cfg.get("diff_include_text", True):
        text = scrub(diff.format_delta(d))
        if text:
            out["changeText"] = text
    return out


def _read_meta(frame_dir, frame):
    try:
        with open(os.path.join(frame_dir, frame[:-4] + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _remove(frame_dir, frame):
    for name in (frame, frame[:-4] + ".json"):
        try:
            os.remove(os.path.join(frame_dir, name))
        except OSError:
            pass
