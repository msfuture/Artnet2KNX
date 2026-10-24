"""Laden und Prüfen der Konfigurationsdateien."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from xknx.telegram import GroupAddress
from xknx.exceptions import CouldNotParseAddress

from .artnet import ARTNET_PORT, parse_universe


class ConfigError(Exception):
    """Fehler in einer Konfigurationsdatei."""


class MappingType(str, Enum):
    SWITCH = "switch"  # DPT 1.001, An/Aus über Schwellwert
    DIM = "dim"  # DPT 5.001, Byte-Wert 0-255


@dataclass
class KnxConfig:
    connection_type: str = "routing"
    multicast_group: str = "224.0.23.12"
    multicast_port: int = 3671
    individual_address: str = "15.15.250"
    local_ip: str | None = None
    gateway_ip: str | None = None
    gateway_port: int = 3671
    route_back: bool = False
    rate_limit: int = 20


@dataclass
class ArtNetConfig:
    bind_ip: str = "0.0.0.0"
    port: int = ARTNET_PORT
    poll_reply: bool = True
    announce_ip: str | None = None
    short_name: str = "Artnet2KNX"
    long_name: str = "Art-Net to KNX Gateway"


@dataclass
class Defaults:
    switch_threshold: int = 128
    switch_min_interval: float = 0.0
    dim_min_interval: float = 0.2


@dataclass
class ChannelMapping:
    universe: int
    channel: int  # 1..512
    type: MappingType
    group_address: str
    name: str = ""
    threshold: int = 128  # nur switch
    invert: bool = False
    min_interval: float = 0.0

    @property
    def label(self) -> str:
        base = f"U{self.universe}/K{self.channel} -> {self.group_address} ({self.type.value})"
        return f"{base} '{self.name}'" if self.name else base


@dataclass
class Config:
    knx: KnxConfig = field(default_factory=KnxConfig)
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    defaults: Defaults = field(default_factory=Defaults)
    log_level: str = "INFO"
    mapping_file: Path = Path("mapping.yaml")
    mappings: list[ChannelMapping] = field(default_factory=list)


CONNECTION_TYPES = {"routing", "tunneling", "tunneling_tcp", "automatic"}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as err:
        raise ConfigError(f"Datei nicht gefunden: {path}") from err
    except yaml.YAMLError as err:
        raise ConfigError(f"YAML-Fehler in {path}: {err}") from err
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: Oberste Ebene muss ein Mapping sein")
    return data


def _fill(obj: Any, values: dict[str, Any] | None, section: str) -> None:
    for key, value in (values or {}).items():
        if not hasattr(obj, key):
            raise ConfigError(f"Unbekannte Option '{section}.{key}'")
        setattr(obj, key, value)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    data = _load_yaml(path)
    cfg = Config()
    known = {"knx", "artnet", "defaults", "logging", "mapping_file"}
    for key in data:
        if key not in known:
            raise ConfigError(f"Unbekannte Option '{key}' in {path}")

    _fill(cfg.knx, data.get("knx"), "knx")
    _fill(cfg.artnet, data.get("artnet"), "artnet")
    _fill(cfg.defaults, data.get("defaults"), "defaults")
    cfg.log_level = str((data.get("logging") or {}).get("level", cfg.log_level)).upper()

    cfg.knx.connection_type = str(cfg.knx.connection_type).lower()
    if cfg.knx.connection_type not in CONNECTION_TYPES:
        raise ConfigError(
            f"knx.connection_type muss eines von {sorted(CONNECTION_TYPES)} sein"
        )
    if cfg.knx.connection_type.startswith("tunneling") and not cfg.knx.gateway_ip:
        raise ConfigError("knx.gateway_ip ist für Tunneling erforderlich")

    mapping_file = Path(data.get("mapping_file", cfg.mapping_file))
    if not mapping_file.is_absolute():
        mapping_file = path.parent / mapping_file
    cfg.mapping_file = mapping_file
    cfg.mappings = load_mappings(mapping_file, cfg.defaults)
    return cfg


def load_mappings(path: Path, defaults: Defaults) -> list[ChannelMapping]:
    data = _load_yaml(path)
    universes = data.get("universes")
    if not isinstance(universes, list) or not universes:
        raise ConfigError(f"{path}: 'universes' muss eine nicht-leere Liste sein")

    mappings: list[ChannelMapping] = []
    for u_idx, uni_entry in enumerate(universes, start=1):
        where = f"{path}: universes[{u_idx}]"
        if not isinstance(uni_entry, dict) or "universe" not in uni_entry:
            raise ConfigError(f"{where}: 'universe' fehlt")
        try:
            universe = parse_universe(uni_entry["universe"])
        except (ValueError, TypeError) as err:
            raise ConfigError(f"{where}: {err}") from err

        channels = uni_entry.get("channels")
        if not isinstance(channels, list):
            raise ConfigError(f"{where}: 'channels' muss eine Liste sein")
        for c_idx, ch in enumerate(channels, start=1):
            mappings.append(
                _parse_channel(ch, universe, defaults, f"{where}.channels[{c_idx}]")
            )
    return mappings


def _parse_channel(
    ch: Any, universe: int, defaults: Defaults, where: str
) -> ChannelMapping:
    if not isinstance(ch, dict):
        raise ConfigError(f"{where}: Eintrag muss ein Mapping sein")
    allowed = {"channel", "type", "group_address", "name", "threshold", "invert", "min_interval"}
    unknown = set(ch) - allowed
    if unknown:
        raise ConfigError(f"{where}: unbekannte Optionen {sorted(unknown)}")

    channel = ch.get("channel")
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 512:
        raise ConfigError(f"{where}: 'channel' muss eine Zahl 1..512 sein")

    try:
        mtype = MappingType(str(ch.get("type", "")).lower())
    except ValueError as err:
        raise ConfigError(f"{where}: 'type' muss 'switch' oder 'dim' sein") from err

    ga = ch.get("group_address")
    if ga is None:
        raise ConfigError(f"{where}: 'group_address' fehlt")
    try:
        GroupAddress(str(ga))
    except CouldNotParseAddress as err:
        raise ConfigError(f"{where}: ungültige Gruppenadresse {ga!r}") from err

    threshold = ch.get("threshold", defaults.switch_threshold)
    if not isinstance(threshold, int) or not 1 <= threshold <= 255:
        raise ConfigError(f"{where}: 'threshold' muss 1..255 sein")

    default_interval = (
        defaults.switch_min_interval if mtype is MappingType.SWITCH else defaults.dim_min_interval
    )
    min_interval = ch.get("min_interval", default_interval)
    if not isinstance(min_interval, (int, float)) or min_interval < 0:
        raise ConfigError(f"{where}: 'min_interval' muss >= 0 sein")

    return ChannelMapping(
        universe=universe,
        channel=channel,
        type=mtype,
        group_address=str(ga),
        name=str(ch.get("name", "")),
        threshold=threshold,
        invert=bool(ch.get("invert", False)),
        min_interval=float(min_interval),
    )
