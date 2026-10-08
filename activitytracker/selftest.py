"""Headless check CI runs against every packaged build: storage, the spool -> database
path, the vision worker (with a stand-in model), OCR on this platform, a real screen
grab (Windows), HTTPS, and the keychain. Writes JSON to --out (the Windows build has no
console) and exits non-zero on failure."""
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import tempfile
import time

from . import __version__, config, ingest, keystore, pause, paths, store, summary, vision
from .scrub import scrub

PHRASE = "Activity Tracker selftest 4217"


def _phrase_image(path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1100, 220), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:
        font = ImageFont.load_default()
    d.text((40, 70), PHRASE, fill="black", font=font)
    img.save(path)
    return img


def _ocr_check(result, work):
    png = os.path.join(work, "phrase.png")
    img = _phrase_image(png)
    if sys.platform == "win32":
        from . import capture_win

        text = capture_win.ocr(img)
        result["ocr_engine"] = "windows" if capture_win.ocr_available() else "unavailable"
        shot = capture_win.grab(None, "display")
        result["screen_grab"] = "%dx%d" % shot.size
    elif sys.platform == "darwin":
        from . import capture_mac

        helper = capture_mac.helper_path()
        result["helper"] = helper or "missing"
        if not helper:
            raise RuntimeError("capture helper missing from the build")
        out = subprocess.run([helper, "--ocr-file", png], capture_output=True, text=True, timeout=60)
        text = out.stdout
        result["ocr_engine"] = "apple-vision"
        perms = subprocess.run([helper, "--check"], capture_output=True, text=True, timeout=30)
        result["permissions"] = perms.stdout.strip().replace("\n", "; ")
    else:
        result["ocr_engine"] = "none on this platform"
        return True
    result["ocr_text"] = text.strip()[:200]
    return "4217" in text and "selftest" in text.lower()


def run(argv):
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    result = {"version": __version__, "platform": platform.platform(), "machine": platform.machine(),
              "python": sys.version.split()[0], "data_dir": paths.data_dir(), "frozen": paths.is_frozen(),
              "ok": False}
    work = tempfile.mkdtemp(prefix="activitytracker-selftest-")
    os.environ["ACTIVITYTRACKER_DB"] = os.path.join(work, "selftest.db")
    try:
        # spool -> database, with scrubbing at ingest
        spool = os.path.join(work, "spool")
        os.makedirs(spool)
        now = dt.datetime.now().astimezone()
        lines = [
            {"ts": now.isoformat(), "source": "selftest", "type": "focus", "proc": "Editor", "title": "notes.txt"},
            {"ts": now.isoformat(), "source": "selftest", "type": "ocr", "proc": "Editor", "title": "notes.txt",
             "text": "draft plan\napi_key=sk-abcdefghijklmnopqrstu"},
            {"ts": now.isoformat(), "source": "selftest", "type": "focus_end", "proc": "Editor", "title": "notes.txt",
             "durationSec": 95},
        ]
        with open(os.path.join(spool, "events-%s.jsonl" % now.date().isoformat()), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        n = ingest.Ingestor(spool).poll()
        rows = store.events_between(now - dt.timedelta(minutes=1))
        result["ingested"] = n
        leaked = any("sk-abc" in r["content"] for r in rows)
        if n != 3 or leaked:
            raise RuntimeError("ingest stored %d events (want 3), secret leaked: %s" % (n, leaked))
        sess = summary.sessions(rows)
        result["session_seconds"] = sess[0]["seconds"] if sess else None

        # vision worker end to end, with a stand-in model (no network)
        cfg = dict(config.DEFAULTS, vision_mode="local")
        frame = os.path.join(work, "frame.jpg")
        _phrase_image(frame.replace(".jpg", ".png")).save(frame, "JPEG")
        worker = vision.VisionWorker(lambda: cfg, lambda name: "")
        vision.models.ollama_generate = lambda *a, **k: "Editing a planning document."
        desc = worker.handle(cfg, frame, {"proc": "Editor", "title": "plan", "text": "a\nb", "ts": now.isoformat()})
        result["vision"] = desc
        if desc != "Editing a planning document.":
            raise RuntimeError("vision worker did not store a description: %r" % desc)

        # pause flag round trip
        pause.pause(spool, 15)
        st = pause.state(spool)
        pause.resume(spool)
        if not st or pause.state(spool) is not None:
            raise RuntimeError("pause flag round trip failed")

        result["scrub"] = scrub("token=abc123") == "token=[REDACTED]"
        result["ocr_ok"] = _ocr_check(result, work)
        if not result["ocr_ok"]:
            raise RuntimeError("OCR didn't read the test phrase: %r" % result.get("ocr_text"))

        result["tls_cafile"] = os.environ.get("SSL_CERT_FILE", "")
        try:
            import urllib.request

            with urllib.request.urlopen("https://github.com/npezarro/activity-tracker-app", timeout=20) as resp:
                result["https"] = "ok %d" % resp.status
        except Exception as exc:
            result["https"] = "failed: %s" % exc
        if sys.platform == "darwin" and paths.is_frozen() and not result["https"].startswith("ok"):
            raise RuntimeError("HTTPS failed in the packaged app: %s" % result["https"])
        result["keychain"] = keystore.backend_name() or "none (data/secrets.json fallback)"
        result["ok"] = True
    except Exception:
        import traceback

        result["error"] = traceback.format_exc()
    finally:
        store.close()
    payload = json.dumps(result, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload)
    if sys.stdout:
        print(payload)
    time.sleep(0.2)
    return 0 if result["ok"] else 1
