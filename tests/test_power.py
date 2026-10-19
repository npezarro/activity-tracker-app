import json
import os
import time

from activitytracker import config, power


def cfg(**kw):
    c = dict(config.DEFAULTS)
    c.update(kw)
    return c


def test_intervals_ac_unchanged():
    assert power.intervals(cfg(), power.AC) == (2, 30)


def test_intervals_battery_and_low_power():
    assert power.intervals(cfg(), power.BATTERY) == (15, 60)
    assert power.intervals(cfg(), power.LOW_POWER) == (30, 60)


def test_intervals_keep_longer_user_setting():
    assert power.intervals(cfg(ocr_interval_s=20, frame_interval_s=120), power.BATTERY) == (20, 120)


def test_intervals_battery_saver_off():
    assert power.intervals(cfg(battery_saver=False), power.BATTERY) == (2, 30)


def test_windows_status_mapping():
    assert power.from_windows_status(1, 0) == power.AC
    assert power.from_windows_status(255, 0) == power.AC  # unknown => full cadence
    assert power.from_windows_status(0, 0) == power.BATTERY
    assert power.from_windows_status(0, 1) == power.LOW_POWER


def _status(tmp_path, body, age=0):
    p = tmp_path / ".daemon-status"
    p.write_text(body, encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return str(tmp_path)


def test_helper_state(tmp_path):
    assert power.helper_state(_status(tmp_path, json.dumps({"power": "battery"}))) == power.BATTERY
    assert power.helper_state(_status(tmp_path, json.dumps({"power": "low-power"}))) == power.LOW_POWER
    assert power.helper_state(_status(tmp_path, json.dumps({"power": "ac"}))) == power.AC


def test_helper_state_defaults_to_ac(tmp_path):
    assert power.helper_state(str(tmp_path)) == power.AC  # no file
    assert power.helper_state(_status(tmp_path, "garbage")) == power.AC
    assert power.helper_state(_status(tmp_path, '{"screenRecording":true}')) == power.AC  # older helper
    assert power.helper_state(_status(tmp_path, '["battery"]')) == power.AC
    assert power.helper_state(_status(tmp_path, '{"power":"battery"}', age=3 * 3600)) == power.AC  # stale


class _FakeApp:
    """Just enough of App for _summaries / _hold_for_battery (no Tk)."""

    def __init__(self, **kw):
        from activitytracker import app
        self.cfg = cfg(summary_engine="local", daily_summary_hour=24, **kw)
        self.state = {}
        self._battery_hold_logged = False
        self._summaries = app.App._summaries.__get__(self)
        self._hold_for_battery = app.App._hold_for_battery.__get__(self)

    def _save_state(self):
        pass


def test_model_summary_held_on_battery_then_covers_whole_stretch(monkeypatch):
    import datetime as dt
    from activitytracker import app
    written = []
    monkeypatch.setattr(app.store, "count_since", lambda start: 1)
    monkeypatch.setattr(app.summary, "write_summary", lambda c, k, start, end, *a: written.append((start, end)))
    state = {"p": power.BATTERY}
    monkeypatch.setattr(app.power, "state", lambda spool: state["p"])
    a = _FakeApp()
    t0 = dt.datetime(2026, 10, 19, 8, 0).astimezone()
    a.state["last_period_end"] = t0.isoformat()
    a._summaries(t0 + dt.timedelta(hours=2))
    a._summaries(t0 + dt.timedelta(hours=7))
    assert written == []  # held while on battery
    state["p"] = power.AC
    a._summaries(t0 + dt.timedelta(hours=8))
    assert written == [(t0, t0 + dt.timedelta(hours=8))]  # one summary for the whole 8 h, not just the last hour


def test_plain_summary_not_held(monkeypatch):
    import datetime as dt
    from activitytracker import app
    written = []
    monkeypatch.setattr(app.store, "count_since", lambda start: 1)
    monkeypatch.setattr(app.summary, "write_summary", lambda c, k, start, end, *a: written.append(start))
    monkeypatch.setattr(app.power, "state", lambda spool: power.BATTERY)
    a = _FakeApp()
    a.cfg["summary_engine"] = "off"
    t0 = dt.datetime(2026, 10, 19, 8, 0).astimezone()
    a.state["last_period_end"] = t0.isoformat()
    a._summaries(t0 + dt.timedelta(hours=1))
    assert written == [t0]


def test_real_power_query_on_windows():
    import sys
    import pytest
    if sys.platform != "win32":
        pytest.skip("Windows API")
    assert power.windows_state() in (power.AC, power.BATTERY, power.LOW_POWER)


def test_daily_summary_not_blocked_by_battery_hold(monkeypatch):
    import datetime as dt
    from activitytracker import app
    written = []
    monkeypatch.setattr(app.store, "count_since", lambda start: 1)
    monkeypatch.setattr(app.summary, "write_summary", lambda c, k, start, end, label, title: written.append(title))
    monkeypatch.setattr(app.summary, "day_bounds", lambda now: (now.replace(hour=0, minute=0), now))
    monkeypatch.setattr(app.power, "state", lambda spool: power.BATTERY)
    a = _FakeApp()
    a.cfg["daily_summary_hour"] = 23
    t0 = dt.datetime(2026, 10, 19, 22, 0).astimezone()
    a.state["last_period_end"] = t0.isoformat()
    a._summaries(t0 + dt.timedelta(hours=1, minutes=5))
    assert written == ["The day"]  # periodic one held, the daily one still written


def test_battery_hold_survives_app_restart(monkeypatch):
    import datetime as dt
    from activitytracker import app
    written = []
    monkeypatch.setattr(app.store, "count_since", lambda start: 1)
    monkeypatch.setattr(app.summary, "write_summary", lambda c, k, start, end, *a: written.append((start, end)))
    state = {"p": power.BATTERY}
    monkeypatch.setattr(app.power, "state", lambda spool: state["p"])
    a = _FakeApp()
    t0 = dt.datetime(2026, 10, 19, 8, 0).astimezone()
    a.state["last_period_end"] = t0.isoformat()
    a._summaries(t0 + dt.timedelta(hours=2))
    b = _FakeApp()  # restart: fresh App, same saved state
    b.state = dict(a.state)
    state["p"] = power.AC
    b._summaries(t0 + dt.timedelta(hours=8))
    assert written == [(t0, t0 + dt.timedelta(hours=8))]
    assert "summary_held" not in b.state


def test_failed_summary_keeps_the_hold(monkeypatch):
    import datetime as dt
    import pytest
    from activitytracker import app
    monkeypatch.setattr(app.store, "count_since", lambda start: 1)
    monkeypatch.setattr(app.power, "state", lambda spool: power.AC)

    def boom(*a):
        raise RuntimeError("model down")
    monkeypatch.setattr(app.summary, "write_summary", boom)
    a = _FakeApp()
    t0 = dt.datetime(2026, 10, 19, 8, 0).astimezone()
    a.state.update(last_period_end=t0.isoformat(), summary_held=True)
    with pytest.raises(RuntimeError):
        a._summaries(t0 + dt.timedelta(hours=8))
    assert a.state["summary_held"] and a.state["last_period_end"] == t0.isoformat()
