from config import _deep_merge, load_config


def test_deep_merge_overlay_overrides_leaf():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    overlay = {"a": {"y": 20, "z": 30}, "c": 4}
    out = _deep_merge(base, overlay)
    assert out == {"a": {"x": 1, "y": 20, "z": 30}, "b": 3, "c": 4}
    # base untouched
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_load_real_config_shape():
    cfg = load_config()
    for section in ("wsserver", "asr", "vlm", "agent", "tts", "mcp", "queue", "notifier", "idle"):
        assert section in cfg, section
    assert cfg["wsserver"]["port"] == 8765
    assert cfg["agent"]["ctx_size"] == 8192
