# CLAUDE.md - точка входа для Claude Code/CLI

Telegram-бот для проверки домашних заданий по математике: **`bot.py`** (Telegram) + **`server.py`** (FastAPI backend), Python, секреты через **`.env`**.

## Первым делом читай

- **[AGENTS.md](AGENTS.md)** - основной контекст репозитория, "память" сессий, Learned Workspace Facts
- **[README.md](README.md)** - установка, запуск, переменные окружения, деплой
- **[docs/integration.md](docs/integration.md)** - интеграция внешних проектов, API, bridge, сеть
- **[docs/error-registry.md](docs/error-registry.md)** - реестр инцидентов и известных ошибок
- **[RELEASE_NOTES.md](RELEASE_NOTES.md)** - журнал изменений

## Быстрые команды

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
python server.py          # backend, порт 8000
python bot.py             # Telegram-бот (нужен SERVER_URL)
docker compose build && docker compose up -d --force-recreate
```

С OCR-профилем: `docker compose --profile preocr build && docker compose --profile preocr up -d --force-recreate`

## Конвенции репозитория

Claude Code не подхватывает `.cursor/rules/` автоматически - при правках соблюдай:

- **Оформление текста**: `е` вместо `ё`, дефис `-` (не тире), прямые кавычки, буллеты без точки в конце - [text-formatting.mdc](.cursor/rules/text-formatting.mdc)
- **Env**: новые переменные - в `.env.example` и в рабочий `.env` без перезаписи существующих значений - [env-example-and-local.mdc](.cursor/rules/env-example-and-local.mdc)
- **Release notes**: при фичах/фиксах - секция `[Unreleased]` в `RELEASE_NOTES.md` - [release-notes.mdc](.cursor/rules/release-notes.mdc)
- **README**: обновлять при изменениях запуска, env, деплоя - [readme-maintain.mdc](.cursor/rules/readme-maintain.mdc)
- **Документация**: в `README.md` и `docs/` не ссылаться на `.cursor/` - [docs-no-cursor-refs.mdc](.cursor/rules/docs-no-cursor-refs.mdc)
- **Git**: не коммитить запрещенный брендовый префикс в файлах и commit message - [no-sensitive-host-prefix.mdc](.cursor/rules/no-sensitive-host-prefix.mdc)
- **Commit message**: `<тип>: <описание на английском>` - [commit-message-suggestion.mdc](.cursor/rules/commit-message-suggestion.mdc)
- **Конец сессии**: обновить `AGENTS.md` при закрытии Cursor - [agents-md-session-end.mdc](.cursor/rules/agents-md-session-end.mdc)

## Карта кода

| Модуль | Назначение |
|--------|------------|
| `bot.py` | Telegram-хендлеры, FSM, inline-кнопки |
| `server.py` | FastAPI: `/check`, `/chat/stream` |
| `ai_checker.py` | LLM-запросы, VLLM + fallback (Cursor bridge) |
| `user_storage.py` | SQLite/Postgres helpers, сессии, блокировки |
| `tgzh_db.py` | Подключение к БД, `init_db` |
| `preocr_client.py` | Pre-OCR для фото ДЗ |
| `stt_client.py` | Whisper STT для голосовых |
| `alembic/` | Миграции Postgres |
| `tests/` | Юнит-тесты (`pytest`, `asyncio_mode = auto`) |
