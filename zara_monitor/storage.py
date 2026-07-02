from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from typing import Any

from .constants import DATA_FILE, DEFAULT_COLOR_ID, STATE_SCHEMA_VERSION
from .errors import MaxTrackedItemsError, StorageCorruptedError, StorageError, StorageValidationError
from .utils import now_iso

logger = logging.getLogger(__name__)


def normalize_subscription(item: dict[str, Any], default_chat_id: str, *, force_new_id: bool = False) -> dict[str, Any]:
    if "product_id" not in item:
        raise StorageValidationError("Subscription is missing product_id")
    if "target_size_id" not in item:
        raise StorageValidationError(f"Subscription {item.get('product_id')} is missing target_size_id")

    product_id = str(item["product_id"])
    target_size_id = str(item["target_size_id"])
    created_at = str(item.get("created_at") or now_iso())
    color_id = str(item.get("color_id") or DEFAULT_COLOR_ID)

    return {
        "id": str(uuid.uuid4().hex if force_new_id else item.get("id") or uuid.uuid4().hex),
        "chat_id": str(item.get("chat_id") or default_chat_id),
        "product_id": product_id,
        "product_name": str(item.get("product_name") or ""),
        "product_image": item.get("product_image"),
        "color_id": color_id,
        "color_name": str(item.get("color_name") or ""),
        "color_image": item.get("color_image") or item.get("product_image"),
        "target_size_id": target_size_id,
        "target_size_label": str(item.get("target_size_label") or target_size_id),
        "last_available": bool(item.get("last_available", False)),
        "last_checked_at": item.get("last_checked_at"),
        "last_error": item.get("last_error"),
        "created_at": created_at,
        "updated_at": str(item.get("updated_at") or created_at),
    }


def dedupe_subscriptions(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for item in items:
        key = subscription_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped, len(items) - len(deduped)


def load_state(chat_ids: list[str]) -> tuple[list[dict[str, Any]], bool]:
    if not chat_ids:
        raise StorageValidationError("At least one chat id is required to load state")
    default_chat_id = chat_ids[0]
    if not DATA_FILE.exists():
        return [], False
    if DATA_FILE.is_dir():
        raise StorageValidationError(f"{DATA_FILE} must be a JSON file, but it is a directory")

    try:
        raw_state = json.loads(DATA_FILE.read_text())
    except json.JSONDecodeError as e:
        raise StorageCorruptedError(f"Failed to parse {DATA_FILE}: {e}") from e
    except OSError as e:
        raise StorageError(f"Failed to read {DATA_FILE}: {e}") from e

    if isinstance(raw_state, list):
        normalized = [
            normalize_subscription(item, chat_id, force_new_id=len(chat_ids) > 1)
            for item in raw_state
            for chat_id in chat_ids
        ]
        deduped, removed_duplicates = dedupe_subscriptions(normalized)
        if removed_duplicates:
            logger.warning("Removed %s duplicate legacy subscription(s) during migration", removed_duplicates)
        return deduped, True

    if not isinstance(raw_state, dict):
        raise StorageValidationError(f"{DATA_FILE} must contain a JSON object or legacy list")

    schema_version = raw_state.get("schema_version")
    subscriptions = raw_state.get("subscriptions")
    if schema_version != STATE_SCHEMA_VERSION:
        logger.warning("Unknown state schema version %r, attempting best-effort load", schema_version)
    if not isinstance(subscriptions, list):
        raise StorageValidationError(f"{DATA_FILE} must contain a subscriptions list")

    normalized = [normalize_subscription(item, default_chat_id) for item in subscriptions]
    deduped, removed_duplicates = dedupe_subscriptions(normalized)
    if removed_duplicates:
        logger.warning("Removed %s duplicate subscription(s) while loading state", removed_duplicates)
    needs_save = deduped != subscriptions or schema_version != STATE_SCHEMA_VERSION
    return deduped, needs_save


def save_state(items: list[dict[str, Any]]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists() and not DATA_FILE.is_file():
        raise StorageValidationError(f"{DATA_FILE} must be a file, but it is not")

    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "subscriptions": items,
    }
    tmp_path = DATA_FILE.with_name(f"{DATA_FILE.name}.tmp")
    backup_path = DATA_FILE.with_name(f"{DATA_FILE.name}.bak")
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"

    try:
        if DATA_FILE.exists():
            shutil.copy2(DATA_FILE, backup_path)
        tmp_path.write_text(payload)
        os.replace(tmp_path, DATA_FILE)
    except OSError as e:
        raise StorageError(f"Failed to save {DATA_FILE}: {e}") from e


class ProductStore:
    """Persistent subscription store backed by a schema-versioned JSON file.

    When `tolerate_load_error` is true, startup storage errors do not crash the
    worker. The store becomes read-only with an empty in-memory list so Telegram
    can still report the problem instead of silently bricking the service.
    """

    def __init__(self, chat_ids: list[str], max_items: int, *, tolerate_load_error: bool = False) -> None:
        self.max_items = max_items
        self.lock = asyncio.Lock()
        self.load_error: StorageError | None = None

        try:
            self.items, needs_save = load_state(chat_ids)
            if needs_save:
                logger.info("Migrating state file to schema v%s", STATE_SCHEMA_VERSION)
                save_state(self.items)
        except StorageError as e:
            if not tolerate_load_error:
                raise
            self.items = []
            self.load_error = e
            logger.error("Storage is unavailable, running in read-only degraded mode: %s", e)

    @property
    def is_available(self) -> bool:
        return self.load_error is None

    def ensure_available(self) -> None:
        if self.load_error is not None:
            raise StorageError(f"Storage is unavailable: {self.load_error}")

    async def add(self, item: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        self.ensure_available()
        async with self.lock:
            existing = self.find_duplicate_locked(item)
            if existing is not None:
                return False, existing
            if len(self.items) >= self.max_items:
                raise MaxTrackedItemsError(f"Cannot track more than {self.max_items} subscriptions")
            item = {**item, "id": item.get("id") or uuid.uuid4().hex, "created_at": now_iso(), "updated_at": now_iso()}
            self.items.append(item)
            save_state(self.items)
            return True, None

    def find_duplicate_locked(self, item: dict[str, Any]) -> dict[str, Any] | None:
        for existing in self.items:
            if subscription_key(existing) == subscription_key(item):
                return existing
        return None

    async def remove_all(self, chat_id: str, product_id: str) -> list[dict[str, Any]]:
        self.ensure_available()
        async with self.lock:
            removed = [
                item for item in self.items if item["chat_id"] == str(chat_id) and item["product_id"] == str(product_id)
            ]
            if not removed:
                return []
            self.items = [item for item in self.items if item not in removed]
            save_state(self.items)
            return removed

    async def remove_by_id(self, chat_id: str, subscription_id: str) -> dict[str, Any] | None:
        self.ensure_available()
        async with self.lock:
            for index, item in enumerate(self.items):
                if item["id"] == subscription_id and item["chat_id"] == str(chat_id):
                    removed = self.items.pop(index)
                    save_state(self.items)
                    return removed
            return None

    async def remove_legacy(self, chat_id: str, product_id: str, target_size_id: str) -> dict[str, Any] | None:
        self.ensure_available()
        async with self.lock:
            for index, item in enumerate(self.items):
                if (
                    item["chat_id"] == str(chat_id)
                    and item["product_id"] == str(product_id)
                    and str(item["target_size_id"]) == str(target_size_id)
                ):
                    removed = self.items.pop(index)
                    save_state(self.items)
                    return removed
            return None

    async def snapshot(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            if chat_id is None:
                return [dict(item) for item in self.items]
            return [dict(item) for item in self.items if item["chat_id"] == str(chat_id)]

    async def set_check_result(
        self,
        subscription_id: str,
        *,
        is_available: bool,
        error: str | None = None,
    ) -> None:
        self.ensure_available()
        async with self.lock:
            for item in self.items:
                if item["id"] == subscription_id:
                    item["last_available"] = is_available
                    item["last_checked_at"] = now_iso()
                    item["last_error"] = error
                    item["updated_at"] = now_iso()
                    save_state(self.items)
                    return

    async def set_error(self, subscription_id: str, error: str) -> None:
        self.ensure_available()
        async with self.lock:
            for item in self.items:
                if item["id"] == subscription_id:
                    item["last_checked_at"] = now_iso()
                    item["last_error"] = error
                    item["updated_at"] = now_iso()
                    save_state(self.items)
                    return

    async def export_state(self, chat_id: str | None = None) -> str:
        items = await self.snapshot(chat_id)
        return json.dumps(
            {"schema_version": STATE_SCHEMA_VERSION, "subscriptions": items},
            ensure_ascii=False,
            indent=2,
        )


def subscription_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item["chat_id"]),
        str(item["product_id"]),
        str(item.get("color_id") or DEFAULT_COLOR_ID),
        str(item["target_size_id"]),
    )
