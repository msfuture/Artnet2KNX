"""Statusseite (aiohttp): HTML-Seite und JSON-API unter /api/status."""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import time
from importlib import resources
from typing import Any

from aiohttp import web

from . import __version__
from .artnet import format_universe
from .config import Config
from .gateway import Gateway
from .status import StatusTracker, format_value

logger = logging.getLogger(__name__)


def knx_status(cfg: Config, xknx: Any | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "connection_type": cfg.knx.connection_type,
        "target": (
            f"{cfg.knx.gateway_ip}:{cfg.knx.gateway_port}"
            if cfg.knx.connection_type.startswith("tunneling")
            else f"{cfg.knx.multicast_group}:{cfg.knx.multicast_port}"
        ),
        "individual_address": cfg.knx.individual_address,
        "rate_limit": cfg.knx.rate_limit,
    }
    if xknx is None:
        info.update(state="DRY_RUN", connected_since=None, queue=0, errors=0)
        return info
    cm = xknx.connection_manager
    info.update(
        state=cm.state.value,
        active_type=cm.connection_type.value,
        connected_since=cm.connected_since.timestamp() if cm.connected_since else None,
        queue=xknx.telegrams.qsize(),
        errors=cm.cemi_count_outgoing_error,
    )
    return info


def build_status(
    cfg: Config,
    gateway: Gateway,
    tracker: StatusTracker,
    xknx: Any | None,
) -> dict[str, Any]:
    now_wall = time.time()
    now_mono = time.monotonic()

    universes = []
    seen = tracker.universes
    for uni in sorted(gateway.universes | set(seen)):
        stats = seen.get(uni)
        universes.append(
            {
                "universe": uni,
                "label": format_universe(uni),
                "mapped": uni in gateway.universes,
                "packets": stats.packets if stats else 0,
                "rate": round(stats.current_rate(now_mono), 1) if stats else 0.0,
                "last_seen_ago": round(now_mono - stats.last_seen, 1) if stats else None,
                "sources": sorted(stats.sources) if stats else [],
            }
        )

    mappings = []
    for out in sorted(gateway.outputs, key=lambda o: (o.mapping.universe, o.mapping.channel)):
        m = out.mapping
        mappings.append(
            {
                "universe": format_universe(m.universe),
                "channel": m.channel,
                "type": m.type.value,
                "group_address": m.group_address,
                "name": m.name,
                "dmx": out.dmx_value,
                "knx": format_value(m.type, out.last_sent),
                "pending": out.pending,
                "sent_count": out.sent_count,
                "last_sent_at": out.last_sent_at,
            }
        )

    telegrams = [
        {
            "time": t.time,
            "group_address": t.group_address,
            "name": t.name,
            "type": t.type.value,
            "value": format_value(t.type, t.value),
        }
        for t in reversed(tracker.telegrams)
    ]

    return {
        "version": __version__,
        "server_time": now_wall,
        "started_at": tracker.started_at,
        "telegram_count": tracker.telegram_count,
        "knx": knx_status(cfg, xknx),
        "artnet": {"bind": f"{cfg.artnet.bind_ip}:{cfg.artnet.port}"},
        "universes": universes,
        "mappings": mappings,
        "telegrams": telegrams,
    }


def _basic_auth_middleware(username: str, password: str):
    expected = f"{username}:{password}".encode()

    @web.middleware
    async def middleware(request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                given = base64.b64decode(header[6:], validate=True)
            except (binascii.Error, ValueError):
                given = b""
            if hmac.compare_digest(given, expected):
                return await handler(request)
        return web.Response(
            status=401,
            text="Anmeldung erforderlich",
            headers={"WWW-Authenticate": 'Basic realm="Artnet2KNX", charset="UTF-8"'},
        )

    return middleware


def create_app(
    cfg: Config,
    gateway: Gateway,
    tracker: StatusTracker,
    xknx: Any | None = None,
) -> web.Application:
    middlewares = []
    if cfg.web.username and cfg.web.password:
        middlewares.append(_basic_auth_middleware(cfg.web.username, cfg.web.password))
    app = web.Application(middlewares=middlewares)
    page = resources.files("artnet2knx").joinpath("static/index.html").read_bytes()

    async def index(_: web.Request) -> web.Response:
        return web.Response(body=page, content_type="text/html", charset="utf-8")

    async def status(_: web.Request) -> web.Response:
        return web.json_response(
            build_status(cfg, gateway, tracker, xknx),
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    return app


async def start_web(app: web.Application, bind_ip: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, bind_ip, port).start()
    logger.info("Statusseite: http://%s:%d/", bind_ip, port)
    return runner
