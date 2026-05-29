"""Desktop AudioGate must behave identically to the Pi's (Plan §7.2)."""

from audiogate import AudioGate

FRAME = b"\x00" * 640  # 20 ms @ 16 kHz int16


class FakeVad:
    def __init__(self):
        self.next = False

    def is_speech(self, frame, sr):
        return self.next


def make_gate():
    return AudioGate(FakeVad(), sr=16000, frame_ms=20, preroll_ms=40, hangover_ms=40)


def test_preroll_included_on_start():
    g = make_gate()
    g.feed(FRAME)
    g.feed(FRAME)  # 2 buffered pre-roll frames
    g.vad.next = True
    event, audio = g.feed(FRAME)
    assert event == "start"
    assert len(audio) == 3 * len(FRAME)


def test_hangover_end():
    g = make_gate()
    g.vad.next = True
    g.feed(FRAME)
    g.vad.next = False
    assert g.feed(FRAME) == (None, FRAME)  # hangover frame still sent
    assert g.feed(FRAME) == ("end", b"")
    assert g.active is False
