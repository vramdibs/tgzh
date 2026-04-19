"""Запуск Cursor CLI (`agent -p`) для неинтерактивного ответа модели."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path


def _default_cwd() -> str:
    raw = (os.getenv("CURSOR_CLI_CWD") or "").strip()
    if raw:
        return raw
    return str(Path(__file__).resolve().parent)


def resolve_cli_binary() -> str | None:
    name = (os.getenv("CURSOR_CLI_BIN") or "agent").strip()
    if os.path.isabs(name) and os.path.isfile(name) and os.access(name, os.X_OK):
        return name
    return shutil.which(name)


def cli_configured() -> bool:
    return resolve_cli_binary() is not None


async def run_cursor_agent(
    prompt: str,
    image_path: str | None = None,
) -> str:
    if not prompt.strip():
        raise ValueError("empty prompt")
    exe = resolve_cli_binary()
    if not exe:
        bin_hint = (os.getenv("CURSOR_CLI_BIN") or "agent").strip()
        raise RuntimeError(
            f"Cursor CLI not found ({bin_hint!r} not in PATH). "
            "Install: curl https://cursor.com/install -fsS | bash"
        )

    cwd = _default_cwd()
    timeout = float((os.getenv("CURSOR_CLI_TIMEOUT_SEC") or "300").strip() or "300")
    if timeout < 30:
        timeout = 30.0

    text = prompt.strip()
    if image_path:
        text = (
            f"{text}\n\n"
            f"Analyze the image at this absolute path (read it via your tools): {image_path}"
        )

    fmt = (os.getenv("CURSOR_CLI_OUTPUT_FORMAT") or "text").strip() or "text"
    extra = shlex.split((os.getenv("CURSOR_CLI_EXTRA_ARGS") or "").strip())
    argv = [exe, *extra, "-p", "--output-format", fmt, text]

    env = os.environ.copy()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"cursor CLI exceeded {timeout}s") from None

    out = (out_b or b"").decode("utf-8", errors="replace").strip()
    err = (err_b or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        tail = (err or out)[:2000]
        raise RuntimeError(
            f"cursor CLI exit {proc.returncode}: {tail or '(no stderr)'}"
        )
    if not out and err:
        return err.strip()
    return out
