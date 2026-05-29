import pytest

import protocol


def test_frame_roundtrip_all_channels():
    for ch in (protocol.CH_CONTROL, protocol.CH_AUDIO, protocol.CH_VIDEO, protocol.CH_UART):
        frame = protocol.encode_frame(ch, b"payload\x00\xff")
        assert frame[0] == ch
        out_ch, payload = protocol.decode_frame(frame)
        assert out_ch == ch
        assert payload == b"payload\x00\xff"


def test_empty_frame_raises():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame(b"")


def test_unknown_channel_raises():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame(b"\x09rest")
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(0x42, b"x")


def test_control_roundtrip():
    obj = {"type": "vad", "event": "start", "n": 3}
    ch, payload = protocol.decode_frame(protocol.encode_control(obj))
    assert ch == protocol.CH_CONTROL
    assert protocol.decode_json(payload) == obj


def test_uart_appends_newline():
    ch, payload = protocol.decode_frame(protocol.encode_uart('{"type":"ping"}'))
    assert ch == protocol.CH_UART
    assert payload.endswith(b"\n")
    # already-terminated lines are not double-terminated
    _, payload2 = protocol.decode_frame(protocol.encode_uart("x\n"))
    assert payload2 == b"x\n"
