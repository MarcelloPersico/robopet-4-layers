import time

from state import RECENT_ANSWERS_MAX, ResolvedFact, WorldState


def test_conversation_and_render():
    ws = WorldState()
    ws.add_user_turn("hi")
    ws.add_assistant_turn("hello")
    rendered = ws.render_conversation()
    assert "user: hi" in rendered and "assistant: hello" in rendered


def test_recent_answers_render_and_cap():
    ws = WorldState()
    assert ws.render_recent_answers() == "(none yet)"
    for i in range(RECENT_ANSWERS_MAX + 10):
        ws.add_resolution(ResolvedFact("object", f"topic{i}", f"res{i}"))
    assert len(ws.recent_answers) == RECENT_ANSWERS_MAX
    text = ws.render_recent_answers()
    assert "res9" not in text.split("\n")[0]  # oldest evicted
    assert "[object] topic" in text


def test_fresh_vision_expiry(monkeypatch):
    ws = WorldState()
    assert ws.fresh_vision() is None
    ws.set_vision("a mug")
    assert ws.fresh_vision() == "a mug"
    # force staleness
    ws._last_see_mono = time.monotonic() - 999
    assert ws.fresh_vision() is None


def test_load_resolutions_truncates_to_cap():
    ws = WorldState()
    facts = [ResolvedFact("c", f"t{i}", f"r{i}") for i in range(RECENT_ANSWERS_MAX + 5)]
    ws.load_resolutions(facts)
    assert len(ws.recent_answers) == RECENT_ANSWERS_MAX
    assert ws.recent_answers[-1].resolution == f"r{RECENT_ANSWERS_MAX + 4}"


def test_telemetry_line():
    ws = WorldState()
    assert "no telemetry" in ws.render_telemetry_line()
    ws.set_telemetry({"mode": "active", "vel_l": 0.1, "vel_r": 0.12, "link_age_ms": 20})
    assert "mode=active" in ws.render_telemetry_line()
