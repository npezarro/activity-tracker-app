"""Pausing capture, and the reminder to resume.

The pause is a file named PAUSED in the spool folder: every capture helper checks for
it before each capture and writes nothing while it exists, so a pause holds even if
the app itself is restarted or crashes. The file carries when the pause started and,
for timed pauses ("pause for 1 hour"), when it ends."""
import json
import os
import time


def flag_path(spool):
    return os.path.join(spool, "PAUSED")


def state(spool):
    """-> None when capturing, else dict(since, until) in epoch seconds (until 0 = open-ended)."""
    path = flag_path(spool)
    if not os.path.exists(path):
        return None
    info = {"since": 0, "until": 0}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        info.update({k: float(data.get(k) or 0) for k in ("since", "until")})
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # an empty PAUSED file (made by hand or by an older helper) is still a pause
    if not info["since"]:
        try:
            info["since"] = os.path.getmtime(path)
        except OSError:
            info["since"] = time.time()
    return info


def pause(spool, minutes=0, now=None):
    now = time.time() if now is None else now
    with open(flag_path(spool), "w", encoding="utf-8") as f:
        json.dump({"since": now, "until": now + minutes * 60 if minutes else 0}, f)


def resume(spool):
    try:
        os.remove(flag_path(spool))
    except FileNotFoundError:
        pass


def auto_resume_due(st, now):
    return bool(st and st["until"] and now >= st["until"])


def nudge_due(st, now, last_nudge, after_min, repeat_min, timed_too=False):
    """Should we remind the user to resume right now?

    First reminder once the pause is ``after_min`` minutes old, then every ``repeat_min``
    minutes. A timed pause ends by itself, so it only nudges with ``timed_too``.
    ``last_nudge`` is the epoch of the previous reminder for THIS pause (0 = none yet)."""
    if not st or not after_min or after_min <= 0:
        return False
    if st["until"] and not timed_too:
        return False
    if now - st["since"] < after_min * 60:
        return False
    if not last_nudge or last_nudge < st["since"]:
        return True
    return now - last_nudge >= max(1, repeat_min or after_min) * 60


def describe(st, now):
    if not st:
        return "Recording"
    mins = int((now - st["since"]) // 60)
    held = "%d min" % mins if mins < 120 else "%.1f h" % (mins / 60)
    if st["until"]:
        left = max(0, int((st["until"] - now + 59) // 60))
        return "Paused (resumes in %d min)" % left
    return "Paused for %s" % held
