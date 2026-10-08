from activitytracker import pause

H = 3600


def st(since, until=0):
    return {"since": since, "until": until}


def test_no_nudge_when_recording_or_disabled():
    assert not pause.nudge_due(None, 10 * H, 0, 15, 15)
    assert not pause.nudge_due(st(0), 10 * H, 0, 0, 15)


def test_first_nudge_after_threshold():
    assert not pause.nudge_due(st(0), 14 * 60, 0, 15, 15)
    assert pause.nudge_due(st(0), 15 * 60, 0, 15, 15)


def test_repeats_every_interval():
    first = 15 * 60
    assert not pause.nudge_due(st(0), first + 10 * 60, first, 15, 15)
    assert pause.nudge_due(st(0), first + 15 * 60, first, 15, 15)
    assert pause.nudge_due(st(0), first + 5 * 60, first, 15, 5)


def test_new_pause_resets_reminder():
    # a reminder for an earlier pause doesn't suppress the first one for this pause
    assert pause.nudge_due(st(H), H + 16 * 60, H - 60, 15, 15)


def test_timed_pause_only_when_asked():
    s = st(0, 2 * H)
    assert not pause.nudge_due(s, 30 * 60, 0, 15, 15)
    assert pause.nudge_due(s, 30 * 60, 0, 15, 15, timed_too=True)


def test_flag_roundtrip_and_auto_resume(tmp_path):
    spool = str(tmp_path)
    assert pause.state(spool) is None
    pause.pause(spool, 60, now=1000)
    s = pause.state(spool)
    assert s == {"since": 1000, "until": 1000 + 3600}
    assert not pause.auto_resume_due(s, 1000 + 3599)
    assert pause.auto_resume_due(s, 1000 + 3600)
    pause.resume(spool)
    assert pause.state(spool) is None


def test_empty_flag_file_is_a_pause(tmp_path):
    (tmp_path / "PAUSED").write_text("")
    s = pause.state(str(tmp_path))
    assert s["until"] == 0 and s["since"] > 0


def test_describe():
    assert pause.describe(None, 0) == "Recording"
    assert pause.describe(st(0), 20 * 60) == "Paused for 20 min"
    assert pause.describe(st(0, 3600), 600) == "Paused (resumes in 50 min)"
