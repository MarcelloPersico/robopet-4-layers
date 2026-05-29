from tts import TTS


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
