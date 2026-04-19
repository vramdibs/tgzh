"""
Анализ текста обратной связи через Text Embeddings Inference (POST /predict):
sentiment (sequence classification) и go_emotions (мульти-лейбл).

Переменные окружения (оба необязательны; пусто — анализ отключен):
TEI_SENTIMENT_URL — базовый URL сервиса (например http://host:8081)
TEI_EMOTION_URL — базовый URL второго сервиса (например http://host:8082)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from tgzh_httpx import async_http_transport_ipv4_lookup

logger = logging.getLogger(__name__)

_TEI_TIMEOUT = 20.0
_MAX_INPUT_CHARS = 2000

_HOSTILE_EMOTIONS = frozenset(
    {
        "anger",
        "annoyance",
        "disapproval",
        "disgust",
        "contempt",
        "grief",
    },
)


def tei_feedback_analysis_enabled() -> bool:
    return bool(_base_url("TEI_SENTIMENT_URL") or _base_url("TEI_EMOTION_URL"))


def _base_url(env_name: str) -> str:
    return (os.getenv(env_name) or "").strip().rstrip("/")


def _normalize_predictions(payload: Any) -> list[dict[str, Any]]:
    """Ответ TEI /predict: список Prediction или вложенный список для батча."""
    if not payload:
        return []
    if not isinstance(payload, list):
        return []
    first = payload[0]
    if isinstance(first, list):
        return [x for x in first if isinstance(x, dict)]
    if isinstance(first, dict):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _best_sentiment(preds: list[dict[str, Any]]) -> tuple[str, float]:
    if not preds:
        return "", 0.0
    best = max(preds, key=lambda x: float(x.get("score") or 0.0))
    return str(best.get("label") or ""), float(best.get("score") or 0.0)


def _top_emotions(preds: list[dict[str, Any]], *, limit: int = 8, min_score: float = 0.22) -> list[dict[str, Any]]:
    scored: list[tuple[str, float]] = []
    for p in preds:
        lab = str(p.get("label") or "").strip()
        sc = float(p.get("score") or 0.0)
        if lab and sc >= min_score:
            scored.append((lab, sc))
    scored.sort(key=lambda t: t[1], reverse=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lab, sc in scored:
        key = lab.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": lab, "score": round(sc, 4)})
        if len(out) >= limit:
            break
    return out


def build_nlp_badges(sentiment_label: str, emotions: list[dict[str, Any]]) -> str:
    """Короткая строка значков для админки /begemot."""
    sl = (sentiment_label or "").lower()
    parts: list[str] = []
    if sl:
        if "negative" in sl or sl.endswith("_neg") or sl == "neg":
            parts.append("\U0001f620")
        elif "positive" in sl or sl.endswith("_pos") or sl == "pos":
            parts.append("\U0001f642")
        else:
            parts.append("\U0001f610")
    else:
        parts.append("\U0001f4ac")

    hostile_max = 0.0
    for e in emotions:
        lab = str(e.get("label") or "").lower().replace(" ", "_")
        sc = float(e.get("score") or 0.0)
        for h in _HOSTILE_EMOTIONS:
            if h in lab or lab == h:
                hostile_max = max(hostile_max, sc)
                break
    if hostile_max >= 0.35:
        parts.append("\U0001f525")
    if hostile_max >= 0.5:
        parts.append("\U000026a1")
    if ("negative" in sl or "neg" in sl) and hostile_max >= 0.28:
        parts.append("\U000026a0\ufe0f")

    return "".join(parts)


async def _post_predict(client: httpx.AsyncClient, base: str, text: str) -> list[dict[str, Any]]:
    url = f"{base}/predict"
    r = await client.post(
        url,
        json={"inputs": text, "truncate": True},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    return _normalize_predictions(data)


async def analyze_feedback_text(text: str) -> dict[str, Any] | None:
    """
    Возвращает словарь для upsert_feedback_ticket_nlp:
    sentiment_label, sentiment_score, emotions_json, badges; либо None если оба URL пусты.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    raw = raw[:_MAX_INPUT_CHARS]

    s_base = _base_url("TEI_SENTIMENT_URL")
    e_base = _base_url("TEI_EMOTION_URL")
    if not s_base and not e_base:
        return None

    sentiment_label = ""
    sentiment_score = 0.0
    emotion_preds: list[dict[str, Any]] = []
    errors: list[str] = []

    async with httpx.AsyncClient(
        timeout=_TEI_TIMEOUT,
        transport=async_http_transport_ipv4_lookup(),
    ) as client:
        if s_base:
            try:
                sp = await _post_predict(client, s_base, raw)
                sentiment_label, sentiment_score = _best_sentiment(sp)
            except Exception as e:
                logger.warning("TEI sentiment failed: %s", e)
                errors.append(f"sentiment:{e!s}"[:200])
        if e_base:
            try:
                ep = await _post_predict(client, e_base, raw)
                emotion_preds = _top_emotions(ep)
            except Exception as e:
                logger.warning("TEI emotion failed: %s", e)
                errors.append(f"emotion:{e!s}"[:200])

    if not sentiment_label and not emotion_preds and errors:
        return {
            "sentiment_label": "",
            "sentiment_score": 0.0,
            "emotions_json": "[]",
            "badges": "\u2754",
            "error": "; ".join(errors),
        }

    emotions_json = json.dumps(emotion_preds, ensure_ascii=False)
    badges = build_nlp_badges(sentiment_label, emotion_preds)
    err = "; ".join(errors) if errors else None
    return {
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "emotions_json": emotions_json,
        "badges": badges,
        "error": err,
    }
