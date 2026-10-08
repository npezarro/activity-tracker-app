import datetime as dt
import json
import os

from activitytracker import blocklist, ingest, store

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_shipped_blocklist_blocks_the_basics():
    bl = blocklist.Blocklist.load(os.path.join(ROOT, "capture", "blocklist.json"))
    assert bl.blocks("1Password 8", "Vault")
    assert bl.blocks("chrome", "Sign in - Google Accounts")
    assert bl.blocks("msedge", "New tab - [InPrivate] - Microsoft Edge")
    assert not bl.blocks("Code", "app.py - project")


def test_build_record_scrubs_and_skips():
    rec = ingest.build_record({"type": "ocr", "proc": "X", "title": "token=abc", "text": "secret: s3cr3t"})
    assert rec[2] == "secret=[REDACTED]" and rec[3]["title"] == "token=[REDACTED]"
    assert ingest.build_record({"type": "frame"}) is None
    assert ingest.build_record({"type": "ocr", "text": ""}) is None
    assert ingest.build_record({"type": "focus_end", "durationSec": 12})[3]["durationSec"] == 12


def test_ingest_offsets_and_partial_lines(tmp_path):
    spool = tmp_path
    f = spool / "events-2026-01-01.jsonl"
    ts = dt.datetime.now().astimezone().isoformat()
    f.write_text(json.dumps({"ts": ts, "type": "focus", "proc": "A", "title": "one"}) + "\n" + '{"ts": "partial')
    ing = ingest.Ingestor(str(spool))
    assert ing.poll() == 1
    assert ing.poll() == 0
    with open(f, "a") as fh:
        fh.write('", "type": "focus", "proc": "A", "title": "two"}\n')
    assert ing.poll() == 1  # a half-written line waits until it is complete
    assert store.latest("focus")["content"] == "two"
    with open(f, "a") as fh:
        fh.write(json.dumps({"ts": ts, "type": "focus", "proc": "B", "title": "three"}) + "\n")
    assert ing.poll() == 1
    assert store.latest("focus")["content"] == "three"
