"""Half-duplex speak/listen gate + AudioGate.reset (Plan §8.7)."""

from audiogate import AudioGate
from half_duplex import SpeakingState

FRAME = b"\x00" * 640  # 20 ms @ 16 kHz int16


class FakeVad:
    def __init__(self):
        self.next = False

    def is_speech(self, frame, sr):
        return self.next


def make_gate():
    return AudioGate(FakeVad(), sr=16000, frame_ms=20, preroll_ms=40, hangover_ms=40)


def test_not_speaking_initially():
    s = SpeakingState(hangover_s=0.5)
    assert s.is_speaking() is False


def test_speaking_while_entered():
    s = SpeakingState(hangover_s=0.5)
    s.enter()
    assert s.is_speaking() is True
    s.exit()
    # still within hangover immediately after exit
    assert s.is_speaking() is True


def test_zero_hangover_releases_immediately():
    s = SpeakingState(hangover_s=0.0)
    s.enter()
    s.exit()
    assert s.is_speaking() is False


def test_nested_playbacks_refcount():
    s = SpeakingState(hangover_s=0.0)
    s.enter()
    s.enter()
    s.exit()
    assert s.is_speaking() is True  # one playback still active
    s.exit()
    assert s.is_speaking() is False


def test_reset_drops_active_burst():
    g = make_gate()
    g.vad.next = True
    g.feed(FRAME)
    assert g.active is True
    g.reset()
    assert g.active is False
    assert len(g.preroll) == 0
    # after reset, a silent frame doesn't spuriously emit 'end'
    g.vad.next = False
    assert g.feed(FRAME) == (None, b"")
