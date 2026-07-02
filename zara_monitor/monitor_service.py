from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Config
from .constants import DEFAULT_COLOR_ID, IN_STOCK_STATUSES, MAIN_MENU_KEYBOARD, REQUEST_DELAY_SEC
from .errors import TelegramError, ZaraError
from .health import HealthMonitor
from .logging_config import sanitize_log_value
from .storage import ProductStore
from .telegram_client import TelegramClient
from .utils import html_escape, product_page_url
from .zara_client import ZaraClient

logger = logging.getLogger(__name__)


def find_target_size(
    product: dict[str, Any], item: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    target_size_id = str(item["target_size_id"])
    color_id = str(item.get("color_id") or DEFAULT_COLOR_ID)
    colors = product.get("colors", [])

    if color_id != DEFAULT_COLOR_ID:
        for color in colors:
            if str(color.get("id")) == color_id:
                target = next((size for size in color.get("sizes", []) if str(size.get("id")) == target_size_id), None)
                return color, target
        return None, None

    # Legacy subscription without color: search all colors and keep old behavior.
    for color in colors:
        target = next((size for size in color.get("sizes", []) if str(size.get("id")) == target_size_id), None)
        if target is not None:
            return color, target
    return None, None


@dataclass(slots=True)
class CheckSummary:
    started: bool
    checked: int = 0
    errors: int = 0
    notifications: int = 0
    available: int = 0
    message: str = ""

    def format_for_user(self) -> str:
        if not self.started:
            return self.message or "Проверка уже выполняется."
        return (
            "Проверка завершена.\n"
            f"Проверено: <b>{self.checked}</b>\n"
            f"В наличии: <b>{self.available}</b>\n"
            f"Уведомлений отправлено: <b>{self.notifications}</b>\n"
            f"Ошибок: <b>{self.errors}</b>"
        )


class MonitorService:
    def __init__(
        self,
        zara: ZaraClient,
        telegram: TelegramClient,
        store: ProductStore,
        config: Config,
        health: HealthMonitor,
    ) -> None:
        self.zara = zara
        self.telegram = telegram
        self.store = store
        self.config = config
        self.health = health
        self.check_lock = asyncio.Lock()

    async def check_once(self) -> CheckSummary:
        if self.check_lock.locked():
            return CheckSummary(started=False, message="Проверка уже выполняется, дождись результата.")

        async with self.check_lock:
            items = await self.store.snapshot()
            if not items:
                self.health.record_success()
                return CheckSummary(started=True, message="Список мониторинга пуст.")

            checked = 0
            errors: list[str] = []
            notifications = 0
            available = 0

            for item in items:
                checked += 1
                try:
                    product = await self.zara.fetch_product(item["product_id"])
                    color, target = find_target_size(product, item)
                    is_available = bool(target) and target["availability"] in IN_STOCK_STATUSES
                    if is_available:
                        available += 1

                    logger.info(
                        "[%s] %s / %s%s: %s",
                        datetime.now().strftime("%H:%M:%S"),
                        item["product_id"],
                        item["target_size_label"],
                        f" / {item['color_name']}" if item.get("color_name") else "",
                        "✅" if is_available else "❌",
                    )

                    became_available = is_available and not item.get("last_available")
                    await self.store.set_check_result(item["id"], is_available=is_available)

                    if became_available:
                        sent = await self.send_stock_notification(item, color)
                        notifications += int(sent)
                        if not sent:
                            await self.store.set_error(
                                item["id"],
                                "Stock is available, but notification was skipped because chat_id is not allowed",
                            )
                except ZaraError as e:
                    error = str(sanitize_log_value(e))
                    errors.append(f"{item['product_id']}: {error}")
                    logger.error("Check failed for %s: %s", item["product_id"], error)
                    await self.store.set_error(item["id"], error)
                except TelegramError as e:
                    error = str(sanitize_log_value(e))
                    errors.append(f"telegram: {error}")
                    logger.error("Notification failed for %s: %s", item["product_id"], error)
                    await self.store.set_error(item["id"], error)
                finally:
                    await asyncio.sleep(REQUEST_DELAY_SEC)

            if errors:
                alert_due = self.health.record_failure(errors[0])
                if alert_due and await self.broadcast_health_alert(errors[0]):
                    self.health.mark_degraded_alert_sent()
            else:
                recovery_due = self.health.record_success()
                if recovery_due:
                    await self.broadcast_recovery()

            return CheckSummary(
                started=True,
                checked=checked,
                errors=len(errors),
                notifications=notifications,
                available=available,
            )

    async def send_stock_notification(self, item: dict[str, Any], color: dict[str, Any] | None) -> bool:
        chat_id = str(item["chat_id"])
        if chat_id not in self.config.tg_chat_ids:
            logger.warning("Skipping notification for unauthorized chat %s", chat_id)
            return False

        label = item.get("product_name") or f"артикул {item['product_id']}"
        color_name = item.get("color_name") or (color or {}).get("name") or ""
        color_line = f"\n🎨 Цвет: <b>{html_escape(color_name)}</b>" if color_name else ""
        product_url = product_page_url(item["product_id"])
        image_url = item.get("color_image") or item.get("product_image") or (color or {}).get("image_url")

        await self.telegram.send_product_message(
            chat_id,
            f"🛍 <b>Zara: размер появился!</b>\n\n"
            f"📦 {html_escape(label)}{color_line}\n"
            f"📐 Размер: <b>{html_escape(item['target_size_label'])}</b>",
            image_url=image_url,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🔗 Открыть товар", "url": product_url},
                        {"text": "🔕 Отписаться", "callback_data": f"remove_id:{item['id']}"},
                    ]
                ]
            },
        )
        return True

    async def broadcast_health_alert(self, error: str) -> bool:
        text = (
            "⚠️ <b>Zara Monitor: проблема с проверкой товаров</b>\n\n"
            f"Не могу стабильно получить данные уже {self.health.consecutive_failed_cycles} "
            "цикл(а/ов) подряд.\n"
            f"Последняя ошибка: <code>{html_escape(error)}</code>\n\n"
            "Повторять это предупреждение не буду, пока состояние не изменится."
        )
        return await self.broadcast_to_admins(text)

    async def broadcast_recovery(self) -> None:
        await self.broadcast_to_admins("✅ <b>Zara Monitor восстановился</b>\nПроверки снова проходят успешно.")

    async def broadcast_to_admins(self, text: str) -> bool:
        delivered = False
        for chat_id in self.config.tg_chat_ids:
            try:
                await self.telegram.send_message(chat_id, text, MAIN_MENU_KEYBOARD)
                delivered = True
            except TelegramError as e:
                logger.error("Failed to send health message to %s: %s", chat_id, e)
        return delivered
