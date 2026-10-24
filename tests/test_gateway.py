import asyncio

from xknx.dpt import DPTArray, DPTBinary

from artnet2knx.artnet import DmxPacket
from artnet2knx.config import ChannelMapping, MappingType
from artnet2knx.gateway import Gateway, dmx_to_knx
from artnet2knx.knx import build_telegram


def mapping(**kw) -> ChannelMapping:
    base = dict(universe=0, channel=1, type=MappingType.DIM, group_address="1/1/1")
    base.update(kw)
    return ChannelMapping(**base)


def dmx(values: dict[int, int], universe: int = 0) -> DmxPacket:
    data = bytearray(512)
    for ch, v in values.items():
        data[ch - 1] = v
    return DmxPacket(universe=universe, sequence=0, data=bytes(data))


def test_dmx_to_knx():
    sw = mapping(type=MappingType.SWITCH, threshold=128)
    assert dmx_to_knx(sw, 127) is False
    assert dmx_to_knx(sw, 128) is True
    assert dmx_to_knx(mapping(type=MappingType.SWITCH, invert=True), 0) is True
    assert dmx_to_knx(mapping(), 200) == 200
    assert dmx_to_knx(mapping(invert=True), 200) == 55


def test_build_telegram():
    assert build_telegram("1/0/1", True).payload.value == DPTBinary(1)
    assert build_telegram("1/0/1", False).payload.value == DPTBinary(0)
    assert build_telegram("1/1/1", 128).payload.value == DPTArray((128,))


async def test_only_changes_are_sent():
    sent = []
    gw = Gateway(
        [
            mapping(channel=1, type=MappingType.SWITCH, group_address="1/0/1"),
            mapping(channel=2, group_address="1/1/2"),
        ],
        lambda ga, v: sent.append((ga, v)),
    )
    gw.handle_dmx(dmx({1: 0, 2: 0}))
    assert sent == [("1/0/1", False), ("1/1/2", 0)]
    gw.handle_dmx(dmx({1: 0, 2: 0}))
    gw.handle_dmx(dmx({1: 50, 2: 0}))  # unter Schwelle -> bleibt aus
    assert len(sent) == 2
    gw.handle_dmx(dmx({1: 255, 2: 10}))
    assert sent[2:] == [("1/0/1", True), ("1/1/2", 10)]
    gw.close()


async def test_other_universe_ignored():
    sent = []
    gw = Gateway([mapping(universe=1)], lambda ga, v: sent.append(v))
    gw.handle_dmx(dmx({1: 99}, universe=0))
    gw.handle_dmx(DmxPacket(universe=1, sequence=0, data=b""))  # zu kurz
    assert sent == []
    gw.close()


async def test_throttle_sends_last_value():
    sent = []
    gw = Gateway([mapping(min_interval=0.05)], lambda ga, v: sent.append(v))
    for v in (10, 20, 30, 40):
        gw.handle_dmx(dmx({1: v}))
    assert sent == [10]  # sofort der erste Wert
    await asyncio.sleep(0.08)
    assert sent == [10, 40]  # dann nur der letzte
    await asyncio.sleep(0.08)
    assert sent == [10, 40]
    gw.close()


async def test_throttle_skips_if_back_to_sent_value():
    sent = []
    gw = Gateway([mapping(min_interval=0.05)], lambda ga, v: sent.append(v))
    gw.handle_dmx(dmx({1: 10}))
    gw.handle_dmx(dmx({1: 20}))
    gw.handle_dmx(dmx({1: 10}))
    await asyncio.sleep(0.08)
    assert sent == [10]
    gw.close()


async def test_sender_error_does_not_break_gateway():
    calls = []

    def sender(ga, v):
        calls.append(v)
        raise RuntimeError("boom")

    gw = Gateway([mapping()], sender)
    gw.handle_dmx(dmx({1: 1}))
    gw.handle_dmx(dmx({1: 2}))
    assert calls == [1, 2]
    gw.close()


async def test_on_sent_and_state():
    seen = []
    gw = Gateway([mapping()], lambda ga, v: None, on_sent=lambda m, v: seen.append((m.group_address, v)))
    out = gw.outputs[0]
    assert out.dmx_value is None and out.last_sent is None
    gw.handle_dmx(dmx({1: 42}))
    assert seen == [("1/1/1", 42)]
    assert out.dmx_value == 42 and out.last_sent == 42 and out.sent_count == 1
    assert out.last_sent_at is not None
    gw.close()
