# Реестр ошибок и инцидентов

Формат записи: **ID**, дата, симптом, диагностика, причина, решение, проверка.

См. также [integration.md](integration.md) - база знаний по подключению внешних сервисов.

---

## INC-2026-09-27-01: бот молчит на `/start` (webhook timeout)

| Поле | Значение |
|------|----------|
| **Дата** | 2026-09-27 |
| **Симптом** | Сообщения в Telegram доставлены (двойные галочки), бот не отвечает на `/start`, `/chat` |
| **Диагностика** | `getWebhookInfo`: `last_error_message = Connection timed out`, `pending_update_count > 0`. Логи `tgzh-bot`: после `Application started` нет обработанных апдейтов |
| **Причина** | Inbound webhook недоступен из интернета. KeenDNS (`tgzh.*.netcraze.pro`) указывает на **IP роутера** (inbound), а исходящий трафик к Telegram API идет через **VPS egress** - это разные пути; менять DNS на egress-IP VPS **нельзя** |
| **Решение** | **`TG_MODE=polling`** в `.env` + `docker compose up -d --force-recreate tgzh-bot`. Бот снимает webhook и получает апдейты через исходящий `getUpdates` |
| **Проверка** | `docker logs tgzh-bot` - `telegram mode=polling`, `Application started`; `getWebhookInfo` - пустой `url`; `/start` получает ответ |
| **Возврат на webhook** | Опционально: починить inbound `:443` на роутере (KeenDNS -> proxy -> `tgzh-bot:8081`), внешний `curl -X POST https://<hostname>/telegram/webhook` -> **403**, затем `TG_MODE=webhook` |

**Связанные env:** `TG_MODE`, `TELEGRAM_WEBHOOK_*`

---

## INC-2026-09-27-02: `/chat` и recheck Cursor - 502, пустой ответ

| Поле | Значение |
|------|----------|
| **Дата** | 2026-09-27 |
| **Симптом** | `/chat` принимает сообщение, ответа нет или «Cursor не вернул ответа». В логах `tgzh-server`: `chat stream failed`, `502 - cursor-agent did not produce a usable answer (exit_code=1)` |
| **Диагностика** | Bridge `:8787` доступен по TCP. `journalctl --user -u discourse-cursor-bridge`: `cursor-agent finished: ok=False exit_code=1 duration~1s`. Ручной запуск `agent -p` без ключа: `Authentication required` |
| **Причина** | В `.env` **cursor-bridge** отсутствовал **`CURSOR_API_KEY`**. Ключ в `.env` tgzh на процесс bridge не передается |
| **Решение** | Добавить **`CURSOR_API_KEY`** в `.env` bridge (User API Key с [cursor.com/dashboard/cloud-agents](https://cursor.com/dashboard/cloud-agents)). Перезапуск: `systemctl --user restart discourse-cursor-bridge` |
| **Проверка** | `curl -X POST http://127.0.0.1:8787/v1/chat/completions` с `Authorization: Bearer <BRIDGE_OPENAI_API_KEY>`, `stream=false` -> JSON с `choices[0].message.content`. `/chat` в Telegram отвечает за несколько секунд |

**Связанные env:** bridge - `CURSOR_API_KEY`, `BRIDGE_OPENAI_API_KEY`; tgzh - `VLLM_FALLBACK_ENABLE`, `VLLM_FALLBACK_BASE_URL`, `VLLM_FALLBACK_API_KEY`

**Примечание:** bridge не поддерживает `stream=true` (ответ **400**). tgzh автоматически переключается на single completion - это нормально.

---

## INC-2026-10-02-01: `/chat` Grok 4.7 - «Cursor не вернул ответа»

| Поле | Значение |
|------|----------|
| **Дата** | 2026-10-02 |
| **Симптом** | В `/chat` модель Grok 4.7 отвечает «Cursor не вернул ответа. Попробуй переформулировать.» Composer и Grok 4.6 работают |
| **Диагностика** | `tgzh-server`: `chat stream failed`, `400 model 'cursor-grok-4.7-low' is not in BRIDGE_OPENAI_ALLOWED_MODELS`. В CSV allowlist был только `cursor-grok-4.6-low`. Сервер отдавал **200 пусто**, бот принимал это за пустой ответ модели. После расширения allowlist `cursor-agent --list-models` для 4.7 Low показывает **`grok-4.7-low`**, не `cursor-grok-4.7-low` - неверный slug дает `502` exit_code=1 |
| **Причина** | Жесткий CSV на bridge плюс глотание 400 в `POST /chat/stream`. Плюс смена CLI-slug Grok 4.7 |
| **Решение** | На bridge поверх CSV разрешены `cursor-grok-…-low` и `grok-…-low` (в т.ч. `5` без минора). С 4.7 CLI без `cursor-` (`cursor-grok-4.8-low` → `grok-4.8-low`); 4.6 остается с префиксом. `GET /v1/models` берет `cursor-agent --list-models`. В tgzh та же канонизация в `chat_canonical_cursor_model`, каталог обновляется при `/chat`, запросе к ассистенту и ошибке ответа. Ошибка до первого токена уходит HTTP 400/502, не пустым 200. Рестарт `discourse-cursor-bridge`, `tgzh-server` и `tgzh-bot` |
| **Проверка** | `pytest` в tgzh (`test_chat_resolve_keeps_grok_xy_low_outside_catalog`, `test_chat_stream_bridge_400_is_not_empty_200`) и в discourse-cursor-bridge (`test_cursor_grok_xy_low_allowed_without_csv`). Ручной: `/chat` -> Модель Grok 4.7 -> короткий вопрос получает текст, не «переформулируй» |

**Связанные env:** `BRIDGE_OPENAI_ALLOWED_MODELS`, `CHAT_CURSOR_MODELS`

---

## INC-2026-09-27-03: ложная диагностика «устаревший DNS»

| Поле | Значение |
|------|----------|
| **Дата** | 2026-09-27 |
| **Симптом** | Публичный DNS hostname -> `80.234.36.161`, текущий WAN egress -> `85.137.166.242` - кажется рассинхроном |
| **Причина** | Это **намеренная** схема: KeenDNS на роутер (inbound webhook), VPS - egress при блокировке Telegram провайдером |
| **Решение** | Не менять A-запись на egress-IP VPS. Для работы бота при блокировке - `TG_MODE=polling`. Для webhook - чинить inbound на роутере |
| **Проверка** | См. [integration.md](integration.md), раздел «Сеть: inbound и outbound» |

---

## INC-2026-09-29-01: `/shot` «Проверить» - sync-поток в AsyncClient (httpx 0.28)

| Поле | Значение |
|------|----------|
| **Дата** | 2026-09-29 |
| **Симптом** | В `/shot` после «Проверить» бот пишет `Ошибка: Attempted to send an sync request with an AsyncClient instance.`; проверка не доходит до сервера |
| **Диагностика** | Ошибка возникает в боте (`_run_photo_check_request`) до ответа сервера. Воспроизведение в контейнере `tgzh-bot`: `client.build_request("POST", ..., files=[...], data=[...])` дает `IteratorByteStream` (только sync, `mro` без `AsyncByteStream`), и `AsyncClient.send` кидает `RuntimeError`. Тот же multipart без `data=` (поля внутри `files`) дает `MultipartStream` (sync + async) |
| **Причина** | На httpx 0.28 одновременная передача `files` **и** `data` **списками** формирует синхронный `IteratorByteStream`. `AsyncClient` его не отправляет. Обычный `POST /check` не задет: там `files`/`data` - **словари**, httpx строит `MultipartStream` |
| **Решение** | В `bot._run_photo_check_request` убрать `data=`; `mode` и `image_roles` класть в тот же multipart-`files` как поля без имени файла: `("mode", (None, mode))`, `("image_roles", (None, role))`. Сервер `POST /photo/check` контракт не меняет |
| **Проверка** | `pytest tests/test_photo_bot.py::test_photo_check_multipart_is_async_stream` (поток - наследник `httpx.AsyncByteStream`); ручной: `/shot` -> фото -> «Проверить» доходит до модели |

**Связанные env:** `PHOTO_CHECK_*`, `BOT_PHOTO_CHECK_TIMEOUT_SEC`

**Примечание:** правило на будущее - для async-запросов с multipart не смешивать списочные `files` и `data`; текстовые поля добавлять в `files` как `(None, value)`, либо использовать `data`-словарь.

---

## Шаблон новой записи

```markdown
## INC-YYYY-MM-DD-NN: краткое название

| Поле | Значение |
|------|----------|
| **Дата** | YYYY-MM-DD |
| **Симптом** | что видит пользователь / в логах |
| **Диагностика** | команды, метрики, фрагменты логов |
| **Причина** | корневая причина |
| **Решение** | шаги |
| **Проверка** | как убедиться, что исправлено |

**Связанные env:** ...
```

---

## INC-2026-10-12-01: перерисовка фото - `image file locate timed out after generation`

| Поле | Значение |
|------|----------|
| **Дата** | 2026-10-12 |
| **Симптом** | В `/imagine` / «Перерисовать фото в стиле примера» бот: `server 502` / `image file locate timed out after generation` |
| **Диагностика** | `tgzh-server` -> `POST /image/generate` -> bridge `POST /v1/images/generations`. Лог bridge: `images generate: locate file timed out`. `cursor-agent` мог завершить stream по `type==result` до записи PNG |
| **Причина** | Ранний выход `AgentRunner` по строке `result` в stream-json убивал процесс до окончания GenerateImage; однократный поиск файла не дожидался появления PNG на диске |
| **Решение** | Обновить **discourse-cursor-bridge**: `run(..., exit_on_result=False)` для images, опрос `_wait_for_located_image` (env **`BRIDGE_IMAGE_LOCATE_TIMEOUT_SEC`**, дефолт 90), перезапуск bridge на хосте с `IMAGE_GEN_BASE_URL` |
| **Проверка** | Повторить перерисовку фото; ответ с PNG в чате. При долгой генерации при необходимости увеличить **`BRIDGE_IMAGE_TIMEOUT_SEC`** и **`IMAGE_GEN_TIMEOUT_SEC`** |

**Связанные env:** `BRIDGE_IMAGE_LOCATE_TIMEOUT_SEC`, `BRIDGE_IMAGE_LOCATE_POLL_SEC`, `BRIDGE_IMAGE_TIMEOUT_SEC`, `IMAGE_GEN_TIMEOUT_SEC`, `IMAGE_GEN_MULTIPART_TIMEOUT_SEC`

---

## INC-2026-10-12-02: перерисовка фото - `image generation timeout` (504)

| Поле | Значение |
|------|----------|
| **Дата** | 2026-10-12 |
| **Симптом** | «Перерисовать фото в стиле примера»: `server 504: {"detail":"image generation timeout"}` |
| **Диагностика** | `tgzh-server` обрывает `asyncio.wait_for` на вызов bridge по **`IMAGE_GEN_TIMEOUT_SEC`** (часто 180), тогда как после фикса INC-2026-10-12-01 bridge может работать до **`BRIDGE_IMAGE_TIMEOUT_SEC`** + еще до **`BRIDGE_IMAGE_LOCATE_TIMEOUT_SEC`** (90) на поиск PNG |
| **Причина** | Один таймаут на все режимы `/image/generate`; для multipart-режимов суммарное время bridge длиннее |
| **Решение** | В **tgzh** `image_gen_multipart_timeout_sec()` (дефолт `IMAGE_GEN_TIMEOUT_SEC + 120`) для `style_redraw` и `image_edit`; бот ждет `+30` с через `image_gen_bot_client_timeout_sec(multipart=True)`. При необходимости явно **`IMAGE_GEN_MULTIPART_TIMEOUT_SEC`** (до 600) |
| **Проверка** | Передеплой `tgzh-server`/`tgzh-bot`; повторить перерисовку; в логах `image_generate done` без 504 |

---

## INC-2026-10-12-03: перерисовка фото - `cursor-agent timed out` (502)

| Поле | Значение |
|------|----------|
| **Дата** | 2026-10-12 |
| **Симптом** | После «Перерисовываю фото…» через ~3 мин: `server 502` / `cursor-agent timed out` (или бот молчит, если сообщение об ошибке не дошло) |
| **Диагностика** | Лог `tgzh-server`: `image backend 502: {"detail":"cursor-agent timed out"}`. Bridge убивает агент по **`BRIDGE_IMAGE_TIMEOUT_SEC`** (дефолт 180); multimodal `style_redraw` часто дольше |
| **Причина** | Таймаут агента на bridge меньше реального времени GenerateImage + чтение SOURCE/STYLE |
| **Решение** | На bridge **`BRIDGE_IMAGE_TIMEOUT_SEC=360`** (или выше, до 600); на tgzh **`IMAGE_GEN_MULTIPART_TIMEOUT_SEC`** не меньше суммы таймаутов bridge + locate (например 480 при 360+90); перезапуск bridge и `tgzh-server` |
| **Проверка** | Повторить перерисовку; в логах bridge `images generate done`, в чате PNG |
