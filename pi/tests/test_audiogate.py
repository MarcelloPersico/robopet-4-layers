"""VAD gate state machine: pre-roll, start, hangover-bounded end (Plan §7.2)."""

from capture import _AudioGate

FRAME = b"\x00" * 640  # 20 ms @ 16 kHz int16


class FakeVad:
    def __init__(self):
        self.next = False

    def is_speech(self, frame, sr):
        return self.next


def make_gate():
    # preroll 40 ms = 2 frames, hangover 40 ms = 2 frames
    return _AudioGate(FakeVad(), sr=16000, frame_ms=20, preroll_ms=40, hangover_ms=40)


def test_preroll_then_start_includes_buffered_frames():
    g = make_gate()
    g.vad.next = False
    assert g.feed(FRAME) == (None, b"")
    assert g.feed(FRAME) == (None, b"")
    g.vad.next = True
    event, audio = g.feed(FRAME)
    assert event == "start"
    # 2 buffered pre-roll frames + the triggering frame
    assert len(audio) == 3 * len(FRAME)
    assert g.active is True


def test_active_streams_frames_then_hangover_end():
    g = make_gate()
    g.vad.next = True
    g.feed(FRAME)  # start
    g.vad.next = True
    assert g.feed(FRAME) == (None, FRAME)  # streamed during speech
    g.vad.next = False
    assert g.feed(FRAME) == (None, FRAME)  # hangover frame 1 still sent
    assert g.feed(FRAME) == ("end", b"")   # hangover reached -> end
    assert g.active is False


def test_restart_after_end():
    g = make_gate()
    g.vad.next = True
    g.feed(FRAME)
    g.vad.next = False
    g.feed(FRAME)
    g.feed(FRAME)  # end
    g.vad.next = True
    event, _ = g.feed(FRAME)
    assert event == "start"
