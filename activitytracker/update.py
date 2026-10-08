"""Update Activity Tracker from its GitHub releases, from inside the app.

1. Ask GitHub for the latest release (api.github.com/repos/<repo>/releases/latest).
2. Pick the file matching how this copy was installed:
     Windows installer copy  -> ActivityTracker-Setup-x64.exe (upgrades in place, settings kept)
     Windows portable zip    -> ActivityTracker-windows-x64.zip (files replaced, data/ kept)
     macOS (dmg or zip)      -> ActivityTracker-macos-<arm64|intel>.zip (the .app is replaced)
3. Download it and check its SHA-256 against the digest GitHub publishes for the file.
4. Hand over to the installer / a small helper script that waits for Activity Tracker to quit,
   swaps the files in, and starts the new version.
Your data folder is never touched by an update.
"""
import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from . import __version__, paths

log = logging.getLogger(__name__)
REPO = "npezarro/activity-tracker-app"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO
IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"


def current_version():
    return os.environ.get("ACTIVITYTRACKER_PRETEND_VERSION") or __version__  # test hook


def parse(v):
    out = []
    for part in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def is_newer(latest, current):
    return parse(latest) > parse(current)


def install_kind():
    """'installer', 'portable', 'mac', or 'source' (running from source: open the page)."""
    if not paths.is_frozen():
        return "source"
    if IS_MAC:
        return "mac"
    if IS_WIN:
        return "installer" if os.path.exists(os.path.join(paths.install_root(), "unins000.exe")) else "portable"
    return "source"


def asset_name(kind):
    if kind == "installer":
        return "ActivityTracker-Setup-x64.exe"
    if kind == "portable":
        return "ActivityTracker-windows-x64.zip"
    if kind == "mac":
        return "ActivityTracker-macos-%s.zip" % ("arm64" if platform.machine() == "arm64" else "intel")
    return None


def latest_release(timeout=20):
    """Latest release info from the GitHub API; if the API refuses (60 unauthenticated
    requests/hour per IP, easily used up on a shared office/CI network), fall back to the
    un-rate-limited /releases/latest redirect and the fixed asset names."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ActivityTracker"}
    token = os.environ.get("GITHUB_TOKEN")  # set in CI only
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(API, headers=headers), timeout=timeout) as resp:
            data = json.load(resp)
        return {"version": data["tag_name"].lstrip("vV"), "notes": (data.get("body") or "").strip(),
                "page": data.get("html_url") or RELEASES_PAGE,
                "assets": {a["name"]: a for a in data.get("assets", [])}}
    except (urllib.error.HTTPError, KeyError) as exc:
        log.info("GitHub API unavailable (%s); using the releases page", exc)
    return _latest_from_redirect(timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _latest_from_redirect(timeout=20):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(RELEASES_PAGE, method="HEAD", headers={"User-Agent": "ActivityTracker"})
    try:
        opener.open(req, timeout=timeout)
        raise RuntimeError("GitHub didn't redirect to the latest release")
    except urllib.error.HTTPError as exc:  # the 302 arrives as an "error" without a redirect handler
        location = exc.headers.get("Location") or ""
    tag = location.rstrip("/").rsplit("/", 1)[-1]
    if "/releases/tag/" not in location or not tag:
        raise RuntimeError("couldn't read the latest release from GitHub")
    base = "https://github.com/%s/releases/download/%s/" % (REPO, tag)
    names = ["ActivityTracker-Setup-x64.exe", "ActivityTracker-windows-x64.zip",
             "ActivityTracker-macos-arm64.zip", "ActivityTracker-macos-intel.zip"]
    # No published checksum on this path: the download is still HTTPS from github.com and
    # is checked for completeness against the server's Content-Length.
    return {"version": tag.lstrip("vV"), "notes": "", "page": location,
            "assets": {n: {"name": n, "browser_download_url": base + n} for n in names}}


def check():
    """-> dict(available, current, latest, notes, page, kind, asset) ; raises on network errors."""
    rel = latest_release()
    kind = install_kind()
    name = asset_name(kind)
    return {"available": is_newer(rel["version"], current_version()), "current": current_version(),
            "latest": rel["version"], "notes": rel["notes"], "page": rel["page"], "kind": kind,
            "asset": rel["assets"].get(name) if name else None}


def download(asset, dest_dir, progress=lambda frac: None):
    """Download a release asset and verify it against GitHub's published SHA-256."""
    path = os.path.join(dest_dir, asset["name"])
    sha = hashlib.sha256()
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "ActivityTracker"})
    done, total = 0, asset.get("size") or 0
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as out:
        total = total or int(resp.headers.get("Content-Length") or 0)
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            out.write(block)
            sha.update(block)
            done += len(block)
            if total:
                progress(done / total)
    if total and done != total:
        raise RuntimeError("download incomplete (%d of %d bytes)" % (done, total))
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:") and digest[7:] != sha.hexdigest():
        os.remove(path)
        raise RuntimeError("download failed its checksum; nothing was installed")
    return path


def _spawn_detached(cmd, console=False):
    """Start a process that outlives Activity Tracker. ``console``: it runs console tools (the
    PowerShell helper), so give it a hidden console instead of none at all."""
    kwargs = {"close_fds": True}
    if IS_WIN:
        NEW_GROUP, NO_WINDOW, DETACHED = 0x00000200, 0x08000000, 0x00000008
        kwargs["creationflags"] = NEW_GROUP | (NO_WINDOW if console else DETACHED)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def portable_script(new_root, app_root, pid, exe, work_dir=None):
    """Windows helper (PowerShell): wait for Activity Tracker to exit, copy the new files over
    (never data\\), restart. PowerShell's Wait-Process needs no console; the first version
    was a .cmd whose tasklist/timeout calls hang when started without a console."""
    q = lambda p: "'" + p.replace("'", "''") + "'"  # noqa: E731  (PowerShell single-quoted string)
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "Wait-Process -Id %d -Timeout 180" % pid,
        "Start-Sleep -Milliseconds 700",
        "robocopy %s %s /E /XD data /R:5 /W:1 /NFL /NDL /NJH /NJS | Out-Null" % (q(new_root), q(app_root)),
        "Start-Process -FilePath %s" % q(exe),
    ]
    if work_dir:
        lines.append("Remove-Item -Recurse -Force %s" % q(work_dir))
    return "\r\n".join(lines) + "\r\n"


def mac_script(new_app, app_path, pid):
    """macOS helper: wait for Activity Tracker to exit, replace the .app, restart."""
    return "\n".join([
        "#!/bin/sh",
        "while kill -0 %d 2>/dev/null; do sleep 1; done" % pid,
        'rm -rf "%s.old" && mv "%s" "%s.old" && mv "%s" "%s" && rm -rf "%s.old"' % (
            app_path, app_path, app_path, new_app, app_path, app_path),
        'open "%s"' % app_path,
        "",
    ])


def apply(info, progress=lambda msg, frac: None):
    """Download and start installing. Returns True when Activity Tracker should now quit."""
    kind, asset = info["kind"], info["asset"]
    if kind == "source" or not asset:
        raise RuntimeError("automatic update isn't available for this copy; download it from %s" % info["page"])
    work = tempfile.mkdtemp(prefix="activitytracker-update-")
    path = download(asset, work, lambda f: progress("Downloading Activity Tracker %s" % info["latest"], f))
    progress("Installing…", 1.0)
    pid = os.getpid()
    if kind == "installer":
        # Inno Setup: upgrade in place (same AppId), keep previous tasks, relaunch afterwards.
        _spawn_detached([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"])
        return True
    if kind == "portable":
        extracted = os.path.join(work, "new")
        with zipfile.ZipFile(path) as z:
            z.extractall(extracted)
        new_root = os.path.join(extracted, "ActivityTracker")
        app_root = paths.install_root()
        script = os.path.join(work, "apply-update.ps1")
        with open(script, "w", encoding="utf-8-sig") as f:
            f.write(portable_script(new_root, app_root, pid, os.path.join(app_root, "ActivityTracker.exe"), work))
        _spawn_detached(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                         "-File", script], console=True)
        return True
    if kind == "mac":
        extracted = os.path.join(work, "new")
        subprocess.run(["ditto", "-x", "-k", path, extracted], check=True)  # keeps symlinks + signature
        new_app = os.path.join(extracted, "ActivityTracker", "ActivityTracker.app")
        app_path = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
        script = os.path.join(work, "apply-update.sh")
        with open(script, "w") as f:
            f.write(mac_script(new_app, app_path, pid))
        os.chmod(script, 0o755)
        _spawn_detached(["/bin/sh", script])
        return True
    return False


def run_cli(out=None):
    """``--update-now``: check, and if newer, download and hand over (used by tests/CI)."""
    result = {"ok": False}
    try:
        info = check()
        result.update({k: info[k] for k in ("current", "latest", "kind", "available")})
        if info["available"]:
            result["handed_over"] = apply(info)
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    if out:
        with open(out, "w") as f:
            json.dump(result, f)
    time.sleep(0.5)
    return 0 if result["ok"] else 1
