"""Юниты для `chat_memory`: safe-IO, лимиты, парсер sleep-ответа, snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

import chat_memory


@pytest.fixture(autouse=True)
def _isolated_memory_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Каждый тест работает в своём пустом каталоге памяти."""
    monkeypatch.setenv("MEMORY_DIR_BASE", str(tmp_path))
    return tmp_path


# ====================== safe filenames ======================


@pytest.mark.parametrize(
    "name",
    [
        "MEMORY.md",
        "projects.md",
        "Plans-Q1.md",
        "a.md",
        "long_topic_name_with_underscores_42.md",
    ],
)
def test_is_safe_memory_filename_accepts_good_names(name: str) -> None:
    assert chat_memory.is_safe_memory_filename(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../etc/passwd",
        "foo/bar.md",
        "..",
        ".hidden.md",
        "-rf.md",
        "no_extension",
        "double..dot.md",
        "with space.md",
        "way_too_long_filename_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.md",
        "wrong.txt",
        "weird;name.md",
    ],
)
def test_is_safe_memory_filename_rejects_unsafe(name: str) -> None:
    assert chat_memory.is_safe_memory_filename(name) is False


# ====================== read/write/list/delete ======================


def test_read_returns_empty_when_missing() -> None:
    assert chat_memory.read_memory_file(42, "no_such.md") == ""


def test_read_rejects_unsafe_name() -> None:
    # Никакого "../" — даже если файл существует за пределами каталога.
    assert chat_memory.read_memory_file(42, "../escape.md") == ""


def test_write_then_read_roundtrip() -> None:
    ok = chat_memory.write_memory_file(7, "projects.md", "Hello\nWorld\n")
    assert ok is True
    assert "Hello" in chat_memory.read_memory_file(7, "projects.md")


def test_write_normalizes_crlf_and_trailing_blank_lines() -> None:
    chat_memory.write_memory_file(1, "n.md", "a\r\nb   \r\n\r\n\r\n\r\n")
    out = chat_memory.read_memory_file(1, "n.md")
    assert "\r" not in out
    assert out.endswith("\n")
    # Обрезаем подряд более 2 пустых строк.
    assert "\n\n\n\n" not in out


def test_write_truncates_index_to_byte_limit() -> None:
    big = "x" * (chat_memory.MEMORY_INDEX_MAX_BYTES + 5_000)
    chat_memory.write_memory_file(2, "MEMORY.md", big)
    saved = chat_memory.read_memory_file(2, "MEMORY.md")
    assert len(saved.encode("utf-8")) <= chat_memory.MEMORY_INDEX_MAX_BYTES


def test_write_rejects_unsafe_name() -> None:
    assert chat_memory.write_memory_file(3, "../boom.md", "x") is False


def test_list_includes_index_first() -> None:
    chat_memory.write_memory_file(9, "z_topic.md", "z")
    chat_memory.write_memory_file(9, "MEMORY.md", "idx")
    chat_memory.write_memory_file(9, "a_topic.md", "a")
    files = chat_memory.list_memory_files(9)
    assert files[0] == "MEMORY.md"
    # тематические — в алфавитном порядке
    assert files[1:] == ["a_topic.md", "z_topic.md"]


def test_list_skips_unsafe_files_on_disk(tmp_path: Path) -> None:
    d = chat_memory.memory_dir(11)
    (d / ".hidden.md").write_text("x")
    (d / "ok.md").write_text("y")
    files = chat_memory.list_memory_files(11)
    assert "ok.md" in files
    assert ".hidden.md" not in files


def test_delete_removes_file() -> None:
    chat_memory.write_memory_file(5, "p.md", "x")
    assert chat_memory.delete_memory_file(5, "p.md") is True
    assert chat_memory.read_memory_file(5, "p.md") == ""
    assert chat_memory.delete_memory_file(5, "p.md") is False


def test_purge_removes_everything() -> None:
    chat_memory.write_memory_file(8, "MEMORY.md", "i")
    chat_memory.write_memory_file(8, "a.md", "1")
    chat_memory.write_memory_file(8, "b.md", "2")
    n = chat_memory.purge_memory(8)
    assert n == 3
    assert chat_memory.list_memory_files(8) == []


# ====================== snapshot + system prompt ======================


def test_memory_snapshot_text_empty_returns_empty_string() -> None:
    assert chat_memory.memory_snapshot_text(99) == ""


def test_memory_snapshot_text_contains_index_and_topics_in_order() -> None:
    chat_memory.write_memory_file(1, "MEMORY.md", "idx body")
    chat_memory.write_memory_file(1, "projects.md", "proj body")
    chat_memory.write_memory_file(1, "preferences.md", "pref body")
    snap = chat_memory.memory_snapshot_text(1)
    assert "## MEMORY.md" in snap
    assert "## projects.md" in snap
    assert "## preferences.md" in snap
    # Индекс — первым.
    assert snap.index("## MEMORY.md") < snap.index("## projects.md")


def test_memory_snapshot_text_truncates_when_over_cap() -> None:
    # Маленький cap, чтобы влезли только первые блоки.
    chat_memory.write_memory_file(1, "MEMORY.md", "x" * 1_000)
    chat_memory.write_memory_file(1, "a.md", "y" * 1_000)
    chat_memory.write_memory_file(1, "b.md", "z" * 1_000)
    snap = chat_memory.memory_snapshot_text(1, max_bytes=1_500)
    assert "truncated" in snap


def test_system_prompt_with_memory_returns_base_when_no_memory() -> None:
    out = chat_memory.system_prompt_with_memory("base sys", "")
    assert out == "base sys"


def test_system_prompt_with_memory_appends_memory_section() -> None:
    out = chat_memory.system_prompt_with_memory("base sys", "X")
    assert "base sys" in out
    assert "X" in out
    assert "Долговременная память" in out


# ====================== parse_sleep_response ======================


def test_parse_sleep_response_extracts_file_blocks() -> None:
    text = (
        "preamble (ignored)\n"
        "<<<FILE:MEMORY.md>>>\n"
        "* projects.md — что строим\n"
        "<<<END>>>\n"
        "<<<FILE:projects.md>>>\n"
        "Project A: ...\n"
        "<<<END>>>\n"
        "trailing junk"
    )
    res = chat_memory.parse_sleep_response(text)
    names = [op.name for op in res.write_ops]
    assert names == ["MEMORY.md", "projects.md"]
    assert "Project A" in res.write_ops[1].content


def test_parse_sleep_response_handles_delete_blocks() -> None:
    text = "<<<DELETE:old_topic.md>>>"
    res = chat_memory.parse_sleep_response(text)
    assert len(res.delete_ops) == 1
    assert res.delete_ops[0].name == "old_topic.md"


def test_parse_sleep_response_protects_index_from_delete() -> None:
    text = "<<<DELETE:MEMORY.md>>>"
    res = chat_memory.parse_sleep_response(text)
    assert res.delete_ops == []
    assert any("MEMORY.md" in w for w in res.parse_warnings)


def test_parse_sleep_response_rejects_unsafe_filename() -> None:
    text = (
        "<<<FILE:../etc/passwd>>>\n"
        "x\n"
        "<<<END>>>\n"
        "<<<DELETE:../boom.md>>>\n"
    )
    res = chat_memory.parse_sleep_response(text)
    assert res.write_ops == []
    assert res.delete_ops == []
    assert len(res.parse_warnings) >= 2


def test_parse_sleep_response_empty_returns_empty() -> None:
    res = chat_memory.parse_sleep_response("")
    assert res.write_ops == [] and res.delete_ops == [] and res.parse_warnings == []


# ====================== apply_sleep_result ======================


def test_apply_sleep_result_writes_and_deletes() -> None:
    chat_memory.write_memory_file(4, "old.md", "stale")
    res = chat_memory.parse_sleep_response(
        "<<<FILE:MEMORY.md>>>\nidx\n<<<END>>>\n"
        "<<<FILE:fresh.md>>>\nnew\n<<<END>>>\n"
        "<<<DELETE:old.md>>>\n"
    )
    stats = chat_memory.apply_sleep_result(4, res)
    assert stats.written == 2
    assert stats.deleted == 1
    files = chat_memory.list_memory_files(4)
    assert files == ["MEMORY.md", "fresh.md"]


def test_apply_sleep_result_caps_topic_count() -> None:
    # Заполним до лимита, потом попробуем добавить новый — должен пропуститься.
    for i in range(chat_memory.MEMORY_MAX_TOPIC_FILES):
        ok = chat_memory.write_memory_file(6, f"topic_{i}.md", "x")
        assert ok
    res = chat_memory.parse_sleep_response(
        "<<<FILE:overflow.md>>>\nx\n<<<END>>>\n"
        # Существующий — должен переписаться без проблем.
        "<<<FILE:topic_0.md>>>\nupd\n<<<END>>>\n"
    )
    stats = chat_memory.apply_sleep_result(6, res)
    assert stats.written == 1  # только топик_0 переписали
    assert stats.skipped_total_cap == 1
    assert "overflow.md" not in chat_memory.list_memory_files(6)


def test_build_sleep_messages_includes_memory_and_transcript() -> None:
    chat_memory.write_memory_file(1, "MEMORY.md", "INDEX-CONTENT")
    msgs = chat_memory.build_sleep_messages(
        1,
        transcript=[
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Привет!"},
        ],
    )
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert "сон" in msgs[0]["content"].lower()
    assert msgs[1]["role"] == "user"
    body = msgs[1]["content"]
    assert "INDEX-CONTENT" in body
    assert "Привет" in body
    assert "Фаза 1" in body or "Фаза 3" in body
    assert "<<<FILE:" in body  # инструкция формата
