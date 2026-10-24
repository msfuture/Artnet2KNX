"""Laufzeit-Statistiken für die Statusseite."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .config import ChannelMapping, MappingType

RATE_WINDOW = 1.0  # Sekunden, über die die Paketrate gemittelt wird
STALE_AFTER = 3.0  # Sekunden ohne Paket -> Rate 0 / "inaktiv"


@dataclass
class UniverseStats:
    universe: int
    mapped: bool
    packets: int = 0
    last_seen: float = 0.0  # time.monotonic()
    sources: dict[str, float] = field(default_factory=dict)  # IP -> time.time()
    rate: float = 0.0
    _window_start: float = 0.0
    _window_count: int = 0

    def record(self, source_ip: str, now: float, wall: float) -> None:
        first = self.packets == 0 or now - self.last_seen > STALE_AFTER
        self.packets += 1
        self.last_seen = now
        self.sources[source_ip] = wall
        if first:
            # Startpunkt des Fensters; gezählt werden die Abstände danach
            self._window_start, self._window_count = now, 0
            return
        self._window_count += 1
        elapsed = now - self._window_start
        if elapsed >= RATE_WINDOW:
            self.rate = self._window_count / elapsed
            self._window_start, self._window_count = now, 0

    def current_rate(self, now: float) -> float:
        return 0.0 if now - self.last_seen > STALE_AFTER else self.rate


@dataclass(frozen=True)
class TelegramLogEntry:
    time: float  # time.time()
    group_address: str
    name: str
    type: MappingType
    value: bool | int


def format_value(mtype: MappingType, value: bool | int | None) -> str:
    if value is None:
        return "–"
    if mtype is MappingType.SWITCH:
        return "EIN" if value else "AUS"
    return f"{value} ({round(value * 100 / 255)} %)"


class StatusTracker:
    """Sammelt Art-Net Empfangsstatistiken und gesendete Telegramme."""

    def __init__(self, mapped_universes: set[int], log_size: int = 100):
        self.started_at = time.time()
        self._mapped = set(mapped_universes)
        self.universes: dict[int, UniverseStats] = {}
        self.telegrams: deque[TelegramLogEntry] = deque(maxlen=log_size)
        self.telegram_count = 0

    def record_dmx(self, universe: int, source_ip: str) -> None:
        stats = self.universes.get(universe)
        if stats is None:
            stats = self.universes[universe] = UniverseStats(universe, universe in self._mapped)
        stats.record(source_ip, time.monotonic(), time.time())

    def record_telegram(self, mapping: ChannelMapping, value: bool | int) -> None:
        self.telegram_count += 1
        self.telegrams.append(
            TelegramLogEntry(time.time(), mapping.group_address, mapping.name, mapping.type, value)
        )
