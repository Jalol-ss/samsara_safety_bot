"""
engine/webhook_server.py
========================
Tiny aiohttp web server that receives Samsara webhook POSTs and hands
each payload to the dispatcher.

Routes:
  * POST /webhook/samsara  — Samsara pushes events here.
  * GET  /health           — Railway/UptimeRobot health check.

Signature verification:
  Samsara (when a secret is configured) signs each POST with
  HMAC-SHA256(secret, raw_body) in the `X-Samsara-Signature` header.
  We reject anything that doesn't match. If no secret is set, we skip
  verification (good enough for a staging deploy, risky for prod — set
  SAMSARA_WEBHOOK_SECRET as soon as it's a real production bot).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict

from aiogram import Bot
from aiohttp import web

from config import SAMSARA_WEBHOOK_SECRET
from engine.dispatcher import dispatch_event
from services.samsara import SamsaraClient
from services.state import State

logger = logging.getLogger(__name__)


def _verify_signature(body: bytes, header_value: str, secret: str) -> bool:
    """Compare the X-Samsara-Signature header against HMAC-SHA256 of
    the raw body with the shared secret. Constant-time compare so we
    don't leak timing info."""
    try:
        want = hmac.new(secret.encode("utf-8"), body,
                        hashlib.sha256).hexdigest()
    except Exception:
        return False
    # Header is sometimes "sha256=<hex>" and sometimes just "<hex>".
    got = (header_value or "").strip().removeprefix("sha256=").strip()
    return hmac.compare_digest(got, want)


async def _handle_samsara(request: web.Request) -> web.Response:
    body = await request.read()

    if SAMSARA_WEBHOOK_SECRET:
        sig = request.headers.get("X-Samsara-Signature") \
              or request.headers.get("X-Hub-Signature-256") \
              or ""
        if not _verify_signature(body, sig, SAMSARA_WEBHOOK_SECRET):
            logger.warning("Rejected webhook with bad/missing signature "
                           "(sig=%r)", sig[:40])
            return web.Response(status=401, text="bad signature")

    try:
        payload: Dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        logger.warning("Rejected webhook with bad JSON: %s", e)
        return web.Response(status=400, text="bad json")

    bot: Bot            = request.app["bot"]
    samsara: SamsaraClient = request.app["samsara"]
    state: State        = request.app["state"]

    # Samsara may batch events in a list, or send one at a time.
    events = payload.get("events")
    if isinstance(events, list):
        for ev in events:
            await dispatch_event(bot, samsara, state, ev)
    else:
        await dispatch_event(bot, samsara, state, payload)

    # 200 fast so Samsara doesn't retry on a slow dispatch. The actual
    # work above awaits anyway because retries would duplicate the
    # delivery — Samsara considers any 2xx a successful ack.
    return web.Response(status=200, text="ok")


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_app(bot: Bot, samsara: SamsaraClient, state: State) -> web.Application:
    app = web.Application()
    app["bot"]     = bot
    app["samsara"] = samsara
    app["state"]   = state
    app.router.add_post("/webhook/samsara", _handle_samsara)
    app.router.add_get ("/health",          _health)
    return app
