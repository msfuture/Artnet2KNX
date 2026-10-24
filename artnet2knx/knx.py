"""KNX-Anbindung über xknx."""

from __future__ import annotations

import logging

from xknx import XKNX
from xknx.dpt import DPTArray, DPTBinary
from xknx.io import ConnectionConfig, ConnectionType
from xknx.telegram import GroupAddress, Telegram
from xknx.telegram.apci import GroupValueWrite

from .config import KnxConfig

logger = logging.getLogger(__name__)

_CONNECTION_TYPES = {
    "automatic": ConnectionType.AUTOMATIC,
    "routing": ConnectionType.ROUTING,
    "tunneling": ConnectionType.TUNNELING,
    "tunneling_tcp": ConnectionType.TUNNELING_TCP,
}


def create_xknx(cfg: KnxConfig) -> XKNX:
    connection_config = ConnectionConfig(
        connection_type=_CONNECTION_TYPES[cfg.connection_type],
        individual_address=cfg.individual_address,
        local_ip=cfg.local_ip,
        gateway_ip=cfg.gateway_ip,
        gateway_port=cfg.gateway_port,
        route_back=cfg.route_back,
        multicast_group=cfg.multicast_group,
        multicast_port=cfg.multicast_port,
    )
    return XKNX(
        connection_config=connection_config,
        rate_limit=cfg.rate_limit,
        multicast_group=cfg.multicast_group,
        multicast_port=cfg.multicast_port,
    )


def build_telegram(group_address: str, value: bool | int) -> Telegram:
    """GroupValueWrite bauen: bool -> DPT 1 (1 Bit), int -> DPT 5 (1 Byte)."""
    if isinstance(value, bool):
        payload = DPTBinary(1 if value else 0)
    else:
        payload = DPTArray((value & 0xFF,))
    return Telegram(
        destination_address=GroupAddress(group_address),
        payload=GroupValueWrite(payload),
    )


class KnxSender:
    def __init__(self, xknx: XKNX):
        self._xknx = xknx

    def __call__(self, group_address: str, value: bool | int) -> None:
        self._xknx.telegrams.put_nowait(build_telegram(group_address, value))


class LogSender:
    """Sender für --dry-run: Telegramme nur protokollieren."""

    def __call__(self, group_address: str, value: bool | int) -> None:
        logger.info("[dry-run] %s <- %s", group_address, build_telegram(group_address, value).payload)
