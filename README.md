# Zara Stock Monitor

Telegram-бот для мониторинга наличия выбранных размеров товаров Zara. Бот работает как фоновый worker: слушает Telegram через long polling, периодически опрашивает Zara API и отправляет уведомление, когда выбранный размер появляется в наличии.

## Возможности

- Добавление товара из Telegram:
  - `/add <product_id>`;
  - кнопка `➕ Добавить`;
  - пересылка/шеринг ссылки Zara в чат с ботом.
- Выбор цвета перед размером, если у товара несколько цветов.
- Выбор размера через inline-кнопки.
- Дедупликация подписок по `chat_id + product_id + color_id + size_id`.
- Удаление подписок через `/remove`, кнопку `➖ Удалить` и inline-кнопку в уведомлении.
- Просмотр текущего списка через `/list` или `📋 Список` с pagination.
- `/cancel`, `/check_now`, `/status`, `/help`, `/export`.
- One-shot health alerts при повторяющихся проблемах с проверками.
- Безопасное логирование без Telegram token в URL-логах.
- Хранение списка отслеживаемых товаров на диске в schema-versioned `data/products.json` / Fly volume `/app/data`.
- Деплой на Fly.io через GitHub Actions при push в `main`.

## Архитектура в двух словах

```text
Telegram User -> Telegram Bot API -> Python worker -> Zara itxrest API
                                      |
                                      v
                              persistent storage
                              /app/data/products.json
```

Worker не поднимает HTTP-сервер. Поэтому в `fly.toml` нет `[http_service]`: приложение не должно засыпать из-за отсутствия HTTP-трафика.

Подробный дизайн, roadmap и диаграммы лежат в [`./.claude/SYSTEM_DESIGN.md`](./.claude/SYSTEM_DESIGN.md).

## Команды Telegram

| Команда | Что делает |
|---|---|
| `/start` / `/menu` | Показывает главное меню. |
| `/help` | Показывает справку по командам. |
| `/add <product_id>` | Загружает товар из Zara, при необходимости предлагает выбрать цвет, затем размер. |
| `/remove` | Открывает paginated-список подписок для удаления. |
| `/remove <product_id>` | Удаляет все отслеживаемые размеры товара в текущем chat. |
| `/list` | Показывает paginated-список ожидания текущего chat. |
| `/cancel` | Отменяет текущий сценарий добавления/выбора. |
| `/check_now` | Запускает внеочередную проверку товаров. |
| `/status` | Показывает состояние worker-а, storage и последних проверок. |
| `/export` | Выгружает JSON текущих подписок chat-а, если он помещается в одно Telegram-сообщение. |

## Product ID Zara

Product ID обычно находится в ссылке товара в query-параметре `v1`:

```text
https://www.zara.com/ru/ru/blazer-p04544820.html?v1=514777031
                                                     ^^^^^^^^^
                                                     product_id
```

Также бот умеет извлекать id из API-ссылок формата:

```text
/product/id/<product_id>
```

## Переменные окружения

Обязательные:

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота от `@BotFather`. |
| `TELEGRAM_CHAT_IDS` | Разрешённые chat id через запятую. |
| `ZARA_STORE_ID` | Store id Zara для выбранного региона. |

Опциональные:

| Переменная | Значение по умолчанию | Описание |
|---|---:|---|
| `ZARA_LOCALE` | `en_GB` | Locale для Zara API. |
| `CHECK_INTERVAL_SEC` | `300` | Пауза между циклами проверки. |
| `REQUEST_DELAY_SEC` | `1` | Пауза между запросами к Zara внутри цикла. |
| `DATA_FILE` | `/app/data/products.json` | Путь к persistent state. |
| `PAGE_SIZE` | `10` | Размер страницы для `/list` и `/remove`. |
| `HEALTH_ERROR_THRESHOLD` | `3` | Сколько failed check cycles подряд нужно для health alert. |
| `MAX_TRACKED_ITEMS` | `200` | Максимальное количество подписок в storage. |
| `TELEGRAM_CONFLICT_EXIT_THRESHOLD` | `5` | Через сколько `409 Conflict` подряд worker падает fail-fast. |

## Локальный запуск

1. Создать `.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_IDS=...
ZARA_STORE_ID=11734
ZARA_LOCALE=en_GB
CHECK_INTERVAL_SEC=300
REQUEST_DELAY_SEC=1
```

2. Запустить через Docker Compose:

```sh
docker compose up --build
```

Локально `docker-compose.yml` монтирует:

```text
./data -> /app/data
```

Поэтому state лежит в:

```text
data/products.json
```

State хранится в schema v2:

```json
{
  "schema_version": 2,
  "subscriptions": []
}
```

Legacy state в виде массива подписок мигрируется автоматически при старте. Так как старая модель была глобальной, каждый legacy item разворачивается в подписку для каждого chat id из `TELEGRAM_CHAT_IDS`; дубли по `chat_id + product_id + color_id + size_id` схлопываются. Перед перезаписью создаётся backup:

```text
products.json.bak
```

## Fly.io

### Основной app

```text
zara-monitor
```

`fly.toml`:

```toml
app = 'zara-monitor'
primary_region = 'iad'

[mounts]
  source = "zara_data"
  destination = "/app/data"
```

### Secrets

Задать runtime secrets:

```sh
fly secrets set \
  TELEGRAM_BOT_TOKEN="..." \
  TELEGRAM_CHAT_IDS="..." \
  ZARA_STORE_ID="11734" \
  ZARA_LOCALE="en_GB" \
  -a zara-monitor
```

Проверить:

```sh
fly secrets list -a zara-monitor
```

### Volume

Для persistence нужен Fly volume:

```sh
fly volume create zara_data -a zara-monitor -r iad -n 1 --size 1
```

Проверить содержимое:

```sh
fly ssh console -a zara-monitor -C "ls -lah /app/data"
fly ssh console -a zara-monitor -C "cat /app/data/products.json"
```

Загрузить локальный `products.json` на Fly volume. `MACHINE_ID` можно взять из `fly machines list -a zara-monitor`:

```sh
fly ssh console -a zara-monitor -C "rm -rf /app/data/products.json"
fly ssh console -a zara-monitor -C "mkdir -p /app/data"
fly ssh console -a zara-monitor -C "sh -c 'cat > /app/data/products.json'" < data/products.json
fly machine restart MACHINE_ID -a zara-monitor
```

### Деплой вручную

```sh
fly deploy --config fly.toml
```

Проверить:

```sh
fly status -a zara-monitor
fly releases -a zara-monitor
fly logs -a zara-monitor
```

## GitHub Actions

Workflow: [`.github/workflows/fly-deploy.yml`](./.github/workflows/fly-deploy.yml)

На push в `main`:

1. запускаются проверки;
2. если проверки прошли — выполняется `flyctl deploy --remote-only --config fly.toml`.

На Pull Request:

- запускаются только проверки;
- деплой не выполняется.

Нужен repository secret:

```text
FLY_API_TOKEN
```

Создать токен:

```sh
fly tokens create deploy -a zara-monitor
```

Добавить в GitHub:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
```

## CI-проверки

Сейчас в pipeline выполняются:

```sh
python -m compileall monitor.py zara_monitor tests
ruff check .
ruff format --check .
pytest
```

Dev-зависимости:

```sh
pip install -r requirements.txt -r requirements-dev.txt
```

Локально лучше использовать virtualenv:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
ruff format --check .
python -m compileall monitor.py zara_monitor tests
pytest
```

## Troubleshooting

### `Process group 'app' needs volumes with name 'zara_data'`

Создать volume в регионе приложения:

```sh
fly volume create zara_data -a zara-monitor -r iad -n 1 --size 1
```

### `products.json: Is a directory`

На volume вместо файла создана директория. Исправить (`MACHINE_ID` можно взять из `fly machines list -a zara-monitor`):

```sh
fly ssh console -a zara-monitor -C "rm -rf /app/data/products.json"
fly ssh console -a zara-monitor -C "sh -c 'cat > /app/data/products.json'" < data/products.json
fly machine restart MACHINE_ID -a zara-monitor
```

### `Monitor started | tracking 0 item(s)`

Проверить, что `/app/data/products.json` существует и содержит список товаров:

```sh
fly ssh console -a zara-monitor -C "ls -lah /app/data && cat /app/data/products.json"
```

### Telegram `409 Conflict` на `getUpdates`

Означает, что второй процесс использует тот же `TELEGRAM_BOT_TOKEN` через long polling.

Проверить старые Fly apps:

```sh
fly apps list
fly machines list -a zara-monitor-crimson-canyon-554
```

Остановить или удалить старый app:

```sh
fly machine stop MACHINE_ID -a zara-monitor-crimson-canyon-554
# или
fly apps destroy zara-monitor-crimson-canyon-554
```

Также проверить локальные процессы:

```sh
ps aux | grep monitor.py
docker ps
```

### Telegram token попал в логи

Перевыпустить токен через `@BotFather`, затем обновить Fly secret:

```sh
fly secrets set TELEGRAM_BOT_TOKEN="новый_токен" -a zara-monitor
```

## Security notes

- Не коммитить `.env`.
- Не публиковать `TELEGRAM_BOT_TOKEN` в issues/chats/screenshots.
- Если токен попал в логи или переписку — перевыпустить через `@BotFather`.
- Runtime secrets должны храниться в Fly secrets, а не в GitHub Actions secrets, кроме `FLY_API_TOKEN` для деплоя.

## Roadmap short list

Сделано в текущей версии:

1. Маскирование Telegram token в логах и отключение noisy `httpx` INFO logs.
2. Atomic storage write + backup + schema version.
3. `/status` и one-shot health alerts.
4. `ZaraClient` / `TelegramClient` с typed exceptions.
5. Deduplication по `chat_id + product_id + color_id + size_id`.
6. Выбор цвета перед выбором размера.
7. `/cancel`, `/check_now`, pagination для `/list` и `/remove`.
8. Базовая модель под несколько пользователей через `chat_id` в подписке.
9. Pytest-набор для parsing/storage/health/security.

Следующие улучшения:

1. Расширить тесты Telegram flow и monitor transition logic.
2. Дальше дробить `zara_monitor/bot_controller.py` на более мелкие flow-модули, если он начнёт расти.
3. Добавить SQLite migration, если state начнёт расти.
4. Добавить `/import` или полноценную отправку `/export` как document.
5. Добавить историю проверок и ошибок.
