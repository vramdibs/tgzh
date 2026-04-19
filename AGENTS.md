# AGENTS.md - контекст репозитория tgzh

Кратко для продолжения работы в новой сессии.

## Что это

Telegram-бот для проверки домашних заданий по математике (фото тетради или **текстовый ответ**, кнопка **«Ответить текстом»**): **`bot.py`** + backend **`server.py`**, Python, зависимости в **`requirements.txt`**, секреты через **`.env`** (шаблон **`.env.example`**).

## Документация

Пользовательская и обзорная информация - корневой **`README.md`**. Отдельного каталога **`docs/`** сейчас нет.

## Правила агента

Локальные инструкции для Cursor лежат в **`.cursor/rules/`** (не дублировать их содержимое в **`README.md`**).

## Learned Workspace Facts

- Правила **`.cursor/`** приведены в соответствие с этим репозиторием (не nfqws/роутер)
- Проверка **текстом** (**«Ответить текстом»**): на **`tgzh-server`** pre-OCR только для **`image/*`**; при пустом MIME или **`application/octet-stream`** валидный **UTF-8** сниффится как **`text/plain`** (**`ai_checker._mime_from_bytes`**), чтобы не уходить в ветку JPEG и **`preocr_client`**
- **`/cursorask`**: переменные в **`.env.example`** (блок Cursor CLI); вход по доке CLI - **`agent login`** или **`CURSOR_API_KEY`** / **`--api-key`** (ключи в Dashboard → Cloud Agents); **Shell Mode** в доке - другой сценарий, не про headless-вход; см. **README** и [Authentication](https://cursor.com/docs/cli/reference/authentication)
- Docker: **`docker-compose.yml`** - **postgres**, **tgzh-server**, **tgzh-bot**, сеть **`tgzh-internal`**; опционально **`tgzh-preocr`** (профиль **`preocr`**, **по умолчанию CPU-образ `Dockerfile.preocr` без `gpus`**, **`restart: always`**, **`PREOCR_URL`**, см. **README**; для GPU нужен `docker-compose.override.yml` с `deploy.resources` и `PREOCR_DOCKERFILE=Dockerfile.preocr.gpu`); типичный передеплой из корня: **`docker compose build && docker compose up -d --force-recreate`**; с OCR: **`docker compose --profile preocr build && docker compose --profile preocr up -d --force-recreate`**. После выкладки: **`curl -fsS http://127.0.0.1:8000/health`**, при профиле **`preocr`** — **`curl -fsS http://127.0.0.1:8088/health`**. Скрипт **`scripts/deploy-docker.sh`**: опционально **`DEPLOY_COMPOSE_PROFILE=preocr`**, **`DEPLOY_RUN_ALEMBIC=1`** (см. **README**, **`.env.example`**)
- Docker-регресс «проверка фото не доходит до API»: в **`Dockerfile.server`** в **`COPY`** должны быть **все** модули, которые импортируют **`ai_checker.py`** / **`server.py`** (пример: **`homework_check_status.py`** для константы метки смешанных чисел). Без этого **`tgzh-server`** падает при старте (**`ModuleNotFoundError`**). Если **`tgzh-server`** «пропал» из сети или DNS между контейнерами ломается — **`docker compose down`** и снова **`docker compose up -d`**. Детали — правило **`.cursor/rules/docker-tgzh-stack.mdc`**
- Запросы бота к **`SERVER_URL`** (проверка, quip, сводка): в **`bot.py`** — транспорт **httpx** с приоритетом резолва **IPv4**; при правках этих путей не убирать без причины
- Админка отзывов (**`/begemot`**): после **`fb:v:{user_id}`** бот шлет сообщение **«Действия для …»** с inline-кнопками **Блок 1 ч** (**`fb:bt`** → причина следующим сообщением, **`context.user_data['admin_ban_wait']`**), **Бан** (**`fb:bp`**), **Разбан** (**`fb:bu`**), **Спасибо за отзыв** (**`fb:th`**, можно много раз). Пользователю при временном блоке - уведомление и **`ban:lift`** (**Снять ограничение**), снимает только temp
- Блокировки в **`user_storage`**: таблица **`user_block`**, **`blocked_state`**, **`set_temp_user_block`** / **`set_permanent_user_block`** / **`clear_user_block`**. Гейт для обычных пользователей в **`bot.py`**: команды **`/start`**, **`/textbook`**, **`/stats`**, **`/support`**, **`/my_support`**, **`/polling`**, **`handle_homework_text`**, **`handle_photo`**, колбэки (кроме сессии begemot)
- PostgreSQL: **`user_block`** в **`scripts/postgres_schema.sql`**, миграция Alembic **`003_user_block`**, импорт SQLite→Postgres в **`scripts/import_sqlite_to_postgres.py`**. При **`DATABASE_URL`** после деплоя проверить **`alembic upgrade head`**
- Тесты блокировок: **`tests/test_user_block.py`**

## Learned User Preferences

- Просил зафиксировать контекст при закрытии сессии; перед этим - передеплой через Docker Compose (**`--profile preocr`**, если в **`.env`** задан **`PREOCR_URL`** на **`tgzh-preocr`**)
- Новые настройки из env: не только **`.env.example`**, но и актуализация рабочего **`.env`** без перезаписи уже заданных значений — правило **`.cursor/rules/env-example-and-local.mdc`**
