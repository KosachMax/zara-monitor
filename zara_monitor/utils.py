from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

from .constants import PRODUCT_URL_ID_PATTERNS


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def html_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


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


def size_label(size: dict[str, Any]) -> str:
    return str(size.get("shortName") or size.get("name") or size.get("id") or "Размер")


def product_page_url(product_id: str) -> str:
    # Zara redirects to the canonical SEO URL. A valid v1 is enough.
    return f"https://www.zara.com/bg/en/-p.html?v1={product_id}"
