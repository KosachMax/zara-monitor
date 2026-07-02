from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
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
class CheckProgress:
    running: bool = False
    total: int = 0
    checked: int = 0
    errors: int = 0
    notifications: int = 0
    available: int = 0
    current_product_id: str = ""
    started_at: datetime | None = None
    updated_at: datetime | None = None

    def progress_bar(self, width: int = 12) -> str:
        if self.total <= 0:
            return "░" * width
        filled = min(width, max(0, round(width * self.checked / self.total)))
        return "█" * filled + "░" * (width - filled)

    def format_for_user(self, title: str | None = None) -> str:
        status = title or ("Проверка выполняется" if self.running else "Проверка завершена")
        elapsed_line = ""
        if self.started_at is not None:
            elapsed_sec = max(0, int((datetime.now() - self.started_at).total_seconds()))
            elapsed_line = f"\n⏱ Уже идёт: <b>{elapsed_sec // 60}:{elapsed_sec % 60:02d}</b>"
        current_line = (
            f"\n📦 Сейчас: <code>{html_escape(self.current_product_id)}</code>" if self.current_product_id else ""
        )
        return (
            f"🔄 <b>{html_escape(status)}</b>\n\n"
            f"<code>{self.progress_bar()}</code> <b>{self.checked}/{self.total}</b>"
            f"{elapsed_line}{current_line}\n"
            f"✅ В наличии: <b>{self.available}</b>\n"
            f"🔔 Уведомлений: <b>{self.notifications}</b>\n"
            f"⚠️ Ошибок: <b>{self.errors}</b>"
        )


ProgressCallback = Callable[[CheckProgress], Awaitable[None]]


@dataclass(slots=True)
class CheckItemResult:
    product_id: str
    product_name: str
    target_size_label: str
    color_name: str = ""
    is_available: bool = False
    notification_sent: bool = False
    error: str | None = None

    def format_for_user(self) -> str:
        status = "⚠️" if self.error else "✅" if self.is_available else "❌"
        label = self.product_name or f"артикул {self.product_id}"
        color_suffix = f" / {self.color_name}" if self.color_name else ""
        notification_suffix = " / 🔔" if self.notification_sent else ""
        error_suffix = f" — <code>{html_escape(self.error)}</code>" if self.error else ""
        return (
            f"{status} <b>{html_escape(label)}</b>{html_escape(color_suffix)}\n"
            f"   📐 {html_escape(self.target_size_label)}{notification_suffix}{error_suffix}"
        )


@dataclass(slots=True)
class CheckSummary:
    started: bool
    checked: int = 0
    errors: int = 0
    notifications: int = 0
    available: int = 0
    message: str = ""
    results: list[CheckItemResult] | None = None

    def format_for_user(self) -> str:
        if not self.started:
            return self.message or "Проверка уже выполняется."
        if self.message:
            return self.message

        lines = [
            "✅ <b>Проверка завершена</b>",
            "",
            f"Проверено: <b>{self.checked}</b>",
            f"В наличии: <b>{self.available}</b>",
            f"Уведомлений отправлено: <b>{self.notifications}</b>",
            f"Ошибок: <b>{self.errors}</b>",
        ]
        if self.results:
            lines.extend(["", "<b>Сводка по товарам:</b>"])
            lines.extend(result.format_for_user() for result in self.results)
        return "\n".join(lines)


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
        self.progress = CheckProgress()
        self.last_summary: CheckSummary | None = None
        # Chats that already have a background /check_now watcher attached to
        # the currently running check. Prevents spawning a new watcher (and a
        # new tracked message) on every repeated button press.
        self.watching_chat_ids: set[str] = set()

    def is_check_running(self) -> bool:
        return self.check_lock.locked()

    def current_progress(self) -> CheckProgress:
        return CheckProgress(
            running=self.progress.running,
            total=self.progress.total,
            checked=self.progress.checked,
            errors=self.progress.errors,
            notifications=self.progress.notifications,
            available=self.progress.available,
            current_product_id=self.progress.current_product_id,
            started_at=self.progress.started_at,
            updated_at=self.progress.updated_at,
        )

    async def publish_progress(self, progress_callback: ProgressCallback | None) -> None:
        if progress_callback is not None:
            await progress_callback(self.current_progress())

    async def check_once(self, progress_callback: ProgressCallback | None = None) -> CheckSummary:
        if self.check_lock.locked():
            return CheckSummary(
                started=False,
                message=self.current_progress().format_for_user("Проверка уже выполняется, дождись результата"),
            )

        async with self.check_lock:
            items = await self.store.snapshot()
            started_at = datetime.now()

            # Several subscriptions (different chats, colors, or sizes) can
            # point at the same Zara product_id — most commonly because the
            # multi-user migration gives every chat its own copy of a legacy
            # subscription. Fetch each unique product once instead of once
            # per subscription, and show progress against that unique count
            # rather than the (larger, less intuitive) subscription count.
            unique_product_ids: list[str] = []
            seen_product_ids: set[str] = set()
            for item in items:
                product_id = str(item["product_id"])
                if product_id not in seen_product_ids:
                    seen_product_ids.add(product_id)
                    unique_product_ids.append(product_id)

            self.progress = CheckProgress(
                running=True,
                total=len(unique_product_ids),
                started_at=started_at,
                updated_at=started_at,
            )
            await self.publish_progress(progress_callback)

            if not items:
                self.health.record_success()
                self.progress = CheckProgress(running=False, total=0, started_at=started_at, updated_at=datetime.now())
                await self.publish_progress(progress_callback)
                self.last_summary = CheckSummary(started=True, message="Список мониторинга пуст.")
                return self.last_summary

            checked = 0
            errors: list[str] = []
            notifications = 0
            available = 0
            results: list[CheckItemResult] = []

            products: dict[str, dict[str, Any]] = {}
            product_errors: dict[str, str] = {}

            for product_id in unique_product_ids:
                self.progress.current_product_id = product_id
                self.progress.updated_at = datetime.now()
                await self.publish_progress(progress_callback)

                try:
                    products[product_id] = await self.zara.fetch_product(product_id)
                except ZaraError as e:
                    error = str(sanitize_log_value(e))
                    product_errors[product_id] = error
                    logger.error("Check failed for %s: %s", product_id, error)
                finally:
                    checked += 1
                    self.progress.checked = checked
                    self.progress.updated_at = datetime.now()
                    await self.publish_progress(progress_callback)
                    await asyncio.sleep(REQUEST_DELAY_SEC)

            for item in items:
                product_id = str(item["product_id"])
                item_result = CheckItemResult(
                    product_id=product_id,
                    product_name=str(item.get("product_name") or ""),
                    target_size_label=str(item["target_size_label"]),
                    color_name=str(item.get("color_name") or ""),
                )

                if product_id in product_errors:
                    error = product_errors[product_id]
                    item_result.error = error
                    errors.append(f"{product_id}: {error}")
                    results.append(item_result)
                    await self.store.set_error(item["id"], error)
                    continue

                try:
                    color, target = find_target_size(products[product_id], item)
                    is_available = bool(target) and target["availability"] in IN_STOCK_STATUSES
                    item_result.is_available = is_available
                    if is_available:
                        available += 1

                    logger.info(
                        "[%s] %s / %s%s: %s",
                        datetime.now().strftime("%H:%M:%S"),
                        product_id,
                        item["target_size_label"],
                        f" / {item['color_name']}" if item.get("color_name") else "",
                        "✅" if is_available else "❌",
                    )

                    became_available = is_available and not item.get("last_available")
                    await self.store.set_check_result(item["id"], is_available=is_available)

                    if became_available:
                        sent = await self.send_stock_notification(item, color)
                        item_result.notification_sent = sent
                        notifications += int(sent)
                        if not sent:
                            item_result.error = (
                                "Stock is available, but notification was skipped because chat_id is not allowed"
                            )
                            await self.store.set_error(item["id"], item_result.error)
                except TelegramError as e:
                    error = str(sanitize_log_value(e))
                    item_result.error = error
                    errors.append(f"telegram: {error}")
                    logger.error("Notification failed for %s: %s", product_id, error)
                    await self.store.set_error(item["id"], error)
                finally:
                    results.append(item_result)
                    self.progress.errors = len(errors)
                    self.progress.notifications = notifications
                    self.progress.available = available
                    self.progress.updated_at = datetime.now()
                    await self.publish_progress(progress_callback)

            if errors:
                alert_due = self.health.record_failure(errors[0])
                if alert_due and await self.broadcast_health_alert(errors[0]):
                    self.health.mark_degraded_alert_sent()
            else:
                recovery_due = self.health.record_success()
                if recovery_due:
                    await self.broadcast_recovery()

            self.progress = CheckProgress(
                running=False,
                total=len(unique_product_ids),
                checked=checked,
                errors=len(errors),
                notifications=notifications,
                available=available,
                started_at=started_at,
                updated_at=datetime.now(),
            )
            await self.publish_progress(progress_callback)

            self.last_summary = CheckSummary(
                started=True,
                checked=checked,
                errors=len(errors),
                notifications=notifications,
                available=available,
                results=results,
            )
            return self.last_summary

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
