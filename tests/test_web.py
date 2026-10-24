import base64

import pytest
from aiohttp.test_utils import TestClient, TestServer

from artnet2knx.artnet import DmxPacket
from artnet2knx.config import ChannelMapping, Config, MappingType
from artnet2knx.gateway import Gateway
from artnet2knx.status import StatusTracker, UniverseStats, format_value
from artnet2knx.web import create_app


def make(cfg: Config | None = None):
    cfg = cfg or Config()
    mappings = [
        ChannelMapping(0, 1, MappingType.SWITCH, "1/0/1", name="Decke"),
        ChannelMapping(0, 2, MappingType.DIM, "1/1/2"),
        ChannelMapping(1, 5, MappingType.DIM, "2/1/5"),
    ]
    tracker = StatusTracker({m.universe for m in mappings})
    gateway = Gateway(mappings, lambda ga, v: None, on_sent=tracker.record_telegram)
    return cfg, gateway, tracker


def test_format_value():
    assert format_value(MappingType.SWITCH, True) == "EIN"
    assert format_value(MappingType.SWITCH, False) == "AUS"
    assert format_value(MappingType.DIM, 255) == "255 (100 %)"
    assert format_value(MappingType.DIM, None) == "–"


def test_universe_rate():
    stats = UniverseStats(0, mapped=True)
    for i in range(45):  # 44 Abstände à 1/44 s
        stats.record("10.0.0.1", now=i / 44, wall=0.0)
    assert stats.rate == pytest.approx(44.0)
    assert stats.current_rate(now=1.5) == pytest.approx(44.0)
    assert stats.current_rate(now=10.0) == 0.0


async def test_status_api():
    cfg, gateway, tracker = make()
    data = bytearray(512)
    data[0], data[1] = 255, 128
    tracker.record_dmx(0, "10.0.0.9")
    gateway.handle_dmx(DmxPacket(0, 0, bytes(data)))
    tracker.record_dmx(7, "10.0.0.9")  # Universum ohne Mapping

    async with TestClient(TestServer(create_app(cfg, gateway, tracker))) as client:
        resp = await client.get("/api/status")
        assert resp.status == 200
        s = await resp.json()

        assert s["knx"]["state"] == "DRY_RUN"
        assert s["telegram_count"] == 2
        assert [u["universe"] for u in s["universes"]] == [0, 1, 7]
        u0, u1, u7 = s["universes"]
        assert u0["mapped"] and u0["sources"] == ["10.0.0.9"] and u0["packets"] == 1
        assert u1["mapped"] and u1["packets"] == 0 and u1["last_seen_ago"] is None
        assert not u7["mapped"]

        m = {x["group_address"]: x for x in s["mappings"]}
        assert m["1/0/1"]["knx"] == "EIN" and m["1/0/1"]["dmx"] == 255
        assert m["1/1/2"]["knx"] == "128 (50 %)" and m["1/1/2"]["sent_count"] == 1
        assert m["2/1/5"]["dmx"] is None and m["2/1/5"]["knx"] == "–"
        assert s["telegrams"][0]["group_address"] == "1/1/2"  # neueste zuerst

        page = await client.get("/")
        assert page.status == 200
        assert "Artnet2KNX" in await page.text()
    gateway.close()


async def test_basic_auth():
    cfg = Config()
    cfg.web.username, cfg.web.password = "admin", "geheim"
    cfg, gateway, tracker = make(cfg)
    async with TestClient(TestServer(create_app(cfg, gateway, tracker))) as client:
        assert (await client.get("/api/status")).status == 401
        bad = base64.b64encode(b"admin:falsch").decode()
        assert (await client.get("/", headers={"Authorization": f"Basic {bad}"})).status == 401
        assert (await client.get("/", headers={"Authorization": "Basic !!!"})).status == 401
        good = base64.b64encode(b"admin:geheim").decode()
        assert (await client.get("/api/status", headers={"Authorization": f"Basic {good}"})).status == 200
    gateway.close()
