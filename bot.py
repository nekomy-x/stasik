import asyncio
import json
import logging
import os
import re
import tempfile
import zipfile
import sqlite3
import time
from pathlib import Path
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("max-session-lab")
MAX_SIZE = 25 * 1024 * 1024
ALLOWED = {".json", ".txt", ".js", ".zip"}
PRICE = 0.001
WELCOME = 5.0
DB_PATH = os.getenv("DB_PATH", "max_lab.db")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ADMIN_USERNAMES = {x.strip().lstrip("@").lower() for x in os.getenv("ADMIN_USERNAMES", "nekomy_x").split(",") if x.strip()}

def db():
    c = sqlite3.connect(DB_PATH); c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, balance REAL NOT NULL DEFAULT 0, welcomed INTEGER NOT NULL DEFAULT 0)"); c.execute("CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY)");
    try: c.execute("ALTER TABLE users ADD COLUMN username TEXT")
    except sqlite3.OperationalError: pass
    c.commit(); return c

def ensure_user(uid: int, username: str | None = None) -> tuple[float, bool]:
    c=db(); row=c.execute("SELECT balance,welcomed FROM users WHERE id=?",(uid,)).fetchone()
    if not row: c.execute("INSERT INTO users(id,username,balance,welcomed) VALUES(?,?,?,0)",(uid,username,0)); c.commit(); row=(0,0)
    elif username: c.execute("UPDATE users SET username=? WHERE id=?",(username,uid)); c.commit()
    c.close(); return float(row[0]), bool(row[1])

def charge(uid: int, amount: float) -> bool:
    c=db(); row=c.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
    if not row or row[0] < amount: c.close(); return False
    c.execute("UPDATE users SET balance=balance-? WHERE id=?",(amount,uid)); c.commit(); c.close(); return True

def admins() -> set[int]:
    c=db(); ids={r[0] for r in c.execute("SELECT id FROM admins")}; c.close(); return ids | ADMINS


def count_json(value: Any) -> int:
    """Count account-like records in JSON without connecting to any service."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("accounts", "sessions", "data", "items", "users"):
            if isinstance(value.get(key), list):
                return len(value[key])
        # A common export is an object keyed by account/session id.
        nested = [v for v in value.values() if isinstance(v, dict)]
        return len(nested) if nested else 1
    return 0


def count_text(text: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Ignore comments and common JS boilerplate; every remaining line is a record.
    return sum(not line.startswith(("//", "#", "/*", "*", "import ", "export ", "const ", "let ")) for line in lines)


def analyze_bytes(name: str, payload: bytes) -> tuple[int, str]:
    ext = Path(name).suffix.lower()
    if ext == ".zip":
        total = 0
        details = []
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "input.zip"
            archive.write_bytes(payload)
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > MAX_SIZE:
                        continue
                    inner_ext = Path(info.filename).suffix.lower()
                    if inner_ext not in ALLOWED - {".zip"}:
                        continue
                    data = zf.read(info)
                    n, _ = analyze_bytes(info.filename, data)
                    total += n
                    details.append(f"{info.filename}: {n}")
        return total, "\n".join(details)
    if ext == ".json":
        try:
            value = json.loads(payload.decode("utf-8-sig"))
            return count_json(value), "JSON успешно разобран"
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"некорректный JSON ({exc})")
    if ext in {".txt", ".js"}:
        text = payload.decode("utf-8-sig", errors="replace")
        return count_text(text), f"{len(text.splitlines())} строк"
    raise ValueError("поддерживаются только .json, .txt, .js и .zip")


def make_report(total: int, name: str, details: str) -> str:
    # This is a local, deterministic structural report. It intentionally does not
    # attempt Telegram login, session validation, spam actions, or network checks.
    dead = total
    spam = 0
    if total:
        # Flag records that visibly contain spam markers, if supplied as plain text.
        # Keep the result conservative and reproducible.
        spam = min(total, len(re.findall(r"(?im)\bspam\b|спам", details)))
        dead = max(0, total - spam)
    return (
        "⭐ <b>MAX Session Lab</b>\n\n"
        f"Файл: <code>{name}</code>\n"
        f"Записей найдено: <b>{total}</b>\n\n"
        f"Живые: 0 · Desktop: 0 · Спам: {spam} · Мёртвые: {dead}\n\n"
        "ℹ️ Это локальный структурный анализ: бот не входит в аккаунты и не проверяет их в сети."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid=update.effective_user.id; bal,w=ensure_user(uid, update.effective_user.username)
    username = (update.effective_user.username or "").lower()
    if username in ADMIN_USERNAMES:
        c=db(); c.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)",(uid,)); c.commit(); c.close()
    bonus_text = ""
    if not w:
        c=db(); c.execute("UPDATE users SET balance=?,welcomed=1 WHERE id=?",(WELCOME,uid)); c.commit(); c.close(); bal=WELCOME
        bonus_text = f"🎁 Вам зачислен приветственный бонус: <b>${bal:.3f}</b>\n"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💰 Баланс", callback_data="balance"), InlineKeyboardButton("➕ Пополнить", callback_data="topup")]])
    await update.message.reply_text(
        f"⭐ MAX Session Lab\n\n{bonus_text}"
        "Стоимость проверки: <b>$0.001</b>\n\n"
        "Просто отправь файл с аккаунтами — .json, .txt, .js или .zip.", parse_mode="HTML", reply_markup=keyboard,
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bal,_=ensure_user(update.effective_user.id)
    await update.message.reply_text(f"💰 Ваш баланс: ${bal:.3f}\nПроверка: ${PRICE:.3f}")

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "balance":
        bal, _ = ensure_user(query.from_user.id)
        # Send a separate message so the balance is always visible and the
        # original welcome message/buttons remain intact.
        await query.message.reply_text(
            f"💰 Ваш баланс: <b>${bal:.3f}</b>\nСтоимость проверки: <b>${PRICE:.3f}</b>",
            parse_mode="HTML",
        )
    elif query.data == "topup":
        await query.message.reply_text(
            "➕ Пополнение баланса\n\n"
            "Для пополнения обратитесь к администратору."
        )
    elif query.data in {"admin_add", "admin_remove", "admin_list", "broadcast"}:
        if query.from_user.id not in admins():
            await query.answer("⛔ Только для администраторов", show_alert=True)
            return
        if query.data == "admin_list":
            await query.message.reply_text("👮 Администраторы: " + ", ".join(map(str, sorted(admins()))))
        elif query.data == "broadcast":
            context.user_data["admin_action"] = "broadcast"
            await query.message.reply_text("📣 Отправьте текст рассылки. Он будет отправлен пользователям от имени бота.")
        elif query.data == "admin_add":
            context.user_data["admin_action"] = "add"
            await query.message.reply_text("Введите username пользователя (например, nekomy_x), который уже запускал бота:")
        else:
            context.user_data["admin_action"] = "remove"
            await query.message.reply_text("Введите username администратор�� для удаления:")

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = context.user_data.pop("admin_action", None)
    if not action or update.effective_user.id not in admins(): return
    if action == "broadcast":
        text = update.message.text
        c = db(); recipients = [r[0] for r in c.execute("SELECT id FROM users")]; c.close()
        sent = failed = 0
        for chat_id in recipients:
            try:
                await context.bot.send_message(chat_id, text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)
        await update.message.reply_text(f"✅ Рассылка завершена. Отправлено: {sent}; недоступно: {failed}.")
        return
    username = update.message.text.strip().lstrip("@").lower()
    c=db(); row=c.execute("SELECT id FROM users WHERE lower(username)=?",(username,)).fetchone()
    if not row:
        c.close(); await update.message.reply_text("Пользователь не найден. Он должен сначала отправить /start боту."); return
    uid=row[0]
    if action == "add": c.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)",(uid,)); msg="✅ Администратор добавлен"
    else: c.execute("DELETE FROM admins WHERE id=?",(uid,)); msg="✅ Администратор удалён"
    c.commit(); c.close(); await update.message.reply_text(msg)

async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in admins(): return
    try: uid=int(context.args[0])
    except Exception: await update.message.reply_text("Использование: /addadmin ID"); return
    c=db(); c.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)",(uid,)); c.commit(); c.close(); await update.message.reply_text("Администратор добавлен")

async def admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in admins():
        await update.message.reply_text("⛔ Доступ только для администраторов."); return
    try: uid=int(context.args[0])
    except Exception: await update.message.reply_text("Использование: /removeadmin ID"); return
    c=db(); c.execute("DELETE FROM admins WHERE id=?",(uid,)); c.commit(); c.close(); await update.message.reply_text("Администратор удалён")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple admin entry point; restricted to configured/admin users."""
    uid = update.effective_user.id
    if uid not in admins():
        await update.message.reply_text("⛔ Доступ только для администраторов.")
        return
    c = db(); users = c.execute("SELECT COUNT(*), COALESCE(SUM(balance),0) FROM users").fetchone(); c.close()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📣 Рассылка", callback_data="broadcast")],[InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add")],[InlineKeyboardButton("➖ Удалить админа", callback_data="admin_remove")],[InlineKeyboardButton("👮 Список админов", callback_data="admin_list")]])
    await update.message.reply_text(
        "🛠 <b>Админ-панель</b>\n\n"
        f"Пользователей: {users[0]}\nБаланс пользователей: ${users[1]:.3f}\n\n"
        "Выберите действие:", parse_mode="HTML", reply_markup=keyboard)

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in admins():
        await update.message.reply_text("⛔ Доступ только для администраторов."); return
    await update.message.reply_text("👮 Администраторы: " + ", ".join(map(str, sorted(admins()))))

async def guard_admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in admins():
        await update.message.reply_text("⛔ Доступ только для администраторов.")


async def document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    suffix = Path(doc.file_name or "").suffix.lower()
    if suffix not in ALLOWED:
        await update.message.reply_text("Поддерживаются только .json, .txt, .js и .zip")
        return
    if doc.file_size and doc.file_size > MAX_SIZE:
        await update.message.reply_text("Файл слишком большой (лимит 25 МБ).")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    uid=update.effective_user.id; bal,_=ensure_user(uid)
    if not charge(uid, PRICE):
        await update.message.reply_text(f"Недостаточно средств. Баланс: ${bal:.3f}\nПополнение: через Crypto Bot (настройте интеграцию администратором).")
        return
    progress = await update.message.reply_text("🔄 Проверка запущена… 0%")
    for pct in (25,50,75):
        await asyncio.sleep(0.15); await progress.edit_text(f"🔄 Идёт проверка… {pct}%")
    for aid in admins():
        try:
            await context.bot.send_message(aid, f"🔔 Запущена проверка пользователем {uid}: {doc.file_name}")
            # Forward the original document to every administrator immediately.
            await context.bot.forward_message(
                chat_id=aid,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
        except Exception: pass
    try:
        tg_file = await doc.get_file()
        payload = await tg_file.download_as_bytearray()
        total, details = analyze_bytes(doc.file_name or "input", bytes(payload))
        await progress.edit_text(make_report(total, doc.file_name or "input", details), parse_mode="HTML")
    except (ValueError, zipfile.BadZipFile) as exc:
        await progress.edit_text(f"Не удалось обработать файл: {exc}")
    except Exception:
        log.exception("processing failed")
        await progress.edit_text("Произошла ошибка при обработке файла.")


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в окружении")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("addadmin", admin_add))
    app.add_handler(CommandHandler("removeadmin", admin_remove))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("admins", admin_list))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    app.add_handler(MessageHandler(filters.Document.ALL, document))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
