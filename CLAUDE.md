# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot (Python asyncio background worker, **no HTTP server**) that monitors Zara product/size availability and notifies subscribed chats when a tracked size comes into stock. Runs as a long-lived process on Fly.io, auto-deployed via GitHub Actions on push to `main`.

## Commands

Setup:
```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Lint / format (ruff, config in `pyproject.toml`: line-length 120, target py312):
```sh
ruff check .
ruff format --check .
```

Compile check (matches CI):
```sh
python -m compileall monitor.py zara_monitor tests
```

Tests:
```sh
pytest
pytest tests/test_monitor.py::test_name -v   # single test
```

Run locally against real Telegram/Zara:
```sh
docker compose up --build
```
Needs a `.env` with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `ZARA_STORE_ID` (see `.env.example`). State persists to `data/products.json` via a `./data` bind mount.

Deploy: push to `main` — `.github/workflows/fly-deploy.yml` runs the checks above, then `flyctl deploy --config fly.toml`. Manual deploy: `fly deploy --config fly.toml -a zara-monitor`.

## Architecture

`monitor.py` is a thin compatibility entrypoint (`from zara_monitor import *`); all real logic lives in `zara_monitor/`. `zara_monitor/app.py:main()` starts two asyncio tasks concurrently:

- `bot_controller.telegram_listener` — long-polls Telegram `getUpdates`, dispatches to `handle_message` / `handle_callback` for the add/remove/list flows (product id or color/size selection, pagination).
- `bot_controller.check_loop` → `MonitorService.check_once` — walks every subscription, fetches the product from Zara, compares availability, and sends a stock notification when it flips unavailable → available.

Key modules and the non-obvious reasons they're shaped this way:

- **`storage.py`** — `ProductStore`, a JSON-file-backed store (schema v2, `data/products.json` / Fly volume `/app/data`). Subscriptions are keyed by `chat_id + product_id + color_id + target_size_id` (`subscription_key`) — color is part of the key because Zara products can have multiple colors, each with its own size list. Handles migration from the old v1 flat-list schema (each legacy item is expanded into one subscription per chat id, then deduped), writes atomically with a `.bak` backup, and can start in a read-only "degraded mode" (`tolerate_load_error=True`) if the file is corrupted, so the bot can still tell you what's wrong over Telegram instead of crash-looping.
- **`zara_client.py`** — talks to `https://www.zara.com/itxrest/4/catalog/store/{store_id}/product/id/{product_id}`. This is not Zara's public web endpoint (that one 404s — see `SNIFFING.md` for how the real one was found); it needs a browser-like `User-Agent`/`Accept-Language` in `HEADERS` but no cookies/auth. Parses sizes **per color**, not flattened, which is why the storage key includes `color_id`.
- **`config.py`** — all env vars are read once via `Config.from_env()`; missing required vars raise `ConfigError` immediately at startup rather than failing later mid-loop.
- **`health.py`** — tracks consecutive failed check cycles and gates a one-shot degraded/recovery alert (`HEALTH_ERROR_THRESHOLD`) so a Zara outage sends one warning, not one per cycle.
- **`errors.py`** — typed exception hierarchy per external system (`ZaraError`, `TelegramError`, `StorageError` and their subclasses). Callers branch on these instead of catching generic `Exception`, so e.g. a `ZaraRateLimited` and a `TelegramConflictError` are handled differently.
- **`logging_config.py`** — every log handler gets a `SecretLogFilter` that regex-masks the Telegram bot token. `httpx`'s own INFO logging prints the full request URL, which contains the token for every Telegram API call, so `httpx`/`httpcore` loggers are also forced to WARNING.

Users can add a product via `/add <id>`, the menu button, or by sharing a Zara URL directly into the chat — `utils.find_zara_url` / `extract_product_id_from_url` pulls the id out of either the `v1=` query param or a `/product/id/<id>` path, whichever the shared link uses.

`fly.toml` intentionally has **no `[http_service]` block** — this is a background worker, and Fly's default HTTP-traffic-based autostop would otherwise kill the machine that's supposed to run 24/7 doing Telegram long-polling.

## Constraints worth knowing before changing things

- Telegram allows only one long-polling `getUpdates` consumer per bot token at a time. Running the bot locally (`docker compose up`) while the Fly deployment is also up causes `409 Conflict` (`TelegramConflictError`) on both sides — stop one before starting the other. Leftover/renamed Fly apps from earlier experiments are a real past cause of this (see `README.md` troubleshooting).
- `REQUEST_DELAY_SEC` (default 1s, in `constants.py`) paces requests to Zara *within* a single check cycle across all subscriptions. Removing it risks bursting the Zara API as the tracked-item list grows.
- `MAX_TRACKED_ITEMS` bounds storage growth — `ProductStore.add` raises `MaxTrackedItemsError` past the limit rather than silently dropping or truncating.
- Deeper design notes, the current/target architecture diagrams, and the roadmap live in `.claude/SYSTEM_DESIGN.md`; operational/runbook detail (Fly secrets, volume setup, troubleshooting recipes) is in `README.md`.

## Note on `.claude/code-review-rules.md`

That file is a generic multi-stack (Django/FastAPI monorepo) code review template unrelated to this project — it appears to have been copied in from elsewhere. Don't treat it as this repo's conventions.
