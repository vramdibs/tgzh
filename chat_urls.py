"""Загрузка и разбор http(s) ссылок для `/chat`."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

import chat_documents

log = logging.getLogger("tgzh.chat_urls")

_URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChatUrlExtract:
    url: str
    text: str
    truncated: bool = False
    error: str | None = None


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, val))


def chat_url_max_count() -> int:
    return _int_env("CHAT_URL_MAX_COUNT", 5, 1, 10)


def chat_url_fetch_timeout_sec() -> float:
    return float(_int_env("CHAT_URL_FETCH_TIMEOUT_SEC", 25, 5, 120))


def chat_url_max_bytes() -> int:
    return _int_env("CHAT_URL_MAX_BYTES", 2_000_000, 10_000, 20_000_000)


def chat_url_extract_max_chars_per_url() -> int:
    raw = (os.getenv("CHAT_URL_EXTRACT_MAX_CHARS_PER_URL") or "").strip()
    if raw:
        return _int_env("CHAT_URL_EXTRACT_MAX_CHARS_PER_URL", 12_000, 500, 100_000)
    return chat_documents.chat_file_extract_max_chars_per_file()


def chat_url_buffer_ttl_sec() -> int:
    return _int_env(
        "CHAT_URL_BUFFER_TTL_SEC",
        chat_documents.chat_file_buffer_ttl_sec(),
        60,
        3600,
    )


def extract_http_urls(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,);]")
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= chat_url_max_count():
            break
    return out


def strip_urls_from_text(text: str) -> str:
    cleaned = _URL_RE.sub(" ", text or "")
    return " ".join(cleaned.split())


def _hostname_blocked_ips(hostname: str) -> bool:
    if not hostname:
        return True
    host = hostname.strip().lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except OSError:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if not ip.is_global:
            return True
    return False


def is_safe_fetch_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if p.username or p.password:
        return False
    host = (p.hostname or "").strip()
    if not host:
        return False
    return not _hostname_blocked_ips(host)


def html_to_text(html: bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _response_to_text(data: bytes, content_type: str | None, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/pdf" or url.lower().rstrip("/").endswith(".pdf"):
        return chat_documents.extract_chat_document_text(
            data,
            content_type,
            "page.pdf",
        )
    if ct in ("text/html", "application/xhtml+xml") or b"<html" in data[:500].lower():
        return html_to_text(data)
    if ct.startswith("text/") or ct == "application/json":
        return decode_response_bytes(data)
    return decode_response_bytes(data)


def decode_response_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


async def fetch_url_text(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ChatUrlExtract:
    if not is_safe_fetch_url(url):
        return ChatUrlExtract(
            url=url,
            text="",
            error="Ссылка недоступна для загрузки (внутренний или небезопасный адрес).",
        )
    timeout = httpx.Timeout(chat_url_fetch_timeout_sec())
    max_b = chat_url_max_bytes()
    own_client = client is None
    hc = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        resp = await hc.get(url)
        log.info(
            "chat url fetch host=%s status=%s bytes=%s",
            urlparse(url).hostname,
            resp.status_code,
            len(resp.content or b""),
        )
        if resp.status_code >= 400:
            return ChatUrlExtract(
                url=url,
                text="",
                error=f"HTTP {resp.status_code} при загрузке страницы.",
            )
        final_url = str(resp.url)
        if not is_safe_fetch_url(final_url):
            return ChatUrlExtract(
                url=url,
                text="",
                error="Редирект вел на недопустимый адрес.",
            )
        data = resp.content or b""
        if len(data) > max_b:
            data = data[:max_b]
        ct_hdr = resp.headers.get("content-type")
        ct = (ct_hdr or "").split(";")[0].strip().lower()
        is_pdf = ct == "application/pdf" or final_url.lower().rstrip("/").endswith(
            ".pdf",
        )
        if is_pdf:
            text = await chat_documents.extract_pdf_text_for_chat(data)
        else:
            text = await asyncio.to_thread(
                _response_to_text,
                data,
                ct_hdr,
                final_url,
            )
        cap = chat_url_extract_max_chars_per_url()
        truncated = len(text) > cap
        body = text[: cap - 1] + "…" if truncated else text
        return ChatUrlExtract(url=url, text=body, truncated=truncated)
    except httpx.RequestError as e:
        return ChatUrlExtract(url=url, text="", error=f"Не удалось загрузить: {e}")
    finally:
        if own_client:
            await hc.aclose()


def compose_chat_urls_and_files_message(
    instruction: str,
    files: list[chat_documents.ChatFileExtract],
    urls: list[ChatUrlExtract],
) -> str:
    items: list[chat_documents.ChatSourceBlock] = []
    for f in files:
        items.append(
            chat_documents.ChatSourceBlock(
                kind_label="Файл",
                name=f.filename,
                text=f.text,
                truncated=f.truncated,
                empty_hint="[не удалось извлечь текст; возможно, скан без текстового слоя]",
            ),
        )
    for u in urls:
        hint = u.error or "[мало текста на странице; возможно, нужен вход или JavaScript]"
        items.append(
            chat_documents.ChatSourceBlock(
                kind_label="URL",
                name=u.url,
                text=u.text,
                truncated=u.truncated,
                empty_hint=hint,
            ),
        )
    per_file = chat_documents.chat_file_extract_max_chars_per_file()
    per_url = chat_url_extract_max_chars_per_url()
    per_item = max(per_file, per_url)
    return chat_documents.compose_chat_sources_user_message(
        instruction,
        items,
        per_item_cap=per_item,
        intro="Пользователь прислал файлы и/или ссылки для анализа в чате.",
    )


def history_placeholder_for_sources(
    filenames: list[str],
    urls: list[str],
    instruction: str,
) -> str:
    parts: list[str] = []
    if filenames:
        names = ", ".join(filenames)
        parts.append(f"файлы: {names}")
    if urls:
        short = []
        for u in urls:
            s = u.strip()
            if len(s) > 60:
                s = s[:57] + "…"
            short.append(s)
        parts.append(f"ссылки: {', '.join(short)}")
    instr = (instruction or "").strip()
    if len(instr) > 120:
        instr = instr[:117] + "…"
    body = " | ".join(parts) if parts else "материалы"
    if instr:
        return f"[{body} | {instr}]"
    return f"[{body}]"
