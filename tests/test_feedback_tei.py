"""Значки и разбор ответа TEI /predict для обратной связи."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import feedback_tei


def test_normalize_predictions_flat() -> None:
    preds = [{"label": "a", "score": 0.5}]
    assert feedback_tei._normalize_predictions(preds) == preds


def test_normalize_predictions_nested() -> None:
    nested = [[{"label": "x", "score": 0.9}]]
    assert feedback_tei._normalize_predictions(nested) == [{"label": "x", "score": 0.9}]


def test_normalize_predictions_empty_or_nonlist() -> None:
    assert feedback_tei._normalize_predictions([]) == []
    assert feedback_tei._normalize_predictions(None) == []
    assert feedback_tei._normalize_predictions({}) == []
    assert feedback_tei._normalize_predictions("x") == []


def test_normalize_predictions_first_not_dict_or_list() -> None:
    assert feedback_tei._normalize_predictions([1, 2]) == []


def test_normalize_predictions_nested_non_dict_filtered() -> None:
    assert feedback_tei._normalize_predictions([[{"label": "a", "score": 0.1}, "skip", 3]]) == [
        {"label": "a", "score": 0.1},
    ]


def test_best_sentiment_empty() -> None:
    assert feedback_tei._best_sentiment([]) == ("", 0.0)


def test_best_sentiment_picks_highest_score() -> None:
    preds = [
        {"label": "neg", "score": 0.3},
        {"label": "pos", "score": 0.71},
    ]
    assert feedback_tei._best_sentiment(preds) == ("pos", 0.71)


def test_top_emotions_respects_min_score_limit_dedup() -> None:
    preds = [
        {"label": "joy", "score": 0.5},
        {"label": "Joy", "score": 0.4},
        {"label": "low", "score": 0.1},
    ]
    out = feedback_tei._top_emotions(preds, limit=1, min_score=0.22)
    assert len(out) == 1
    assert out[0]["label"] == "joy"
    assert out[0]["score"] == 0.5


def test_tei_feedback_analysis_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEI_SENTIMENT_URL", raising=False)
    monkeypatch.delenv("TEI_EMOTION_URL", raising=False)
    assert feedback_tei.tei_feedback_analysis_enabled() is False
    monkeypatch.setenv("TEI_EMOTION_URL", "http://127.0.0.1:2")
    assert feedback_tei.tei_feedback_analysis_enabled() is True


def test_build_nlp_badges_negative_hostile() -> None:
    emo = [{"label": "anger", "score": 0.9}]
    b = feedback_tei.build_nlp_badges("negative", emo)
    assert "\U0001f620" in b
    assert "\U0001f525" in b


def test_build_nlp_badges_positive() -> None:
    b = feedback_tei.build_nlp_badges("positive", [])
    assert "\U0001f642" in b


def test_build_nlp_badges_neutral() -> None:
    b = feedback_tei.build_nlp_badges("neutral", [])
    assert "\U0001f610" in b


def test_build_nlp_badges_no_sentiment() -> None:
    b = feedback_tei.build_nlp_badges("", [{"label": "joy", "score": 0.8}])
    assert "\U0001f4ac" in b


def _response_ok(data: object) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=data)
    return r


@pytest.mark.asyncio
async def test_analyze_feedback_text_uses_ipv4_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(
        feedback_tei,
        "async_http_transport_ipv4_lookup",
        lambda: sentinel,
    )

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, *a: object, **k: object) -> MagicMock:
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=[])
            return r

    factory = MagicMock(side_effect=lambda **kw: FakeClient(**kw))
    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", factory)
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TEI_EMOTION_URL", "")

    await feedback_tei.analyze_feedback_text("текст отзыва")

    factory.assert_called_once()
    assert factory.call_args.kwargs.get("transport") is sentinel
    assert factory.call_args.kwargs.get("timeout") == feedback_tei._TEI_TIMEOUT


@pytest.mark.asyncio
async def test_analyze_feedback_text_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEI_SENTIMENT_URL", raising=False)
    monkeypatch.delenv("TEI_EMOTION_URL", raising=False)
    assert await feedback_tei.analyze_feedback_text("hello") is None


@pytest.mark.asyncio
async def test_analyze_feedback_text_none_when_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:1")
    assert await feedback_tei.analyze_feedback_text("   \n") is None


@pytest.mark.asyncio
async def test_analyze_feedback_text_strips_trailing_slash_on_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[str] = []

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> MagicMock:
            posts.append(url)
            return _response_ok([{"label": "x", "score": 0.5}])

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://example.test:8081/")
    monkeypatch.setenv("TEI_EMOTION_URL", "")

    await feedback_tei.analyze_feedback_text("hi")
    assert posts == ["http://example.test:8081/predict"]


@pytest.mark.asyncio
async def test_analyze_feedback_text_both_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, json: dict, **kwargs: object) -> MagicMock:
            calls.append((url, str(json.get("inputs", ""))))
            if "8081" in url:
                return _response_ok([{"label": "negative", "score": 0.88}])
            return _response_ok(
                [
                    {"label": "annoyance", "score": 0.4},
                    {"label": "joy", "score": 0.1},
                ]
            )

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("TEI_EMOTION_URL", "http://127.0.0.1:8082")

    out = await feedback_tei.analyze_feedback_text("bad service")
    assert out is not None
    assert out["sentiment_label"] == "negative"
    assert out["sentiment_score"] == 0.88
    assert "annoyance" in out["emotions_json"]
    assert out["error"] is None
    assert "\U0001f620" in (out["badges"] or "")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_analyze_feedback_text_both_http_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> MagicMock:
            raise httpx.ConnectError("All connection attempts failed", request=MagicMock())

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("TEI_EMOTION_URL", "http://127.0.0.1:8082")

    out = await feedback_tei.analyze_feedback_text("x")
    assert out is not None
    assert out["sentiment_label"] == ""
    assert out["emotions_json"] == "[]"
    assert out["badges"] == "\u2754"
    assert out["error"] is not None
    assert "sentiment:" in out["error"]
    assert "emotion:" in out["error"]


@pytest.mark.asyncio
async def test_analyze_feedback_text_partial_emotion_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> MagicMock:
            if "8081" in url:
                return _response_ok([{"label": "positive", "score": 0.9}])
            raise httpx.TimeoutException("timeout", request=MagicMock())

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("TEI_EMOTION_URL", "http://127.0.0.1:8082")

    out = await feedback_tei.analyze_feedback_text("ok")
    assert out is not None
    assert out["sentiment_label"] == "positive"
    assert out["error"] is not None
    assert "emotion:" in out["error"]


@pytest.mark.asyncio
async def test_analyze_feedback_text_only_emotion_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> MagicMock:
            return _response_ok([{"label": "joy", "score": 0.9}])

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.delenv("TEI_SENTIMENT_URL", raising=False)
    monkeypatch.setenv("TEI_EMOTION_URL", "http://127.0.0.1:8082")

    out = await feedback_tei.analyze_feedback_text("yay")
    assert out is not None
    assert out["sentiment_label"] == ""
    assert "joy" in out["emotions_json"]
    assert out["error"] is None


@pytest.mark.asyncio
async def test_analyze_feedback_text_truncates_to_max_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, json: dict, **kwargs: object) -> MagicMock:
            captured.append(str(json.get("inputs", "")))
            return _response_ok([])

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TEI_EMOTION_URL", "")

    long_text = "a" * 5000
    await feedback_tei.analyze_feedback_text(long_text)
    assert len(captured) == 1
    assert len(captured[0]) == feedback_tei._MAX_INPUT_CHARS


@pytest.mark.asyncio
async def test_analyze_feedback_text_post_predict_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
            bodies.append(dict(json or {}))
            return _response_ok([])

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TEI_EMOTION_URL", "")

    await feedback_tei.analyze_feedback_text("probe")
    assert bodies == [{"inputs": "probe", "truncate": True}]


@pytest.mark.asyncio
async def test_analyze_feedback_text_http_status_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> MagicMock:
            r = MagicMock()
            r.raise_for_status.side_effect = httpx.HTTPStatusError(
                "err",
                request=MagicMock(),
                response=MagicMock(status_code=503),
            )
            return r

    monkeypatch.setattr(feedback_tei.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("TEI_SENTIMENT_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("TEI_EMOTION_URL", raising=False)

    out = await feedback_tei.analyze_feedback_text("x")
    assert out is not None
    assert out["sentiment_label"] == ""
    assert out["badges"] == "\u2754"
    assert out["error"] is not None
    assert "sentiment:" in out["error"]
