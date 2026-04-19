"""Таблица feedback_ticket_nlp."""

from __future__ import annotations

import os
import tempfile

import user_storage


def test_upsert_and_get_nlp() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        uid, tid = user_storage.append_user_feedback(path, 99, "x", "hello")
        assert uid == 99
        assert tid > 0
        user_storage.upsert_feedback_ticket_nlp(
            path,
            tid,
            sentiment_label="negative",
            sentiment_score=0.91,
            emotions_json='[{"label":"anger","score":0.7}]',
            badges="😠🔥",
            error=None,
        )
        m = user_storage.get_feedback_nlp_by_ticket_ids(path, [tid])
        assert tid in m
        assert m[tid].badges == "😠🔥"
        assert m[tid].sentiment_label == "negative"
        ids = user_storage.list_feedback_ticket_ids_for_user_recent_asc(path, 99, 5)
        assert ids == [tid]

        user_storage.upsert_feedback_ticket_nlp(
            path,
            tid,
            sentiment_label="",
            sentiment_score=0.0,
            emotions_json="[]",
            badges="\u2754",
            error="sentiment:ConnectError('fail')",
        )
        m2 = user_storage.get_feedback_nlp_by_ticket_ids(path, [tid])
        assert m2[tid].error is not None
        assert "ConnectError" in m2[tid].error
    finally:
        os.unlink(path)
