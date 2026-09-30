"""`/photo`: приоритет `_PHOTO_CHECK_ACTIVE` над `/chat` и ДЗ."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import bot
import user_storage


@pytest.fixture
def db_path(monkeypatch: pytest.MonkeyPatch) -> str:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    user_storage.init_db(path)
    monkeypatch.setattr(bot, "USER_DB_PATH", path)
    yield path
    os.unlink(path)


class _FakeUserData(dict):
    pass


class _FakeBot:
    async def send_message(self, *_a: Any, **_kw: Any) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.user_data = _FakeUserData()
        self.bot = _FakeBot()


class _FakePhoto:
    file_id = "fid:photo"


class _FakeMessage:
    def __init__(self, chat_id: int = 1) -> None:
        self.chat_id = chat_id
        self.message_id = 100
        self.media_group_id = None
        self.photo = [_FakePhoto()]


class _FakeUpdate:
    effective_user = type("U", (), {"id": 555})()
    effective_chat = type("C", (), {"id": 1})()
    message = _FakeMessage()


@pytest.mark.asyncio
async def test_photo_check_active_routes_before_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    ctx.user_data[bot._PHOTO_CHECK_ACTIVE] = True
    ctx.user_data[bot._PHOTO_CHECK_MODE] = "single_album"
    ctx.user_data[bot._CHAT_ACTIVE] = True

    dispatch = AsyncMock()
    chat_photo = AsyncMock()
    monkeypatch.setattr(bot, "_dispatch_photo_check_upload", dispatch)
    monkeypatch.setattr(bot, "_handle_chat_photo", chat_photo)
    monkeypatch.setattr(bot, "_safe_blocked_state", AsyncMock(return_value=(None, True)))

    upd = _FakeUpdate()
    await bot.handle_photo(upd, ctx)

    dispatch.assert_awaited_once()
    chat_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_check_stores_mixed_role(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    ctx.user_data[bot._PHOTO_CHECK_ACTIVE] = True
    ctx.user_data[bot._PHOTO_CHECK_MODE] = "single_album"
    ctx.user_data[bot._PHOTO_CHECK_MIXED_SINGLE] = True

    send_status = AsyncMock()
    monkeypatch.setattr(bot, "_send_photo_check_status", send_status)

    upd = _FakeUpdate()
    await bot._dispatch_photo_check_upload(
        upd,
        ctx,
        user_id=555,
        photo_file_id="fid1",
        message_id=10,
        media_group_id=None,
    )

    entries = ctx.user_data.get(bot._PHOTO_CHK_ENTRIES)
    assert entries
    assert entries[0][2] == "mixed"


@pytest.mark.asyncio
async def test_photo_run_callback_invokes_check(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    ctx.user_data[bot._PHOTO_CHECK_ACTIVE] = True
    ctx.user_data[bot._PHOTO_CHK_ENTRIES] = [(1, "fid1", "unspecified")]

    run_check = AsyncMock()
    monkeypatch.setattr(bot, "_run_photo_check_request", run_check)

    class _FakeQuery:
        from_user = type("U", (), {"id": 555})()
        message = type("M", (), {"chat_id": 1, "message_id": 42})()

        async def edit_message_text(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(bot, "_answer_query_once", AsyncMock())

    await bot._handle_photo_check_callback(_FakeQuery(), ctx, "photo:run")

    run_check.assert_awaited_once()
    assert run_check.await_args.kwargs["user_id"] == 555


@pytest.mark.asyncio
async def test_photo_check_result_returns_to_shot_upload(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
) -> None:
    assert db_path
    ctx = _FakeContext()
    ctx.user_data[bot._PHOTO_CHECK_ACTIVE] = True
    ctx.user_data[bot._PHOTO_CHK_ENTRIES] = [(1, "fid1", "mixed")]
    sent: list[tuple[str, Any]] = []
    edits: list[Any] = []

    class _File:
        async def download_as_bytearray(self) -> bytearray:
            return bytearray(b"jpeg")

    class _Bot:
        async def get_file(self, _fid: str) -> _File:
            return _File()

        async def send_message(self, _chat_id: int, text: str, **kw: Any) -> None:
            sent.append((text, kw.get("reply_markup")))

    ctx.bot = _Bot()

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"result": "Задача 1: верно"}

    class _Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def post(self, *_a: Any, **_kw: Any) -> _Resp:
            return _Resp()

    async def _edit(_text: str, **kw: Any) -> None:
        edits.append(kw.get("reply_markup"))

    async def _typing(_bot: Any, _chat_id: int, coro: Any) -> Any:
        return await coro

    monkeypatch.setattr(bot.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(bot, "async_http_transport_ipv4_lookup", lambda: None)
    monkeypatch.setattr(bot, "_hub_check_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "run_with_typing", _typing)

    await bot._run_photo_check_request(
        ctx,
        user_id=555,
        chat_id=1,
        status_msg_id=1,
        edit_message=_edit,
    )

    def _callbacks(markup: Any) -> list[str]:
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    result_cbs = _callbacks(edits[-1])
    assert "cfv:1" in result_cbs
    assert "show_sol" not in result_cbs
    assert "answer_text" not in result_cbs
    assert "chg_hw" not in result_cbs

    assert sent
    assert "photo:mode:single_album" in _callbacks(sent[-1][1])
    assert ctx.user_data[bot._PHOTO_CHECK_ACTIVE] is True
    assert not ctx.user_data.get(bot._PHOTO_CHK_ENTRIES)


@pytest.mark.asyncio
async def test_photo_check_result_offers_textbook_task_buttons(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
) -> None:
    assert db_path
    ctx = _FakeContext()
    ctx.user_data[bot._PHOTO_CHECK_ACTIVE] = True
    ctx.user_data[bot._PHOTO_CHK_ENTRIES] = [(1, "fid1", "mixed")]
    edits: list[Any] = []

    class _File:
        async def download_as_bytearray(self) -> bytearray:
            return bytearray(b"jpeg")

    class _Bot:
        async def get_file(self, _fid: str) -> _File:
            return _File()

        async def send_message(self, *_a: Any, **_kw: Any) -> None:
            return None

    ctx.bot = _Bot()

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"result": "Задача 6: верно.\n[tgzh_result:partial]\n[tgzh_offer:10,11]"}

    class _Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def post(self, *_a: Any, **_kw: Any) -> _Resp:
            return _Resp()

    async def _edit(_text: str, **kw: Any) -> None:
        edits.append((_text, kw.get("reply_markup")))

    async def _typing(_bot: Any, _chat_id: int, coro: Any) -> Any:
        return await coro

    monkeypatch.setattr(bot.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(bot, "async_http_transport_ipv4_lookup", lambda: None)
    monkeypatch.setattr(bot, "_hub_check_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "run_with_typing", _typing)

    await bot._run_photo_check_request(
        ctx,
        user_id=555,
        chat_id=1,
        status_msg_id=1,
        edit_message=_edit,
    )

    text, markup = edits[-1]
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "photo:ex:10" in cbs
    assert "photo:ex:11" in cbs
    assert "cfv:1" in cbs
    assert "tgzh_offer" not in text.lower()
    assert "Нажми номер" in text
    assert ctx.user_data[bot._PHOTO_EXPLAIN_ENTRIES] == [("fid1", "mixed")]


@pytest.mark.asyncio
async def test_photo_explain_callback_posts_task_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    ctx.user_data[bot._PHOTO_EXPLAIN_ENTRIES] = [("fid1", "mixed")]
    posted: list[Any] = []
    edits: list[str] = []

    class _File:
        async def download_as_bytearray(self) -> bytearray:
            return bytearray(b"jpeg")

    class _WaitMsg:
        async def edit_text(self, text: str, **_kw: Any) -> None:
            edits.append(text)

    class _Bot:
        async def get_file(self, _fid: str) -> _File:
            return _File()

        async def send_message(self, *_a: Any, **_kw: Any) -> _WaitMsg:
            return _WaitMsg()

    ctx.bot = _Bot()

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"result": "Ход: 731 - 296 = 435.\nОтвет: 435."}

    class _Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def post(self, *_a: Any, **kw: Any) -> _Resp:
            posted.append(kw.get("files"))
            return _Resp()

    async def _typing(_bot: Any, _chat_id: int, coro: Any) -> Any:
        return await coro

    monkeypatch.setattr(bot.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(bot, "async_http_transport_ipv4_lookup", lambda: None)
    monkeypatch.setattr(bot, "_hub_check_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "run_with_typing", _typing)

    await bot._run_photo_explain_request(
        ctx,
        user_id=555,
        chat_id=1,
        task_no="10",
    )

    assert posted
    names = [item[0] for item in posted[0]]
    assert "explain_task" in names
    explain_part = next(item for item in posted[0] if item[0] == "explain_task")
    assert explain_part[1][1] == "10"
    assert edits
    assert "435" in edits[-1]
    assert "✅" not in edits[-1]


def test_photo_check_multipart_is_async_stream() -> None:
    """httpx 0.28: multipart с mode/roles внутри files должен быть async-совместим.

    Если положить текстовые поля в отдельный `data=`-список, поток становится
    только-sync IteratorByteStream и AsyncClient.send падает с
    "Attempted to send an sync request with an AsyncClient instance".
    """
    blob = b"\xff\xd8\xff\x00\x01\x02"
    roles = ["mixed", "unspecified"]
    multipart_files: list[tuple[str, tuple]] = [("mode", (None, "single_album"))]
    for r in roles:
        multipart_files.append(("image_roles", (None, r)))
    for i in range(2):
        multipart_files.append(("images", (f"photo_{i + 1}.jpg", blob, "image/jpeg")))

    client = httpx.AsyncClient()
    req = client.build_request("POST", "http://example.invalid/photo/check", files=multipart_files)
    assert isinstance(req.stream, httpx.AsyncByteStream)
