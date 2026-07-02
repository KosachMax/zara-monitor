"""
Zara stock monitor.

Product ID берём из URL:
  https://www.zara.com/ru/ru/blazer-p04544820.html?v1=347068588
                                                     ^^^^^^^^
                                                     PRODUCT_ID

Store ID и locale — числовой идентификатор магазина/сайта Zara (не путать
со страной сайта: с 2022 zara.ru не существует). Смотрятся сниффингом
трафика приложения/сайта Zara — см. SNIFFING.md.

ВНИМАНИЕ: сейчас это Фаза A — просто проверяем, что API вообще доступен
и стабильно отдаёт данные. Сверка с конкретным целевым размером (ZARA_SIZE)
временно отключена, монитор просто логирует и шлёт в Telegram полный список
размеров с их статусом наличия.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

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


def parse_sizes(data: dict) -> list[dict]:
    """
    Разбираем ответ Zara itxrest API.

    Структура ответа:
      { "detail": { "colors": [ { "sizes": [
          { "id": 18, "name": "1½ years (86 cm)", "shortName": "1½ y", "availability": "out_of_stock" }
      ] } ] } }

    Возвращает список всех размеров товара с их статусом наличия
    (без сверки с каким-либо конкретным целевым размером — это Фаза B).
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

    try:
        response = await client.get(
            url, headers=HEADERS, params={"locale": locale}, timeout=15.0
        )
        response.raise_for_status()
        return parse_sizes(response.json())

    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP {e.response.status_code} — {url}")
        raise

    except httpx.RequestError as e:
        logger.warning(f"Network error: {e}")
        raise


async def notify_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=10.0)
        resp.raise_for_status()

    logger.info("Telegram notification sent ✓")


async def main() -> None:
    # --- Config from env ---
    product_id = os.environ["ZARA_PRODUCT_ID"]
    store_id = os.environ["ZARA_STORE_ID"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    locale = os.environ.get("ZARA_LOCALE", "en_GB")
    interval = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
    product_name = os.environ.get("ZARA_PRODUCT_NAME", f"article {product_id}")

    product_url = f"https://www.zara.com/product/id/{product_id}"

    logger.info(
        f"Monitor started (Phase A: connectivity check only) | "
        f"product={product_id} ({product_name}) | store={store_id} | "
        f"interval={interval}s"
    )

    last_sizes: list[dict] | None = None
    consecutive_errors = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                sizes = await fetch_sizes(client, product_id, store_id, locale)
                consecutive_errors = 0

                summary = ", ".join(
                    f"{s['shortName'] or s['name']}: {s['availability']}" for s in sizes
                )
                logger.info(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"sizes: {summary or 'none returned'}"
                )

                if sizes != last_sizes:
                    message = (
                        f"🛍 <b>Zara Monitor: список размеров</b>\n\n"
                        f"📦 {product_name}\n"
                        f"🔗 {product_url}\n\n"
                        + "\n".join(
                            f"• {s['shortName'] or s['name']}: {s['availability']}"
                            for s in sizes
                        )
                    )
                    await notify_telegram(tg_token, tg_chat_id, message)

                last_sizes = sizes

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Check failed ({consecutive_errors}x): {e}")

                # После 5 ошибок подряд — уведомить в TG
                if consecutive_errors == 5:
                    try:
                        await notify_telegram(
                            tg_token,
                            tg_chat_id,
                            f"⚠️ <b>Zara Monitor: ошибки</b>\n"
                            f"5 неудачных попыток подряд.\n"
                            f"Возможно, Zara блокирует запросы.\n"
                            f"Проверь логи контейнера.",
                        )
                    except Exception:
                        pass

            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
