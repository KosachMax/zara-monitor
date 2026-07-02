import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor  # noqa: E402
import zara_monitor.monitor_service as monitor_service_module  # noqa: E402
import zara_monitor.storage as storage_module  # noqa: E402


def test_extract_product_id_from_zara_urls():
    assert (
        monitor.extract_product_id_from_url("https://www.zara.com/ru/ru/blazer-p04544820.html?v1=514777031")
        == "514777031"
    )
    assert (
        monitor.extract_product_id_from_url("https://www.zara.com/itxrest/4/catalog/store/11734/product/id/543271392")
        == "543271392"
    )
    assert monitor.extract_product_id_from_url("https://www.zara.com/ru/ru/no-id.html") is None


def test_find_zara_url_in_shared_text():
    text = "Посмотри товар https://www.zara.com/ru/ru/item-p123.html?v1=500897896 спасибо"
    assert monitor.find_zara_url(text) == "https://www.zara.com/ru/ru/item-p123.html?v1=500897896"
    assert monitor.find_zara_url("https://example.com/item?v1=1") is None


def test_parse_colors_keeps_color_boundaries():
    data = {
        "name": "Test product",
        "detail": {
            "colors": [
                {
                    "id": 101,
                    "name": "Ecru",
                    "xmedia": [{"extraInfo": {"deliveryUrl": "https://example.com/ecru.jpg"}}],
                    "sizes": [{"id": 24, "name": "2 years", "shortName": "2 y", "availability": "out_of_stock"}],
                },
                {
                    "id": 202,
                    "name": "Blue",
                    "sizes": [{"id": 24, "name": "2 years", "shortName": "2 y", "availability": "in_stock"}],
                },
            ]
        },
    }

    colors = monitor.parse_colors(data)

    assert [color["id"] for color in colors] == ["101", "202"]
    assert colors[0]["name"] == "Ecru"
    assert colors[0]["image_url"] == "https://example.com/ecru.jpg"
    assert colors[0]["sizes"][0]["availability"] == "out_of_stock"
    assert colors[1]["sizes"][0]["availability"] == "in_stock"


def test_find_target_size_uses_color_id_when_present():
    product = {
        "colors": [
            {"id": "101", "sizes": [{"id": "24", "availability": "out_of_stock"}]},
            {"id": "202", "sizes": [{"id": "24", "availability": "in_stock"}]},
        ]
    }
    item = {"color_id": "202", "target_size_id": "24"}

    color, size = monitor.find_target_size(product, item)

    assert color["id"] == "202"
    assert size["availability"] == "in_stock"


def test_storage_migrates_legacy_list_to_all_chat_ids_and_deduplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_file = tmp_path / "products.json"
    legacy_item = {
        "product_id": "543271392",
        "product_name": "Sweatshirt",
        "target_size_id": 24,
        "target_size_label": "2 y",
        "last_available": False,
    }
    data_file.write_text(json.dumps([legacy_item, legacy_item]))
    monkeypatch.setattr(storage_module, "DATA_FILE", data_file)

    store = monitor.ProductStore(chat_ids=["111", "222"], max_items=10)

    async def scenario():
        assert len(await store.snapshot()) == 2

        for chat_id in ("111", "222"):
            items = await store.snapshot(chat_id)
            assert len(items) == 1
            assert items[0]["chat_id"] == chat_id
            assert items[0]["color_id"] == monitor.DEFAULT_COLOR_ID
            assert items[0]["target_size_id"] == "24"

        added, existing = await store.add(
            {
                "id": "new-id",
                "chat_id": "111",
                "product_id": "543271392",
                "product_name": "Sweatshirt",
                "product_image": None,
                "color_id": monitor.DEFAULT_COLOR_ID,
                "color_name": "",
                "color_image": None,
                "target_size_id": "24",
                "target_size_label": "2 y",
                "last_available": False,
                "last_checked_at": None,
                "last_error": None,
            }
        )
        assert added is False
        assert existing is not None
        assert len(await store.snapshot("111")) == 1

    asyncio.run(scenario())

    saved_state = json.loads(data_file.read_text())
    assert saved_state["schema_version"] == monitor.STATE_SCHEMA_VERSION
    assert len(saved_state["subscriptions"]) == 2


def test_storage_rejects_directory_at_data_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_file = tmp_path / "products.json"
    data_file.mkdir()
    monkeypatch.setattr(storage_module, "DATA_FILE", data_file)

    with pytest.raises(monitor.StorageValidationError):
        monitor.ProductStore(chat_ids=["123"], max_items=10)


def test_legacy_migrated_subscriptions_notify_all_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class FakeZara:
        async def fetch_product(self, product_id: str):
            return {
                "product_id": product_id,
                "name": "Sweatshirt",
                "image_url": None,
                "colors": [
                    {
                        "id": "color-1",
                        "name": "Ecru",
                        "image_url": None,
                        "sizes": [{"id": "24", "availability": "in_stock"}],
                    }
                ],
            }

    class FakeTelegram:
        def __init__(self):
            self.sent_chat_ids = []

        async def send_product_message(self, chat_id, text, image_url=None, reply_markup=None):
            self.sent_chat_ids.append(str(chat_id))

        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent_chat_ids.append(str(chat_id))

    data_file = tmp_path / "products.json"
    data_file.write_text(
        json.dumps(
            [
                {
                    "product_id": "543271392",
                    "product_name": "Sweatshirt",
                    "target_size_id": 24,
                    "target_size_label": "2 y",
                    "last_available": False,
                }
            ]
        )
    )
    monkeypatch.setattr(storage_module, "DATA_FILE", data_file)
    monkeypatch.setattr(monitor_service_module, "REQUEST_DELAY_SEC", 0)

    store = monitor.ProductStore(chat_ids=["111", "222"], max_items=10)
    telegram = FakeTelegram()
    config = monitor.Config(
        tg_token="token",
        tg_chat_ids=["111", "222"],
        store_id="11734",
        locale="en_GB",
        interval=300,
        health_error_threshold=3,
        max_tracked_items=10,
        telegram_conflict_exit_threshold=5,
    )
    service = monitor.MonitorService(FakeZara(), telegram, store, config, monitor.HealthMonitor(threshold=3))

    summary = asyncio.run(service.check_once())

    assert summary.notifications == 2
    assert telegram.sent_chat_ids == ["111", "222"]


def test_health_monitor_marks_degraded_only_after_alert_delivery():
    health = monitor.HealthMonitor(threshold=2)

    assert health.record_failure("first") is False
    assert health.state == "OK"
    assert health.record_failure("second") is True
    assert health.state == "OK"

    # If Telegram delivery fails, next failed cycle should request alert again.
    assert health.record_failure("third") is True
    assert health.state == "OK"

    health.mark_degraded_alert_sent()
    assert health.state == "DEGRADED"
    assert health.record_failure("fourth") is False
    assert health.record_success() is True
    assert health.state == "OK"


def test_log_sanitizer_masks_telegram_tokens():
    raw = "https://api.telegram.org/bot8709359650:AAHZHsnTA4o8DKwR6tofGMA7Eurynma2Aho/getUpdates"

    sanitized = monitor.sanitize_log_value(raw)

    assert "AAHZHsn" not in sanitized
    assert "bot<redacted>" in sanitized
