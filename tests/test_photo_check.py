"""Модуль /photo: сбор multimodal-сообщений и mock-прогон."""

from __future__ import annotations

import io

import pytest
from PIL import Image

import photo_check


def _tiny_jpeg() -> bytes:
    im = Image.new("RGB", (8, 8), color=(128, 64, 32))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


def test_role_label_mixed() -> None:
    assert "одном снимке" in photo_check.role_label_for_index(1, "mixed")


def test_build_messages_splits_more_than_four_images() -> None:
    images = [_tiny_jpeg() for _ in range(5)]
    roles: list[photo_check.ImageRole] = ["unspecified"] * 5
    msgs = photo_check.build_photo_check_messages(
        images=images,
        roles=roles,
        mode="single_album",
        stage="consolidated",
    )
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 2
    first_content = user_msgs[0]["content"]
    assert isinstance(first_content, list)
    img_parts = [p for p in first_content if p.get("type") == "image_url"]
    assert len(img_parts) == 4


def test_parse_structure_json_extracts_block() -> None:
    raw = 'noise {"tasks": [{"id": "9", "condition_photos": [1], "solution_photos": [1], "same_frame": true}], "ignored": []}'
    parsed = photo_check.parse_structure_json(raw)
    assert parsed is not None
    assert parsed["tasks"][0]["same_frame"] is True


@pytest.mark.asyncio
async def test_run_photo_check_ai_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    out = await photo_check.run_photo_check(
        images=[_tiny_jpeg()],
        roles=["mixed"],
        mode="single_album",
    )
    assert "mock" in out.lower() or "Тестовый" in out


def test_photo_check_model_slugs_default() -> None:
    slugs = photo_check.photo_check_model_slugs()
    assert "composer-2.5" in slugs


def _system_prompt(stage: str) -> str:
    msgs = photo_check.build_photo_check_messages(
        images=[_tiny_jpeg()],
        roles=["unspecified"],
        mode="single_album",
        stage=stage,
    )
    system = next(m for m in msgs if m["role"] == "system")
    return str(system["content"])


def test_photo_check_prompts_match_solution_to_condition() -> None:
    for stage in ("structure", "verify", "consolidated"):
        text = _system_prompt(stage)
        assert "Якорь - рукопись ученика" in text, stage
        assert "Не дописывай условие, которого не видно" in text, stage
        assert "Печатный номер задачи на странице учебника" in text, stage
        assert "[tgzh_offer:" not in text, stage
        assert "написан рукой рядом с решением" in text, stage
        assert "Контрпример" in text, stage
        if stage in ("verify", "consolidated"):
            assert "Маркер списка не заменяй" in text, stage
            assert "дефис" in text, stage
            assert "выражение после запятой" in text, stage
            assert "c : 3" in text, stage
