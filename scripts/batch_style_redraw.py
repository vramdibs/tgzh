#!/usr/bin/env python3
"""Пачка style_redraw через POST /image/generate.

Тот же путь, что кнопка «Перерисовать в стиле примера»: mode=style_redraw,
size по умолчанию original_max1280 (пропорции исходника, длинная сторона до 1280).
Нужен запущенный tgzh-server (SERVER_URL) и настроенный IMAGE_GEN_*.

Пример:

  python scripts/batch_style_redraw.py --input ./in --output ./out --limit 2
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import image_gen  # noqa: E402

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ERROR_BODY_MAX = 400


def _list_sources(input_dir: Path) -> list[Path]:
    files = [
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    ]
    return sorted(files, key=lambda p: p.name.lower())


def _output_png(output_dir: Path, source: Path) -> Path:
    return output_dir / f"{source.stem}.png"


def _already_done(dest: Path) -> bool:
    if not dest.is_file():
        return False
    try:
        if dest.stat().st_size < len(_PNG_MAGIC):
            return False
        with dest.open("rb") as f:
            return f.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


def _server_url(raw: str | None) -> str:
    value = (raw or os.environ.get("SERVER_URL") or "http://127.0.0.1:8000").strip()
    return value.rstrip("/")


async def _check_health(client: httpx.AsyncClient, base: str) -> None:
    url = f"{base}/health"
    try:
        r = await client.get(url)
    except httpx.HTTPError as e:
        raise SystemExit(
            f"Нет ответа {url}: {e}\n"
            "Поднимите сервер: docker compose up -d или python server.py"
        ) from e
    if r.status_code != 200:
        raise SystemExit(
            f"{url} вернул HTTP {r.status_code}. "
            "Поднимите сервер: docker compose up -d или python server.py"
        )


def _truncate(text: str) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= _ERROR_BODY_MAX:
        return text
    return text[:_ERROR_BODY_MAX] + "…"


async def _redraw_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    *,
    base: str,
    source: Path,
    dest: Path,
    prompt: str,
    size: str,
    user_id: int,
    timeout_s: float,
    log_lock: asyncio.Lock,
    error_log: Path,
) -> str:
    async with sem:
        try:
            raw = source.read_bytes()
        except OSError as e:
            await _record_error(log_lock, error_log, source.name, "read", str(e))
            print(f"fail {source.name} read: {e}", file=sys.stderr)
            return "fail"
        if not raw:
            await _record_error(log_lock, error_log, source.name, "read", "empty file")
            print(f"fail {source.name} empty", file=sys.stderr)
            return "fail"
        payload = {
            "user_id": user_id,
            "mode": "style_redraw",
            "prompt": prompt,
            "source_image_b64": base64.b64encode(raw).decode("ascii"),
            "size": size,
        }
        try:
            r = await client.post(
                f"{base}/image/generate",
                json=payload,
                timeout=httpx.Timeout(timeout_s),
            )
        except httpx.HTTPError as e:
            await _record_error(log_lock, error_log, source.name, "http", str(e))
            print(f"fail {source.name} http: {e}", file=sys.stderr)
            return "fail"
        if r.status_code != 200:
            body = _truncate(r.text or "")
            await _record_error(
                log_lock, error_log, source.name, str(r.status_code), body
            )
            print(f"fail {source.name} HTTP {r.status_code} {body}", file=sys.stderr)
            return "fail"
        data = r.content
        if not data.startswith(_PNG_MAGIC):
            await _record_error(
                log_lock, error_log, source.name, "200", "response is not PNG"
            )
            print(f"fail {source.name} response is not PNG", file=sys.stderr)
            return "fail"
        tmp = dest.with_suffix(".png.part")
        try:
            tmp.write_bytes(data)
            tmp.replace(dest)
        except OSError as e:
            await _record_error(log_lock, error_log, source.name, "write", str(e))
            print(f"fail {source.name} write: {e}", file=sys.stderr)
            return "fail"
        print(f"ok {source.name} -> {dest.name} ({len(data)} bytes)")
        return "ok"


async def _record_error(
    lock: asyncio.Lock,
    error_log: Path,
    name: str,
    code: str,
    detail: str,
) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp}\t{name}\t{code}\t{_truncate(detail)}\n"
    async with lock:
        with error_log.open("a", encoding="utf-8") as f:
            f.write(line)


async def _run(args: argparse.Namespace) -> int:
    input_dir = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    if not input_dir.is_dir():
        print(f"Нет каталога --input: {input_dir}", file=sys.stderr)
        return 2
    sources = _list_sources(input_dir)
    if not sources:
        print(f"В {input_dir} нет jpg/jpeg/png", file=sys.stderr)
        return 2

    pending: list[Path] = []
    skipped = 0
    for src in sources:
        dest = _output_png(output_dir, src)
        if _already_done(dest):
            skipped += 1
            continue
        pending.append(src)
    if args.limit is not None:
        pending = pending[: max(0, args.limit)]

    print(
        f"sources={len(sources)} skip={skipped} pending={len(pending)} "
        f"jobs={args.jobs} size={args.size}"
    )
    if args.dry_run:
        for src in pending:
            print(f"would {src.name} -> {_output_png(output_dir, src).name}")
        return 0
    if not pending:
        print(f"ok=0 skip={skipped} fail=0")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    base = _server_url(args.server_url)
    timeout_s = image_gen.image_gen_bot_client_timeout_sec(multipart=True)
    error_log = output_dir / "batch_errors.log"
    sem = asyncio.Semaphore(max(1, args.jobs))
    log_lock = asyncio.Lock()

    async with httpx.AsyncClient() as client:
        await _check_health(client, base)
        tasks = [
            _redraw_one(
                client,
                sem,
                base=base,
                source=src,
                dest=_output_png(output_dir, src),
                prompt=args.prompt,
                size=args.size,
                user_id=args.user_id,
                timeout_s=timeout_s,
                log_lock=log_lock,
                error_log=error_log,
            )
            for src in pending
        ]
        results = await asyncio.gather(*tasks)

    ok = sum(1 for r in results if r == "ok")
    fail = sum(1 for r in results if r == "fail")
    print(f"ok={ok} skip={skipped} fail={fail}")
    return 1 if fail else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Пачка POST /image/generate mode=style_redraw (как кнопка в боте).",
    )
    parser.add_argument("--input", required=True, help="Каталог jpg/jpeg/png")
    parser.add_argument("--output", required=True, help="Каталог для PNG")
    parser.add_argument(
        "--server-url",
        default=None,
        help="База tgzh-server (иначе SERVER_URL из .env, иначе http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--size",
        default=image_gen.CHAT_IMAGINE_SIZE_ORIGINAL,
        help="Поле size (дефолт original_max1280)",
    )
    parser.add_argument(
        "--prompt",
        default="",
        help="Уточнение; пусто - как перерисовка без подписи в боте",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Сколько запросов к серверу держать одновременно",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Обработать не больше N еще не готовых файлов",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только список, без HTTP",
    )
    parser.add_argument("--user-id", type=int, default=0)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
