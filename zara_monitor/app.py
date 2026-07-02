from __future__ import annotations

import asyncio
import logging

import httpx

from .bot_controller import check_loop, telegram_listener
from .config import Config
from .errors import TelegramError
from .health import HealthMonitor
from .logging_config import setup_logging
from .monitor_service import MonitorService
from .storage import ProductStore
from .telegram_client import TelegramClient
from .zara_client import ZaraClient

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    config = Config.from_env()
    store = ProductStore(config.tg_chat_ids, config.max_tracked_items)
    health = HealthMonitor(config.health_error_threshold)

    logger.info(
        "Monitor started | tracking %s subscription(s) | store=%s | locale=%s | interval=%ss",
        len(await store.snapshot()),
        config.store_id,
        config.locale,
        config.interval,
    )

    async with httpx.AsyncClient() as client:
        telegram = TelegramClient(client, config.tg_token)
        zara = ZaraClient(client, config.store_id, config.locale)
        monitor = MonitorService(zara, telegram, store, config, health)

        try:
            await telegram.set_my_commands()
        except TelegramError as e:
            logger.warning("Failed to register Telegram commands: %s", e)

        await asyncio.gather(
            telegram_listener(telegram, zara, store, monitor, health, config),
            check_loop(monitor, config),
        )
