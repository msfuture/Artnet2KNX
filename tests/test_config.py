from pathlib import Path

import pytest

from artnet2knx.config import ConfigError, MappingType, load_config

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, config: str, mapping: str) -> Path:
    (tmp_path / "mapping.yaml").write_text(mapping, encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    return cfg


MAPPING = """
universes:
  - universe: "0:0:2"
    channels:
      - {channel: 1, type: switch, group_address: 1/0/1}
      - {channel: 2, type: dim, group_address: 1/1/2, name: Test}
"""


def test_example_config_is_valid():
    cfg = load_config(ROOT / "config" / "config.yaml")
    assert cfg.knx.connection_type == "routing"
    assert cfg.knx.multicast_group == "224.0.23.12"
    assert len(cfg.mappings) == 4


def test_defaults_applied(tmp_path):
    cfg = load_config(
        write(tmp_path, "defaults: {switch_threshold: 1, dim_min_interval: 0.5}\n", MAPPING)
    )
    sw, dim = cfg.mappings
    assert sw.universe == 2 and sw.type is MappingType.SWITCH and sw.threshold == 1
    assert sw.min_interval == 0.0
    assert dim.min_interval == 0.5 and dim.name == "Test"


@pytest.mark.parametrize(
    "config,mapping,message",
    [
        ("foo: 1\n", MAPPING, "Unbekannte Option"),
        ("knx: {bar: 1}\n", MAPPING, "knx.bar"),
        ("knx: {connection_type: tunneling}\n", MAPPING, "gateway_ip"),
        ("", "universes: []\n", "nicht-leere Liste"),
        ("", "universes: [{universe: 0, channels: [{channel: 513, type: dim, group_address: 1/1/1}]}]", "channel"),
        ("", "universes: [{universe: 0, channels: [{channel: 1, type: blink, group_address: 1/1/1}]}]", "type"),
        ("", "universes: [{universe: 0, channels: [{channel: 1, type: dim, group_address: 99/9/9}]}]", "Gruppenadresse"),
        ("", "universes: [{universe: 0, channels: [{channel: 1, type: dim}]}]", "group_address"),
        ("", "universes: [{universe: 99999, channels: []}]", "Universum"),
        ("web: {port: 0}\n", MAPPING, "web.port"),
        ("web: {username: admin}\n", MAPPING, "gemeinsam"),
    ],
)
def test_invalid(tmp_path, config, mapping, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, config, mapping))
