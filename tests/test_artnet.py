import asyncio
import logging
import struct

import pytest

from artnet2knx.artnet import (
    OP_DMX,
    OP_POLL,
    POLL_REPLY_LENGTH,
    ArtNetProtocol,
    build_poll_replies,
    parse_packet,
    parse_poll_target,
    parse_universe,
    start_artnet_listener,
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


def artpoll(flags: int = 0, top: int | None = None, bottom: int | None = None) -> bytes:
    pkt = b"Art-Net\x00" + struct.pack("<H", OP_POLL) + b"\x00\x0e" + bytes([flags, 0])
    if top is not None:
        pkt += struct.pack(">HH", top, bottom)
    return pkt


def test_parse_poll_target():
    assert parse_poll_target(artpoll()) is None  # 14 Bytes, Art-Net 4 Minimum
    assert parse_poll_target(artpoll(flags=0x02, top=10, bottom=5)) is None
    assert parse_poll_target(artpoll(flags=0x20, top=40, bottom=30)) == (30, 40)
    assert parse_poll_target(artpoll(flags=0x20)) == (0, 0)  # fehlende Felder = 0


def test_node_report_counter():
    first = build_poll_replies("10.0.0.5", [0], "s", "l", counter=1)[0]
    later = build_poll_replies("10.0.0.5", [0], "s", "l", counter=10001)[0]
    assert first[108:172].rstrip(b"\x00") == b"#0001 [0001] OK"
    assert later[108:172].rstrip(b"\x00") == b"#0001 [0001] OK"  # Überlauf bei 9999
    assert build_poll_replies("10.0.0.5", [0], "s", "l", counter=42)[0][108:120] == b"#0001 [0042]"


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def is_closing(self):
        return False


def make_protocol(universes, **kw):
    polls = []
    proto = ArtNetProtocol(
        lambda pkt: None,
        universes,
        on_poll=lambda ip, replied, note: polls.append((ip, replied, note)),
        announce_ip="10.0.0.5",
        reply_delay_max=0,
        **kw,
    )
    transport = FakeTransport()
    proto.connection_made(transport)
    return proto, transport, polls


async def test_poll_reply_sent_with_counter():
    proto, transport, polls = make_protocol([38])
    proto.datagram_received(artpoll(), ("172.17.77.53", 6454))
    proto.datagram_received(artpoll(), ("172.17.77.53", 6454))
    await asyncio.sleep(0.01)
    assert polls == [("172.17.77.53", True, ""), ("172.17.77.53", True, "")]
    assert [addr for _, addr in transport.sent] == [("172.17.77.53", 6454)] * 2
    reply = transport.sent[1][0]
    assert reply[108:120] == b"#0001 [0002]"
    assert reply[19] == 2 and reply[190] == 6  # Universum 38 = 0:2:6


async def test_poll_reply_is_delayed():
    proto, transport, _ = make_protocol([0])
    proto._reply_delay_max = 0.05
    proto.datagram_received(artpoll(), ("10.0.0.9", 6454))
    assert transport.sent == []  # nicht sofort, sondern nach zufälliger Wartezeit
    await asyncio.sleep(0.08)
    assert len(transport.sent) == 1


async def test_targeted_mode():
    proto, transport, polls = make_protocol([38])
    proto.datagram_received(artpoll(flags=0x20, top=20, bottom=0), ("10.0.0.9", 6454))
    proto.datagram_received(artpoll(flags=0x20, top=40, bottom=30), ("10.0.0.9", 6454))
    await asyncio.sleep(0.01)
    assert polls[0] == ("10.0.0.9", False, "Targeted Mode 0-20: kein eigenes Universum im Bereich")
    assert polls[1] == ("10.0.0.9", True, "Targeted Mode 30-40")
    assert len(transport.sent) == 1


async def test_poll_reply_disabled_is_reported():
    proto, transport, polls = make_protocol([0], poll_reply=False)
    proto.datagram_received(artpoll(), ("10.0.0.9", 6454))
    await asyncio.sleep(0.01)
    assert polls == [("10.0.0.9", False, "poll_reply ist aus")]
    assert transport.sent == []


async def test_bind_ip_warning(caplog):
    caplog.set_level(logging.WARNING)
    proto, _, _ = make_protocol([0])
    transport = await start_artnet_listener(proto, "127.0.0.1", 0)
    transport.close()
    assert "Broadcasts" in caplog.text
    caplog.clear()
    transport = await start_artnet_listener(make_protocol([0])[0], "0.0.0.0", 0)
    transport.close()
    assert "Broadcasts" not in caplog.text
