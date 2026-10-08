"""Start Activity Tracker when you sign in (Windows Run key / macOS LaunchAgent)."""
import os
import plistlib
import sys

from . import paths

LABEL = "ca.pezant.activitytracker"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command():
    if paths.is_frozen():
        return [sys.executable]
    return [sys.executable, "-m", "activitytracker"]


def _plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def is_enabled():
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, "ActivityTracker")
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return os.path.exists(_plist_path())
    return False


def set_enabled(on):
    if sys.platform == "win32":
        import subprocess
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, "ActivityTracker", 0, winreg.REG_SZ, subprocess.list2cmdline(_command()))
            else:
                try:
                    winreg.DeleteValue(k, "ActivityTracker")
                except OSError:
                    pass
    elif sys.platform == "darwin":
        path = _plist_path()
        if on:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                plistlib.dump({"Label": LABEL, "ProgramArguments": _command(), "RunAtLoad": True,
                               "ProcessType": "Interactive"}, f)
        elif os.path.exists(path):
            os.remove(path)
