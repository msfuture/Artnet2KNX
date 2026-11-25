"""Busmonitor: KNX-Telegramme per IP Routing (Multicast) mitlesen und anzeigen.

Aufruf: ``artnet2knx -c config/config.yaml --monitor``

Liest KNXnet/IP Routing Indications (cEMI L_Data) von der Multicast-Gruppe aus
der Konfiguration und gibt pro Telegramm eine Zeile aus:

    Zeit | Gruppenadresse | Absender | DPT/Länge | Typ | Wert | Hops
"""

from __future__ import annotations

import os
import socket
import struct
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, TextIO

# KNXnet/IP
_HEADER_LEN = 0x06
_PROTOCOL_VERSION = 0x10
ROUTING_INDICATION = 0x0530
ROUTING_LOST_MESSAGE = 0x0531
ROUTING_BUSY = 0x0532
SECURE_WRAPPER = 0x0950

# cEMI Message Codes
_CEMI_CODES = {0x11: "L_Data.req", 0x29: "L_Data.ind", 0x2E: "L_Data.con"}

# Anwendungsdienste (APCI, 10 Bit). Bei den 4-Bit-Diensten ist nur das obere
# Nibble relevant, die unteren 6 Bit enthalten Daten.
_APCI_4BIT = {
    0x000: "GroupValueRead",
    0x040: "GroupValueResponse",
    0x080: "GroupValueWrite",
    0x0C0: "IndividualAddressWrite",
    0x180: "ADCRead",
    0x1C0: "ADCResponse",
    0x200: "MemoryRead",
    0x240: "MemoryResponse",
    0x280: "MemoryWrite",
    0x340: "DeviceDescriptorRead",
    0x380: "DeviceDescriptorResponse",
    0x3C0: "Restart",
}
_APCI_10BIT = {
    0x100: "IndividualAddressRead",
    0x140: "IndividualAddressResponse",
    0x1C8: "SystemNetworkParameterRead",
    0x1C9: "SystemNetworkParameterResponse",
    0x1CA: "SystemNetworkParameterWrite",
    0x1D1: "PropertyExtValueRead",
    0x1D2: "PropertyExtValueResponse",
    0x1D5: "FunctionPropertyExtCommand",
    0x2C9: "UserMemoryRead",
    0x2CA: "UserMemoryResponse",
    0x2CB: "UserMemoryWrite",
    0x2C5: "UserManufacturerInfoRead",
    0x2C6: "UserManufacturerInfoResponse",
    0x2C7: "FunctionPropertyCommand",
    0x2C8: "FunctionPropertyStateRead",
    0x3D1: "AuthorizeRequest",
    0x3D2: "AuthorizeResponse",
    0x3D3: "KeyWrite",
    0x3D4: "KeyResponse",
    0x3D5: "PropertyValueRead",
    0x3D6: "PropertyValueResponse",
    0x3D7: "PropertyValueWrite",
    0x3D8: "PropertyDescriptionRead",
    0x3D9: "PropertyDescriptionResponse",
    0x3DA: "NetworkParameterRead",
    0x3DB: "NetworkParameterResponse",
    0x3DC: "IndividualAddressSerialNumberRead",
    0x3DD: "IndividualAddressSerialNumberResponse",
    0x3DE: "IndividualAddressSerialNumberWrite",
    0x3E0: "DomainAddressWrite",
    0x3E1: "DomainAddressRead",
    0x3E2: "DomainAddressResponse",
    0x3E3: "DomainAddressSelectiveRead",
    0x3E4: "NetworkParameterWrite",
    0x3F1: "SecureService",
}

_PRIORITIES = ("System", "Normal", "Dringend", "Niedrig")


def format_physical(raw: int) -> str:
    return f"{raw >> 12}.{(raw >> 8) & 0x0F}.{raw & 0xFF}"


def format_group(raw: int) -> str:
    return f"{raw >> 11}/{(raw >> 8) & 0x07}/{raw & 0xFF}"


@dataclass
class BusTelegram:
    message_code: str
    source: int
    destination: int
    group: bool  # Ziel ist Gruppenadresse
    hops: int
    priority: str
    repeated: bool
    service: str  # z. B. GroupValueWrite, T_Connect
    small_value: int | None = None  # 6-Bit-Wert im APCI-Byte (DPT 1/2/3)
    data: bytes = b""  # Nutzdaten nach dem APCI

    @property
    def source_str(self) -> str:
        return format_physical(self.source)

    @property
    def destination_str(self) -> str:
        return format_group(self.destination) if self.group else format_physical(self.destination)

    @property
    def has_value(self) -> bool:
        return self.service in ("GroupValueWrite", "GroupValueResponse")

    @property
    def length_str(self) -> str:
        """Nutzdatenlänge, z. B. '1 Bit', '6 Bit', '2 Byte'."""
        if self.small_value is not None:
            return "6 Bit" if self.small_value > 1 else "1 Bit"
        if self.data:
            return f"{len(self.data)} Byte"
        return ""


class ParseError(ValueError):
    pass


def parse_cemi(cemi: bytes) -> BusTelegram:
    """cEMI L_Data-Frame (Standard- oder Extended-Frame) zerlegen."""
    if len(cemi) < 2:
        raise ParseError("cEMI zu kurz")
    code = cemi[0]
    if code not in _CEMI_CODES:
        raise ParseError(f"cEMI Message Code 0x{code:02X} nicht unterstützt")
    pos = 2 + cemi[1]  # Zusatzinformationen überspringen
    if len(cemi) < pos + 7:
        raise ParseError("cEMI zu kurz")
    ctrl1, ctrl2, src, dst, npdu_len = struct.unpack_from(">BBHHB", cemi, pos)
    tpdu = cemi[pos + 7 :]
    if len(tpdu) < npdu_len + 1:
        raise ParseError("cEMI Nutzdaten unvollständig")
    tpdu = tpdu[: npdu_len + 1]

    tel = BusTelegram(
        message_code=_CEMI_CODES[code],
        source=src,
        destination=dst,
        group=bool(ctrl2 & 0x80),
        hops=(ctrl2 >> 4) & 0x07,
        priority=_PRIORITIES[(ctrl1 >> 2) & 0x03],
        # Bit 5: 1 = nicht wiederholt (cEMI), 0 = Wiederholung
        repeated=not ctrl1 & 0x20,
        service="",
    )

    tpci = tpdu[0]
    if tpci & 0x80:  # Steuer-Telegramme (Transportschicht)
        if tpci & 0x40:
            tel.service = "T_NAK" if tpci & 0x01 else "T_ACK"
            tel.service += f" #{(tpci >> 2) & 0x0F}"
        else:
            tel.service = "T_Disconnect" if tpci & 0x01 else "T_Connect"
        return tel

    if npdu_len == 0:
        tel.service = "Leer"
        return tel
    apci = ((tpci & 0x03) << 8) | tpdu[1]
    four = apci & 0x3C0
    if apci not in _APCI_10BIT and four in _APCI_4BIT:
        tel.service = _APCI_4BIT[four]
        if npdu_len == 1:
            if four in (0x040, 0x080):  # Write/Response mit 6-Bit-Wert
                tel.small_value = apci & 0x3F
        else:
            tel.data = bytes(tpdu[2:])
    else:
        tel.service = _APCI_10BIT.get(apci, f"APCI 0x{apci:03X}")
        tel.data = bytes(tpdu[2:])
    if tpci & 0x40:  # nummerierte Verbindung
        tel.service += f" #{(tpci >> 2) & 0x0F}"
    return tel


@dataclass
class KnxIpFrame:
    service: int
    body: bytes


def parse_knxip(packet: bytes) -> KnxIpFrame:
    if len(packet) < _HEADER_LEN:
        raise ParseError("KNXnet/IP Paket zu kurz")
    header_len, version, service, total = struct.unpack_from(">BBHH", packet)
    if header_len != _HEADER_LEN or version != _PROTOCOL_VERSION:
        raise ParseError("kein KNXnet/IP Paket")
    if total > len(packet):
        raise ParseError("KNXnet/IP Paket unvollständig")
    return KnxIpFrame(service, packet[header_len:total])


# --- Werte -----------------------------------------------------------------


def decode_value(tel: BusTelegram, dpt: str | None) -> str:
    """Wert als Text. Mit bekanntem DPT dekodiert, sonst Rohwert."""
    if not tel.has_value:
        return ""
    if tel.small_value is not None:
        if dpt == "1.001":
            return "EIN" if tel.small_value & 0x01 else "AUS"
        return str(tel.small_value)
    data = tel.data
    if dpt == "5.001" and len(data) == 1:
        return f"{round(data[0] * 100 / 255)} % ({data[0]})"
    hexstr = " ".join(f"{b:02X}" for b in data)
    if len(data) == 1:
        return f"{data[0]} (0x{hexstr})"
    return hexstr


# --- Ausgabe ---------------------------------------------------------------


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"


DIM, BOLD = "2", "1"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "31", "32", "33", "34", "35", "36"

_SERVICE_COLORS = {
    "GroupValueWrite": (GREEN,),
    "GroupValueRead": (YELLOW,),
    "GroupValueResponse": (CYAN,),
}

SEP = " │ "
_WIDTHS = (12, 10, 10, 9, 18, 16, 4)  # Zeit, GA, Absender, DPT, Typ, Wert, Hops


def header_line(style: Style) -> str:
    titles = ("Zeit", "Ziel", "Absender", "DPT/Länge", "Typ", "Wert", "Hops")
    cells = [t.ljust(w) for t, w in zip(titles, _WIDTHS)]
    return style(SEP.join(cells).rstrip(), BOLD)


def format_line(
    tel: BusTelegram,
    style: Style,
    timestamp: datetime,
    names: dict[int, tuple[str, str]] | None = None,
) -> str:
    """Eine Ausgabezeile. ``names``: Gruppenadresse (raw) -> (DPT, Name)."""
    dpt, name = (None, "")
    if tel.group and names and tel.destination in names:
        dpt, name = names[tel.destination]

    w = _WIDTHS
    ts = timestamp.strftime("%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}"
    dest = tel.destination_str.ljust(w[1])
    dest = style(dest, BOLD) if tel.group else style(dest, MAGENTA)
    size = (f"DPT {dpt}" if dpt else tel.length_str).ljust(w[3])
    service = tel.service.replace("GroupValue", "") if tel.group else tel.service
    if tel.repeated:
        service += " (Wdh.)"
    service_color = _SERVICE_COLORS.get(tel.service, (MAGENTA,) if not tel.group else ())
    value = decode_value(tel, dpt).ljust(w[5])
    hops = str(tel.hops).rjust(w[6] - 2).ljust(w[6])

    cells = [
        style(ts.ljust(w[0]), DIM),
        dest,
        tel.source_str.ljust(w[2]),
        style(size, DIM),
        style(service.ljust(w[4]), *service_color),
        style(value, BOLD) if value.strip() else value,
        style(hops, RED) if tel.hops == 0 else hops,
    ]
    line = SEP.join(cells)
    if name:
        line += "  " + style(name, DIM)
    return line


def handle_packet(
    packet: bytes,
    style: Style,
    names: dict[int, tuple[str, str]] | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> str | None:
    """Ein empfangenes UDP-Paket in eine Ausgabezeile umwandeln (None = ignorieren)."""
    timestamp = now()
    ts = timestamp.strftime("%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}"
    try:
        frame = parse_knxip(packet)
    except ParseError:
        return None
    if frame.service == ROUTING_INDICATION:
        try:
            tel = parse_cemi(frame.body)
        except ParseError as err:
            return style(f"{ts.ljust(_WIDTHS[0])}{SEP}Fehler: {err}", RED)
        return format_line(tel, style, timestamp, names)
    if frame.service == ROUTING_BUSY:
        return style(f"{ts.ljust(_WIDTHS[0])}{SEP}ROUTING_BUSY (Router überlastet)", YELLOW)
    if frame.service == ROUTING_LOST_MESSAGE:
        lost = struct.unpack_from(">H", frame.body, 2)[0] if len(frame.body) >= 4 else "?"
        return style(f"{ts.ljust(_WIDTHS[0])}{SEP}ROUTING_LOST_MESSAGE ({lost} verloren)", RED)
    if frame.service == SECURE_WRAPPER:
        return style(f"{ts.ljust(_WIDTHS[0])}{SEP}KNX IP Secure (verschlüsselt)", DIM)
    return None


def names_from_mappings(mappings: Iterable) -> dict[int, tuple[str, str]]:
    """Gruppenadressen aus dem Mapping: raw -> (DPT, Name)."""
    from xknx.telegram import GroupAddress

    result: dict[int, tuple[str, str]] = {}
    for m in mappings:
        raw = GroupAddress(m.group_address).raw
        if raw in result:
            continue
        dpt = "1.001" if m.type.value == "switch" else "5.001"
        result[raw] = (dpt, m.name)
    return result


def use_color(stream: TextIO, force: bool | None) -> bool:
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def open_socket(group: str, port: int, local_ip: str | None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", port))
    iface = socket.inet_aton(local_ip) if local_ip else struct.pack("=I", socket.INADDR_ANY)
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + iface
    )
    return sock


def run_monitor(
    group: str,
    port: int,
    local_ip: str | None = None,
    names: dict[int, tuple[str, str]] | None = None,
    color: bool | None = None,
    out: TextIO = sys.stdout,
) -> int:
    style = Style(use_color(out, color))
    try:
        sock = open_socket(group, port, local_ip)
    except OSError as err:
        print(f"Multicast {group}:{port} kann nicht geöffnet werden: {err}", file=sys.stderr)
        return 1
    iface = local_ip or "alle Schnittstellen"
    print(style(f"Busmonitor auf {group}:{port} ({iface}), Beenden mit Strg+C", DIM), file=out)
    print(header_line(style), file=out, flush=True)
    try:
        while True:
            packet, _addr = sock.recvfrom(1024)
            line = handle_packet(packet, style, names)
            if line is not None:
                print(line, file=out, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0
