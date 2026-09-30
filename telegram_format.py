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


_FRAC_RE = re.compile(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_WRAP_CMD_RE = re.compile(
    r"\\(?:mathbf|mathrm|text|textbf|mathit|operatorname)\s*\{([^{}]*)\}"
)
_LATEX_SYMBOLS: tuple[tuple[str, str], ...] = (
    (r"\\cdot", " · "),
    (r"\\times", " × "),
    (r"\\div", ":"),
    (r"\\pm", "±"),
    (r"\\leq", "≤"),
    (r"\\geq", "≥"),
    (r"\\neq", "≠"),
    (r"\\approx", "≈"),
    (r"\\left", ""),
    (r"\\right", ""),
    (r"\\,", " "),
    (r"\\;", " "),
    (r"\\quad", " "),
    (r"\\qquad", " "),
)
_LINE_SPLIT_RE = re.compile(r"^(\s*)((?:[-*•]\s+)?)(.*)$")
_VERNO_WORD_RE = re.compile(r"(?<![а-яёА-ЯЁ*])верно(?![а-яёА-ЯЁ*])", re.IGNORECASE)
_ERROR_LINE_RE = re.compile(r"неверно|ошибк|неправильн", re.IGNORECASE)
_UNCLEAR_LINE_RE = re.compile(
    r"нельзя|неясно|непонятно|не читается|нет условия|не удалось",
    re.IGNORECASE,
)
# Нет формулировки на снимке учебника - строка со знаком вопроса, фраза жирным.
_MISSING_CONDITION_RE = re.compile(
    r"("
    r"текст\w*\s+задач\w*[^.\n]{0,80}?нет"
    r"|формулировк\w*[^.\n]{0,80}?не\s+видн\w*"
    r")",
    re.IGNORECASE,
)
_TASK_NUMBER_RE = re.compile(
    r"(?<!\*)(Задач[аеи]\s*№\s*\d+(?:\s*\([^)\n]{1,40}\))?)",
    re.IGNORECASE,
)
_ANSWER_BREAK_RE = re.compile(r"(?<=\S)\s+(?=ответ\b)", re.IGNORECASE)
_MARK_OK = "✅"
_MARK_BAD = "❌"
_MARK_UNK = "❓"
_MARKS = (_MARK_OK, _MARK_BAD, _MARK_UNK)
_MARK_TOKEN = r"(?:✅|❌|❓|✓|✔|✗|✕|✖)(?:\ufe0f)?"
_ANY_MARK_RE = re.compile(rf"{_MARK_TOKEN}\s*")
_SPLIT_MARK_RE = re.compile(rf"({_MARK_TOKEN})")


def _plain_math_from_latex(text: str) -> str:
    """LaTeX-обрывки модели -> обычная запись. Обратные слэши в показ не попадают."""
    t = text.replace(r"\(", "").replace(r"\)", "")
    t = t.replace(r"\[", "").replace(r"\]", "")
    t = t.replace(r"\$", "")
    t = re.sub(r"\$([^$\n]+)\$", r"\1", t)
    t = t.replace("$", "")
    for _ in range(8):
        nxt = _FRAC_RE.sub(
            lambda m: f"{m.group(1).strip()}/{m.group(2).strip()}",
            t,
        )
        nxt = _WRAP_CMD_RE.sub(lambda m: m.group(1), nxt)
        if nxt == t:
            break
        t = nxt
    for pat, repl in _LATEX_SYMBOLS:
        t = re.sub(pat, repl, t)
    t = re.sub(r"\\[a-zA-Z]+\s*", "", t)
    t = t.replace("\\", "")
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t


def _strip_leading_mark(body: str) -> tuple[str, str]:
    stripped = body.lstrip()
    pad = body[: len(body) - len(stripped)]
    for mark in _MARKS:
        if stripped.startswith(mark):
            rest = stripped[len(mark):]
            if rest.startswith("\ufe0f"):
                rest = rest[1:]
            return mark, pad + rest.lstrip()
    return "", body


def _canonical_mark(mark: str) -> str:
    bare = mark.replace("\ufe0f", "")
    if bare in ("✓", "✔", _MARK_OK):
        return _MARK_OK
    if bare in ("✗", "✕", "✖", _MARK_BAD):
        return _MARK_BAD
    return _MARK_UNK


def _split_marked_chunks(body: str) -> list[tuple[str, str]]:
    """Текст и значки проверки. Пустой знак - фрагмент до первой пометки."""
    parts = _SPLIT_MARK_RE.split(body)
    if len(parts) == 1:
        return [("", body)]
    chunks: list[tuple[str, str]] = []
    head = parts[0].strip()
    if head:
        chunks.append(("", head))
    idx = 1
    while idx < len(parts):
        mark = _canonical_mark(parts[idx])
        text = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
        chunks.append((mark, text))
        idx += 2
    return chunks or [("", body)]


def _annotate_check_line(line: str) -> str:
    if not line.strip():
        return line
    matched = _LINE_SPLIT_RE.match(line)
    if matched is None:
        return line
    indent, bullet, body = matched.group(1), matched.group(2), matched.group(3)
    existing, body = _strip_leading_mark(body)
    if existing:
        existing = _canonical_mark(existing)
    low = _ANY_MARK_RE.sub("", body).replace("*", "").lower()
    if existing == _MARK_BAD or _ERROR_LINE_RE.search(low):
        kind = "bad"
    elif existing == _MARK_UNK or _UNCLEAR_LINE_RE.search(low) or _MISSING_CONDITION_RE.search(low):
        kind = "unk"
    elif existing == _MARK_OK or _VERNO_WORD_RE.search(low):
        kind = "ok"
    else:
        return f"{indent}{bullet}{body}" if bullet else line
    body = _ANY_MARK_RE.sub("", body).strip()
    if kind == "ok":
        body = _VERNO_WORD_RE.sub(lambda m: f"**{m.group(0)}**", body)
    mark = {"ok": _MARK_OK, "bad": _MARK_BAD, "unk": _MARK_UNK}[kind]
    return f"{indent}{mark} {body}".rstrip()


def _expand_check_line(line: str) -> list[str]:
    """Пункт с несколькими примерами -> заголовок и отдельная строка на пример."""
    if not line.strip():
        return [line]
    matched = _LINE_SPLIT_RE.match(line)
    if matched is None:
        return [_annotate_check_line(line)]
    indent, body = matched.group(1), matched.group(3)
    chunks = _split_marked_chunks(body)
    if not any(mark for mark, _text in chunks):
        return [_annotate_check_line(line)]
    out: list[str] = []
    rest = chunks
    if chunks[0][0] == "":
        head = chunks[0][1].strip()
        if head:
            out.append(f"{indent}{head}")
        rest = chunks[1:]
    for mark, text in rest:
        out.append(_annotate_check_line(f"{indent}{mark} {text}".rstrip()))
    return out or [_annotate_check_line(line)]


def _polish_check_line(line: str) -> str:
    """Жирным: номер задачи и фразы про невидимую формулировку в учебнике."""
    if not line.strip():
        return line
    line = _TASK_NUMBER_RE.sub(lambda m: f"**{m.group(1)}**", line)

    def _bold_missing(match: re.Match[str]) -> str:
        phrase = match.group(1)
        if phrase.startswith("**"):
            return phrase
        return f"**{phrase}**"

    return _MISSING_CONDITION_RE.sub(_bold_missing, line)


def _break_answer_lines(line: str) -> list[str]:
    """Слово "ответ" начинает новую строку."""
    if not line.strip():
        return [line]
    parts = _ANSWER_BREAK_RE.split(line)
    if len(parts) == 1:
        return [line]
    out = [parts[0].rstrip()]
    out.extend(part.strip() for part in parts[1:] if part.strip())
    return out


def prepare_check_display_text(text: str) -> str:
    """
    Текст проверки для чата: без LaTeX и слэшей, одна пометка на пример, **верно**.
    Не использовать для подсчета вердикта.
    """
    if not text:
        return ""
    plain = _plain_math_from_latex(text)
    lines: list[str] = []
    for line in plain.split("\n"):
        for piece in _break_answer_lines(line):
            lines.extend(_expand_check_line(piece))
    return "\n".join(_polish_check_line(line) for line in lines)


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


# --- Полноценный конвертер markdown → Telegram HTML (для /chat) -----------------
# Telegram HTML понимает только: b/strong, i/em, u/ins, s/strike/del, code, pre,
# pre><code class="language-…">, a, span class="tg-spoiler", blockquote.
# Никаких таблиц, заголовков, списков как блочных конструкций. Заголовки рендерим
# жирным+перенос; маркер списка превращаем в "• "; нумерованные оставляем как есть.

_MD_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"(?<!\*)\*\*(?!\s)([^*\n][^*]*?)\*\*(?!\*)|__([^_\n][^_]*?)__")
# Курсив: одиночные * с пробелами вокруг = умножение, не трогаем. Используем _ … _.
_MD_ITALIC_RE = re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)")
_MD_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MD_HRULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")

_PLACEHOLDER_PREFIX = "\x00TGFMT\x00"


def _placeholder(idx: int) -> str:
    return f"{_PLACEHOLDER_PREFIX}{idx}\x00"


def _restore_placeholders(text: str, store: list[str]) -> str:
    if not store:
        return text
    out = text
    for i, val in enumerate(store):
        out = out.replace(_placeholder(i), val)
    return out


def markdown_to_telegram_html(text: str) -> str:
    """Markdown → Telegram-совместимый HTML. Безопасен для **`parse_mode=HTML`**.

    Поддерживает: ```fenced code```, `inline code`, **bold**, __bold__, *italic*…
    нет (одиночная `*` оставлена под умножение), `_italic_`, `~~strike~~`,
    [text](url), маркированные/нумерованные списки (как `• …`), заголовки `# …`
    (рендерятся **жирным** + перевод строки), горизонтальные линии — пустая строка.
    Прочее — обычный текст, html-экранированный.

    На вход допустим частично сформированный markdown (стрим LLM): лишние ``` или
    `*` без пары не выкидывают исключения, обрабатываются «как есть» (экранируются).
    """
    if not text:
        return ""

    store: list[str] = []

    def _store_pre(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        body = match.group(2)
        body_esc = html.escape(body, quote=False)
        if lang:
            html_block = (
                f'<pre><code class="language-{html.escape(lang, quote=True)}">{body_esc}</code></pre>'
            )
        else:
            html_block = f"<pre>{body_esc}</pre>"
        store.append(html_block)
        return _placeholder(len(store) - 1)

    def _store_inline_code(match: re.Match[str]) -> str:
        body_esc = html.escape(match.group(1), quote=False)
        store.append(f"<code>{body_esc}</code>")
        return _placeholder(len(store) - 1)

    def _store_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        url = match.group(2).strip()
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            store.append(label)
            return _placeholder(len(store) - 1)
        url_attr = html.escape(url, quote=True)
        store.append(f'<a href="{url_attr}">{label}</a>')
        return _placeholder(len(store) - 1)

    work = _MD_FENCE_RE.sub(_store_pre, text)
    work = _MD_INLINE_CODE_RE.sub(_store_inline_code, work)
    work = _MD_LINK_RE.sub(_store_link, work)

    out_lines: list[str] = []
    for raw_line in work.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            out_lines.append("")
            continue

        if _MD_HRULE_RE.match(line):
            out_lines.append("")
            continue

        h = _MD_HEADING_RE.match(line)
        if h:
            content = h.group(2).strip()
            content = _apply_inline_md(content)
            out_lines.append(f"<b>{content}</b>")
            continue

        b = _MD_BULLET_RE.match(line)
        if b:
            indent = b.group(1)
            item = b.group(2).strip()
            item = _apply_inline_md(item)
            level = len(indent) // 2
            prefix = "  " * level + "• "
            out_lines.append(html.escape(prefix, quote=False) + item)
            continue

        out_lines.append(_apply_inline_md(line))

    body = "\n".join(out_lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return _restore_placeholders(body, store)


def _apply_inline_md(text: str) -> str:
    """Bold/italic/strike + html.escape для остального. Используется внутри строк."""
    if not text:
        return ""
    store: list[str] = []

    def _bold(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        store.append(f"<b>{html.escape(inner, quote=False)}</b>")
        return _placeholder(len(store) - 1)

    def _italic(m: re.Match[str]) -> str:
        store.append(f"<i>{html.escape(m.group(1), quote=False)}</i>")
        return _placeholder(len(store) - 1)

    def _strike(m: re.Match[str]) -> str:
        store.append(f"<s>{html.escape(m.group(1), quote=False)}</s>")
        return _placeholder(len(store) - 1)

    work = _MD_BOLD_RE.sub(_bold, text)
    work = _MD_STRIKE_RE.sub(_strike, work)
    work = _MD_ITALIC_RE.sub(_italic, work)
    work = html.escape(work, quote=False)
    return _restore_placeholders(work, store)
