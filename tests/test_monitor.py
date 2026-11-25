from datetime import datetime

from artnet2knx.monitor import (
    Style,
    format_group,
    format_physical,
    handle_packet,
    parse_cemi,
)

NOW = datetime(2026, 1, 2, 12, 34, 56, 789000)
PLAIN = Style(False)


def knxip(service: int, body: bytes) -> bytes:
    return bytes([0x06, 0x10]) + service.to_bytes(2, "big") + (6 + len(body)).to_bytes(2, "big") + body


def cemi(src: int, dst: int, tpdu: bytes, group: bool = True, hops: int = 6, ctrl1: int = 0xBC) -> bytes:
    ctrl2 = (0x80 if group else 0) | (hops << 4)
    return (
        bytes([0x29, 0x00, ctrl1, ctrl2])
        + src.to_bytes(2, "big")
        + dst.to_bytes(2, "big")
        + bytes([len(tpdu) - 1])
        + tpdu
    )


GA_1_0_1 = (1 << 11) | (0 << 8) | 1
GA_1_1_2 = (1 << 11) | (1 << 8) | 2
PA_1_1_5 = (1 << 12) | (1 << 8) | 5


def test_addresses():
    assert format_group(GA_1_1_2) == "1/1/2"
    assert format_physical(PA_1_1_5) == "1.1.5"


def test_group_write_1bit():
    tel = parse_cemi(cemi(PA_1_1_5, GA_1_0_1, bytes([0x00, 0x81])))
    assert tel.group and tel.service == "GroupValueWrite"
    assert tel.small_value == 1 and tel.length_str == "1 Bit"
    assert tel.hops == 6 and not tel.repeated
    assert tel.source_str == "1.1.5" and tel.destination_str == "1/0/1"


def test_group_read_and_response_bytes():
    read = parse_cemi(cemi(PA_1_1_5, GA_1_1_2, bytes([0x00, 0x00])))
    assert read.service == "GroupValueRead" and read.length_str == ""
    resp = parse_cemi(cemi(PA_1_1_5, GA_1_1_2, bytes([0x00, 0x40, 0x0C, 0x1A])))
    assert resp.service == "GroupValueResponse"
    assert resp.data == b"\x0c\x1a" and resp.length_str == "2 Byte"


def test_repeated_flag_and_hops():
    tel = parse_cemi(cemi(PA_1_1_5, GA_1_0_1, bytes([0x00, 0x80]), hops=4, ctrl1=0x9C))
    assert tel.repeated and tel.hops == 4


def test_point_to_point_services():
    conn = parse_cemi(cemi(PA_1_1_5, 0x1101, bytes([0x80]), group=False))
    assert conn.service == "T_Connect" and conn.destination_str == "1.1.1"
    ack = parse_cemi(cemi(PA_1_1_5, 0x1101, bytes([0xC2 | (3 << 2)]), group=False))
    assert ack.service == "T_ACK #3"
    prop = parse_cemi(cemi(PA_1_1_5, 0x1101, bytes([0x43, 0xD5, 0, 1, 0x10, 0]), group=False))
    assert prop.service == "PropertyValueRead #0"


def test_line_with_mapping_names():
    names = {GA_1_1_2: ("5.001", "Küche"), GA_1_0_1: ("1.001", "Decke")}
    line = handle_packet(
        knxip(0x0530, cemi(PA_1_1_5, GA_1_1_2, bytes([0x00, 0x80, 0xFF]))), PLAIN, names, lambda: NOW
    )
    assert line is not None
    cells = [c.strip() for c in line.split("│")]
    assert cells[:6] == ["12:34:56.789", "1/1/2", "1.1.5", "DPT 5.001", "Write", "100 % (255)"]
    assert cells[6].startswith("6") and "Küche" in cells[6]

    line = handle_packet(
        knxip(0x0530, cemi(PA_1_1_5, GA_1_0_1, bytes([0x00, 0x80]))), PLAIN, names, lambda: NOW
    )
    assert "AUS" in line


def test_line_without_mapping_shows_length_and_raw():
    line = handle_packet(
        knxip(0x0530, cemi(PA_1_1_5, GA_1_1_2, bytes([0x00, 0x80, 0x0C, 0x1A]))), PLAIN, None, lambda: NOW
    )
    cells = [c.strip() for c in line.split("│")]
    assert cells[3] == "2 Byte" and cells[5] == "0C 1A"


def test_color_and_other_services():
    line = handle_packet(
        knxip(0x0530, cemi(PA_1_1_5, GA_1_0_1, bytes([0x00, 0x81]))), Style(True), None, lambda: NOW
    )
    assert "\033[32m" in line  # Write grün
    assert "BUSY" in handle_packet(knxip(0x0532, b"\x06\x00\x00\x64\x00\x00"), PLAIN, now=lambda: NOW)
    assert handle_packet(b"garbage", PLAIN) is None
    assert handle_packet(knxip(0x0201, b""), PLAIN) is None  # Search Request ignorieren
    assert "Fehler" in handle_packet(knxip(0x0530, b"\x29\x00\xbc"), PLAIN, now=lambda: NOW)
