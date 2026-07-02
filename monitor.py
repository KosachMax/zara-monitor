"""
Zara stock monitor.

Product ID берём из URL:
  https://www.zara.com/ru/ru/blazer-p04544820.html?v1=347068588
                                                     ^^^^^^^^
                                                     PRODUCT_ID
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
    "https://www.zara.com/{country}/{lang}/product/{product_id}/extra-detail.json"
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


def parse_sizes(data: dict, target_size: str) -> tuple[bool, list[str]]:
    """
    Разбираем ответ Zara API.

    Структура ответа:
      { "detail": { "colors": [ { "sizes": [ { "name": "M", "availability": "in_stock" } ] } ] } }
    """
    available_sizes: list[str] = []

    # Некоторые эндпоинты оборачивают данные в "detail", некоторые нет
    detail = data.get("detail", data)
    colors = detail.get("colors", [])

    for color in colors:
        for size in color.get("sizes", []):
            name = size.get("name", "").strip().upper()
            status = size.get("availability", "").lower()
            if status in IN_STOCK_STATUSES and name:
                available_sizes.append(name)

    target = target_size.strip().upper()
    return target in available_sizes, available_sizes


async def fetch_availability(
    client: httpx.AsyncClient,
    product_id: str,
    target_size: str,
    country: str,
    lang: str,
) -> tuple[bool, list[str]]:
    url = PRODUCT_API_URL.format(country=country, lang=lang, product_id=product_id)

    try:
        response = await client.get(url, headers=HEADERS, timeout=15.0)
        response.raise_for_status()
        return parse_sizes(response.json(), target_size)

    except httpx.HTTPStatusError as e:
        # 403 — Zara заблокировала запрос (нужны куки / другой User-Agent)
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
    target_size = os.environ["ZARA_SIZE"].strip().upper()
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    country = os.environ.get("ZARA_COUNTRY", "ru")
    lang = os.environ.get("ZARA_LANG", "ru")
    interval = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
    product_name = os.environ.get("ZARA_PRODUCT_NAME", f"article {product_id}")

    product_url = f"https://www.zara.com/{country}/{lang}/product/{product_id}"

    logger.info(
        f"Monitor started | product={product_id} ({product_name}) | "
        f"size={target_size} | interval={interval}s"
    )

    last_state: bool | None = None
    consecutive_errors = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                is_available, available_sizes = await fetch_availability(
                    client, product_id, target_size, country, lang
                )
                consecutive_errors = 0

                status_emoji = "✅" if is_available else "❌"
                logger.info(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"{target_size}: {status_emoji} | "
                    f"in stock: {', '.join(available_sizes) or 'none'}"
                )

                # Уведомляем при смене состояния out→in или при старте если уже есть
                should_notify = is_available and (last_state is False or last_state is None)

                if should_notify:
                    prefix = "уже в наличии" if last_state is None else "появился"
                    message = (
                        f"🛍 <b>Zara: размер {prefix}!</b>\n\n"
                        f"📦 {product_name}\n"
                        f"📐 Размер: <b>{target_size}</b>\n"
                        f"🔗 {product_url}\n\n"
                        f"✅ Доступные: {', '.join(available_sizes)}"
                    )
                    await notify_telegram(tg_token, tg_chat_id, message)

                last_state = is_available

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
