import struct

import pytest

from artnet2knx.artnet import (
    OP_DMX,
    OP_POLL,
    POLL_REPLY_LENGTH,
    build_poll_replies,
    parse_packet,
    parse_universe,
)


def artdmx(universe: int, data: bytes, seq: int = 0) -> bytes:
    return (
        b"Art-Net\x00"
        + struct.pack("<H", OP_DMX)
        + struct.pack(">H", 14)
        + bytes([seq, 0, universe & 0xFF, universe >> 8])
        + struct.pack(">H", len(data))
        + data
    )


def test_parse_dmx():
    opcode, pkt = parse_packet(artdmx(0x123, bytes([1, 2, 3, 4]), seq=7))
    assert opcode == OP_DMX
    assert pkt.universe == 0x123
    assert pkt.sequence == 7
    assert pkt.data == bytes([1, 2, 3, 4])


def test_parse_poll_and_garbage():
    poll = b"Art-Net\x00" + struct.pack("<H", OP_POLL) + b"\x00\x0e\x00\x00"
    assert parse_packet(poll) == (OP_POLL, None)
    assert parse_packet(b"hello world") == (0, None)


@pytest.mark.parametrize(
    "value,expected",
    [(0, 0), (17, 17), ("5", 5), ("0:0:1", 1), ("1:2:3", 0x123), ("127:15:15", 0x7FFF)],
)
def test_parse_universe(value, expected):
    assert parse_universe(value) == expected


@pytest.mark.parametrize("value", [-1, 0x8000, "0:16:0", "1:2", True, "x"])
def test_parse_universe_invalid(value):
    with pytest.raises(ValueError):
        parse_universe(value)


def test_poll_reply_groups_ports():
    replies = build_poll_replies("10.0.0.5", [0, 1, 2, 3, 4, 0x100], "short", "long")
    # Net 0/Sub 0: 5 Universen -> 2 Pakete, Net 1/Sub 0: 1 Paket
    assert len(replies) == 3
    for r in replies:
        assert len(r) == POLL_REPLY_LENGTH
        assert r[:8] == b"Art-Net\x00"
        assert struct.unpack_from("<H", r, 8)[0] == 0x2100
        assert r[10:14] == bytes([10, 0, 0, 5])
    first = replies[0]
    assert struct.unpack_from(">H", first, 172)[0] == 4
    assert list(first[190:194]) == [0, 1, 2, 3]
    assert replies[2][18] == 1  # NetSwitch
    assert [r[211] for r in replies] == [1, 2, 3]  # BindIndex
