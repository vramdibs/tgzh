# Design: `/photo` - проверка по снимкам без OCR и ГДЗ

Дата: 2026-09-29

## Цель

Команда `/photo` для проверки домашней работы по одному или нескольким фото: условие (учебник, доска) и решение (тетрадь) на разных кадрах или на одном. Без pre-OCR и без загрузки условия с ГДЗ.

## Модели

Цепочка через Cursor bridge: Composer (`composer-2.5`), затем Grok (`cursor-grok-4.6-low`); fallback - Qwen VL по `VLLM_BASE_URL` с прямой передачей `image_url`.

## API

`POST /photo/check` - multipart `images[]`, `image_roles[]`, `mode=two_step|single_album`.

## Бот

FSM `_PHOTO_CHECK_*`, приоритет в `handle_photo` выше `/chat` и обычного ДЗ. Три UX-варианта: одно mixed-фото, два шага, один альбом.

## Реализация

- [`photo_check.py`](../../photo_check.py) - промпты, двухстадийный JSON + verify, consolidated fallback
- [`server.py`](../../server.py) - эндпоинт
- [`bot.py`](../../bot.py) - команда и колбэки `photo:`
