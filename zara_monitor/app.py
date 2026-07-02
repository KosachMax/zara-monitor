from __future__ import annotations

import asyncio
import logging

import httpx

from .bot_controller import check_loop, telegram_listener
from .config import Config
from .errors import StorageError, TelegramError
from .health import HealthMonitor
from .logging_config import setup_logging
from .monitor_service import MonitorService
from .storage import ProductStore
from .telegram_client import TelegramClient
from .utils import html_escape
from .zara_client import ZaraClient

logger = logging.getLogger(__name__)


async def notify_storage_startup_error(telegram: TelegramClient, chat_ids: list[str], error: StorageError) -> None:
    text = (
        "🚨 <b>Zara Monitor: storage недоступен</b>\n\n"
        "Worker запущен в read-only degraded mode, чтобы бот мог сообщить о проблеме, "
        "но мониторинг и изменения списка подписок не работают до ремонта файла.\n\n"
        f"Ошибка: <code>{html_escape(error)}</code>"
    )
    for chat_id in chat_ids:
        try:
            await telegram.send_message(chat_id, text)
        except TelegramError as e:
            logger.error("Failed to send storage startup alert to %s: %s", chat_id, e)


async def main() -> None:
    setup_logging()
    config = Config.from_env()
    store = ProductStore(config.tg_chat_ids, config.max_tracked_items, tolerate_load_error=True)
    health = HealthMonitor(config.health_error_threshold)
    if store.load_error is not None:
        health.record_failure(str(store.load_error))

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

        if store.load_error is not None:
            await notify_storage_startup_error(telegram, config.tg_chat_ids, store.load_error)

        await asyncio.gather(
            telegram_listener(telegram, zara, store, monitor, health, config),
            check_loop(monitor, config),
        )
