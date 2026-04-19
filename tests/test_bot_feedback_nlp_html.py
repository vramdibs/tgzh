"""HTML блока TEI в /begemot (без полного сценария бота)."""

from __future__ import annotations

import bot
import user_storage


def test_format_feedback_nlp_detail_html_sentiment_and_emotions() -> None:
    row = user_storage.FeedbackNlpRow(
        ticket_id=1,
        sentiment_label="negative",
        sentiment_score=0.91,
        emotions_json='[{"label":"anger","score":0.7}]',
        badges="\U0001f620",
        error=None,
    )
    html = bot._format_feedback_nlp_detail_html(row)
    assert "Тональность и эмоции" in html
    assert "negative" in html
    assert "anger" in html
    assert "TEI:" not in html


def test_format_feedback_nlp_detail_html_escapes_error() -> None:
    row = user_storage.FeedbackNlpRow(
        ticket_id=2,
        sentiment_label="",
        sentiment_score=0.0,
        emotions_json="[]",
        badges="\u2754",
        error='<script>x</script>',
    )
    html = bot._format_feedback_nlp_detail_html(row)
    assert "TEI:" in html
    assert "<script>" not in html
