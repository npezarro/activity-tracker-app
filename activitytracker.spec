# PyInstaller spec: one-folder build (portable: unzip and run).
import os
import sys

sys.path.insert(0, SPECPATH)  # noqa: F821 (injected by PyInstaller)

from PyInstaller.utils.hooks import collect_submodules

from activitytracker import __version__

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

datas = [("assets/icon.png", "assets"), ("capture/blocklist.json", "capture")]
binaries = []
if IS_WIN:
    hidden = ["pystray._win32", "PIL.ImageGrab"] + collect_submodules("winrt")
elif IS_MAC:
    # the Swift capture helper, compiled by CI (swiftc) before this spec runs
    binaries.append(("build/activity-capture", "capture"))
    hidden = ["AppKit", "Foundation", "objc"]
else:
    hidden = []

a = Analysis(
    ["run_activitytracker.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "numpy", "pandas", "scipy", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ActivityTracker",
    console=False,
    target_arch=None,
    icon="assets/icon.ico" if IS_WIN else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ActivityTracker")
if IS_MAC:
    app = BUNDLE(
        coll,
        name="ActivityTracker.app",
        bundle_identifier="ca.pezant.activitytracker",
        icon="build/icon.icns" if os.path.exists("build/icon.icns") else None,
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "Activity Tracker",
            "CFBundleShortVersionString": __version__,
            "NSAppleEventsUsageDescription": "Activity Tracker asks your browser whether the front window is "
                                             "private/incognito, so private windows are never captured.",
            "NSScreenCaptureUsageDescription": "Activity Tracker reads the window you are working in to keep "
                                               "a private record of your activity on this Mac.",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",
        },
    )
