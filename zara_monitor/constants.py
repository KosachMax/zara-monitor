from __future__ import annotations

import os
import re
from pathlib import Path

PRODUCT_API_URL = "https://www.zara.com/itxrest/4/catalog/store/{store_id}/product/id/{product_id}"

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

IN_STOCK_STATUSES = {"in_stock", "low_on_stock", "available"}
DEFAULT_COLOR_ID = "default"
STATE_SCHEMA_VERSION = 2

DATA_FILE = Path(os.environ.get("DATA_FILE", "/app/data/products.json"))
REQUEST_DELAY_SEC = float(os.environ.get("REQUEST_DELAY_SEC", "1"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "10"))
CHECK_NOW_WATCH_POLL_SEC = float(os.environ.get("CHECK_NOW_WATCH_POLL_SEC", "3"))

BTN_ADD = "➕ Добавить"
BTN_REMOVE = "➖ Удалить"
BTN_LIST = "📋 Список"
BTN_CHECK_NOW = "🔄 Проверить"

CB_NOOP = "noop"
CB_CHECK_NOW = "check_now"

MAIN_MENU_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_ADD}, {"text": BTN_LIST}],
        [{"text": BTN_REMOVE}, {"text": BTN_CHECK_NOW}],
    ],
    "resize_keyboard": True,
}

PRODUCT_URL_ID_PATTERNS = [
    re.compile(r"[?&]v1=(\d+)"),
    re.compile(r"/product/id/(\d+)"),
]
