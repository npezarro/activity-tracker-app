"""Battery mode. Plugged in, capture runs at the configured cadence. On battery the
expensive work (screenshot + OCR, or frames for a vision model) runs less often and
periodic model summaries wait for the charger. The 1 s focus poll is cheap and is
left alone, so window switches and time-in-window keep their resolution, and a
window switch still captures at once.

On macOS the Swift helper reads the power source itself (ACTIVITY_POWER_AWARE) and
reports it in the spool's .daemon-status; on Windows the capture loop asks
GetSystemPowerStatus. See SPOOL-SCHEMA.md ("Power")."""
import json
import os
import sys
import time

AC, BATTERY, LOW_POWER = "ac", "battery", "low-power"

BATTERY_OCR_S = 15  # re-read the same window this often on battery
LOW_POWER_OCR_S = 30  # ...and this often with Windows battery saver / macOS Low Power Mode on
BATTERY_FRAME_S = 60  # with a vision model: describe at most this often on battery
STATUS_MAX_AGE_S = 2 * 3600  # the helper rewrites .daemon-status at least hourly


def intervals(cfg, state):
    """-> (ocr_interval_s, frame_interval_s) for this power state. max() keeps a longer
    interval the user chose for plugged-in use."""
    ocr, frame = cfg["ocr_interval_s"], cfg["frame_interval_s"]
    if state == AC or not cfg.get("battery_saver", True):
        return ocr, frame
    return max(ocr, LOW_POWER_OCR_S if state == LOW_POWER else BATTERY_OCR_S), max(frame, BATTERY_FRAME_S)


def from_windows_status(ac_line, status_flag):
    """SYSTEM_POWER_STATUS fields -> state. ACLineStatus 0 = battery (1 = AC, 255 =
    unknown, treated as AC); SystemStatusFlag 1 = battery saver on."""
    if ac_line != 0:
        return AC
    return LOW_POWER if status_flag == 1 else BATTERY


def windows_state():
    import ctypes
    import ctypes.wintypes as wt

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", wt.BYTE), ("BatteryFlag", wt.BYTE), ("BatteryLifePercent", wt.BYTE),
                    ("SystemStatusFlag", wt.BYTE), ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]

    st = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return AC
    return from_windows_status(st.ACLineStatus, st.SystemStatusFlag)


def helper_state(spool, now=None):
    """Power state the macOS helper last reported. Missing, unreadable or stale => AC."""
    path = os.path.join(spool, ".daemon-status")
    try:
        if (now or time.time()) - os.path.getmtime(path) > STATUS_MAX_AGE_S:
            return AC
        with open(path, encoding="utf-8") as f:
            power = json.load(f).get("power")
    except (OSError, ValueError, AttributeError):
        return AC
    return power if power in (BATTERY, LOW_POWER) else AC


def state(spool):
    try:
        if sys.platform == "win32":
            return windows_state()
        if sys.platform == "darwin":
            return helper_state(spool)
    except Exception:
        pass
    return AC
