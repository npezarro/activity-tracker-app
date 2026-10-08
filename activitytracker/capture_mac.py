"""macOS capture: runs the bundled Swift helper (capture/macos/capture-daemon.swift),
which reads the focused window, OCRs it with Apple's Vision framework and writes the
spool. The helper is a child of the app, so macOS attributes its Screen Recording and
Accessibility use to Activity Tracker.app: one grant, kept across updates because every
release is signed with the same certificate."""
import json
import logging
import os
import subprocess
import threading
import time

from . import blocklist, config, paths

log = logging.getLogger(__name__)
SETTINGS_URLS = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}


def helper_path():
    for p in (paths.resource("capture", "activity-capture"),
              os.path.join(paths.install_root(), "build", "activity-capture")):
        if os.path.isfile(p):
            return p
    return None


def helper_env(cfg):
    spool = config.spool_dir(cfg)
    env = dict(os.environ)
    env.update({
        "ACTIVITY_SPOOL_DIR": spool,
        "ACTIVITY_BLOCKLIST": blocklist.user_path(spool),
        "ACTIVITY_CAPTURE_MODE": "frame" if cfg["vision_mode"] != "off" else "ocr",
        "ACTIVITY_FRAME_DIR": config.frame_dir(cfg),
        "ACTIVITY_FRAME_INTERVAL": str(cfg["frame_interval_s"]),
        "ACTIVITY_OCR_INTERVAL": str(cfg["ocr_interval_s"]),
        "ACTIVITY_IDLE_SECONDS": str(cfg["idle_seconds"]),
        "ACTIVITY_CAPTURE_SCOPE": cfg["capture_scope"],
        "ACTIVITY_PARENT_PID": str(os.getpid()),
    })
    return env


class Capture:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self.proc = None
        self.problem = ""
        self._want = False
        self._lock = threading.Lock()
        self._restarts = []
        threading.Thread(target=self._supervise, name="capture-supervisor", daemon=True).start()

    def start(self):
        self._want = True
        self._spawn()

    def stop(self):
        self._want = False
        self._kill()

    def restart(self):
        self._kill()
        if self._want:
            self._spawn()

    def _spawn(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            helper = helper_path()
            if not helper:
                self.problem = "capture helper missing from this build"
                return
            log_path = os.path.join(paths.data_dir(), "capture.log")
            out = open(log_path, "ab")
            self.proc = subprocess.Popen([helper], env=helper_env(self.get_cfg()), stdout=out, stderr=out,
                                         stdin=subprocess.DEVNULL)
            out.close()
            log.info("capture helper started (pid %d)", self.proc.pid)

    def _kill(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def _supervise(self):
        """The helper exits on purpose when its loop stalls; bring it back, but not in a tight loop."""
        while True:
            time.sleep(5)
            if self._want and self.proc is not None and self.proc.poll() is not None:
                now = time.time()
                self._restarts = [t for t in self._restarts if now - t < 600] + [now]
                if len(self._restarts) > 5:
                    self.problem = "capture helper keeps stopping; see capture.log"
                    time.sleep(60)
                log.warning("capture helper exited (%s); restarting", self.proc.returncode)
                self.proc = None
                self._spawn()

    def permissions(self):
        """-> {"screen": bool, "accessibility": bool} as last reported by the helper, or {}."""
        try:
            with open(os.path.join(config.spool_dir(self.get_cfg()), ".daemon-status"), encoding="utf-8") as f:
                data = json.load(f)
            return {"screen": bool(data.get("screenRecording")), "accessibility": bool(data.get("accessibility"))}
        except (OSError, ValueError):
            return {}

    def status(self):
        running = bool(self.proc and self.proc.poll() is None)
        problem = self.problem
        perms = self.permissions()
        missing = [n for n, ok in (("Screen Recording", perms.get("screen", True)),
                                   ("Accessibility", perms.get("accessibility", True))) if not ok]
        if missing:
            problem = "macOS permission needed: " + " and ".join(missing)
        return {"running": running, "problem": problem, "missing": missing}


def open_permission_settings(which):
    subprocess.Popen(["open", SETTINGS_URLS[which]])
