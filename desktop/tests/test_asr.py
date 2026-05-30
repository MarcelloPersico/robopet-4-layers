"""ASR anti-hallucination filters (energy gate + per-segment confidence)."""

from dataclasses import dataclass

import numpy as np

from asr import ASR


@dataclass
class FakeSeg:
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


class FakeModel:
    def __init__(self, segs):
        self._segs = segs
        self.kwargs = None

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        return iter(self._segs), {}


def _asr(segs):
    a = ASR("m")
    a._model = FakeModel(segs)
    return a


def test_too_quiet_detects_silence():
    assert ASR._too_quiet(np.zeros(16000, dtype=np.float32)) is True
    loud = np.full(16000, 0.2, dtype=np.float32)
    assert ASR._too_quiet(loud) is False


def test_drops_high_no_speech_prob_segment():
    a = _asr([FakeSeg("Thank you.", no_speech_prob=0.95, avg_logprob=-0.2)])
    assert a._run_model(np.full(16000, 0.2, dtype=np.float32), partial=False) == ""


def test_drops_low_confidence_segment():
    a = _asr([FakeSeg("garbled", no_speech_prob=0.1, avg_logprob=-2.5)])
    assert a._run_model(np.full(16000, 0.2, dtype=np.float32), partial=False) == ""


def test_keeps_confident_speech():
    a = _asr([FakeSeg("hello there", no_speech_prob=0.05, avg_logprob=-0.3)])
    assert a._run_model(np.full(16000, 0.2, dtype=np.float32), partial=False) == "hello there"


def test_drops_bare_hallucination_phrase():
    a = _asr([FakeSeg("you", no_speech_prob=0.1, avg_logprob=-0.3)])
    assert a._run_model(np.full(16000, 0.2, dtype=np.float32), partial=False) == ""


def test_partial_pass_disables_vad_filter():
    a = _asr([FakeSeg("hi", no_speech_prob=0.1, avg_logprob=-0.3)])
    a._vad_filter = True
    a._run_model(np.full(16000, 0.2, dtype=np.float32), partial=True)
    assert a._model.kwargs["vad_filter"] is False  # partials stay cheap
