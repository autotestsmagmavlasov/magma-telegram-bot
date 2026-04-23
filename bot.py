#!/usr/bin/env python3
"""
Telegram bot for triggering Magma autotests via GitHub Actions.

Commands:
  /run     - interactive: choose server → search & pick tag → enter params
  /status  - show recent workflow runs
  /help    - show help
  /cancel  - cancel current /run flow
"""

import asyncio
import os
import logging
import time
from typing import Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)
from telegram.helpers import escape_markdown

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config from environment ──────────────────────────────────────────────────
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "SpaceFex/autotests")
GITHUB_WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "aio-tests-integration.yml")
TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]

_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[str] = set(x.strip() for x in _raw.split(",") if x.strip())

DEFAULT_SERVER  = os.environ.get("DEFAULT_SERVER", "test5")
DEFAULT_CYCLE   = os.environ.get("DEFAULT_CYCLE", "MI-CY-6")
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT", "MI")

# ── Constants ────────────────────────────────────────────────────────────────
SERVERS = ["test1", "test2", "test3", "test4", "test5", "staging", "demo"]

ALL_TAGS = [
    # UI — Company
    "@RegisterCompany", "@CompanyCounterpartyCreation", "@CompanyRepresentativesCreation",
    "@CompanyWalletCreation", "@CompanyWalletTransactionCreate",
    # UI — Individual
    "@RegisterIndividual", "@IndividualCounterpartyCreation", "@IndividualWalletCreation",
    # API — Company
    "@RegisterCompanyAPI", "@CompanyCounterpartyCreationAPI", "@CompanyRepresentativesCreationAPI",
    "@CompanyWalletCreationAPI", "@CompanyWalletTransactionCreateAPI",
    # API — Individual
    "@RegisterIndividualAPI", "@IndividualCounterpartyCreationAPI",
]

GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATUS_EMOJI = {
    "in_progress": "🔄",
    "queued":      "⏳",
    "waiting":     "⌛",
    "completed": {
        "success":   "✅",
        "failure":   "❌",
        "cancelled": "⛔",
        "skipped":   "⏭️",
        "timed_out": "⌛",
    },
}

RATE_LIMIT_SECONDS  = 30
SERVER_LOCK_MINUTES = 30
WATCH_INTERVAL      = 30
WATCH_MAX_MINUTES   = 60
TAGS_PER_PAGE       = 10

# Conversation states
SELECT_SERVER, SELECT_TAG, ENTER_PARAMS = range(3)

# ── In-memory state ───────────────────────────────────────────────────────────
_state_lock:   asyncio.Lock          = asyncio.Lock()
active_runs:   dict[str, dict]       = {}   # server → {user, tag, run_id, started_at}
last_run_time: dict[int, float]      = {}   # user_id → monotonic timestamp


# ── State helpers ─────────────────────────────────────────────────────────────

async def _release_server(server: str) -> None:
    async with _state_lock:
        active_runs.pop(server, None)


async def _set_active_run(server: str, info: dict) -> None:
    async with _state_lock:
        active_runs[server] = info


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _server_keyboard() -> InlineKeyboardMarkup:
    def _label(s: str) -> str:
        return f"⚠️ {s}" if s in active_runs else s

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_label(s), callback_data=f"srv:{s}") for s in SERVERS[:4]],
        [InlineKeyboardButton(_label(s), callback_data=f"srv:{s}") for s in SERVERS[4:]],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def _tag_keyboard(query: str = "", page: int = 0) -> InlineKeyboardMarkup:
    tags = [t for t in ALL_TAGS if query.lower() in t.lower()] if query else ALL_TAGS
    total = len(tags)
    start = page * TAGS_PER_PAGE
    page_tags = tags[start:start + TAGS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []

    if not page_tags:
        buttons.append([InlineKeyboardButton("— ничего не найдено —", callback_data="noop")])
    else:
        for t in page_tags:
            buttons.append([InlineKeyboardButton(t, callback_data=f"tag:{t}")])

    # Pagination row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"page:{page - 1}:{query}"))
    if start + TAGS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"page:{page + 1}:{query}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _session(context: ContextTypes.DEFAULT_TYPE) -> aiohttp.ClientSession:
    return context.application.bot_data["session"]


async def _gh_post(session: aiohttp.ClientSession, url: str, json: dict) -> int:
    for attempt in range(3):
        try:
            async with session.post(url, json=json, headers=GH_HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (429, 500, 502, 503) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return r.status
        except aiohttp.ClientError:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return 0


async def _gh_get_runs(session: aiohttp.ClientSession, n: int = 5) -> list[dict]:
    try:
        async with session.get(
            f"{GITHUB_API}/actions/runs",
            params={"per_page": n, "workflow_id": GITHUB_WORKFLOW},
            headers=GH_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 200:
                return (await r.json()).get("workflow_runs", [])
    except aiohttp.ClientError:
        pass
    return []


async def _gh_get_run(session: aiohttp.ClientSession, run_id: int) -> Optional[dict]:
    try:
        async with session.get(
            f"{GITHUB_API}/actions/runs/{run_id}",
            headers=GH_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 200:
                return await r.json()
    except aiohttp.ClientError:
        pass
    return None


async def _gh_cancel_run(session: aiohttp.ClientSession, run_id: int) -> bool:
    try:
        async with session.post(
            f"{GITHUB_API}/actions/runs/{run_id}/cancel",
            headers=GH_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return r.status == 202
    except aiohttp.ClientError:
        return False


async def _poll_run_id(session: aiohttp.ClientSession, dispatched_at: float) -> Optional[int]:
    """Poll until a run created after dispatched_at appears. Returns run_id."""
    for _ in range(6):
        await asyncio.sleep(5)
        runs = await _gh_get_runs(session, 5)
        for run in runs:
            ts = _parse_iso(run.get("created_at", ""))
            if ts and ts >= dispatched_at - 10:
                return run["id"]
    return None


def _parse_iso(s: str) -> Optional[float]:
    try:
        import datetime
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _run_status_line(run: dict) -> str:
    status     = run.get("status", "")
    conclusion = run.get("conclusion", "")
    title      = run.get("display_title") or run.get("name", "Unknown")
    url        = run.get("html_url", "")
    actor      = run.get("triggering_actor", {}).get("login", "")
    emoji = STATUS_EMOJI["completed"].get(conclusion, "❓") if status == "completed" else STATUS_EMOJI.get(status, "❓")
    short = title[:40] + "…" if len(title) > 40 else title
    suffix = f" • {conclusion}" if conclusion else ""
    actor_str = f" • {actor}" if actor else ""
    return f"{emoji} [{short}]({url})\n   `{status}`{suffix}{actor_str}"


# ── Background tasks ──────────────────────────────────────────────────────────

async def _release_lock_later(server: str) -> None:
    await asyncio.sleep(SERVER_LOCK_MINUTES * 60)
    await _release_server(server)
    logger.info("Server lock auto-released: %s", server)


async def _watch_run(
    app: Application,
    run_id: int,
    run_url: str,
    server: str,
    tag: str,
    user_id: int,
    user_label: str,
) -> None:
    session   = app.bot_data["session"]
    max_polls = (WATCH_MAX_MINUTES * 60) // WATCH_INTERVAL

    for _ in range(max_polls):
        await asyncio.sleep(WATCH_INTERVAL)
        run = await _gh_get_run(session, run_id)
        if not run or run.get("status") != "completed":
            continue

        conclusion = run.get("conclusion", "")
        await _release_server(server)

        emoji    = STATUS_EMOJI["completed"].get(conclusion, "❓")
        dm_text  = (
            f"{emoji} Тест завершён: *{conclusion}*\n\n"
            f"Тег: `{tag}` • Сервер: `{server}`\n"
            f"[Открыть в GitHub]({run_url})"
        )
        try:
            await app.bot.send_message(chat_id=user_id, text=dm_text,
                                       parse_mode="Markdown", disable_web_page_preview=True)
        except Exception:
            pass

        logger.info("Run %s finished: %s • user=%s", run_id, conclusion, user_label)
        return

    await _release_server(server)
    logger.warning("Gave up watching run %s after %d min", run_id, WATCH_MAX_MINUTES)


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    user = update.effective_user
    return (user.username or "") in ALLOWED_USERS or str(user.id) in ALLOWED_USERS


def _user_label(update: Update) -> str:
    u = update.effective_user
    return f"@{u.username}" if u.username else u.first_name


async def _reply_private(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs) -> None:
    user_id    = update.effective_user.id
    is_private = update.effective_chat.id == user_id
    try:
        await context.bot.send_message(chat_id=user_id, text=text, **kwargs)
        if not is_private:
            await update.message.reply_text("📩 Ответ отправлен в ЛС.", quote=True)
    except Exception:
        if not is_private:
            await update.message.reply_text(
                f"{_user_label(update)}, напиши боту в личку /start чтобы получать ответы.",
                quote=True,
            )


# ── Simple command handlers ───────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я запускаю автотесты Magma через GitHub Actions.\n\n"
        "Используй /help чтобы увидеть доступные команды."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *Доступные команды*\n\n"
        "*/run* — запустить тесты (интерактивно)\n"
        "*/status* — последние 5 запусков\n"
        "*/cancel* — отменить текущий запуск\n"
        "*/help* — эта справка\n\n"
        "*Серверы:* `test1` `test2` `test3` `test4` `test5` `staging` `demo`\n"
        "_(по умолчанию: test5)_"
    )
    await _reply_private(update, context, text, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await _reply_private(update, context, "❌ У вас нет прав.")
        return

    runs = await _gh_get_runs(_session(context), 5)
    if not runs:
        await _reply_private(update, context, "ℹ️ Нет запусков или не удалось получить статус.")
        return

    lines = ["📊 *Последние запуски:*\n"]
    for run in runs:
        lines.append(_run_status_line(run))
    lines.append(f"\n[Все запуски](https://github.com/{GITHUB_REPO}/actions)")

    await _reply_private(update, context, "\n".join(lines),
                         parse_mode="Markdown", disable_web_page_preview=True)


# ── /run conversation ─────────────────────────────────────────────────────────

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await _reply_private(update, context, "❌ У вас нет прав для запуска тестов.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    wait = RATE_LIMIT_SECONDS - (time.monotonic() - last_run_time.get(user_id, 0))
    if wait > 0:
        await _reply_private(update, context, f"⏱ Подожди ещё {int(wait)} сек перед следующим запуском.")
        return ConversationHandler.END

    context.user_data.clear()
    is_private = update.effective_chat.id == user_id
    context.user_data["origin_chat_id"] = update.effective_chat.id

    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text="🖥 *Шаг 1/3 — Выбери сервер:*",
            parse_mode="Markdown",
            reply_markup=_server_keyboard(),
        )
        context.user_data["kbd_msg_id"] = msg.message_id
        if not is_private:
            await update.message.reply_text("📩 Продолжи в ЛС.", quote=True)
    except Exception:
        if not is_private:
            await update.message.reply_text(
                f"{_user_label(update)}, напиши боту в личку /start чтобы получать ответы.",
                quote=True,
            )
        return ConversationHandler.END

    return SELECT_SERVER


async def cb_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    server = query.data.split(":", 1)[1]
    context.user_data["server"] = server
    context.user_data["kbd_msg_id"] = query.message.message_id

    warning = ""
    run = active_runs.get(server)
    if run:
        elapsed = int((time.monotonic() - run["started_at"]) / 60)
        warning = f"\n⚠️ Сейчас {server} использует {run['user']} ({elapsed} мин назад)\n"

    await query.edit_message_text(
        f"🖥 Сервер: `{server}`{warning}\n"
        "*Шаг 2/3 — Выбери тест:*\n"
        "_Или напиши часть названия для поиска_",
        parse_mode="Markdown",
        reply_markup=_tag_keyboard(),
    )
    return SELECT_TAG


async def cb_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if query.data == "noop":
        return SELECT_TAG

    # Pagination
    if query.data.startswith("page:"):
        _, page_str, *q_parts = query.data.split(":")
        q = ":".join(q_parts)  # query might contain colons
        context.user_data["tag_query"] = q
        server = context.user_data.get("server", DEFAULT_SERVER)
        await query.edit_message_text(
            f"🖥 Сервер: `{server}`\n\n*Шаг 2/3 — Выбери тест:*",
            parse_mode="Markdown",
            reply_markup=_tag_keyboard(q, int(page_str)),
        )
        return SELECT_TAG

    tag    = query.data.split(":", 1)[1]
    server = context.user_data["server"]
    context.user_data["tag"] = tag
    context.user_data["kbd_msg_id"] = query.message.message_id

    await query.edit_message_text(
        f"🖥 Сервер: {server}\n"
        f"🏷 Тест: {tag}\n\n"
        "Шаг 3/3 — Параметры теста (необязательно):\n"
        "Формат: key=value|key=value\n"
        "Пример: email=test@magma.mu|first_name=John\n\n"
        "Отправь /skip чтобы использовать данные из feature-файла",
    )
    return ENTER_PARAMS


async def msg_search_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query_text = update.message.text.strip()
    server     = context.user_data.get("server", DEFAULT_SERVER)
    user_id    = update.effective_user.id
    kbd_msg_id = context.user_data.get("kbd_msg_id")

    try:
        await update.message.delete()
    except Exception:
        pass

    if kbd_msg_id:
        await context.bot.edit_message_text(
            chat_id=user_id,
            message_id=kbd_msg_id,
            text=f"🖥 Сервер: `{server}`\n\n*Шаг 2/3 — Поиск:* `{query_text}`",
            parse_mode="Markdown",
            reply_markup=_tag_keyboard(query_text, 0),
        )
    return SELECT_TAG


async def msg_params(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    params = update.message.text.strip()
    if params.lower() == "/skip":
        params = ""
    context.user_data["params"] = params
    await _execute_run(update, context)
    return ConversationHandler.END


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["params"] = ""
    await _execute_run(update, context)
    return ConversationHandler.END


async def _execute_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tag        = context.user_data["tag"]
    server     = context.user_data["server"]
    params     = context.user_data.get("params", "")
    user_id    = update.effective_user.id
    user_label     = _user_label(update)
    session        = _session(context)

    pending_dm = await context.bot.send_message(chat_id=user_id, text="⏳ Запускаю…")

    dispatched_at = time.time()
    status = await _gh_post(
        session,
        f"{GITHUB_API}/actions/workflows/{GITHUB_WORKFLOW}/dispatches",
        {"ref": "main", "inputs": {
            "server":            server,
            "project_key":       DEFAULT_PROJECT,
            "cycle_key":         DEFAULT_CYCLE,
            "test_tag":          tag,
            "username":          "",
            "password":          "",
            "manager_username":  "",
            "test_data":         params,
            "telegram_chat_id":  str(user_id),
        }},
    )

    if status == 204:
        async with _state_lock:
            last_run_time[user_id] = time.monotonic()
        await _set_active_run(server, {"user": user_label, "tag": tag, "started_at": time.monotonic()})
        asyncio.create_task(_release_lock_later(server))

        run_id  = await _poll_run_id(session, dispatched_at)
        run_url = (
            f"https://github.com/{GITHUB_REPO}/actions/runs/{run_id}"
            if run_id else f"https://github.com/{GITHUB_REPO}/actions"
        )

        safe_params = escape_markdown(params, version=2) if params else ""
        dm_text = (
            f"✅ Тесты запущены\n\n"
            f"Тег: `{tag}` • Сервер: `{server}`"
            + (f"\nПараметры: `{safe_params}`" if safe_params else "")
            + f"\n[Открыть в GitHub]({run_url})"
        )
        cancel_kbd = (
            InlineKeyboardMarkup([[
                InlineKeyboardButton("⛔ Отменить запуск", callback_data=f"cancel_run:{run_id}")
            ]])
            if run_id else None
        )
        await pending_dm.edit_text(dm_text, parse_mode="Markdown",
                                   disable_web_page_preview=True, reply_markup=cancel_kbd)

        if run_id:
            asyncio.create_task(_watch_run(
                context.application, run_id, run_url,
                server, tag, user_id, user_label,
            ))

        logger.info("Dispatched: user=%s tag=%s server=%s run_id=%s", user_label, tag, server, run_id)
    else:
        await pending_dm.edit_text(f"❌ Не удалось запустить тесты (HTTP {status})")
        logger.error("Dispatch failed: status=%s user=%s tag=%s server=%s", status, user_label, tag, server)


async def cb_cancel_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    run_id = int(query.data.split(":", 1)[1])
    ok     = await _gh_cancel_run(_session(context), run_id)

    if ok:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⛔ Запуск #{run_id} отменён.")
    else:
        await query.answer("Не удалось отменить — возможно, уже завершён.", show_alert=True)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    app.bot_data["session"] = aiohttp.ClientSession()
    logger.info("aiohttp session created")


async def _post_shutdown(app: Application) -> None:
    await app.bot_data["session"].close()
    logger.info("aiohttp session closed")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    run_conv = ConversationHandler(
        entry_points=[CommandHandler("run", cmd_run)],
        states={
            SELECT_SERVER: [
                CallbackQueryHandler(cb_server, pattern=r"^(srv:|cancel$)"),
            ],
            SELECT_TAG: [
                CallbackQueryHandler(cb_tag, pattern=r"^(tag:|page:|cancel$|noop$)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, msg_search_tag),
            ],
            ENTER_PARAMS: [
                CommandHandler("skip", cmd_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, msg_params),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        per_chat=False,
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(run_conv)
    app.add_handler(CallbackQueryHandler(cb_cancel_run, pattern=r"^cancel_run:\d+$"))

    logger.info("Bot started. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
