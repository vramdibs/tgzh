"""
Движок PaddleOCR: режим ocr (быстрый, ru) и structure (PP-StructureV3, формулы и графики).
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
from typing import Any

import numpy as np
from PIL import Image, ImageOps

# ВАЖНО: paddleocr/paddlex импортируем на уровне модуля. При ленивом импорте внутри
# request-хендлера paddlex (3.x) даёт RuntimeError "PDX has already been initialized"
# из-за реентерабельности _initialize() при загрузке под uvicorn worker-потоком.
# Импорт на уровне модуля гарантирует одноразовую инициализацию в main thread.
from paddleocr import PaddleOCR, PPStructureV3  # noqa: E402

from preocr.schemas import PreOcrRegion, PreOcrResponse, RegionType  # noqa: E402

logger = logging.getLogger(__name__)

_ocr_singleton: Any = None
_structure_singleton: Any = None
_singleton_lock = threading.Lock()


def _max_side() -> int:
    raw = (os.getenv("PREOCR_MAX_SIDE") or "").strip()
    if raw.isdigit():
        v = int(raw)
        return max(256, min(4096, v))
    return 1024


def _pipeline_name() -> str:
    p = (os.getenv("PREOCR_PIPELINE") or "ocr").strip().lower()
    return "structure" if p == "structure" else "ocr"


def _lang() -> str:
    return (os.getenv("PREOCR_OCR_LANG") or "ru").strip() or "ru"


def _prepare_image_bytes(data: bytes) -> tuple[str, int, int]:
    """
    Пишет временный PNG после EXIF и уменьшения по длинной стороне.
    Возвращает (path, width, height).
    """
    max_side = _max_side()
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    if w > 0 and h > 0 and max(w, h) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    w, h = im.size
    fd, path = tempfile.mkstemp(suffix=".png", prefix="preocr_")
    os.close(fd)
    im.save(path, format="PNG")
    return path, w, h


def _bbox_from_poly(poly: Any) -> list[list[float]] | None:
    if poly is None:
        return None
    try:
        arr = np.asarray(poly)
        if arr.shape[0] < 4 or arr.shape[1] != 2:
            return None
        return [[float(arr[i, 0]), float(arr[i, 1])] for i in range(arr.shape[0])]
    except Exception:
        return None


def _get_ocr():
    global _ocr_singleton
    if _ocr_singleton is None:
        with _singleton_lock:
            if _ocr_singleton is None:
                _ocr_singleton = PaddleOCR(
                    lang=_lang(),
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                )
    return _ocr_singleton


def _get_structure():
    global _structure_singleton
    if _structure_singleton is None:
        with _singleton_lock:
            if _structure_singleton is None:
                _structure_singleton = PPStructureV3(
                    lang=_lang(),
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_formula_recognition=True,
                    use_chart_recognition=True,
                    use_table_recognition=False,
                    use_seal_recognition=False,
                )
    return _structure_singleton


def warm_up() -> None:
    """Предзагрузка модели согласно PREOCR_PIPELINE — вызывается из FastAPI lifespan."""
    if _pipeline_name() == "structure":
        _get_structure()
    else:
        _get_ocr()


def _map_block_label(label: str) -> RegionType:
    if label == "formula":
        return "latex"
    if label in ("image", "chart"):
        return "figure"
    return "text"


def run_ocr(path: str) -> dict[str, Any]:
    ocr = _get_ocr()
    warn: list[str] = []
    out = ocr.predict(
        path,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        text_det_limit_side_len=_max_side(),
    )
    regions: list[PreOcrRegion] = []
    lines: list[str] = []
    if not out:
        return PreOcrResponse(
            pipeline="ocr",
            regions=[],
            merged_markdown="",
            warnings=["Пустой результат OCR"],
        ).model_dump()
    r0 = out[0]
    try:
        texts = r0["rec_texts"]
        scores = r0.get("rec_scores") or []
        polys = r0.get("rec_polys") or []
    except (KeyError, TypeError) as e:
        warn.append(f"Неожиданная структура OCR: {e}")
        return PreOcrResponse(pipeline="ocr", regions=[], merged_markdown="", warnings=warn).model_dump()

    for i, t in enumerate(texts):
        t = (t or "").strip()
        if not t:
            continue
        sc = float(scores[i]) if i < len(scores) else None
        bbox = _bbox_from_poly(polys[i]) if i < len(polys) else None
        regions.append(PreOcrRegion(type="text", text=t, score=sc, bbox=bbox))
        lines.append(t)

    merged = "\n".join(lines)
    return PreOcrResponse(
        pipeline="ocr",
        regions=regions,
        merged_markdown=merged,
        warnings=warn,
    ).model_dump()


def run_structure(path: str) -> dict[str, Any]:
    warn: list[str] = []
    try:
        pipe = _get_structure()
        out = pipe.predict(
            path,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_formula_recognition=True,
            use_chart_recognition=True,
            use_table_recognition=False,
            use_seal_recognition=False,
            text_det_limit_side_len=_max_side(),
        )
    except Exception as e:
        logger.exception("structure pipeline failed")
        warn.append(f"structure: {e}")
        return PreOcrResponse(
            pipeline="structure",
            regions=[],
            merged_markdown="",
            warnings=warn,
        ).model_dump()

    if not out:
        warn.append("Пустой результат layout")
        return PreOcrResponse(pipeline="structure", regions=[], merged_markdown="", warnings=warn).model_dump()

    res = out[0]
    merged = ""
    try:
        md_info = res.markdown
        merged = (md_info.get("markdown_texts") or "").strip()
    except Exception as e:
        warn.append(f"markdown: {e}")

    regions: list[PreOcrRegion] = []
    try:
        pages = res["parsing_res_list"]
        for page_blocks in pages:
            if not page_blocks:
                continue
            for block in page_blocks:
                label = getattr(block, "label", "") or ""
                rtype = _map_block_label(label)
                content = getattr(block, "content", "") or ""
                if isinstance(content, tuple):
                    content = str(content[0]) if content else ""
                else:
                    content = str(content).strip()
                if rtype == "figure" and not content:
                    content = f"[{label}]"
                bbox = None
                if getattr(block, "bbox", None) is not None:
                    bbox = _bbox_from_poly(block.bbox)
                regions.append(
                    PreOcrRegion(
                        type=rtype,
                        text=content,
                        score=None,
                        bbox=bbox,
                    )
                )
    except Exception as e:
        warn.append(f"regions: {e}")

    return PreOcrResponse(
        pipeline="structure",
        regions=regions,
        merged_markdown=merged,
        warnings=warn,
    ).model_dump()


def run_preocr(image_bytes: bytes) -> dict[str, Any]:
    path, _w, _h = _prepare_image_bytes(image_bytes)
    try:
        if _pipeline_name() == "structure":
            return run_structure(path)
        return run_ocr(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
