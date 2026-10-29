"""
bot.py — Samsara Safety Bot entry point
========================================
Boots:
  1) The aiogram Dispatcher (Telegram polling for operator commands).
  2) The aiohttp web server (Samsara webhook receiver on /webhook/samsara).
  3) Optionally the safety-events poller (fallback mode).

All three run concurrently in one process. Shutdown is cooperative —
Ctrl+C or Railway's SIGTERM cancels the task group and closes
sessions cleanly.

Deploy target: Railway.app (single service, start command = `python bot.py`).
Port binding: reads $PORT (Railway sets this automatically).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from config import (
    BOT_TOKEN_SAFETY,
    ENABLE_SAFETY_POLLER,
    LOG_LEVEL,
    PORT,
    STATE_FILE,
)
from engine.poller import run_poller
from engine.webhook_server import build_app
from handlers import commands_router
from services.samsara import SamsaraClient
from services.state import State

logger = logging.getLogger("safety_bot")


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # aiohttp access logs are noisy; keep them at WARNING+.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _start_web(bot: Bot, samsara: SamsaraClient, state: State,
                     port: int) -> web.AppRunner:
    """Start the aiohttp webhook server. Returns the runner so we can
    cleanly shut it down on exit."""
    app = build_app(bot, samsara, state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Webhook server listening on 0.0.0.0:%d "
                "(POST /webhook/samsara)", port)
    return runner


async def _start_tg(bot: Bot, dp: Dispatcher,
                    samsara: SamsaraClient, state: State) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting Telegram long-poll…")
    # NB: pass as `app_state`, not `state` — `state` is reserved by aiogram's
    # FSM middleware (it would be shadowed by FSMContext in every handler).
    await dp.start_polling(bot, samsara=samsara, app_state=state,
                           handle_signals=False)


async def main() -> None:
    _configure_logging()

    if not BOT_TOKEN_SAFETY:
        raise SystemExit(
            "BOT_TOKEN_SAFETY is not set. Create a bot with @BotFather and "
            "put the token in .env or Railway env vars."
        )

    # ---- shared singletons ----
    state   = State(STATE_FILE)
    samsara = SamsaraClient()
    bot     = Bot(
        token=BOT_TOKEN_SAFETY,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Handlers receive samsara/state via aiogram 3 dependency injection
    # (kwargs passed to dp.start_polling are injected into every handler).
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(commands_router)

    # ---- start subsystems ----
    port = int(os.getenv("PORT", str(PORT)))
    runner = await _start_web(bot, samsara, state, port)

    stop_event = asyncio.Event()

    def _stop(_sig_number=None, _frame=None):
        logger.info("Shutdown signal received.")
        stop_event.set()

    # Register signal handlers in the main thread so Railway's SIGTERM
    # exits cleanly instead of being swallowed by the task group.
    try:
        loop = asyncio.get_running_loop()
        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, _stop)
    except NotImplementedError:
        pass  # Windows / unsupported env — Ctrl+C still works via KeyboardInterrupt.

    tasks: list[asyncio.Task] = [
        asyncio.create_task(_start_tg(bot, dp, samsara, state), name="tg_poll"),
    ]
    if ENABLE_SAFETY_POLLER:
        tasks.append(asyncio.create_task(run_poller(bot, samsara, state),
                                         name="poller"))
    else:
        logger.info("Safety-events poller DISABLED "
                    "(set ENABLE_SAFETY_POLLER=1 to turn on).")

    # Wait for either (a) stop signal, or (b) any critical task crashing.
    stop_task = asyncio.create_task(stop_event.wait(), name="stop")
    done, _pending = await asyncio.wait(
        tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED,
    )
    for t in done:
        if t is stop_task:
            continue
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Task %s crashed", t.get_name())

    # ---- cleanup ----
    logger.info("Shutting down…")
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        try: await t
        except Exception: pass
    await runner.cleanup()
    await bot.session.close()
    await samsara.close()
    logger.info("Bye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
