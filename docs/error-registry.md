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

## INC-2026-09-27-03: ложная диагностика «устаревший DNS»

| Поле | Значение |
|------|----------|
| **Дата** | 2026-09-27 |
| **Симптом** | Публичный DNS hostname -> `80.234.36.161`, текущий WAN egress -> `85.137.166.242` - кажется рассинхроном |
| **Причина** | Это **намеренная** схема: KeenDNS на роутер (inbound webhook), VPS - egress при блокировке Telegram провайдером |
| **Решение** | Не менять A-запись на egress-IP VPS. Для работы бота при блокировке - `TG_MODE=polling`. Для webhook - чинить inbound на роутере |
| **Проверка** | См. [integration.md](integration.md), раздел «Сеть: inbound и outbound» |

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
