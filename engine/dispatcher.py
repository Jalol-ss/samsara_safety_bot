"""
engine/dispatcher.py
====================
Takes a normalized event (see engine.normalize), posts it into the
right Telegram topic in the Safety supergroup, and attaches the
dashcam clip when possible.

Pipeline:
  1) De-dup via state.is_seen(event.id) — webhook + poller may both
     deliver the same event.
  2) Resolve/create the topic for the event's label.
  3) Format the body (driver, truck, time, location, speed).
  4) Try to attach a video:
       a. If event.media_urls has one, send that directly.
       b. Else if ATTACH_DASHCAM_VIDEO is on, fire a Samsara media
          retrieval around happened_at and poll until ready.
       c. Else fall back to text only.
  5) Send a text-only message if no video; otherwise send the video
     with the formatted caption.

Everything is wrapped in try/except so a single bad event never kills
the stream — errors just get logged.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    URLInputFile,
)

from config import (
    ATTACH_DASHCAM_VIDEO,
    SAFETY_EVENT_TYPES,
    SAFETY_GROUP_CHAT_ID,
    VIDEO_LOOKAHEAD_SECONDS,
    VIDEO_LOOKBACK_SECONDS,
    VIDEO_POLL_SECONDS,
)
from services.samsara import SamsaraClient, SamsaraError
from services.state import State
from services.topics import ensure_topic

logger = logging.getLogger(__name__)

# Max file size Telegram accepts via sendVideo in a bot message.
# Clips over this get sent as a URL (Telegram pulls it directly) or as
# an "Open video" link if that fails too.
TELEGRAM_VIDEO_LIMIT = 48 * 1024 * 1024


def _should_forward(event_type: str) -> bool:
    """Respect SAFETY_EVENT_TYPES allowlist. Empty list = forward all."""
    if not SAFETY_EVENT_TYPES:
        return True
    return event_type in SAFETY_EVENT_TYPES


def _fmt_header(ev: Dict[str, Any]) -> str:
    """The caption that goes with the video / stand-alone message.
    Plain text — no emojis — in the order:
        TOPIC
        Unit #...
        Driver: ...
        Time: ...
        Location: ...
        Coordinates: lat, lon
        Speed: ... mph (optional)
    """
    unit   = ev.get("unit_number") or ev.get("vehicle_name") or "?"
    driver = ev.get("driver_name") or ""

    ts: datetime = ev["happened_at"]
    when = ts.astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC")

    lines: list[str] = [
        f"<b>{ev['topic_name'].upper()}</b>",
        f"Unit #<b>{unit}</b>",
    ]
    if driver:
        lines.append(f"Driver: <b>{driver}</b>")
    lines.append(f"Time: {when}")
    if ev.get("location"):
        lines.append(f"Location: {ev['location']}")
    if ev.get("lat") is not None and ev.get("lon") is not None:
        lines.append(f"Coordinates: {ev['lat']:.6f}, {ev['lon']:.6f}")
    if ev.get("speed_mph") is not None:
        lines.append(f"Speed: {ev['speed_mph']} mph")
    return "\n".join(lines)


def _build_kb(ev: Dict[str, Any]) -> Optional[InlineKeyboardMarkup]:
    """Inline keyboard shown under the alert. Primary action is the
    Samsara Report link; a secondary 'Open in Maps' row is added when
    we have GPS coordinates. No emojis in button labels."""
    buttons: list[list[InlineKeyboardButton]] = []
    if ev.get("dashboard_url"):
        buttons.append([InlineKeyboardButton(
            text="Samsara Report",
            url=ev["dashboard_url"],
        )])
    if ev.get("lat") is not None and ev.get("lon") is not None:
        maps_url = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        buttons.append([InlineKeyboardButton(
            text="Open in Maps",
            url=maps_url,
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


async def _download(url: str, cap_bytes: int = TELEGRAM_VIDEO_LIMIT
                    ) -> Optional[bytes]:
    """Fetch a signed Samsara URL into memory. Returns None if the
    download fails or the file is larger than `cap_bytes` (we fall
    back to sending the URL in that case)."""
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    logger.warning("Media download %s returned %s",
                                   url[:80], r.status)
                    return None
                # Reject oversize before buffering everything.
                clen = int(r.headers.get("Content-Length") or 0)
                if clen and clen > cap_bytes:
                    logger.info("Media %s too big (%d > %d) — sending URL.",
                                url[:60], clen, cap_bytes)
                    return None
                buf = io.BytesIO()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    buf.write(chunk)
                    if buf.tell() > cap_bytes:
                        logger.info("Media %s exceeded %d while streaming",
                                    url[:60], cap_bytes)
                        return None
                return buf.getvalue()
    except Exception as e:
        logger.warning("Media download failed for %s: %s", url[:80], e)
        return None


async def _resolve_video(ev: Dict[str, Any],
                         samsara: SamsaraClient) -> Optional[str]:
    """Return a video URL for this event or None.

    Priority:
      1. media_urls already on the event payload.
      2. New media retrieval around happened_at (if enabled + we have
         a vehicle_id).
    """
    for u in ev.get("media_urls") or []:
        if isinstance(u, str) and u.startswith("http"):
            return u

    if not ATTACH_DASHCAM_VIDEO:
        return None
    if not ev.get("vehicle_id"):
        return None

    try:
        rid = await samsara.request_media(
            vehicle_id=ev["vehicle_id"],
            happened_at=ev["happened_at"],
            lookback_s=VIDEO_LOOKBACK_SECONDS,
            lookahead_s=VIDEO_LOOKAHEAD_SECONDS,
            media_type="video",
        )
    except SamsaraError as e:
        logger.info("Media retrieval request failed for event %s: %s",
                    ev.get("id"), e)
        return None

    # Poll — Samsara needs a few seconds to pull the clip off the
    # gateway. VIDEO_POLL_SECONDS caps the wait so a stuck/offline
    # camera doesn't block the event forever.
    deadline = asyncio.get_event_loop().time() + VIDEO_POLL_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        try:
            res = await samsara.poll_media(rid)
        except SamsaraError as e:
            logger.info("poll_media %s failed: %s", rid, e)
            return None
        media = (res.get("data") or {}).get("media") or []
        # Prefer road-facing clip, fall back to any available one.
        pick = None
        for m in media:
            if m.get("status") == "available" and m.get("input") == "dashcamRoadFacing":
                pick = m
                break
        if pick is None:
            for m in media:
                if m.get("status") == "available":
                    pick = m
                    break
        if pick:
            url = (pick.get("urlInfo") or {}).get("url")
            if url:
                return url
        # Give up if all slots are terminally bad.
        if media and all(m.get("status") in ("unavailable", "failed")
                         for m in media):
            logger.info("Media retrieval %s terminal: no clip available", rid)
            return None
        await asyncio.sleep(5)

    logger.info("Media retrieval %s timed out after %ds",
                rid, VIDEO_POLL_SECONDS)
    return None


async def dispatch_event(bot: Bot, samsara: SamsaraClient, state: State,
                         raw_event: Dict[str, Any],
                         force: bool = False) -> bool:
    """Normalize + post one event. Returns True if we sent something,
    False if we skipped (dedup, filtered, chat not configured).

    Never raises — logs and swallows so a single bad event can't take
    the webhook server down."""
    try:
        from engine.normalize import normalize_safety_event
        ev = normalize_safety_event(raw_event)
    except Exception as e:
        logger.exception("normalize failed: %s", e)
        return False

    if not SAFETY_GROUP_CHAT_ID:
        logger.warning("SAFETY_GROUP_CHAT_ID is not set — dropping event %s",
                       ev.get("id"))
        return False

    if not ev["id"]:
        # No id = can't dedup, but we still forward (synthesize a
        # content hash so a retry doesn't double-post).
        ev["id"] = f"synth:{ev['type']}:{ev['vehicle_name']}:{ev['happened_at'].isoformat()}"

    if not force and state.is_seen(ev["id"]):
        logger.info("Dedup: event %s already seen", ev["id"])
        return False

    if not _should_forward(ev["type"]):
        logger.debug("Filter skip: event type %r not in allowlist", ev["type"])
        state.mark_seen(ev["id"])
        return False

    # Resolve topic
    try:
        thread_id = await ensure_topic(
            bot, SAFETY_GROUP_CHAT_ID, ev["topic_name"], state,
        )
    except TelegramAPIError as e:
        logger.error("Can't route event %s — topic failure: %s", ev["id"], e)
        return False

    caption = _fmt_header(ev)
    kb      = _build_kb(ev)

    # Try the video.
    video_url: Optional[str] = await _resolve_video(ev, samsara)

    sent = False
    if video_url:
        file_bytes = await _download(video_url)
        try:
            if file_bytes:
                await bot.send_video(
                    chat_id=SAFETY_GROUP_CHAT_ID,
                    message_thread_id=thread_id,
                    video=BufferedInputFile(file_bytes, filename="dashcam.mp4"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                    supports_streaming=True,
                )
            else:
                # Too big / download failed → let Telegram fetch the URL.
                await bot.send_video(
                    chat_id=SAFETY_GROUP_CHAT_ID,
                    message_thread_id=thread_id,
                    video=URLInputFile(video_url, filename="dashcam.mp4"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                    supports_streaming=True,
                )
            sent = True
        except TelegramAPIError as e:
            logger.warning("sendVideo failed for event %s: %s — falling back "
                           "to text with link", ev["id"], e)
            caption += f"\n🎥 <a href=\"{video_url}\">Open dashcam clip</a>"

    if not sent:
        try:
            await bot.send_message(
                chat_id=SAFETY_GROUP_CHAT_ID,
                message_thread_id=thread_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=False,
            )
            sent = True
        except TelegramAPIError as e:
            logger.error("sendMessage failed for event %s: %s", ev["id"], e)

    if sent:
        state.mark_seen(ev["id"])
        logger.info("Dispatched event %s (%s) → topic %r",
                    ev["id"], ev["type"], ev["topic_name"])
    return sent
