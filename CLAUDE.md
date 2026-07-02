# Zara Stock Monitor

## Контекст
Мониторинг наличия размеров на Zara с уведомлениями в Telegram.
Запуск через Docker Compose.

## Текущий статус
- Базовый скрипт готов (monitor.py) — простые httpx запросы
- Следующий шаг: anti-bot защита

## Задача
Zara блокирует простые HTTP-запросы (Akamai/Cloudflare).
Нужно добавить одно из:
- Playwright + playwright-stealth
- Снять mobile API эндпоинты через mitmproxy
- Передать куки из реального браузера

## Решение выбрать вместе, приоритет — минимальная сложность

≈
