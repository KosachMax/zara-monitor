"""
Zara stock monitor, управляемый через Telegram.

Товары добавляются командой /add <артикул> прямо в чате — сниффинг трафика
нужен был только один раз, чтобы найти формат API (см. SNIFFING.md).
Артикул (product_id) виден в URL товара:
  https://www.zara.com/ru/ru/blazer-p04544820.html?v1=514777031
                                                     ^^^^^^^^
                                                     PRODUCT_ID

Store ID (магазин/регион, например 11734) — фиксирован для одного сайта Zara
и задаётся один раз в .env, менять его для каждого нового товара не нужно.

Управление — через кнопки меню в чате (➕ Добавить / ➖ Удалить / 📋 Список),
плюс те же действия командами:
  /add <артикул>     — показать размеры товара и предложить выбрать целевой
  /remove <артикул>  — снять товар (все выбранные для него размеры) с мониторинга
  /list              — показать все отслеживаемые товары и их текущий статус

Товар можно добавить и шерингом прямо из приложения Zara (Поделиться →
Telegram → этот бот) — бот сам узнаёт ссылку, достаёт из неё артикул
и спрашивает подтверждение перед тем как показать размеры.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

PRODUCT_API_URL = (
    "https://www.zara.com/itxrest/4/catalog/store/{store_id}/product/id/{product_id}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.zara.com/",
}

# Zara uses these strings to signal availability
IN_STOCK_STATUSES = {"in_stock", "low_on_stock", "available"}

DATA_FILE = Path(os.environ.get("DATA_FILE", "/app/data/products.json"))

# Пауза между запросами к Zara внутри одного цикла проверки — чтобы не долбить
# API пачкой запросов подряд, когда товаров в списке много.
REQUEST_DELAY_SEC = float(os.environ.get("REQUEST_DELAY_SEC", "1"))

BTN_ADD = "➕ Добавить"
BTN_REMOVE = "➖ Удалить"
BTN_LIST = "📋 Список"

MAIN_MENU_KEYBOARD = {
    "keyboard": [[{"text": BTN_ADD}, {"text": BTN_LIST}, {"text": BTN_REMOVE}]],
    "resize_keyboard": True,
}


def parse_sizes(data: dict) -> list[dict]:
    """
    Разбираем ответ Zara itxrest API.

    Структура ответа:
      { "detail": { "colors": [ { "sizes": [
          { "id": 18, "name": "1½ years (86 cm)", "shortName": "1½ y", "availability": "out_of_stock" }
      ] } ] } }
    """
    sizes: list[dict] = []

    detail = data.get("detail", data)
    colors = detail.get("colors", [])

    for color in colors:
        for size in color.get("sizes", []):
            sizes.append(
                {
                    "id": size.get("id"),
                    "name": size.get("name", ""),
                    "shortName": size.get("shortName", ""),
                    "availability": size.get("availability", "").lower(),
                }
            )

    return sizes


def extract_image_url(data: dict) -> str | None:
    """Достаём URL первой фотографии товара (первый цвет, первое изображение)."""
    detail = data.get("detail", data)
    colors = detail.get("colors", [])
    if not colors:
        return None

    xmedia = colors[0].get("xmedia", [])
    if not xmedia:
        return None

    return xmedia[0].get("extraInfo", {}).get("deliveryUrl")


async def fetch_product(
    client: httpx.AsyncClient,
    product_id: str,
    store_id: str,
    locale: str,
) -> tuple[str, str | None, list[dict]]:
    url = PRODUCT_API_URL.format(store_id=store_id, product_id=product_id)

    response = await client.get(
        url, headers=HEADERS, params={"locale": locale}, timeout=15.0
    )
    response.raise_for_status()
    data = response.json()
    return data.get("name", ""), extract_image_url(data), parse_sizes(data)


async def fetch_sizes(
    client: httpx.AsyncClient,
    product_id: str,
    store_id: str,
    locale: str,
) -> list[dict]:
    _, _, sizes = await fetch_product(client, product_id, store_id, locale)
    return sizes


# Артикул (product_id) в ссылках на товар Zara встречается либо как
# query-параметр v1, либо в самом пути itxrest-эндпоинта /product/id/<id>.
PRODUCT_URL_ID_PATTERNS = [
    re.compile(r"[?&]v1=(\d+)"),
    re.compile(r"/product/id/(\d+)"),
]


def find_zara_url(text: str) -> str | None:
    url_match = re.search(r"https?://\S+", text)
    if url_match and "zara.com" in url_match.group(0):
        return url_match.group(0)
    return None


def extract_product_id_from_url(url: str) -> str | None:
    for pattern in PRODUCT_URL_ID_PATTERNS:
        id_match = pattern.search(url)
        if id_match:
            return id_match.group(1)
    return None


def load_products() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read {DATA_FILE}, starting with empty list: {e}")
        return []


def save_products(products: list[dict]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(products, ensure_ascii=False, indent=2))


class ProductStore:
    """Список отслеживаемых товаров с сохранением в JSON-файл на диске."""

    def __init__(self) -> None:
        self.items: list[dict] = load_products()
        self.lock = asyncio.Lock()

    async def add(self, item: dict) -> None:
        async with self.lock:
            self.items.append(item)
            save_products(self.items)

    async def remove_all(self, product_id: str) -> list[dict]:
        async with self.lock:
            removed = [i for i in self.items if i["product_id"] == product_id]
            self.items = [i for i in self.items if i["product_id"] != product_id]
            save_products(self.items)
            return removed

    async def remove_one(self, product_id: str, target_size_id) -> dict | None:
        async with self.lock:
            for i, item in enumerate(self.items):
                if item["product_id"] == product_id and item["target_size_id"] == target_size_id:
                    removed = self.items.pop(i)
                    save_products(self.items)
                    return removed
            return None

    async def snapshot(self) -> list[dict]:
        async with self.lock:
            return list(self.items)

    async def set_availability(
        self, product_id: str, target_size_id, is_available: bool
    ) -> None:
        async with self.lock:
            for item in self.items:
                if (
                    item["product_id"] == product_id
                    and item["target_size_id"] == target_size_id
                ):
                    item["last_available"] = is_available
            save_products(self.items)


async def send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = await client.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()


async def send_photo(
    client: httpx.AsyncClient,
    token: str,
    chat_id,
    photo_url: str,
    caption: str,
    reply_markup: dict | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = await client.post(url, json=payload, timeout=15.0)
    resp.raise_for_status()


async def send_product_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id,
    text: str,
    image_url: str | None = None,
    reply_markup: dict | None = None,
) -> None:
    """Сообщение о конкретном товаре — с фото, если оно есть, иначе обычный текст."""
    if image_url:
        try:
            await send_photo(client, token, chat_id, image_url, text, reply_markup)
            return
        except Exception as e:
            logger.warning(f"sendPhoto failed, falling back to text: {e}")

    await send_message(client, token, chat_id, text, reply_markup)


async def answer_callback_query(client: httpx.AsyncClient, token: str, callback_query_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    resp = await client.post(url, json={"callback_query_id": callback_query_id}, timeout=10.0)
    resp.raise_for_status()


def size_label(size: dict) -> str:
    return size["shortName"] or size["name"]


def product_page_url(product_id: str) -> str:
    # Zara редиректит на канонический SEO-URL сама, достаточно правильного v1
    return f"https://www.zara.com/us/en/-p.html?v1={product_id}"


def sizes_inline_keyboard(sizes: list[dict]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"{size_label(s)} — {s['availability']}", "callback_data": f"size:{i}"}]
            for i, s in enumerate(sizes)
        ]
    }


def removable_items_inline_keyboard(items: list[dict]) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{i.get('product_name') or i['product_id']} / {i['target_size_label']}",
                    "callback_data": f"remove:{i['product_id']}:{i['target_size_id']}",
                }
            ]
            for i in items
        ]
    }


async def send_add_prompt(client: httpx.AsyncClient, config: "Config", chat_id, pending: dict) -> None:
    pending[chat_id] = {"stage": "awaiting_id"}
    await send_message(client, config.tg_token, chat_id, "Пришли артикул товара (число из URL на zara.com).")


async def start_add_flow(
    client: httpx.AsyncClient, config: "Config", chat_id, product_id: str, pending: dict
) -> None:
    if not product_id.isdigit():
        await send_message(client, config.tg_token, chat_id, "Артикул должен быть числом. Попробуй ещё раз.")
        return

    try:
        name, image_url, sizes = await fetch_product(client, product_id, config.store_id, config.locale)
    except Exception as e:
        pending.pop(chat_id, None)
        await send_message(
            client, config.tg_token, chat_id, f"Не удалось получить товар {product_id}: {e}"
        )
        return

    if not sizes:
        pending.pop(chat_id, None)
        await send_message(client, config.tg_token, chat_id, f"У товара {product_id} не нашлось размеров.")
        return

    pending[chat_id] = {
        "stage": "choosing_size",
        "product_id": product_id,
        "product_name": name,
        "product_image": image_url,
        "sizes": sizes,
    }
    await send_product_message(
        client, config.tg_token, chat_id,
        f"{name or f'артикул {product_id}'}. Выбери размер:",
        image_url=image_url,
        reply_markup=sizes_inline_keyboard(sizes),
    )


async def send_remove_prompt(
    client: httpx.AsyncClient, store: ProductStore, config: "Config", chat_id
) -> None:
    items = await store.snapshot()
    if not items:
        await send_message(
            client, config.tg_token, chat_id, "Список пуст, нечего удалять.", reply_markup=MAIN_MENU_KEYBOARD
        )
        return
    await send_message(
        client, config.tg_token, chat_id,
        "Что убрать из мониторинга?",
        reply_markup=removable_items_inline_keyboard(items),
    )


def format_waitlist(items: list[dict]) -> str:
    lines = [f"<b>Список ожидания · {len(items)} товаров</b>"]
    for item in items:
        name = (item.get("product_name") or item["product_id"]).title()
        size = item["target_size_label"]
        if item.get("last_available"):
            status = f"<b><i>↳ {size} · ✅ появился!</i></b>"
        else:
            status = f"<i>↳ {size} · ❌ нет в наличии</i>"
        lines.append(f"\n<b>{name}</b>\n{status}")
    return "\n".join(lines)


async def send_list(client: httpx.AsyncClient, store: ProductStore, config: "Config", chat_id) -> None:
    items = await store.snapshot()
    if not items:
        await send_message(
            client, config.tg_token, chat_id, "Список пуст. Нажми ➕ Добавить, чтобы начать.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    await send_message(
        client, config.tg_token, chat_id, format_waitlist(items),
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def handle_message(
    client: httpx.AsyncClient,
    store: ProductStore,
    config: "Config",
    chat_id,
    text: str,
    pending: dict,
) -> None:
    text = text.strip()

    if text in ("/start", "/menu"):
        pending.pop(chat_id, None)
        await send_message(
            client, config.tg_token, chat_id,
            "Zara Stock Monitor. Выбери действие:",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Пришла ссылка на товар (например, шеринг из приложения Zara)
    zara_url = find_zara_url(text)
    if zara_url:
        shared_product_id = extract_product_id_from_url(zara_url)
        if not shared_product_id:
            await send_message(
                client, config.tg_token, chat_id,
                "Это похоже на ссылку Zara, но не удалось найти в ней артикул. "
                "Пришли артикул числом или нажми ➕ Добавить.",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
            return

        try:
            name, image_url, sizes = await fetch_product(
                client, shared_product_id, config.store_id, config.locale
            )
        except Exception as e:
            await send_message(
                client, config.tg_token, chat_id,
                f"Не удалось получить товар {shared_product_id}: {e}",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
            return

        pending[chat_id] = {
            "stage": "confirm_add",
            "product_id": shared_product_id,
            "product_name": name,
            "product_image": image_url,
            "sizes": sizes,
        }
        await send_product_message(
            client, config.tg_token, chat_id,
            f"Похоже на товар Zara: <b>{name or f'артикул {shared_product_id}'}</b>.\n"
            f"Добавить его для мониторинга?",
            image_url=image_url,
            reply_markup={
                "inline_keyboard": [[
                    {"text": "✅ Да", "callback_data": f"confirm_add:{shared_product_id}"},
                    {"text": "❌ Нет", "callback_data": "confirm_add:no"},
                ]]
            },
        )
        return

    if text == BTN_ADD:
        await send_add_prompt(client, config, chat_id, pending)
        return

    if text.startswith("/add"):
        parts = text.split(maxsplit=1)
        product_id = parts[1].strip() if len(parts) > 1 else ""
        if not product_id:
            await send_add_prompt(client, config, chat_id, pending)
            return
        await start_add_flow(client, config, chat_id, product_id, pending)
        return

    if text == BTN_REMOVE:
        await send_remove_prompt(client, store, config, chat_id)
        return

    if text.startswith("/remove"):
        parts = text.split(maxsplit=1)
        product_id = parts[1].strip() if len(parts) > 1 else ""
        if not product_id:
            await send_remove_prompt(client, store, config, chat_id)
            return

        removed = await store.remove_all(product_id)
        if removed:
            await send_message(
                client, config.tg_token, chat_id,
                f"Товар {product_id} снят с мониторинга ({len(removed)} размер(ов)).",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
        else:
            await send_message(
                client, config.tg_token, chat_id, f"Товар {product_id} не отслеживался.",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
        return

    if text == BTN_LIST or text.startswith("/list"):
        await send_list(client, store, config, chat_id)
        return

    # Пользователь набрал артикул текстом после нажатия "➕ Добавить"
    if pending.get(chat_id, {}).get("stage") == "awaiting_id":
        await start_add_flow(client, config, chat_id, text, pending)
        return

    # Фолбэк: выбор размера текстом вместо нажатия инлайн-кнопки
    if pending.get(chat_id, {}).get("stage") == "choosing_size":
        info = pending.pop(chat_id)
        sizes = info["sizes"]
        chosen = None

        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(sizes):
                chosen = sizes[idx]

        if chosen is None:
            for s in sizes:
                if text.lower() in {s["shortName"].lower(), s["name"].lower()}:
                    chosen = s
                    break

        if chosen is None:
            await send_message(
                client, config.tg_token, chat_id,
                "Не понял выбор размера, добавление отменено. Начни заново.",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
            return

        await add_selected_size(
            client, store, config, chat_id, info["product_id"],
            info.get("product_name", ""), info.get("product_image"), chosen,
        )
        return

    await send_message(
        client, config.tg_token, chat_id,
        "Не понял. Используй кнопки меню ниже.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def add_selected_size(
    client: httpx.AsyncClient,
    store: ProductStore,
    config: "Config",
    chat_id,
    product_id: str,
    product_name: str,
    product_image: str | None,
    chosen: dict,
) -> None:
    is_available = chosen["availability"] in IN_STOCK_STATUSES
    item = {
        "product_id": product_id,
        "product_name": product_name,
        "product_image": product_image,
        "target_size_id": chosen["id"],
        "target_size_label": size_label(chosen),
        "last_available": is_available,
    }
    await store.add(item)
    label = product_name or f"артикул {product_id}"
    status = "уже в наличии ✅" if is_available else "пока нет в наличии, сообщу когда появится ❌"
    await send_product_message(
        client, config.tg_token, chat_id,
        f"Добавлено: {label} / {item['target_size_label']} — {status}",
        image_url=product_image,
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def handle_callback(
    client: httpx.AsyncClient,
    store: ProductStore,
    config: "Config",
    chat_id,
    data: str,
    pending: dict,
) -> None:
    if data.startswith("confirm_add:"):
        value = data.split(":", 1)[1]
        if value == "no":
            pending.pop(chat_id, None)
            await send_message(client, config.tg_token, chat_id, "Ок, не добавляю.", reply_markup=MAIN_MENU_KEYBOARD)
            return

        info = pending.get(chat_id)
        if not info or info.get("stage") != "confirm_add" or info.get("product_id") != value:
            # Кэш устарел или не совпал — просто заново пройдём флоу добавления
            await start_add_flow(client, config, chat_id, value, pending)
            return

        pending[chat_id] = {**info, "stage": "choosing_size"}
        name = info.get("product_name", "")
        image_url = info.get("product_image")
        await send_product_message(
            client, config.tg_token, chat_id,
            f"{name or f'артикул {value}'}. Выбери размер:",
            image_url=image_url,
            reply_markup=sizes_inline_keyboard(info["sizes"]),
        )
        return

    if data.startswith("size:"):
        info = pending.get(chat_id)
        if not info or info.get("stage") != "choosing_size":
            await send_message(client, config.tg_token, chat_id, "Этот выбор устарел, начни заново.")
            return
        idx = int(data.split(":", 1)[1])
        sizes = info["sizes"]
        if not (0 <= idx < len(sizes)):
            return
        pending.pop(chat_id, None)
        await add_selected_size(
            client, store, config, chat_id, info["product_id"],
            info.get("product_name", ""), info.get("product_image"), sizes[idx],
        )
        return

    if data.startswith("remove:"):
        _, product_id, target_size_id = data.split(":", 2)
        removed = await store.remove_one(product_id, int(target_size_id))
        if removed:
            label = removed.get("product_name") or product_id
            await send_message(
                client, config.tg_token, chat_id,
                f"Убрано: {label} / {removed['target_size_label']}",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
        else:
            await send_message(client, config.tg_token, chat_id, "Уже удалено.", reply_markup=MAIN_MENU_KEYBOARD)
        return


async def telegram_listener(client: httpx.AsyncClient, store: ProductStore, config: "Config") -> None:
    pending: dict = {}
    offset = None

    while True:
        try:
            url = f"https://api.telegram.org/bot{config.tg_token}/getUpdates"
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = await client.get(url, params=params, timeout=40.0)
            resp.raise_for_status()
            updates = resp.json()["result"]
        except Exception as e:
            logger.warning(f"getUpdates failed: {e}")
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
                    await answer_callback_query(client, config.tg_token, callback_query["id"])
                    await handle_callback(client, store, config, chat_id, data, pending)
                except Exception as e:
                    logger.error(f"Failed to handle callback {data!r}: {e}")
                continue

            message = update.get("message") or {}
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text")

            if chat_id is None or not text:
                continue
            if str(chat_id) not in config.tg_chat_ids:
                logger.warning(f"Ignoring message from unknown chat {chat_id}")
                continue

            try:
                await handle_message(client, store, config, chat_id, text, pending)
            except Exception as e:
                logger.error(f"Failed to handle message {text!r}: {e}")


async def check_loop(client: httpx.AsyncClient, store: ProductStore, config: "Config") -> None:
    while True:
        for item in await store.snapshot():
            try:
                sizes = await fetch_sizes(client, item["product_id"], config.store_id, config.locale)
            except Exception as e:
                logger.error(f"Check failed for {item['product_id']}: {e}")
                await asyncio.sleep(REQUEST_DELAY_SEC)
                continue

            target = next((s for s in sizes if s["id"] == item["target_size_id"]), None)
            is_available = bool(target) and target["availability"] in IN_STOCK_STATUSES
            was_available = item.get("last_available")

            logger.info(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{item['product_id']} / {item['target_size_label']}: "
                f"{'✅' if is_available else '❌'}"
            )

            if is_available and not was_available:
                label = item.get("product_name") or f"артикул {item['product_id']}"
                product_url = product_page_url(item["product_id"])
                for chat_id in config.tg_chat_ids:
                    await send_product_message(
                        client, config.tg_token, chat_id,
                        f"🛍 <b>Zara: размер появился!</b>\n\n"
                        f"📦 {label}\n"
                        f"📐 Размер: <b>{item['target_size_label']}</b>",
                        image_url=item.get("product_image"),
                        reply_markup={
                            "inline_keyboard": [[
                                {"text": "🔗 Открыть товар", "url": product_url},
                                {
                                    "text": "🔕 Отписаться",
                                    "callback_data": f"remove:{item['product_id']}:{item['target_size_id']}",
                                },
                            ]]
                        },
                    )

            await store.set_availability(item["product_id"], item["target_size_id"], is_available)
            await asyncio.sleep(REQUEST_DELAY_SEC)

        await asyncio.sleep(config.interval)


class Config:
    def __init__(self) -> None:
        self.tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.tg_chat_ids = {
            chat_id.strip()
            for chat_id in os.environ["TELEGRAM_CHAT_IDS"].split(",")
            if chat_id.strip()
        }
        self.store_id = os.environ["ZARA_STORE_ID"]
        self.locale = os.environ.get("ZARA_LOCALE", "en_GB")
        self.interval = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))


async def main() -> None:
    config = Config()
    store = ProductStore()

    logger.info(
        f"Monitor started | tracking {len(store.items)} item(s) | "
        f"store={config.store_id} | interval={config.interval}s"
    )

    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            telegram_listener(client, store, config),
            check_loop(client, store, config),
        )


if __name__ == "__main__":
    asyncio.run(main())
