from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigError


@dataclass(slots=True)
class Config:
    tg_token: str
    tg_chat_ids: list[str]
    store_id: str
    locale: str
    interval: int
    health_error_threshold: int
    max_tracked_items: int
    telegram_conflict_exit_threshold: int

    @property
    def default_chat_id(self) -> str:
        return self.tg_chat_ids[0]

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            tg_token=required_env("TELEGRAM_BOT_TOKEN"),
            tg_chat_ids=parse_chat_ids(required_env("TELEGRAM_CHAT_IDS")),
            store_id=required_env("ZARA_STORE_ID"),
            locale=os.environ.get("ZARA_LOCALE", "en_GB"),
            interval=parse_int_env("CHECK_INTERVAL_SEC", 300, minimum=1),
            health_error_threshold=parse_int_env("HEALTH_ERROR_THRESHOLD", 3, minimum=1),
            max_tracked_items=parse_int_env("MAX_TRACKED_ITEMS", 200, minimum=1),
            telegram_conflict_exit_threshold=parse_int_env("TELEGRAM_CONFLICT_EXIT_THRESHOLD", 5, minimum=1),
        )


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def parse_chat_ids(value: str) -> list[str]:
    chat_ids = [chat_id.strip() for chat_id in value.split(",") if chat_id.strip()]
    if not chat_ids:
        raise ConfigError("TELEGRAM_CHAT_IDS must contain at least one chat id")
    return chat_ids


def parse_int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw_value!r}") from e
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value
