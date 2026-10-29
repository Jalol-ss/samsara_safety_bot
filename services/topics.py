"""
services/topics.py
==================
Auto-manages Telegram "forum" topics inside the Safety supergroup.

Telegram supergroups with Topics enabled let a bot route messages into
sub-threads by passing `message_thread_id`. The group chat you saw in
the screenshot is exactly this: one supergroup, one topic per event
type.

What this module does:
  * `ensure_topic(bot, chat_id, name)` — return the thread_id for
    `name`, creating the topic on Telegram the first time we see it
    and caching the id in state.json so we don't spam createForumTopic
    on every event.
  * `rebuild_all(bot, chat_id, names)` — one-shot bootstrap when the
    operator wants to pre-create every event-type topic.

Why not createForumTopic every time? Telegram rate-limits forum
operations hard, and duplicate topics make the group messy. Caching
by display name keeps it idempotent.
"""
from __future__ import annotations

import logging
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from services.state import State

logger = logging.getLogger(__name__)


# A small palette of colored icons so each topic gets a distinct look
# without us having to hand-pick one per event. Telegram allows ints
# from a fixed set — these are the official allowed values.
_TOPIC_ICON_COLORS = [
    0x6FB9F0,  # light blue
    0xFFD67E,  # amber
    0xCB86DB,  # purple
    0x8EEE98,  # green
    0xFF93B2,  # pink
    0xFB6F5F,  # red
]


def _color_for(name: str) -> int:
    """Deterministic color pick from the allowed set — the same event
    type always gets the same icon color across restarts."""
    return _TOPIC_ICON_COLORS[sum(ord(c) for c in name) % len(_TOPIC_ICON_COLORS)]


async def ensure_topic(bot: Bot, chat_id: int, name: str,
                       state: State) -> int:
    """Return the message_thread_id for the topic displayed as `name`.

    Looks it up in state first; if missing, calls createForumTopic on
    the chat and persists the new thread id. Raises TelegramAPIError
    if the chat doesn't have Topics enabled or the bot lacks
    `can_manage_topics` permission.
    """
    cached = state.get_topic_id(name)
    if cached is not None:
        return cached

    try:
        res = await bot.create_forum_topic(
            chat_id=chat_id,
            name=name,
            icon_color=_color_for(name),
        )
    except TelegramAPIError as e:
        logger.error(
            "createForumTopic failed for %r in chat %s: %s. "
            "Check that the group has Topics enabled and that the bot "
            "has 'Manage topics' permission.",
            name, chat_id, e,
        )
        raise

    thread_id = int(res.message_thread_id)
    state.set_topic_id(name, thread_id)
    logger.info("Created forum topic %r (thread_id=%s) in chat %s",
                name, thread_id, chat_id)
    return thread_id


async def rebuild_all(bot: Bot, chat_id: int, names: Iterable[str],
                      state: State) -> dict[str, int]:
    """Idempotently ensure every topic in `names` exists. Returns the
    final name→thread_id map including anything that was already
    cached. Safe to call on every boot."""
    out: dict[str, int] = {}
    for n in names:
        try:
            out[n] = await ensure_topic(bot, chat_id, n, state)
        except TelegramAPIError as e:
            logger.warning("Skipping topic %r: %s", n, e)
    return out
