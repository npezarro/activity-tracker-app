"""Reads the capture helper's spool (events-YYYY-MM-DD.jsonl, see SPOOL-SCHEMA.md) into
the database. Polls with a byte offset per file rather than file-change notifications,
which are unreliable on network and VM-mounted folders. Scrubs at this choke point so
every reader of the database inherits it."""
import glob
import json
import logging
import os

from . import paths, store
from .scrub import scrub

log = logging.getLogger(__name__)
KINDS = ("focus", "focus_end", "ocr")  # `frame` events are the vision worker's


def build_record(ev):
    """-> (source, type, content, meta, ts) or None for events we don't store."""
    kind = ev.get("type")
    if kind not in KINDS:
        return None
    title = scrub(ev.get("title") or "")
    meta = {"proc": ev.get("proc") or "Unknown", "title": title, "ts": ev.get("ts")}
    if kind == "focus_end":
        if isinstance(ev.get("durationSec"), (int, float)):
            meta["durationSec"] = ev["durationSec"]
        content = title
    elif kind == "ocr":
        content = scrub(ev.get("text") or "")
        if not content:
            return None
    else:
        content = title
    return ev.get("source") or "host", kind, content, meta, ev.get("ts")


class Ingestor:
    def __init__(self, spool):
        self.spool = spool
        self.offsets_path = os.path.join(paths.data_dir(), "spool-offsets.json")
        try:
            with open(self.offsets_path, encoding="utf-8") as f:
                self.offsets = json.load(f)
        except (OSError, ValueError):
            self.offsets = {}

    def poll(self):
        """Ingest everything new. -> number of events stored."""
        stored = 0
        live = set()
        for path in sorted(glob.glob(os.path.join(self.spool, "events-*.jsonl"))):
            name = os.path.basename(path)
            live.add(name)
            offset = self.offsets.get(name, 0)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < offset:  # file was replaced; start over
                offset = 0
            if size == offset:
                continue
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
            end = chunk.rfind(b"\n")
            if end < 0:
                continue  # a line still being written; take it next time
            for line in chunk[:end].splitlines():
                try:
                    rec = build_record(json.loads(line))
                except ValueError:
                    continue
                if rec:
                    store.insert(rec[0], rec[1], rec[2], rec[3], ts=rec[4])
                    stored += 1
            self.offsets[name] = offset + end + 1
        self.offsets = {k: v for k, v in self.offsets.items() if k in live}
        tmp = self.offsets_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.offsets, f)
        os.replace(tmp, self.offsets_path)
        return stored
