"""Юнит-тесты cursor_cli_client (без живого agent)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import cursor_cli_client


def test_default_cwd_without_env(monkeypatch):
    monkeypatch.delenv("CURSOR_CLI_CWD", raising=False)
    expected = str(Path(cursor_cli_client.__file__).resolve().parent)
    assert cursor_cli_client._default_cwd() == expected


def test_default_cwd_with_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CURSOR_CLI_CWD", str(tmp_path))
    assert cursor_cli_client._default_cwd() == str(tmp_path)


def test_resolve_cli_binary_respects_absolute_executable(tmp_path, monkeypatch):
    fake = tmp_path / "my-agent"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    assert cursor_cli_client.resolve_cli_binary() == str(fake)


def test_resolve_cli_binary_missing(monkeypatch):
    monkeypatch.setenv("CURSOR_CLI_BIN", "nonexistent-binary-xyz-12345")
    monkeypatch.delenv("PATH", raising=False)
    assert cursor_cli_client.resolve_cli_binary() is None


def test_resolve_cli_binary_via_which(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    agent = bindir / "agent"
    agent.write_text("#!/bin/sh\necho ok\n")
    agent.chmod(0o755)
    monkeypatch.delenv("CURSOR_CLI_BIN", raising=False)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert cursor_cli_client.resolve_cli_binary() == str(agent)


def test_cli_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("CURSOR_CLI_BIN", "nonexistent-binary-xyz-12345")
    monkeypatch.delenv("PATH", raising=False)
    assert cursor_cli_client.cli_configured() is False

    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    assert cursor_cli_client.cli_configured() is True


@pytest.mark.asyncio
async def test_run_cursor_agent_requires_binary(monkeypatch):
    monkeypatch.setenv("CURSOR_CLI_BIN", "nonexistent-binary-xyz-12345")
    monkeypatch.delenv("PATH", raising=False)
    with pytest.raises(RuntimeError, match="Cursor CLI not found"):
        await cursor_cli_client.run_cursor_agent("hello")


@pytest.mark.asyncio
async def test_run_cursor_agent_empty_prompt():
    with pytest.raises(ValueError, match="empty prompt"):
        await cursor_cli_client.run_cursor_agent("   ")
    with pytest.raises(ValueError, match="empty prompt"):
        await cursor_cli_client.run_cursor_agent("")


@pytest.mark.asyncio
async def test_run_cursor_agent_success_stdout(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\necho answer\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    out = await cursor_cli_client.run_cursor_agent("  hi  ")
    assert out == "answer"


@pytest.mark.asyncio
async def test_run_cursor_agent_nonzero_exit(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\necho err-msg >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    with pytest.raises(RuntimeError, match="err-msg"):
        await cursor_cli_client.run_cursor_agent("hi")


@pytest.mark.asyncio
async def test_run_cursor_agent_returns_stderr_when_stdout_empty(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\necho only-stderr >&2\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    out = await cursor_cli_client.run_cursor_agent("hi")
    assert out == "only-stderr"


@pytest.mark.asyncio
async def test_run_cursor_agent_uses_cursor_cli_cwd(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\npwd\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    work = tmp_path / "workdir"
    work.mkdir()
    monkeypatch.setenv("CURSOR_CLI_CWD", str(work))
    out = await cursor_cli_client.run_cursor_agent("hi")
    assert out.strip() == str(work)


@pytest.mark.asyncio
async def test_run_cursor_agent_appends_image_path_to_prompt(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf "%s\\n" "$a"; done\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"x")
    raw = await cursor_cli_client.run_cursor_agent("look", str(img))
    lines = raw.splitlines()
    assert "-p" in lines
    assert "--output-format" in lines
    assert "text" in lines
    assert "look" in raw
    assert str(img) in raw
    assert "Analyze the image" in raw


@pytest.mark.asyncio
async def test_run_cursor_agent_extra_args_and_output_format(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf "%s\\n" "$a"; done\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    monkeypatch.setenv("CURSOR_CLI_EXTRA_ARGS", "--verbose")
    monkeypatch.setenv("CURSOR_CLI_OUTPUT_FORMAT", "json")
    out = await cursor_cli_client.run_cursor_agent("prompt")
    lines = out.splitlines()
    assert lines[0] == "--verbose"
    assert "-p" in lines
    assert "--output-format" in lines
    idx = lines.index("--output-format")
    assert lines[idx + 1] == "json"
    assert lines[-1] == "prompt"


@pytest.mark.asyncio
async def test_run_cursor_agent_timeout_floor_applied_to_wait_for(tmp_path, monkeypatch):
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    monkeypatch.setenv("CURSOR_CLI_TIMEOUT_SEC", "5")
    timeouts: list[float] = []
    orig = asyncio.wait_for

    async def capture(coro, timeout):
        timeouts.append(timeout)
        return await orig(coro, timeout=timeout)

    with patch.object(asyncio, "wait_for", capture):
        await cursor_cli_client.run_cursor_agent("x")
    assert timeouts == [30.0]


@pytest.mark.asyncio
async def test_run_cursor_agent_timeout_kills_process(monkeypatch):
    killed: list[bool] = []

    class FakeProc:
        returncode = -9

        async def communicate(self):
            await asyncio.sleep(10**6)
            return b"", b""

        def kill(self) -> None:
            killed.append(True)

        async def wait(self):
            return 0

    proc = FakeProc()
    monkeypatch.setenv("CURSOR_CLI_TIMEOUT_SEC", "100")

    async def instant_timeout(coro, *, timeout=None):
        if asyncio.iscoroutine(coro):
            coro.close()
        raise asyncio.TimeoutError()

    with patch.object(
        asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc
    ):
        with patch.object(asyncio, "wait_for", instant_timeout):
            with pytest.raises(TimeoutError, match="cursor CLI exceeded 100"):
                await cursor_cli_client.run_cursor_agent("x")
    assert killed == [True]
