import datetime as dt

from activitytracker import store, summary


def test_sessions_and_plain_summary():
    base = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone.utc)
    def ev(sec, kind, proc, title, content="", **meta):
        return {"timestamp": store.to_utc_text((base + dt.timedelta(seconds=sec)).timestamp()),
                "event_type": kind, "content": content or title, "meta": dict(proc=proc, title=title, **meta)}
    events = [ev(0, "focus", "Editor", "plan.md"), ev(30, "vision", "Editor", "plan.md", "Writing the plan"),
              ev(600, "focus_end", "Editor", "plan.md", durationSec=600), ev(601, "focus", "Mail", "Inbox (3)")]
    s = summary.sessions(events)
    assert [x["proc"] for x in s] == ["Editor", "Mail"]
    assert s[0]["seconds"] == 600 and s[0]["notes"][0][1] == "Writing the plan"
    assert "Editor 10 min" in summary.plain_summary(s)
    assert "SAW: Writing the plan" in summary.compact_lines(s)


def test_summary_falls_back_when_engine_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(summary.models, "write_text", boom)
    now = dt.datetime.now().astimezone()
    store.insert("t", "focus", "x", {"proc": "P", "title": "x"})
    body, used = summary.summarize({"summary_engine": "online"}, lambda n: "", now - dt.timedelta(hours=1),
                                   now + dt.timedelta(minutes=1), "test")
    assert used == "plain" and "P" in body
