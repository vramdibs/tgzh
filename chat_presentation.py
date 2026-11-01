"""Сборка презентации конкурса «Спортивный муниципалитет года» из вложений /chat."""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from chat_documents import extract_pptx_text, pptx_slide_count

logger = logging.getLogger(__name__)

_FORM_LINE_COL = 2
# Столбцы формы 1-ФК (1-based): загруженность / мощность для строки 41 раздела III
_COL_LOAD = 13
_COL_CAPACITY = 14


def _num(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s or s in ("Х", "х", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_sheet(wb: Any, prefix: str) -> Any | None:
    for name in wb.sheetnames:
        if name == prefix or name.startswith(prefix):
            return wb[name]
    return None


def _row_by_form_line(ws: Any, line_no: int, *, max_scan: int = 250) -> list[Any] | None:
    for r in range(1, max_scan + 1):
        cell = ws.cell(r, _FORM_LINE_COL).value
        if cell is None:
            continue
        try:
            n = int(float(str(cell).strip()))
        except (ValueError, TypeError):
            continue
        if n == line_no:
            return [ws.cell(r, c).value for c in range(1, 20)]
    return None


def parse_1fk_workbook(data: bytes) -> dict[str, Any]:
    """Ключевые показатели приложения № 1 из формы 1-ФК."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {"error": "openpyxl не установлен"}

    out: dict[str, Any] = {}
    try:
        wb = load_workbook(io.BytesIO(data), read_only=False, data_only=True)
    except Exception as e:
        return {"error": f"не удалось открыть xlsx: {e}"}

    try:
        ws3 = _find_sheet(wb, "Раздел3")
        if ws3 is not None:
            row41 = _row_by_form_line(ws3, 41)
            if row41:
                load = _num(row41[_COL_LOAD - 1] if len(row41) >= _COL_LOAD else None)
                cap = _num(row41[_COL_CAPACITY - 1] if len(row41) >= _COL_CAPACITY else None)
                eff: float | None = None
                if load is not None and cap and cap > 0:
                    eff = round(load / cap * 100.0, 2)
                out["facility_efficiency_pct"] = eff
                out["facility_load"] = load
                out["facility_capacity"] = cap
                out["facility_line41_label"] = str(row41[0] or "").strip()

        ws5 = _find_sheet(wb, "Раздел5")
        if ws5 is not None:
            row93 = _row_by_form_line(ws5, 93, max_scan=30)
            if row93:
                c3 = _num(row93[2] if len(row93) > 2 else None)
                c4 = _num(row93[3] if len(row93) > 3 else None)
                out["sportspeople_total"] = c3
                out["sportspeople_prev_year"] = c4
                dyn: float | None = None
                if c3 is not None and c4 and c4 > 0:
                    dyn = round(c3 / c4 * 100.0, 2)
                out["sportspeople_dynamics_pct"] = dyn
                out["sportspeople_line93_label"] = str(row93[0] or "").strip()
    finally:
        wb.close()

    return out


def format_appendix1_metrics(metrics: dict[str, Any]) -> str:
    """Краткая сводка для промпта LLM."""
    if metrics.get("error"):
        return f"[1-ФК: {metrics['error']}]"
    lines = [
        "Показатели приложения № 1 (из формы 1-ФК):",
        "1. Финансирование ФКиС на 01.11.2026: [сумма позже — пользователь заполнит]",
    ]
    eff = metrics.get("facility_efficiency_pct")
    if eff is not None:
        lines.append(
            f"2. Эффективность использования спортсооружений (строка 41, раздел III): "
            f"{eff}% "
            f"(загруженность {metrics.get('facility_load')}, "
            f"мощность {metrics.get('facility_capacity')})",
        )
    else:
        lines.append("2. Эффективность спортсооружений: — (нет данных строки 41)")

    dyn = metrics.get("sportspeople_dynamics_pct")
    total = metrics.get("sportspeople_total")
    prev = metrics.get("sportspeople_prev_year")
    if total is not None:
        lines.append(
            f"3. Численность занимающихся видами спорта (строка 93): "
            f"{int(total) if total == int(total) else total} чел.",
        )
    if dyn is not None and prev:
        lines.append(
            f"   Динамика к прошлому году: {dyn}% "
            f"(было {int(prev) if prev == int(prev) else prev})",
        )
    elif total is not None and prev is not None:
        delta = total - prev
        lines.append(f"   Прирост к прошлому году: {int(delta) if delta == int(delta) else delta} чел.")

    lines.append(
        "4–7. Доли населения по возрастам и инфресурс — из текста черновика/регламента, "
        "если нет в 1-ФК; не выдумывать цифры.",
    )
    return "\n".join(lines)


def parse_slides_json(raw: str) -> list[dict[str, Any]]:
    """Разбор JSON слайдов из ответа LLM."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("пустой ответ модели")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    slides = data.get("slides") if isinstance(data, dict) else None
    if not isinstance(slides, list) or not slides:
        raise ValueError("нет массива slides")
    out: list[dict[str, Any]] = []
    for item in slides:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        bullets_raw = item.get("bullets")
        bullets: list[str] = []
        if isinstance(bullets_raw, list):
            bullets = [str(b).strip() for b in bullets_raw if str(b).strip()]
        elif isinstance(bullets_raw, str) and bullets_raw.strip():
            bullets = [bullets_raw.strip()]
        out.append({"title": title, "bullets": bullets})
    if not out:
        raise ValueError("slides пуст")
    return out


def _shape_text_len(shape: Any) -> int:
    if not getattr(shape, "has_text_frame", False):
        return 0
    try:
        return len(shape.text_frame.text or "")
    except Exception:
        return 0


def _set_shape_text(shape: Any, text: str) -> None:
    if not getattr(shape, "has_text_frame", False):
        return
    tf = shape.text_frame
    tf.clear()
    p = tf.paragraphs[0]
    p.text = text


def _set_shape_bullets(shape: Any, bullets: list[str]) -> None:
    if not getattr(shape, "has_text_frame", False):
        return
    tf = shape.text_frame
    tf.clear()
    if not bullets:
        return
    for i, line in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.level = 0


def apply_slides_to_pptx(template_bytes: bytes, slides: list[dict[str, Any]]) -> bytes:
    """Подставить title/bullets в текстовые фреймы слайдов шаблона."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(template_bytes))
    slide_objs = list(prs.slides)
    for idx, spec in enumerate(slides):
        if idx >= len(slide_objs):
            break
        slide = slide_objs[idx]
        text_shapes = [
            sh
            for sh in slide.shapes
            if getattr(sh, "has_text_frame", False) and _shape_text_len(sh) >= 0
        ]
        if not text_shapes:
            continue
        text_shapes.sort(
            key=lambda sh: (
                int(getattr(sh, "top", 0) or 0),
                int(getattr(sh, "left", 0) or 0),
            ),
        )
        title = str(spec.get("title") or "").strip()
        bullets = spec.get("bullets") or []
        if not isinstance(bullets, list):
            bullets = [str(bullets)]
        bullets = [str(b).strip() for b in bullets if str(b).strip()]

        if len(text_shapes) == 1:
            body = title
            if bullets:
                body = title + "\n" + "\n".join(bullets) if title else "\n".join(bullets)
            _set_shape_text(text_shapes[0], body.strip())
        else:
            if title:
                _set_shape_text(text_shapes[0], title)
            _set_shape_bullets(text_shapes[1], bullets)
            for extra in text_shapes[2:]:
                _set_shape_text(extra, "")

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def template_slide_text_summary(template_bytes: bytes) -> str:
    return extract_pptx_text(template_bytes)


def expected_slide_count(template_bytes: bytes) -> int:
    return pptx_slide_count(template_bytes)


def attachment_content_disposition(filename: str) -> str:
    """Content-Disposition safe for Starlette (latin-1 headers) with UTF-8 filename."""
    name = (filename or "presentation.pptx").strip()
    if not name.lower().endswith(".pptx"):
        name = "presentation.pptx"
    ascii_fallback = "presentation.pptx"
    encoded = quote(name, safe="")
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded}"
    )
