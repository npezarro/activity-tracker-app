"""Timeline and summaries.

The timeline is deterministic: consecutive events in the same window form a session,
with its time on screen and what the model saw there. Summaries are optional and use
the chosen engine (local, online, Claude, Codex); with no engine, or if the engine
fails, a plain summary is built from the timeline instead, so a summary always exists.
Written to data/summaries/YYYY-MM-DD.md."""
import datetime as dt
import logging
import os

from . import diff, models, paths, store
from .scrub import scrub

log = logging.getLogger(__name__)
GAP = 5 * 60  # a session with no events for this long has ended

SYSTEM = ("You summarize a person's computer activity log for that same person. Write in second person "
          "(\"you\"). Be concrete: name the documents, sites, projects, people and topics from the log. "
          "Group related work, say roughly how long each took, and note anything left unfinished. "
          "Never include passwords, keys, account numbers or other credentials even if they appear. "
          "Use short Markdown bullet points under 2-4 headings. No preamble.")


def sessions(events):
    """Group events into window sessions, oldest first."""
    out = []
    for ev in events:
        meta = ev.get("meta", {})
        t = store.from_utc_text(ev["timestamp"])
        key = diff.window_key(meta.get("proc"), meta.get("title"))
        cur = out[-1] if out else None
        held = ev["event_type"] == "focus_end"  # the helper measured that the window stayed in front
        if not cur or cur["key"] != key or ((t - cur["end"]).total_seconds() > GAP and not held):
            if cur and cur["measured"] is None:
                cur["seconds"] = max(cur["seconds"], min((t - cur["start"]).total_seconds(), GAP))
            cur = {"key": key, "proc": meta.get("proc") or "Unknown", "title": scrub(meta.get("title") or ""),
                   "start": t, "end": t, "seconds": 0, "measured": None, "notes": [], "text": ""}
            out.append(cur)
        cur["end"] = t
        kind = ev["event_type"]
        if kind == "focus_end" and isinstance(meta.get("durationSec"), (int, float)):
            cur["measured"] = (cur["measured"] or 0) + meta["durationSec"]
        elif kind == "vision":
            note = scrub(ev["content"])
            if meta.get("delta"):
                note += "  Progress: " + scrub(meta["delta"])
            cur["notes"].append((t, note, scrub(meta.get("changeText") or "")))
        elif kind == "ocr" and len(ev["content"]) > len(cur["text"]):
            cur["text"] = scrub(ev["content"])
        if cur["measured"] is None:
            cur["seconds"] = (cur["end"] - cur["start"]).total_seconds()
    for s in out:
        if s["measured"] is not None:
            s["seconds"] = s["measured"]
    return out


def fmt_duration(seconds):
    m = int(round(seconds / 60))
    return "<1 min" if m < 1 else "%d min" % m if m < 60 else "%dh %02dm" % (m // 60, m % 60)


def compact_lines(sess, max_chars=12000, text_chars=400):
    """The activity log as model input: one block per session, newest content kept."""
    lines = []
    for s in sess:
        head = "%s  %s - %s (%s)" % (s["start"].strftime("%H:%M"), s["proc"], s["title"][:120],
                                     fmt_duration(s["seconds"]))
        lines.append(head)
        for _, note, change in s["notes"][-4:]:
            lines.append("   SAW: " + note[:400])
            if change:
                lines.append("   CHANGED: " + change.replace("\n", " | ")[:300])
        if not s["notes"] and s["text"]:
            lines.append("   TEXT: " + " ".join(s["text"].split())[:text_chars])
    text = "\n".join(lines)
    return text[-max_chars:]


def plain_summary(sess):
    if not sess:
        return "_No activity captured in this period._"
    by_app = {}
    for s in sess:
        by_app[s["proc"]] = by_app.get(s["proc"], 0) + s["seconds"]
    top = sorted(by_app.items(), key=lambda kv: -kv[1])[:8]
    out = ["**Time by app:** " + ", ".join("%s %s" % (a, fmt_duration(t)) for a, t in top), ""]
    longest = sorted((s for s in sess if s["seconds"] >= 60), key=lambda s: -s["seconds"])[:10]
    for s in sorted(longest, key=lambda s: s["start"]):
        note = s["notes"][-1][1] if s["notes"] else ""
        out.append("- %s %s - %s (%s)%s" % (s["start"].strftime("%H:%M"), s["proc"], s["title"][:100] or "untitled",
                                             fmt_duration(s["seconds"]), (": " + note[:200]) if note else ""))
    return "\n".join(out)


def summarize(cfg, get_key, start, end, label):
    """-> (markdown, engine_used)."""
    sess = sessions(store.events_between(start, end, ("focus", "focus_end", "ocr", "vision")))
    engine = cfg.get("summary_engine", "off")
    if engine != "off" and sess:
        try:
            body = models.write_text(engine, cfg, get_key, SYSTEM,
                                     "Activity log for %s:\n\n%s" % (label, compact_lines(sess)), timeout=180)
            if body:
                return scrub(body), engine
        except Exception as exc:
            log.warning("summary via %s failed, writing a plain one: %s", engine, str(exc)[:200])
    return plain_summary(sess), "plain"


def summary_path(day):
    return os.path.join(paths.sub("summaries"), "%s.md" % day.isoformat())


def write_summary(cfg, get_key, start, end, label, heading):
    body, engine = summarize(cfg, get_key, start, end, label)
    path = summary_path(start.date())
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Activity, %s\n\n" % start.strftime("%A %d %B %Y"))
        f.write("## %s\n\n%s\n\n<sub>written by: %s</sub>\n\n" % (heading, body, engine))
    return path


def hour_bounds(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end - dt.timedelta(hours=1), end


def day_bounds(now):
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)
