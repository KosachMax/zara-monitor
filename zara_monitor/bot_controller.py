from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from .config import Config
from .constants import (
    BTN_ADD,
    BTN_CHECK_NOW,
    BTN_LIST,
    BTN_REMOVE,
    CHECK_NOW_WATCH_POLL_SEC,
    DEFAULT_COLOR_ID,
    IN_STOCK_STATUSES,
    MAIN_MENU_KEYBOARD,
    PAGE_SIZE,
)
from .errors import MaxTrackedItemsError, TelegramConflictError, TelegramError, ZaraError
from .health import HealthMonitor
from .logging_config import sanitize_log_value
from .monitor_service import CheckProgress, MonitorService
from .storage import ProductStore
from .telegram_client import TelegramClient
from .utils import extract_product_id_from_url, find_zara_url, html_escape, now_iso, product_page_url, size_label
from .zara_client import ZaraClient, product_image

logger = logging.getLogger(__name__)


def clamp_page(page: int, total_items: int) -> int:
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    return max(0, min(page, total_pages - 1))


def paginate(items: list[dict[str, Any]], page: int) -> tuple[list[dict[str, Any]], int, int]:
    page = clamp_page(page, len(items))
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    return items[start : start + PAGE_SIZE], page, total_pages


def pagination_buttons(prefix: str, page: int, total_pages: int) -> list[dict[str, str]]:
    buttons: list[dict[str, str]] = []
    if page > 0:
        buttons.append({"text": "⬅️ Назад", "callback_data": f"{prefix}:{page - 1}"})
    if page + 1 < total_pages:
        buttons.append({"text": "Вперёд ➡️", "callback_data": f"{prefix}:{page + 1}"})
    return buttons


def format_waitlist(items: list[dict[str, Any]], page: int) -> tuple[str, dict[str, Any] | None]:
    page_items, page, total_pages = paginate(items, page)
    lines = [f"<b>Список ожидания · {len(items)} подписок</b>", f"Страница {page + 1}/{total_pages}"]

    start = page * PAGE_SIZE
    for index, item in enumerate(page_items, start=start + 1):
        name = html_escape(item.get("product_name") or item["product_id"])
        size = html_escape(item["target_size_label"])
        color = html_escape(item.get("color_name") or "")
        product_url = product_page_url(item["product_id"])
        status = "✅ появился!" if item.get("last_available") else "❌ нет в наличии"
        color_line = f"\n🎨 Цвет: {color}" if color else ""
        error_line = f"\n⚠️ {html_escape(item['last_error'])}" if item.get("last_error") else ""
        lines.append(
            f'\n<b>{index}. {name}</b>{color_line}\n↳ <a href="{product_url}">{size}</a> · {status}{error_line}'
        )

    nav = pagination_buttons("list_page", page, total_pages)
    keyboard = {"inline_keyboard": [nav]} if nav else None
    return "\n".join(lines), keyboard


def sizes_inline_keyboard(sizes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{index + 1}. {size_label(size)} — {size['availability']}",
                    "callback_data": f"size:{index}",
                }
            ]
            for index, size in enumerate(sizes)
        ]
    }


def colors_inline_keyboard(colors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{index + 1}. {color.get('name') or 'Цвет'}",
                    "callback_data": f"color:{index}",
                }
            ]
            for index, color in enumerate(colors)
        ]
    }


def removable_items_inline_keyboard(items: list[dict[str, Any]], page: int) -> dict[str, Any]:
    page_items, page, total_pages = paginate(items, page)
    rows: list[list[dict[str, str]]] = []
    for item in page_items:
        color_suffix = f" / {item['color_name']}" if item.get("color_name") else ""
        text = f"{item.get('product_name') or item['product_id']}{color_suffix} / {item['target_size_label']}"
        if len(text) > 60:
            text = f"{text[:57]}..."
        rows.append([{"text": text, "callback_data": f"remove_id:{item['id']}"}])

    nav = pagination_buttons("remove_page", page, total_pages)
    if nav:
        rows.append(nav)
    return {"inline_keyboard": rows}


async def send_help(telegram: TelegramClient, chat_id: str | int) -> None:
    await telegram.send_message(
        chat_id,
        "<b>Zara Stock Monitor</b>\n\n"
        "Команды:\n"
        "/add &lt;артикул&gt; — добавить товар\n"
        "/list — список ожидания\n"
        "/remove — выбрать товар для удаления\n"
        "/remove &lt;артикул&gt; — удалить все размеры товара\n"
        "/check_now — проверить товары сейчас\n"
        "/status — статус мониторинга\n"
        "/cancel — отменить текущий сценарий\n"
        "/help — справка\n\n"
        "Можно также прислать ссылку Zara — бот сам найдёт артикул.",
        MAIN_MENU_KEYBOARD,
    )


async def send_status(
    telegram: TelegramClient,
    store: ProductStore,
    health: HealthMonitor,
    config: Config,
    chat_id: str | int,
) -> None:
    items = await store.snapshot()
    await telegram.send_message(chat_id, health.status_text(len(items), config.interval), MAIN_MENU_KEYBOARD)


async def edit_or_send_status(
    telegram: TelegramClient,
    chat_id: str | int,
    text: str,
    message_id: int | None,
) -> int | None:
    if message_id is None:
        message = await telegram.send_message(chat_id, text, MAIN_MENU_KEYBOARD)
        return int(message["message_id"]) if message and "message_id" in message else None
    try:
        await telegram.edit_message_text(chat_id, message_id, text, MAIN_MENU_KEYBOARD)
        return message_id
    except TelegramError as e:
        logger.warning("Failed to edit check status message %s for chat %s: %s", message_id, chat_id, e)
        return message_id


async def run_check_now_and_report(
    telegram: TelegramClient,
    monitor: MonitorService,
    chat_id: str | int,
    message_id: int | None = None,
) -> None:
    last_text = ""

    async def publish(progress: CheckProgress) -> None:
        nonlocal last_text, message_id
        text = progress.format_for_user()
        if text == last_text:
            return
        message_id = await edit_or_send_status(telegram, chat_id, text, message_id)
        last_text = text

    try:
        summary = await monitor.check_once(progress_callback=publish)
        final_text = summary.format_for_user()
        if final_text != last_text:
            await edit_or_send_status(telegram, chat_id, final_text, message_id)
    except Exception as e:
        logger.error("Background /check_now failed for chat %s: %s", chat_id, sanitize_log_value(str(e)))
        try:
            await edit_or_send_status(
                telegram,
                chat_id,
                f"Проверка завершилась ошибкой: <code>{html_escape(e)}</code>",
                message_id,
            )
        except TelegramError as send_error:
            logger.error("Failed to report /check_now error to %s: %s", chat_id, send_error)


async def watch_running_check_and_report(
    telegram: TelegramClient,
    monitor: MonitorService,
    chat_id: str | int,
    message_id: int | None = None,
) -> None:
    last_text = ""
    while monitor.is_check_running():
        text = monitor.current_progress().format_for_user("Проверка уже выполняется, дождись результата")
        if text != last_text:
            message_id = await edit_or_send_status(telegram, chat_id, text, message_id)
            last_text = text
        await asyncio.sleep(CHECK_NOW_WATCH_POLL_SEC)

    summary = monitor.last_summary
    final_text = summary.format_for_user() if summary is not None else "Проверка завершена, но сводка недоступна."
    if final_text != last_text:
        await edit_or_send_status(telegram, chat_id, final_text, message_id)


async def send_add_prompt(
    telegram: TelegramClient,
    chat_id: str | int,
    pending: dict[str, dict[str, Any]],
) -> None:
    pending[str(chat_id)] = {"stage": "awaiting_id"}
    await telegram.send_message(chat_id, "Пришли артикул товара или ссылку Zara.", MAIN_MENU_KEYBOARD)


async def start_add_flow(
    telegram: TelegramClient,
    zara: ZaraClient,
    store: ProductStore,
    chat_id: str | int,
    product_id: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    product_id = product_id.strip()
    if not product_id.isdigit():
        await telegram.send_message(chat_id, "Артикул должен быть числом. Попробуй ещё раз.", MAIN_MENU_KEYBOARD)
        return

    try:
        product = await zara.fetch_product(product_id)
    except ZaraError as e:
        pending.pop(str(chat_id), None)
        await telegram.send_message(
            chat_id,
            f"Не удалось получить товар {html_escape(product_id)}: <code>{html_escape(e)}</code>",
            MAIN_MENU_KEYBOARD,
        )
        return

    await continue_add_with_product(telegram, store, chat_id, product, pending)


async def continue_add_with_product(
    telegram: TelegramClient,
    store: ProductStore,
    chat_id: str | int,
    product: dict[str, Any],
    pending: dict[str, dict[str, Any]],
) -> None:
    colors = [color for color in product.get("colors", []) if color.get("sizes")]
    if not colors:
        pending.pop(str(chat_id), None)
        await telegram.send_message(
            chat_id,
            f"У товара {html_escape(product['product_id'])} не нашлось размеров.",
            MAIN_MENU_KEYBOARD,
        )
        return

    if len(colors) > 1:
        pending[str(chat_id)] = {"stage": "choosing_color", "product": product, "colors": colors}
        await telegram.send_product_message(
            chat_id,
            f"{html_escape(product.get('name') or f'артикул {product["product_id"]}')}. Выбери цвет:",
            image_url=product_image(product),
            reply_markup=colors_inline_keyboard(colors),
        )
        return

    await prompt_size_selection(telegram, chat_id, product, colors[0], pending)


async def prompt_size_selection(
    telegram: TelegramClient,
    chat_id: str | int,
    product: dict[str, Any],
    color: dict[str, Any],
    pending: dict[str, dict[str, Any]],
) -> None:
    sizes = color.get("sizes", [])
    if not sizes:
        pending.pop(str(chat_id), None)
        await telegram.send_message(chat_id, "У выбранного цвета не нашлось размеров.", MAIN_MENU_KEYBOARD)
        return

    pending[str(chat_id)] = {
        "stage": "choosing_size",
        "product": product,
        "color": color,
        "sizes": sizes,
    }
    color_suffix = f" · цвет {html_escape(color['name'])}" if color.get("name") else ""
    await telegram.send_product_message(
        chat_id,
        f"{html_escape(product.get('name') or f'артикул {product["product_id"]}')}{color_suffix}. Выбери размер:",
        image_url=color.get("image_url") or product_image(product),
        reply_markup=sizes_inline_keyboard(sizes),
    )


async def add_selected_size(
    telegram: TelegramClient,
    store: ProductStore,
    chat_id: str | int,
    product: dict[str, Any],
    color: dict[str, Any],
    chosen: dict[str, Any],
) -> None:
    is_available = chosen["availability"] in IN_STOCK_STATUSES
    item = {
        "id": uuid.uuid4().hex,
        "chat_id": str(chat_id),
        "product_id": str(product["product_id"]),
        "product_name": str(product.get("name") or ""),
        "product_image": product_image(product),
        "color_id": str(color.get("id") or DEFAULT_COLOR_ID),
        "color_name": str(color.get("name") or ""),
        "color_image": color.get("image_url") or product_image(product),
        "target_size_id": str(chosen["id"]),
        "target_size_label": size_label(chosen),
        "last_available": is_available,
        "last_checked_at": now_iso(),
        "last_error": None,
    }

    try:
        added, existing = await store.add(item)
    except MaxTrackedItemsError as e:
        await telegram.send_message(chat_id, f"Невозможно добавить товар: {html_escape(e)}", MAIN_MENU_KEYBOARD)
        return

    label = item["product_name"] or f"артикул {item['product_id']}"
    color_line = f"\n🎨 Цвет: <b>{html_escape(item['color_name'])}</b>" if item.get("color_name") else ""

    if not added:
        assert existing is not None
        await telegram.send_product_message(
            chat_id,
            "Невозможно добавить товар: он уже есть в мониторинге.\n\n"
            f"📦 {html_escape(existing.get('product_name') or existing['product_id'])}{color_line}\n"
            f"📐 Размер: <b>{html_escape(existing['target_size_label'])}</b>",
            image_url=existing.get("color_image") or existing.get("product_image"),
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    status = "уже в наличии ✅" if is_available else "пока нет в наличии, сообщу когда появится ❌"
    await telegram.send_product_message(
        chat_id,
        f"Добавлено: {html_escape(label)}{color_line}\n"
        f"📐 Размер: <b>{html_escape(item['target_size_label'])}</b> — {status}",
        image_url=item.get("color_image") or item.get("product_image"),
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def send_list(
    telegram: TelegramClient,
    store: ProductStore,
    chat_id: str | int,
    page: int = 0,
) -> None:
    items = await store.snapshot(str(chat_id))
    if not items:
        await telegram.send_message(
            chat_id,
            "Список пуст. Нажми ➕ Добавить, чтобы начать.",
            MAIN_MENU_KEYBOARD,
        )
        return

    text, keyboard = format_waitlist(items, page)
    await telegram.send_message(chat_id, text, keyboard or MAIN_MENU_KEYBOARD)


async def send_remove_prompt(
    telegram: TelegramClient,
    store: ProductStore,
    chat_id: str | int,
    page: int = 0,
) -> None:
    items = await store.snapshot(str(chat_id))
    if not items:
        await telegram.send_message(chat_id, "Список пуст, нечего удалять.", MAIN_MENU_KEYBOARD)
        return

    page = clamp_page(page, len(items))
    await telegram.send_message(
        chat_id,
        "Что убрать из мониторинга?",
        removable_items_inline_keyboard(items, page),
    )


async def handle_shared_zara_url(
    telegram: TelegramClient,
    zara: ZaraClient,
    chat_id: str | int,
    zara_url: str,
    pending: dict[str, dict[str, Any]],
) -> bool:
    shared_product_id = extract_product_id_from_url(zara_url)
    if not shared_product_id:
        await telegram.send_message(
            chat_id,
            "Это похоже на ссылку Zara, но не удалось найти в ней артикул. "
            "Пришли артикул числом или нажми ➕ Добавить.",
            MAIN_MENU_KEYBOARD,
        )
        return True

    try:
        product = await zara.fetch_product(shared_product_id)
    except ZaraError as e:
        await telegram.send_message(
            chat_id,
            f"Не удалось получить товар {html_escape(shared_product_id)}: <code>{html_escape(e)}</code>",
            MAIN_MENU_KEYBOARD,
        )
        return True

    pending[str(chat_id)] = {"stage": "confirm_add", "product": product}
    await telegram.send_product_message(
        chat_id,
        f"Похоже на товар Zara: <b>{html_escape(product.get('name') or f'артикул {shared_product_id}')}</b>.\n"
        "Добавить его для мониторинга?",
        image_url=product_image(product),
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "✅ Да", "callback_data": f"confirm_add:{shared_product_id}"},
                    {"text": "❌ Нет", "callback_data": "confirm_add:no"},
                ]
            ]
        },
    )
    return True


async def handle_message(
    telegram: TelegramClient,
    zara: ZaraClient,
    store: ProductStore,
    monitor: MonitorService,
    health: HealthMonitor,
    config: Config,
    chat_id: str | int,
    text: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    chat_key = str(chat_id)
    text = text.strip()

    if text in ("/start", "/menu"):
        pending.pop(chat_key, None)
        await telegram.send_message(chat_id, "Zara Stock Monitor. Выбери действие:", MAIN_MENU_KEYBOARD)
        return

    if text in ("/help", "help"):
        await send_help(telegram, chat_id)
        return

    if text == "/cancel":
        had_pending = pending.pop(chat_key, None) is not None
        message = "Ок, текущее действие отменено." if had_pending else "Нет активного действия для отмены."
        await telegram.send_message(chat_id, message, MAIN_MENU_KEYBOARD)
        return

    if text == "/status":
        await send_status(telegram, store, health, config, chat_id)
        return

    if text in ("/check_now", BTN_CHECK_NOW):
        if monitor.is_check_running():
            # watch_running_check_and_report sends the first status update itself
            # on its own first loop iteration — sending one here too would just
            # duplicate that message.
            asyncio.create_task(watch_running_check_and_report(telegram, monitor, chat_id))
            return

        await telegram.send_message(chat_id, "Запускаю внеочередную проверку в фоне...", MAIN_MENU_KEYBOARD)
        asyncio.create_task(run_check_now_and_report(telegram, monitor, chat_id))
        return

    if text == "/export":
        exported = await store.export_state(chat_key)
        if len(exported) > 3500:
            await telegram.send_message(
                chat_id,
                "Экспорт слишком большой для одного сообщения. Используй backup на Fly volume.",
                MAIN_MENU_KEYBOARD,
            )
        else:
            await telegram.send_message(chat_id, f"<pre>{html_escape(exported)}</pre>", MAIN_MENU_KEYBOARD)
        return

    zara_url = find_zara_url(text)
    if zara_url:
        handled = await handle_shared_zara_url(telegram, zara, chat_id, zara_url, pending)
        if handled:
            return

    if text == BTN_ADD:
        await send_add_prompt(telegram, chat_id, pending)
        return

    if text.startswith("/add"):
        parts = text.split(maxsplit=1)
        product_id = parts[1].strip() if len(parts) > 1 else ""
        if not product_id:
            await send_add_prompt(telegram, chat_id, pending)
            return
        await start_add_flow(telegram, zara, store, chat_id, product_id, pending)
        return

    if text == BTN_REMOVE:
        await send_remove_prompt(telegram, store, chat_id)
        return

    if text.startswith("/remove"):
        parts = text.split(maxsplit=1)
        product_id = parts[1].strip() if len(parts) > 1 else ""
        if not product_id:
            await send_remove_prompt(telegram, store, chat_id)
            return

        removed = await store.remove_all(chat_key, product_id)
        if removed:
            await telegram.send_message(
                chat_id,
                f"Товар {html_escape(product_id)} снят с мониторинга ({len(removed)} размер(ов)).",
                MAIN_MENU_KEYBOARD,
            )
        else:
            await telegram.send_message(
                chat_id,
                f"Товар {html_escape(product_id)} не отслеживался.",
                MAIN_MENU_KEYBOARD,
            )
        return

    if text == BTN_LIST or text.startswith("/list"):
        await send_list(telegram, store, chat_id)
        return

    stage = pending.get(chat_key, {}).get("stage")

    if stage == "awaiting_id":
        await start_add_flow(telegram, zara, store, chat_id, text, pending)
        return

    if stage == "choosing_color":
        await handle_text_color_choice(telegram, chat_id, text, pending)
        return

    if stage == "choosing_size":
        await handle_text_size_choice(telegram, store, chat_id, text, pending)
        return

    await telegram.send_message(chat_id, "Не понял. Используй кнопки меню ниже или /help.", MAIN_MENU_KEYBOARD)


async def handle_text_color_choice(
    telegram: TelegramClient,
    chat_id: str | int,
    text: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    chat_key = str(chat_id)
    info = pending.get(chat_key)
    if not info or info.get("stage") != "choosing_color":
        await telegram.send_message(chat_id, "Этот выбор устарел, начни заново.", MAIN_MENU_KEYBOARD)
        return

    colors = info["colors"]
    chosen: dict[str, Any] | None = None
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(colors):
            chosen = colors[index]

    if chosen is None:
        for color in colors:
            if text.lower() == str(color.get("name", "")).lower():
                chosen = color
                break

    if chosen is None:
        await telegram.send_message(chat_id, "Не понял выбор цвета. Нажми кнопку или ответь номером.")
        return

    await prompt_size_selection(telegram, chat_id, info["product"], chosen, pending)


async def handle_text_size_choice(
    telegram: TelegramClient,
    store: ProductStore,
    chat_id: str | int,
    text: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    chat_key = str(chat_id)
    info = pending.get(chat_key)
    if not info or info.get("stage") != "choosing_size":
        await telegram.send_message(chat_id, "Этот выбор устарел, начни заново.", MAIN_MENU_KEYBOARD)
        return

    sizes = info["sizes"]
    chosen: dict[str, Any] | None = None
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(sizes):
            chosen = sizes[index]

    if chosen is None:
        for size in sizes:
            if text.lower() in {str(size.get("shortName", "")).lower(), str(size.get("name", "")).lower()}:
                chosen = size
                break

    if chosen is None:
        await telegram.send_message(chat_id, "Не понял выбор размера. Нажми кнопку или ответь номером.")
        return

    pending.pop(chat_key, None)
    await add_selected_size(telegram, store, chat_id, info["product"], info["color"], chosen)


async def handle_callback(
    telegram: TelegramClient,
    zara: ZaraClient,
    store: ProductStore,
    chat_id: str | int,
    data: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    chat_key = str(chat_id)

    if data.startswith("confirm_add:"):
        value = data.split(":", 1)[1]
        if value == "no":
            pending.pop(chat_key, None)
            await telegram.send_message(chat_id, "Ок, не добавляю.", MAIN_MENU_KEYBOARD)
            return

        info = pending.get(chat_key)
        if not info or info.get("stage") != "confirm_add" or info.get("product", {}).get("product_id") != value:
            await start_add_flow(telegram, zara, store, chat_id, value, pending)
            return

        await continue_add_with_product(telegram, store, chat_id, info["product"], pending)
        return

    if data.startswith("color:"):
        info = pending.get(chat_key)
        if not info or info.get("stage") != "choosing_color":
            await telegram.send_message(chat_id, "Этот выбор устарел, начни заново.", MAIN_MENU_KEYBOARD)
            return
        index = int(data.split(":", 1)[1])
        colors = info["colors"]
        if not (0 <= index < len(colors)):
            return
        await prompt_size_selection(telegram, chat_id, info["product"], colors[index], pending)
        return

    if data.startswith("size:"):
        info = pending.get(chat_key)
        if not info or info.get("stage") != "choosing_size":
            await telegram.send_message(chat_id, "Этот выбор устарел, начни заново.", MAIN_MENU_KEYBOARD)
            return
        index = int(data.split(":", 1)[1])
        sizes = info["sizes"]
        if not (0 <= index < len(sizes)):
            return
        pending.pop(chat_key, None)
        await add_selected_size(telegram, store, chat_id, info["product"], info["color"], sizes[index])
        return

    if data.startswith("list_page:"):
        page = int(data.split(":", 1)[1])
        await send_list(telegram, store, chat_id, page)
        return

    if data.startswith("remove_page:"):
        page = int(data.split(":", 1)[1])
        await send_remove_prompt(telegram, store, chat_id, page)
        return

    if data.startswith("remove_id:"):
        subscription_id = data.split(":", 1)[1]
        removed = await store.remove_by_id(chat_key, subscription_id)
        if removed:
            label = removed.get("product_name") or removed["product_id"]
            await telegram.send_message(
                chat_id,
                f"Убрано: {html_escape(label)} / {html_escape(removed['target_size_label'])}",
                MAIN_MENU_KEYBOARD,
            )
        else:
            await telegram.send_message(chat_id, "Уже удалено.", MAIN_MENU_KEYBOARD)
        return

    # Backward compatibility with old notification buttons: remove:<product_id>:<target_size_id>
    if data.startswith("remove:"):
        _, product_id, target_size_id = data.split(":", 2)
        removed = await store.remove_legacy(chat_key, product_id, target_size_id)
        if removed:
            label = removed.get("product_name") or product_id
            await telegram.send_message(
                chat_id,
                f"Убрано: {html_escape(label)} / {html_escape(removed['target_size_label'])}",
                MAIN_MENU_KEYBOARD,
            )
        else:
            await telegram.send_message(chat_id, "Уже удалено.", MAIN_MENU_KEYBOARD)
        return


async def telegram_listener(
    telegram: TelegramClient,
    zara: ZaraClient,
    store: ProductStore,
    monitor: MonitorService,
    health: HealthMonitor,
    config: Config,
) -> None:
    pending: dict[str, dict[str, Any]] = {}
    offset = None
    conflict_count = 0

    while True:
        try:
            updates = await telegram.get_updates(offset)
            conflict_count = 0
        except TelegramConflictError as e:
            conflict_count += 1
            logger.error("%s", e)
            if conflict_count >= config.telegram_conflict_exit_threshold:
                raise
            await asyncio.sleep(10)
            continue
        except TelegramError as e:
            logger.warning("getUpdates failed: %s", e)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1

            callback_query = update.get("callback_query")
            if callback_query is not None:
                chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
                data = callback_query.get("data")
                if chat_id is None or not data or str(chat_id) not in config.tg_chat_ids:
                    continue
                try:
                    await telegram.answer_callback_query(callback_query["id"])
                    await handle_callback(telegram, zara, store, chat_id, data, pending)
                except Exception as e:
                    logger.error("Failed to handle callback %r: %s", data, sanitize_log_value(str(e)))
                continue

            message = update.get("message") or {}
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text")

            if chat_id is None or not text:
                continue
            if str(chat_id) not in config.tg_chat_ids:
                logger.warning("Ignoring message from unknown chat %s", chat_id)
                continue

            try:
                await handle_message(telegram, zara, store, monitor, health, config, chat_id, text, pending)
            except Exception as e:
                logger.error("Failed to handle message %r: %s", text, sanitize_log_value(str(e)))


async def check_loop(monitor: MonitorService, config: Config) -> None:
    while True:
        await monitor.check_once()
        await asyncio.sleep(config.interval)
