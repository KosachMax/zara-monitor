from __future__ import annotations

import logging
import re
import sys

TELEGRAM_TOKEN_PATTERN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
RAW_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def sanitize_log_value(value: object) -> object:
    """Mask Telegram bot tokens in log messages/exceptions."""
    if not isinstance(value, str):
        return value
    value = TELEGRAM_TOKEN_PATTERN.sub("bot<redacted>", value)
    return RAW_TELEGRAM_TOKEN_PATTERN.sub("<telegram-token-redacted>", value)


class SecretLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_log_value(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: sanitize_log_value(value) for key, value in record.args.items()}
            else:
                record.args = tuple(sanitize_log_value(arg) for arg in record.args)
        return True


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    for handler in logging.getLogger().handlers:
        if not any(isinstance(existing_filter, SecretLogFilter) for existing_filter in handler.filters):
            handler.addFilter(SecretLogFilter())

    # httpx INFO logs include full request URLs. Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


setup_logging()
