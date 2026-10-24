"""Umsetzung von DMX-Werten auf KNX-Telegramme (inkl. Drosselung)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from .artnet import DmxPacket
from .config import ChannelMapping, MappingType

logger = logging.getLogger(__name__)

# Sender bekommt Gruppenadresse und Wert (bool für switch, int 0-255 für dim)
Sender = Callable[[str, "bool | int"], None]


def dmx_to_knx(mapping: ChannelMapping, dmx_value: int) -> bool | int:
    """DMX-Wert (0-255) in den KNX-Wert des Mappings umrechnen."""
    if mapping.invert:
        dmx_value = 255 - dmx_value
    if mapping.type is MappingType.SWITCH:
        return dmx_value >= mapping.threshold
    return dmx_value


class ChannelOutput:
    """Zustand eines Mappings: sendet nur Änderungen, höchstens alle `min_interval` s.

    Die erste Änderung nach einer Pause geht sofort raus, weitere Änderungen
    innerhalb des Intervalls werden zusammengefasst und der letzte Wert am Ende
    des Intervalls gesendet. So bleibt der Endwert eines Fades immer korrekt.
    """

    def __init__(self, mapping: ChannelMapping, sender: Sender, loop: asyncio.AbstractEventLoop):
        self.mapping = mapping
        self._sender = sender
        self._loop = loop
        self._last_sent: bool | int | None = None
        self._last_sent_time = float("-inf")
        self._pending: bool | int | None = None
        self._timer: asyncio.TimerHandle | None = None

    def update(self, dmx_value: int) -> None:
        value = dmx_to_knx(self.mapping, dmx_value)
        if self._timer is not None:
            self._pending = value
            return
        if value == self._last_sent:
            return
        wait = self._last_sent_time + self.mapping.min_interval - self._loop.time()
        if wait <= 0:
            self._send(value)
        else:
            self._pending = value
            self._timer = self._loop.call_later(wait, self._flush)

    def _flush(self) -> None:
        self._timer = None
        value, self._pending = self._pending, None
        if value is not None and value != self._last_sent:
            self._send(value)

    def _send(self, value: bool | int) -> None:
        self._last_sent = value
        self._last_sent_time = self._loop.time()
        logger.debug("%s = %s", self.mapping.label, value)
        try:
            self._sender(self.mapping.group_address, value)
        except Exception:  # noqa: BLE001 - ein fehlerhaftes Telegramm darf den Gateway nicht stoppen
            logger.exception("Senden fehlgeschlagen: %s", self.mapping.label)

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


class Gateway:
    """Verteilt eingehende DMX-Pakete auf die konfigurierten Mappings."""

    def __init__(
        self,
        mappings: Iterable[ChannelMapping],
        sender: Sender,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        loop = loop or asyncio.get_running_loop()
        self._by_universe: dict[int, list[ChannelOutput]] = {}
        for mapping in mappings:
            self._by_universe.setdefault(mapping.universe, []).append(
                ChannelOutput(mapping, sender, loop)
            )

    @property
    def universes(self) -> set[int]:
        return set(self._by_universe)

    def handle_dmx(self, packet: DmxPacket) -> None:
        data = packet.data
        for output in self._by_universe.get(packet.universe, ()):
            index = output.mapping.channel - 1
            if index < len(data):
                output.update(data[index])

    def close(self) -> None:
        for outputs in self._by_universe.values():
            for output in outputs:
                output.cancel()
