"""The capture blocklist: a matching window is never captured (no title, no text, no
screenshot). Shared format with the macOS helper (capture/blocklist.json)."""
import json
import logging
import os
import re
import shutil

from . import paths

log = logging.getLogger(__name__)


def user_path(spool):
    """The editable copy lives in the spool folder, where the capture helpers read it."""
    path = os.path.join(spool, "blocklist.json")
    if not os.path.exists(path):
        try:
            shutil.copyfile(paths.resource("capture", "blocklist.json"), path)
        except OSError:
            log.warning("no default blocklist shipped; capturing without one", exc_info=True)
    return path


def _compile(pattern):
    flags = 0
    if pattern.startswith("(?i)"):  # ICU-style inline flag; Python wants it handled the same way
        pattern, flags = pattern[4:], re.I
    try:
        return re.compile(pattern, flags)
    except re.error:
        log.warning("bad blocklist pattern ignored: %r", pattern)
        return None


class Blocklist:
    def __init__(self, procs=(), patterns=()):
        self.procs = [p.lower() for p in procs if p]
        self.patterns = [r for r in (_compile(p) for p in patterns) if r]

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return cls(data.get("procNames", []), data.get("titlePatterns", []))
        except (OSError, ValueError):
            log.warning("blocklist not loaded from %s; capturing with NO blocklist", path)
            return cls()

    def blocks(self, proc, title):
        p = (proc or "").lower()
        if any(name in p for name in self.procs):
            return True
        return any(r.search(title or "") for r in self.patterns)
