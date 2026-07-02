from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import TelegramConflictError, TelegramError, TelegramRateLimitedError, TelegramRequestError

logger = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client = client
        self.token = token

    def api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def request(
        self,
        method: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        try:
            if json_payload is not None:
                response = await self.client.post(
                    self.api_url(method), json=json_payload, params=params, timeout=timeout
                )
            else:
                response = await self.client.get(self.api_url(method), params=params, timeout=timeout)
        except httpx.TimeoutException as e:
            raise TelegramRequestError(f"Telegram {method} timeout") from e
        except httpx.RequestError as e:
            raise TelegramRequestError(f"Telegram {method} request failed: {e.__class__.__name__}") from e

        if response.status_code == 409:
            raise TelegramConflictError("Telegram getUpdates conflict: another process is polling the same bot token")
        if response.status_code == 429:
            raise TelegramRateLimitedError(f"Telegram {method} rate limit exceeded")
        if response.status_code >= 400:
            raise TelegramRequestError(f"Telegram {method} failed: HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as e:
            raise TelegramRequestError(f"Telegram {method} returned invalid JSON") from e

        if not data.get("ok", False):
            description = data.get("description", "unknown Telegram API error")
            raise TelegramRequestError(f"Telegram {method} failed: {description}")

        return data

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset
        data = await self.request("getUpdates", params=params, timeout=40.0)
        return list(data.get("result", []))

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        data = await self.request("sendMessage", json_payload=payload, timeout=10.0)
        result = data.get("result")
        return result if isinstance(result, dict) else None

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.request("editMessageText", json_payload=payload, timeout=10.0)

    async def send_photo(
        self,
        chat_id: str | int,
        photo_url: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.request("sendPhoto", json_payload=payload, timeout=15.0)

    async def send_product_message(
        self,
        chat_id: str | int,
        text: str,
        image_url: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if image_url:
            try:
                await self.send_photo(chat_id, image_url, text, reply_markup)
                return
            except TelegramError as e:
                logger.warning("sendPhoto failed, falling back to text: %s", e)
        await self.send_message(chat_id, text, reply_markup)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        await self.request(
            "answerCallbackQuery",
            json_payload={"callback_query_id": callback_query_id},
            timeout=10.0,
        )

    async def set_my_commands(self) -> None:
        commands = [
            {"command": "start", "description": "Показать меню"},
            {"command": "add", "description": "Добавить товар Zara"},
            {"command": "list", "description": "Показать список ожидания"},
            {"command": "remove", "description": "Удалить товар из мониторинга"},
            {"command": "check_now", "description": "Проверить товары сейчас"},
            {"command": "status", "description": "Показать статус мониторинга"},
            {"command": "cancel", "description": "Отменить текущее действие"},
            {"command": "help", "description": "Справка"},
        ]
        await self.request("setMyCommands", json_payload={"commands": commands}, timeout=10.0)
