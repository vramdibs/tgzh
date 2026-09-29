# Бот для проверки домашних заданий по математике

Telegram-бот принимает **фото** тетрадного листа с ДЗ или **текстовый ответ** ученика (классы 6–11 в каталоге), отправляет на сервер и возвращает результат проверки. Перед этим нужно выбрать класс и учебник (список совпадает с разделом математики на gdz.ru).

Лицензия: [Apache-2.0](LICENSE)

## Документация для интеграции

- [docs/integration.md](docs/integration.md) - база знаний: HTTP API, контракт с cursor-bridge, env, сеть, чек-лист подключения внешних проектов
- [docs/error-registry.md](docs/error-registry.md) - реестр известных инцидентов (симптом, причина, решение)
- [docs/README.md](docs/README.md) - оглавление каталога `docs/`

## Связанные репозитории

- [discourse-cursor-bridge](https://github.com/vramdibs/discourse-cursor-bridge) - OpenAI-совместимый proxy к `cursor-agent` (порт 8787); нужен для `/chat` и «Проверить ещё раз (Cursor)»

## Быстрый старт

```bash
# Установка зависимостей
pip install -r requirements.txt

# Настройка
cp .env.example .env
# Отредактируй .env и укажи BOT_TOKEN (см. раздел ниже)

# Терминал 1: запуск сервера
python3 server.py

# Терминал 2: запуск бота
python3 bot.py
```

Переменные читаются из `.env` автоматически. Файл **`data/gdz_matematika_textbooks.json`** уже есть в репозитории; при необходимости обнови каталог:

```bash
python3 scripts/fetch_gdz_textbooks.py
```

Скрипт ходит на gdz.ru с обычным браузерным User-Agent и делает паузу между запросами к 6 и 7 классу.

### Тесты

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Перед прогоном без **`DATABASE_URL`** в окружении (или фикстура в **`tests/conftest.py`** обнуляет его): иначе **`user_storage`** уйдет в Postgres вместо временных файлов SQLite.

Интеграция с **PostgreSQL** (после **`docker compose up`**, порт **`127.0.0.1:15432`**):

```bash
export TGZH_TEST_DATABASE_URL=postgresql://tgzh:tgzh@127.0.0.1:15432/tgzh
pytest tests/test_postgres_integration.py -v
```

Схема в БД должна быть актуальна (**`alembic upgrade head`** в контейнере **`tgzh-bot`** или рабочий **`init_db`**).

Юнит-тесты: **`ai_checker`** (URL VLLM, MIME, mock, опциональный preocr), **`gdz_solution`**, API **`/health`** и **`POST /check`** через **`TestClient`**, сервис **`preocr`** (схемы, **`engine`** с моками Paddle, FastAPI **`TestClient`**, **`preocr_client`**), **`feedback_tei`** / **`tgzh_httpx`** (TEI **`/predict`**, IPv4-транспорт), **`feedback_ticket_nlp`**, HTML TEI в **`bot`**

Проверка **доступности LLM** по HTTP (`GET /v1/models`, OpenAI-совместимый эндпоинт) не запускается в обычном прогоне. Явно:

```bash
RUN_LLM_LIVE=1 pytest -m llm
```

Нужны **`VLLM_BASE_URL`** и **`AI_MOCK`** не равный **`1`** (как при реальной проверке ДЗ)

## Архитектура и зависимости сервисов

Карта запущенных процессов и того, какой эндпоинт по какой фиче дёргается. **Жирным** выделены обязательные для базовой работы компоненты, *курсивом* — опциональные.

```mermaid
flowchart LR
    subgraph Internet
        user(("Пользователь<br/>Telegram"))
        tg["Telegram Bot API<br/>api.telegram.org:443"]
    end

    subgraph Compose["Docker Compose (tgzh-internal)"]
        bot["**tgzh-bot**<br/>polling, FSM, RAM-история чата"]
        server["**tgzh-server** (FastAPI)<br/>/check, /chat/stream,<br/>/check/summarize, /check/quip, /health"]
        pg[("**postgres** (или SQLite<br/>в томе tgzh-data)")]
        preocr["*tgzh-preocr*<br/>profile=preocr<br/>POST /v1/preocr"]
        stt["**tgzh-stt** (hwdsl2/whisper-server)<br/>POST /v1/audio/transcriptions<br/>faster-whisper, CPU"]
    end

    subgraph External["Внешние сервисы (могут жить где угодно)"]
        vllm["**VLLM Qwen3-VL**<br/>POST /v1/chat/completions<br/>VLLM_BASE_URL"]
        bridge["*cursor-bridge*<br/>POST /v1/chat/completions<br/>VLLM_FALLBACK_BASE_URL"]
        cursorcli["*cursor-agent CLI*<br/>(headless, на хосте бриджа)"]
        tei["*TEI sentiment/emotion*<br/>POST /predict"]
        gdz["gdz.ru"]
    end

    user <-- "сообщения / inline / голос" --> tg
    tg <-- "webhook / polling" --> bot
    bot -- "POST /check, /check/summarize,<br/>/chat/stream" --> server
    bot <-- "SQL: профили, сессии, диалоги,<br/>отзывы, статистика" --> pg
    bot -- "HTTPS: каталог, оглавление,<br/>условия и картинки" --> gdz
    bot -- "POST /predict" --> tei
    bot -- "POST /v1/audio/transcriptions<br/>(только /chat)" --> stt
    server -- "openai client" --> vllm
    server -- "fallback / forced cursor" --> bridge
    bridge -- "subprocess --print" --> cursorcli
    server -- "POST /v1/preocr" --> preocr
```

### Что обязательно запустить и где

| Компонент | Обязательность | Где живёт | Чем рулится |
|---|---|---|---|
| **`tgzh-bot`** | всегда | Docker `tgzh-bot` (или `python3 bot.py` на хосте) | `BOT_TOKEN`, `SERVER_URL`, `DATABASE_URL` |
| **`tgzh-server`** (FastAPI) | всегда | Docker `tgzh-server` (или `python3 server.py`) | `PORT`, `VLLM_*`, `PREOCR_URL` |
| **PostgreSQL** *или* SQLite | одно из двух | `postgres`-контейнер либо том `tgzh-data` | `DATABASE_URL` (пусто → SQLite по `USER_DB_PATH`) |
| **VLLM (Qwen3-VL)** | для проверки ДЗ при `AI_MOCK=0` | внешний хост (см. `VLLM_BASE_URL`) | `VLLM_BASE_URL`/`API_KEY`/`MODEL` |
| *cursor-bridge* + *cursor-agent CLI* | для `/chat` и кнопки «Проверить ещё раз (Cursor)» | [discourse-cursor-bridge](https://github.com/vramdibs/discourse-cursor-bridge) на хосте `BRIDGE_HOST` | `VLLM_FALLBACK_*` (включая `_BASE_URL`, `_API_KEY`, `_MODEL=cursor-agent`) |
| *tgzh-preocr* (PaddleOCR) | для recheck «Cursor» по фото; общий буст качества | Docker profile `preocr` | `PREOCR_URL=http://tgzh-preocr:8088`, `PREOCR_*` |
| *TEI sentiment / emotion* | украшает `/begemot` (тон отзывов) | внешние сервисы | `TEI_SENTIMENT_URL`, `TEI_EMOTION_URL` |
| **`tgzh-stt`** (Whisper) | для голосовых в `/chat`; без него фича выключена и бот вежливо сообщает | Docker `tgzh-stt` (`hwdsl2/whisper-server`); либо внешний OpenAI-совместимый `/v1/audio/transcriptions` | `STT_BASE_URL`, `WHISPER_*` (для локального) или `STT_API_KEY` (для облачного) |
| *Cursor IDE на десктопе* | **не требуется** | — | — |

`docker compose up -d` поднимает `postgres` + `tgzh-server` + `tgzh-bot` + `tgzh-stt`. Чтобы добавить pre-OCR — `docker compose --profile preocr up -d --build`. Внешние сервисы (VLLM, bridge, TEI) поднимаются отдельно.

### Маршруты эндпоинтов по фичам

| Фича | Источник | Цепочка вызовов | Обязательные сервисы |
|---|---|---|---|
| Проверка ДЗ — **фото** (основная) | `«Проверить»` в боте | bot → `POST /check` (multipart `photo`) → preocr (если `PREOCR_URL`) → VLLM | bot, server, VLLM, *(preocr опц.)* |
| Проверка ДЗ — **несколько фото** | альбом → «Проверить» | bot → N×`POST /check` → `POST /check/summarize` → VLLM | bot, server, VLLM |
| **`/shot`** — условие и решение по снимкам | команда `/shot` | bot → `POST /photo/check` (multimodal, **без** preocr и ГДЗ) → Composer/Grok bridge или Qwen VL | bot, server, bridge или VLLM |
| Проверка ДЗ — **текст** | «Ответить текстом» | bot → `POST /check` (multipart `text/plain`, без preocr) → VLLM | bot, server, VLLM |
| **«Проверить ещё раз (Cursor)»** — текст | кнопка под результатом | bot → `POST /check` с `engine=cursor` → bridge → cursor-agent | bot, server, bridge+CLI, `VLLM_FALLBACK_*` |
| **«Проверить ещё раз (Cursor)»** — фото | кнопка под результатом | bot → `POST /check` `engine=cursor` → preocr → bridge (только текст) | bot, server, **preocr**, bridge+CLI |
| `/chat` — стриминговый ИИ-ассистент | `/chat` + пароль | bot → `POST /chat/stream` (SSE-like) → bridge → cursor-agent | bot, server, bridge+CLI |
| `/chat` — фото в чате | фото с подписью | bot → `POST /chat/stream` (multimodal user-msg, без preocr) → bridge → cursor-agent | bot, server, bridge+CLI |
| `/chat` — голосовое сообщение | voice/audio в `/chat` | bot → `POST /v1/audio/transcriptions` (`tgzh-stt`, faster-whisper) → распознанный текст → `POST /chat/stream` → bridge → cursor-agent | bot, server, bridge+CLI, **`tgzh-stt`** |
| Проверка ДЗ — **голосовой ответ** | voice/audio в шаге «Напиши решение» | bot → `POST /v1/audio/transcriptions` (`tgzh-stt`) → распознанный текст → `POST /check` (multipart `text/plain`) → VLLM | bot, server, **`tgzh-stt`**, VLLM |
| `/begemot` — отзывы | `/begemot` + пароль | bot → SQL (`user_feedback`, `feedback_ticket`) | bot, БД |
| Тон/эмоции отзывов | 👎 под результатом проверки | bot → `POST /predict` (TEI) → SQL `feedback_ticket_nlp` | bot, *(TEI опц.)*, БД |
| «Показать ГДЗ», условие задания | в сценарии ДЗ | bot → HTTPS `gdz.ru`, кеш `GDZ_CACHE_DIR` | bot, исходящий 443 |
| Метрики Prometheus | scrape | `bot.py` и `server.py` слушают `METRICS_PORT` `/metrics` | *(опц.)* |

### `/shot` (без учебника и ГДЗ)

Команда **`/shot`** в меню под **`/start`** (раньше могли называть `/photo`). Отдельный сценарий: снимки условия и решения (на одном кадре или несколькими фото). Сервер: **`POST /photo/check`**, multimodal **Composer** → **Grok** (bridge) или **Qwen VL**, **без** pre-OCR и **без** ГДЗ. Профиль параграфа не используется. Env: **`PHOTO_CHECK_*`**, **`BOT_PHOTO_CHECK_TIMEOUT_SEC`**.

Команда **`/stats`** в меню не показывается, но по-прежнему работает, если ввести вручную.

### Голосовые команды

Бот понимает голосовые в двух режимах (по приоритету):

1. **Ответ на ДЗ** — пока бот ждёт текстового ответа после кнопки «Ответить текстом» (флаг `_AWAIT_TEXT_ANSWER` в `user_data`), голосовое распознаётся и **сразу** уходит на проверку через `_run_homework_text_answer_check` тем же путём, что текст. Никаких подтверждений от ученика бот не ждёт.
2. **Промпт в `/chat`** — если активна `/chat`-сессия (гейт паролем), распознанный текст идёт в `_handle_chat_user_message` с пометкой для Cursor о возможных ошибках STT; в истории — плейсхолдер `[голос: …]`. Пароль `/chat` можно ввести голосом

Вне этих двух режимов голосовые **игнорируются молча** — мы не хотим шумно реагировать на каждое случайное голосовое от ученика.

Аналогичный приоритет действует и для **фото**: если ученик нажал «Отправить фото» в режиме проверки ДЗ (флаг `_AWAIT_PHOTO_ANSWER`), отправленное фото идёт **в проверку**, а не в чат-бот — даже если параллельно жива `/chat`-сессия. Это защищает от бага, когда после рестарта бота лениво ожившая `/chat` уводила фото-ответ к ДЗ в Cursor (он описывал картинку вместо проверки).

#### Сервис `tgzh-stt` (по умолчанию)

`docker compose up -d` поднимает локальный сервис **`tgzh-stt`** (`hwdsl2/whisper-server`, faster-whisper, OpenAI-совместимый POST `/v1/audio/transcriptions`). Бот ходит к нему по имени сервиса в compose-сети: `STT_BASE_URL=http://tgzh-stt:9000/v1`.

- Контейнер `tgzh-stt`, `restart: always`, том **`tgzh-stt-models`** для кэша моделей (на первом старте качается ~465 МБ для `WHISPER_MODEL=small`).
- Healthcheck по `GET /v1/models`. Бот его не ждёт (`condition: service_started`) — пока модель грузится 1–3 минуты, голосовые честно возвращают «не вернул текст»/таймаут, но всё остальное работает.
- Хост-порт `127.0.0.1:9110->9000` — для локальной отладки (`curl http://127.0.0.1:9110/v1/models`); из интернета не доступно. Порт 9100 на хосте занят node_exporter.
- Дефолт `WHISPER_DEVICE=cpu`. На GPU sm_120 (RTX 50xx) ctranslate2 пока без поддержки — оставлен CPU + `int8` + 4 потока. Для других карт можно переключить через `.env` (см. блок `WHISPER_*`).

Альтернатива — внешний провайдер: `STT_BASE_URL=https://api.openai.com/v1`, `STT_API_KEY=sk-...`, `STT_MODEL=whisper-1`. Тогда сервис `tgzh-stt` можно остановить/удалить — `stt_client.is_configured()` смотрит только на `STT_BASE_URL`.

Чтобы отключить фичу — очисти `STT_BASE_URL` и убери сервис из compose; бот будет вежливо отвечать «Распознавание голоса не настроено на сервере».

#### Поток обработки

1. В `bot.main()` зарегистрирован **`MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)`**. Порядок: `_CHAT_PW_WAIT` (пароль голосом) → `_AWAIT_TEXT_ANSWER` (ответ на ДЗ) → ленивый `_CHAT_ACTIVE` из БД (`chat_session_active_until`); иначе — тихий выход.
2. Если STT не сконфигурирован — отвечает «Распознавание голоса не настроено на сервере». Если есть `_CHAT_BUSY` — просит подождать. В режиме ДЗ дополнительно сверяется, что профиль и привязка задания заполнены — иначе сбрасывает `_AWAIT_TEXT_ANSWER` и подсказывает «Сначала укажи задание».
3. Скачивает аудио через **`Bot.get_file(...).download_as_bytearray()`** (Telegram отдаёт максимум ~20 МБ — это лимит Bot API; мы дополнительно проверяем `voice.file_size` заранее).
4. Шлёт байты в **`stt_client.transcribe(audio, mime, filename)`** — это POST на OpenAI-совместимый **`/audio/transcriptions`** (`response_format=json`, `language` по умолчанию `ru`, можно `auto`). Транскрипция вынесена в общий хелпер **`_stt_transcribe_voice_message`**, чтобы оба маршрута делили один и тот же путь скачивания, лимиты и обработку ошибок.
5. Распознанный текст показывается в превью **«🎙 Распознано: «…»»**, затем уходит в **`_run_homework_text_answer_check`** (ДЗ) или **`_handle_chat_user_message`** (`/chat`, с обёрткой STT для модели). Политика **`CHAT_SAFETY_POLICY`** п.0 требует отвечать на STT по смыслу, а не отказывать шаблоном п.3 из-за искажённого распознавания.

#### Конфиг

`.env` / `.env.example`, блок `STT_*` (читается ботом, `stt_client.py`):

| Переменная             | Значение по умолчанию              | Назначение                                                                                              |
|------------------------|------------------------------------|---------------------------------------------------------------------------------------------------------|
| `STT_BASE_URL`         | `http://tgzh-stt:9000/v1`          | OpenAI-совместимый base URL до `/v1`. Пусто — фича выключена. На хосте без compose: `http://127.0.0.1:9110/v1`. |
| `STT_API_KEY`          | пусто                              | Bearer-ключ. Локальный `tgzh-stt` не требует. Для OpenAI/Groq — обязательно.                            |
| `STT_MODEL`            | `whisper-1`                        | `whisper-server` использует свою активную модель из `WHISPER_MODEL` независимо от этого поля.           |
| `STT_LANGUAGE`         | `ru`                               | ISO-код. Спецзначение `auto` — не передавать `language`, дать модели угадать.                           |
| `STT_TIMEOUT_S`        | `60`                               | HTTP-таймаут запроса (clamp 5..600 с).                                                                  |
| `STT_MAX_AUDIO_BYTES`  | `26 214 400` (25 МБ)               | Лимит размера аудио (clamp 64 КБ … 200 МБ). Бот сверяется сам и до запроса.                             |

Параметры самого сервиса `tgzh-stt` (читаются `docker-compose.yml` через `${WHISPER_*}`):

| Переменная             | Значение по умолчанию | Назначение                                                                                              |
|------------------------|-----------------------|----------------------------------------------------------------------------------------------------------|
| `WHISPER_MODEL`        | `small`               | `tiny` ~75 МБ / `base` ~145 МБ / `small` ~465 МБ / `medium` ~1.5 ГБ / `large-v3` ~3 ГБ / `large-v3-turbo` ~1.6 ГБ. |
| `WHISPER_LANGUAGE`     | `ru`                  | Язык по умолчанию. `auto` — автоопределение.                                                            |
| `WHISPER_DEVICE`       | `cpu`                 | `cpu` или `cuda`. На sm_120 (RTX 50xx) держим `cpu`.                                                    |
| `WHISPER_COMPUTE_TYPE` | `int8`                | Для CPU — `int8` (память); для CUDA — `float16`/`float32`.                                              |
| `WHISPER_THREADS`      | `4`                   | Потоки CPU. Не больше физических ядер.                                                                  |
| `WHISPER_BEAM`         | `1`                   | `1` — быстро (greedy), `5` — точнее.                                                                    |

Метрика — **`tgzh_stt_requests_total{outcome}`** (`ok`/`disabled`/`too_large`/`timeout`/`http_error`/`empty`/`other`). Логи бота для каждого голосового пишут только метаданные (длительность, mime, размер, длина распознанного текста) — содержимое распознавания **не** логируется.

Сменить модель: правишь `WHISPER_MODEL` в `.env`, делаешь `docker compose up -d tgzh-stt`. Старая модель остаётся в томе, новая подкачивается на первом запросе. Прогрев — обращение `curl http://127.0.0.1:9110/v1/audio/transcriptions -F file=@1s.wav -F model=whisper-1`.

### Нужен ли работающий инстанс Cursor на ПК?

**Нет.** Декстопный Cursor IDE (Electron) для бота не требуется и в схеме не участвует. Через bridge мы общаемся не с IDE, а с **`cursor-agent` CLI** — отдельным бинарником из [`cursor.com/install`](https://cursor.com/install), который запускается subprocess'ом на машине бриджа (`discourse-cursor-bridge`, см. его `agent_runner.py`). Что важно:

- На хосте бриджа должен быть установлен `cursor-agent` (`curl https://cursor.com/install -fsS | bash` или ручная установка) и **разово выполнен логин** (`cursor-agent login`) — после этого `~/.cursor/agent-cli-state.json` хранит токен, и заходить в IDE для каждого запроса не нужно.
- Декстопное приложение Cursor может быть закрыто/удалено — на работу бриджа это не влияет, у CLI собственная аутентификация.
- Если bridge не настроен или `VLLM_FALLBACK_ENABLE=0`, теряются: `/chat`, sleep памяти, кнопка «Проверить ещё раз (Cursor)». Основная проверка ДЗ через VLLM продолжает работать.

### Минимальный «здоровый» чек-лист

```bash
docker compose ps                # tgzh-bot, tgzh-server, tgzh-stt, postgres → Up (healthy)
curl -fsS http://127.0.0.1:8000/health        # server жив
curl -fsS http://127.0.0.1:9110/v1/models     # tgzh-stt жив, активная модель видна
docker compose exec tgzh-bot getent hosts tgzh-server tgzh-stt   # DNS внутри сети ОК
docker compose exec tgzh-bot alembic current   # схема в актуальной ревизии
# опц.:
curl -fsS http://127.0.0.1:8088/health        # tgzh-preocr (если профиль активен)
curl -fsS "$VLLM_FALLBACK_BASE_URL/models" \
  -H "Authorization: Bearer $VLLM_FALLBACK_API_KEY"     # bridge виден
```

---

### Кнопка «Проверить ещё раз (Cursor)» под результатом

Раньше повторная проверка через Cursor шла отдельной командой **`/cursorask`** с локальным **headless**-CLI **`agent -p`**. Сейчас этого нет: команда **снята**, вместо неё под результатом проверки появляется кнопка **«Проверить ещё раз (Cursor)»**, которая повторно отправляет тот же набор фото на сервер с **`engine=cursor`** в форме **`/check`** (и в JSON **`/check/summarize`**). Сервер форсирует **fallback**-эндпоинт (см. ниже «Опциональный fallback LLM») и возвращает ответ от Cursor; основная LLM (qwen) при этом не дёргается.

- Кнопка показывается под **любым** результатом проверки (фото или текст), пока у бота заданы **`VLLM_FALLBACK_ENABLE=1`** и **`VLLM_FALLBACK_BASE_URL`** (см. **`.env.example`**) — иначе она бесполезна и сбивает ученика
- Бот хранит последнюю попытку: фото в **`context.user_data["last_check_file_ids"]`** или текст в **`["last_check_text_answer"]`** (записи взаимоисключающие — новая попытка снимает другой ключ). При перезапуске процесса бота кнопка скажет «Не нашёл последнюю попытку — загрузи фото или ответь текстом ещё раз»
- На стороне сервера **`engine`** валидируется в **`server._resolve_engine`** (только **`auto`** и **`cursor`**); при **`engine=cursor`** в **`ai_checker._chat_with_fallback`** пропускается primary VLLM и сразу делается запрос к fallback-клиенту (метрика **`tgzh_llm_fallback_total{reason="manual"}`**)
- Cursor отвечает медленнее основной модели — типичный ответ занимает минуту-две; статус-сообщение бота честно об этом предупреждает
- **После основной проверки стикер не отправляется** (ни для фото-, ни для текст-проверки) — пользователь и так видит ✅/❌ в заголовке результата. Стикер-награда теперь привязан только к ветке Cursor: **`_send_recheck_reward_sticker`** шлёт стикер из наградного пула (**`tada`**, без мотивационных) **только** при вердикте **`correct`** от **`homework_check_status.homework_check_stats_result`** по тексту ответа Cursor. При **`partial`/`absent`** (или если нет наградных стикеров) бот вместо стикера выводит **«Повторная проверка завершена. См. результат выше.»**, чтобы чат не оставался молчаливым после долгого ответа cursor-agent

### Скрытая команда `/chat` — стриминговый ИИ-ассистент через Cursor

В **`bot.py`** зарегистрирована «непубличная» команда `/chat` (не входит в **`set_my_commands`**, видна только тому, кто знает). Она открывает диалог с Cursor через тот же **fallback OpenAI-эндпоинт** (см. ниже «Опциональный fallback LLM»), используя `chat.completions` со `stream=True`.

- Доступ закрыт паролем **`CHAT_PASSWORD`** (если пусто — используется **`ADMIN_PASSWORD`**, как у `/begemot`); пусто и там — команда отвечает, что недоступна. Сравнение через **`hmac.compare_digest`**.
- После успешного ввода пароля сессия сохраняется в таблицу **`chat_session`** на **`user_storage.CHAT_SESSION_TTL_DAYS = 365`** суток (миграция Alembic **`009_chat_session`**, столбец в **`scripts/postgres_schema.sql`**). Повторный вход «продлевает» окно (UPSERT). Выход — кнопка **«Выйти из чата»** или команда **`/chat_logout`**.
- Скрытая админ-команда **`/begemot`** ведёт себя симметрично: после успешного пароля сессия пишется в отдельную таблицу **`admin_session`** на **`user_storage.ADMIN_SESSION_TTL_DAYS = 365`** суток (миграция Alembic **`012_admin_session`**, столбец в **`scripts/postgres_schema.sql`**). При следующем `/begemot` пароль не спрашивается — сразу открывается дашборд отзывов. Закрыть досрочно — команда **`/begemot_logout`**. Таблицы независимы: logout одного раздела не выкидывает из другого, пароли можно разводить (`CHAT_PASSWORD` vs `ADMIN_PASSWORD`).
- Каждое следующее сообщение пользователя в чате с ботом, **если** он не находится внутри FSM проверки ДЗ (`_HW_STEP`, `_AWAIT_TEXT_ANSWER` и т. п.), уходит как очередная реплика в Cursor. Активная история живёт **в памяти бота** (24 последние реплики, кнопка **«Новый диалог»** её очищает); по требованию RGPD/этики содержимое реплик в логи не пишется.
- **Сохранённые диалоги.** Inline-меню `/chat` содержит пять действий: **«Новый диалог»**, **«Мои чаты (10 последних)»**, **«Мои чаты — очистить»**, **«Выйти из чата»**, **«Вернуться к проверке ДЗ»**. После каждого ответа Cursor бот делает **`user_storage.chat_dialog_upsert`** в таблицу **`chat_dialog`** (миграция Alembic **`010_chat_dialog`**, лимит **`CHAT_DIALOG_HISTORY_LIMIT = 10`** на пользователя — старейшие записи подрезаются автоматически по `updated_at`). При нажатии **«Новый диалог»** активный `chat_dialog_id` отвязывается от RAM-истории — следующий ответ создаст свежую запись; **«Мои чаты»** показывает заголовки (первая user-реплика, до 60 символов) с локальной датой/временем и количеством реплик; клик на запись подгружает её в RAM (`_CHAT_HISTORY` + `_CHAT_DIALOG_ID`) и можно продолжать переписку. **«Мои чаты — очистить»** удаляет все записи пользователя одним запросом. После рестарта бота активный `dialog_id` теряется в RAM, но диалоги остаются в БД и доступны через «Мои чаты».
- Бот шлёт **`POST /chat/stream`** на сервер (FastAPI `StreamingResponse`, `text/plain`), сервер тут же стримит токены из Cursor. Бот собирает их в буфер и каждые ≈1.2 с делает **`bot.edit_message_text`** одного и того же сообщения (Telegram лимит ~1 edit/c в чате), показывая курсор-«хвостик» **▌**. По окончании финальный текст рендерится конвертером **`telegram_format.markdown_to_telegram_html`**: понимает **`**bold**`**, **`_italic_`**, **`~~strike~~`**, **`` `inline code` ``**, **```` ```fenced``` ````** с языковым тегом, **`[text](url)`** только для **http(s)**/**tg:**, маркер `-` → `• `, заголовки `#…` → `<b>…</b>`. Telegram Markdown как таковой не используется — `parse_mode=HTML`.
- **Фото в `/chat` уходит в Cursor напрямую (без pre-OCR).** Это сознательное исключение из общего правила «Cursor — текстовый ассистент»: `/chat` доступен только админу, и качество ответа на «что это за цветок?» по фото важнее жёсткой текст-only гарантии. Бот скачивает фото, сжимает через **`photo_prepare.prepare_photo_for_upload`**, кодирует в base64 и собирает multimodal user-сообщение OpenAI chat.completions: `[{type: text, text: "<подпись>"}, {type: image_url, image_url: {url: "data:image/jpeg;base64,…"}}]`. Подпись по умолчанию (если её нет) — «Что на фото? Помоги разобрать содержимое.» Сервер **`/chat/stream`** валидирует структуру (только `text` и `image_url`, схема `data:`/`http(s):`, лимиты: ≤4 картинки на сообщение, ≤8 МБ на каждую, ≤8000 символов суммарного текста — `image_url` в подсчёт не входит) и пробрасывает в **`stream_chat_via_cursor`**. Для бриджа в **`discourse-cursor-bridge`** это обычный multimodal-запрос; если бридж/`cursor-agent` фото не понимает, ответом будет честный отказ модели — это ожидаемо, переключаться обратно на OCR не нужно. В RAM/DB-историю кладётся **только текстовый плейсхолдер `[фото: <подпись>]`** (или `[фото без подписи]`) — base64 в `chat_dialog.history_json` не уходит и не пересылается повторно. Альбомы — по одной картинке за раз, флаг `_CHAT_BUSY` отсекает параллельные запуски. Никакого `PREOCR_URL` для этого пути не требуется.
- Тонкие настройки: **`CHAT_SYSTEM_PROMPT`** (системное), **`CHAT_MAX_HISTORY_TURNS`** (2..64, default 12), **`CHAT_TEMPERATURE`** (0..2, default 0.7), **`CHAT_MAX_RESPONSE_TOKENS`** (128..8192, default 2048). Тайм-аут на стрим — общий **`BOT_CHECK_TIMEOUT_CURSOR_SEC`** (default 360 с).
- **Политика `/chat` — нерасторжимый префикс system_prompt.** Поверх любого **`CHAT_SYSTEM_PROMPT`** бот и сервер всегда подмешивают константу **`CHAT_SAFETY_POLICY`** (**`ai_checker.CHAT_SAFETY_POLICY`** ≡ **`bot._CHAT_SAFETY_POLICY`**, синхронизированы; тест **`test_bot_and_server_safety_policy_are_in_sync`** ловит рассинхрон). Префикс **нельзя выключить через env** — защита от случайного ослабления при кастомном **`CHAT_SYSTEM_PROMPT`**. Содержание политики (кратко):
  - **П.0 — multimodal-контент:** изображения, текст и STT из **текущего** сообщения обязаны разбирать по существу (распознавание, описание, ответ на вопрос). Временные пути вида **`/tmp/cursor-openai-sandbox/...`**, **`file://`**, **`data:image/...;base64,...`**, пришедшие вместе с сообщением, считаются **вложением пользователя**, а не «произвольным файлом сервера» — отказы вида «не могу открыть по пути», «работаю только по OCR», «прикрепите фото» при реально переданном вложении запрещены.
  - **П.1 — запрет произвольных действий на сервере:** shell/CLI, листинг каталогов, чтение чужих файлов, env, процессы, сеть, скачивание URL.
  - **П.2 — узкое исключение по пути вложения:** можно читать только **этот** файл как вход модели; нельзя листать соседние файлы, подниматься по дереву каталогов, угадывать чужие пути; в ответе пользователю не светить абсолютные пути сервера.
  - **П.3 — отказ на просьбы из п.1** одной фразой: **«Не могу: разрешено только обычное общение и разбор того, что вы прислали в сообщении»**. Просьбы «посмотри фото / проверь ДЗ по фото» относятся к п.0, а не к п.1.
  - **П.4 — не выдумывать** результат shell/чтения файлов «по памяти»; описание присланной картинки и текста из сообщения этим **не** ограничено.
  - **П.5 — без мета-преамбул:** не писать «сначала загружу инструкции», «теперь посмотрю фото», «приступаю к разбору» и т.п. — сразу ответ по существу.
  Бридж с **`cursor-agent`** имеет полный набор IDE-тулов в sandbox; единственный рычаг ограничения здесь — **`system_prompt`**; тесты **`test_chat_safety_policy_*`** проверяют наличие префикса и ключевой подстроки **`multimodal-контент`**.
- **Контекст `/chat`.** Долговременная «сон-память» удалена: бот больше не ведёт файлы `data/memory/` и не делает фоновых «снов». Контекст диалога — это RAM-история текущей переписки (последние реплики) плюс сохранённые диалоги, доступные через **«Мои чаты»** (таблица `chat_dialog`, до 10 на пользователя). Между сессиями факты о пользователе не переносятся.

---

## Как подключить бота к Telegram

### 1. Создать бота

1. Открой Telegram и найди **@BotFather**.
2. Отправь команду `/newbot`.
3. Введи имя бота (например: `Домашка Математика`).
4. Введи username бота (например: `math_homework_checker_bot`). Username должен заканчиваться на `bot`.
5. BotFather пришлёт **токен** вида:
   ```
   7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
6. Сохрани токен — он нужен для `BOT_TOKEN` в `.env`.

### 2. Настроить переменные окружения

Создай файл `.env` в корне проекта:

```env
BOT_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SERVER_URL=http://localhost:8000
PORT=8000
LOG_LEVEL=INFO
AI_MOCK=1
USER_DB_PATH=data/users.sqlite
GDZ_CATALOG_PATH=data/gdz_matematika_textbooks.json
GDZ_CACHE_DIR=data/gdz_cache

# Для реальной проверки через VLLM (см. раздел ниже)
# VLLM_BASE_URL=http://...
# VLLM_API_KEY=...
# VLLM_MODEL=qwen/qwen3-vl-8b
# AI_MOCK=0
# Опционально pre-OCR (см. раздел Docker, профиль preocr): PREOCR_URL=http://tgzh-preocr:8088
# Опционально TEI для тона/эмоций отзывов (см. раздел "TEI и отзывы"): TEI_SENTIMENT_URL, TEI_EMOTION_URL
```

- `BOT_TOKEN` — токен от BotFather (обязательно)
- **`TG_MODE`** — режим приема апдейтов Telegram: **`webhook`** (по умолчанию, нужен публичный inbound HTTPS до бота через KeenDNS/роутер, см. **`TELEGRAM_WEBHOOK_*`**) или **`polling`** (исходящий **`getUpdates`** до **`api.telegram.org`**). При блокировке Telegram провайдером и egress через VPS/прокси рекомендуется **`TG_MODE=polling`**. При **`TG_MODE=polling`** бот снимает webhook при старте; для возврата на вебхук после починки inbound port-forward выставь **`TG_MODE=webhook`** и передеплой **`tgzh-bot`**
- `LOG_LEVEL` - уровень логов в stderr для бота и `server.py` (`DEBUG`, `INFO`, `WARNING`, `ERROR`)
- `SERVER_URL` — адрес сервера проверки. При локальном запуске: `http://localhost:8000`
- `AI_MOCK=1` — тестовая заглушка; **`AI_MOCK=0`** и заданные **`VLLM_*`** — запросы к VLLM
- `USER_DB_PATH` — путь к файлу SQLite с выбранным учебником пользователя (файл создается при первом выборе), если не задан **`DATABASE_URL`**
- **`DATABASE_URL`** — опционально, строка подключения **PostgreSQL** (например `postgresql://user:pass@host:5432/dbname`). Если задана, **`user_storage`** и **`bot_stats`** используют эту БД; путь **`USER_DB_PATH`** для подключения не используется (его можно оставить в **`.env`** для совместимости). Схему можно создать запуском бота (**`init_db`** / **`init_stats`**) или **`alembic upgrade head`** при **`DATABASE_URL`**, либо **`psql`** и файл **`scripts/postgres_schema.sql`**. Разовый перенос с SQLite: **`scripts/import_sqlite_to_postgres.py`** (нужны **`DATABASE_URL`** и путь к файлу SQLite)
- **`METRICS_PORT`** — если задано положительное число, процессы **`bot.py`** и **`server.py`** поднимают HTTP **`/metrics`** для **Prometheus** на этом порту (**`METRICS_BIND`**, по умолчанию **`0.0.0.0`**). Пример дашборда: **`grafana/dashboards/tgzh-overview.json`** (импорт в Grafana, выбери свой источник Prometheus)
- `GDZ_CATALOG_PATH` — JSON со списком учебников для кнопок бота
- `GDZ_CACHE_DIR` — кеш «Показать ГДЗ» (пусто / `false` / `off` — без кеша)
- **`VLLM_MODEL`** — идентификатор модели для API и **подпись в конце ответа проверки** (строка **`Модель: …`** в тексте, который видит ученик; в **`AI_MOCK=1`** добавляется **`(тестовый режим)`**)
- **`PREOCR_URL`** — если не пусто, перед VLLM для фото вызывается сервис предварительного OCR; при успешном ответе в результат проверки добавляется строка про использование **pre-OCR** (подробности — **`.env.example`**, раздел VLLM в README)
- **`TEI_SENTIMENT_URL`**, **`TEI_EMOTION_URL`** — опционально, базовые URL сервисов **Text Embeddings Inference** (классификация тональности и эмоций). Пусто - анализ не запускается. См. раздел **"TEI и отзывы"** ниже

### TEI и отзывы

Если задан хотя бы один из **`TEI_SENTIMENT_URL`** или **`TEI_EMOTION_URL`**, после дописывания текста в тикет обратной связи (сценарий 👎 под результатом проверки) бот асинхронно вызывает **`POST {base}/predict`** с телом **`{"inputs": "<текст>", "truncate": true}`** (контракт в духе Hugging Face TEI). Текст обрезается до **2000** символов, таймаут HTTP **20** с. Результат сохраняется в таблице **`feedback_ticket_nlp`** (миграция **`007_feedback_ticket_nlp`**) и показывается админу в **`/begemot`** блоком **"Тональность и эмоции"** (эмодзи-метки, лейбл sentiment, список эмоций; при сбое - строка **`TEI: …`**).

Запросы к TEI выполняет **`feedback_tei.py`** через общий транспорт **`tgzh_httpx.async_http_transport_ipv4_lookup()`** (предпочтение **IPv4** при резолве имени — тот же класс проблем **Docker/DNS**, что и для вызовов бота к **`SERVER_URL`**). Если недоступен только один из двух URL, в админке может отобразиться частичный результат и текст ошибки по второму сервису; если оба недоступны и данных нет — в **`badges`** остается символ **U+2754** (как в **`feedback_tei`**) и склеенное сообщение об ошибке в поле **`error`**

В **Docker** не задавай **`http://localhost:…`** для TEI на хосте: **`localhost`** внутри контейнера — это сам контейнер. Используй имя сервиса в **`docker-compose`**, IP хоста или **`host.docker.internal`** (где поддерживается). Примеры в **`.env.example`**.

Проверки: **`tests/test_feedback_tei.py`** (разбор ответа, сетевые ошибки, тело запроса), **`tests/test_feedback_nlp.py`** (запись в БД), **`tests/test_bot_feedback_nlp_html.py`** (HTML блока в боте).

### Требования к сети (бот и сервер проверки)

На одной VPS или в Docker часто запускают и **бот**, и **сервер проверки**. Им нужен **разный** исходящий доступ

- **Процесс Telegram-бота (`bot.py`)** должен иметь **исходящий HTTPS (порт 443)** до **Telegram Bot API** (как правило **`api.telegram.org`**, плюс связанные с Bot API хосты у Telegram). Без этого не работают polling, приём обновлений и отправка сообщений в чат
- **Сервер проверки (`server.py`) к Telegram API не обращается** — ему не нужен доступ к `api.telegram.org` для своей работы
- Бот дополнительно вызывает **`SERVER_URL`** (наш backend: **`POST /check`** и при необходимости проверки доступности API)
- Сценарий **«Показать ГДЗ»** и разбор оглавления с **gdz.ru** выполняются **из процесса бота** (исходящий HTTPS на сайт-источник)
- При **`AI_MOCK=0`** процесс **`server.py`** должен достигать **`VLLM_BASE_URL`** по сети; при **`AI_MOCK=1`** для проверки ДЗ внешний VLLM не требуется
- Если задан **`PREOCR_URL`**, **`tgzh-server`** дополнительно ходит на **`POST …/v1/preocr`** того же хоста (в Docker — имя сервиса **`tgzh-preocr`**, порт **8088**)
- При заданных **`TEI_*_URL`** процесс **`bot.py`** должен достигать этих хостов по HTTP (**`POST /predict`**); **`server.py`** к TEI не обращается

Если до Telegram с машины бота нет прямого исходящего **443**, задай **`TELEGRAM_PROXY`** в **`.env`** (см. абзац **«Таймаут при старте бота»** ниже и **`.env.example`**)

#### KeenDNS (inbound webhook) и VPS egress (outbound polling)

Два независимых сетевых пути:

- **Inbound webhook** (`TG_MODE=webhook`): Telegram шлет POST на публичный hostname (например KeenDNS **`tgzh.example.netcraze.pro`**), который указывает на **IP роутера**; далее **`:443`** -> reverse proxy -> **`tgzh-bot:8081`**. Egress-IP VPS **не** подставляется в KeenDNS - это другой путь
- **Outbound polling** (`TG_MODE=polling`): бот сам ходит в **`api.telegram.org`** через исходящий канал (VPS, **`TELEGRAM_PROXY`** и т.п.) - штатный режим, если провайдер блокирует Telegram

Чек-лист возврата на webhook (если inbound починен):

1. KeenDNS оставить на роутере (не менять на egress-IP VPS)
2. Снаружи (не из LAN): **`curl -m 15 -o /dev/null -w "%{http_code}\n" -X POST https://<hostname>/telegram/webhook`** - ожидаем **403**
3. **`TG_MODE=webhook`** в **`.env`**, передеплой **`tgzh-bot`**

### 3. Запуск

**Вариант A: Локально**

```bash
# Терминал 1
python3 server.py

# Терминал 2
python3 bot.py
```

**Вариант B: На сервере (VPS)**

1. Размести проект на VPS.
2. Запусти сервер и бота (через systemd, screen или docker).
3. В `.env` укажи `SERVER_URL=http://localhost:8000` (если бот и сервер на одной машине) или `http://IP_СЕРВЕРА:8000`.

**Вариант C: ngrok для теста с локальной машины**

Если сервер на твоём компьютере, а бот должен быть доступен извне:

```bash
ngrok http 8000
```

В выводе будет URL вида `https://xxxx.ngrok.io`. В `.env` для бота укажи:

```env
SERVER_URL=https://xxxx.ngrok.io
```

**Вариант D: Docker**

В корне есть **`docker-compose.yml`**, **`Dockerfile.server`**, **`Dockerfile.bot`**: сервисы **`postgres`**, **`tgzh-server`**, **`tgzh-bot`** в общей сети **`tgzh-internal`** (встроенный DNS Compose для имени **`tgzh-server`**). **`tgzh-server`** публикует API на **`127.0.0.1:8000`** хоста, **`tgzh-bot`**, **`restart: always`**. Сервис **`postgres`** (том **`tgzh-pg`**) опционален для **`DATABASE_URL`** в **`.env`**; без него бот по-прежнему хранит данные в SQLite в томе **`tgzh-data`**. JSON каталога учебников вшит в образ (**`/app/static`**), **SQLite (профили и статистика)** и **кеш GDZ** лежат в именованном томе **`tgzh-data`**, смонтированном в **`/app/data`** у **`tgzh-bot`**: при **удалении и пересоздании контейнера** данные сохраняются, пока том не удален (**`docker compose down -v`** удалит том и все записи). При необходимости замени том на каталог на хосте, например **`./data:/app/data`**. Для **`tgzh-bot`** задано **`SERVER_URL=http://tgzh-server:8000`** (имя сервиса API во внутренней сети Docker; **`network: host` в файле относится к этапу сборки**, не к рантайму контейнеров); пути **`USER_DB_PATH`**, **`GDZ_CATALOG_PATH`**, **`GDZ_CACHE_DIR`** и **`STATS_TIMEZONE`** — в **`docker-compose`**

```bash
docker compose up -d --build
```

<a id="homework-stack-smoke"></a>

**motok hub + LM Studio (smoke):** хаб на mf (`../motok/local/hub.env`), в **`.env`** — `MOTOK_HUB_*` как в хабе. LM Studio на `127.0.0.1:1234`. Сервер в host-сети (иначе bridge не видит localhost LM Studio):

```bash
export MOTOK_INTERNAL_TOKEN MOTOK_HUB_TOKEN_SECRET   # из hub.env
# в .env для tgzh-bot в Docker: MOTOK_HUB_URL=http://host.docker.internal:54326
export SERVER_URL=http://host.docker.internal:8000 # если tgzh-server в host network (lmstudio.yml)
export VLLM_BASE_URL=http://127.0.0.1:1234/v1
export VLLM_MODEL=qwen3.8-27b-xs-16gb-vram   # id из GET /v1/models
export VLLM_VISION=0 VLLM_FALLBACK_ENABLE=0
docker compose -f docker-compose.yml -f docker-compose.lmstudio.yml up -d --build tgzh-server postgres
bash scripts/smoke_homework_stack.sh
```

**Предварительное OCR (опционально)**

Сервис **`tgzh-preocr`** (каталог [`preocr/`](preocr/), по умолчанию [`Dockerfile.preocr`](Dockerfile.preocr) — CPU; для GPU есть [`Dockerfile.preocr.gpu`](Dockerfile.preocr.gpu)) поднимается профилем Compose. Как пользоваться:

- В **`.env`** задай **`PREOCR_URL=http://tgzh-preocr:8088`** (имя сервиса во внутренней сети; пустой URL — OCR не вызывается, в VLM уходит только картинка). Использует **`tgzh-server`** при проверке **только для `image/*`** (**`preocr_client`** → **`ai_checker`**); сценарий **«Ответить текстом»** и файлы **`text/plain`** (и соседние текстовые MIME) идут **сразу в LLM**, без pre-OCR. Если у части multipart **потерян** тип и пришло **`application/octet-stream`**, сервер по байтам распознает валидный **UTF-8** как текст (**`ai_checker._mime_from_bytes`**), чтобы не подставлять **`image/jpeg`** и не дергать OCR
- Поднять стек с OCR: **`docker compose --profile preocr up -d --build`**. Без запущенного **`tgzh-preocr`** при непустом **`PREOCR_URL`** проверка фото на сервере будет падать на вызове OCR — либо поднимай профиль, либо очисти **`PREOCR_URL`**
- В **`docker-compose.yml`** у **`tgzh-server`** задано **`depends_on`** на **`tgzh-preocr`** с **`required: false`**: при активном профиле **`preocr`** сервер стартует после контейнера OCR; без профиля зависимость не мешает обычному **`docker compose up`**

Транспорт: перед запросом к LLM для **`image/*`** в промпт подмешивается текст из **`merged_markdown`**. У сервиса **`restart: always`**. HTTP API: **`POST /v1/preocr`** (multipart **`image`**), ответ JSON с **`regions`** и **`merged_markdown`**; Swagger — **`http://127.0.0.1:8088/docs`**. Том **`tgzh-preocr-models`** кеширует веса под **`/root/.paddlex`**. Параметры пайплайна — **`.env.example`** (**`PREOCR_PIPELINE`**, **`PREOCR_MAX_SIDE`**, **`PREOCR_OCR_LANG`**, **`PREOCR_TIMEOUT_SEC`**, **`VLLM_PROMPT_PREOCR_BLOCK`**)

В ответе проверки в боте (текст с сервера) в конце, перед строкой **`Модель: …`**, при успешном непустом OCR появляется фраза **`Предварительное распознавание текста (pre-OCR) использовано.`** — по ней видно, что блок OCR попал в промпт. Если **`PREOCR_URL`** пустой, сервис OCR недоступен или **`merged_markdown`** пустой, этой строки не будет

**Водяной знак gdz.ru / гдз.ру в pre-OCR** автоматически вычищается из ответа OCR (**`preocr_client.strip_gdz_watermark`**) — варианты `gdz.ru`, `gdz ru`, `gdzru`, `гдз.ру`, `гдз ру`, `гдзру` (любая раскладка/регистр) удаляются ДО передачи в LLM, чтобы они не попадали ни в основную проверку, ни в **«Проверить ещё раз (Cursor)»** как «текст ученика». Дополнительно в самом промпте проверки (**`VLLM_PROMPT_IGNORE_GDZ`**) есть напоминание модели игнорировать водяной знак.

**Повторная проверка через Cursor по фото — только через pre-OCR.** Cursor-bridge ходит к `cursor-agent`, который текстовый, поэтому base64-картинку он бы всё равно «не увидел». При нажатии **«Проверить ещё раз (Cursor)»** для фото сервер сначала зовёт **`POST /v1/preocr`** и отправляет в Cursor **только текст** (pre-OCR-блок + та же инструкция со сверкой по `gdz_task_condition` с gdz.ru). Если **`PREOCR_URL`** не настроен или OCR пуст, recheck вернёт явное сообщение «не удалось получить OCR… Cursor работает только с текстом» — без обращения к bridge. Основная проверка (qwen) по-прежнему получает и картинку, и pre-OCR-блок одновременно

**CPU (дефолт в compose):** в **`docker-compose.yml`** значение **`PREOCR_DOCKERFILE=${PREOCR_DOCKERFILE:-Dockerfile.preocr}`** и блока **`gpus: all`** **нет**. Образ работает на любой машине без CUDA; первый вызов прогревает PaddleOCR-модели в lifespan-хуке (см. **`preocr/app.py`**, **`preocr/engine.warm_up`**)

**GPU (опционально):** создай **`docker-compose.override.yml`** в корне:

```yaml
services:
  tgzh-preocr:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    runtime: nvidia
    environment:
      PREOCR_DOCKERFILE: Dockerfile.preocr.gpu
```

Также положи в **`.env`** **`PREOCR_DOCKERFILE=Dockerfile.preocr.gpu`** и (опционально) **`PADDLE_GPU_IMAGE`** под нужную CUDA, например **`paddlepaddle/paddle:3.2.2-gpu-cuda12.6-cudnn9.5`** (другие теги — на Docker Hub **`paddlepaddle/paddle`**: **`3.2.2-gpu-cuda11.8-cudnn8.9`**, **`...-cuda12.9-cudnn9.9`**, **`...-cuda13.0-cudnn9.13`**). Нужны драйвер NVIDIA и **[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)**. **Известное ограничение:** PaddlePaddle 3.2.2 поддерживает sm_75…90; на **sm_120 (Blackwell, RTX 50xx)** запуск падает — оставь CPU-дефолт

Деплой на удалённый хост по SSH: если в корне проекта есть **`.env`**, скрипт **`scripts/deploy-docker.sh`** копирует его на сервер перед **`docker compose`**; иначе положи **`.env`** в каталог деплоя на сервере вручную

```bash
export DEPLOY_HOST=user@example.com
# опционально: профиль preocr (сервис tgzh-preocr), иначе в скрипте поднимаются только postgres, tgzh-server, tgzh-bot
# export DEPLOY_COMPOSE_PROFILE=preocr
./scripts/deploy-docker.sh
```

Опционально **`DEPLOY_REMOTE_PATH`** (по умолчанию **`~/tgzh-docker`**) — каталог на сервере. Переменные деплоя описаны в конце **`.env.example`** (их не нужно класть в **`.env`** на сервере, только экспортировать перед запуском скрипта на своей машине)

**Обновление после изменений в репозитории**

- **Локальный передеплой** (из корня репозитория): **`docker compose build && docker compose up -d --force-recreate`**. Сервис **pre-OCR**: **`docker compose --profile preocr build && docker compose --profile preocr up -d --force-recreate`**
- Локально или на сервере без скрипта: **`docker compose build && docker compose up -d`** — то же, что **`docker compose up -d --build`** в блоке выше; при необходимости полной пересборки контейнеров добавь к **`up`** флаги **`--force-recreate`**
- При **PostgreSQL** после выкладки с новыми миграциями: **`docker compose exec tgzh-bot alembic upgrade head`** (в образе **`tgzh-bot`** есть **`alembic`** и файлы миграций; **`DATABASE_URL`** из **`.env`**)
- Скрипт деплоя: **`DEPLOY_RUN_ALEMBIC=1`** вместе с **`DEPLOY_HOST`** — после **`up`** выполнить **`alembic upgrade head`** на сервере (имеет смысл только при заданном **`DATABASE_URL`**)
- Если после точечного перезапуска контейнеров пропал резолв **`tgzh-server`** из бота или **`docker network inspect`** не показывает **`tgzh-server`** с IPv4 на **`tgzh-internal`**: **`docker compose down`** и снова **`docker compose up -d`** (или полный **`--force-recreate`** всего стека)

Если при **`docker compose build`** на VPS pip не видит PyPI (**Temporary failure in name resolution**), проверь DNS на хосте; в **`docker-compose.yml`** для этапа сборки задано **`network: host`**, чтобы сборка использовала сеть хоста

**Ошибка при «Проверить» фото: `[Errno -3] Temporary failure in name resolution`**

Сообщение **«Ошибка связи с сервером»** с этим текстом значит, что бот не смог разрешить **имя хоста из `SERVER_URL`** (запрос к **`POST /check`** не дошел до API)

- В **Docker Compose** имя **`tgzh-server`** видно **только внутри** сети проекта. В **`docker-compose.yml`** у сервиса **`tgzh-bot`** задано **`environment: SERVER_URL: http://tgzh-server:8000`**, оно **перекрывает** строку **`SERVER_URL`** из **`.env`** для контейнера бота. Проверка с хоста: **`docker compose exec tgzh-bot getent hosts tgzh-server`** и **`docker compose exec tgzh-bot python3 -c "import urllib.request; print(urllib.request.urlopen('http://tgzh-server:8000/health', timeout=5).read())"`** (или **`curl`** в контейнере, если установлен)
- Если бот запускаешь **на хосте** (**`python3 bot.py`**), в **`.env`** укажи **`SERVER_URL=http://127.0.0.1:8000`** (или URL через Nginx), **не** **`http://tgzh-server:8000`** - на хосте **`tgzh-server`** обычно **не** резолвится и дает **-3**
- Если в **`SERVER_URL`** внешний домен и ошибка в контейнере: проверь DNS внутри контейнера (**`/etc/resolv.conf`**, доступность резолвера); при необходимости в **`docker-compose.yml`** для **`tgzh-bot`** можно задать **`dns:`** (например публичный резолвер), согласовав с политикой сети
- Если **`getent hosts tgzh-server`** в контейнере бота показывает IPv4, а **`python3 -c "import socket; socket.getaddrinfo('tgzh-server', 8000)"`** падает с **-3**, а с **`AF_INET`** — нет: в коде бота для вызовов к **`SERVER_URL`** используется транспорт **httpx** с резолвом через **IPv4** (см. **`bot.py`**)

**Nginx + HTTPS (тот же домен, что и сайт)**

Чтобы API проверки (**`/check`**, **`/health`**) было снаружи по HTTPS и не занимало корень сайта, добавь в существующий **`server { listen 443 ssl; ... }`** блок **`location`** из **`deploy/nginx-tgzh-api.location.conf.example`**: префикс **`/tgzh-api/`** проксируется на **`127.0.0.1:8000/`** (куда слушает **`tgzh-server`** в Docker). Тогда проверка: **`curl -fsS https://ВАШ_ДОМЕН/tgzh-api/health`**

**Таймаут при старте бота (`get_me`, `Timed out`)**

Это сбой доступа к **Telegram Bot API** (**`api.telegram.org`**, исходящий **443**), а не к нашему **`server.py`**. См. раздел **«Требования к сети»** выше. Проверка с хоста, где крутится бот: **`curl -4 -m 10 https://api.telegram.org/`**. Если прямой доступ закрыт, задай **`TELEGRAM_PROXY`** в **`.env`** — HTTP(S) или SOCKS5-прокси до Telegram (см. **`.env.example`**). Для SOCKS5 установи **`httpx[socks]`** в окружении бота (или соответствующие пакеты в образе)

---

## Использование бота

1. Найди бота в Telegram по username
2. Отправь `/start`. Если учебник еще не выбран - выбери **класс** (по каталогу, обычно **6–11**), затем **учебник** в списке (при длинном списке — «Далее» / «Назад»). Если учебник уже сохранен, `/start` сразу открывает шаг выбора **параграфа**. Ровно у **одного** учебника в списке этого класса показывается **один** 🔥 — у того, кого больше всего выборов в базе профилей (при равенстве берется первый по порядку в каталоге); это подсказка ученику, если не уточняли у учителя, какой учебник открыть. Команды в меню (сверху вниз): **`/start`**, **`/shot`**, **`/chat`**, **`/textbook`** (**/textbook** — последняя строка меню) доступны слева от поля ввода (кнопка **Menu** / иконка у имени бота / список при вводе `/`); отдельной **reply**-клавиатуры с крупными кнопками команд под полем ввода нет. **`/stats`** в меню не показывается — только если ввести команду вручную. Статистика общая по всем пользователям, **по учебным годам** (с **1 сентября** по **31 августа** следующего года, граница по **`STATS_TIMEZONE`**, в Docker по умолчанию **Europe/Moscow**): сколько фото загружено, проверок завершено, технических сбоев, две строки по тексту модели (**✅** частично верные с замечаниями, **❌** нет решения на листе; счетчик полностью верных ведется в **`bot_stats`**, в тексте **`/stats`** отдельной строки нет), **итоги 👍/👎** под результатом проверки и блок **опроса `/polling`** с **полосками** Да/Нет (данные в той же БД, что профили: SQLite по **`USER_DB_PATH`** или PostgreSQL по **`DATABASE_URL`**). В **`/stats`** в блоке посещаемости: **график** по дням за **7** дней (полоски в моноширинном блоке Telegram **pre**) и **число** уникальных пользователей за **30** дней без графика; граница дня — **`STATS_TIMEZONE`**; данные в **`bot_user_visit_day`**. Команда **`/stats`** под сообщением **без** inline-кнопок, если учебник уже выбран (только меню команд) Замечания по **👎** под результатом проверки сохраняются в БД (**таблица `user_feedback`**, тикет **`feedback_ticket`**): **одна строка на пользователя**, новые сообщения **дописываются** в поле **`body`** через внутренний разделитель; у каждого фрагмента своя метка времени. При заданных **`TEI_SENTIMENT_URL`** и/или **`TEI_EMOTION_URL`** бот вызывает **Text Embeddings Inference** (**`POST /predict`**), пишет **`feedback_ticket_nlp`** и **эмоджи-метки** тона и раздражения. Админ в **`/begemot`** видит **список по датам** (и полный текст по кнопке с **user id**). Скрытая команда **`/begemot`** в меню не показывается; пароль **`ADMIN_PASSWORD`** в **`.env`**; сессия админа сбрасывается при **`/start`** и **`/textbook`**
3. Укажи задание (после выбора учебника бот сам попросит): **параграф** кнопками **§1…§N** (N по оглавлению gdz.ru) или **«Свой номер»** текстом; после параграфа бот подгружает оглавление и на шаге 2 предлагает **«Выбрать упражнение»** — цифры **только если из них еще можно собрать номер из списка упражнений этого параграфа** (например при максимуме 198 после первой цифры **2** не будет лишних вариантов для «трехзначного» ввода), подтверждение **«Готово»**; **страницы проверочных** из оглавления — сначала кнопка **«Выбрать проверочную»**, затем отдельный экран с кнопками **номера страницы** (**N**, без префикса "стр.") (потолок **`GDZ_VERIF_BUTTONS_MAX`**, **`0`** — без обрезки, с осторожностью к лимиту Telegram ~100 кнопок); **«или Ввести номер»** — клавиатура **1–999**; номер упражнения или страницу по-прежнему можно **ввести текстом**. После сохранения привязки бот подгружает со страницы задания на gdz.ru **текст условия** (блок как в учебнике) и присылает его отдельным сообщением, если удалось извлечь. Пока задание не заполнено, **«Загрузить фото»** нет — есть **«Указать задание»**
4. После привязки задания доступны **«Загрузить фото»**, **«Ответить текстом»** и **«Показать ГДЗ»** (бот подтягивает условие и картинки с gdz.ru; при повторном запросе того же задания от любого пользователя используется **кеш на диске** `GDZ_CACHE_DIR`, без повторной загрузки с сайта)
5. **Вариант А — фото.** Нажми **«Загрузить фото»** и отправь фото тетрадного листа кнопкой скрепки 📎 в поле ввода (на шаге ожидания фото есть **«Назад»**). Можно выбрать **несколько снимков одним альбомом** (Telegram сгруппирует их): бот ждет короткую паузу, затем шлет **одно** сообщение «Получено фото: N» с клавиатурой **под последним** кадром альбома, без дублирования кнопок на каждый снимок
6. **Вариант Б — текст.** Нажми **«Ответить текстом»**: бот по возможности подтягивает условие с gdz.ru и показывает заголовок **«Условия задачи (можно скопировать и дописать решение)»** и **один** блок **`<pre>`** (в Telegram у блока есть копирование). Подпункты **а)**, **б)** … или **1)**, **2)** … в условии размечаются с пустыми строками между пунктами. Ниже — просьба написать решение **одним сообщением** (до порядка **15000** символов) и курсивом подсказка про отмену кнопкой **«Назад»**. Ранее сохраненные в памяти бота **file_id** фото для проверки сбрасываются. Бот вызывает **`POST /check`** с телом **`text/plain`** (тот же набор полей формы, что и для фото); на сервере pre-OCR вызывается **только** для **`image/*`**, текст уходит **напрямую** в LLM (при потере MIME см. раздел **«Предварительное OCR»** про сниффинг **UTF-8**). Отдельное сообщение с условием после сохранения задания (если бот его уже прислал) может стоять выше в чате — это тот же текст, что в **`<pre>`**
7. Если шел сценарий с фото — нажми **«Проверить»**. Список **file_id** загруженных снимков хранится в памяти процесса бота до смены учебника или сброса задания. Бот готовит каждое фото по очереди (поворот по EXIF, длинная сторона не больше 1024 px, JPEG) и для каждого вызывает **`POST /check`**; если у **`tgzh-server`** задан **`PREOCR_URL`**, перед обращением к VLM для **`image/*`** может вызываться **`POST …/v1/preocr`** и в промпт подмешивается **`merged_markdown`**. При **двух и более** фото сервер затем вызывает **второй** запрос к LLM (**`POST /check/summarize`**, только текст) и показывает **одну** сводку. Если сводка недоступна, в чате остаются блоки **Фото 1…N** с ответами модели по каждому снимку и префикс по **`format_merged_check_prefix`** в **`homework_check_status.py`**
8. Получи результат проверки: перед **«Результат проверки:»** в чате ✅, если на работе есть разбор (модель не считает, что решения нет), и ❌, если по тексту похоже, что решения на фото нет или не видно. Числовой процент в сообщении не показывается. **Текст разбора** от модели идет **первым**; сервер дописывает **в конец** (перед хвостовой меткой смешанных чисел, если она есть - см. **`detach_trailing_mixed_numbers_marker`** в **`homework_check_status.py`**) блок: при успешном pre-OCR — **«Предварительное распознавание текста (pre-OCR) использовано.»**, всегда — **`Модель: …`** (**`VLLM_MODEL`**; в mock - с **`(тестовый режим)`**). Если в задании **смешанные числа**, модель может добавить в самый конец сырого ответа метку **`[tgzh_mixed_numbers]`** - для статистики это **partial**, при показе ученику метка снимается (**`strip_homework_check_machine_tags`**). Затем **пустая строка**, оговорка *курсивом* и **«Оцени ответ»** с кнопками **👍** / **👎** (**один** голос на сообщение; **👎** предлагает дописать замечание; итоги в **`/stats`**, при **`METRICS_PORT`** - **`tgzh_check_feedback_total`** для Grafana). Фрагменты **`**текст**`** - жирный (**`telegram_format.markdownish_to_telegram_html`**). После проверки бот шлет **один** стикер из объединенного пула (**`STICKER_SET_POOL`** или отдельно **`TADA_STICKER_SET_NAMES`** / **`MOTIVATION_STICKER_SET_NAMES`** - см. раздел про стикеры ниже и **`check_sticker_reward`**)

Пока идет проверка, загрузка **«Показать ГДЗ»** или сохранение шагов задания, периодически показывается статус **"печатает…"** — он означает, что бот не завис

Выбор учебника и привязка к заданию хранятся в SQLite (вместе с глобальной статистикой в **`bot_stats.py`**). **«Выбрать упражнение или проверочную»** сбрасывает параграф/упражнение/страницу и просит ввести заново. Сменить учебник — команда **`/textbook`** из меню команд у поля ввода

Если в **`.env`** задано **`DISCLAIMER_VERSION`** и заполнены **`DISCLAIMER_TEXT`**, **`DISCLAIMER_QUIZ_QUESTION`**, **`DISCLAIMER_QUIZ_CORRECT`** и минимум два непустых **`DISCLAIMER_QUIZ_OPT_0`…`3`**, после **`/start`** сначала показываются условия и викторина (inline), затем кнопка принятия; запись хранится в SQLite (таблица **`user_consent`**). **`DISCLAIMER_QUIZ_CORRECT`** - целое **0..3**, совпадающее с суффиксом **`DISCLAIMER_QUIZ_OPT_N`** (верный текст у **`OPT_1`** значит **`CORRECT=1`**, не «вторая кнопка сверху»). После смены верного индекса или текстов вариантов увеличивай **`DISCLAIMER_VERSION`**, иначе старые кнопки в чате перестанут совпадать с конфигом (бот подскажет **`/start`**). Увеличение версии заставляет пройти блок заново. Команды **`/textbook`**, **`/stats`**, **`/chat`** до принятия отвечают подсказкой открыть **`/start`**. Пустой **`DISCLAIMER_VERSION`** отключает гейт; **`DISCLAIMER_SKIP=1`** только для отладки (в прод не включать)

---

## Динамические inline-кнопки (подходы в `bot.py`)

Ниже - как устроена генерация клавиатур и обработка нажатий, без дублирования кода в стиле "одна функция на каждый экран"

- **Клавиатуры и команды**: сценарий ДЗ идет через **inline**-кнопки с короткими **`callback_data`** (префиксы вроде **`tb:`**, **`hw_p:`**, **`exd:`**). В **меню команд** Telegram (**`post_init_commands`**, **`BotCommandScopeAllPrivateChats`**, **`MenuButtonCommands`**): **`/start`**, **`/shot`**, **`/chat`**, **`/textbook`** (последняя в списке). **`/stats`** и **`/begemot`** в меню не показываются — только ручной ввод. После **`flow_remove_reply_keyboard`** для чата выставляется **`MenuButtonDefault`** (у бота это тоже список команд), затем снова глобально **`MenuButtonCommands`** — так меню чаще появляется в **Telegram Desktop** (иконка **/** слева от поля ввода или ввод **`/`**). При **`/textbook`** и в **`flow_purge_except`** reply-клавиатура снимается (**`flow_remove_reply_keyboard`**), если клиент еще показывал старую. Дисклеймер: префикс **`dc:`** (**`dc:quiz:V:N`** с версией **`V`**, для старых сообщений еще **`dc:q:N`**, **`dc:accept`**). **Оцени ответ** (голос): **`cfv:1`** / **`cfv:-1`**. Админские колбэки отзывов и тикетов — префикс **`fb:`** (после **`/begemot`** и верного пароля; у пользователя после просмотра отзывов — список **активных** тикетов, **Рассмотрено** / **Отклонено** и ответ пользователю в личный чат с ботом; рассмотренные в **`fb:arx`** — архив; у выбранного автора отзыва — **⏲️ Блок 1 ч** (**`fb:bt`**, причина следующим сообщением), **💀 Бан** (**`fb:bp`**), **🔄 Разбан** (**`fb:bu`**), **Спасибо за отзыв** (**`fb:th`**); при временном блоке пользователь получает уведомление с **`ban:lift`** (**Снять ограничение**))
- **Один обработчик колбэков**: весь разбор **`CallbackQuery`** сосредоточен в **`button_callback`** - по префиксу строки **`query.data`** выбирается ветка; состояние многошагового ввода хранится в **`context.user_data`** (шаг, черновик параграфа, буфер цифр, списки из gdz.ru)
- **Главное меню по состоянию профиля**: **`get_main_keyboard`** собирает строки из условий - задание не заполнено / заполнено без фото / есть фото; при заполненном задании всегда есть **«Ответить текстом»** (**`answer_text`**); кнопки статистики в этом меню нет — команда **`/stats`** и меню команд Telegram; после текста **результата проверки** кнопка **«Проверить»** скрыта (остаются **«Показать ГДЗ»**, **«Ответить текстом»** и **«Выбрать упражнение или проверочную»**), при настроенном fallback (**`VLLM_FALLBACK_ENABLE=1`** и **`VLLM_FALLBACK_BASE_URL`**) добавляется кнопка **«Проверить ещё раз (Cursor)»** (callback **`recheck_cursor`**, использует сохранённые в **`context.user_data["last_check_file_ids"]`** id фото и форсирует **`engine=cursor`** на сервере), плюс одна строка **👍** / **👎** (**`get_check_result_keyboard`**), до голосования; ожидание текста после **«Ответить текстом»** — флаг **`await_text_answer`** в **`context.user_data`**; без отдельных "экранных" FSM-таблиц в БД
- **Каталог учебников**: список из JSON режется на страницы фиксированного размера (**`PAGE_SIZE`**); на кнопке - глобальный индекс **`tb:{grade}:{idx}`**; листание **`pg:{grade}:{page}`**. Подпись кнопки - **`label`** из каталога; префикс **🔥** (ровно один, максимум у одной кнопки на класс) - у slug с максимумом выборов в **`user_storage.textbook_popularity_by_grade`**, при ничьей - первый такой в порядке каталога; обрезка до **64** символов (**лимит Telegram** на текст inline-кнопки)
- **Параграф §1…§N**: число **N** берется с разбора оглавления gdz.ru (с потолком **`GDZ_PARAGRAPH_BUTTONS_MAX`** и кешем TTL), кнопки раскладываются в ряды по **6**; **`hw_p:{n}`** или ручной ввод
- **Шаг 2 после параграфа**: один раз подгружается **`gdz_solution.fetch_paragraph_task_meta`** - в **`user_data`** попадает множество номеров упражнений (**`frozenset`**) и список страниц проверочных; при пустом списке упражнений - запасной диапазон **`GDZ_EXERCISE_KEYPAD_FALLBACK_MAX`**
- **Клавиатура номера упражнения (динамическая сетка цифр)**: на каждом шаге пересчитывается множество **допустимых следующих цифр** по префиксу (**`_allowed_exercise_next_digits`**) - нельзя набрать заведомо лишний номер кнопками. **«Готово»** срабатывает, если введенное число **целиком** есть в списке упражнений параграфа (1, 2 или 3 цифры по фактической длине номера в оглавлении), без требования «добить» префикс до однозначности. Раскладка цифр - по порядку **1…9, 0** с переносом строк по **6** кнопок в ряд (число рядов меняется)
- **Страницы проверочных**: на основном шаге 2 — колбэк **`hw_verif_open`** (**«Выбрать проверочную»**); на подэкране — кнопки **`hw_pf:{page}`** (на кнопке только номер страницы), ряды по **6**; список в **`user_data`**; если страниц больше **`GDZ_VERIF_BUTTONS_MAX`**, список **обрезается**, в тексте сообщения - пометка; **«Назад»** с подэкрана — **`back_hw:vf`**; остальные страницы - текстом или **«или Ввести номер»**
- **Набор страницы 1–999**: фиксированная сетка цифр (**`page_keypad_keyboard`**), буфер в **`user_data`**, колбэки **`pgd:`** / **`pgk:`** (цифра, удалить, подтвердить, назад)
- **Стикеры после проверки**: список наборов задается в **`.env`**: общий ключ **`STICKER_SET_POOL`** (имена через запятую, как в **`t.me/addstickers/ИМЯ`**) подставляется и для награды, и для мотивации, пока не заданы отдельно **`TADA_STICKER_SET_NAMES`** и **`MOTIVATION_STICKER_SET_NAMES`**. Один стикер из объединенного пула (при первой попытке предпочтение эмодзи 🎉); сбои **`get_sticker_set`/`send_sticker`** - **`WARNING`** в лог; учет в **`check_sticker_reward`**. Если все три ключа пустые - встроенный пул в **`bot.py`**; перед отправкой наборы **перемешиваются**, внутри набора - случайный стикер. Эндпоинт **`POST /check/quip`** на сервере (тело JSON с **`excerpt`**) для внешних вызовов или отладки - бот после проверки его не дергает

- **Обновление экрана**: после нажатия чаще всего **`edit_message_text`** с новым текстом и новой **`reply_markup`**; при неизменном содержимом возможен **`BadRequest`** от Telegram - отдельные ветки логируют и не падают
- **Длинные операции**: пока грузится gdz.ru или идет тяжелый шаг, вспомогательно крутится **"печатает"** (**`run_with_typing`**) - это не часть разметки кнопок, но снижает ощущение "зависшего" меню

---

## VLLM (Qwen3-VL и OpenAI-совместимый API)

Сервер ходит в **`POST /v1/chat/completions`** (клиент `openai` с кастомным `base_url`).

Пример переменных:

```env
AI_MOCK=0
VLLM_BASE_URL=http://HOST:PORT/v1/chat/completions
VLLM_API_KEY=1
VLLM_MODEL=qwen/qwen3-vl-8b
VLLM_CONTEXT_WINDOW=32768
VLLM_MAX_TOKENS=4096
VLLM_NO_THINK=1
# Полный текст промпта - в .env.example: VLLM_CHECK_HOMEWORK и VLLM_CHECK_RUBRIC
```

- **`VLLM_BASE_URL`** — можно указать полный URL до `.../v1/chat/completions` или базу `http://HOST:PORT/v1`
- Временный backend: LM Studio на хосте, например `http://host.docker.internal:1234/v1`. Позже тот же контракт на vLLM
- **`VLLM_VISION`**: `1`/`0` явно. Пусто — по имени модели (`vl` / `vision`). Текстовые Qwen 3 8b / 3.5 9b: фото только через pre-OCR
- Модель с **VL (vision)** — для фото ДЗ передается `image_url` с `data:image/...;base64,...`
- Если на tgzh-server задан **`MOTOK_HUB_TOKEN_SECRET`**, `POST /check` требует `Authorization: Bearer` (JWT хаба motok)
- Контекст **32768** задается для справки; лимит ответа ограничивается **`VLLM_MAX_TOKENS`**
- **`VLLM_NO_THINK`** (по умолчанию **1**) — для Qwen3: в текст запроса добавляется **` /no_think`**, в вызов API — **`extra_body`** с **`chat_template_kwargs.enable_thinking: false`** (vLLM); **`0`** — не добавлять
- К **тексту ответа** проверки и к **сводке** по нескольким фото сервер дописывает **`Модель: {VLLM_MODEL}`**; при ошибках обращения к VLLM та же подпись добавляется к сообщению об ошибке. Если сработал pre-OCR (непустой ответ **`POST /v1/preocr`** для **изображения**), в результат добавляется строка про **pre-OCR**; для **`text/plain`** и прочих текстовых MIME pre-OCR **не** вызывается. Метка смешанных чисел остается в хвосте ответа для логики статистики (**`detach_trailing_mixed_numbers_marker`** в **`homework_check_status.py`** вставляет футер **перед** ней)

В **`POST /check`** (multipart) кроме файла **`photo`** можно передать поля формы (их подставляет бот): **`paragraph`**, **`exercise`**, **`page`**, **`textbook_label`**, **`grade`**, плюс **`gdz_exercises`** и **`gdz_verif_pages`** (номера через запятую из оглавления gdz.ru для параграфа) и опционально **`gdz_verif_works`** (многострочный список проверочных работ с подписями из оглавления — если в параграфе несколько работ с пересекающимися номерами заданий), а также **`gdz_task_condition`** — текст условия со страницы соответствующего задания на gdz.ru (как в учебнике; бот подгружает оглавление и при необходимости страницу задания перед проверкой). Тот же **`gdz_task_condition`** бот **вставляет в чат** в сообщении **«Результат проверки»** перед текстом модели (оригинальная формулировка задания), чтобы не полагаться на пересказ нейросетью. Модель сверяет снимок со списком упражнений и сообщает ученику, если на **этом** фото не видно части номеров (при нескольких фото сводка **`POST /check/summarize`** дополнительно просит объединить вывод о полноте). Для сводки — **`POST /check/summarize`** (JSON **`{"parts": ["...", "..."]}`**), см. **`VLLM_CHECK_MULTI_SUMMARY`** и **`VLLM_PROMPT_MULTI_SUMMARY_COVERAGE`** в **`.env.example`**.

Текст промптов для модели задается в **`.env`**: основной шаблон задания **`VLLM_CHECK_HOMEWORK`** (плейсхолдеры **`{book}`**, **`{g}`**, **`{anchor}`**); если он пустой - **`VLLM_CHECK_HOMEWORK_DEFAULT`**, иначе встроенный шаблон в **`ai_checker.py`**. Рубрика - **`VLLM_CHECK_RUBRIC`** (пусто - блок не добавляется). Остальные фрагменты системного промпта проверки по фото, сводки, шаблон quip (для **`POST /check/quip`**) и короткие сообщения об ошибках переопределяются переменными **`VLLM_PROMPT_*`**, **`VLLM_QUIP_*`**, **`VLLM_MSG_*`** - полный список плейсхолдеров и имен ключей в **`.env.example`**. Если ключ не задан или значение пустое, используется значение по умолчанию из кода (**`ai_checker.py`**, константы **`_DEFAULT_*`**). Сводка по нескольким фото: **`VLLM_CHECK_MULTI_SUMMARY`** (шаблон с **`{parts}`**, **`{n}`**) или **`VLLM_PROMPT_MULTI_SUMMARY_INTRO`** при пустом первом

**Типы файлов для `POST /check`** (поле `photo` в multipart, как раньше):

| Тип | MIME (пример) | Поведение |
|-----|----------------|-----------|
| txt | text/plain | текст в промпт |
| html | text/html | текст в промпт |
| markdown | text/markdown | текст в промпт |
| pdf | application/pdf | base64 в мультимодальный запрос (если бэкенд поддерживает) |
| jpeg, png, … | image/* | vision |
| doc, docx, rtf | см. ниже | файл принимается, но без конвертации модель не получает содержимое — лучше прислать фото или PDF |

Расширения для справки: **doc, docx, html, markdown, pdf, rtf, txt** (константа `VLLM_DOCUMENT_EXTENSIONS` в `ai_checker.py`).

Telegram-бот по-прежнему шлет **фото как JPEG**; остальные типы удобно гонять через `curl` или отдельный клиент к API.

### Опциональный fallback LLM (например, через cursor-bridge)

Если основной VLLM упал (отказ соединения, **5xx/408/429**, таймаут или пустые `choices`), `ai_checker.py` может сходить во **второй** OpenAI-совместимый эндпоинт. Сценарий придуман под **cursor-bridge** (`discourse-cursor-bridge`), у которого появился `POST /v1/chat/completions` поверх `cursor-agent`, но подойдёт любой OpenAI API.

Включить (по умолчанию выключено):

```bash
VLLM_FALLBACK_ENABLE=1
VLLM_FALLBACK_BASE_URL=http://bridge.example.internal:8787/v1
VLLM_FALLBACK_API_KEY=<тот же BRIDGE_OPENAI_API_KEY на стороне bridge>
VLLM_FALLBACK_MODEL=composer-2     # bridge пробрасывает в `cursor-agent --model`; допустимы composer-2 / gpt-5 / gpt-5-codex / claude-sonnet-4 / claude-sonnet-4-thinking / auto
VLLM_FALLBACK_TIMEOUT_SEC=180
# Источники IP для VLLM_FALLBACK_BASE_URL — на случай, когда системный
# резолвер (типа кеша домашнего роутера) отдает протухший адрес.
# Приоритет: HTTP-echo > UDP-DNS > системный резолвер.
#
# (1) HTTP-echo сервис, отдающий публичный IP клиента plain text-ом
# (например, no-ip). Подходит, когда bridge стоит **за тем же NAT**, что
# и контейнер: WAN-IP клиента == адрес, по которому bridge виден извне.
# Самый авторитетный источник: минует кеши публичных DNS.
VLLM_FALLBACK_RESOLVE_HTTP_ECHO_URL=http://http-echo.example.com
# (2) Upstream DNS-сервера (CSV IPv4) для UDP-резолва, минуя /etc/resolv.conf.
# Используются, если HTTP-echo не сконфигурирован или временно упал.
VLLM_FALLBACK_DNS_SERVERS=1.1.1.1,8.8.8.8
```

Подмена применяется **только** к хосту из `VLLM_FALLBACK_BASE_URL`; для всех остальных запросов httpx использует системный резолвер. Заголовок `Host:` сохраняется (виртуальные хосты на bridge продолжают работать). Реализация — `tgzh_httpx._HttpEchoBackend` / `_UpstreamDnsBackend`, оба с TTL-кэшем.

Логика:

- триггеры: `APIConnectionError`, `APIStatusError` со статусом **>=500**, **408**, **429**, `asyncio.TimeoutError`, **пустые** `choices` у primary;
- **4xx** (400/401/403/404/422) **не** триггерят fallback — эти ошибки клиента fallback не починит;
- если оба эндпоинта упали, пользователь получает текущее «человеческое» сообщение об ошибке primary;
- в подписи **`Модель: …`** оказывается имя модели того эндпоинта, который реально ответил.

Метрика Prometheus (если включён `METRICS_PORT`): `tgzh_llm_fallback_total{stage,reason}` — **stage** ∈ `check|summarize|quip`, **reason** ∈ `connection|server_error|timeout|empty_choices|other`. По всплеску этой метрики видно деградацию primary VLLM.

> ⚠ `cursor-agent` существенно медленнее vLLM и **не** предназначен для постоянной нагрузки. Fallback должен срабатывать редко — иначе перенастрой primary.

---

## Структура проекта

```
tgzh/
├── bot.py              # Telegram-бот (inline-клавиатуры и callback — см. раздел «Динамические inline-кнопки»)
├── bot_stats.py        # Глобальная статистика по учебным годам (фото, проверки, сбои); SQLite или PostgreSQL
├── homework_check_status.py   # В UI ✅/❌, заголовок **Результат проверки**, снятие метки **`[tgzh_mixed_numbers]`**, отцепление хвоста метки для футера анализа; футер HTML перед **Оцени ответ**; **`homework_check_stats_result`**
├── gdz_solution.py     # Поиск страницы задания на gdz.ru, условие и картинки решения
├── gdz_cache.py        # Файловый кеш решений (папка GDZ_CACHE_DIR), быстрый повтор для всех пользователей
├── photo_prepare.py    # Перед /check: EXIF, уменьшение до 1024 px по длинной стороне, JPEG
├── user_storage.py     # Профиль, отзывы, согласия, опрос; SQLite или PostgreSQL (DATABASE_URL)
├── tgzh_db.py          # Подключение к БД и плейсхолдеры SQL
├── tgzh_metrics.py     # Опциональные метрики Prometheus (METRICS_PORT)
├── tgzh_httpx.py       # Общий httpx AsyncClient: резолв имён через IPv4 (бот → SERVER_URL, TEI)
├── feedback_tei.py     # TEI sentiment/emotion для отзывов (TEI_SENTIMENT_URL / TEI_EMOTION_URL)
├── server.py           # FastAPI-сервер
├── ai_checker.py       # Проверка: mock / VLLM, промпты, футер **Модель** / **pre-OCR**, сводка **`summarize_check_parts`**
├── preocr_client.py    # HTTP-клиент **`PREOCR_URL`** → **`/v1/preocr`** (вызывается из **`ai_checker`**)
├── preocr/             # Сервис предварительного OCR (FastAPI, PaddleOCR / PP-StructureV3)
├── docker-compose.yml
├── alembic.ini / alembic/   # Миграции PostgreSQL (DATABASE_URL; цепочка 001…007 и далее)
├── grafana/dashboards/      # Пример дашборда для Prometheus
├── Dockerfile.server / Dockerfile.bot / Dockerfile.preocr / Dockerfile.preocr.gpu
├── deploy/
│   └── nginx-tgzh-api.location.conf.example   # префикс /tgzh-api/ для Nginx
├── scripts/
│   ├── fetch_gdz_textbooks.py   # Обновление каталога с gdz.ru
│   ├── postgres_schema.sql      # DDL PostgreSQL (и для psql, и для Alembic)
│   ├── import_sqlite_to_postgres.py  # Перенос данных SQLite → PostgreSQL
│   └── deploy-docker.sh         # архив по SSH + docker compose; опционально DEPLOY_RUN_ALEMBIC=1
├── data/
│   ├── gdz_matematika_textbooks.json   # Список учебников для бота
│   └── users.sqlite        # Создается при работе бота (не в git)
├── requirements.txt
├── .env.example
└── README.md
```
