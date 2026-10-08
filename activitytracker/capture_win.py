"""Windows capture: the same loop as the macOS helper, in-process.

Every second: read the foreground window and its program. On a window change, write a
`focus` event (and a `focus_end` with the measured time in the previous window). Then
either OCR the window with the built-in Windows OCR engine (no model chosen) or save a
downscaled, change-detected JPEG plus its OCR text for the vision worker.

Same invariants as the macOS helper (see SPOOL-SCHEMA.md): a blocklisted window gets
zero bytes; the screenshot only ever exists in memory; PAUSED halts capture; nothing
is captured while idle or locked. Windows doesn't gate screen capture, so there is
nothing to grant."""
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import logging
import os
import threading
import time

from . import blocklist, config, diff, pause
from .scrub import scrub

log = logging.getLogger(__name__)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
OWN_PROCS = ("activitytracker",)  # never capture our own windows
MAX_TEXT = 2000
FRAME_MAX_WIDTH = 1400
FRAME_MIN_GAP = 10
TEXT_CHANGE_RATIO = 0.02
HASH_CHANGE_BITS = 10
SPOOL_RETENTION_H = 48


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds():
    lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    return ((kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def is_locked():
    """The secure desktop (lock screen, UAC) can't be opened by a normal process."""
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    user32.CloseDesktop(desk)
    return False


def foreground():
    """-> (hwnd, program name, title) or None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = "Unknown"
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            size = wt.DWORD(1024)
            pbuf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                name = os.path.splitext(os.path.basename(pbuf.value))[0]
        finally:
            kernel32.CloseHandle(h)
    return hwnd, name, buf.value


def window_rect(hwnd):
    """Visible bounds (without the invisible resize border) in screen pixels."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def grab(hwnd, scope):
    from PIL import ImageGrab

    if scope == "window":
        l, t, r, b = window_rect(hwnd)
        if r - l >= 120 and b - t >= 80:
            try:
                return ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
            except OSError:
                pass
    return ImageGrab.grab()


# ---------------- OCR (Windows.Media.Ocr through the WinRT projection) ----------------

_engine = None


def ocr_available():
    try:
        return _get_engine() is not None
    except Exception:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        from winrt.windows.media.ocr import OcrEngine

        _engine = OcrEngine.try_create_from_user_profile_languages()
    return _engine


def ocr(img):
    """PIL image -> text ('' if OCR is unavailable)."""
    try:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        engine = _get_engine()
        if engine is None:
            return ""
        limit = _max_dimension()
        if max(img.size) > limit:
            scale = limit / max(img.size)
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        rgba = img.convert("RGBA")
        writer = DataWriter()
        writer.write_bytes(rgba.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.RGBA8,
                                                        rgba.width, rgba.height)
        result = asyncio.run(_recognize(engine, bitmap))
        return "\n".join(line.text for line in result.lines)
    except Exception:
        log.warning("Windows OCR failed", exc_info=True)
        return ""


def _max_dimension():
    try:
        from winrt.windows.media.ocr import OcrEngine

        return int(OcrEngine.max_image_dimension)  # a static property in WinRT
    except Exception:
        return 2600


async def _recognize(engine, bitmap):
    return await engine.recognize_async(bitmap)


# ---------------- change detection ----------------

def avg_hash(img):
    small = img.convert("L").resize((16, 16))
    px = list(small.getdata())
    mean = sum(px) / len(px)
    return [p > mean for p in px]


def hamming(a, b):
    return sum(x != y for x, y in zip(a, b)) if a and b else 256


def text_change_ratio(a, b):
    return diff.diff_text(a, b)["change_ratio"]


# ---------------- the loop ----------------

def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Capture:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self._stop = threading.Event()
        self.thread = None
        self.problem = ""
        self.last_tick = 0

    # same surface as capture_mac.Capture
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def restart(self):
        pass  # reads its settings every loop

    def status(self):
        running = bool(self.thread and self.thread.is_alive())
        problem = self.problem or ("" if running or self._stop.is_set() else "capture stopped; see the log")
        return {"running": running, "problem": problem, "missing": []}

    def _run(self):
        while not self._stop.is_set():
            try:
                self._loop()
            except Exception:
                log.exception("capture loop crashed; restarting in 10 s")
                self.problem = "capture hit an error; retrying"
                self._stop.wait(10)

    def _write(self, spool, event):
        path = os.path.join(spool, "events-%s.jsonl" % dt.date.today().isoformat())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _prune(self, spool):
        cutoff = time.time() - SPOOL_RETENTION_H * 3600
        for name in os.listdir(spool):
            if name.startswith("events-") and name.endswith(".jsonl"):
                p = os.path.join(spool, name)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass

    def _loop(self):
        last_key = ""
        focus = None  # (proc, safe_title, since)
        last_ocr_at = 0
        last_ocr_text = ""
        last_frame_at = 0
        last_frame_hash, last_frame_text = [], ""
        bl, bl_loaded, bl_path = None, 0, None
        last_prune = 0
        self.problem = "" if ocr_available() else "Windows OCR unavailable (add an OCR language in Windows Settings)"
        while not self._stop.is_set():
            self.last_tick = time.time()
            cfg = self.get_cfg()
            spool = config.spool_dir(cfg)
            now = time.time()
            path = blocklist.user_path(spool)
            if bl is None or path != bl_path or now - bl_loaded > 60:
                bl, bl_loaded, bl_path = blocklist.Blocklist.load(path), now, path
            if now - last_prune > 3600:
                self._prune(spool)
                last_prune = now
            paused = pause.state(spool) is not None or not cfg.get("capture_enabled", True)
            if paused or is_locked() or idle_seconds() > cfg["idle_seconds"]:
                if focus:
                    self._end_focus(spool, focus)
                    focus, last_key = None, ""
                self._stop.wait(1)
                continue
            fg = foreground()
            if not fg:
                self._stop.wait(1)
                continue
            hwnd, proc, title = fg
            if proc.lower() in OWN_PROCS or bl.blocks(proc, title):
                if focus:
                    self._end_focus(spool, focus)
                focus, last_key = None, ""
                self._stop.wait(1)
                continue
            safe_title = scrub(title)
            key = "%s\x01%s" % (proc, title)
            changed = key != last_key
            if changed:
                if focus:
                    self._end_focus(spool, focus)
                self._write(spool, {"ts": now_iso(), "source": "windows", "type": "focus", "proc": proc,
                                    "title": safe_title})
                focus, last_key = (proc, safe_title, now), key
                last_frame_hash, last_frame_text, last_ocr_text = [], "", ""
            if cfg["vision_mode"] != "off":
                gap = now - last_frame_at
                if gap >= cfg["frame_interval_s"] or (changed and gap >= FRAME_MIN_GAP):
                    last_frame_at = now
                    saved = self._frame(cfg, spool, hwnd, proc, safe_title, changed, last_frame_hash, last_frame_text)
                    if saved:
                        last_frame_hash, last_frame_text = saved
            elif changed or now - last_ocr_at >= cfg["ocr_interval_s"]:
                last_ocr_at = now
                img = grab(hwnd, cfg["capture_scope"])
                text = scrub(ocr(img))[:MAX_TEXT]
                del img
                if text and text != last_ocr_text:
                    last_ocr_text = text
                    self._write(spool, {"ts": now_iso(), "source": "windows", "type": "ocr", "proc": proc,
                                        "title": safe_title, "text": text})
            self._stop.wait(1)
        if focus:
            self._end_focus(config.spool_dir(self.get_cfg()), focus)

    def _end_focus(self, spool, focus):
        proc, title, since = focus
        dwell = int(round(time.time() - since))
        if dwell > 0:
            self._write(spool, {"ts": now_iso(), "source": "windows", "type": "focus_end", "proc": proc,
                                "title": title, "durationSec": dwell})

    def _frame(self, cfg, spool, hwnd, proc, title, force, last_hash, last_text):
        img = grab(hwnd, cfg["capture_scope"])
        try:
            text = scrub(ocr(img))
            h = avg_hash(img)
            if not force:
                pixel_changed = not last_hash or hamming(h, last_hash) >= HASH_CHANGE_BITS
                moved = pixel_changed if not text else (text_change_ratio(last_text, text) >= TEXT_CHANGE_RATIO
                                                        or pixel_changed)
                if not moved:
                    return None
            frame_dir = config.frame_dir(cfg)
            base = "frame-%d" % int(time.time() * 1000)
            if img.width > FRAME_MAX_WIDTH:
                img = img.resize((FRAME_MAX_WIDTH, int(img.height * FRAME_MAX_WIDTH / img.width)))
            img.convert("RGB").save(os.path.join(frame_dir, base + ".jpg"), "JPEG", quality=70)
            ts = now_iso()
            with open(os.path.join(frame_dir, base + ".json"), "w", encoding="utf-8") as f:
                json.dump({"ts": ts, "proc": proc, "title": title, "frame": base + ".jpg", "text": text,
                           "scope": cfg["capture_scope"]}, f)
            self._write(spool, {"ts": ts, "source": "windows", "type": "frame", "proc": proc, "title": title,
                                "frame": base + ".jpg"})
            return h, text
        finally:
            del img
