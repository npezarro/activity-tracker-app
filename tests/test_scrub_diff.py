from activitytracker import diff
from activitytracker.scrub import scrub


def test_scrub_patterns():
    assert scrub("password: hunter2") == "password=[REDACTED]"
    assert "[REDACTED]" in scrub("key sk-proj-abcdefghijklmnop1234")
    assert scrub("ghp_" + "a" * 30) == "[REDACTED]"
    assert scrub("https://bob:pw@example.com/x") == "https://bob:[REDACTED]@example.com/x"
    assert scrub("plain words") == "plain words"
    assert scrub("") == "" and scrub(None) is None


def test_diff_added_removed_and_first_seen():
    d = diff.diff_text(None, "hello world")
    assert d["first_seen"]
    d = diff.diff_text("Title line\nold paragraph here", "Title line\nnew paragraph written today")
    assert d["added"] == ["new paragraph written today"]
    assert d["removed"] == ["old paragraph here"]
    assert diff.format_delta(d).startswith("+ new paragraph")


def test_volatile_lines_ignored():
    d = diff.diff_text("Doc body text\n10:41", "Doc body text\n10:42")
    assert d["added"] == [] and d["change_ratio"] == 0


def test_window_key_ignores_counters():
    assert diff.window_key("Mail", "Inbox (4)") == diff.window_key("Mail", "Inbox (7)")
