"""
Telegram-бот для проверки домашних заданий по математике.
Учебник (GDZ) → параграф (§1…§N или текст) → шаг 2: упражнения/проверочные из оглавления (кнопки и умная клавиатура) → фото → проверка.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import io
import json
import os
import random
import re
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    MenuButtonCommands,
    MenuButtonDefault,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

load_dotenv()

from logging_config import setup_logging
from tgzh_httpx import async_http_transport_ipv4_lookup

logger = setup_logging("tgzh.bot")


def _h(s: str) -> str:
    return html.escape(s or "", quote=False)


def _schedule_feedback_tei_analysis(ticket_id: int, text: str) -> None:
    if not feedback_tei.tei_feedback_analysis_enabled():
        return

    async def _run() -> None:
        try:
            snap = await feedback_tei.analyze_feedback_text(text)
            if not snap:
                return
            await asyncio.to_thread(
                user_storage.upsert_feedback_ticket_nlp,
                USER_DB_PATH,
                ticket_id,
                sentiment_label=str(snap.get("sentiment_label") or ""),
                sentiment_score=float(snap.get("sentiment_score") or 0.0),
                emotions_json=str(snap.get("emotions_json") or "[]"),
                badges=str(snap.get("badges") or ""),
                error=snap.get("error"),
            )
        except Exception:
            logger.exception("feedback_tei failed ticket_id=%s", ticket_id)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.warning("feedback_tei no running loop ticket_id=%s", ticket_id)


def _format_feedback_nlp_detail_html(nlp: user_storage.FeedbackNlpRow) -> str:
    lines: list[str] = [f"<b>Тональность и эмоции</b> {_h(nlp.badges)}"]
    if nlp.sentiment_label:
        sc = float(nlp.sentiment_score)
        pct = int(round(sc * 100)) if sc <= 1.0 else int(round(sc))
        lines.append(f"Sentiment: {_h(nlp.sentiment_label)} ({pct}%)")
    try:
        emo = json.loads(nlp.emotions_json or "[]")
        if isinstance(emo, list) and emo:
            bits: list[str] = []
            for x in emo[:8]:
                if isinstance(x, dict):
                    lab = str(x.get("label") or "")
                    sv = float(x.get("score") or 0.0)
                    bits.append(f"{lab} {int(round(sv * 100))}%")
            if bits:
                lines.append("Эмоции: " + _h(", ".join(bits)))
    except json.JSONDecodeError:
        pass
    if nlp.error:
        lines.append(f"<i>TEI: {_h(nlp.error)}</i>")
    return "\n".join(lines)


def _infer_textbook_edition_year(slug: str, label: str) -> str | None:
    """Год из slug (суффикс -2013) или первое число 19xx/20xx в подписи."""
    s = (slug or "").rstrip("/")
    if s:
        m = re.search(r"-((?:19|20)\d{2})(?:/|$)", s + "/")
        if m:
            return m.group(1)
    for chunk in (label, slug):
        if not chunk:
            continue
        m2 = re.search(r"\b((?:19|20)\d{2})\b", chunk)
        if m2:
            y = int(m2.group(1))
            if 1960 <= y <= 2035:
                return str(y)
    return None


from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import bot_stats
import cursor_cli_client
import feedback_tei
import gdz_solution
import homework_check_status
import photo_prepare
import telegram_format
import tgzh_metrics
import user_storage


def _textbook_label_html(profile: user_storage.UserProfile) -> str:
    slug = profile.textbook_slug or ""
    label = profile.textbook_label or ""
    y = _infer_textbook_edition_year(slug, label)
    base = _h(label)
    if y:
        return f"{base}, {y} г."
    return base


BOT_TOKEN = os.getenv("BOT_TOKEN")


def _server_url_from_env() -> str:
    """
    Базовый URL API проверки. Пустая строка в .env (SERVER_URL=) не подставляет дефолт через getenv(..., default).
    """
    raw = ((os.getenv("SERVER_URL") or "").strip().lstrip("\ufeff")).strip()
    if not raw or not raw.startswith(("http://", "https://")):
        return "http://localhost:8000"
    return raw.rstrip("/")


SERVER_URL = _server_url_from_env()
if "SERVER_URL" in os.environ:
    _su_raw = ((os.environ.get("SERVER_URL") or "").strip().lstrip("\ufeff")).strip()
    if not _su_raw or not _su_raw.startswith(("http://", "https://")):
        logger.warning(
            "SERVER_URL в окружении пуст или без http/https; для API проверки используется %s",
            SERVER_URL,
        )
USER_DB_PATH = os.getenv("USER_DB_PATH", "data/users.sqlite")
GDZ_CATALOG_PATH = os.getenv("GDZ_CATALOG_PATH", "data/gdz_matematika_textbooks.json")


def _cursor_ask_allowlist_ids() -> set[int]:
    raw = (os.getenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p, 10))
        except ValueError:
            continue
    return out


def _cursor_ask_feature_enabled() -> bool:
    return bool(_cursor_ask_allowlist_ids() and cursor_cli_client.cli_configured())

_FEEDBACK_WAITING = "feedback_waiting"
_FEEDBACK_STAFF_WAIT = "feedback_staff_wait"
_POLL_AWAIT_CAREER = "poll_await_career"
_POLL_LIKES_MATH = "poll_likes_math"
_BEGEMOT_PW_WAIT = "begemot_pw_wait"
_BEGEMOT_OK = "begemot_ok"
_ADMIN_BAN_WAIT = "admin_ban_wait"
_CHECK_DISLIKE_FEEDBACK_WAIT = "check_dislike_feedback_wait"
_FEEDBACK_PAGE = 3


def _admin_password_expected() -> str:
    return (os.getenv("ADMIN_PASSWORD") or "").strip()


def _admin_password_matches(got: str, expected: str) -> bool:
    if not expected:
        return False
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


def _clear_feedback_and_begemot_wait(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_FEEDBACK_WAITING, None)
    context.user_data.pop(_BEGEMOT_PW_WAIT, None)
    context.user_data.pop(_FEEDBACK_STAFF_WAIT, None)
    context.user_data.pop(_ADMIN_BAN_WAIT, None)
    context.user_data.pop(_CHECK_DISLIKE_FEEDBACK_WAIT, None)


def _clear_begemot_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_BEGEMOT_OK, None)
    context.user_data.pop(_FEEDBACK_STAFF_WAIT, None)
    context.user_data.pop(_ADMIN_BAN_WAIT, None)


_DISCLAIMER_WAIT_ACCEPT = "disclaimer_wait_accept"
_TELEGRAM_INLINE_LABEL_MAX = 64


@dataclass(frozen=True)
class DisclaimerConfig:
    version: int
    text_html: str
    question: str
    correct_index: int
    options: tuple[tuple[int, str], ...]
    accept_button: str


def _disclaimer_skip_env() -> bool:
    return os.getenv("DISCLAIMER_SKIP", "").strip().lower() in ("1", "true", "yes")


def load_disclaimer_config() -> DisclaimerConfig | None:
    if _disclaimer_skip_env():
        return None
    raw_v = (os.getenv("DISCLAIMER_VERSION") or "").strip()
    if not raw_v:
        return None
    try:
        version = int(raw_v)
    except ValueError:
        logger.warning("DISCLAIMER_VERSION is not an integer, disclaimer gate disabled")
        return None
    text = (os.getenv("DISCLAIMER_TEXT") or "").strip()
    question = (os.getenv("DISCLAIMER_QUIZ_QUESTION") or "").strip()
    raw_c = (os.getenv("DISCLAIMER_QUIZ_CORRECT") or "").strip()
    if not text or not question or raw_c == "":
        logger.warning("DISCLAIMER_TEXT, DISCLAIMER_QUIZ_QUESTION or DISCLAIMER_QUIZ_CORRECT empty, gate disabled")
        return None
    try:
        correct_index = int(raw_c)
    except ValueError:
        logger.warning("DISCLAIMER_QUIZ_CORRECT is not an integer, disclaimer gate disabled")
        return None
    opts: list[tuple[int, str]] = []
    for i in range(4):
        lab = (os.getenv(f"DISCLAIMER_QUIZ_OPT_{i}") or "").strip()
        if lab:
            opts.append((i, lab[:_TELEGRAM_INLINE_LABEL_MAX]))
    if len(opts) < 2:
        logger.warning("need at least 2 non-empty DISCLAIMER_QUIZ_OPT_N, disclaimer gate disabled")
        return None
    valid_indices = {i for i, _ in opts}
    if correct_index not in valid_indices:
        logger.warning("DISCLAIMER_QUIZ_CORRECT not among non-empty options, disclaimer gate disabled")
        return None
    accept_btn = (os.getenv("DISCLAIMER_ACCEPT_BUTTON") or "").strip() or "Принимаю условия"
    accept_btn = accept_btn[:_TELEGRAM_INLINE_LABEL_MAX]
    return DisclaimerConfig(
        version=version,
        text_html=text,
        question=question,
        correct_index=correct_index,
        options=tuple(opts),
        accept_button=accept_btn,
    )


def _disclaimer_quiz_markup(cfg: DisclaimerConfig) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"dc:quiz:{cfg.version}:{idx}")]
        for idx, label in cfg.options
    ]
    return InlineKeyboardMarkup(rows)


def _disclaimer_accept_markup(cfg: DisclaimerConfig) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(cfg.accept_button, callback_data="dc:accept")]],
    )


async def _send_disclaimer_quiz_screen(
    bot,
    chat_id: int,
    cfg: DisclaimerConfig,
) -> None:
    body = f"{cfg.text_html}\n\n<b>{_h(cfg.question)}</b>"
    await bot.send_message(
        chat_id,
        body,
        reply_markup=_disclaimer_quiz_markup(cfg),
        parse_mode=ParseMode.HTML,
    )


async def _send_disclaimer_accept_screen(
    bot,
    chat_id: int,
    cfg: DisclaimerConfig,
) -> None:
    await bot.send_message(
        chat_id,
        "Ответ верный. Подтверди согласие с условиями кнопкой ниже.",
        reply_markup=_disclaimer_accept_markup(cfg),
        parse_mode=ParseMode.HTML,
    )


async def _continue_start_after_consent(
    bot,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    if profile is None:
        await flow_remove_reply_keyboard(bot, chat_id)
        await bot.send_message(
            chat_id,
            "<b>Привет! 👋</b> Я проверяю домашние задания по математике.\n\n"
            "Сначала выбери класс, затем учебник из списка.",
            reply_markup=grade_keyboard(back_to_main=False),
            parse_mode=ParseMode.HTML,
        )
        return
    context.user_data["hw_entry"] = "main"
    await flow_remove_reply_keyboard(bot, chat_id)
    try:
        await asyncio.wait_for(
            run_with_typing(
                bot,
                chat_id,
                _send_paragraph_prompt(bot, chat_id, context, user_id),
            ),
            timeout=40.0,
        )
    except TimeoutError:
        logger.error("paragraph prompt timed out user_id=%s", user_id)
        await bot.send_message(
            chat_id,
            "Не удалось вовремя загрузить оглавление параграфов (таймаут). Попробуй еще раз: /start",
        )
        return
    except Exception:
        logger.exception("paragraph prompt failed user_id=%s", user_id)
        await bot.send_message(
            chat_id,
            "Не удалось показать выбор параграфа. Попробуй /start еще раз или смени учебник: /textbook",
        )
        return


async def _disclaimer_reply_need_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message:
        await update.message.reply_text(
            "Сначала открой /start и пройди короткий блок с условиями и вопросом.",
        )


async def _disclaimer_consent_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    dcfg = load_disclaimer_config()
    if dcfg is None or not update.effective_user:
        return True
    uid = update.effective_user.id
    row = await asyncio.to_thread(user_storage.get_user_consent, USER_DB_PATH, uid)
    if user_storage.consent_fully_accepted(row, dcfg.version):
        return True
    await _disclaimer_reply_need_start(update, context)
    return False


CATALOG: dict[str, list[dict]] = {}

PAGE_SIZE = 6

# Стикеры после проверки (.env):
#   STICKER_SET_POOL — общий список наборов (через запятую), если не заданы отдельно TADA / MOTIVATION
#   TADA_STICKER_SET_NAMES — только награда 50%+; MOTIVATION_STICKER_SET_NAMES — только 0% / смешанные
# Имя набора — хвост URL t.me/addstickers/ИМЯ. Перед отправкой наборы перемешиваются (random.shuffle в _send_sticker_from_pack)
_BUILTIN_STICKER_SET_POOL: tuple[str, ...] = (
    "Komnata_lomki",
    "NBstickeriaBrat",
    "VikostVSpack",
    "ShadowKitty",
    "taxiderm",
    "DonutTheDog",
    "HotCherry",
    "UtyaDuck",
)
_TADA_BUILTIN_STICKER_SET_NAMES = _BUILTIN_STICKER_SET_POOL
_MOTIVATION_BUILTIN_STICKER_SET_NAMES = _BUILTIN_STICKER_SET_POOL
_TADA_EMOJI_PREFER = "\U0001f389"  # 🎉

user_photos: dict[int, list[str]] = {}

_PHOTO_BATCH_GID = "photo_batch_gid"
_PHOTO_BATCH_ENTRIES = "photo_batch_entries"
_PHOTO_BATCH_TASK = "photo_batch_task"
_PHOTO_DEBOUNCE_ALBUM_SEC = 0.5


def _user_photo_file_ids(user_id: int) -> list[str]:
    v = user_photos.get(user_id)
    if not v:
        return []
    return list(v)


def _user_has_uploaded_photo(user_id: int) -> bool:
    return bool(_user_photo_file_ids(user_id))


def _store_photo_batch(user_id: int, ids: list[str]) -> None:
    user_photos[user_id] = list(ids)


def _clear_user_photos(user_id: int) -> None:
    user_photos.pop(user_id, None)


def _cancel_photo_batch_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    t = context.user_data.pop(_PHOTO_BATCH_TASK, None)
    if t is not None and not t.done():
        t.cancel()


def _photo_batch_clear_all(context: ContextTypes.DEFAULT_TYPE) -> None:
    _cancel_photo_batch_task(context)
    context.user_data.pop(_PHOTO_BATCH_ENTRIES, None)
    context.user_data.pop(_PHOTO_BATCH_GID, None)


async def _photo_batch_flush_delayed(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    delay: float,
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    context.user_data.pop(_PHOTO_BATCH_TASK, None)
    entries = context.user_data.pop(_PHOTO_BATCH_ENTRIES, None)
    context.user_data.pop(_PHOTO_BATCH_GID, None)
    if not entries:
        return
    sorted_ent = sorted(entries, key=lambda x: x[0])
    ids = [fid for _, fid in sorted_ent]
    last_mid = sorted_ent[-1][0]
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    if profile is None or not user_storage.homework_complete(profile):
        return
    _store_photo_batch(user_id, ids)
    for _ in ids:
        await asyncio.to_thread(bot_stats.record_photo_uploaded, USER_DB_PATH)
    n = len(ids)
    msg = await context.bot.send_message(
        chat_id,
        f"Получено фото: <b>{n}</b>.\n\n<b>Нажми «Проверить»</b>, чтобы отправить на проверку.",
        reply_markup=get_main_keyboard(uploaded=True, profile=profile, user_id=user_id),
        reply_to_message_id=last_mid,
        parse_mode=ParseMode.HTML,
    )
    flow_note(context, msg)


_STATS_REPLY_BUTTON_TEXT = "📊 Статистика"

_SAVED_HW_FOLLOWUP_HTML = (
    "Теперь <b>загрузи фото тетради</b>, нажми <b>«Ответить текстом»</b> "
    "или посмотри решение из ГДЗ"
)

_HW_STEP = "hw_step"
_HW_PARAGRAPH_PICK = "paragraph_pick"
_HW_PARAGRAPH_MANUAL = "paragraph_manual"
_HW_PAGE_KEYPAD = "page_keypad"
_HW_PAGE_BUF = "hw_page_digit_buf"
_HW_PAR_BTN_MAX = "hw_paragraph_btn_max"
_HW_EX_KEYPAD = "exercise_keypad"
_HW_EX_BUF = "hw_ex_digit_buf"
_HW_EX_VALID = "hw_ex_valid_set"
_HW_VERIF_PAGES = "hw_verif_pages"
_HW_VERIF_TRUNC = "hw_verif_pages_truncated"
_HW_VERIF_SUBSCREEN = "hw_verif_subscreen"
_HW_PAGE_FROM_VERIF = "hw_page_keypad_from_verif"
_FLOW_MSG_IDS = "flow_bot_msg_ids"
# Ожидаем текстовый ответ ученика для проверки (кнопка «Ответить текстом»)
_AWAIT_TEXT_ANSWER = "await_text_answer"

_PARAGRAPH_ROW = 6
_PARAGRAPH_COUNT_CACHE: dict[str, tuple[float, int]] = {}


def _paragraph_buttons_cap() -> int:
    raw = os.getenv("GDZ_PARAGRAPH_BUTTONS_MAX", "48").strip()
    try:
        return max(6, min(int(raw), 100))
    except ValueError:
        return 48


def _paragraph_index_cache_ttl() -> float:
    raw = os.getenv("GDZ_PARAGRAPH_INDEX_CACHE_SEC", "3600").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 3600.0


def _exercise_keypad_fallback_max() -> int:
    raw = os.getenv("GDZ_EXERCISE_KEYPAD_FALLBACK_MAX", "999").strip()
    try:
        return max(9, min(int(raw), 9999))
    except ValueError:
        return 999


def _verif_buttons_cap() -> int:
    """0 — без обрезки (осторожно: лимит Telegram ~100 кнопок на клавиатуру). Иначе максимум кнопок с номером страницы."""
    raw = os.getenv("GDZ_VERIF_BUTTONS_MAX", "96").strip()
    try:
        n = int(raw)
    except ValueError:
        return 96
    if n <= 0:
        return 0
    return min(n, 96)


def _pop_step2_gdz_meta(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_HW_EX_VALID, None)
    context.user_data.pop(_HW_VERIF_PAGES, None)
    context.user_data.pop(_HW_VERIF_TRUNC, None)
    context.user_data.pop(_HW_EX_BUF, None)
    context.user_data.pop(_HW_VERIF_SUBSCREEN, None)
    context.user_data.pop(_HW_PAGE_FROM_VERIF, None)


def _exercise_valid_from_context(context: ContextTypes.DEFAULT_TYPE) -> frozenset[int]:
    v = context.user_data.get(_HW_EX_VALID)
    if isinstance(v, frozenset) and len(v) > 0:
        return v
    return frozenset(range(1, _exercise_keypad_fallback_max() + 1))


def _prefix_can_complete(buf: str, valid: frozenset[int]) -> bool:
    if not buf:
        return len(valid) > 0
    return any(str(n).startswith(buf) for n in valid)


def _allowed_exercise_next_digits(buf: str, valid: frozenset[int]) -> set[str]:
    return {d for d in "0123456789" if _prefix_can_complete(buf + d, valid)}


def _max_exercise_input_len(valid: frozenset[int]) -> int:
    return max((len(str(n)) for n in valid), default=3)


def _can_confirm_exercise(buf: str, valid: frozenset[int]) -> bool:
    """True, если введенное число есть в оглавлении. «Готово» не требует довода до однозначного префикса."""
    if not buf or not buf.isdigit():
        return False
    return int(buf) in valid


def exercise_keypad_caption(buf: str, valid: frozenset[int]) -> str:
    display = buf if buf else "—"
    mn, mx = min(valid), max(valid)
    return (
        f'Нажми цифры, затем "Готово". В параграфе упражнения {mn}...{mx}. '
        f"Страницу проверочной из оглавления выбери на шаге 2 кнопкой <b>Выбрать проверочную</b>.\n"
        f"Выбрано: <b>{_h(display)}</b>"
    )


def exercise_keypad_keyboard(buf: str, valid: frozenset[int]) -> InlineKeyboardMarkup:
    allowed = _allowed_exercise_next_digits(buf, valid)
    ordered = [d for d in "1234567890" if d in allowed]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(ordered), 6):
        chunk = ordered[i : i + 6]
        rows.append([InlineKeyboardButton(d, callback_data=f"exd:{d}") for d in chunk])
    rows.append(
        [
            InlineKeyboardButton("Удал.", callback_data="exk:bs"),
            InlineKeyboardButton("Готово", callback_data="exk:ok"),
        ],
    )
    rows.append([InlineKeyboardButton("Назад", callback_data="back_hw:xk")])
    return InlineKeyboardMarkup(rows)


def _format_gdz_verif_works_for_check(meta: gdz_solution.ParagraphTaskMeta) -> str:
    """Многострочное описание проверочных для поля gdz_verif_works (промпт LLM)."""
    if not meta.verification_works:
        return ""
    lines: list[str] = []
    for w in meta.verification_works:
        parts = [w.label]
        low = w.label.lower()
        if w.page is not None and "стр." not in low and "стр " not in low:
            parts.append(f"стр. {w.page}")
        parts.append(f"item {w.item_index}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _apply_paragraph_task_meta(context: ContextTypes.DEFAULT_TYPE, meta: gdz_solution.ParagraphTaskMeta) -> None:
    if meta.exercise_items:
        context.user_data[_HW_EX_VALID] = meta.exercise_items
    else:
        context.user_data[_HW_EX_VALID] = frozenset(
            range(1, _exercise_keypad_fallback_max() + 1),
        )
    pages = list(meta.verification_pages)
    cap = _verif_buttons_cap()
    if cap > 0 and len(pages) > cap:
        context.user_data[_HW_VERIF_PAGES] = pages[:cap]
        context.user_data[_HW_VERIF_TRUNC] = True
    else:
        context.user_data[_HW_VERIF_PAGES] = pages
        context.user_data.pop(_HW_VERIF_TRUNC, None)


async def _prepare_step2_after_paragraph(
    context: ContextTypes.DEFAULT_TYPE,
    textbook_url: str,
    paragraph: str,
) -> None:
    meta = await asyncio.to_thread(
        gdz_solution.fetch_paragraph_task_meta,
        textbook_url,
        paragraph,
    )
    _apply_paragraph_task_meta(context, meta)
    context.user_data.pop(_HW_VERIF_SUBSCREEN, None)
    context.user_data.pop(_HW_PAGE_FROM_VERIF, None)
    logger.info(
        "gdz paragraph meta para=%r exercises=%s verif_pages=%s",
        paragraph,
        len(context.user_data.get(_HW_EX_VALID) or ()),
        len(context.user_data.get(_HW_VERIF_PAGES) or []),
    )


def _step2_hw_caption(context: ContextTypes.DEFAULT_TYPE) -> str:
    valid = _exercise_valid_from_context(context)
    mn, mx = min(valid), max(valid)
    pages = context.user_data.get(_HW_VERIF_PAGES) or []
    if not isinstance(pages, list):
        pages = []
    has_verif = len(pages) > 0
    sub = bool(context.user_data.get(_HW_VERIF_SUBSCREEN))
    lines = [
        "<b>Шаг 2 из 2.</b> Выбери упражнение или страницу проверочной.",
        "",
        f"• <b>Упражнения</b> в этом параграфе на gdz.ru: номера <b>{mn}…{mx}</b>.",
    ]
    if sub:
        lines.append("• Страницы <b>проверочных</b> из оглавления - кнопки с номером страницы ниже.")
        if context.user_data.get(_HW_VERIF_TRUNC):
            cap = _verif_buttons_cap()
            lines.append(
                f"(Показаны первые {cap} страниц проверочных из оглавления; остальные — текстом или «или Ввести номер».)",
            )
    elif has_verif:
        lines.append(
            "• Страницы <b>проверочных</b> из оглавления - кнопка <b>Выбрать проверочную</b> ниже.",
        )
    return "\n".join(lines)


def exercise_step_keyboard_from_context(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    pages = context.user_data.get(_HW_VERIF_PAGES) or []
    if not isinstance(pages, list):
        pages = []
    sub = bool(context.user_data.get(_HW_VERIF_SUBSCREEN))
    row_w = 6
    if sub:
        rows: list[list[InlineKeyboardButton]] = []
        for i in range(0, len(pages), row_w):
            chunk = pages[i : i + row_w]
            rows.append(
                [InlineKeyboardButton(str(p), callback_data=f"hw_pf:{p}") for p in chunk],
            )
        rows.append(
            [InlineKeyboardButton("или Ввести номер", callback_data="hw_page_keypad")],
        )
        rows.append([InlineKeyboardButton("Назад", callback_data="back_hw:vf")])
        return InlineKeyboardMarkup(rows)

    rows = [
        [InlineKeyboardButton("Выбрать упражнение", callback_data="hw_ex_keypad")],
    ]
    if pages:
        rows.append([InlineKeyboardButton("Выбрать проверочную", callback_data="hw_verif_open")])
    rows.append(
        [InlineKeyboardButton("или Ввести номер", callback_data="hw_page_keypad")],
    )
    rows.append([InlineKeyboardButton("Назад", callback_data="back_hw:e")])
    return InlineKeyboardMarkup(rows)


def flow_note(context: ContextTypes.DEFAULT_TYPE, message: object | None) -> None:
    """Запоминает id сообщения бота, чтобы потом убрать его при смене учебника / старте."""
    if message is None:
        return
    mid = getattr(message, "message_id", None)
    if mid is None:
        return
    context.user_data.setdefault(_FLOW_MSG_IDS, []).append(mid)


def _bot_commands_list() -> list[BotCommand]:
    cmds = [
        BotCommand("start", "Новое упражнение"),
        BotCommand("support", "Оставить отзыв"),
        BotCommand("my_support", "Активные обращения"),
        BotCommand("polling", "Пройти опрос"),
        BotCommand("stats", "Статистика проверок"),
        BotCommand("textbook", "Сменить класс или учебник"),
    ]
    if _cursor_ask_feature_enabled():
        cmds.append(
            BotCommand(
                "cursorask",
                "Вопрос через Cursor CLI (agent)",
            ),
        )
    return cmds


async def flow_ensure_chat_commands_menu(bot, chat_id: int) -> None:
    """
    Восстанавливает меню команд. Для чата сначала MenuButtonDefault (у бота это список команд) -
    так Telegram Desktop чаще показывает кнопку; затем обновляем глобальную кнопку MenuButtonCommands.
    """
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonDefault())
    except Exception as e:
        logger.debug("set_chat_menu_button MenuButtonDefault chat_id=%s: %s", chat_id, e)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.debug("set_chat_menu_button MenuButtonCommands global: %s", e)


async def flow_purge_except(
    bot,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    keep_message_id: int | None = None,
) -> None:
    """Удаляет вспомогательные сообщения (промпты, решение GDZ); снимает reply-клавиатуру."""
    _clear_feedback_and_begemot_wait(context)
    context.user_data.pop(_DISCLAIMER_WAIT_ACCEPT, None)
    context.user_data.pop(_POLL_AWAIT_CAREER, None)
    context.user_data.pop(_POLL_LIKES_MATH, None)
    context.user_data.pop(_FEEDBACK_STAFF_WAIT, None)
    _photo_batch_clear_all(context)
    ids = context.user_data.pop(_FLOW_MSG_IDS, None)
    if ids:
        for mid in ids:
            if keep_message_id is not None and mid == keep_message_id:
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception as e:
                logger.debug("flow_purge delete %s: %s", mid, e)
    await flow_remove_reply_keyboard(bot, chat_id)


async def flow_remove_reply_keyboard(bot, chat_id: int) -> None:
    """Убирает reply-клавиатуру под полем ввода; восстанавливает меню команд у чата."""
    try:
        rm = await bot.send_message(chat_id, ".", reply_markup=ReplyKeyboardRemove())
        await bot.delete_message(chat_id, rm.message_id)
    except Exception as e:
        logger.debug("reply keyboard remove: %s", e)
    await flow_ensure_chat_commands_menu(bot, chat_id)


async def post_init_commands(application: Application) -> None:
    bot = application.bot
    cmds = _bot_commands_list()
    await bot.set_my_commands(cmds)
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def _send_stats_message(
    bot,
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    text = await asyncio.to_thread(bot_stats.format_all_stats_html, USER_DB_PATH)
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    kb = grade_keyboard(back_to_main=False) if profile is None else None
    msg = await bot.send_message(
        chat_id,
        text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )
    flow_note(context, msg)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if await _reply_if_blocked_cmd(update, context):
        return
    logger.info("cmd /stats user_id=%s", update.effective_user.id)
    await _send_stats_message(
        context.bot,
        update.effective_chat.id,
        update.effective_user.id,
        context,
    )


def _admin_feedback_nav_keyboard(offset: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_o = max(0, offset - _FEEDBACK_PAGE)
        nav.append(InlineKeyboardButton("Назад", callback_data=f"fb:o:{prev_o}"))
    if offset + _FEEDBACK_PAGE < total:
        nav.append(InlineKeyboardButton("Далее", callback_data=f"fb:o:{offset + _FEEDBACK_PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Обновить список", callback_data=f"fb:o:{offset}")])
    return InlineKeyboardMarkup(rows)


def _format_feedback_list_html(rows: list[user_storage.UserFeedbackRow]) -> str:
    if not rows:
        return "Пока нет отзывов."
    chunks: list[str] = []
    for r in rows:
        un = f"@{_h(r.username)}" if r.username else "без username"
        entries = user_storage.parse_feedback_entries(r.body)
        n = len(entries)
        head = (
            f"<b>user</b> <code>{r.user_id}</code> | {un}\n"
            f"Сообщений: <b>{n}</b>, с {_h(r.created_at[:19])} по {_h(r.updated_at[:19])}"
        )
        lines = [head, ""]
        show = entries[-5:] if len(entries) > 5 else entries
        if len(entries) > 5:
            lines.append(f"<i>(показаны последние 5 из {n})</i>")
        for e in show:
            dt = _h(e.at[:19]) if e.at else "?"
            prev = e.text if len(e.text) <= 220 else e.text[:220] + "…"
            lines.append(f"• <i>{dt}</i> {_h(prev)}")
        chunks.append("\n".join(lines))
    return "\n\n---\n\n".join(chunks)


def _admin_feedback_user_actions_keyboard(target_uid: int, admin_user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if target_uid != admin_user_id:
        rows.append(
            [
                InlineKeyboardButton("⏲️ Блок 1 ч", callback_data=f"fb:bt:{target_uid}"),
                InlineKeyboardButton("💀 Бан", callback_data=f"fb:bp:{target_uid}"),
            ],
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 Разбан", callback_data=f"fb:bu:{target_uid}"),
            InlineKeyboardButton("Спасибо за отзыв", callback_data=f"fb:th:{target_uid}"),
        ],
    )
    return InlineKeyboardMarkup(rows)


def _format_temp_block_notice_for_user(reason: str, until_iso: str) -> str:
    r = _h((reason or "").strip() or "не указана")
    u = (until_iso or "").strip()
    tail = (_h(u[:19]) + " UTC") if u else "?"
    return (
        "<b>Участие в боте приостановлено на 1 час.</b>\n"
        f"Причина: {r}\n"
        f"До (UTC): {tail}\n\n"
        "Кнопка ниже снимает ограничение досрочно."
    )


def _blocked_user_message_html(st: dict) -> str:
    if st.get("kind") == user_storage.USER_BLOCK_PERMANENT:
        return "<b>Доступ к боту ограничен без срока.</b>"
    reason = st.get("reason") or "не указана"
    until_s = (st.get("until_utc") or "")[:19]
    return (
        "<b>Участие в боте приостановлено (временный блок).</b>\n"
        f"Причина: {_h(str(reason))}\n"
        f"До (UTC): {_h(until_s)}"
    )


def _blocked_user_reply_markup(st: dict) -> InlineKeyboardMarkup | None:
    if st.get("kind") == user_storage.USER_BLOCK_TEMP:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Снять ограничение", callback_data="ban:lift")]],
        )
    return None


_BLOCKED_STATE_DB_ERROR_HTML = (
    "Временная техническая ошибка. Попробуй ещё раз через минуту."
)


async def _safe_blocked_state(uid: int) -> tuple[dict | None, bool]:
    """
    Возвращает (state, ok). ok=False — БД недоступна/ошибка; политика fail-closed:
    вызывающий должен прервать обработку и показать пользователю сообщение об ошибке.
    """
    try:
        st = await asyncio.to_thread(user_storage.blocked_state, USER_DB_PATH, uid)
        return st, True
    except Exception:
        logger.exception("blocked_state failed user_id=%s", uid)
        return None, False


async def _reply_if_blocked_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_user:
        return False
    if context.user_data.get(_BEGEMOT_OK) or context.user_data.get(_BEGEMOT_PW_WAIT):
        return False
    uid = update.effective_user.id
    st, ok = await _safe_blocked_state(uid)
    if not ok:
        with suppress(Exception):
            await update.message.reply_text(_BLOCKED_STATE_DB_ERROR_HTML)
        return True
    if st is None:
        return False
    await update.message.reply_text(
        _blocked_user_message_html(st),
        reply_markup=_blocked_user_reply_markup(st),
        parse_mode=ParseMode.HTML,
    )
    return True


async def _reply_if_blocked_callback(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Аналог _reply_if_blocked_cmd для callback-запросов. Используется в начале button_callback."""
    if not query or not query.message or not query.from_user:
        return False
    if context.user_data.get(_BEGEMOT_OK) or context.user_data.get(_BEGEMOT_PW_WAIT):
        return False
    uid = query.from_user.id
    st, ok = await _safe_blocked_state(uid)
    if not ok:
        with suppress(Exception):
            await query.answer(_BLOCKED_STATE_DB_ERROR_HTML, show_alert=True)
        return True
    if st is None:
        return False
    with suppress(Exception):
        await query.answer()
    with suppress(Exception):
        await query.message.reply_text(
            _blocked_user_message_html(st),
            reply_markup=_blocked_user_reply_markup(st),
            parse_mode=ParseMode.HTML,
        )
    return True


async def _handle_ban_lift(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = query.from_user.id if query.from_user else 0
    st, ok = await _safe_blocked_state(uid)
    if not ok:
        await query.answer(_BLOCKED_STATE_DB_ERROR_HTML, show_alert=True)
        return
    if st is None or st.get("kind") != user_storage.USER_BLOCK_TEMP:
        await query.answer("Нет активного временного ограничения.", show_alert=True)
        return
    try:
        await asyncio.to_thread(user_storage.clear_user_block, USER_DB_PATH, uid)
    except Exception:
        logger.exception("clear_user_block failed user_id=%s", uid)
        await query.answer(_BLOCKED_STATE_DB_ERROR_HTML, show_alert=True)
        return
    await query.answer("Ограничение снято.")
    if query.message:
        with suppress(BadRequest):
            await query.edit_message_reply_markup(reply_markup=None)


async def _send_long_html(bot, chat_id: int, text: str, *, chunk: int = 3500) -> None:
    """Несколько сообщений; по возможности режем по разделителям между отзывами."""
    if len(text) <= chunk:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        return
    pieces = text.split("\n\n---\n\n")
    buf = ""
    for p in pieces:
        sep = "\n\n---\n\n" if buf else ""
        cand = buf + sep + p
        if len(cand) <= chunk:
            buf = cand
        else:
            if buf:
                await bot.send_message(chat_id, buf, parse_mode=ParseMode.HTML)
            buf = p
    while buf:
        if len(buf) <= chunk:
            await bot.send_message(chat_id, buf, parse_mode=ParseMode.HTML)
            break
        await bot.send_message(chat_id, buf[:chunk], parse_mode=ParseMode.HTML)
        buf = buf[chunk:]


async def polling_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    logger.info("cmd /polling user_id=%s", update.effective_user.id)
    if await _reply_if_blocked_cmd(update, context):
        return
    if not await _disclaimer_consent_ok(update, context):
        return
    _clear_feedback_and_begemot_wait(context)
    context.user_data.pop(_POLL_AWAIT_CAREER, None)
    context.user_data.pop(_POLL_LIKES_MATH, None)
    await update.message.reply_text(
        "<b>Опрос</b>\n\nНравится математика?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Да", callback_data="poll:m:1")],
                [InlineKeyboardButton("Нет", callback_data="poll:m:0")],
            ],
        ),
        parse_mode=ParseMode.HTML,
    )


async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    logger.info("cmd /support user_id=%s", update.effective_user.id)
    if await _reply_if_blocked_cmd(update, context):
        return
    if not await _disclaimer_consent_ok(update, context):
        return
    _clear_feedback_and_begemot_wait(context)
    context.user_data[_FEEDBACK_WAITING] = True
    await update.message.reply_text(
        "Напиши отзыв или пожелание <b>одним следующим сообщением</b> (до ~8000 символов). "
        "Можно без форматирования.\n\n"
        "Обращения на рассмотрении: /my_support\n\n"
        "Чтобы отменить ожидание - команда /start или /textbook.",
        parse_mode=ParseMode.HTML,
    )


def _format_my_support_ticket_block(t: user_storage.FeedbackTicketRow) -> str:
    dt = _h(t.created_at[:19]) if t.created_at else "?"
    head = f"<b>Обращение #{t.id}</b> ({dt})\n"
    body_preview = t.body if len(t.body) <= 600 else t.body[:600] + "…"
    body_block = f"Текст: {_h(body_preview)}\n"
    st = (t.status or "").strip().lower()
    if st == user_storage.FEEDBACK_TICKET_STATUS_PENDING:
        return head + "<i>На рассмотрении</i>\n" + body_block
    if st == user_storage.FEEDBACK_TICKET_STATUS_REVIEWED:
        note = _h((t.staff_response or "").strip() or "—")
        return head + "<b>Рассмотрено</b>\nКомментарий разработчика/админа: " + note + "\n" + body_block
    if st == user_storage.FEEDBACK_TICKET_STATUS_REJECTED:
        reason = _h((t.staff_response or "").strip() or "—")
        return head + "<b>Отклонено</b>\nПричина: " + reason + "\n" + body_block
    return head + f"<i>Статус: {_h(st)}</i>\n" + body_block


async def my_support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    logger.info("cmd /my_support user_id=%s", update.effective_user.id)
    if await _reply_if_blocked_cmd(update, context):
        return
    if not await _disclaimer_consent_ok(update, context):
        return
    _clear_feedback_and_begemot_wait(context)
    uid = update.effective_user.id
    rows = await asyncio.to_thread(user_storage.list_feedback_tickets_for_user, USER_DB_PATH, uid)
    if not rows:
        await update.message.reply_text(
            "Нет активных обращений на рассмотрении. Новое: /support",
        )
        return
    parts = ["<b>Мои обращения</b>\n"]
    for t in rows:
        parts.append(_format_my_support_ticket_block(t))
        parts.append("")
    text_html = "\n".join(parts).strip()
    await _send_long_html(context.bot, update.effective_chat.id, text_html)


async def begemot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    logger.info("cmd /begemot user_id=%s", update.effective_user.id)
    if await _reply_if_blocked_cmd(update, context):
        return
    expected = _admin_password_expected()
    if not expected:
        await update.message.reply_text("Раздел администратора не настроен.")
        return
    _clear_feedback_and_begemot_wait(context)
    context.user_data[_BEGEMOT_PW_WAIT] = True
    await update.message.reply_text("Введите пароль одним сообщением.")


async def _typing_keepalive(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("send_chat_action failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
            break
        except asyncio.TimeoutError:
            continue


async def run_with_typing(bot, chat_id: int, coro):
    """Пока выполняется coro, каждые ~4 с показывает в чате статус «печатает»."""
    stop = asyncio.Event()
    task = asyncio.create_task(_typing_keepalive(bot, chat_id, stop))
    try:
        return await coro
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _parse_sticker_set_names_env(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _shared_sticker_pool_from_config() -> list[str] | None:
    raw = (os.getenv("STICKER_SET_POOL") or "").strip()
    if not raw:
        return None
    return _parse_sticker_set_names_env(raw)


def _tada_sticker_set_name_candidates() -> list[str]:
    raw = (os.getenv("TADA_STICKER_SET_NAMES") or "").strip()
    if raw:
        return _parse_sticker_set_names_env(raw)
    shared = _shared_sticker_pool_from_config()
    if shared:
        return shared
    return list(_TADA_BUILTIN_STICKER_SET_NAMES)


def _motivation_sticker_set_name_candidates() -> list[str]:
    raw = (os.getenv("MOTIVATION_STICKER_SET_NAMES") or "").strip()
    if raw:
        return _parse_sticker_set_names_env(raw)
    shared = _shared_sticker_pool_from_config()
    if shared:
        return shared
    return list(_MOTIVATION_BUILTIN_STICKER_SET_NAMES)


def _sticker_set_names_minus(seen: set[str], names: list[str]) -> list[str]:
    return [n for n in names if n not in seen]


async def _send_sticker_from_pack(
    bot,
    chat_id: int,
    set_names: list[str],
    *,
    prefer_tada_emoji: bool,
) -> tuple[str, str] | None:
    """Отправить случайный стикер из перечисленных наборов. Возвращает (file_id, set_name) или None."""
    if not set_names:
        return None
    random.shuffle(set_names)
    for set_name in set_names:
        try:
            st_set = await bot.get_sticker_set(set_name)
            stickers = list(st_set.stickers)
            if not stickers:
                continue
            if prefer_tada_emoji:
                with_tada = [s for s in stickers if _TADA_EMOJI_PREFER in (s.emoji or "")]
                pool = with_tada if with_tada else stickers
            else:
                pool = stickers
            chosen = random.choice(pool)
            msg = await bot.send_sticker(chat_id, chosen.file_id)
            fid = msg.sticker.file_id if msg.sticker else chosen.file_id
            logger.info(
                "check sticker chat_id=%s set=%s prefer_tada_emoji=%s",
                chat_id,
                set_name,
                prefer_tada_emoji,
            )
            return (fid, set_name)
        except Exception as e:
            logger.warning("check sticker skip set=%s chat_id=%s: %s", set_name, chat_id, e)
    if set_names:
        logger.warning(
            "check sticker none sent chat_id=%s tried_sets=%s prefer_tada_emoji=%s",
            chat_id,
            set_names,
            prefer_tada_emoji,
        )
    return None


async def _after_check_stickers_quip_record(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    _outs: list[str],
    _body_raw: str,
    _summary_ok: bool,
) -> None:
    tada = _tada_sticker_set_name_candidates()
    motivation = _motivation_sticker_set_name_candidates()
    merged = list(dict.fromkeys(tada + motivation))
    random.shuffle(merged)
    res = await _send_sticker_from_pack(
        context.bot,
        chat_id,
        merged,
        prefer_tada_emoji=True,
    )
    if not res:
        res = await _send_sticker_from_pack(
            context.bot,
            chat_id,
            merged,
            prefer_tada_emoji=False,
        )
    if not res:
        return
    fid, sn = res
    tada_set = set(tada)
    kind = (
        user_storage.CHECK_STICKER_KIND_REWARD
        if sn in tada_set
        else user_storage.CHECK_STICKER_KIND_MOTIVATION
    )
    await asyncio.to_thread(
        user_storage.record_check_sticker_reward,
        USER_DB_PATH,
        user_id,
        kind,
        fid,
        sn,
        None,
    )


def load_catalog(path: str) -> dict[str, list[dict]]:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"Нет файла каталога учебников: {p.resolve()}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Каталог: ожидается объект JSON с ключами классов")
    for key in ("6", "7", "8", "9", "10", "11"):
        if key not in data:
            data[key] = []
    return data


def grade_keyboard(back_to_main: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("6 класс", callback_data="g:6"),
            InlineKeyboardButton("7 класс", callback_data="g:7"),
        ],
        [
            InlineKeyboardButton("8 класс", callback_data="g:8"),
            InlineKeyboardButton("9 класс", callback_data="g:9"),
        ],
        [
            InlineKeyboardButton("10 класс", callback_data="g:10"),
            InlineKeyboardButton("11 класс", callback_data="g:11"),
        ],
    ]
    if back_to_main:
        rows.append([InlineKeyboardButton("Назад в меню", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def _total_pages(grade: int) -> int:
    items = CATALOG.get(str(grade), [])
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def textbook_caption(grade: int, page: int) -> str:
    total = _total_pages(grade)
    page = max(0, min(page, total - 1))
    return (
        f"Класс {grade}. Страница {page + 1} из {total}.\n"
        "Выбери учебник (источник: gdz.ru). "
        "🔥 только у одного варианта — чаще всего его выбирают в этом классе по данным бота "
        "(подсказка, если не уточняли у учителя, какой учебник открыть)."
    )


_TG_INLINE_BTN_TEXT_MAX = 64


def _textbook_popularity_leader_slug(
    items: list[dict],
    slug_counts: dict[str, int],
) -> str | None:
    """
    Один slug с максимумом выборов в классе; при равенстве — первый по порядку в каталоге.
    Нет счетчиков или все нули — None.
    """
    if not slug_counts:
        return None
    max_c = 0
    for item in items:
        slug = str(item.get("slug", ""))
        max_c = max(max_c, slug_counts.get(slug, 0))
    if max_c <= 0:
        return None
    for item in items:
        slug = str(item.get("slug", ""))
        if slug_counts.get(slug, 0) == max_c:
            return slug
    return None


def _truncate_inline_button_text(text: str, max_len: int = _TG_INLINE_BTN_TEXT_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def textbook_keyboard(
    grade: int,
    page: int,
    slug_counts: dict[str, int] | None = None,
) -> InlineKeyboardMarkup:
    items = CATALOG.get(str(grade), [])
    n = len(items)
    total_pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]

    leader_slug: str | None = None
    if slug_counts:
        leader_slug = _textbook_popularity_leader_slug(items, slug_counts)

    rows: list[list[InlineKeyboardButton]] = []
    for i, item in enumerate(chunk):
        idx = start + i
        label = str(item.get("label", "Учебник"))
        if item.get("is_premium"):
            label = f"{label} (Премиум)"
        slug = str(item.get("slug", ""))
        flame = "🔥 " if (leader_slug and slug == leader_slug) else ""
        full = flame + label
        full = _truncate_inline_button_text(full)
        rows.append([InlineKeyboardButton(full, callback_data=f"tb:{grade}:{idx}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Назад", callback_data=f"pg:{grade}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Далее", callback_data=f"pg:{grade}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Назад", callback_data="chg_tb")])
    return InlineKeyboardMarkup(rows)


def get_main_keyboard(
    uploaded: bool,
    profile: user_storage.UserProfile,
    *,
    show_check_button: bool = True,
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    hw_ok = user_storage.homework_complete(profile)
    rows: list[list[InlineKeyboardButton]] = []
    if not hw_ok:
        rows.append([InlineKeyboardButton("Указать задание", callback_data="start_hw")])
    else:
        if not uploaded:
            rows.append(
                [
                    InlineKeyboardButton("Загрузить фото", callback_data="upload"),
                    InlineKeyboardButton("Показать ГДЗ", callback_data="show_sol"),
                ],
            )
        elif show_check_button:
            rows.append(
                [
                    InlineKeyboardButton("Проверить", callback_data="check"),
                    InlineKeyboardButton("Показать ГДЗ", callback_data="show_sol"),
                ],
            )
        else:
            rows.append([InlineKeyboardButton("Показать ГДЗ", callback_data="show_sol")])
        rows.append(
            [InlineKeyboardButton("Ответить текстом", callback_data="answer_text")],
        )
        rows.append(
            [InlineKeyboardButton("Выбрать упражнение или проверочную", callback_data="chg_hw")],
        )
    return InlineKeyboardMarkup(rows)


def get_check_result_keyboard(
    profile: user_storage.UserProfile,
    user_id: int,
) -> InlineKeyboardMarkup:
    """Меню после проверки: как у загруженного фото без кнопки «Проверить», плюс 👍/👎."""
    base = get_main_keyboard(
        uploaded=True,
        profile=profile,
        show_check_button=False,
        user_id=user_id,
    )
    rows = [list(r) for r in base.inline_keyboard]
    rows.append(
        [
            InlineKeyboardButton("👍", callback_data="cfv:1"),
            InlineKeyboardButton("👎", callback_data="cfv:-1"),
        ],
    )
    return InlineKeyboardMarkup(rows)


def upload_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Назад", callback_data="back_from_upload")]]
    )


def paragraph_step_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Назад", callback_data="back_hw:p")]]
    )


def _paragraph_range_label(num_buttons: int) -> str:
    n = max(1, min(int(num_buttons), _paragraph_buttons_cap()))
    return "§1" if n <= 1 else f"§1–§{n}"


def paragraph_choice_keyboard(num_buttons: int) -> InlineKeyboardMarkup:
    """Кнопки §1…§N по числу из оглавления gdz.ru (с потолком); значение для БД - номер строкой."""
    n = max(1, min(int(num_buttons), _paragraph_buttons_cap()))
    nums = list(range(1, n + 1))
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(nums), _PARAGRAPH_ROW):
        chunk = nums[i : i + _PARAGRAPH_ROW]
        rows.append([InlineKeyboardButton(f"§{x}", callback_data=f"hw_p:{x}") for x in chunk])
    rows.append(
        [InlineKeyboardButton("Свой номер (ввод текста)", callback_data="hw_p:custom")],
    )
    rows.append([InlineKeyboardButton("Назад", callback_data="back_hw:p")])
    return InlineKeyboardMarkup(rows)


def _paragraph_prompt_caption(num_buttons: int) -> str:
    span = _paragraph_range_label(num_buttons)
    return (
        f"<b>Шаг 1 из 2.</b> Выбери параграф кнопкой ({span}) или нажми «Свой номер»."
    )


def _fetch_paragraph_button_count_sync(textbook_url: str) -> int:
    headers = {"User-Agent": gdz_solution.USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=25.0) as client:
            r = client.get(textbook_url.strip())
            r.raise_for_status()
            m = gdz_solution.max_chapter_index_from_index_html(r.text, textbook_url)
    except Exception as e:
        logger.warning("paragraph index fetch failed url=%s: %s", textbook_url[:80], e)
        m = None
    default = 6
    if m is None or m < 1:
        return default
    return min(int(m), _paragraph_buttons_cap())


async def _resolve_paragraph_button_count(textbook_url: str) -> int:
    url = textbook_url.strip()
    if not url:
        return 6
    now = time.monotonic()
    ttl = _paragraph_index_cache_ttl()
    hit = _PARAGRAPH_COUNT_CACHE.get(url)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    n = await asyncio.to_thread(_fetch_paragraph_button_count_sync, url)
    _PARAGRAPH_COUNT_CACHE[url] = (now, n)
    return n


def page_keypad_caption(buf: str) -> str:
    display = buf if buf else "—"
    return (
        "<b>Шаг 2 из 2.</b> Номер страницы проверочной (стр.): нажми цифры, затем «Стр. — подтвердить».\n"
        f"Сейчас: {_h(display)} (от 1 до 999)"
    )


def page_keypad_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for nums in ([1, 2, 3], [4, 5, 6], [7, 8, 9]):
        rows.append(
            [InlineKeyboardButton(str(n), callback_data=f"pgd:{n}") for n in nums],
        )
    rows.append(
        [
            InlineKeyboardButton("Удал.", callback_data="pgk:bs"),
            InlineKeyboardButton("0", callback_data="pgd:0"),
        ],
    )
    rows.append([InlineKeyboardButton("Стр. — подтвердить", callback_data="pgk:ok")])
    rows.append([InlineKeyboardButton("Назад", callback_data="back_hw:pk")])
    return InlineKeyboardMarkup(rows)


def _main_menu_caption(profile: user_storage.UserProfile) -> str:
    prem = " (Премиум)" if profile.is_premium else ""
    if user_storage.homework_complete(profile):
        hint = (
            "\n\nМожно «Загрузить фото», «Ответить текстом», «Показать ГДЗ» с gdz.ru "
            "или «Проверить» после фото."
        )
    else:
        hint = (
            "\n\n<i>Дальше укажи задание:</i> параграф и упражнение или страницу проверочной "
            "— кнопка «Указать задание»."
        )
    up = user_storage.homework_complete(profile)
    hw_line = _hw_summary_html(profile) if up else _h("еще не задана")
    book = _textbook_label_html(profile)
    g = profile.grade
    return (
        f"<b>Привет! 👋</b> Твой учебник{prem}: {book} ({g} класс).\n"
        f"<i>Текущая привязка:</i> {hw_line}.{hint}"
    )


def _hw_summary(profile: user_storage.UserProfile) -> str:
    p = profile.hw_paragraph or "—"
    if profile.hw_page is not None:
        return f"Параграф {p}, страница {profile.hw_page} (проверочная)"
    if profile.hw_exercise:
        return f"Параграф {p}, упражнение {profile.hw_exercise}"
    return f"Параграф {p}"


def _hw_summary_html(profile: user_storage.UserProfile) -> str:
    p = profile.hw_paragraph or "—"
    pe = _h(p)
    if profile.hw_page is not None:
        return f"Параграф {pe}, страница {_h(str(profile.hw_page))} (<b>проверочная</b>)"
    if profile.hw_exercise:
        return f"Параграф {pe}, упражнение {_h(profile.hw_exercise)}"
    return f"Параграф {pe}"


def _saved_homework_title_html(prof: user_storage.UserProfile) -> str:
    return f"<b>Сохранено:</b> {_hw_summary_html(prof)}."


def _strip_trailing_item_punct(s: str) -> str:
    """Убирает хвостовые пробелы, точку, запятую, точку с запятой (не двоеточие в середине фразы)."""
    return (s or "").rstrip(" \t.,;\u00a0")


# Буквенные подпункты а) б) — не цепляют «34)» внутри (51 + 34)
_TASK_LETTER_ITEM_RE = re.compile(
    r"(?:^|(?<=[\s:.;,\n\u2014\u2013\u2212\-]))([а-яёА-ЯЁa-zA-Z])\)\s*",
)
# Нумерованные 1) 2) только после начала строки, : или ; — не после «+ 34)»
_TASK_NUM_ITEM_RE = re.compile(r"(?:(?:^|\n)\s*|(?<=:)\s*|(?<=;)\s*)(\d+)\)\s*")


def split_task_condition_items(text: str) -> tuple[str, list[str]]:
    """
    Делит текст условия с gdz.ru на вводную строку и пункты «а) …», «1) …».
    У каждого пункта снимаются хвостовые . , ;
    """
    raw = (text or "").strip()
    if not raw:
        return "", []
    raw = _strip_trailing_item_punct(raw)
    matches = list(_TASK_LETTER_ITEM_RE.finditer(raw))
    if not matches:
        matches = list(_TASK_NUM_ITEM_RE.finditer(raw))
    if not matches:
        return _strip_trailing_item_punct(raw), []
    intro = _strip_trailing_item_punct(raw[: matches[0].start()].strip())
    items: list[str] = []
    for i, m in enumerate(matches):
        g1 = m.group(1)
        label = f"{g1})"
        body_start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        chunk = _strip_trailing_item_punct(raw[body_start:end].strip())
        items.append(f"{label} {chunk}".strip() if chunk else label)
    return intro, items


def _format_text_answer_prompt_html(condition_raw: str) -> str:
    """
    HTML для экрана «Ответить текстом»: один блок <pre> с условием (кнопка копирования в Telegram).
    """
    cr = (condition_raw or "").strip()
    if not cr:
        return (
            "<i>Условие с gdz.ru сейчас не подставилось — ориентируйся на сообщение бота "
            "с условием выше, если оно было.</i>\n\n"
        )
    intro, items = split_task_condition_items(cr)
    cap = 2800
    if items:
        cond_lines: list[str] = []
        if intro:
            cond_lines.append(_strip_trailing_item_punct(intro))
        for it in items:
            cond_lines.append(_strip_trailing_item_punct(it))
        body_plain = "\n\n".join(cond_lines)
    else:
        body_plain = _strip_trailing_item_punct(intro)
    body_plain = body_plain[:cap]
    return (
        "<b>Условия задачи (можно скопировать и дописать решение)</b>\n"
        f"<pre>{html.escape(body_plain)}</pre>\n\n"
    )


async def _send_gdz_task_condition_to_chat(
    bot,
    chat_id: int,
    profile: user_storage.UserProfile,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """После сохранения привязки ДЗ — публикует текст условия из блока задания на gdz.ru (как в учебнике)."""
    turl = (profile.textbook_url or "").strip()
    para = (profile.hw_paragraph or "").strip()
    if not turl or not para:
        return
    ex = (profile.hw_exercise or "").strip() or None
    pg = profile.hw_page
    if ex is None and pg is None:
        return

    async def _work() -> None:
        _meta, cond = await asyncio.to_thread(
            gdz_solution.fetch_homework_check_gdz_data,
            turl,
            para,
            ex,
            pg,
        )
        cond = (cond or "").strip()
        if not cond:
            logger.info(
                "gdz task condition empty after hw save user_id=%s para=%r ex=%r page=%s",
                user_id,
                para[:40],
                ex,
                pg,
            )
            return
        cap = 3500
        if len(cond) > cap:
            cond = cond[: cap - 3] + "..."
        header = "<b>Условие задачи</b> (текст с gdz.ru, как в учебнике):\n"
        body = telegram_format.markdownish_to_telegram_html(cond)
        msg_html = header + body
        if len(msg_html) > 4096:
            msg_html = msg_html[:4093] + "..."
        try:
            flow_note(
                context,
                await bot.send_message(chat_id, msg_html, parse_mode=ParseMode.HTML),
            )
        except BadRequest as e:
            logger.warning("gdz task condition HTML failed user_id=%s: %s", user_id, e)
            plain = f"Условие задачи (текст с gdz.ru):\n\n{cond}"
            flow_note(context, await bot.send_message(chat_id, plain))
        logger.info(
            "gdz task condition sent user_id=%s para=%r ex=%r page=%s len=%s",
            user_id,
            para[:40],
            ex,
            pg,
            len(cond),
        )

    await _work()


def _textbook_pick_saved_html(label: str, slug: str, grade: int, prem: str) -> str:
    y = _infer_textbook_edition_year(slug, label)
    esc = _h(label)
    book = f"{esc}, {y} г." if y else esc
    return f"<b>Сохранено{prem}:</b> {book} ({grade} класс)."


def _upload_return_caption_html(profile: user_storage.UserProfile) -> str:
    prem = " (Премиум)" if profile.is_premium else ""
    book = _textbook_label_html(profile)
    g = profile.grade
    hw_line = (
        _hw_summary_html(profile)
        if user_storage.homework_complete(profile)
        else _h("задание еще не указано")
    )
    return (
        f"Твой учебник{prem}: {book} ({g} класс).\n"
        f"<i>Привязка:</i> {hw_line}.\n\n"
        "Дальше: укажи задание или загрузи фото."
    )


def parse_exercise_or_page(text: str) -> tuple[str | None, int | None]:
    t = text.strip()
    if not t:
        return None, None
    m = re.search(r"(?i)(?:страница|стр\.?)\s*:?\s*(\d+)\b", t)
    if m:
        return None, int(m.group(1))
    m2 = re.search(r"(?i)\bpage\s*:?\s*(\d+)\b", t)
    if m2:
        return None, int(m2.group(1))
    t2 = re.sub(r"(?i)^(упр\.?|упражнение|номер|№)\s*:?\s*", "", t).strip()
    if t2:
        return t2, None
    return None, None


def _paragraph_n_from_context(context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        return max(1, min(int(context.user_data.get(_HW_PAR_BTN_MAX) or 6), _paragraph_buttons_cap()))
    except (TypeError, ValueError):
        return 6


async def _send_paragraph_prompt(
    bot,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    context.user_data.pop("hw_paragraph_draft", None)
    _pop_step2_gdz_meta(context)
    context.user_data.pop(_HW_PAGE_BUF, None)
    context.user_data[_HW_STEP] = _HW_PARAGRAPH_PICK
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    n = 6
    if profile and (profile.textbook_url or "").strip():
        n = await _resolve_paragraph_button_count(profile.textbook_url)
    context.user_data[_HW_PAR_BTN_MAX] = n
    msg = await bot.send_message(
        chat_id,
        _paragraph_prompt_caption(n),
        reply_markup=paragraph_choice_keyboard(n),
        parse_mode=ParseMode.HTML,
    )
    flow_note(context, msg)


async def _send_gdz_solution_to_chat(
    bot,
    chat_id: int,
    profile: user_storage.UserProfile,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.info(
        "gdz fetch user_id=%s paragraph=%r exercise=%r page=%s",
        user_id,
        (profile.hw_paragraph or "")[:80],
        profile.hw_exercise,
        profile.hw_page,
    )

    async def _work() -> None:
        res, err = await asyncio.to_thread(
            gdz_solution.fetch_solution,
            profile.textbook_url,
            profile.hw_paragraph or "",
            profile.hw_exercise,
            profile.hw_page,
        )
        if err:
            logger.warning("gdz fetch failed user_id=%s: %s", user_id, err[:200] if err else "")
            flow_note(context, await bot.send_message(chat_id, err))
            return
        from_cache = bool(res.image_paths)
        logger.info(
            "gdz ok user_id=%s cache=%s images=%s",
            user_id,
            from_cache,
            len(res.image_paths) if res.image_paths else len(res.image_urls),
        )
        header = "Готовое решение (из кеша, без запроса к сайту)" if from_cache else "Готовое решение на gdz.ru"
        lines = [f"{header}: {res.page_url}"]
        if res.condition_text:
            lines.append("")
            lines.append("Условие:")
            lines.append(res.condition_text)
        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4093] + "..."
        html_text = telegram_format.markdownish_to_telegram_html(text)
        try:
            flow_note(
                context,
                await bot.send_message(chat_id, html_text, parse_mode=ParseMode.HTML),
            )
        except BadRequest as e:
            logger.warning("gdz send_message HTML failed, plain: %s", e)
            flow_note(context, await bot.send_message(chat_id, text))

        total = len(res.image_paths) if res.image_paths else len(res.image_urls)
        limit = 8

        if res.image_paths:
            for i, path in enumerate(res.image_paths[:limit]):
                try:
                    path_p = Path(path)
                    raw = await asyncio.to_thread(path_p.read_bytes)
                    ext = ".png" if path.lower().endswith(".png") else ".jpg"
                    flow_note(
                        context,
                        await bot.send_photo(
                            chat_id,
                            photo=InputFile(io.BytesIO(raw), filename=f"gdz{i}{ext}"),
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        "gdz send_photo from cache failed path=%s user_id=%s: %s",
                        path,
                        user_id,
                        e,
                    )
        else:
            headers = {"User-Agent": gdz_solution.USER_AGENT}
            async with httpx.AsyncClient(
                timeout=45.0,
                headers=headers,
                transport=async_http_transport_ipv4_lookup(),
            ) as client:
                for i, url in enumerate(res.image_urls[:limit]):
                    try:
                        r = await client.get(url)
                        r.raise_for_status()
                        ct = (r.headers.get("content-type") or "").lower()
                        ext = ".png" if "png" in ct else ".jpg"
                        flow_note(
                            context,
                            await bot.send_photo(
                                chat_id,
                                photo=InputFile(io.BytesIO(r.content), filename=f"gdz{i}{ext}"),
                            ),
                        )
                    except Exception as e:
                        logger.warning(
                            "gdz send_photo from url failed url=%s user_id=%s: %s",
                            url[:120],
                            user_id,
                            e,
                        )

        if total > limit:
            flow_note(
                context,
                await bot.send_message(
                    chat_id,
                    f"Еще {total - limit} снимков — открой страницу по ссылке выше",
                ),
            )

        has_photo = _user_has_uploaded_photo(user_id)
        await bot.send_message(
            chat_id,
            "Меню:",
            reply_markup=get_main_keyboard(uploaded=has_photo, profile=profile, user_id=user_id),
        )

    await run_with_typing(bot, chat_id, _work())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if await _reply_if_blocked_cmd(update, context):
        return
    user_id = update.effective_user.id
    context.user_data.pop(_HW_STEP, None)
    context.user_data.pop("hw_paragraph_draft", None)
    context.user_data.pop(_HW_PAGE_BUF, None)
    context.user_data.pop(_HW_PAR_BTN_MAX, None)
    context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
    _pop_step2_gdz_meta(context)
    _clear_begemot_session(context)
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    logger.info("cmd /start user_id=%s has_profile=%s", user_id, profile is not None)
    cid = update.effective_chat.id
    await flow_purge_except(context.bot, cid, context, keep_message_id=None)

    dcfg = load_disclaimer_config()
    if dcfg is not None:
        consent_row = await asyncio.to_thread(user_storage.get_user_consent, USER_DB_PATH, user_id)
        if not user_storage.consent_fully_accepted(consent_row, dcfg.version):
            if user_storage.consent_awaiting_final_accept(consent_row, dcfg.version):
                await _send_disclaimer_accept_screen(context.bot, cid, dcfg)
            else:
                await _send_disclaimer_quiz_screen(context.bot, cid, dcfg)
            return

    await _continue_start_after_consent(context.bot, cid, context, user_id)


async def textbook_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        logger.info("cmd /textbook user_id=%s", update.effective_user.id)
    if not update.message or not update.effective_user:
        return
    if await _reply_if_blocked_cmd(update, context):
        return
    context.user_data.pop(_HW_STEP, None)
    context.user_data.pop("hw_paragraph_draft", None)
    context.user_data.pop(_HW_PAGE_BUF, None)
    context.user_data.pop(_HW_PAR_BTN_MAX, None)
    context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
    _pop_step2_gdz_meta(context)
    _clear_begemot_session(context)
    uid = update.effective_user.id
    cid = update.effective_chat.id
    await flow_purge_except(context.bot, cid, context, keep_message_id=None)
    if not await _disclaimer_consent_ok(update, context):
        return
    await update.message.reply_text("Выбери класс:", reply_markup=grade_keyboard(back_to_main=False))


async def handle_homework_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if not (
        context.user_data.get(_BEGEMOT_OK)
        or context.user_data.get(_BEGEMOT_PW_WAIT)
        or context.user_data.get(_ADMIN_BAN_WAIT)
        or context.user_data.get(_FEEDBACK_STAFF_WAIT)
        or context.user_data.get(_CHECK_DISLIKE_FEEDBACK_WAIT)
    ):
        st_blk, ok_blk = await _safe_blocked_state(user_id)
        if not ok_blk:
            flow_note(
                context,
                await update.message.reply_text(_BLOCKED_STATE_DB_ERROR_HTML),
            )
            return
        if st_blk is not None:
            flow_note(
                context,
                await update.message.reply_text(
                    _blocked_user_message_html(st_blk),
                    reply_markup=_blocked_user_reply_markup(st_blk),
                    parse_mode=ParseMode.HTML,
                ),
            )
            return

    if text == _STATS_REPLY_BUTTON_TEXT:
        await _send_stats_message(
            context.bot,
            update.effective_chat.id,
            update.effective_user.id,
            context,
        )
        return

    if context.user_data.get(_CHECK_DISLIKE_FEEDBACK_WAIT):
        if not text:
            flow_note(
                context,
                await update.message.reply_text(
                    "Напиши текст замечания одним сообщением или отмени: /start.",
                ),
            )
            return
        context.user_data.pop(_CHECK_DISLIKE_FEEDBACK_WAIT, None)
        uname = update.effective_user.username
        fb_body = f"[Дизлайк оценки проверки ДЗ]\n{text}"
        try:
            _, ticket_id = await asyncio.to_thread(
                user_storage.append_user_feedback,
                USER_DB_PATH,
                user_id,
                uname,
                fb_body,
            )
        except ValueError:
            flow_note(
                context,
                await update.message.reply_text("Текст не может быть пустым."),
            )
            return
        _schedule_feedback_tei_analysis(ticket_id, fb_body)
        flow_note(
            context,
            await update.message.reply_text(
                "Сохранили обращение. Статус: /my_support.",
            ),
        )
        return

    if context.user_data.get(_POLL_AWAIT_CAREER):
        likes = context.user_data.get(_POLL_LIKES_MATH)
        if likes not in (0, 1):
            context.user_data.pop(_POLL_AWAIT_CAREER, None)
            context.user_data.pop(_POLL_LIKES_MATH, None)
            await update.message.reply_text(
                "Сначала нажми «Да» или «Нет» под вопросом про математику или начни заново: /polling",
            )
            return
        if not text:
            flow_note(
                context,
                await update.message.reply_text(
                    "Текст пустой. Напиши, кем хочешь стать, одним сообщением или отмени: /start.",
                ),
            )
            return
        try:
            await asyncio.to_thread(
                user_storage.upsert_user_poll,
                USER_DB_PATH,
                user_id,
                likes,
                text,
            )
        except ValueError as e:
            flow_note(context, await update.message.reply_text(str(e)))
            return
        context.user_data.pop(_POLL_AWAIT_CAREER, None)
        context.user_data.pop(_POLL_LIKES_MATH, None)
        logger.info("poll saved user_id=%s likes_math=%s", user_id, likes)
        tgzh_metrics.record_poll_completed()
        flow_note(
            context,
            await update.message.reply_text("Спасибо, ответы сохранены. Пройти опрос еще раз: /polling."),
        )
        return

    if context.user_data.get(_FEEDBACK_WAITING):
        if not text:
            flow_note(
                context,
                await update.message.reply_text(
                    "Текст пустой. Напиши отзыв текстом или отмени: /start или /textbook.",
                ),
            )
            return
        uname = update.effective_user.username
        try:
            _, ticket_id = await asyncio.to_thread(
                user_storage.append_user_feedback,
                USER_DB_PATH,
                user_id,
                uname,
                text,
            )
        except ValueError:
            flow_note(
                context,
                await update.message.reply_text("Текст не может быть пустым."),
            )
            return
        _schedule_feedback_tei_analysis(ticket_id, text)
        context.user_data.pop(_FEEDBACK_WAITING, None)
        logger.info("feedback appended user_id=%s ticket_id=%s", user_id, ticket_id)
        flow_note(
            context,
            await update.message.reply_text(
                "Спасибо, сообщение сохранено. Оставить еще одно обращение - команда /support. "
                "Статусы: /my_support",
            ),
        )
        return

    staff_spec = context.user_data.get(_FEEDBACK_STAFF_WAIT)
    if staff_spec:
        if not context.user_data.get(_BEGEMOT_OK):
            context.user_data.pop(_FEEDBACK_STAFF_WAIT, None)
            return
        if not text or not text.strip():
            flow_note(
                context,
                await update.message.reply_text("Текст не может быть пустым. Напиши ответ одним сообщением."),
            )
            return
        ticket_id = int(staff_spec["ticket_id"])
        kind = str(staff_spec.get("kind") or "")
        tk_before = await asyncio.to_thread(
            user_storage.get_feedback_ticket_by_id,
            USER_DB_PATH,
            ticket_id,
        )
        context.user_data.pop(_FEEDBACK_STAFF_WAIT, None)
        status = (
            user_storage.FEEDBACK_TICKET_STATUS_REVIEWED
            if kind == "review"
            else user_storage.FEEDBACK_TICKET_STATUS_REJECTED
        )
        try:
            ok = await asyncio.to_thread(
                user_storage.resolve_feedback_ticket,
                USER_DB_PATH,
                ticket_id,
                status,
                text.strip(),
            )
        except ValueError as e:
            flow_note(context, await update.message.reply_text(str(e)))
            return
        if ok:
            note = text.strip()
            if (
                tk_before is not None
                and tk_before.status == user_storage.FEEDBACK_TICKET_STATUS_PENDING
            ):
                target_uid = tk_before.user_id
                if status == user_storage.FEEDBACK_TICKET_STATUS_REVIEWED:
                    user_html = f"<b>Обращение #{ticket_id} рассмотрено.</b>\n\n{_h(note)}"
                else:
                    user_html = (
                        f"<b>Обращение #{ticket_id} отклонено.</b>\n\n"
                        f"<b>Причина:</b>\n{_h(note)}"
                    )
                try:
                    await context.bot.send_message(
                        target_uid,
                        user_html,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    logger.warning(
                        "ticket resolve notify failed user_id=%s ticket_id=%s",
                        target_uid,
                        ticket_id,
                        exc_info=True,
                    )
            flow_note(
                context,
                await update.message.reply_text("Ответ по тикету сохранен, тикет в архиве."),
            )
        else:
            flow_note(
                context,
                await update.message.reply_text(
                    "Тикет не найден или уже обработан (не в статусе «на рассмотрении»).",
                ),
            )
        return

    ban_spec = context.user_data.get(_ADMIN_BAN_WAIT)
    if ban_spec:
        if not context.user_data.get(_BEGEMOT_OK):
            context.user_data.pop(_ADMIN_BAN_WAIT, None)
            return
        if not text.strip():
            flow_note(
                context,
                await update.message.reply_text(
                    "Причина не может быть пустой. Напиши текст одним сообщением.",
                ),
            )
            return
        target_id = int(ban_spec["target_id"])
        context.user_data.pop(_ADMIN_BAN_WAIT, None)
        if target_id == user_id:
            flow_note(
                context,
                await update.message.reply_text("Нельзя заблокировать свой аккаунт."),
            )
            return
        reason = text.strip()
        until_s = await asyncio.to_thread(
            user_storage.set_temp_user_block,
            USER_DB_PATH,
            target_id,
            reason,
            1.0,
        )
        notice = _format_temp_block_notice_for_user(reason, until_s)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Снять ограничение", callback_data="ban:lift")]])
        try:
            await context.bot.send_message(
                target_id,
                notice,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.warning("temp block notify failed target_id=%s", target_id, exc_info=True)
            flow_note(
                context,
                await update.message.reply_text(
                    "Блок записан, но сообщение пользователю не доставлено "
                    "(нет диалога с ботом или ошибка Telegram).",
                ),
            )
        else:
            flow_note(
                context,
                await update.message.reply_text(
                    f"Временный блок на 1 ч для <code>{target_id}</code> записан.",
                    parse_mode=ParseMode.HTML,
                ),
            )
        return

    if context.user_data.get(_BEGEMOT_PW_WAIT):
        expected = _admin_password_expected()
        context.user_data.pop(_BEGEMOT_PW_WAIT, None)
        if not expected:
            await update.message.reply_text("Раздел недоступен.")
            return
        if not _admin_password_matches(text, expected):
            await update.message.reply_text("Неверный пароль.")
            return
        context.user_data[_BEGEMOT_OK] = True
        total = await asyncio.to_thread(user_storage.count_user_feedback, USER_DB_PATH)
        v_up, v_down = await asyncio.to_thread(
            user_storage.check_result_vote_totals,
            USER_DB_PATH,
        )
        rows = await asyncio.to_thread(
            user_storage.list_user_feedback,
            USER_DB_PATH,
            limit=_FEEDBACK_PAGE,
            offset=0,
        )
        header = (
            f"Доступ открыт. Пользователей с отзывами: <b>{total}</b>.\n"
            f"Оценки проверки (👍/👎 под результатом): <b>{v_up}</b> / <b>{v_down}</b>."
        )
        body = _format_feedback_list_html(rows)
        markup_rows: list[list[InlineKeyboardButton]] = []
        if rows:
            markup_rows.append(
                [
                    InlineKeyboardButton(str(r.user_id), callback_data=f"fb:v:{r.user_id}")
                    for r in rows
                ],
            )
        markup_rows.extend(_admin_feedback_nav_keyboard(0, total).inline_keyboard)
        await update.message.reply_text(
            header + "\n\n" + body,
            reply_markup=InlineKeyboardMarkup(markup_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    if context.user_data.get(_AWAIT_TEXT_ANSWER):
        if not await _disclaimer_consent_ok(update, context):
            context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
            return
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None or not user_storage.homework_complete(profile):
            context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
            flow_note(
                context,
                await update.message.reply_text(
                    "Сначала укажи задание — кнопка «Указать задание» или /start.",
                    reply_markup=grade_keyboard(back_to_main=False)
                    if profile is None
                    else get_main_keyboard(
                        uploaded=_user_has_uploaded_photo(user_id),
                        profile=profile,
                        user_id=user_id,
                    ),
                ),
            )
            return
        if not text.strip():
            flow_note(
                context,
                await update.message.reply_text(
                    "Напиши ответ одним сообщением или нажми «Назад» под подсказкой.",
                ),
            )
            return
        if len(text) > 15000:
            flow_note(
                context,
                await update.message.reply_text(
                    "Слишком длинное сообщение. Сократи текст (до примерно 15000 символов).",
                ),
            )
            return
        context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
        cid = update.effective_chat.id
        await run_with_typing(
            context.bot,
            cid,
            _run_homework_text_answer_check(
                update,
                context,
                user_id=user_id,
                chat_id=cid,
                profile=profile,
                answer_plain=text,
            ),
        )
        return

    step = context.user_data.get(_HW_STEP)
    if not step:
        return

    logger.debug("hw_text user_id=%s step=%s text_len=%s", user_id, step, len(text))
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    if profile is None:
        context.user_data.pop(_HW_STEP, None)
        context.user_data.pop(_HW_PAR_BTN_MAX, None)
        _pop_step2_gdz_meta(context)
        flow_note(
            context,
            await update.message.reply_text(
                "Сначала выбери учебник: /start",
                reply_markup=grade_keyboard(back_to_main=False),
            ),
        )
        return

    if step == _HW_PARAGRAPH_PICK:
        pn = _paragraph_n_from_context(context)
        flow_note(
            context,
            await update.message.reply_text(
                f"Сначала нажми кнопку с параграфом ({_paragraph_range_label(pn)}) или «Свой номер».",
                reply_markup=paragraph_choice_keyboard(pn),
            ),
        )
        return

    if step == _HW_PARAGRAPH_MANUAL:
        if not text:
            flow_note(context, await update.message.reply_text("Номер параграфа не может быть пустым. Введи еще раз."))
            return
        context.user_data["hw_paragraph_draft"] = text
        context.user_data[_HW_STEP] = "exercise_page"
        pcid = update.effective_chat.id
        para_draft = text
        book_url = (profile.textbook_url or "").strip()

        async def _step2_msg():
            await _prepare_step2_after_paragraph(context, book_url, para_draft)
            return await update.message.reply_text(
                _step2_hw_caption(context),
                reply_markup=exercise_step_keyboard_from_context(context),
                parse_mode=ParseMode.HTML,
            )

        flow_note(context, await run_with_typing(context.bot, pcid, _step2_msg()))
        return

    if step == _HW_EX_KEYPAD:
        flow_note(
            context,
            await update.message.reply_text(
                "Набери номер упражнения кнопками под сообщением или нажми «Назад».",
            ),
        )
        return

    if step == _HW_PAGE_KEYPAD:
        flow_note(
            context,
            await update.message.reply_text(
                "Набери номер страницы кнопками под сообщением или нажми «Назад».",
            ),
        )
        return

    if step == "exercise_page":
        paragraph = context.user_data.get("hw_paragraph_draft") or ""
        if not paragraph.strip():
            context.user_data[_HW_STEP] = _HW_PARAGRAPH_PICK
            _pop_step2_gdz_meta(context)
            pn = _paragraph_n_from_context(context)
            flow_note(
                context,
                await update.message.reply_text(
                    f"Параграф потерян. Выбери параграф кнопкой ({_paragraph_range_label(pn)}) или «Свой номер»:",
                    reply_markup=paragraph_choice_keyboard(pn),
                ),
            )
            return
        ex, pg = parse_exercise_or_page(text)
        if not ex and pg is None:
            flow_note(
                context,
                await update.message.reply_text(
                    "Не разобрал ответ. Пришли номер упражнения (например 7) "
                    "или страницу проверочной: стр 42",
                ),
            )
            return

        cid = update.effective_chat.id

        async def _save_hw_reply() -> None:
            await asyncio.to_thread(
                user_storage.set_homework_meta,
                USER_DB_PATH,
                user_id,
                paragraph,
                ex,
                pg,
            )
            context.user_data.pop(_HW_STEP, None)
            context.user_data.pop("hw_paragraph_draft", None)
            context.user_data.pop(_HW_PAGE_BUF, None)
            context.user_data.pop(_HW_PAR_BTN_MAX, None)
            _pop_step2_gdz_meta(context)
            prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
            assert prof is not None
            logger.info(
                "hw saved user_id=%s summary=%r",
                user_id,
                _hw_summary(prof),
            )
            flow_note(
                context,
                await update.message.reply_text(
                    _saved_homework_title_html(prof),
                    parse_mode=ParseMode.HTML,
                ),
            )
            await _send_gdz_task_condition_to_chat(context.bot, cid, prof, user_id, context)
            flow_note(
                context,
                await context.bot.send_message(
                    cid,
                    _SAVED_HW_FOLLOWUP_HTML,
                    reply_markup=get_main_keyboard(uploaded=False, profile=prof, user_id=user_id),
                    parse_mode=ParseMode.HTML,
                ),
            )

        await run_with_typing(context.bot, cid, _save_hw_reply())


async def _handle_disclaimer_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    dcfg = load_disclaimer_config()
    user_id = query.from_user.id if query.from_user else 0
    chat_id = query.message.chat_id
    if dcfg is None:
        await query.answer()
        return
    parts = data.split(":")
    picked: int | None = None
    # Новый формат: dc:quiz:{DISCLAIMER_VERSION}:{OPT_N} - не совпадает с устаревшей клавиатурой
    if len(parts) == 4 and parts[0] == "dc" and parts[1] == "quiz":
        try:
            msg_ver = int(parts[2].strip())
            picked = int(parts[3].strip())
        except ValueError:
            await query.answer()
            return
        if msg_ver != dcfg.version:
            await query.answer(
                "Текст условий обновился. Нажми /start внизу чата.",
                show_alert=True,
            )
            return
    # Старый формат dc:q:{idx} (сообщения до обновления бота)
    elif len(parts) == 3 and parts[0] == "dc" and parts[1] == "q":
        try:
            picked = int(parts[2].strip())
        except ValueError:
            await query.answer()
            return

    if picked is not None:
        if picked == dcfg.correct_index:
            try:
                await asyncio.to_thread(
                    user_storage.upsert_disclaimer_quiz_passed,
                    USER_DB_PATH,
                    user_id,
                    dcfg.version,
                )
            except Exception:
                logger.exception(
                    "disclaimer quiz upsert failed user_id=%s data=%r",
                    user_id,
                    data,
                )
                await query.answer(
                    "Не удалось сохранить ответ. Попробуй еще раз или отправь /start.",
                    show_alert=True,
                )
                return
            context.user_data[_DISCLAIMER_WAIT_ACCEPT] = True
            await query.answer("Верно")
            compact = "<b>Согласие</b>\nОтвет верный. Подтверди условия кнопкой ниже."
            try:
                await query.edit_message_text(
                    compact,
                    reply_markup=_disclaimer_accept_markup(dcfg),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                await context.bot.send_message(
                    chat_id,
                    "Ответ верный. Подтверди согласие с условиями кнопкой ниже.",
                    reply_markup=_disclaimer_accept_markup(dcfg),
                    parse_mode=ParseMode.HTML,
                )
        else:
            logger.info(
                "disclaimer quiz wrong user_id=%s picked=%s correct=%s disclaimer_version=%s data=%r",
                user_id,
                picked,
                dcfg.correct_index,
                dcfg.version,
                data,
            )
            await query.answer(
                "Неверно. Прочитай текст выше и выбери другой вариант.",
                show_alert=True,
            )
        return
    if len(parts) >= 2 and parts[1] == "accept":
        row = await asyncio.to_thread(user_storage.get_user_consent, USER_DB_PATH, user_id)
        can = context.user_data.get(_DISCLAIMER_WAIT_ACCEPT) or user_storage.consent_awaiting_final_accept(
            row,
            dcfg.version,
        )
        if not can:
            await query.answer("Сначала ответь на вопрос.", show_alert=True)
            return
        await asyncio.to_thread(
            user_storage.set_disclaimer_accepted,
            USER_DB_PATH,
            user_id,
            dcfg.version,
        )
        context.user_data.pop(_DISCLAIMER_WAIT_ACCEPT, None)
        await query.answer("Принято")
        with suppress(BadRequest):
            await query.edit_message_reply_markup(reply_markup=None)
        await _continue_start_after_consent(context.bot, chat_id, context, user_id)
        return
    await query.answer()


async def _handle_poll_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    user_id = query.from_user.id if query.from_user else 0
    chat_id = query.message.chat_id
    parts = data.split(":")
    if len(parts) == 3 and parts[1] == "m":
        try:
            likes = int(parts[2])
        except ValueError:
            await query.answer()
            return
        if likes not in (0, 1):
            await query.answer()
            return
        context.user_data[_POLL_LIKES_MATH] = likes
        context.user_data[_POLL_AWAIT_CAREER] = True
        await query.answer("Записано")
        try:
            await query.edit_message_text(
                "<b>Опрос</b>\n\nКем хочешь стать? Напиши одним следующим сообщением (до ~2000 символов).",
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            await context.bot.send_message(
                chat_id,
                "Кем хочешь стать? Напиши одним следующим сообщением.",
            )
        return
    await query.answer()


async def _handle_check_feedback_vote(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != "cfv":
        await query.answer()
        return
    try:
        vote = int(parts[1])
    except ValueError:
        await query.answer()
        return
    if vote not in (1, -1):
        await query.answer()
        return
    msg = query.message
    if not msg:
        await query.answer()
        return
    uid = query.from_user.id if query.from_user else 0
    changed = await asyncio.to_thread(
        user_storage.upsert_check_result_vote,
        USER_DB_PATH,
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        user_id=uid,
        vote=vote,
    )
    if changed:
        label = "like" if vote == 1 else "dislike"
        await asyncio.to_thread(tgzh_metrics.record_check_feedback, vote=label)
        if vote == 1:
            context.user_data.pop(_CHECK_DISLIKE_FEEDBACK_WAIT, None)
        if vote == -1:
            context.user_data[_CHECK_DISLIKE_FEEDBACK_WAIT] = True
            try:
                await context.bot.send_message(
                    chat_id,
                    "Что не так с проверкой? Напиши <b>одним следующим сообщением</b> - сохраним как обращение "
                    "(активные обращения смотри в /my_support). Не обязательно: можно просто продолжить или /start.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("check dislike prompt send failed: %s", e)
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, uid)
    try:
        if profile is not None:
            await query.edit_message_reply_markup(
                reply_markup=get_main_keyboard(
                    uploaded=True,
                    profile=profile,
                    show_check_button=False,
                    user_id=uid,
                ),
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await query.answer("Спасибо" if changed else "Оценка уже сохранена")


def _check_result_task_condition_html(gdz_task_condition: str) -> str:
    """Фрагмент HTML: условие из ГДЗ перед телом ответа модели в «Результат проверки»."""
    t = (gdz_task_condition or "").strip()
    if not t:
        return ""
    cap = 2200
    if len(t) > cap:
        t = t[: cap - 3] + "..."
    body = telegram_format.markdownish_to_telegram_html(t)
    return f"<b>Условие задачи</b> (по учебнику, с gdz.ru):\n{body}\n\n"


async def _run_homework_text_answer_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    chat_id: int,
    profile: user_storage.UserProfile,
    answer_plain: str,
) -> None:
    """Отправка текстового ответа на POST /check как text/plain (тот же пайплайн, что и для файла)."""
    reply_mid = update.message.message_id if update.message else None
    bot = context.bot
    status_msg = await bot.send_message(
        chat_id,
        "<b>Проверяю текстовый ответ…</b>\n<i>Подождите минуту.</i>",
        reply_to_message_id=reply_mid,
        parse_mode=ParseMode.HTML,
    )
    flow_note(context, status_msg)

    para = (profile.hw_paragraph or "").strip()
    turl = (profile.textbook_url or "").strip()
    gdz_ex, gdz_vp, gdz_vw, gdz_tc = "", "", "", ""
    if para and turl:
        ex_for_gdz = (profile.hw_exercise or "").strip() or None
        meta, gdz_tc = await asyncio.to_thread(
            gdz_solution.fetch_homework_check_gdz_data,
            turl,
            para,
            ex_for_gdz,
            profile.hw_page,
        )
        gdz_ex = ",".join(str(x) for x in sorted(meta.exercise_items))
        gdz_vp = ",".join(str(x) for x in meta.verification_pages)
        gdz_vw = _format_gdz_verif_works_for_check(meta)

    form = {
        "paragraph": profile.hw_paragraph or "",
        "exercise": profile.hw_exercise or "",
        "page": str(profile.hw_page) if profile.hw_page is not None else "",
        "textbook_label": profile.textbook_label,
        "grade": str(profile.grade),
        "gdz_exercises": gdz_ex,
        "gdz_verif_pages": gdz_vp,
        "gdz_verif_works": gdz_vw,
        "gdz_task_condition": gdz_tc,
    }
    _check_url = f"{SERVER_URL.rstrip('/')}/check"
    outs: list[str] = []

    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            transport=async_http_transport_ipv4_lookup(),
        ) as client:
            t0 = time.perf_counter()
            response = await client.post(
                _check_url,
                files={
                    "photo": (
                        "answer.txt",
                        answer_plain.encode("utf-8"),
                        "text/plain; charset=utf-8",
                    ),
                },
                data=form,
            )
            elapsed = time.perf_counter() - t0
            logger.info(
                "check text user_id=%s status=%s elapsed_s=%.2f chars=%s",
                user_id,
                response.status_code,
                elapsed,
                len(answer_plain),
            )
            response.raise_for_status()
            result = response.json()
            outs.append(result.get("result", "Результат не получен."))
    except httpx.RequestError as e:
        logger.warning("check text request error user_id=%s err=%s", user_id, e)
        await asyncio.to_thread(bot_stats.record_check_technical_failed, USER_DB_PATH)
        prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert prof is not None
        await status_msg.edit_text(
            f"Ошибка связи с сервером: {_h(str(e))}",
            reply_markup=get_main_keyboard(
                uploaded=_user_has_uploaded_photo(user_id),
                profile=prof,
                user_id=user_id,
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        logger.exception("check text unexpected user_id=%s", user_id)
        await asyncio.to_thread(bot_stats.record_check_technical_failed, USER_DB_PATH)
        prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert prof is not None
        await status_msg.edit_text(
            f"Ошибка: {_h(str(e))}",
            reply_markup=get_main_keyboard(
                uploaded=_user_has_uploaded_photo(user_id),
                profile=prof,
                user_id=user_id,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    final_text = outs[0]
    await asyncio.to_thread(bot_stats.record_check_completed, USER_DB_PATH, final_text)
    summary_ok = True
    body_raw = final_text
    prefix = homework_check_status.format_check_result_prefix(final_text)

    prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    assert prof is not None
    cond_html = _check_result_task_condition_html(gdz_tc)
    suffix = homework_check_status.format_check_result_suffix_html()
    raw_plain = telegram_format.format_llm_check_reply_plain(
        homework_check_status.strip_homework_check_machine_tags(body_raw),
    )
    base_budget = len(prefix) + len(suffix) + len(cond_html) + 80
    body_cap = max(500, min(3200, 4096 - base_budget))
    if len(raw_plain) > body_cap:
        raw_plain = raw_plain[: body_cap - 3] + "..."
    body_html = telegram_format.markdownish_to_telegram_html(raw_plain)
    full_html = prefix + cond_html + body_html + suffix
    for _ in range(6):
        if len(full_html) <= 4096 or len(raw_plain) < 120:
            break
        raw_plain = raw_plain[: max(80, len(raw_plain) - max(50, len(full_html) - 4088))] + "..."
        body_html = telegram_format.markdownish_to_telegram_html(raw_plain)
        full_html = prefix + cond_html + body_html + suffix
    check_kb = get_check_result_keyboard(prof, user_id)
    try:
        await status_msg.edit_text(
            full_html,
            reply_markup=check_kb,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        logger.warning("check text result HTML parse failed: %s", e)
        await status_msg.edit_text(
            full_html[:4093] + "...",
            reply_markup=check_kb,
            parse_mode=ParseMode.HTML,
        )

    await _after_check_stickers_quip_record(
        context,
        chat_id,
        user_id,
        outs,
        body_raw,
        summary_ok,
    )


async def _run_homework_check(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    chat_id: int,
    profile: user_storage.UserProfile,
    file_ids: list[str],
) -> None:
    n_img = len(file_ids)
    await query.edit_message_text(
        "<b>Проверяю работу…</b>\n"
        f"Фото: <b>{n_img}</b>.\n"
        "<i>Скачиваю и отправляю на сервер по очереди.</i>\n"
        "Ожидайте минуту - статус \"печатает...\" означает, что бот не завис",
        parse_mode=ParseMode.HTML,
    )

    async def _do_check() -> None:
        para = (profile.hw_paragraph or "").strip()
        turl = (profile.textbook_url or "").strip()
        gdz_ex, gdz_vp, gdz_vw, gdz_tc = "", "", "", ""
        if para and turl:
            ex_for_gdz = (profile.hw_exercise or "").strip() or None
            meta, gdz_tc = await asyncio.to_thread(
                gdz_solution.fetch_homework_check_gdz_data,
                turl,
                para,
                ex_for_gdz,
                profile.hw_page,
            )
            gdz_ex = ",".join(str(x) for x in sorted(meta.exercise_items))
            gdz_vp = ",".join(str(x) for x in meta.verification_pages)
            gdz_vw = _format_gdz_verif_works_for_check(meta)
            logger.info(
                "check gdz meta user_id=%s para=%r exercises=%s verif_pages=%s verif_works=%s cond_len=%s",
                user_id,
                para[:40],
                len(meta.exercise_items),
                len(meta.verification_pages),
                len(meta.verification_works),
                len(gdz_tc),
            )
        form = {
            "paragraph": profile.hw_paragraph or "",
            "exercise": profile.hw_exercise or "",
            "page": str(profile.hw_page) if profile.hw_page is not None else "",
            "textbook_label": profile.textbook_label,
            "grade": str(profile.grade),
            "gdz_exercises": gdz_ex,
            "gdz_verif_pages": gdz_vp,
            "gdz_verif_works": gdz_vw,
            "gdz_task_condition": gdz_tc,
        }
        _check_url = f"{SERVER_URL.rstrip('/')}/check"
        _summarize_url = f"{SERVER_URL.rstrip('/')}/check/summarize"
        outs: list[str] = []

        async with httpx.AsyncClient(
            timeout=120.0,
            transport=async_http_transport_ipv4_lookup(),
        ) as client:
            for idx, file_id in enumerate(file_ids):
                file = await context.bot.get_file(file_id)
                photo_bytes = await file.download_as_bytearray()
                logger.info(
                    "check download user_id=%s idx=%s/%s telegram_file=%s raw_bytes=%s",
                    user_id,
                    idx + 1,
                    n_img,
                    file_id[:32] + "..." if len(file_id) > 32 else file_id,
                    len(photo_bytes),
                )
                try:
                    photo_jpeg = await asyncio.to_thread(
                        photo_prepare.prepare_photo_for_upload,
                        bytes(photo_bytes),
                    )
                except Exception as e:
                    logger.exception("check photo_prepare user_id=%s idx=%s", user_id, idx)
                    await asyncio.to_thread(bot_stats.record_check_technical_failed, USER_DB_PATH)
                    prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
                    assert prof is not None
                    await query.edit_message_text(
                        f"Не удалось обработать фото {idx + 1} из {n_img}: {e}\n\n"
                        "Попробуй другое изображение.",
                        reply_markup=get_main_keyboard(
                            uploaded=True,
                            profile=prof,
                            show_check_button=False,
                            user_id=user_id,
                        ),
                    )
                    return

                t0 = time.perf_counter()
                try:
                    response = await client.post(
                        _check_url,
                        files={"photo": ("photo.jpg", photo_jpeg, "image/jpeg")},
                        data=form,
                    )
                    elapsed = time.perf_counter() - t0
                    logger.info(
                        "check response user_id=%s idx=%s status=%s elapsed_s=%.2f",
                        user_id,
                        idx + 1,
                        response.status_code,
                        elapsed,
                    )
                    response.raise_for_status()
                    result = response.json()
                    outs.append(result.get("result", "Результат не получен."))
                except httpx.RequestError as e:
                    logger.warning(
                        "check request error user_id=%s idx=%s err=%s",
                        user_id,
                        idx + 1,
                        e,
                    )
                    await asyncio.to_thread(bot_stats.record_check_technical_failed, USER_DB_PATH)
                    prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
                    assert prof is not None
                    await query.edit_message_text(
                        f"Ошибка связи с сервером (фото {idx + 1} из {n_img}): {e}",
                        reply_markup=get_main_keyboard(
                            uploaded=True,
                            profile=prof,
                            show_check_button=False,
                            user_id=user_id,
                        ),
                    )
                    return
                except Exception as e:
                    logger.exception("check unexpected user_id=%s idx=%s", user_id, idx)
                    await asyncio.to_thread(bot_stats.record_check_technical_failed, USER_DB_PATH)
                    prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
                    assert prof is not None
                    await query.edit_message_text(
                        f"Ошибка: {e}",
                        reply_markup=get_main_keyboard(
                            uploaded=True,
                            profile=prof,
                            show_check_button=False,
                            user_id=user_id,
                        ),
                    )
                    return

            summary_ok = False
            final_text = ""
            prefix = ""
            body_raw = ""

            if len(outs) == 1:
                final_text = outs[0]
                await asyncio.to_thread(bot_stats.record_check_completed, USER_DB_PATH, final_text)
                prefix = homework_check_status.format_check_result_prefix(final_text)
                body_raw = final_text
            else:
                try:
                    sr = await client.post(_summarize_url, json={"parts": outs})
                    sr.raise_for_status()
                    merged = (sr.json().get("result") or "").strip()
                    bad = (
                        not merged
                        or merged.startswith("Не удалось подключиться")
                        or merged.startswith("Ошибка VLLM при сводке")
                        or merged.startswith("Ошибка при сводке")
                        or "Сводка недоступна" in merged[:120]
                    )
                    if not bad:
                        summary_ok = True
                        final_text = merged
                except Exception as e:
                    logger.warning("summarize request failed user_id=%s err=%s", user_id, e)

                if summary_ok:
                    await asyncio.to_thread(bot_stats.record_check_completed, USER_DB_PATH, final_text)
                    prefix = homework_check_status.format_check_result_prefix(final_text)
                    body_raw = final_text
                else:
                    body_raw = "\n\n".join(
                        f"**Фото {i} из {len(outs)}**\n{t}" for i, t in enumerate(outs, start=1)
                    )
                    await asyncio.to_thread(bot_stats.record_check_completed, USER_DB_PATH, body_raw)
                    prefix = homework_check_status.format_merged_check_prefix(outs)

        prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert prof is not None
        cond_html = _check_result_task_condition_html(gdz_tc)
        suffix = homework_check_status.format_check_result_suffix_html()
        raw_plain = telegram_format.format_llm_check_reply_plain(
            homework_check_status.strip_homework_check_machine_tags(body_raw),
        )
        base_budget = len(prefix) + len(suffix) + len(cond_html) + 80
        body_cap = max(500, min(3200, 4096 - base_budget))
        if len(raw_plain) > body_cap:
            raw_plain = raw_plain[: body_cap - 3] + "..."
        body_html = telegram_format.markdownish_to_telegram_html(raw_plain)
        full_html = prefix + cond_html + body_html + suffix
        for _ in range(6):
            if len(full_html) <= 4096 or len(raw_plain) < 120:
                break
            raw_plain = raw_plain[: max(80, len(raw_plain) - max(50, len(full_html) - 4088))] + "..."
            body_html = telegram_format.markdownish_to_telegram_html(raw_plain)
            full_html = prefix + cond_html + body_html + suffix
        check_kb = get_check_result_keyboard(prof, user_id)
        try:
            await query.edit_message_text(
                full_html,
                reply_markup=check_kb,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.warning("check result HTML parse failed, truncate: %s", e)
            await query.edit_message_text(
                full_html[:4093] + "...",
                reply_markup=check_kb,
                parse_mode=ParseMode.HTML,
            )

        await _after_check_stickers_quip_record(
            context,
            chat_id,
            user_id,
            outs,
            body_raw,
            summary_ok,
        )

    await run_with_typing(context.bot, chat_id, _do_check())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    user_id = query.from_user.id if query.from_user else 0
    data = query.data or ""
    chat_id = query.message.chat_id
    data_prefix = data.split(":", 1)[0] if data else ""
    logger.info(
        "callback user_id=%s prefix=%s len=%s", user_id, data_prefix, len(data)
    )

    if data == "ban:lift":
        await _handle_ban_lift(query, context)
        return

    if await _reply_if_blocked_callback(query, context):
        return

    if data.startswith("cfv:"):
        await _handle_check_feedback_vote(query, context, data)
        return

    if data.startswith("dc:"):
        await _handle_disclaimer_callback(query, context, data)
        return

    if data.startswith("poll:"):
        await _handle_poll_callback(query, context, data)
        return

    if data.startswith("fb:"):
        if not context.user_data.get(_BEGEMOT_OK):
            await query.answer("Сначала войдите: команда /begemot", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) >= 3 and parts[1] == "o":
            try:
                offset = int(parts[2])
            except ValueError:
                await query.answer()
                return
            offset = max(0, offset)
            total = await asyncio.to_thread(user_storage.count_user_feedback, USER_DB_PATH)
            rows = await asyncio.to_thread(
                user_storage.list_user_feedback,
                USER_DB_PATH,
                limit=_FEEDBACK_PAGE,
                offset=offset,
            )
            body = _format_feedback_list_html(rows)
            header = f"Отзывы: пользователей <b>{total}</b>, смещение {offset}."
            text_html = header + "\n\n" + body
            markup_rows: list[list[InlineKeyboardButton]] = []
            if rows:
                markup_rows.append(
                    [
                        InlineKeyboardButton(str(r.user_id), callback_data=f"fb:v:{r.user_id}")
                        for r in rows
                    ],
                )
            markup_rows.extend(_admin_feedback_nav_keyboard(offset, total).inline_keyboard)
            await query.answer()
            try:
                await query.edit_message_text(
                    text_html,
                    reply_markup=InlineKeyboardMarkup(markup_rows),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                await context.bot.send_message(
                    chat_id,
                    text_html,
                    reply_markup=InlineKeyboardMarkup(markup_rows),
                    parse_mode=ParseMode.HTML,
                )
            return
        if len(parts) >= 3 and parts[1] == "v":
            try:
                uid_fb = int(parts[2])
            except ValueError:
                await query.answer()
                return
            row = await asyncio.to_thread(
                user_storage.get_user_feedback_by_user_id,
                USER_DB_PATH,
                uid_fb,
            )
            await query.answer()
            if row is None:
                await context.bot.send_message(chat_id, "Запись не найдена.")
                return
            un = f"@{_h(row.username)}" if row.username else "без username"
            entries = user_storage.parse_feedback_entries(row.body)
            ids_align = await asyncio.to_thread(
                user_storage.list_feedback_ticket_ids_for_user_recent_asc,
                USER_DB_PATH,
                uid_fb,
                len(entries),
            )
            nlp_by_tid = await asyncio.to_thread(
                user_storage.get_feedback_nlp_by_ticket_ids,
                USER_DB_PATH,
                ids_align,
            )
            head = (
                f"<b>Отзывы user</b> <code>{row.user_id}</code> | {un}\n"
                f"Сообщений: {len(entries)}, с {_h(row.created_at[:19])} по {_h(row.updated_at[:19])}\n"
            )
            blocks: list[str] = []
            for i, e in enumerate(entries, 1):
                dt = _h(e.at[:19]) if e.at else "?"
                tid_i = ids_align[i - 1] if i - 1 < len(ids_align) else None
                nlp_e = nlp_by_tid.get(tid_i) if tid_i is not None else None
                badge_e = (nlp_e.badges + " ") if nlp_e and nlp_e.badges else ""
                blocks.append(f"{i}. {badge_e}<i>{dt}</i>\n{_h(e.text)}")
            detail = head + "\n\n" + "\n\n---\n\n".join(blocks)
            await _send_long_html(context.bot, chat_id, detail)
            tickets = await asyncio.to_thread(
                user_storage.list_feedback_tickets_for_admin_user,
                USER_DB_PATH,
                uid_fb,
                archived=False,
                limit=30,
            )
            n_arch = await asyncio.to_thread(
                user_storage.count_feedback_tickets_for_admin_user,
                USER_DB_PATH,
                uid_fb,
                archived=True,
            )
            if tickets:
                lines = ["<b>Тикеты на рассмотрении</b> (одно сообщение = один тикет)"]
                btn_rows: list[list[InlineKeyboardButton]] = []
                nlp_act = await asyncio.to_thread(
                    user_storage.get_feedback_nlp_by_ticket_ids,
                    USER_DB_PATH,
                    [t.id for t in tickets],
                )
                for t in tickets:
                    nb = nlp_act.get(t.id)
                    prefix = (nb.badges + " ") if nb and nb.badges else ""
                    lines.append(f"{prefix}#{t.id} ожидает {_h(t.created_at[:19])}")
                    btn_lbl = f"{nb.badges} #{t.id}" if nb and nb.badges else f"#{t.id}"
                    btn_rows.append([InlineKeyboardButton(btn_lbl, callback_data=f"fb:tk:{t.id}")])
                await context.bot.send_message(
                    chat_id,
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(btn_rows),
                    parse_mode=ParseMode.HTML,
                )
            else:
                await context.bot.send_message(
                    chat_id,
                    "Активных тикетов нет."
                    if n_arch == 0
                    else "Активных тикетов нет, рассмотренные лежат в архиве.",
                )
            if n_arch > 0:
                await context.bot.send_message(
                    chat_id,
                    f"Архив обращений: <b>{n_arch}</b> (виден только в /begemot).",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"Открыть архив ({n_arch})",
                                    callback_data=f"fb:arx:{uid_fb}",
                                ),
                            ],
                        ],
                    ),
                    parse_mode=ParseMode.HTML,
                )
            await context.bot.send_message(
                chat_id,
                f"Действия для <code>{uid_fb}</code>:",
                reply_markup=_admin_feedback_user_actions_keyboard(uid_fb, user_id),
                parse_mode=ParseMode.HTML,
            )
            return
        if len(parts) >= 3 and parts[1] == "arx":
            try:
                uid_arx = int(parts[2])
            except ValueError:
                await query.answer()
                return
            await query.answer()
            arch_tickets = await asyncio.to_thread(
                user_storage.list_feedback_tickets_for_admin_user,
                USER_DB_PATH,
                uid_arx,
                archived=True,
                limit=50,
            )
            if not arch_tickets:
                await context.bot.send_message(chat_id, "В архиве пусто.")
                return
            lines = [f"<b>Архив тикетов</b> user <code>{uid_arx}</code>"]
            btn_rows_arx: list[list[InlineKeyboardButton]] = []
            nlp_arx = await asyncio.to_thread(
                user_storage.get_feedback_nlp_by_ticket_ids,
                USER_DB_PATH,
                [t.id for t in arch_tickets],
            )
            for t in arch_tickets:
                st_short = (
                    "ок"
                    if t.status == user_storage.FEEDBACK_TICKET_STATUS_REVIEWED
                    else "откл"
                )
                nb = nlp_arx.get(t.id)
                prefix = (nb.badges + " ") if nb and nb.badges else ""
                lines.append(f"{prefix}#{t.id} {_h(st_short)} {_h(t.created_at[:19])}")
                btn_lbl_arx = f"{nb.badges} #{t.id}" if nb and nb.badges else f"#{t.id}"
                btn_rows_arx.append(
                    [InlineKeyboardButton(btn_lbl_arx, callback_data=f"fb:tk:{t.id}")],
                )
            await context.bot.send_message(
                chat_id,
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(btn_rows_arx),
                parse_mode=ParseMode.HTML,
            )
            return
        if len(parts) >= 3 and parts[1] == "tk":
            try:
                tid = int(parts[2])
            except ValueError:
                await query.answer()
                return
            tk = await asyncio.to_thread(user_storage.get_feedback_ticket_by_id, USER_DB_PATH, tid)
            await query.answer()
            if tk is None:
                await context.bot.send_message(chat_id, "Тикет не найден.")
                return
            st = tk.status
            raw_body = tk.body
            body_show = _h(raw_body) if len(raw_body) <= 3500 else _h(raw_body[:3500]) + "…"
            arch_lbl = " <i>(архив)</i>" if tk.archived else ""
            hdr = (
                f"<b>Тикет #{tk.id}</b> user <code>{tk.user_id}</code>{arch_lbl}\n"
                f"Статус: <code>{_h(st)}</code>\n\n{body_show}"
            )
            if tk.archived and (tk.staff_response or "").strip():
                hdr += (
                    "\n\n<b>Ответ админа (в архиве):</b>\n"
                    f"{_h((tk.staff_response or '').strip())}"
                )
            nlp_one = await asyncio.to_thread(
                user_storage.get_feedback_nlp_by_ticket_ids,
                USER_DB_PATH,
                [tid],
            )
            nlp_row = nlp_one.get(tid)
            if nlp_row is not None:
                hdr += "\n\n" + _format_feedback_nlp_detail_html(nlp_row)
            rows_bt: list[list[InlineKeyboardButton]] = []
            if st == user_storage.FEEDBACK_TICKET_STATUS_PENDING:
                rows_bt = [
                    [
                        InlineKeyboardButton("Рассмотрено", callback_data=f"fb:rs:{tid}:r"),
                        InlineKeyboardButton("Отклонено", callback_data=f"fb:rs:{tid}:x"),
                    ],
                ]
            await context.bot.send_message(
                chat_id,
                hdr,
                reply_markup=InlineKeyboardMarkup(rows_bt) if rows_bt else None,
                parse_mode=ParseMode.HTML,
            )
            return
        if len(parts) >= 4 and parts[1] == "rs":
            try:
                tid = int(parts[2])
            except ValueError:
                await query.answer()
                return
            mode = parts[3]
            if mode not in ("r", "x"):
                await query.answer()
                return
            tk = await asyncio.to_thread(user_storage.get_feedback_ticket_by_id, USER_DB_PATH, tid)
            await query.answer()
            if tk is None:
                await context.bot.send_message(chat_id, "Тикет не найден.")
                return
            if tk.status != user_storage.FEEDBACK_TICKET_STATUS_PENDING:
                await context.bot.send_message(chat_id, "Тикет уже обработан.")
                return
            kind = "review" if mode == "r" else "reject"
            context.user_data[_FEEDBACK_STAFF_WAIT] = {"ticket_id": tid, "kind": kind}
            if kind == "review":
                msg = (
                    "Напиши <b>комментарий для пользователя</b> одним сообщением "
                    "(он придет ему в чат с ботом; в /my_support останутся только активные обращения)."
                )
            else:
                msg = (
                    "Напиши <b>причину отклонения</b> одним сообщением "
                    "(она придет пользователю в чат с ботом)."
                )
            await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
            return
        if len(parts) >= 3 and parts[1] == "bt":
            try:
                tuid = int(parts[2])
            except ValueError:
                await query.answer("Неверный id", show_alert=True)
                return
            if tuid == user_id:
                await query.answer("Нельзя заблокировать свой аккаунт.", show_alert=True)
                return
            await query.answer()
            context.user_data.pop(_FEEDBACK_STAFF_WAIT, None)
            context.user_data[_ADMIN_BAN_WAIT] = {"target_id": tuid}
            await context.bot.send_message(
                chat_id,
                "Напиши <b>причину временного блока на 1 час</b> одним сообщением "
                "(ее увидит пользователь).",
                parse_mode=ParseMode.HTML,
            )
            return
        if len(parts) >= 3 and parts[1] == "bp":
            try:
                tuid = int(parts[2])
            except ValueError:
                await query.answer("Неверный id", show_alert=True)
                return
            if tuid == user_id:
                await query.answer("Нельзя забанить свой аккаунт.", show_alert=True)
                return
            await query.answer()
            await asyncio.to_thread(user_storage.set_permanent_user_block, USER_DB_PATH, tuid)
            try:
                await context.bot.send_message(
                    tuid,
                    "<b>Доступ к боту ограничен без срока.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.warning("permanent ban notify failed target_id=%s", tuid, exc_info=True)
            await context.bot.send_message(
                chat_id,
                f"Пожизненный бан для <code>{tuid}</code> записан.",
                parse_mode=ParseMode.HTML,
            )
            return
        if len(parts) >= 3 and parts[1] == "bu":
            try:
                tuid = int(parts[2])
            except ValueError:
                await query.answer("Неверный id", show_alert=True)
                return
            await query.answer()
            await asyncio.to_thread(user_storage.clear_user_block, USER_DB_PATH, tuid)
            try:
                await context.bot.send_message(
                    tuid,
                    "Ограничение доступа снято. Можно снова пользоваться ботом.",
                )
            except Exception:
                logger.warning("unban notify failed target_id=%s", tuid, exc_info=True)
            await context.bot.send_message(
                chat_id,
                f"Разбан для <code>{tuid}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if len(parts) >= 3 and parts[1] == "th":
            try:
                tuid = int(parts[2])
            except ValueError:
                await query.answer("Неверный id", show_alert=True)
                return
            await query.answer("Отправлено")
            try:
                await context.bot.send_message(
                    tuid,
                    "Спасибо за обратную связь. Мы рассмотрели ваше обращение.",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id,
                    "Не удалось доставить сообщение пользователю.",
                )
            return
        await query.answer()
        return

    await query.answer()

    if data == "stats":
        await _send_stats_message(context.bot, chat_id, user_id, context)
        return

    if data == "chg_tb":
        context.user_data.pop(_HW_STEP, None)
        context.user_data.pop("hw_paragraph_draft", None)
        context.user_data.pop(_HW_PAGE_BUF, None)
        context.user_data.pop(_HW_PAR_BTN_MAX, None)
        _pop_step2_gdz_meta(context)
        _clear_begemot_session(context)
        await flow_purge_except(
            context.bot,
            chat_id,
            context,
            keep_message_id=query.message.message_id,
        )
        await query.edit_message_text(
            "Выбери класс:",
            reply_markup=grade_keyboard(back_to_main=False),
        )
        return

    if data == "back_main":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text("Выбери класс:", reply_markup=grade_keyboard(back_to_main=False))
            return
        has_photo = _user_has_uploaded_photo(user_id)
        await query.edit_message_text(
            _main_menu_caption(profile),
            reply_markup=get_main_keyboard(uploaded=has_photo, profile=profile, user_id=user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "back_hw:p" and context.user_data.get(_HW_STEP) == _HW_PARAGRAPH_MANUAL:
        context.user_data[_HW_STEP] = _HW_PARAGRAPH_PICK
        pn = _paragraph_n_from_context(context)
        await query.edit_message_text(
            _paragraph_prompt_caption(pn),
            reply_markup=paragraph_choice_keyboard(pn),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "back_hw:p":
        context.user_data.pop(_HW_STEP, None)
        context.user_data.pop("hw_paragraph_draft", None)
        context.user_data.pop(_HW_PAGE_BUF, None)
        context.user_data.pop(_HW_PAR_BTN_MAX, None)
        _pop_step2_gdz_meta(context)
        entry = context.user_data.get("hw_entry", "main")
        if entry == "textbook":
            grade = context.user_data.get("hw_tb_grade")
            page = int(context.user_data.get("hw_tb_page") or 0)
            if isinstance(grade, int) and CATALOG.get(str(grade)):
                slug_counts = await asyncio.to_thread(
                    user_storage.textbook_popularity_by_grade,
                    USER_DB_PATH,
                    grade,
                )
                await query.edit_message_text(
                    textbook_caption(grade, page),
                    reply_markup=textbook_keyboard(grade, page, slug_counts),
                )
            else:
                await query.edit_message_text(
                    "Выбери класс:",
                    reply_markup=grade_keyboard(back_to_main=False),
                )
            return
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text("Выбери класс:", reply_markup=grade_keyboard(back_to_main=False))
            return
        has_photo = _user_has_uploaded_photo(user_id)
        await query.edit_message_text(
            _main_menu_caption(profile),
            reply_markup=get_main_keyboard(uploaded=has_photo, profile=profile, user_id=user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("hw_pf:"):
        if context.user_data.get(_HW_STEP) != "exercise_page":
            return
        tag = data[6:]
        if not tag.isdigit():
            return
        page_num = int(tag)
        pages = context.user_data.get(_HW_VERIF_PAGES) or []
        if page_num not in pages:
            return
        paragraph = context.user_data.get("hw_paragraph_draft") or ""
        if not paragraph.strip():
            return
        await asyncio.to_thread(
            user_storage.set_homework_meta,
            USER_DB_PATH,
            user_id,
            paragraph,
            None,
            page_num,
        )
        context.user_data.pop(_HW_STEP, None)
        context.user_data.pop("hw_paragraph_draft", None)
        context.user_data.pop(_HW_PAGE_BUF, None)
        context.user_data.pop(_HW_PAR_BTN_MAX, None)
        _pop_step2_gdz_meta(context)
        prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert prof is not None
        logger.info("hw saved verif_btn user_id=%s summary=%r", user_id, _hw_summary(prof))
        await query.edit_message_text(
            _saved_homework_title_html(prof),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
        await run_with_typing(
            context.bot,
            chat_id,
            _send_gdz_task_condition_to_chat(context.bot, chat_id, prof, user_id, context),
        )
        await context.bot.send_message(
            chat_id,
            _SAVED_HW_FOLLOWUP_HTML,
            reply_markup=get_main_keyboard(uploaded=False, profile=prof, user_id=user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("hw_p:"):
        tag = data[5:]
        if tag == "custom":
            context.user_data[_HW_STEP] = _HW_PARAGRAPH_MANUAL
            await query.edit_message_text(
                "Введи номер параграфа текстом (как в учебнике или на gdz.ru), одним сообщением.\n"
                "Например: 12 или 2.1",
                reply_markup=paragraph_step_keyboard(),
            )
            return
        if tag.isdigit():
            n = int(tag)
            mx = _paragraph_n_from_context(context)
            if 1 <= n <= mx:
                context.user_data["hw_paragraph_draft"] = str(n)
                context.user_data[_HW_STEP] = "exercise_page"
                profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
                url = (profile.textbook_url if profile else "").strip()

                async def _go_step2() -> None:
                    await _prepare_step2_after_paragraph(context, url, str(n))
                    await query.edit_message_text(
                        _step2_hw_caption(context),
                        reply_markup=exercise_step_keyboard_from_context(context),
                        parse_mode=ParseMode.HTML,
                    )

                await run_with_typing(context.bot, chat_id, _go_step2())
                return
        logger.warning("unexpected hw_p callback data=%s", data)
        return

    if data == "back_hw:e":
        context.user_data[_HW_STEP] = _HW_PARAGRAPH_PICK
        context.user_data.pop("hw_paragraph_draft", None)
        context.user_data.pop(_HW_PAGE_BUF, None)
        _pop_step2_gdz_meta(context)
        pn = _paragraph_n_from_context(context)
        await query.edit_message_text(
            _paragraph_prompt_caption(pn),
            reply_markup=paragraph_choice_keyboard(pn),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "back_hw:vf":
        if context.user_data.get(_HW_STEP) != "exercise_page":
            await query.answer()
            return
        if not context.user_data.get(_HW_VERIF_SUBSCREEN):
            await query.answer()
            return
        context.user_data.pop(_HW_VERIF_SUBSCREEN, None)
        await query.edit_message_text(
            _step2_hw_caption(context),
            reply_markup=exercise_step_keyboard_from_context(context),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "hw_verif_open":
        if context.user_data.get(_HW_STEP) != "exercise_page":
            await query.answer()
            return
        pages = context.user_data.get(_HW_VERIF_PAGES) or []
        if not pages:
            await query.answer(
                "Для этого параграфа нет страниц проверочных в оглавлении.",
                show_alert=True,
            )
            return
        context.user_data[_HW_VERIF_SUBSCREEN] = True
        await query.edit_message_text(
            _step2_hw_caption(context),
            reply_markup=exercise_step_keyboard_from_context(context),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "back_hw:pk":
        if context.user_data.get(_HW_STEP) != _HW_PAGE_KEYPAD:
            await query.answer()
            return
        from_verif = context.user_data.pop(_HW_PAGE_FROM_VERIF, False)
        context.user_data[_HW_STEP] = "exercise_page"
        context.user_data.pop(_HW_PAGE_BUF, None)
        if from_verif:
            context.user_data[_HW_VERIF_SUBSCREEN] = True
        else:
            context.user_data.pop(_HW_VERIF_SUBSCREEN, None)
        await query.edit_message_text(
            _step2_hw_caption(context),
            reply_markup=exercise_step_keyboard_from_context(context),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "hw_page_keypad":
        if context.user_data.get(_HW_STEP) != "exercise_page":
            await query.answer("Сначала открой шаг 2 после выбора параграфа.", show_alert=True)
            return
        paragraph = context.user_data.get("hw_paragraph_draft") or ""
        if not paragraph.strip():
            await query.answer("Сначала выбери параграф.", show_alert=True)
            return
        context.user_data[_HW_PAGE_FROM_VERIF] = bool(context.user_data.get(_HW_VERIF_SUBSCREEN))
        context.user_data[_HW_STEP] = _HW_PAGE_KEYPAD
        context.user_data[_HW_PAGE_BUF] = ""
        await query.edit_message_text(
            page_keypad_caption(""),
            reply_markup=page_keypad_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "hw_ex_keypad":
        if context.user_data.get(_HW_STEP) != "exercise_page":
            return
        paragraph = context.user_data.get("hw_paragraph_draft") or ""
        if not paragraph.strip():
            return
        context.user_data.pop(_HW_VERIF_SUBSCREEN, None)
        context.user_data[_HW_STEP] = _HW_EX_KEYPAD
        context.user_data[_HW_EX_BUF] = ""
        valid = _exercise_valid_from_context(context)
        await query.edit_message_text(
            exercise_keypad_caption("", valid),
            reply_markup=exercise_keypad_keyboard("", valid),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("exd:") and len(data) == 5 and data[4].isdigit():
        if context.user_data.get(_HW_STEP) != _HW_EX_KEYPAD:
            return
        digit = data[4]
        valid = _exercise_valid_from_context(context)
        buf = context.user_data.get(_HW_EX_BUF) or ""
        if digit not in _allowed_exercise_next_digits(buf, valid):
            return
        mx = _max_exercise_input_len(valid)
        if len(buf) >= mx:
            return
        buf = buf + digit
        context.user_data[_HW_EX_BUF] = buf
        try:
            await query.edit_message_text(
                exercise_keypad_caption(buf, valid),
                reply_markup=exercise_keypad_keyboard(buf, valid),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.debug("exercise_keypad edit: %s", e)
        return

    if data == "exk:bs":
        if context.user_data.get(_HW_STEP) != _HW_EX_KEYPAD:
            return
        valid = _exercise_valid_from_context(context)
        buf = context.user_data.get(_HW_EX_BUF) or ""
        buf = buf[:-1]
        context.user_data[_HW_EX_BUF] = buf
        try:
            await query.edit_message_text(
                exercise_keypad_caption(buf, valid),
                reply_markup=exercise_keypad_keyboard(buf, valid),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.debug("exercise_keypad edit: %s", e)
        return

    if data == "exk:ok":
        if context.user_data.get(_HW_STEP) != _HW_EX_KEYPAD:
            return
        valid = _exercise_valid_from_context(context)
        buf = (context.user_data.get(_HW_EX_BUF) or "").strip()
        if not buf:
            await query.answer("Набери номер цифрами, затем «Готово».", show_alert=True)
            return
        if not _can_confirm_exercise(buf, valid):
            await query.answer(
                "Такого номера упражнения нет в оглавлении этого параграфа.",
                show_alert=True,
            )
            return
        n = int(buf)
        paragraph = context.user_data.get("hw_paragraph_draft") or ""
        if not paragraph.strip():
            return
        await asyncio.to_thread(
            user_storage.set_homework_meta,
            USER_DB_PATH,
            user_id,
            paragraph,
            str(n),
            None,
        )
        context.user_data.pop(_HW_STEP, None)
        context.user_data.pop("hw_paragraph_draft", None)
        context.user_data.pop(_HW_PAGE_BUF, None)
        context.user_data.pop(_HW_PAR_BTN_MAX, None)
        _pop_step2_gdz_meta(context)
        prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert prof is not None
        logger.info("hw saved exercise_keypad user_id=%s summary=%r", user_id, _hw_summary(prof))
        await query.edit_message_text(
            _saved_homework_title_html(prof),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
        await run_with_typing(
            context.bot,
            chat_id,
            _send_gdz_task_condition_to_chat(context.bot, chat_id, prof, user_id, context),
        )
        await context.bot.send_message(
            chat_id,
            _SAVED_HW_FOLLOWUP_HTML,
            reply_markup=get_main_keyboard(uploaded=False, profile=prof, user_id=user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "back_hw:xk":
        if context.user_data.get(_HW_STEP) != _HW_EX_KEYPAD:
            return
        context.user_data[_HW_STEP] = "exercise_page"
        context.user_data.pop(_HW_EX_BUF, None)
        await query.edit_message_text(
            _step2_hw_caption(context),
            reply_markup=exercise_step_keyboard_from_context(context),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("pgd:") and len(data) == 5 and data[4].isdigit():
        if context.user_data.get(_HW_STEP) != _HW_PAGE_KEYPAD:
            return
        digit = data[4]
        buf = context.user_data.get(_HW_PAGE_BUF) or ""
        if len(buf) >= 3:
            await query.answer("Не больше трёх цифр", show_alert=True)
            return
        buf = buf + digit
        context.user_data[_HW_PAGE_BUF] = buf
        try:
            await query.edit_message_text(
                page_keypad_caption(buf),
                reply_markup=page_keypad_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.debug("page_keypad edit: %s", e)
        return

    if data == "pgk:bs":
        if context.user_data.get(_HW_STEP) != _HW_PAGE_KEYPAD:
            return
        buf = context.user_data.get(_HW_PAGE_BUF) or ""
        buf = buf[:-1]
        context.user_data[_HW_PAGE_BUF] = buf
        try:
            await query.edit_message_text(
                page_keypad_caption(buf),
                reply_markup=page_keypad_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.debug("page_keypad edit: %s", e)
        return

    if data == "pgk:ok":
        if context.user_data.get(_HW_STEP) != _HW_PAGE_KEYPAD:
            return
        buf = (context.user_data.get(_HW_PAGE_BUF) or "").strip()
        if not buf:
            await query.answer("Набери хотя бы одну цифру", show_alert=True)
            return
        try:
            page_num = int(buf)
        except ValueError:
            await query.answer("Неверный номер", show_alert=True)
            return
        if not (1 <= page_num <= 999):
            await query.answer("Номер страницы от 1 до 999", show_alert=True)
            return
        paragraph = context.user_data.get("hw_paragraph_draft") or ""
        if not paragraph.strip():
            await query.answer("Параграф потерян. Начни с шага 1.", show_alert=True)
            context.user_data[_HW_STEP] = _HW_PARAGRAPH_PICK
            _pop_step2_gdz_meta(context)
            return
        await asyncio.to_thread(
            user_storage.set_homework_meta,
            USER_DB_PATH,
            user_id,
            paragraph,
            None,
            page_num,
        )
        context.user_data.pop(_HW_STEP, None)
        context.user_data.pop("hw_paragraph_draft", None)
        context.user_data.pop(_HW_PAGE_BUF, None)
        context.user_data.pop(_HW_PAR_BTN_MAX, None)
        _pop_step2_gdz_meta(context)
        prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert prof is not None
        logger.info("hw saved page_keypad user_id=%s summary=%r", user_id, _hw_summary(prof))
        await query.edit_message_text(
            _saved_homework_title_html(prof),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
        await run_with_typing(
            context.bot,
            chat_id,
            _send_gdz_task_condition_to_chat(context.bot, chat_id, prof, user_id, context),
        )
        await context.bot.send_message(
            chat_id,
            _SAVED_HW_FOLLOWUP_HTML,
            reply_markup=get_main_keyboard(uploaded=False, profile=prof, user_id=user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "start_hw":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text(
                "Сначала выбери учебник: /start",
                reply_markup=grade_keyboard(back_to_main=False),
            )
            return
        context.user_data["hw_entry"] = "main"
        await query.edit_message_reply_markup(reply_markup=None)
        await run_with_typing(
            context.bot,
            chat_id,
            _send_paragraph_prompt(context.bot, chat_id, context, user_id),
        )
        return

    if data == "show_sol":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.answer("Сначала выбери учебник", show_alert=True)
            return
        if not user_storage.homework_complete(profile):
            await query.answer(
                "Сначала укажи параграф и упражнение или страницу",
                show_alert=True,
            )
            return
        await query.answer("Ищу решение… Ответ в чате, внизу — «печатает»")
        await _send_gdz_solution_to_chat(context.bot, chat_id, profile, user_id, context)
        return

    if data == "chg_hw":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text("Сначала выбери учебник: /start", reply_markup=grade_keyboard(back_to_main=False))
            return
        context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
        await asyncio.to_thread(user_storage.clear_homework_meta, USER_DB_PATH, user_id)
        _clear_user_photos(user_id)
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        assert profile is not None
        context.user_data["hw_entry"] = "main"
        await query.edit_message_text(
            "Привязка к заданию сброшена. Введи данные заново.",
            reply_markup=None,
        )
        await run_with_typing(
            context.bot,
            chat_id,
            _send_paragraph_prompt(context.bot, chat_id, context, user_id),
        )
        return

    if data == "back_from_upload":
        context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text(
                "Сначала выбери класс и учебник: команда /start",
                reply_markup=grade_keyboard(back_to_main=False),
            )
            return
        has_photo = _user_has_uploaded_photo(user_id)
        await query.edit_message_text(
            _upload_return_caption_html(profile),
            reply_markup=get_main_keyboard(uploaded=has_photo, profile=profile, user_id=user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("g:"):
        parts = data.split(":")
        if len(parts) == 2 and parts[1] in ("6", "7", "8", "9", "10", "11"):
            grade = int(parts[1])
            if not CATALOG.get(str(grade)):
                await query.edit_message_text(
                    f"Для класса {grade} нет учебников в каталоге. Обнови data/gdz_matematika_textbooks.json",
                    reply_markup=grade_keyboard(back_to_main=False),
                )
                return
            slug_counts = await asyncio.to_thread(
                user_storage.textbook_popularity_by_grade,
                USER_DB_PATH,
                grade,
            )
            await query.edit_message_text(
                textbook_caption(grade, 0),
                reply_markup=textbook_keyboard(grade, 0, slug_counts),
            )
        return

    if data.startswith("pg:"):
        parts = data.split(":")
        if len(parts) == 3:
            grade, page = int(parts[1]), int(parts[2])
            slug_counts = await asyncio.to_thread(
                user_storage.textbook_popularity_by_grade,
                USER_DB_PATH,
                grade,
            )
            await query.edit_message_text(
                textbook_caption(grade, page),
                reply_markup=textbook_keyboard(grade, page, slug_counts),
            )
        return

    if data.startswith("tb:"):
        parts = data.split(":")
        if len(parts) == 3:
            grade, idx = int(parts[1]), int(parts[2])
            books = CATALOG.get(str(grade), [])
            if idx < 0 or idx >= len(books):
                await query.edit_message_text(
                    "Неверный выбор. Начни с /start",
                    reply_markup=grade_keyboard(back_to_main=False),
                )
                return
            book = books[idx]
            slug = str(book.get("slug", ""))
            url = str(book.get("url", ""))
            label = str(book.get("label", slug))
            premium = bool(book.get("is_premium"))
            context.user_data.pop(_HW_STEP, None)
            context.user_data.pop("hw_paragraph_draft", None)
            context.user_data.pop(_HW_PAGE_BUF, None)
            context.user_data.pop(_HW_PAR_BTN_MAX, None)
            _pop_step2_gdz_meta(context)
            _clear_user_photos(user_id)

            prev_prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
            subj = prev_prof.subject_slug if prev_prof else "matematika"

            async def _after_textbook_pick() -> None:
                await asyncio.to_thread(
                    user_storage.set_textbook,
                    USER_DB_PATH,
                    user_id,
                    grade,
                    slug,
                    url,
                    label,
                    premium,
                    subj,
                )
                context.user_data["hw_entry"] = "textbook"
                context.user_data["hw_tb_grade"] = grade
                context.user_data["hw_tb_page"] = idx // PAGE_SIZE
                prem = " (Премиум)" if premium else ""
                prof = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
                assert prof is not None
                await query.edit_message_text(
                    _textbook_pick_saved_html(label, slug, grade, prem),
                    reply_markup=None,
                    parse_mode=ParseMode.HTML,
                )
                await _send_paragraph_prompt(context.bot, chat_id, context, user_id)

            await run_with_typing(context.bot, chat_id, _after_textbook_pick())
        return

    if data == "upload":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text(
                "Сначала выбери класс и учебник: команда /start",
                reply_markup=grade_keyboard(back_to_main=False),
            )
            return
        if not user_storage.homework_complete(profile):
            await query.edit_message_text(
                "Сначала укажи параграф и упражнение или страницу проверочной — кнопка «Указать задание».",
                reply_markup=get_main_keyboard(uploaded=False, profile=profile, user_id=user_id),
            )
            return
        context.user_data.pop(_AWAIT_TEXT_ANSWER, None)
        await query.edit_message_text(
            "<b>Отправь фото тетрадного листа</b> с домашним заданием, нажав кнопку скрепки 📎 в поле ввода.",
            reply_markup=upload_prompt_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "answer_text":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text(
                "Сначала выбери класс и учебник: команда /start",
                reply_markup=grade_keyboard(back_to_main=False),
            )
            return
        if not user_storage.homework_complete(profile):
            await query.edit_message_text(
                "Сначала укажи параграф и упражнение или страницу проверочной — кнопка «Указать задание».",
                reply_markup=get_main_keyboard(uploaded=False, profile=profile, user_id=user_id),
            )
            return
        _clear_user_photos(user_id)
        context.user_data[_AWAIT_TEXT_ANSWER] = True
        gdz_tc = ""
        turl = (profile.textbook_url or "").strip()
        para = (profile.hw_paragraph or "").strip()
        if turl and para:
            ex_for = (profile.hw_exercise or "").strip() or None
            pg = profile.hw_page
            if ex_for is not None or pg is not None:
                _meta, gdz_tc = await asyncio.to_thread(
                    gdz_solution.fetch_homework_check_gdz_data,
                    turl,
                    para,
                    ex_for,
                    pg,
                )
        head = _format_text_answer_prompt_html((gdz_tc or "").strip())
        tail = (
            "<b>Напиши решение или ход задачи одним сообщением</b> "
            "(можно несколько абзацев). После отправки текст уйдет на проверку.\n\n"
            '<i>Отмена - кнопка "Назад" ниже.</i>'
        )
        full = head + tail
        if len(full) > 4096:
            full = full[:4093] + "..."
        await query.edit_message_text(
            full,
            reply_markup=upload_prompt_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "check":
        profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
        if profile is None:
            await query.edit_message_text(
                "Сначала выбери учебник: /start",
                reply_markup=grade_keyboard(back_to_main=False),
            )
            return
        if not user_storage.homework_complete(profile):
            await query.edit_message_text(
                "Сначала укажи задание — кнопка «Указать задание».",
                reply_markup=get_main_keyboard(uploaded=False, profile=profile, user_id=user_id),
            )
            return
        file_ids = _user_photo_file_ids(user_id)
        if not file_ids:
            await query.edit_message_text(
                "Сначала загрузи фото. Нажми «Загрузить фото» и отправь изображение.",
                reply_markup=get_main_keyboard(uploaded=False, profile=profile, user_id=user_id),
            )
            return

        await _run_homework_check(
            query,
            context,
            user_id=user_id,
            chat_id=chat_id,
            profile=profile,
            file_ids=file_ids,
        )
        return


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if not (
        context.user_data.get(_BEGEMOT_OK) or context.user_data.get(_BEGEMOT_PW_WAIT)
    ):
        st_ph, ok_blk = await _safe_blocked_state(user_id)
        if not ok_blk:
            flow_note(
                context,
                await update.message.reply_text(_BLOCKED_STATE_DB_ERROR_HTML),
            )
            return
        if st_ph is not None:
            flow_note(
                context,
                await update.message.reply_text(
                    _blocked_user_message_html(st_ph),
                    reply_markup=_blocked_user_reply_markup(st_ph),
                    parse_mode=ParseMode.HTML,
                ),
            )
            return
    step_photo = context.user_data.get(_HW_STEP)
    if step_photo:
        hint = (
            "Сейчас жду набор номера страницы кнопками под сообщением или нажми «Назад»."
            if step_photo == _HW_PAGE_KEYPAD
            else "Сейчас жду текст (параграф или упражнение / страница). Допиши шаги или нажми /start"
        )
        flow_note(
            context,
            await update.message.reply_text(hint),
        )
        return
    profile = await asyncio.to_thread(user_storage.get_profile, USER_DB_PATH, user_id)
    if profile is None:
        flow_note(
            context,
            await update.message.reply_text(
                "Сначала выбери класс и учебник: команда /start",
                reply_markup=grade_keyboard(back_to_main=False),
            ),
        )
        return
    if not user_storage.homework_complete(profile):
        flow_note(
            context,
            await update.message.reply_text(
                "Сначала укажи параграф и упражнение или страницу — кнопка «Указать задание».",
                reply_markup=get_main_keyboard(uploaded=False, profile=profile, user_id=user_id),
            ),
        )
        return

    context.user_data.pop(_AWAIT_TEXT_ANSWER, None)

    photo = update.message.photo[-1]
    msg = update.message
    chat_id = msg.chat_id
    media_gid = msg.media_group_id

    logger.info(
        "photo received user_id=%s media_group_id=%s file_id=%s size_hint=%s",
        user_id,
        media_gid,
        photo.file_id[:24] + "..." if len(photo.file_id) > 24 else photo.file_id,
        photo.file_size,
    )

    if media_gid is not None:
        gid_key = str(media_gid)
        if context.user_data.get(_PHOTO_BATCH_GID) != gid_key:
            _photo_batch_clear_all(context)
            context.user_data[_PHOTO_BATCH_GID] = gid_key
            context.user_data[_PHOTO_BATCH_ENTRIES] = []
        entries: list[tuple[int, str]] = context.user_data[_PHOTO_BATCH_ENTRIES]
        entries.append((msg.message_id, photo.file_id))
        _cancel_photo_batch_task(context)
        context.user_data[_PHOTO_BATCH_TASK] = asyncio.create_task(
            _photo_batch_flush_delayed(
                context,
                chat_id,
                user_id,
                _PHOTO_DEBOUNCE_ALBUM_SEC,
            ),
        )
        return

    _photo_batch_clear_all(context)
    _store_photo_batch(user_id, [photo.file_id])
    await asyncio.to_thread(bot_stats.record_photo_uploaded, USER_DB_PATH)
    flow_note(
        context,
        await msg.reply_text(
            "Фото получено.\n\n<b>Нажми «Проверить»</b>, чтобы отправить на проверку.",
            reply_markup=get_main_keyboard(uploaded=True, profile=profile, user_id=user_id),
            reply_to_message_id=msg.message_id,
            parse_mode=ParseMode.HTML,
        ),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    tgzh_metrics.record_bot_handler_error()
    logger.error("unhandled handler error", exc_info=context.error)


def _plain_chunks(s: str, limit: int = 4000) -> list[str]:
    t = (s or "").strip()
    if not t:
        return ["(пустой ответ)"]
    if len(t) <= limit:
        return [t]
    return [t[i : i + limit] for i in range(0, len(t), limit)]


async def cursor_ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    logger.info("cmd /cursorask user_id=%s", uid)
    if await _reply_if_blocked_cmd(update, context):
        return
    if not await _disclaimer_consent_ok(update, context):
        return
    if not cursor_cli_client.cli_configured():
        await update.message.reply_text(
            "Cursor CLI недоступен: нет исполняемого <code>agent</code> в PATH "
            "(или задайте <code>CURSOR_CLI_BIN</code>). Установка: "
            "<code>curl https://cursor.com/install -fsS | bash</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    allow = _cursor_ask_allowlist_ids()
    if not allow or uid not in allow:
        await update.message.reply_text(
            "Команда доступна только для user id из списка CURSOR_ASK_ALLOW_TELEGRAM_IDS.",
        )
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Использование: <code>/cursorask</code> текст\n"
            "Или одно фото с подписью <code>/cursorask</code> текст",
            parse_mode=ParseMode.HTML,
        )
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        out = await cursor_cli_client.run_cursor_agent(text, None)
    except Exception as e:
        logger.exception("cursor_ask failed user_id=%s", uid)
        await update.message.reply_text(f"Ошибка Cursor CLI: {_h(repr(e))}", parse_mode=ParseMode.HTML)
        return
    for part in _plain_chunks(out):
        await update.message.reply_text(_h(part), parse_mode=ParseMode.HTML)


async def cursor_ask_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.photo:
        return
    uid = update.effective_user.id
    logger.info("cmd /cursorask (photo) user_id=%s", uid)
    if await _reply_if_blocked_cmd(update, context):
        return
    if not await _disclaimer_consent_ok(update, context):
        return
    if not cursor_cli_client.cli_configured():
        await update.message.reply_text(
            "Cursor CLI недоступен: нет исполняемого <code>agent</code> в PATH "
            "(или задайте <code>CURSOR_CLI_BIN</code>). Установка: "
            "<code>curl https://cursor.com/install -fsS | bash</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    allow = _cursor_ask_allowlist_ids()
    if not allow or uid not in allow:
        await update.message.reply_text(
            "Команда доступна только для user id из списка CURSOR_ASK_ALLOW_TELEGRAM_IDS.",
        )
        return
    if update.message.media_group_id is not None:
        await update.message.reply_text(
            "Для этой команды пришли одно фото, не альбом.",
        )
        return
    cap = update.message.caption or ""
    m = re.match(r"^/cursorask(?:@\S*)?\s*(.*)$", cap, flags=re.DOTALL)
    prompt = (m.group(1) if m else "").strip()
    if not prompt:
        await update.message.reply_text(
            "В подписи к фото укажи текст после <code>/cursorask</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    photo = update.message.photo[-1]
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        file = await context.bot.get_file(photo.file_id)
        raw = await file.download_as_bytearray()
    except Exception as e:
        logger.exception("cursorask photo download user_id=%s", uid)
        await update.message.reply_text(f"Не удалось скачать фото: {_h(repr(e))}", parse_mode=ParseMode.HTML)
        return
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", prefix="tgzh_cursor_", delete=False
        ) as f:
            f.write(bytes(raw))
            tmp_path = f.name
        out = await cursor_cli_client.run_cursor_agent(prompt, tmp_path)
    except Exception as e:
        logger.exception("cursor_ask photo failed user_id=%s", uid)
        await update.message.reply_text(
            f"Ошибка Cursor CLI: {_h(repr(e))}", parse_mode=ParseMode.HTML
        )
        return
    finally:
        if tmp_path:
            with suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
    for part in _plain_chunks(out):
        await update.message.reply_text(_h(part), parse_mode=ParseMode.HTML)


def main() -> None:
    global CATALOG

    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")

    CATALOG = load_catalog(GDZ_CATALOG_PATH)
    user_storage.init_db(USER_DB_PATH)
    bot_stats.init_stats(USER_DB_PATH)

    telegram_proxy = (os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()

    logger.info(
        "starting bot catalog=%s db=%s server_url=%s telegram_proxy=%s",
        GDZ_CATALOG_PATH,
        USER_DB_PATH,
        SERVER_URL,
        "on" if telegram_proxy else "off",
    )

    builder = Application.builder().token(BOT_TOKEN).post_init(post_init_commands)
    if telegram_proxy:
        builder = builder.request(
            HTTPXRequest(
                proxy=telegram_proxy,
                connect_timeout=30.0,
                read_timeout=60.0,
                write_timeout=60.0,
            )
        )
    app = builder.build()
    app.add_error_handler(error_handler)

    tgzh_metrics.maybe_start_http_server()

    async def _metrics_on_update(u: Update, _c: ContextTypes.DEFAULT_TYPE) -> None:
        tgzh_metrics.record_bot_update(u)
        eu = u.effective_user
        if eu is not None and eu.id > 0:
            await asyncio.to_thread(bot_stats.record_user_visit_day, USER_DB_PATH, eu.id)

    app.add_handler(TypeHandler(Update, _metrics_on_update), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("textbook", textbook_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("my_support", my_support_cmd))
    app.add_handler(CommandHandler("polling", polling_cmd))
    app.add_handler(CommandHandler("begemot", begemot_cmd))
    app.add_handler(CommandHandler("cursorask", cursor_ask_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_homework_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CaptionRegex(r"^/cursorask(@\S*)?(\s|$)"),
            cursor_ask_photo_cmd,
        ),
    )
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
