"""
Проверка ДЗ по нескольким фото без pre-OCR и без ГДЗ (/photo).
Multimodal: Cursor bridge (Composer / Grok) или Qwen VL.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Final, Literal

from photo_prepare import prepare_photo_for_upload

logger = logging.getLogger("tgzh.photo_check")

ImageRole = Literal["condition", "solution", "mixed", "unspecified"]
PhotoCheckMode = Literal["two_step", "single_album"]

_IMAGES_PER_MESSAGE: Final = 4

# Общее правило сопоставления: якорь - рукопись в тетради, условие ищется под номер решения.
_MATCH_CONDITION_TO_SOLUTION_RULE: Final = """Сопоставление условие-решение:
- Якорь - рукопись ученика: для каждой видимой записи в тетради определи номер задачи и суть (числа, действия).
- На снимках с условиями найди задачу с тем же номером, в том числе соседнюю на странице, даже если крупнее или заметнее другая (последняя на странице, блок "Решение задач", задача внизу листа).
- Проверяй только такие пары. Задачи без решения ученика не делай главными и не проси прислать тетрадь именно по ним.
- Проси другой снимок только если условия для уже решенной задачи на фото нет или не читается; не подменяй его соседним видимым номером на странице учебника.
- Номер в строке "Задача №…" только тот, который написан на фото. Не бери номер по памяти и не ставь соседний.
- Числа, действия и текст условия копируй с фото. Не заменяй записи ученика другой задачей: другие числа, уравнения вместо примеров на листе, переменные, которых на снимке нет.
- Если формулировки учебника на снимке нет, проверяй сами записи в тетради под видимым номером и так и напиши. Не дописывай условие, которого не видно."""

_DEFAULT_STRUCTURE_PROMPT: Final = """Ты репетитор по математике. На фото могут быть:
- условия из учебника, доски или листа;
- рукописные решения в тетради;
- один кадр с условием и решением одновременно (учебник и тетрадь в одном кадре).

Задача этапа: только структура. Верни **строго JSON** без markdown:
{
  "tasks": [
    {
      "id": "краткий идентификатор (например №9 или задача 1)",
      "condition_summary": "1-2 предложения условия по видимому тексту",
      "condition_photos": [1],
      "solution_photos": [2],
      "same_frame": false,
      "confidence": "high|medium|low"
    }
  ],
  "ignored": ["что на фото не относится к проверяемым задачам"]
}

Нумерация фото - с 1, как в подписях к изображениям.
Сопоставляй только задачи, для которых на фото есть решение ученика.
В JSON включай только такие задачи; condition_photos - кадр, где нашлось условие с тем же номером, что в решении, а не самый заметный номер на странице.
Не используй ГДЗ и внешние источники. Не выдумывай условия вне видимого на фото.
Для одного кадра с условием и решением: condition_photos и solution_photos оба содержат этот номер, same_frame=true.

""" + _MATCH_CONDITION_TO_SOLUTION_RULE

_DEFAULT_VERIFY_PROMPT: Final = """Ты репетитор по математике. Проверь решения ученика по приложенным фото.

Правила:
- Условие бери только с фото; не используй ГДЗ и интернет.
- Если на странице несколько задач - проверяй только те, для которых есть решение на фото; условие подбирай под номер в тетради, а не под самую заметную задачу на странице учебника.

""" + _MATCH_CONDITION_TO_SOLUTION_RULE + """

- Для каждой проверенной задачи: кратко условие, оценка хода решения, верность ответа.
- Выражения пиши обычными цифрами и знаками: умножение ·, дробь через /. Без LaTeX: без обратного слэша, без frac, cdot и без оберток \\( \\).
- Слово верно выделяй **жирным**.
- Каждый пример задачи пиши с новой строки. В начале строки одна пометка: зеленая галочка, красный крестик или знак вопроса. Маркер списка не ставь и пометку в строке не повторяй.
- Строка "Задача №…:" без пометки, номер задачи выделяй **жирным**. Заголовок "Результат проверки" не пиши.
- Если текста задачи или формулировки из учебника на фото нет - отдельная строка со знаком вопроса, эту фразу выделяй **жирным**.
- Слово "Ответ" начинай с новой строки.
- Если условие или решение не читается - скажи об этом, попроси более четкий снимок; не отказывай без причины.
- В конце ответа отдельной строкой укажи итог: [tgzh_result:correct] или [tgzh_result:partial] или [tgzh_result:incorrect]
  (correct - все проверенные задачи верны; partial - есть ошибки или неполная проверка; incorrect - решения нет или все неверно)."""

_DEFAULT_CONSOLIDATED_PROMPT: Final = """Ты репетитор по математике. На фото - условия (учебник/доска) и/или рукописные решения.

Сам определи задачи, сопоставь решения, проверь ход и ответы. Игнорируй лишние задачи без решения ученика.

""" + _MATCH_CONDITION_TO_SOLUTION_RULE + """

Один кадр может содержать и условие, и решение - раздели зоны на изображении.

Не используй ГДЗ. Не выдумывай условия вне фото.

Формат ответа: строка "Задача №…:" без пометки, номер задачи **жирным**, затем каждый пример с новой строки.
В начале примера одна пометка (зеленая галочка, красный крестик или знак вопроса), без маркера списка и без повтора значка.
Если текста задачи или формулировки из учебника на фото нет - строка со знаком вопроса, фраза **жирным**.
Слово "Ответ" - с новой строки.
Заголовок "Результат проверки" не пиши.
Выражения - обычными цифрами и знаками (·, дробь через /), без LaTeX и обратных слэшей.
Слово верно - **жирным**.
В конце: [tgzh_result:correct|partial|incorrect]."""


def _int_env(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if raw.isdigit():
        return max(lo, min(hi, int(raw)))
    return default


def photo_check_enabled() -> bool:
    return (os.getenv("PHOTO_CHECK_ENABLE") or "1").strip() == "1"


def photo_check_max_images() -> int:
    return _int_env("PHOTO_CHECK_MAX_IMAGES", 12, lo=1, hi=24)


def photo_check_timeout_s() -> float:
    return float(_int_env("PHOTO_CHECK_TIMEOUT_SEC", 360, lo=30, hi=600))


def photo_check_qwen_fallback_enabled() -> bool:
    return (os.getenv("PHOTO_CHECK_QWEN_FALLBACK") or "1").strip() == "1"


def photo_check_model_slugs() -> list[str]:
    raw = (os.getenv("PHOTO_CHECK_MODELS") or "composer-2.5,cursor-grok-4.6-low").strip()
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        slug = part.strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    if not out:
        out = ["composer-2.5", "cursor-grok-4.6-low"]
    return out


def _role_caption(role: ImageRole) -> str:
    if role == "condition":
        return "условие"
    if role == "solution":
        return "решение"
    if role == "mixed":
        return "условие и решение на одном снимке"
    return "роль не указана - определи сам"


def role_label_for_index(index_1based: int, role: ImageRole) -> str:
    return f"Фото {index_1based} ({_role_caption(role)})"


def build_photo_data_urls(
    images: list[bytes],
) -> list[str]:
    urls: list[str] = []
    for raw in images:
        prepared = prepare_photo_for_upload(raw)
        b64 = base64.standard_b64encode(prepared).decode("ascii")
        urls.append(f"data:image/jpeg;base64,{b64}")
    return urls


def build_photo_check_messages(
    *,
    images: list[bytes],
    roles: list[ImageRole],
    mode: PhotoCheckMode,
    stage: Literal["structure", "verify", "consolidated"],
    structure_json: str = "",
) -> list[dict]:
    if not images:
        raise ValueError("no images")
    if len(images) > photo_check_max_images():
        raise ValueError("too many images")

    roles_norm: list[ImageRole] = []
    for i in range(len(images)):
        if i < len(roles):
            roles_norm.append(roles[i])
        else:
            roles_norm.append("unspecified")

    data_urls = build_photo_data_urls(images)

    if stage == "structure":
        intro = _DEFAULT_STRUCTURE_PROMPT
        if mode == "two_step":
            intro += "\n\nРежим загрузки: условие и решение могли быть отправлены отдельными группами фото."
        elif mode == "single_album":
            intro += "\n\nРежим: один альбом, порядок и роли кадров могут быть любыми."
    elif stage == "verify":
        intro = _DEFAULT_VERIFY_PROMPT
        if structure_json.strip():
            intro += f"\n\nСтруктура от предыдущего шага (JSON):\n{structure_json.strip()}"
    else:
        intro = _DEFAULT_CONSOLIDATED_PROMPT

    custom = (os.getenv("PHOTO_CHECK_PROMPT") or "").strip()
    if custom and stage == "consolidated":
        intro = custom

    messages: list[dict] = [{"role": "system", "content": intro}]

    chunk_texts: list[str] = []
    chunk_urls: list[str] = []

    def flush_chunk() -> None:
        nonlocal chunk_texts, chunk_urls
        if not chunk_urls:
            return
        parts: list[dict] = []
        if chunk_texts:
            parts.append({"type": "text", "text": "\n".join(chunk_texts)})
        for url in chunk_urls:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": parts})
        chunk_texts = []
        chunk_urls = []

    for idx, (url, role) in enumerate(zip(data_urls, roles_norm, strict=True), start=1):
        if len(chunk_urls) >= _IMAGES_PER_MESSAGE:
            flush_chunk()
        chunk_texts.append(role_label_for_index(idx, role))
        chunk_urls.append(url)
    flush_chunk()

    return messages


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def parse_structure_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


async def _completion_once(
    client,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
) -> str:
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False,
        temperature=0.2,
        max_tokens=max_tokens,
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    msg = getattr(choices[0], "message", None)
    content = getattr(msg, "content", None) if msg is not None else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


async def _photo_check_via_bridge(messages: list[dict]) -> str:
    from ai_checker import (
        _cursor_model_unavailable,
        _cursor_openai_client,
        chat_cursor_model_try_chain,
    )

    client = await _cursor_openai_client()
    slugs = photo_check_model_slugs()
    primary = slugs[0]
    chain = chat_cursor_model_try_chain(primary)
    for extra in slugs[1:]:
        if extra not in chain:
            chain.append(extra)

    last_exc: Exception | None = None
    max_tokens = _int_env("PHOTO_CHECK_MAX_TOKENS", 4096, lo=256, hi=16384)
    for idx, slug in enumerate(chain):
        try:
            return await _completion_once(client, slug, messages, max_tokens=max_tokens)
        except Exception as e:
            last_exc = e
            if idx < len(chain) - 1 and _cursor_model_unavailable(e):
                logger.warning("photo_check bridge model %s unavailable, retry next", slug)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return ""


async def _photo_check_via_qwen(messages: list[dict]) -> str:
    from openai import AsyncOpenAI

    from ai_checker import VLLM_MODEL_DEFAULT, model_accepts_images

    base_url = (os.getenv("VLLM_BASE_URL") or "").strip()
    if not base_url or not model_accepts_images():
        raise RuntimeError("qwen vl not configured for photo check")
    api_key = os.getenv("VLLM_API_KEY", "") or "EMPTY"
    model = (os.getenv("VLLM_MODEL") or VLLM_MODEL_DEFAULT).strip()
    timeout = photo_check_timeout_s()
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
    max_tokens = _int_env("PHOTO_CHECK_MAX_TOKENS", 4096, lo=256, hi=16384)
    return await _completion_once(client, model, messages, max_tokens=max_tokens)


async def _run_messages(messages: list[dict]) -> str:
    from ai_checker import _fallback_ready

    if _fallback_ready():
        try:
            return await _photo_check_via_bridge(messages)
        except Exception:
            logger.exception("photo_check bridge failed")
            if not photo_check_qwen_fallback_enabled():
                raise
    elif not photo_check_qwen_fallback_enabled():
        raise RuntimeError("photo check: bridge not configured and qwen fallback disabled")
    return await _photo_check_via_qwen(messages)


async def run_photo_check(
    *,
    images: list[bytes],
    roles: list[ImageRole],
    mode: PhotoCheckMode,
) -> str:
    if os.getenv("AI_MOCK") == "1":
        return (
            f"[Тестовый режим /photo] Получено {len(images)} фото, mode={mode}.\n"
            "- Задача 1: mock-проверка.\n[tgzh_result:partial]"
        )

    if not images:
        return "Не получено ни одного изображения."

    try:
        struct_messages = build_photo_check_messages(
            images=images,
            roles=roles,
            mode=mode,
            stage="structure",
        )
        struct_raw = await _run_messages(struct_messages)
        parsed = parse_structure_json(struct_raw)
        if parsed and parsed.get("tasks"):
            verify_messages = build_photo_check_messages(
                images=images,
                roles=roles,
                mode=mode,
                stage="verify",
                structure_json=json.dumps(parsed, ensure_ascii=False),
            )
            verify_raw = await _run_messages(verify_messages)
            if verify_raw.strip():
                return verify_raw.strip()
    except Exception:
        logger.exception("photo_check two-stage failed, falling back to consolidated")

    consolidated = build_photo_check_messages(
        images=images,
        roles=roles,
        mode=mode,
        stage="consolidated",
    )
    out = await _run_messages(consolidated)
    return (out or "Не удалось получить ответ модели.").strip()
