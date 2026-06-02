"""MoodState unit tests: clamping, decay→baseline, circadian swing, emotion
mapping into the OLED enum, and JSON round-trip. Plan §12."""

import re

import pytest

from mood import OLED_EMOTIONS, MoodState
from tools import AGENT_TOOL_SPECS


def _set_emotion_enum() -> set[str]:
    for spec in AGENT_TOOL_SPECS:
        if spec["function"]["name"] == "set_emotion":
            return set(spec["function"]["parameters"]["properties"]["emotion"]["enum"])
    raise AssertionError("set_emotion spec not found")


def test_oled_emotions_match_tools_enum():
    # Guard against drift between mood.py and the set_emotion tool schema.
    assert OLED_EMOTIONS == _set_emotion_enum()


def test_update_clamps_to_unit_range():
    m = MoodState(pleasure=0.0, arousal=0.0)
    m.update(dp=5.0, da=-5.0)
    assert m.pleasure == 1.0
    assert m.arousal == -1.0


def test_first_decay_only_anchors_clock():
    m = MoodState(pleasure=1.0, half_life_s=100.0, circadian=False, baseline_pleasure=0.0)
    m.decay(now_mono=0.0)  # first call: no change, just sets the clock
    assert m.pleasure == 1.0


def test_decay_halves_toward_baseline_over_one_half_life():
    m = MoodState(pleasure=1.0, half_life_s=100.0, circadian=False, baseline_pleasure=0.0)
    m.decay(now_mono=0.0)
    m.decay(now_mono=100.0)  # one half-life elapsed → halfway to baseline (0)
    assert m.pleasure == pytest.approx(0.5)


def test_decay_converges_to_nonzero_baseline():
    m = MoodState(pleasure=0.0, half_life_s=50.0, circadian=False, baseline_pleasure=0.4)
    m.decay(now_mono=0.0)
    for t in range(50, 1000, 50):
        m.decay(now_mono=float(t))
    assert m.pleasure == pytest.approx(0.4, abs=0.02)


def test_circadian_baseline_calmer_at_night():
    m = MoodState(circadian=True)
    _, arousal_day, _ = m._baseline(14)   # mid-afternoon
    _, arousal_night, _ = m._baseline(3)  # pre-dawn
    assert arousal_day > arousal_night


def test_suggest_emotion_always_in_enum():
    for p in [-1.0, -0.5, -0.2, 0.0, 0.2, 0.5, 1.0]:
        for a in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            m = MoodState(pleasure=p, arousal=a)
            assert m.suggest_emotion() in OLED_EMOTIONS


def test_suggest_emotion_specific_cases():
    assert MoodState(pleasure=0.0, arousal=-0.8).suggest_emotion() == "sleepy"
    assert MoodState(pleasure=0.6, arousal=0.6).suggest_emotion() == "excited"
    assert MoodState(pleasure=0.5, arousal=0.0).suggest_emotion() == "happy"
    assert MoodState(pleasure=-0.6, arousal=0.0).suggest_emotion() == "sad"


def test_render_is_one_short_line():
    line = MoodState(pleasure=0.2, arousal=-0.1).render()
    assert "\n" not in line
    assert re.search(r"pleasure [+-]\d", line)


def test_json_round_trip():
    m = MoodState(pleasure=0.3, arousal=-0.2, dominance=0.1, half_life_s=900.0,
                  circadian=False, baseline_pleasure=0.2)
    m2 = MoodState.from_json(m.to_json())
    assert (m2.pleasure, m2.arousal, m2.dominance) == (0.3, -0.2, 0.1)
    assert m2.half_life_s == 900.0 and m2.circadian is False and m2.baseline_pleasure == 0.2


def test_from_json_bad_input_returns_default():
    m = MoodState.from_json("not json{")
    assert (m.pleasure, m.arousal, m.dominance) == (0.0, 0.0, 0.0)
