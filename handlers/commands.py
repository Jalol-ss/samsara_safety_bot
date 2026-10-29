"""
handlers/commands.py
====================
Operator-facing slash commands. All lightweight — nothing destructive
except /unregister_webhook which asks for confirmation.

Commands:
  /ping                     Round-trip test. Confirms Samsara + Telegram.
  /setup_topics             Create one topic per entry in EVENT_TOPIC_NAMES.
  /topics                   List all cached topics + their thread ids.
  /recent [hours] [limit]   Pull recent safety events from Samsara and
                            dispatch any that haven't been posted yet.
                            Default: last 2 hours, up to 20 events.
  /list_webhooks            Show webhooks registered on Samsara.
  /register_webhook         Register PUBLIC_BASE_URL/webhook/samsara
                            with Samsara as a push target.
  /unregister_webhook <id>  Delete a webhook (operator must pass id).
  /chat_id                  Reply with the chat id — useful for finding
                            SAFETY_GROUP_CHAT_ID on first setup.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import (
    EVENT_TOPIC_NAMES,
    PUBLIC_BASE_URL,
    SAFETY_GROUP_CHAT_ID,
    SAMSARA_WEBHOOK_SECRET,
)
from engine.dispatcher import dispatch_event
from services.samsara import SamsaraClient, SamsaraError
from services.state import State
from services.topics import rebuild_all

logger = logging.getLogger(__name__)

router = Router(name="safety_commands")


# ---------- /chat_id ------------------------------------------------
@router.message(Command("chat_id"))
async def on_chat_id(message: Message, samsara: SamsaraClient, app_state: State) -> None:
    await message.reply(
        f"Chat id: <code>{message.chat.id}</code>\n"
        f"Thread id: <code>{message.message_thread_id or '(none)'}</code>"
    )


# ---------- /ping ---------------------------------------------------
@router.message(Command("ping"))
async def on_ping(message: Message, samsara: SamsaraClient, app_state: State) -> None:
    lines = ["🏓 pong"]
    try:
        hooks = await samsara.list_webhooks()
        lines.append(f"• Samsara reachable — {len(hooks)} webhook(s) registered.")
    except SamsaraError as e:
        lines.append(f"• Samsara: ❌ <code>{e}</code>")
    lines.append(f"• Target chat: <code>{SAFETY_GROUP_CHAT_ID or '(unset)'}</code>")
    await message.reply("\n".join(lines))


# ---------- /setup_topics ------------------------------------------
@router.message(Command("setup_topics"))
async def on_setup_topics(message: Message, samsara: SamsaraClient, app_state: State) -> None:
    if not SAFETY_GROUP_CHAT_ID:
        await message.reply("⚠️ SAFETY_GROUP_CHAT_ID is not set.")
        return
    # Dedup display names while preserving insertion order.
    names: list[str] = []
    seen: set[str] = set()
    for v in EVENT_TOPIC_NAMES.values():
        if v not in seen:
            names.append(v); seen.add(v)
    created = await rebuild_all(message.bot, SAFETY_GROUP_CHAT_ID, names, app_state)
    body = "\n".join(f"• {n} → <code>{tid}</code>"
                     for n, tid in created.items()) or "(none)"
    await message.reply(f"✅ Topics ensured ({len(created)}):\n{body}")


# ---------- /topics -------------------------------------------------
@router.message(Command("topics"))
async def on_topics(message: Message, samsara: SamsaraClient, app_state: State) -> None:
    topics = app_state.all_topics()
    if not topics:
        await message.reply("No topics cached yet. Run /setup_topics first.")
        return
    body = "\n".join(f"• {n} → <code>{tid}</code>" for n, tid in topics.items())
    await message.reply(f"Cached topics:\n{body}")


# ---------- /recent -------------------------------------------------
@router.message(Command("recent"))
async def on_recent(message: Message, command: CommandObject,
                    samsara: SamsaraClient, app_state: State) -> None:
    """Pull recent events from Samsara and dispatch new ones."""
    hours = 2
    limit = 20
    if command.args:
        parts = command.args.split()
        try:
            hours = max(1, min(int(parts[0]), 72))
        except (ValueError, IndexError):
            pass
        try:
            limit = max(1, min(int(parts[1]), 100))
        except (ValueError, IndexError):
            pass

    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    waiter = await message.reply(
        f"🔎 Scanning last {hours}h of safety events (cap {limit})…"
    )

    count = 0
    sent  = 0
    try:
        async for row in samsara.iter_safety_events(start_time=start,
                                                    end_time=end,
                                                    page_size=min(limit, 100)):
            count += 1
            if await dispatch_event(message.bot, samsara, app_state, row):
                sent += 1
            if count >= limit:
                break
    except SamsaraError as e:
        await waiter.edit_text(f"❌ Samsara error: <code>{e}</code>")
        return

    await waiter.edit_text(
        f"✅ Scanned <b>{count}</b> event(s), dispatched <b>{sent}</b>.\n"
        f"<i>(Already-seen events are deduped.)</i>"
    )


# ---------- /list_webhooks ------------------------------------------
# ---------- /seed_each --------------------------------------------
# Pull the LATEST event of each type we care about and post one to
# each matching topic. Great for a smoke-test / showing the group what
# each topic will look like without waiting for real incidents.
@router.message(Command("seed_each"))
async def on_seed_each(message: Message, command: CommandObject,
                       samsara: SamsaraClient, app_state: State) -> None:
    days = 7
    if command.args:
        try: days = max(1, min(int(command.args.split()[0]), 30))
        except (ValueError, IndexError): pass

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    waiter = await message.reply(
        f"🌱 Seeding one event per topic from the last {days}d…"
    )

    # Walk recent events most-recent-first, keep first of each topic_name.
    from engine.normalize import normalize_safety_event
    picked: dict[str, dict] = {}  # topic_name -> raw event
    scanned = 0
    try:
        async for row in samsara.iter_safety_events(start_time=start,
                                                    end_time=end,
                                                    page_size=100):
            scanned += 1
            try:
                ev = normalize_safety_event(row)
            except Exception:
                continue
            tn = ev.get("topic_name")
            if tn and tn not in picked:
                picked[tn] = row
            if scanned > 2000:
                break  # safety cap
    except SamsaraError as e:
        await waiter.edit_text(f"❌ Samsara error: <code>{e}</code>")
        return

    if not picked:
        await waiter.edit_text(
            f"ℹ️ No safety events found in the last {days}d. "
            f"Try a wider window: <code>/seed_each 30</code>."
        )
        return

    sent = 0
    for tn, raw in picked.items():
        try:
            if await dispatch_event(message.bot, samsara, app_state, raw,
                                    force=True):
                sent += 1
        except Exception:
            logger.exception("seed_each failed for %r", tn)

    await waiter.edit_text(
        f"✅ Seeded <b>{sent}</b> topic(s) from the last {days}d "
        f"(scanned {scanned} event(s))."
    )


@router.message(Command("list_webhooks"))
async def on_list_webhooks(message: Message, samsara: SamsaraClient, app_state: State) -> None:
    try:
        hooks = await samsara.list_webhooks()
    except SamsaraError as e:
        await message.reply(f"❌ <code>{e}</code>")
        return
    if not hooks:
        await message.reply("No webhooks registered on Samsara yet.")
        return
    body = "\n".join(
        f"• <code>{h.get('id')}</code> — {h.get('name','?')} → {h.get('url','')}"
        for h in hooks
    )
    await message.reply(f"Registered webhooks:\n{body}")


# ---------- /register_webhook ---------------------------------------
@router.message(Command("register_webhook"))
async def on_register_webhook(message: Message, samsara: SamsaraClient, app_state: State) -> None:
    if not PUBLIC_BASE_URL:
        await message.reply("⚠️ PUBLIC_BASE_URL is not set.")
        return
    url = f"{PUBLIC_BASE_URL}/webhook/samsara"
    try:
        created = await samsara.create_webhook(
            name="Safety Bot",
            url=url,
            secret=SAMSARA_WEBHOOK_SECRET or None,
        )
    except SamsaraError as e:
        await message.reply(f"❌ <code>{e}</code>")
        return
    await message.reply(
        f"✅ Webhook created on Samsara:\n"
        f"• id: <code>{created.get('id')}</code>\n"
        f"• url: {url}\n\n"
        f"<b>Next step</b>: in Samsara dashboard create one or more "
        f"<i>Alert Configurations</i> that trigger on the safety events "
        f"you care about (Harsh Brake, Rolling Stop, …) and set this "
        f"webhook as the action. Events start flowing automatically."
    )


# ---------- /unregister_webhook ------------------------------------
@router.message(Command("unregister_webhook"))
async def on_unregister_webhook(message: Message, command: CommandObject,
                                samsara: SamsaraClient, app_state: State) -> None:
    if not command.args:
        await message.reply(
            "Usage: <code>/unregister_webhook &lt;id&gt;</code>\n"
            "Get the id from <code>/list_webhooks</code>."
        )
        return
    wid = command.args.strip()
    try:
        await samsara.delete_webhook(wid)
    except SamsaraError as e:
        await message.reply(f"❌ <code>{e}</code>")
        return
    await message.reply(f"🗑 Deleted webhook <code>{wid}</code>.")
