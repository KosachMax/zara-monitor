# Универсальный Code Review — X5 Monorepo

> **Назначение:** единый документ для Codex, Cursor и ревьюеров. Любой сервис workspace: Django-монолит, FastAPI-микросервис, библиотека, workers. Bootstrap нового MS, PR/diff review, self-review перед merge.
>
> **PB-specific (PLU/KM/masterdata):** `.codex/code-review-agent.md`
>
> **Дополнительные rules (по стеку):** `.cursor/rules/frameworks/django-patterns.mdc`, `fastapi-patterns.mdc`, `languages/python-core.mdc`, `process/testing.mdc`

---

## Содержание

1. [Промпт ревьюера](#1-промпт-ревьюера)
2. [Главный принцип и severity](#2-главный-принцип-и-severity)
3. [Паттерны и антипаттерны](#3-паттерны-и-антипаттерны)
4. [Workflow ревью](#4-workflow-ревью)
5. [Формат ответа](#5-формат-ответа)
6. [Границы нового сервиса — шаблон](#6-границы-нового-сервиса--шаблон)
7. [Checklist](#7-checklist)

---

## 1. Промпт ревьюера

```
Ты — Senior Staff Backend Reviewer для monorepo X5 Partners.
Ревьюируешь любой сервис workspace. Задача — находить реальные риски, а не вкусовщину.

Пиши по-русски, профессионально и прямо. Не начинай с похвалы.
Итог — только структурированный результат ревью по формату из раздела 5.

Контекст:
  Сервис: {{SERVICE_NAME}}
  Стек: {{STACK}}
  Роль: {{SERVICE_ROLE}}

<service-boundaries>{{SERVICE_BOUNDARIES}}</service-boundaries>
<additional-context>{{CONTEXT}}</additional-context>
<diff>{{DIFF}}</diff>
```

### Что грузить перед ревью

| Приоритет | Файл | Когда |
|-----------|------|-------|
| 1 | Этот документ | всегда |
| 2 | `frameworks/django-patterns.mdc` | `partners-backend/**`, `x5-partners-users/**` |
| 3 | `frameworks/fastapi-patterns.mdc` | FastAPI MS |
| 4 | `core/partners-backend-feature-implementation.mdc` | PB masterdata |
| 5 | `core/validation-layer-placement.mdc` | PB validation |
| 6 | `core/chat-code-style-rule.mdc` | `partners-chats/**` |
| 7 | `core/esm-code-stle-rule.mdc` | `esm/**` |
| 8 | `languages/python-core.mdc` | `**/*.py` |
| 9 | `process/testing.mdc` | тесты, команды |
| 10 | Раздел 6 этого файла | новый сервис / bootstrap |

---

## 2. Главный принцип и severity

### Приоритет проверок

1. Корректность поведения и бизнес-инварианты
2. Безопасность и права доступа
3. Целостность данных и транзакции
4. Интеграции: idempotency, retry, post-commit
5. Производительность на hot path
6. Тесты на рискованные ветки
7. Поддерживаемость — только если есть реальный риск

**Не комментировать**, если:
- нет конкретного риска
- замечание вне diff
- это вопрос вкуса
- линтер уже ловит без влияния на смысл

### Severity

| Уровень | Когда |
|---------|-------|
| `BLOCKER` | Потеря/порча данных, уязвимость, сломан critical flow, падение приложения, утечка прав |
| `HIGH` | Регрессия процесса, race condition, некорректный статус, плохая миграция, существенная деградация perf |
| `MEDIUM` | Неполный edge case, слабая обработка ошибок, недостаточные тесты, обход write-path |
| `LOW` | Поддерживаемость, читаемость без очевидного риска |
| `NIT` | Мелочь. Редко. |

### Вердикт

`APPROVE` | `APPROVE WITH COMMENTS` | `REQUEST CHANGES` (есть BLOCKER/HIGH) | `NEEDS CONTEXT`

---

## 3. Паттерны и антипаттерны

### 3.1 Архитектура и слои

#### ✅ Паттерн

```
Router/View/Controller → Service → Repository/Manager/QuerySet → Model
```

| Слой | Ответственность |
|------|----------------|
| HTTP (router/view) | auth, маппинг HTTP-ошибок, serializer/schema in/out |
| Service | оркестрация, бизнес-инварианты, интеграции, статусы |
| Repository | доступ к данным, фильтры, prefetch, **без** бизнес-правил |
| Model | persistence, model-local invariants |

Дополнительно:
- Контракты (OpenAPI, Pydantic, DRF serializer, MQ/XML) — явные и версионируемые
- Внешний I/O на границе service: известные сбои → domain error / typed empty result, не случайный 500
- Soft delete через `is_deleted` там, где домен так устроен
- Repository **не** импортирует Service

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Бизнес-логика в router/view/serializer | Обход при worker/internal/import path | HIGH |
| View/Router ходит в ORM/Repository для новой логики | Дублирование, HTTP-only инварианты | HIGH |
| Repository импортирует Service | Циклы, размытые границы | MEDIUM |
| «God service» 500+ строк без доменного разбиения | Нетестируемость | MEDIUM |
| Hard delete без обоснования | Потеря аудита/ссылок | HIGH |
| Смешение API, ORM, интеграций и форматирования ответа | Не переиспользуется вне HTTP | MEDIUM |

---

### 3.2 Валидация и write-path

#### ✅ Паттерн

Перед добавлением проверки — аудит:

```text
Правило: что запрещаем/разрешаем.
Владелец: сущность/поле/роль/workflow.
Слой: где лежит проверка.
Почему: какие пути вызова покрывает.
Антипаттерн: куда класть нельзя.
Пути: GET, PATCH/PUT, import, worker, internal API — только применимые.
```

| Тип правила | Слой |
|-------------|------|
| Формат, тип, обязательность на входе HTTP | Pydantic `Field` / DRF serializer field |
| Бизнес-инвариант с БД, статусом, соседями | Service `validate_*` / `ensure_*` |
| Правило одного property/поля, все write-path | Domain validator / PropertyValueValidator (PB) |
| Ролевое исключение | Единый policy-helper для GET и PATCH |
| UI `disabled` / `readonly` | Только отображение; write защищён отдельно |

Обязательно:
- Один механизм на **все** write-path: HTTP, Celery, FastStream, internal, import
- Неизменённое значение заблокированного поля — разрешено
- Bypass одного ограничения не снимает независимые (`is_editable`, `is_locked`, категория и т.д.)

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Валидация только в router при `if request` | Worker/internal обходит | BLOCKER |
| `disabled` в GET — единственная защита | PATCH/import меняет данные | BLOCKER |
| Дублирование правила в view + serializer + service | Рассинхрон | HIGH |
| Ролевой bypass снимает все блокировки | Обход PLU/lock/категорий | BLOCKER |
| MDM/pre-send в edit-access layer (и наоборот) | Неверные ошибки, пропуск MDM | HIGH |

---

### 3.3 Django / ORM (`partners-backend`, `x5-partners-users`)

#### ✅ Паттерн

- `select_related` / `prefetch_related` / `in_bulk` / `values_list` против N+1
- `transaction.atomic()` для связанных изменений
- `update_fields` при точечном `.save()`
- Фильтры по user/partner через `user_restricted()` или аналог
- Внешние вызовы вне транзакции (если не требуется иначе осознанно)
- Custom Manager/QuerySet для повторяемых запросов
- Django ORM **синхронный** — async ORM не вводить

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Запросы в цикле в serializer/view | N+1, таймауты | HIGH |
| `.all()` на большой таблице без лимита | OOM, блокировки | HIGH |
| `get_or_create` / смена статуса без транзакции/unique | Race, дубли | HIGH |
| Async ORM в Django | Несовместимость стека | BLOCKER |
| Raw SQL + конкатенация user input | SQL injection | BLOCKER |

---

### 3.4 FastAPI / SQLAlchemy (новые и существующие MS)

#### ✅ Паттерн

- `async` I/O в handlers и services
- Pydantic v2: request/response schemas, `Field` constraints
- `Depends` + Container/factory для repo и service
- SQLAlchemy 2.0: `Mapped`, `mapped_column`, async session
- Repository: queries, `selectinload`/`joinedload` для batch
- Router = HTTP mapping; service = бизнес-сценарий
- Health endpoint для k8s probes
- Idempotency guard для retry/integration side effects
- Тяжёлая логика: Pydantic DTO / `@dataclass`, не opaque `dict` chains

Структура нового сервиса:

```
app/api/routers/     → HTTP, auth, response mapping
app/services/        → бизнес-инварианты, оркестрация
app/repositories/    → SQLAlchemy queries
app/models/          → ORM models
app/schemas/         → Pydantic request/response
```

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Sync blocking I/O в async handler | Event loop block | HIGH |
| SQL в router | Нетестируемость, дубли | MEDIUM |
| Бизнес-ветвления в Pydantic schema вместо service | Не работает в worker | HIGH |
| Session/commit в router | Утечки транзакций | MEDIUM |
| `_normalize_*` на одну строку без reuse | Шум | LOW |
| `dict[str, Any]` через 3+ шага workflow | Потеря контракта | MEDIUM |
| Прямой доступ к чужой БД | Нарушение границ MS | BLOCKER |

---

### 3.5 API, права, контракты

#### ✅ Паттерн

- Явные `permission_classes` / `Depends(get_current_user)` / service-user для internal
- Разделение `/api/internal` и `/api/external` с разной auth-моделью
- Валидация: schema + service
- Breaking change — осознанно: consumers, migration path, тесты контракта
- Не раскрывать внутренние поля, токены, PII

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Endpoint без auth на mutable resource | IDOR, data leak | BLOCKER |
| Доступ к чужим entity без partner/user filter | Утечка данных | BLOCKER |
| Изменение response shape без совместимости | Ломает consumers | HIGH |
| 500 на известный сбой интеграции | Шум алертов | MEDIUM |

---

### 3.6 Интеграции и фоновые задачи

#### ✅ Паттерн

- Retry/backoff + idempotency key / dedup guard
- Celery/FastStream task **после** commit
- Лог: entity id, integration name, correlation id — **без** секретов
- Timeout / circuit-breaker на внешние HTTP/MQ
- Явная обработка partial failure в batch

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Повторная отправка без idempotency | Дубли в MDM/ESM/email | HIGH |
| Task читает данные до commit | Race, stale data | HIGH |
| `except Exception: pass` | Silent data loss | HIGH |
| Секреты в логах/ответах | Compliance incident | BLOCKER |

---

### 3.7 Миграции и данные

#### ✅ Паттерн

- Обратимость или явная необратимость в описании
- Data migration батчами на больших таблицах (>10k rows)
- `default`/`null`/`index`/`constraint` согласованы с кодом
- Historical models в data migrations (Django), не live imports
- Alembic для FastAPI MS

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| `ALTER` без оценки lock time | Downtime | HIGH |
| Конфликт номеров миграций | CI/deploy fail | MEDIUM |
| Nullable в БД, required в API без default | Runtime 500 | MEDIUM |

---

### 3.8 Безопасность

Проверять всегда:

- SQL injection, unsafe raw SQL
- SSRF / path traversal (URL, файлы, S3 keys)
- Upload: size, mime, extension, content-type
- `pickle` / `eval` / `exec` / shell из user input
- Admin/export/import endpoints — права
- Env/secrets не в коде, логах, тестовых фикстурах в репо

---

### 3.9 Тесты

#### ✅ Паттерн

- Happy path + edge cases + error paths
- Свой ресурс / чужой ресурс (403)
- Транзакционные и идемпотентные сценарии
- Не-HTTP write-path, если существует
- Docker-based pytest
- Моки на границе I/O, не внутри доменной логики целиком
- Проходят под `pytest -n auto`

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Нет теста на новую валидацию/permission | Регрессия в prod | HIGH |
| Тест зависит от порядка | Flaky CI | MEDIUM |
| Over-mock | Ложная уверенность | MEDIUM |

---

### 3.10 Поддерживаемость и diff-гигиена

#### ✅ Паттерн

- Минимальный diff — только затронутая функциональность
- Имена из предметной области
- Domain exceptions с понятным сообщением
- Переиспользование validators/services/constants сервиса
- Нет one-line private helpers без reuse
- Тяжёлая логика с явными DTO/dataclass

#### ❌ Антипаттерн

| Антипаттерн | Риск | Severity |
|-------------|------|----------|
| Переформатирование/кавычки вне задачи | Шум, merge conflicts | LOW |
| Drive-by refactor | Скрытые регрессии | MEDIUM |
| Архитектурное переписывание без риска | — не требовать | — |

---

## 4. Workflow ревью

1. Определи сервис, стек, бизнес-flow.
2. Загрузи rules по стеку (таблица в разделе 1).
3. Если новый сервис — сверь diff с **Out of scope** (раздел 6).
4. Review по приоритету:
   - write-path и валидация (один механизм на все пути)
   - authZ / partner scope / IDOR
   - транзакции, race, статусы
   - контракты API/MQ
   - интеграции: idempotency, post-commit
   - N+1 / batch perf
   - миграции
   - тесты на запрещённые сценарии
   - diff-гигиена
5. Findings first, по severity, с file:line.
6. Вердикт.

### Bootstrap нового сервиса (до первого PR)

1. Заполни раздел 6 → сохрани в `<service>/docs/boundaries.md`
2. Добавь сервис в `.codex/AGENTS.md` workspace map
3. Codex **не выходит** за **Out of scope** без явного запроса
4. Service-specific rule (`core/<service>-code-style-rule.mdc`) — только для уникальных правил

---

## 5. Формат ответа

Начинай с findings. Для каждого:

```markdown
### `<SEVERITY>`: `<краткое описание>`

**Где:** `path/to/file.py:42` (или метод/класс)
**Проблема:** что не так.
**Почему важно:** какой сценарий ломается.
**Как исправить:** конкретное предложение.
**Тест:** какой тест добавить.
```

После findings:

```markdown
## Open Questions
(только реальные блокеры понимания)

## Проверки
- pre-commit run -a (если есть)
- Docker pytest
- targeted tests

## Вердикт
APPROVE | APPROVE WITH COMMENTS | REQUEST CHANGES | NEEDS CONTEXT
```

### Правила точности

- Не выдумывай поведение; гипотезы помечай
- Не требуй переписывания без риска
- Тест — только если ловит конкретный сценарий
- Если замечаний нет: «Существенных проблем не нашёл.»
- Ответ — только структурированный результат, без черновых рассуждений

---

## 6. Границы нового сервиса — шаблон

> Заполни при bootstrap. Копия: `<service>/docs/boundaries.md`. Codex и ревьюер читают как **источник дозволенного и запрещённого**.

### Мета

| Поле | Значение |
|------|----------|
| **Имя сервиса** | `my-service/` |
| **Стек** | FastAPI + SQLAlchemy async + PostgreSQL + pytest |
| **Роль в экосистеме** | Что делает, с кем интегрируется |
| **Владелец домена** | Команда / контакт |
| **Task** | PP-XXXXXX |

### In scope (разрешено)

- [ ] CRUD / read API для сущностей: `...`
- [ ] Internal API (`/api/internal/...`) с service-user / Keycloak
- [ ] External callbacks: `...`
- [ ] Собственная БД — только свои таблицы
- [ ] Celery/FastStream consumers: `...`
- [ ] Кэш Redis: `...`

### Out of scope (запрещено без отдельной задачи)

- [ ] Прямой доступ к БД чужого сервиса
- [ ] Дублирование PLU/KP/Offers логики — только через контракт
- [ ] Breaking change контрактов без migration plan
- [ ] Hard delete с внешними ссылками
- [ ] Sync blocking I/O в async handlers
- [ ] Бизнес-правила только в router

### Зависимости

| Направление | Как | Запрещено |
|-------------|-----|-----------|
| `my-service` → `partners-backend` | HTTP internal API | Shared DB |
| `my-service` → `x5-mailing` | MQ / HTTP | Прямой SMTP |

### API (черновик)

| Method | Path | Auth | Назначение |
|--------|------|------|------------|
| GET | `/health` | none | k8s probe |
| GET | `/api/internal/v1/...` | service-user | ... |

### Инварианты домена

1. Статусы: `DRAFT` → `ACTIVE` → `ARCHIVED` (только такие переходы)
2. ...

### Ошибки

| Ситуация | HTTP | Тело |
|----------|------|------|
| Не найдено | 404 | `{"detail": "..."}` |
| Нет прав | 403 | ... |
| Конфликт | 409 | ... |

### Данные

| Таблица | Владелец | Soft delete | Критичные поля |
|---------|----------|-------------|----------------|
| `entities` | my-service | `is_deleted` | `status`, `partner_id` |

### Интеграции

| Система | Направление | Протокол | Idempotency | On failure |
|---------|-------------|----------|-------------|------------|
| partners-backend | outbound | HTTP | request_id header | retry 3x, DLQ |

### Безопасность

- Все mutable endpoints: auth + partner scope
- PII: не логировать
- Secrets: только env / vault

### Тесты (минимум для merge)

- [ ] `test_health`
- [ ] CRUD happy path
- [ ] 403 на чужой `partner_id`
- [ ] Invalid status transition → 409
- [ ] Integration client mocked at boundary

```bash
cd my-service
docker-compose up -d
docker-compose exec -T web bash -c "pytest -v"
```

### Review gate для сервиса

1. Ничего из **Out of scope** в diff
2. Новые endpoints в таблице API
3. Инварианты в service layer + тестах
4. Интеграции: idempotency, post-commit
5. Слои не нарушены (раздел 3.1)

---

## 7. Checklist

1. Какой сервис и бизнес-flow?
2. Все write-path покрыты одним механизмом валидации?
3. Права и partner/user scope на новых endpoints?
4. Контракт API/MQ не сломан?
5. Транзакции и race на смене статуса?
6. N+1 / batch perf?
7. Интеграции: retry, idempotency, post-commit?
8. Миграции безопасны для prod?
9. Тесты: запрещённый сценарий + чужие данные?
10. Diff минимален?
