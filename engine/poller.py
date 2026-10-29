"""
engine/poller.py
================
Fallback mode. If ENABLE_SAFETY_POLLER=1, this coroutine runs
forever, pulling `/fleet/safety-events` every POLL_INTERVAL_SECONDS
and feeding new events into the dispatcher.

Use the poller when:
  * Your Samsara plan doesn't include outgoing webhooks (rare).
  * You want a backstop in case webhooks miss events (we dedup by
    event id so running both at once is safe).

State:
  * `state.last_poll_time` — the end of the window we last processed.
    On startup we go back POLL_INTERVAL_SECONDS from "now" so we don't
    miss events that landed while the bot was redeploying.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from config import POLL_INTERVAL_SECONDS
from engine.dispatcher import dispatch_event
from services.samsara import SamsaraClient, SamsaraError
from services.state import State

logger = logging.getLogger(__name__)


async def _one_poll(bot: Bot, samsara: SamsaraClient, state: State) -> int:
    """One iteration — returns # of events dispatched."""
    now = datetime.now(timezone.utc).replace(microsecond=0)

    last_iso = state.get_last_poll_time()
    if last_iso:
        try:
            start = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        except ValueError:
            start = now - timedelta(seconds=POLL_INTERVAL_SECONDS)
    else:
        start = now - timedelta(seconds=POLL_INTERVAL_SECONDS)

    # Guard: Samsara rejects windows with startTime > endTime.
    if start >= now:
        start = now - timedelta(seconds=10)

    sent = 0
    try:
        async for row in samsara.iter_safety_events(start_time=start,
                                                    end_time=now,
                                                    page_size=100):
            try:
                if await dispatch_event(bot, samsara, state, row):
                    sent += 1
            except Exception as e:  # dispatch_event already swallows,
                logger.exception("dispatch error: %s", e)  # belt + braces
    except SamsaraError as e:
        logger.warning("poll_safety_events error: %s", e)
    except Exception as e:
        logger.exception("Unexpected poll error: %s", e)

    # Advance the cursor only on a successful run so a transient
    # 5xx just retries the same window next iteration.
    state.set_last_poll_time(now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return sent


async def run_poller(bot: Bot, samsara: SamsaraClient, state: State) -> None:
    """Forever loop. Cancellable from the main task group."""
    logger.info("Safety-events poller started "
                "(interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            n = await _one_poll(bot, samsara, state)
            if n:
                logger.info("Poller cycle: dispatched %d event(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Poller cycle crashed: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
