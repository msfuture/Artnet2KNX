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
from .config import Config, MappingType
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
    indexed = sorted(
        enumerate(gateway.outputs), key=lambda io: (io[1].mapping.universe, io[1].mapping.channel)
    )
    for index, out in indexed:
        m = out.mapping
        mappings.append(
            {
                "id": index,  # Index in gateway.outputs, für /api/send
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
            "source": t.source,
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
        "web": {"allow_send": cfg.web.allow_send},
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


def parse_test_value(mtype: MappingType, value: Any) -> bool | int:
    """Testwert aus JSON prüfen: switch -> true/false, dim -> 0..255."""
    if mtype is MappingType.SWITCH:
        if not isinstance(value, bool):
            raise ValueError("Schalten erwartet true oder false")
        return value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError("Dimmen erwartet eine Zahl 0..255")
    return value


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

    async def send(request: web.Request) -> web.Response:
        if not cfg.web.allow_send:
            return web.json_response({"error": "Senden ist deaktiviert (web.allow_send)"}, status=403)
        # Nur JSON annehmen: fremde Webseiten können ohne CORS-Freigabe keine
        # JSON-Requests schicken, das schützt vor Cross-Site-Requests.
        if request.content_type != "application/json":
            return web.json_response({"error": "Content-Type muss application/json sein"}, status=415)
        try:
            body = await request.json()
            index = body["id"]
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("id muss eine Zahl sein")
            if not 0 <= index < len(gateway.outputs):
                raise ValueError(f"Unbekanntes Mapping {index}")
            output = gateway.outputs[index]
            value = parse_test_value(output.mapping.type, body["value"])
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Ungültige Anfrage: {err}"}, status=400)
        try:
            output.send_test(value)
        except Exception as err:  # noqa: BLE001 - Fehler an die Seite melden statt 500
            logger.exception("Testwert konnte nicht gesendet werden")
            return web.json_response({"error": f"Senden fehlgeschlagen: {err}"}, status=502)
        return web.json_response(
            {
                "group_address": output.mapping.group_address,
                "value": format_value(output.mapping.type, value),
            }
        )

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/send", send)
    return app


async def start_web(app: web.Application, bind_ip: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, bind_ip, port).start()
    logger.info("Statusseite: http://%s:%d/", bind_ip, port)
    return runner
