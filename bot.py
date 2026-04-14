#!/usr/bin/env python3
"""
Telegram bot for triggering Magma autotests via GitHub Actions.

Commands:
  /run <tag> [server] [cycle_key]  - trigger tests
  /status                          - show recent workflow runs
  /help                            - show help
"""

import os
import time
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# Optional: comma-separated list of allowed Telegram usernames OR user IDs.
# Leave empty to allow everyone.
_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[str] = set(x.strip() for x in _raw.split(",") if x.strip())

# ── Constants ────────────────────────────────────────────────────────────────
SERVERS = ["test1", "test2", "test3", "test4", "test5", "staging", "demo"]
DEFAULT_SERVER  = "test5"
DEFAULT_CYCLE   = "MI-CY-6"
DEFAULT_PROJECT = "MI"

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    user = update.effective_user
    return (user.username or "") in ALLOWED_USERS or str(user.id) in ALLOWED_USERS


def _dispatch_workflow(inputs: dict) -> requests.Response:
    url = f"{GITHUB_API}/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
    return requests.post(url, json={"ref": "main", "inputs": inputs}, headers=GH_HEADERS, timeout=15)


def _get_recent_runs(n: int = 5) -> list[dict]:
    url = f"{GITHUB_API}/actions/runs"
    resp = requests.get(url, params={"per_page": n, "workflow_id": GITHUB_WORKFLOW}, headers=GH_HEADERS, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("workflow_runs", [])
    return []


def _run_status_line(run: dict) -> str:
    status     = run.get("status", "")
    conclusion = run.get("conclusion", "")
    title      = run.get("display_title") or run.get("name", "Unknown")
    url        = run.get("html_url", "")

    if status == "completed":
        emoji = STATUS_EMOJI["completed"].get(conclusion, "❓")
    else:
        emoji = STATUS_EMOJI.get(status, "❓")

    short = title[:45] + "…" if len(title) > 45 else title
    suffix = f" • {conclusion}" if conclusion else ""
    return f"{emoji} [{short}]({url})\n   `{status}`{suffix}"


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я запускаю автотесты Magma через GitHub Actions.\n\n"
        "Используй /help чтобы увидеть доступные команды."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *Доступные команды*\n\n"
        "*/run* `<tag> [server] [cycle]`\n"
        "Запустить тесты по тегу.\n\n"
        "*Примеры:*\n"
        "• `/run @RegisterCompany`\n"
        "• `/run @RegisterCompany test3`\n"
        "• `/run @RegisterCompany test3 MI-CY-6`\n"
        '• `/run "@MI-TC-128 or @RegisterCompany" test5`\n\n'
        "*Серверы:* `test1` `test2` `test3` `test4` `test5` `staging` `demo`\n"
        "_(по умолчанию: `test5`)_\n\n"
        "*/status* — последние 5 запусков\n"
        "*/help*   — эта справка"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("❌ У вас нет прав для запуска тестов.")
        return

    args = context.args or []

    if not args:
        await update.message.reply_text(
            "❌ Укажи тег.\n"
            "Пример: `/run @RegisterCompany`\n"
            "Или:    `/run @RegisterCompany test3 MI-CY-6`",
            parse_mode="Markdown",
        )
        return

    tag       = args[0] if args[0].startswith("@") else f"@{args[0]}"
    server    = args[1] if len(args) > 1 else DEFAULT_SERVER
    cycle_key = args[2] if len(args) > 2 else DEFAULT_CYCLE

    if server not in SERVERS:
        await update.message.reply_text(
            f"❌ Неизвестный сервер: `{server}`\n"
            f"Доступные: {', '.join(SERVERS)}",
            parse_mode="Markdown",
        )
        return

    pending = await update.message.reply_text("⏳ Запускаю…")

    resp = _dispatch_workflow({
        "server":       server,
        "project_key":  DEFAULT_PROJECT,
        "cycle_key":    cycle_key,
        "test_tag":     tag,
        "username":     "",
        "password":     "",
        "manager_username": "",
        "test_data":    "",
    })

    if resp.status_code == 204:
        time.sleep(4)  # wait for GitHub to register the run
        runs = _get_recent_runs(1)
        run_url = runs[0]["html_url"] if runs else f"https://github.com/{GITHUB_REPO}/actions"

        await pending.edit_text("✅ Готово, тесты запущены.")
        logger.info("Workflow dispatched: tag=%s server=%s cycle=%s run_url=%s", tag, server, cycle_key, run_url)
    else:
        error = ""
        try:
            error = resp.json().get("message", "")
        except Exception:
            pass
        await pending.edit_text(
            f"❌ Не удалось запустить тесты.\n"
            f"HTTP {resp.status_code}" + (f": {error}" if error else ""),
        )
        logger.error("Dispatch failed: status=%s body=%s", resp.status_code, resp.text[:300])


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("❌ У вас нет прав.")
        return

    pending = await update.message.reply_text("⏳ Получаю статус…")

    runs = _get_recent_runs(5)
    if not runs:
        await pending.edit_text("ℹ️ Нет запусков или не удалось получить статус.")
        return

    lines = ["📊 *Последние запуски:*\n"]
    for run in runs:
        lines.append(_run_status_line(run))
    lines.append(f"\n[Все запуски](https://github.com/{GITHUB_REPO}/actions)")

    await pending.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("run",    cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("Bot started. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
