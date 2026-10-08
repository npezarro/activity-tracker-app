"""What changed in one window between two captures of it.

A description alone restates the screen every cycle ("editing a document"); the
diff says what moved. Pure set arithmetic over normalized lines: no model, no I/O,
so it is the ground truth a model's narrative is checked against. Callers own
scrubbing: the strings that come out are literal screen text."""
import re

# Clocks, unread badges, percentages: chrome that flips every capture.
_VOLATILE = re.compile(r"^[\d\s:.,/%-]+$")
NAVIGATION_RATIO = 0.7  # above this the view was replaced (navigated, scrolled), not edited


def is_volatile(line):
    return not line or len(line) < 3 or bool(_VOLATILE.match(line))


def normalize_lines(text):
    out, seen = [], set()
    for line in str(text or "").split("\n"):
        raw = line.strip()
        if is_volatile(raw):
            continue
        key = " ".join(raw.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append((raw, key))
    return out


def diff_text(prev, new):
    """-> dict(added, removed, change_ratio, first_seen). ``prev`` None = no earlier capture."""
    a, b = normalize_lines(prev), normalize_lines(new)
    a_keys, b_keys = {k for _, k in a}, {k for _, k in b}
    added = [r for r, k in b if k not in a_keys]
    removed = [r for r, k in a if k not in b_keys]
    union = len(a_keys | b_keys)
    return {"added": added, "removed": removed,
            "change_ratio": 0.0 if union == 0 else (len(added) + len(removed)) / union,
            "first_seen": prev is None}


def _informative(lines, max_lines):
    """Longest lines first (real prose beats tab strips and buttons), shown in screen order."""
    cands = [(i, l, len(l.split())) for i, l in enumerate(lines) if len(l) >= 8]
    cands.sort(key=lambda c: (-c[2], c[0]))
    picked = sorted(cands[:max_lines])
    return [l for _, l, _ in picked]


def format_delta(d, max_lines=4, max_chars=160):
    """Compact human block, or '' when nothing meaningful moved."""
    if not d or d["first_seen"]:
        return ""
    picked = _informative(d["added"], max_lines)
    parts = []
    if picked and d["change_ratio"] >= NAVIGATION_RATIO:
        parts.append("(view replaced, not edited)")
    parts += ["+ " + (l[:max_chars] + "…" if len(l) > max_chars else l) for l in picked]
    extra = len(d["added"]) - len(picked)
    if extra > 0:
        parts.append("+ %d more line(s)" % extra)
    if d["removed"]:
        parts.append("− %d line(s) gone" % len(d["removed"]))
    return "\n".join(parts)


def window_key(proc, title):
    """Identity of a window for diffing: unread counters are not identity
    ("Inbox (4)" and "Inbox (7)" are the same window)."""
    t = re.sub(r"\(\d+\)", "", str(title or ""))
    t = re.sub(r"^[•●‎‏\s*]+", "", t)
    return "%s\x01%s" % (proc or "Unknown", " ".join(t.split()).lower())
