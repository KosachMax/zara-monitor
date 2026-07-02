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

Команды в Telegram:
  /add <артикул>     — показать размеры товара и предложить выбрать целевой
  /remove <артикул>  — снять товар (все выбранные для него размеры) с мониторинга
  /list              — показать все отслеживаемые товары и их текущий статус
"""

import asyncio
import json
import logging
import os
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


async def fetch_sizes(
    client: httpx.AsyncClient,
    product_id: str,
    store_id: str,
    locale: str,
) -> list[dict]:
    url = PRODUCT_API_URL.format(store_id=store_id, product_id=product_id)

    response = await client.get(
        url, headers=HEADERS, params={"locale": locale}, timeout=15.0
    )
    response.raise_for_status()
    return parse_sizes(response.json())


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

    async def remove(self, product_id: str) -> list[dict]:
        async with self.lock:
            removed = [i for i in self.items if i["product_id"] == product_id]
            self.items = [i for i in self.items if i["product_id"] != product_id]
            save_products(self.items)
            return removed

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


async def notify_telegram(client: httpx.AsyncClient, token: str, chat_id, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    resp = await client.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()


def format_size_choice(index: int, size: dict) -> str:
    label = size["shortName"] or size["name"]
    return f"{index}. {label} — {size['availability']}"


async def handle_message(
    client: httpx.AsyncClient,
    store: ProductStore,
    config: "Config",
    chat_id,
    text: str,
    pending: dict,
) -> None:
    text = text.strip()

    if text.startswith("/add"):
        parts = text.split(maxsplit=1)
        product_id = parts[1].strip() if len(parts) > 1 else ""
        if not product_id.isdigit():
            await notify_telegram(client, config.tg_token, chat_id, "Использование: /add <артикул>")
            return

        try:
            sizes = await fetch_sizes(client, product_id, config.store_id, config.locale)
        except Exception as e:
            await notify_telegram(
                client, config.tg_token, chat_id, f"Не удалось получить товар {product_id}: {e}"
            )
            return

        if not sizes:
            await notify_telegram(
                client, config.tg_token, chat_id, f"У товара {product_id} не нашлось размеров."
            )
            return

        pending[chat_id] = {"product_id": product_id, "sizes": sizes}
        lines = [format_size_choice(i + 1, s) for i, s in enumerate(sizes)]
        await notify_telegram(
            client,
            config.tg_token,
            chat_id,
            f"Товар {product_id}. Выбери размер (ответь номером или названием):\n\n"
            + "\n".join(lines),
        )
        return

    if text.startswith("/remove"):
        parts = text.split(maxsplit=1)
        product_id = parts[1].strip() if len(parts) > 1 else ""
        if not product_id:
            await notify_telegram(client, config.tg_token, chat_id, "Использование: /remove <артикул>")
            return

        removed = await store.remove(product_id)
        if removed:
            await notify_telegram(
                client, config.tg_token, chat_id,
                f"Товар {product_id} снят с мониторинга ({len(removed)} размер(ов)).",
            )
        else:
            await notify_telegram(client, config.tg_token, chat_id, f"Товар {product_id} не отслеживался.")
        return

    if text.startswith("/list"):
        items = await store.snapshot()
        if not items:
            await notify_telegram(client, config.tg_token, chat_id, "Список пуст. Добавь товар: /add <артикул>")
            return

        lines = [
            f"• {i['product_id']} / {i['target_size_label']}: "
            f"{'✅' if i.get('last_available') else '❌'}"
            for i in items
        ]
        await notify_telegram(
            client, config.tg_token, chat_id, "Отслеживаемые товары:\n\n" + "\n".join(lines)
        )
        return

    # Ответ на предыдущий /add — выбор размера из списка
    if chat_id in pending:
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
            await notify_telegram(
                client, config.tg_token, chat_id,
                "Не понял выбор размера, добавление отменено. Начни заново: /add <артикул>",
            )
            return

        is_available = chosen["availability"] in IN_STOCK_STATUSES
        item = {
            "product_id": info["product_id"],
            "target_size_id": chosen["id"],
            "target_size_label": chosen["shortName"] or chosen["name"],
            "last_available": is_available,
        }
        await store.add(item)
        status = "уже в наличии ✅" if is_available else "пока нет в наличии, сообщу когда появится ❌"
        await notify_telegram(
            client, config.tg_token, chat_id,
            f"Добавлено: {info['product_id']} / {item['target_size_label']} — {status}",
        )
        return

    await notify_telegram(
        client, config.tg_token, chat_id,
        "Команды:\n/add <артикул>\n/remove <артикул>\n/list",
    )


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
            message = update.get("message") or {}
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text")

            if chat_id is None or not text:
                continue
            if str(chat_id) != str(config.tg_chat_id):
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
                await notify_telegram(
                    client, config.tg_token, config.tg_chat_id,
                    f"🛍 <b>Zara: размер появился!</b>\n\n"
                    f"📦 Артикул {item['product_id']}\n"
                    f"📐 Размер: <b>{item['target_size_label']}</b>\n"
                    f"🔗 https://www.zara.com/product/id/{item['product_id']}",
                )

            await store.set_availability(item["product_id"], item["target_size_id"], is_available)

        await asyncio.sleep(config.interval)


class Config:
    def __init__(self) -> None:
        self.tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]
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
