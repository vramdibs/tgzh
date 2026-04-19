"""
Разметка для Telegram: API понимает HTML (или MarkdownV2), а не классический **markdown**.
"""

from __future__ import annotations

import html
import re

_BOLD_SEGMENTS = re.compile(r"(\*\*[^*]+\*\*)")

# Длинные фразы первыми, чтобы не резать по короткому префиксу
_LLM_CHECK_PARAGRAPH_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "В остальных заданиях",
            "В остальных",
            "В первом фото",
            "В первом",
            "Во втором фото",
            "Во втором",
            "В третьем фото",
            "В третьем",
            "В четвёртом фото",
            "В четвертом фото",
            "В четвертом",
            "В пятом фото",
            "В пятом",
            "В целом,",
            "В целом",
        ),
        key=len,
        reverse=True,
    ),
)


def format_llm_check_reply_plain(text: str) -> str:
    """
    Улучшает читаемость ответа проверки ДЗ: тире —/– в ASCII -, абзацы перед типичными вступлениями.
    Не вызывать для строки, по которой считается вердикт (stats) - только для показа в чате.
    """
    if not text:
        return ""
    t = text.strip()
    t = t.replace("\u2014", "-").replace("\u2013", "-")
    for pfx in _LLM_CHECK_PARAGRAPH_PREFIXES:
        t = t.replace(". " + pfx, ".\n\n" + pfx)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t


def markdownish_to_telegram_html(text: str) -> str:
    """
    **жирный** -> <b>жирный</b>, остальное экранируется под parse_mode=HTML.
    Одиночные * не трогаем (часто умножение в тексте задач).
    """
    if not text:
        return ""
    parts: list[str] = []
    for i, seg in enumerate(_BOLD_SEGMENTS.split(text)):
        if i % 2 == 1 and seg.startswith("**") and seg.endswith("**") and len(seg) >= 4:
            inner = seg[2:-2]
            parts.append("<b>" + html.escape(inner, quote=False) + "</b>")
        else:
            parts.append(html.escape(seg, quote=False))
    return "".join(parts)
