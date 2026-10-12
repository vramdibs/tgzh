# Design: разделение маршрутизации `/chat` и `/imagine`

Дата: 2026-10-12

## Проблема

После фичи `image_edit` (правки последней картинки текстом) уточняющий вопрос в обычном `/chat` с фото (например про усилитель) ошибочно уходит в `POST /image/generate` mode=`image_edit`, если в `user_data` остались `_CHAT_IMAGINE_LAST_PNG` и `_CHAT_IMAGINE_SESSION` от прошлой генерации `/imagine`. Пользователь видит чужую картинку и UI перерисовки вместо ответа Grok.

## Решение (согласовано)

**Вариант A:** в `/chat` после разбора фото все текстовые реплики идут только в Cursor/Grok. Правки картинки текстом - только в явной сессии `/imagine` (меню «Сгенерировать фото», перерисовка, успешная генерация). Если в `/chat` пользователь просит изменить фото - короткий ответ с просьбой открыть `/imagine` или кнопку «Сгенерировать фото».

## Подходы (рассмотренные)

| Подход | Суть | Минус |
|--------|------|-------|
| **1. Сброс imagine при любом чат-фото/тексте** | `_clear_chat_imagine_session` в `_handle_chat_photo` и перед `_handle_chat_user_message` | Грубо, но предсказуемо |
| **2. Жесткий гейт `image_edit` (рекомендуется)** | Роутинг в `image_edit` только если в последних N репликах истории есть маркеры `[/imagine]`, `[сгенерировано фото`, `[перерисовано фото`, `[отредактировано фото]` **и** есть `_CHAT_IMAGINE_LAST_PNG`; сброс imagine-сессии при входе в multimodal-чат с фото | Чуть больше кода, четкие границы |
| **3. Отдельный FSM `chat_focus`** | Явное состояние cursor vs imagine | Избыточно для двух веток |

Реализуем **2**: минимальный дифф, сохраняет сценарий «убери провода» после перерисовки в `/imagine`.

## Архитектура

### Два независимых контекста

1. **Cursor-чат** - RAM `_CHAT_HISTORY`, плейсхолдеры `[фото: …]`, стрим `/chat/stream`.
2. **Imagine** - `_CHAT_IMAGINE_SESSION`, `_CHAT_IMAGINE_LAST_PNG`, FSM `_CHAT_IMG_PROMPT_WAIT` / `_CHAT_IMAGINE_STYLE_PHOTO_WAIT`, API `/image/generate`.

Правило: активный multimodal-вопрос в `/chat` **снимает** imagine-контекст (last PNG + session), чтобы старые картинки не перехватывали текст.

### Маршрутизация текста в `handle_homework_text` (порядок без изменений)

1. `_should_route_chat_text_to_imagine` - длинный промпт + недавний imagine в истории.
2. `_should_route_chat_text_to_imagine_edit` - **ужесточить** (см. ниже).
3. Опционально: `_should_nudge_imagine_for_edit_in_chat` - эвристика «просит править картинку» без imagine-сессии -> reply с подсказкой `/imagine`, **не** вызывать Grok с этой репликой как edit (или вызвать Grok - см. ниже).
4. `_handle_chat_user_message` - обычный чат.

### Ужесточение `_should_route_chat_text_to_imagine_edit`

Возвращать `True` только если одновременно:

- `image_gen` настроен;
- нет `_CHAT_IMG_PROMPT_WAIT` / `_CHAT_IMAGINE_STYLE_PHOTO_WAIT`;
- есть `_CHAT_IMAGINE_LAST_PNG`;
- **`_chat_history_has_recent_imagine(history)`** в последних 8 репликах (убрать OR с голым `_CHAT_IMAGINE_SESSION` без маркеров в истории);
- опционально: `_CHAT_IMAGINE_SESSION` True (синхронизировать с успешным imagine, сбрасывать при clear);
- текст не команда, не `_text_likely_imagine_prompt`, не `_text_looks_like_chat_question`, длина в лимите.

Так реплика после `[фото: …]` + ответ ассистента без imagine-маркеров **никогда** не попадет в `image_edit`, даже если session забыли сбросить.

### Сброс imagine-контекста

Вызывать `_clear_chat_imagine_session` (уже есть) дополнительно:

- в начале `_handle_chat_photo` (перед стримом в Cursor);
- в начале `_handle_chat_user_message` при `image_b64` или при user text после недавнего `[фото:` в истории (уточнение к разбору фото) - **достаточно** сброса на `_handle_chat_photo` и на любой `_handle_chat_user_message` без imagine wait flags, если последняя user-реплика в истории - `[фото:` без последующего `[/imagine]` - проще: **сброс на каждом `_handle_chat_user_message` текстом**, кроме случая когда `_should_route_chat_text_to_imagine_edit` уже True. Еще проще: сброс при `_handle_chat_photo` only + ужесточенный гейт. Для усилителя сценарий: фото ушло в chat, history has `[фото:`, no imagine markers -> edit false even if stale PNG.

Дополнительно сброс при `_handle_chat_photo` очищает stale PNG при новом чат-фото.

### Подсказка «перейдите в /imagine»

Новая функция `_text_looks_like_image_edit_request(text)` - эвристика (убери/удали/перерисуй/сделай ярче/убрать провода + опционально «фото/снимок/картинк»).

Если в активном `/chat`, **не** imagine edit route, **не** imagine waits, и эвристика True:

- `reply_text` с HTML: для изменения картинки открой `/imagine` или кнопку «Сгенерировать фото» в меню чата; в обычном чате Grok только обсуждает фото, не рисует заново.
- **Не** писать реплику в историю как user message к Cursor (или писать короткий system-side ответ только локально - лучше не добавлять в `_CHAT_HISTORY`, чтобы не путать модель).

Если эвристика ложноположительная на обычный вопрос - пользователь все равно может повторить; эвристика консервативная (нужны глаголы правки).

## Компоненты

| Файл | Изменения |
|------|-----------|
| `bot.py` | гейт edit, сброс session на chat photo, nudge helper + ветка в `handle_homework_text` |
| `tests/test_chat_imagine.py` | тест: chat history with `[фото:` -> edit false; imagine history -> edit true; nudge text |
| `RELEASE_NOTES.md` | Fixed: уточнения в `/chat` после фото не уходят в imagine |

Bridge и `server.py` без изменений.

## Ошибки и границы

- После рестарта бота `_CHAT_IMAGINE_LAST_PNG` пропадает - правки текстом в imagine требуют новой генерации (как сейчас).
- Подсказка `/imagine` не блокирует обычные вопросы без глаголов правки.
- Сценарий «убери провода» после перерисовки в `/imagine` сохраняется, если в истории есть `[перерисовано фото` / `[/imagine edit]`.

## Тест-план

1. Unit: `_should_route_chat_text_to_imagine_edit` с history `[фото: x]` + assistant text -> False при наличии last PNG.
2. Unit: history с `[перерисовано фото` + last PNG + «убери провода» -> True.
3. Unit: «убери провода» в chat без imagine history -> nudge, не edit.
4. Ручной: фото усилителя в `/chat` -> уточнение про 140 Вт -> текстовый ответ Grok, без send_photo из image API.

## UI 2+3 (реализовано)

- Полное меню: **Чат:** модель из **Модель** (Grok/Composer); кнопка фото с подписью по состоянию
- После успешной генерации: урезанное меню **Фото:** `image_gen_user_facing_label()` (Composer на bridge, не Grok; не DALL-E при `IMAGE_GEN_MODEL=auto`)
- **Выйти из режима фото** (`chat:imagine:exit`), **Меню чата** сбрасывает compact UI
