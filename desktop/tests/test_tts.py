import numpy as np

from tts import KokoroTTS, PrintingKokoroTTS, PrintingTTS, TTS, build_tts


def _drain(t):
    out = []
    while not t._sentences.empty():
        out.append(t._sentences.get_nowait())
    return out


def test_sentence_splitting_streamed():
    t = TTS(piper_exe="piper", voice_model="missing-voice.onnx")
    assert t.sample_rate == 22050  # default when no voice json present
    t.feed("Hello there. How are")
    assert _drain(t) == ["Hello there."]
    t.feed(" you? Fine.")
    # "How are you?" completes; "Fine." needs trailing space or flush
    assert _drain(t) == ["How are you?"]
    t.flush()
    assert _drain(t) == ["Fine."]


def test_flush_emits_unterminated_tail():
    t = TTS(piper_exe="piper", voice_model="missing-voice.onnx")
    t.feed("an unfinished thought")
    assert _drain(t) == []
    t.flush()
    assert _drain(t) == ["an unfinished thought"]


def test_multiple_sentences_one_feed():
    t = TTS(piper_exe="piper", voice_model="missing-voice.onnx")
    t.feed("One! Two? Three. ")
    assert _drain(t) == ["One!", "Two?", "Three."]


# --- backend selection + Kokoro ----------------------------------------------

def test_build_tts_defaults_to_piper():
    t = build_tts({"piper_exe": "piper", "voice_model": "missing.onnx"})
    assert isinstance(t, TTS) and not isinstance(t, KokoroTTS)


def test_build_tts_selects_kokoro():
    t = build_tts({"backend": "kokoro", "kokoro_voice": "af_bella", "kokoro_device": "cpu"})
    assert isinstance(t, KokoroTTS)
    assert t._voice == "af_bella" and t._device == "cpu"
    assert t.sample_rate == 24000


def test_build_tts_echo_returns_printing_variants():
    assert isinstance(build_tts({"piper_exe": "p", "voice_model": "m"}, echo=True), PrintingTTS)
    assert isinstance(build_tts({"backend": "kokoro"}, echo=True), PrintingKokoroTTS)


class _FakePipe:
    def __call__(self, sentence, voice):
        yield ("g", "p", np.array([0.0, 0.5, -0.5, 2.0], dtype=np.float32))  # 2.0 clips to 1.0


def test_kokoro_render_converts_float_to_int16_pcm():
    k = KokoroTTS(device="cpu")
    k._pipeline = _FakePipe()  # bypass the real (heavy) model load
    pcm = k._render("hi there")
    got = np.frombuffer(pcm, dtype=np.int16)
    expected = (np.clip(np.array([0.0, 0.5, -0.5, 2.0]), -1.0, 1.0) * 32767.0).astype(np.int16)
    assert np.array_equal(got, expected)


def test_kokoro_render_empty_when_no_audio():
    class _Empty:
        def __call__(self, sentence, voice):
            return iter(())

    k = KokoroTTS(device="cpu")
    k._pipeline = _Empty()
    assert k._render("x") == b""
