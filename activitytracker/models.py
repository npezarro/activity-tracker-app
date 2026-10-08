"""Every model the app can use, behind two calls: describe a screenshot, write text.

  local   Ollama on this computer (http://127.0.0.1:11434). Nothing leaves the machine.
  online  any OpenAI-compatible API (OpenAI, Gemini, Groq, or your own server) with an
          API key from the OS keychain, or "codex" = the Codex CLI on your ChatGPT login.
  claude  Claude Code on your Claude login (text only: summaries).
Same provider set and key storage as LocalFlow. Stdlib HTTP only."""
import base64
import json
import logging
import re
import urllib.error
import urllib.request

from . import clis

log = logging.getLogger(__name__)

ONLINE_PRESETS = {
    "openai": {"label": "OpenAI (API key)", "base_url": "https://api.openai.com/v1", "model": "gpt-5.4-mini",
               "key_name": "openai_api_key", "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Google Gemini (API key)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-3.8-flash", "key_name": "gemini_api_key", "key_url": "https://aistudio.google.com/apikey"},
    "groq": {"label": "Groq (API key, free tier)", "base_url": "https://api.groq.com/openai/v1",
             "model": "qwen/qwen3.8-27b", "key_name": "groq_api_key", "key_url": "https://console.groq.com/keys"},
    "custom": {"label": "Custom OpenAI-compatible server", "base_url": "http://localhost:1234/v1", "model": "",
               "key_name": "custom_api_key", "key_url": ""},
    "codex": {"label": "ChatGPT login (Codex CLI)", "base_url": "", "model": "", "key_name": "", "key_url": ""},
}


class ModelError(RuntimeError):
    pass


def _post_json(url, payload, headers=None, timeout=120):
    h = {"Content-Type": "application/json", "User-Agent": "ActivityTracker"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ModelError("HTTP %d from %s: %s" % (exc.code, url.split("?")[0], detail)) from exc
    except urllib.error.URLError as exc:
        raise ModelError("cannot reach %s: %s" % (url.split("/v1")[0], exc.reason)) from exc


def clean(text):
    """Strip reasoning blocks and code fences some models wrap answers in."""
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    return re.sub(r"^```\w*\n?|\n?```$", "", text.strip()).strip()


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


# ---------------- local (Ollama) ----------------

def ollama_generate(cfg, prompt, model=None, image=None, json_mode=False, timeout=120):
    payload = {"model": model or cfg["local_vision_model"], "prompt": prompt, "stream": False,
               "options": {"temperature": 0.1}}
    if image:
        payload["images"] = [_b64(image)]
    if json_mode:
        payload["format"] = "json"
    data = _post_json(cfg["local_endpoint"].rstrip("/") + "/api/generate", payload, timeout=timeout)
    if data.get("error"):
        raise ModelError("local model: %s" % data["error"])
    return clean(data.get("response", ""))


def ollama_models(cfg, timeout=5):
    """Installed local models, or raises when Ollama isn't running."""
    url = cfg["local_endpoint"].rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ActivityTracker"}),
                                    timeout=timeout) as resp:
            return [m["name"] for m in json.load(resp).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ModelError("Ollama isn't running at %s (%s)" % (cfg["local_endpoint"], exc)) from exc


def local_text_model(cfg):
    return cfg.get("local_text_model") or cfg["local_vision_model"]


# ---------------- online ----------------

def preset(cfg):
    return ONLINE_PRESETS.get(cfg.get("online_provider"), ONLINE_PRESETS["openai"])


def online_chat(cfg, get_key, system, user, image=None, timeout=120):
    provider = cfg.get("online_provider", "openai")
    if provider == "codex":
        prompt = (system + "\n\n" + user) if user else system
        return clean(clis.run_codex(cfg, prompt, image=image, timeout=timeout))
    p = preset(cfg)
    base = (cfg.get("online_base_url") if provider == "custom" else "") or p["base_url"]
    model = cfg.get("online_model") or p["model"]
    if not model:
        raise ModelError("choose a model name for the custom server")
    content = user
    if image:
        content = [{"type": "text", "text": user},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(image)}}]
    payload = {"model": model, "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": content}]}
    if not model.startswith(("gpt-5", "o1", "o3", "o4")):  # reasoning models reject temperature
        payload["temperature"] = 0.2
    key = get_key(p["key_name"]) if p["key_name"] else ""
    if not key and provider != "custom":
        raise ModelError("no API key saved for %s" % p["label"])
    headers = {"Authorization": "Bearer " + key} if key else {}
    data = _post_json(base.rstrip("/") + "/chat/completions", payload, headers, timeout)
    try:
        return clean(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError("unexpected response: %s" % str(data)[:200]) from exc


# ---------------- one entry point for text ----------------

def write_text(engine, cfg, get_key, system, user, timeout=120):
    """Text generation through the chosen summary engine."""
    if engine == "local":
        return ollama_generate(cfg, system + "\n\n" + user, model=local_text_model(cfg), timeout=timeout)
    if engine == "online":
        return online_chat(cfg, get_key, system, user, timeout=timeout)
    if engine == "claude":
        return clean(clis.run_claude(cfg, system, user, timeout))
    if engine == "codex":
        return clean(clis.run_codex(cfg, system + "\n\nThe input is in the <stdin> block.", stdin_text=user,
                                    timeout=timeout))
    raise ModelError("no model chosen for summaries")


def test_connection(kind, cfg, get_key):
    """Settings 'Test' buttons. -> (ok, message)."""
    try:
        if kind == "local":
            names = ollama_models(cfg)
            want = cfg["local_vision_model"]
            if not any(n == want or n.split(":")[0] == want for n in names):
                return False, "Ollama is running but %s isn't installed. Run: ollama pull %s" % (want, want)
            return True, "Ollama OK; %d model(s) installed" % len(names)
        if kind == "online":
            out = online_chat(cfg, get_key, "Reply with the single word: ready", "ping", timeout=40)
            return True, "Online model answered: %s" % out[:60]
        if kind in ("claude", "codex"):
            return clis.check(kind, cfg)
    except Exception as exc:
        return False, str(exc)[:300]
    return False, "unknown"
