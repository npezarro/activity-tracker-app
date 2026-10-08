from activitytracker import config, store, vision


def cfg(mode):
    return dict(config.DEFAULTS, vision_mode=mode)


def test_parse_and_classify():
    p = vision.parse_local_eval('{"description": "Bank site", "sensitive": "true", "reason": "bank"}')
    assert p["sensitive"] and p["description"] == "Bank site"
    assert vision.parse_local_eval("not json")["description"] == "not json"
    assert vision.classify_sensitive("shows a credit card form", False)[0]
    assert not vision.classify_sensitive("editing code", False)[0]


def test_hybrid_keeps_sensitive_frames_local(monkeypatch):
    calls = []
    monkeypatch.setattr(vision.models, "ollama_generate",
                        lambda *a, **k: '{"description": "A bank statement", "sensitive": true, "reason": "bank"}')
    monkeypatch.setattr(vision.models, "online_chat", lambda *a, **k: calls.append(1) or "online")
    w = vision.VisionWorker(lambda: cfg("hybrid"), lambda n: "")
    text, meta = w.describe(cfg("hybrid"), "x.jpg", "", None)
    assert calls == [] and meta["visionSource"] == "local-sensitive" and meta["sensitive"] is True


def test_hybrid_fails_closed(monkeypatch):
    calls = []

    def boom(*a, **k):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(vision.models, "ollama_generate", boom)
    monkeypatch.setattr(vision.models, "online_chat", lambda *a, **k: calls.append(1) or "online")
    w = vision.VisionWorker(lambda: cfg("hybrid"), lambda n: "")
    text, meta = w.describe(cfg("hybrid"), "x.jpg", "", None)
    assert text == "" and calls == [] and meta["visionSource"] == "local-failed"


def test_hybrid_merges_non_sensitive(monkeypatch):
    answers = iter(['{"description": "Writing code", "sensitive": false}', "Merged summary"])
    monkeypatch.setattr(vision.models, "ollama_generate", lambda *a, **k: next(answers))
    monkeypatch.setattr(vision.models, "online_chat", lambda *a, **k: "Online says code")
    w = vision.VisionWorker(lambda: cfg("hybrid"), lambda n: "")
    text, meta = w.describe(cfg("hybrid"), "x.jpg", "", None)
    assert text == "Merged summary" and meta["visionSource"] == "hybrid"


def test_change_text_withheld_on_sensitive():
    d = {"added": ["a long line of new text"], "removed": [], "change_ratio": 0.5, "first_seen": False}
    assert "changeText" in vision.diff_meta(d, {}, cfg("local"))
    assert "changeText" not in vision.diff_meta(d, {"sensitive": True}, cfg("local"))
    assert "changeText" not in vision.diff_meta(d, {}, dict(cfg("local"), diff_include_text=False))


def test_off_mode_deletes_waiting_frames(tmp_path, monkeypatch):
    c = dict(config.DEFAULTS, vision_mode="off", spool_dir=str(tmp_path))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "frame-1.jpg").write_bytes(b"x")
    (frames / "frame-1.json").write_text("{}")
    w = vision.VisionWorker(lambda: c, lambda n: "")
    assert w.process_next() is False
    assert list(frames.iterdir()) == []


def test_handle_stores_scrubbed_event(monkeypatch, tmp_path):
    monkeypatch.setattr(vision.models, "ollama_generate", lambda *a, **k: "Reading docs; api_key=zzz")
    w = vision.VisionWorker(lambda: cfg("local"), lambda n: "")
    desc = w.handle(cfg("local"), str(tmp_path / "f.jpg"), {"proc": "Browser", "title": "Docs", "text": "x"})
    assert desc == "Reading docs; api_key=[REDACTED]"
    assert store.latest("vision")["content"] == desc
