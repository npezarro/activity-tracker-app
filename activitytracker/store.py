"""SQLite store for captured activity (data/activity.db).

Schema matches the original activity-tracker service, so tools written for one read
the other: events(id, timestamp UTC 'YYYY-MM-DD HH:MM:SS', source, event_type,
content, metadata_json)."""
import datetime as dt
import json
import os
import sqlite3
import threading

from . import paths

_lock = threading.RLock()
_conn = None
_path = None


def db_path():
    return os.environ.get("ACTIVITYTRACKER_DB") or os.path.join(paths.data_dir(), "activity.db")


def conn():
    global _conn, _path
    with _lock:
        if _conn is None or _path != db_path():
            _path = db_path()
            _conn = sqlite3.connect(_path, check_same_thread=False, timeout=5)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                  source TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  content TEXT NOT NULL,
                  metadata_json TEXT DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_source_type ON events(source, event_type);
            """)
        return _conn


def close():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def to_utc_text(ts=None):
    """ISO 8601 (any offset) or epoch -> 'YYYY-MM-DD HH:MM:SS' UTC; None = now."""
    if ts is None:
        t = dt.datetime.now(dt.timezone.utc)
    elif isinstance(ts, (int, float)):
        t = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    else:
        try:
            t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            t = dt.datetime.now(dt.timezone.utc)
        if t.tzinfo is None:
            t = t.astimezone()
        t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def from_utc_text(text):
    """-> aware local datetime."""
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone()


def insert(source, event_type, content, meta=None, ts=None):
    with _lock:
        c = conn()
        cur = c.execute("INSERT INTO events (timestamp, source, event_type, content, metadata_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (to_utc_text(ts), source, event_type, content or "", json.dumps(meta or {})))
        c.commit()
        return cur.lastrowid


def _rows(sql, params=()):
    with _lock:
        out = []
        for r in conn().execute(sql, params).fetchall():
            d = dict(r)
            try:
                d["meta"] = json.loads(d.pop("metadata_json") or "{}")
            except ValueError:
                d["meta"] = {}
            out.append(d)
        return out


def events_between(start, end=None, types=None):
    """Events with start <= ts < end (aware datetimes or None), oldest first."""
    sql, params = "SELECT * FROM events WHERE timestamp >= ?", [to_utc_text(start.timestamp())]
    if end is not None:
        sql += " AND timestamp < ?"
        params.append(to_utc_text(end.timestamp()))
    if types:
        sql += " AND event_type IN (%s)" % ",".join("?" * len(types))
        params += list(types)
    return _rows(sql + " ORDER BY timestamp, id", params)


def latest(event_type):
    rows = _rows("SELECT * FROM events WHERE event_type = ? ORDER BY id DESC LIMIT 1", (event_type,))
    return rows[0] if rows else None


def count_since(since, event_type=None):
    sql, params = "SELECT COUNT(*) FROM events WHERE timestamp >= ?", [to_utc_text(since.timestamp())]
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    with _lock:
        return conn().execute(sql, params).fetchone()[0]


def search(text, limit=200):
    like = "%" + text.replace("%", r"\%").replace("_", r"\_") + "%"
    return _rows("SELECT * FROM events WHERE content LIKE ? ESCAPE '\\' OR metadata_json LIKE ? ESCAPE '\\' "
                 "ORDER BY id DESC LIMIT ?", (like, like, limit))


def run_retention(days, text_days):
    """Drop everything older than ``days``; screen text and descriptions after ``text_days``."""
    with _lock:
        c = conn()
        n = c.execute("DELETE FROM events WHERE timestamp < datetime('now', ?)", ("-%d days" % days,)).rowcount
        n += c.execute("DELETE FROM events WHERE event_type IN ('ocr', 'vision') AND timestamp < datetime('now', ?)",
                       ("-%d days" % text_days,)).rowcount
        c.commit()
        return n
