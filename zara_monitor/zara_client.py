from __future__ import annotations

from typing import Any

import httpx

from .constants import DEFAULT_COLOR_ID, HEADERS, PRODUCT_API_URL
from .errors import ZaraProductNotFound, ZaraRateLimited, ZaraRequestError, ZaraTemporaryError


def normalize_size(size: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(size.get("id")),
        "name": str(size.get("name") or ""),
        "shortName": str(size.get("shortName") or ""),
        "availability": str(size.get("availability") or "").lower(),
    }


def extract_image_url_from_color(color: dict[str, Any]) -> str | None:
    for media in color.get("xmedia", []) or []:
        extra_info = media.get("extraInfo", {}) or {}
        delivery_url = extra_info.get("deliveryUrl")
        if delivery_url:
            return str(delivery_url)
        url = media.get("url")
        if url:
            return str(url)
    return None


def parse_colors(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Zara colors with their own size lists.

    The older implementation flattened all sizes across colors. For correct
    subscriptions we keep color boundaries and deduplicate by product/color/size.
    """
    detail = data.get("detail", data) or {}
    raw_colors = detail.get("colors", []) or []
    parsed_colors: list[dict[str, Any]] = []

    for index, color in enumerate(raw_colors):
        color_id = color.get("id") or color.get("reference") or color.get("name") or index
        color_name = color.get("name") or color.get("colorName") or f"Цвет {index + 1}"
        parsed_colors.append(
            {
                "id": str(color_id),
                "name": str(color_name),
                "image_url": extract_image_url_from_color(color),
                "sizes": [normalize_size(size) for size in color.get("sizes", []) or []],
            }
        )

    if not parsed_colors and detail.get("sizes"):
        parsed_colors.append(
            {
                "id": DEFAULT_COLOR_ID,
                "name": "",
                "image_url": None,
                "sizes": [normalize_size(size) for size in detail.get("sizes", []) or []],
            }
        )

    return parsed_colors


def parse_sizes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Backward-compatible helper: flatten all sizes across colors."""
    sizes: list[dict[str, Any]] = []
    for color in parse_colors(data):
        sizes.extend(color["sizes"])
    return sizes


def extract_image_url(data: dict[str, Any]) -> str | None:
    for color in parse_colors(data):
        if color.get("image_url"):
            return str(color["image_url"])
    return None


def product_image(product: dict[str, Any]) -> str | None:
    if product.get("image_url"):
        return str(product["image_url"])
    for color in product.get("colors", []):
        if color.get("image_url"):
            return str(color["image_url"])
    return None


class ZaraClient:
    def __init__(self, client: httpx.AsyncClient, store_id: str, locale: str) -> None:
        self.client = client
        self.store_id = store_id
        self.locale = locale

    async def fetch_product(self, product_id: str) -> dict[str, Any]:
        url = PRODUCT_API_URL.format(store_id=self.store_id, product_id=product_id)
        try:
            response = await self.client.get(
                url,
                headers=HEADERS,
                params={"locale": self.locale},
                timeout=15.0,
            )
        except httpx.TimeoutException as e:
            raise ZaraTemporaryError(f"Zara API timeout for product {product_id}") from e
        except httpx.RequestError as e:
            raise ZaraRequestError(f"Zara API request failed for product {product_id}: {e.__class__.__name__}") from e

        if response.status_code == 404:
            raise ZaraProductNotFound(f"Product {product_id} was not found in Zara API")
        if response.status_code == 429:
            raise ZaraRateLimited("Zara API rate limit exceeded")
        if response.status_code >= 500:
            raise ZaraTemporaryError(f"Zara API temporary error: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ZaraRequestError(f"Zara API error: HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as e:
            raise ZaraTemporaryError("Zara API returned invalid JSON") from e

        detail = data.get("detail", data) or {}
        name = data.get("name") or detail.get("name") or ""
        colors = parse_colors(data)
        return {
            "product_id": product_id,
            "name": str(name),
            "image_url": extract_image_url(data),
            "colors": colors,
        }
