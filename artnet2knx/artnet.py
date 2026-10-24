"""Minimaler Art-Net Empfänger (ArtDmx + ArtPoll/ArtPollReply)."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ARTNET_PORT = 6454
ARTNET_HEADER = b"Art-Net\x00"
ARTNET_PROTOCOL_VERSION = 14

OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000

POLL_REPLY_LENGTH = 239


@dataclass(frozen=True)
class DmxPacket:
    """Inhalt eines ArtDmx Pakets."""

    universe: int  # 15 bit Port-Address (Net << 8 | SubNet << 4 | Universe)
    sequence: int
    data: bytes


def parse_universe(value: int | str) -> int:
    """Universum als Port-Address (int) oder als "Net:SubNet:Universe" lesen."""
    if isinstance(value, bool):
        raise ValueError(f"Ungültiges Universum: {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 0x7FFF:
            raise ValueError(f"Universum {value} außerhalb 0..32767")
        return value
    parts = str(value).strip().split(":")
    if len(parts) == 1:
        return parse_universe(int(parts[0]))
    if len(parts) != 3:
        raise ValueError(f"Ungültiges Universum: {value!r} (erwartet Zahl oder Net:SubNet:Universe)")
    net, sub, uni = (int(p) for p in parts)
    if not (0 <= net <= 127 and 0 <= sub <= 15 and 0 <= uni <= 15):
        raise ValueError(f"Ungültiges Universum: {value!r} (Net 0-127, SubNet 0-15, Universe 0-15)")
    return (net << 8) | (sub << 4) | uni


def format_universe(port_address: int) -> str:
    return f"{port_address} ({port_address >> 8}:{(port_address >> 4) & 0xF}:{port_address & 0xF})"


def parse_packet(data: bytes) -> tuple[int, DmxPacket | None]:
    """Art-Net Paket parsen. Liefert (OpCode, DmxPacket|None); OpCode 0 = kein Art-Net."""
    if len(data) < 10 or not data.startswith(ARTNET_HEADER):
        return 0, None
    opcode = struct.unpack_from("<H", data, 8)[0]
    if opcode != OP_DMX:
        return opcode, None
    if len(data) < 18:
        return opcode, None
    sequence = data[12]
    sub_uni = data[14]
    net = data[15] & 0x7F
    length = struct.unpack_from(">H", data, 16)[0]
    payload = data[18 : 18 + min(length, 512)]
    return opcode, DmxPacket(universe=(net << 8) | sub_uni, sequence=sequence, data=payload)


def build_poll_replies(
    ip: str,
    universes: Iterable[int],
    short_name: str,
    long_name: str,
) -> list[bytes]:
    """ArtPollReply Pakete bauen. Pro Paket max. 4 Ports mit gleichem Net/SubNet."""
    groups: dict[tuple[int, int], list[int]] = {}
    for uni in sorted(set(universes)):
        groups.setdefault((uni >> 8, (uni >> 4) & 0xF), []).append(uni & 0xF)

    chunks: list[tuple[int, int, list[int]]] = []
    for (net, sub), unis in groups.items():
        for i in range(0, len(unis), 4):
            chunks.append((net, sub, unis[i : i + 4]))
    if not chunks:
        chunks.append((0, 0, []))

    ip_bytes = socket.inet_aton(ip)
    replies = []
    for bind_index, (net, sub, unis) in enumerate(chunks, start=1):
        pkt = bytearray(POLL_REPLY_LENGTH)
        pkt[0:8] = ARTNET_HEADER
        struct.pack_into("<H", pkt, 8, OP_POLL_REPLY)
        pkt[10:14] = ip_bytes
        struct.pack_into("<H", pkt, 14, ARTNET_PORT)
        struct.pack_into(">H", pkt, 16, 1)  # Firmware-Version
        pkt[18] = net
        pkt[19] = sub
        pkt[23] = 0xD0  # Status1: Indikatoren normal, Port-Address per Netzwerk/Config
        pkt[26:44] = short_name.encode("ascii", "replace")[:17].ljust(18, b"\x00")
        pkt[44:108] = long_name.encode("ascii", "replace")[:63].ljust(64, b"\x00")
        report = f"#0001 [{bind_index:04d}] OK".encode("ascii")
        pkt[108:172] = report.ljust(64, b"\x00")
        struct.pack_into(">H", pkt, 172, len(unis))
        for port, uni in enumerate(unis):
            pkt[174 + port] = 0x80  # Port kann Art-Net -> DMX ausgeben
            pkt[182 + port] = 0x80  # GoodOutputA: Daten werden ausgegeben
            pkt[190 + port] = uni  # SwOut
        pkt[200] = 0x00  # StNode
        pkt[207:211] = ip_bytes  # BindIp
        pkt[211] = bind_index
        pkt[212] = 0x08  # Status2: Art-Net 3/4 (15 bit Port-Address)
        replies.append(bytes(pkt))
    return replies


def local_ip_for(remote_ip: str) -> str:
    """Lokale IP ermitteln, über die `remote_ip` erreicht wird."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((remote_ip, ARTNET_PORT))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


class ArtNetProtocol(asyncio.DatagramProtocol):
    """asyncio Protokoll, das ArtDmx an einen Callback weiterreicht."""

    def __init__(
        self,
        on_dmx: Callable[[DmxPacket], None],
        universes: Iterable[int],
        poll_reply: bool = True,
        announce_ip: str | None = None,
        short_name: str = "Artnet2KNX",
        long_name: str = "Art-Net to KNX Gateway",
    ) -> None:
        self._on_dmx = on_dmx
        self._universes = set(universes)
        self._poll_reply = poll_reply
        self._announce_ip = announce_ip
        self._short_name = short_name
        self._long_name = long_name
        self._transport: asyncio.DatagramTransport | None = None
        self._unknown_universes: set[int] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        opcode, dmx = parse_packet(data)
        if dmx is not None:
            if dmx.universe in self._universes:
                self._on_dmx(dmx)
            elif dmx.universe not in self._unknown_universes:
                self._unknown_universes.add(dmx.universe)
                logger.debug(
                    "Ignoriere Universum %s von %s (kein Mapping)",
                    format_universe(dmx.universe),
                    addr[0],
                )
        elif opcode == OP_POLL and self._poll_reply:
            self._send_poll_reply(addr)

    def _send_poll_reply(self, addr: tuple[str, int]) -> None:
        if self._transport is None:
            return
        ip = self._announce_ip or local_ip_for(addr[0])
        for reply in build_poll_replies(ip, self._universes, self._short_name, self._long_name):
            self._transport.sendto(reply, (addr[0], ARTNET_PORT))
        logger.debug("ArtPollReply an %s gesendet", addr[0])

    def error_received(self, exc: Exception) -> None:
        logger.warning("Art-Net Socket-Fehler: %s", exc)


async def start_artnet_listener(
    protocol: ArtNetProtocol, bind_ip: str = "0.0.0.0", port: int = ARTNET_PORT
) -> asyncio.DatagramTransport:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((bind_ip, port))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: protocol, sock=sock)
    logger.info("Art-Net Empfang auf %s:%d", bind_ip, port)
    return transport
