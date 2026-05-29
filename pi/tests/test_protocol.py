"""The Pi protocol copy must behave identically to the desktop one (Plan §3.2)."""

import pytest

import protocol


def test_roundtrip():
    for ch in (protocol.CH_CONTROL, protocol.CH_AUDIO, protocol.CH_VIDEO, protocol.CH_UART):
        out_ch, payload = protocol.decode_frame(protocol.encode_frame(ch, b"data"))
        assert out_ch == ch and payload == b"data"


def test_control_and_uart_helpers():
    ch, payload = protocol.decode_frame(protocol.encode_control({"a": 1}))
    assert ch == protocol.CH_CONTROL and protocol.decode_json(payload) == {"a": 1}
    ch, payload = protocol.decode_frame(protocol.encode_uart('{"type":"ping"}'))
    assert ch == protocol.CH_UART and payload.endswith(b"\n")


def test_errors():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame(b"")
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(0x99, b"x")
