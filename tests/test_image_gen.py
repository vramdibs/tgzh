"""Юниты для `image_gen.generate_image` и хелперов конфигурации."""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ai_checker
import image_gen


def test_is_image_gen_configured_requires_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert image_gen.is_image_gen_configured() is False
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    assert image_gen.is_image_gen_configured() is False
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-test")
    assert image_gen.is_image_gen_configured() is True
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "   ")
    assert image_gen.is_image_gen_configured() is False


def test_image_gen_defaults_when_unset() -> None:
    assert image_gen.image_gen_model() == "gpt-image-1"
    assert image_gen.image_gen_size() == "1024x1024"
    assert image_gen.image_gen_timeout_sec() == 120.0


def test_image_gen_timeout_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "3")
    assert image_gen.image_gen_timeout_sec() == 10.0
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "9999")
    assert image_gen.image_gen_timeout_sec() == 600.0
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "abc")
    assert image_gen.image_gen_timeout_sec() == 120.0


def test_image_gen_multipart_timeout_default_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GEN_MULTIPART_TIMEOUT_SEC", raising=False)
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "180")
    assert image_gen.image_gen_multipart_timeout_sec() == 300.0
    assert image_gen.image_gen_bot_client_timeout_sec(multipart=True) == 1230.0


def test_image_gen_multipart_timeout_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_MULTIPART_TIMEOUT_SEC", "360")
    assert image_gen.image_gen_multipart_timeout_sec() == 360.0


def test_generate_image_raises_when_not_configured() -> None:
    with pytest.raises(image_gen.ImageGenNotConfigured):
        asyncio.run(image_gen.generate_image("cat"))


def test_generate_image_rejects_empty_and_long_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    with pytest.raises(image_gen.ImageGenBadPrompt):
        asyncio.run(image_gen.generate_image(""))
    with pytest.raises(image_gen.ImageGenBadPrompt):
        asyncio.run(image_gen.generate_image("a" * (image_gen.PROMPT_MAX_LEN + 1)))


def test_generate_image_returns_b64_decoded_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    raw = b"\x89PNG\r\n\x1a\nfake-bytes"
    payload_b64 = base64.b64encode(raw).decode("ascii")
    fake_resp = SimpleNamespace(
        data=[SimpleNamespace(b64_json=payload_b64, url=None)],
    )

    captured: dict[str, object] = {}

    class FakeImages:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return fake_resp

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.images = FakeImages()

    async def _run() -> tuple[bytes, ai_checker.CompletionUsage]:
        ai_checker.reset_completion_usage()
        with patch("openai.AsyncOpenAI", FakeClient):
            out = await image_gen.generate_image("cat sitting on a fence")
        return out, ai_checker.take_completion_usage()

    out, usage = asyncio.run(_run())

    assert out == raw
    assert usage.prompt_tokens >= 1
    assert usage.completion_tokens >= 1
    assert captured["model"] == "gpt-image-1"
    assert captured["size"] == "1024x1024"
    assert captured["n"] == 1
    assert captured["prompt"] == "cat sitting on a fence"
    # Не должен передавать response_format (gpt-image-1 не принимает).
    assert "response_format" not in captured


def test_generate_image_falls_back_to_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если бэкенд вернул только `url` (стиль dall-e-3) — модуль скачивает картинку."""
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    fake_resp = SimpleNamespace(
        data=[SimpleNamespace(b64_json=None, url="https://cdn.example/x.png")],
    )

    class FakeImages:
        async def generate(self, **kwargs):
            return fake_resp

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.images = FakeImages()

    fake_get_resp = SimpleNamespace(
        content=b"png-bytes-from-url",
        raise_for_status=lambda: None,
    )

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def get(self, url):
            assert url == "https://cdn.example/x.png"
            return fake_get_resp

    with (
        patch("openai.AsyncOpenAI", FakeClient),
        patch("httpx.AsyncClient", FakeHttpxClient),
    ):
        out = asyncio.run(image_gen.generate_image("dog"))

    assert out == b"png-bytes-from-url"


def test_generate_image_raises_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    fake_resp = SimpleNamespace(data=[])

    class FakeImages:
        async def generate(self, **kwargs):
            return fake_resp

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.images = FakeImages()

    with patch("openai.AsyncOpenAI", FakeClient):
        with pytest.raises(RuntimeError, match="empty data"):
            asyncio.run(image_gen.generate_image("x"))


def test_record_image_gen_usage_uses_response_when_present() -> None:
    ai_checker.reset_completion_usage()
    image_gen.record_image_gen_usage(
        {"usage": {"inputTokens": 5, "outputTokens": 15}},
        prompt="x",
    )
    usage = ai_checker.take_completion_usage()
    assert usage.prompt_tokens == 5
    assert usage.completion_tokens == 15


def test_estimated_image_gen_usage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_ESTIMATE_OUTPUT_TOKENS", "100")
    monkeypatch.setenv("IMAGE_GEN_ESTIMATE_SOURCE_IMAGE_TOKENS", "50")
    u = image_gen.estimated_image_gen_usage("abcd", includes_source_image=True)
    assert u.prompt_tokens == 1 + 50
    assert u.completion_tokens == 100


def test_generate_image_style_redraw_posts_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    style_file = tmp_path / "style.png"
    style_file.write_bytes(b"style-ref")
    monkeypatch.setenv("IMAGE_GEN_STYLE_REFERENCE_PATH", str(style_file))
    captured: dict = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"data": [{"b64_json": base64.b64encode(b"out-png").decode()}]},
            )

    with patch("httpx.AsyncClient", FakeHttpxClient):
        out = asyncio.run(
            image_gen.generate_image_style_redraw(b"photo", extra_prompt="notes"),
        )

    assert out == b"out-png"
    assert captured["url"] == "https://api.example/v1/images/generations"
    assert captured["json"]["source_image_b64"]
    assert captured["json"]["style_reference_b64"]
    assert captured["json"]["prompt"] == "notes"


def test_generate_image_style_redraw_empty_notes_uses_default_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    style_file = tmp_path / "style.png"
    style_file.write_bytes(b"style-ref")
    monkeypatch.setenv("IMAGE_GEN_STYLE_REFERENCE_PATH", str(style_file))
    captured: dict = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"data": [{"b64_json": base64.b64encode(b"x").decode()}]},
            )

    with patch("httpx.AsyncClient", FakeHttpxClient):
        asyncio.run(image_gen.generate_image_style_redraw(b"photo", extra_prompt=""))

    assert captured["json"]["prompt"] == image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "ярче" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "прищур" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "челюсть" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "узнаваемости сцены" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "Не подменяй брови" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "наклон" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "не дорисовывай крик" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "стиснут" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT
    assert "смазан" in image_gen.STYLE_REDRAW_DEFAULT_PROMPT


def test_image_gen_user_facing_label_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_MODEL", "auto")
    monkeypatch.delenv("BRIDGE_IMAGE_CURSOR_MODEL", raising=False)
    assert image_gen.image_gen_user_facing_label() == "Composer 2.5"


def test_image_gen_user_facing_label_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_MODEL", "gpt-image-1")
    assert image_gen.image_gen_user_facing_label() == "OpenAI Images"


def test_generate_image_edit_posts_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    captured: dict = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"data": [{"b64_json": base64.b64encode(b"out").decode()}]},
            )

    with patch("httpx.AsyncClient", FakeHttpxClient):
        out = asyncio.run(
            image_gen.generate_image_edit(b"photo", instruction="убери провода"),
        )

    assert out == b"out"
    assert captured["json"]["image_edit"] is True
    assert captured["json"]["source_image_b64"]
    assert "style_reference_b64" not in captured["json"]
    assert captured["json"]["prompt"] == "убери провода"


def test_generate_image_propagates_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "10")

    class FakeImages:
        async def generate(self, **kwargs):
            await asyncio.sleep(5)  # перекроется через wait_for(timeout=10), но мокнем wait_for
            return SimpleNamespace(data=[])

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.images = FakeImages()

    async def fake_wait_for(coro, timeout):
        coro.close()  # отменим внутренний awaitable, чтобы не ругался pytest
        raise asyncio.TimeoutError

    with (
        patch("openai.AsyncOpenAI", FakeClient),
        patch("asyncio.wait_for", new=fake_wait_for),
    ):
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(image_gen.generate_image("x"))


def test_resolve_image_gen_size_override_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GEN_SIZE", raising=False)
    assert image_gen.resolve_image_gen_size(None) == "1024x1024"
    assert image_gen.resolve_image_gen_size("256x256") == "256x256"
    monkeypatch.setenv("IMAGE_GEN_SIZE", "512x512")
    assert image_gen.resolve_image_gen_size(None) == "512x512"
    assert image_gen.resolve_image_gen_size("1024x1024") == "1024x1024"


def test_resolve_original_max1280_from_image_bytes() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), color="red").save(buf, format="PNG")
    out = image_gen.resolve_image_gen_size(
        image_gen.CHAT_IMAGINE_SIZE_ORIGINAL,
        source_bytes=buf.getvalue(),
    )
    assert out == "1280x640"


def test_is_api_client_image_size_accepts_original_token() -> None:
    assert image_gen.is_api_client_image_size(image_gen.CHAT_IMAGINE_SIZE_ORIGINAL)
    assert image_gen.is_api_client_image_size("800x600")
    assert not image_gen.is_api_client_image_size("2000x1000")


def test_image_gen_size_allowlist_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_SIZE", "9999x9999")
    assert image_gen.image_gen_size() == "1024x1024"
    monkeypatch.setenv("IMAGE_GEN_SIZE", "1024x1024")
    assert image_gen.image_gen_size() == "1024x1024"


@pytest.mark.asyncio
async def test_bridge_pool_two_urls_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "IMAGE_GEN_BASE_URLS",
        "https://bridge-a.example/v1,https://bridge-b.example/v1",
    )
    monkeypatch.delenv("IMAGE_GEN_BASE_URL", raising=False)
    image_gen.reset_image_gen_bridge_pool()
    async with image_gen.image_gen_bridge_slot() as u1:
        async with image_gen.image_gen_bridge_slot() as u2:
            assert u1 != u2
            assert {u1, u2} == {
                "https://bridge-a.example/v1",
                "https://bridge-b.example/v1",
            }


@pytest.mark.asyncio
async def test_bridge_pool_third_waits_until_slot_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "IMAGE_GEN_BASE_URLS",
        "https://bridge-a.example/v1,https://bridge-b.example/v1",
    )
    image_gen.reset_image_gen_bridge_pool()
    hold = asyncio.Event()
    third_started = asyncio.Event()

    async def _hold_bridge() -> None:
        async with image_gen.image_gen_bridge_slot():
            await hold.wait()

    t1 = asyncio.create_task(_hold_bridge())
    t2 = asyncio.create_task(_hold_bridge())
    await asyncio.sleep(0.02)

    async def _third() -> None:
        async with image_gen.image_gen_bridge_slot():
            third_started.set()

    t3 = asyncio.create_task(_third())
    await asyncio.sleep(0.03)
    assert not third_started.is_set()
    hold.set()
    await asyncio.gather(t1, t2, t3)
    assert third_started.is_set()


def test_image_gen_base_urls_csv_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GEN_BASE_URLS", raising=False)
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://one.example/v1/")
    image_gen.reset_image_gen_bridge_pool()
    assert image_gen.image_gen_base_urls() == ["https://one.example/v1"]
    monkeypatch.setenv(
        "IMAGE_GEN_BASE_URLS",
        "https://a.example/v1, https://b.example/v1 ,https://a.example/v1",
    )
    assert image_gen.image_gen_base_urls() == [
        "https://a.example/v1",
        "https://b.example/v1",
    ]
    assert image_gen.image_gen_max_concurrent() == 2
    assert image_gen.image_gen_style_album_limit() == 2


def test_image_gen_pool_cap_twelve_and_album_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = ",".join(f"https://b{i}.example/v1" for i in range(12))
    monkeypatch.setenv("IMAGE_GEN_BASE_URLS", urls)
    monkeypatch.delenv("IMAGE_GEN_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("IMAGE_GEN_BASE_URL", raising=False)
    image_gen.reset_image_gen_bridge_pool()
    assert image_gen.image_gen_max_concurrent() == 12
    assert image_gen.image_gen_style_album_limit() == 10
    monkeypatch.setenv(
        "IMAGE_GEN_BASE_URLS",
        urls + ",https://b12.example/v1",
    )
    monkeypatch.setenv("IMAGE_GEN_MAX_CONCURRENT", "13")
    image_gen.reset_image_gen_bridge_pool()
    assert image_gen.image_gen_max_concurrent() == 12
    assert image_gen.image_gen_style_album_limit() == 10


def test_prepare_bridge_source_bytes_shrinks_large_jpeg() -> None:
    from PIL import Image

    buf = __import__("io").BytesIO()
    im = Image.new("RGB", (2400, 1800), color=(128, 64, 32))
    im.save(buf, format="JPEG", quality=95)
    raw = buf.getvalue()
    assert len(raw) > 30_000
    out = image_gen.prepare_bridge_source_bytes(raw)
    assert len(out) < len(raw)
    out_im = Image.open(__import__("io").BytesIO(out))
    assert max(out_im.size) <= image_gen.image_gen_source_max_side()


def test_style_redraw_original_size_from_unshrunk_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from PIL import Image

    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.setenv("IMAGE_GEN_SOURCE_MAX_SIDE", "768")
    style_file = tmp_path / "style.png"
    style_file.write_bytes(b"style-ref")
    monkeypatch.setenv("IMAGE_GEN_STYLE_REFERENCE_PATH", str(style_file))
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), color=(10, 20, 30)).save(buf, format="JPEG")
    source_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    captured: dict = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"data": [{"b64_json": base64.b64encode(b"x").decode()}]},
            )

    source = image_gen.decode_source_image_b64(source_b64)
    with patch("httpx.AsyncClient", FakeHttpxClient):
        asyncio.run(
            image_gen.generate_image_style_redraw(
                source,
                size=image_gen.CHAT_IMAGINE_SIZE_ORIGINAL,
            ),
        )

    assert captured["json"]["size"] == "1280x640"
    sent = Image.open(io.BytesIO(base64.b64decode(captured["json"]["source_image_b64"])))
    assert sent.size == (768, 384)
