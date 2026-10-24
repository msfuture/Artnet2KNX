"""Einstiegspunkt: python -m artnet2knx -c config/config.yaml"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .artnet import ArtNetProtocol, format_universe, start_artnet_listener
from .config import Config, ConfigError, load_config
from .gateway import Gateway
from .knx import KnxSender, LogSender, create_xknx

logger = logging.getLogger("artnet2knx")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="artnet2knx", description="Art-Net zu KNX Gateway")
    parser.add_argument(
        "-c", "--config", default="config/config.yaml", help="Pfad zur config.yaml"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Keine KNX-Verbindung aufbauen, Telegramme nur loggen",
    )
    parser.add_argument(
        "--check", action="store_true", help="Konfiguration prüfen, Mapping ausgeben und beenden"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


async def run(cfg: Config, dry_run: bool) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    xknx = None
    if dry_run:
        sender = LogSender()
    else:
        xknx = create_xknx(cfg.knx)
        await xknx.start()
        sender = KnxSender(xknx)

    gateway = Gateway(cfg.mappings, sender, loop)
    protocol = ArtNetProtocol(
        gateway.handle_dmx,
        gateway.universes,
        poll_reply=cfg.artnet.poll_reply,
        announce_ip=cfg.artnet.announce_ip,
        short_name=cfg.artnet.short_name,
        long_name=cfg.artnet.long_name,
    )
    transport = await start_artnet_listener(protocol, cfg.artnet.bind_ip, cfg.artnet.port)
    logger.info(
        "Gateway läuft: %d Mappings in %d Universen",
        len(cfg.mappings),
        len(gateway.universes),
    )
    try:
        await stop.wait()
    finally:
        logger.info("Beende Gateway")
        transport.close()
        gateway.close()
        if xknx is not None:
            await xknx.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        print(f"Konfigurationsfehler: {err}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.check:
        print(f"Konfiguration OK, {len(cfg.mappings)} Mappings aus {cfg.mapping_file}:")
        for m in sorted(cfg.mappings, key=lambda m: (m.universe, m.channel)):
            print(f"  Universum {format_universe(m.universe)}: {m.label}")
        return 0

    try:
        asyncio.run(run(cfg, args.dry_run))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
