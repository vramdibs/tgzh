"""Тикеты обращений (feedback_ticket) и смена статуса."""

from __future__ import annotations

import os
import tempfile

import user_storage


def test_resolve_feedback_ticket_reviewed() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.append_user_feedback(path, 7, "u", "hello")
        tix = user_storage.list_feedback_tickets_for_user(path, 7)
        assert len(tix) == 1
        tid = tix[0].id
        ok = user_storage.resolve_feedback_ticket(
            path,
            tid,
            user_storage.FEEDBACK_TICKET_STATUS_REVIEWED,
            "Спасибо, учтем",
        )
        assert ok is True
        again = user_storage.get_feedback_ticket_by_id(path, tid)
        assert again is not None
        assert again.status == user_storage.FEEDBACK_TICKET_STATUS_REVIEWED
        assert again.archived is True
        assert "учтем" in (again.staff_response or "")
        assert user_storage.list_feedback_tickets_for_user(path, 7) == []
        arch = user_storage.list_feedback_tickets_for_admin_user(path, 7, archived=True)
        assert len(arch) == 1
        assert arch[0].id == tid
        ok2 = user_storage.resolve_feedback_ticket(
            path,
            tid,
            user_storage.FEEDBACK_TICKET_STATUS_REJECTED,
            "x",
        )
        assert ok2 is False
    finally:
        os.unlink(path)


def test_resolve_feedback_ticket_rejected() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.append_user_feedback(path, 8, None, "spam")
        tid = user_storage.list_feedback_tickets_for_user(path, 8)[0].id
        ok = user_storage.resolve_feedback_ticket(
            path,
            tid,
            user_storage.FEEDBACK_TICKET_STATUS_REJECTED,
            "Не по теме бота",
        )
        assert ok is True
        row = user_storage.get_feedback_ticket_by_id(path, tid)
        assert row is not None
        assert row.status == user_storage.FEEDBACK_TICKET_STATUS_REJECTED
        assert row.archived is True
        assert user_storage.list_feedback_tickets_for_user(path, 8) == []
    finally:
        os.unlink(path)
