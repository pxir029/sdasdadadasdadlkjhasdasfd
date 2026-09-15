# -*- coding: utf-8 -*-
import os
import sys
import json
import uuid
import sqlite3
import time
import requests
import telebot
from telebot import types
from datetime import datetime

# ================== تنظیمات ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8944694178:AAE3NZPRLpBjxRmfHLAxg0_gl9IxT-7nmkc")
CF_API_BASE = "https://api.cloudflare.com/client/v4"
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7326030446"))

DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")
DB_PATH = os.path.join(DATA_DIR, "bot_data.db")

TOKEN_URL = (
    "https://dash.cloudflare.com/profile/api-tokens"
    "?permissionGroupKeys=%5B%7B%22key%22%3A%22account_settings%22%2C%22type%22%3A%22read%22%7D"
    "%2C%7B%22key%22%3A%22d1%22%2C%22type%22%3A%22edit%22%7D"
    "%2C%7B%22key%22%3A%22workers_scripts%22%2C%22type%22%3A%22edit%22%7D%5D"
    "&accountId=*&zoneId=all&name=PX%20Deploy"
)

WORKER_CODE_URL = "https://raw.githubusercontent.com/iran-px-panel/px_wokers/refs/heads/main/worker.js"

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ================== دیتابیس دائمی ==================
def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT,
            is_banned INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id INTEGER PRIMARY KEY,
            account_id TEXT,
            account_name TEXT,
            token TEXT,
            workers TEXT DEFAULT '[]',
            db_uuids TEXT DEFAULT '[]',
            updated_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ---- migration: اضافه کردن ستون‌های گمشده اگر جدول قدیمی باشد ----
    c.execute("PRAGMA table_info(sessions)")
    columns = [row[1] for row in c.fetchall()]

    if "token" not in columns:
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN token TEXT")
            print("✅ migration: ستون token اضافه شد")
        except Exception as e:
            print(f"migration token: {e}")

    if "workers" not in columns:
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN workers TEXT DEFAULT '[]'")
            print("✅ migration: ستون workers اضافه شد")
        except Exception as e:
            print(f"migration workers: {e}")

    if "db_uuids" not in columns:
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN db_uuids TEXT DEFAULT '[]'")
            print("✅ migration: ستون db_uuids اضافه شد")
        except Exception as e:
            print(f"migration db_uuids: {e}")

    if "updated_at" not in columns:
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
            print("✅ migration: ستون updated_at اضافه شد")
        except Exception as e:
            print(f"migration updated_at: {e}")

    if "account_name" not in columns:
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN account_name TEXT")
            print("✅ migration: ستون account_name اضافه شد")
        except Exception as e:
            print(f"migration account_name: {e}")

    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    result = c.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result


def db_get_user(chat_id):
    row = db_execute(
        "SELECT chat_id, username, first_name, is_banned FROM users WHERE chat_id=?",
        (chat_id,),
        fetch=True,
    )
    if row:
        return {
            "chat_id": row[0][0],
            "username": row[0][1],
            "first_name": row[0][2],
            "is_banned": bool(row[0][3]),
        }
    return None


def db_save_user(chat_id, username, first_name):
    db_execute(
        "INSERT OR REPLACE INTO users (chat_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
        (chat_id, username, first_name, datetime.now().isoformat()),
    )


def db_get_all_users():
    rows = db_execute("SELECT chat_id FROM users WHERE is_banned=0", fetch=True)
    return [r[0] for r in rows] if rows else []


def db_set_ban(chat_id, banned=True):
    db_execute("UPDATE users SET is_banned=? WHERE chat_id=?", (1 if banned else 0, chat_id))


def db_save_session(chat_id, account_id, account_name, token, workers, db_uuids):
    db_execute(
        "INSERT OR REPLACE INTO sessions (chat_id, account_id, account_name, token, workers, db_uuids, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            account_id,
            account_name,
            token,
            json.dumps(workers),
            json.dumps(db_uuids),
            datetime.now().isoformat(),
        ),
    )


def db_get_session(chat_id):
    row = db_execute(
        "SELECT account_id, account_name, token, workers, db_uuids FROM sessions WHERE chat_id=?",
        (chat_id,),
        fetch=True,
    )
    if not row:
        return None
    account_id, account_name, token, workers, db_uuids = row[0]
    return {
        "account_id": account_id,
        "account_name": account_name,
        "token": token,
        "workers": json.loads(workers or "[]"),
        "db_uuids": json.loads(db_uuids or "[]"),
    }


def db_get_state(key, default=None):
    row = db_execute("SELECT value FROM bot_state WHERE key=?", (key,), fetch=True)
    return row[0][0] if row else default


def db_set_state(key, value):
    db_execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, str(value)))


# ================== وضعیت سراسری ==================
USER_SESSIONS = {}
BANNED_USERS = {}
ADMIN_REPLY_MAP = {}
CANCEL_FLAGS = set()
BOT_ENABLED = True
WAITING_FOR = {}


# ================== توابع Cloudflare ==================
def cf_session(token):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return s


def verify_token_and_get_account(token):
    s = cf_session(token)
    r = s.get(f"{CF_API_BASE}/user/tokens/verify", timeout=15)
    data = r.json()
    if not data.get("success"):
        raise Exception(f"توکن نامعتبر: {data.get('errors')}")

    r_acc = s.get(f"{CF_API_BASE}/accounts", timeout=15)
    acc_data = r_acc.json()
    if not acc_data.get("success"):
        raise Exception(f"خطا در گرفتن اکانت: {acc_data.get('errors')}")

    accounts = acc_data.get("result", [])
    if not accounts:
        raise Exception("هیچ اکانتی با این توکن پیدا نشد")

    return {
        "account_id": accounts[0]["id"],
        "account_name": accounts[0]["name"],
        "session": s,
        "token": token,
        "workers": [],
        "db_uuids": [],
    }


def generate_random_name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def create_d1_database(session, account_id, db_name):
    url = f"{CF_API_BASE}/accounts/{account_id}/d1/database"
    r = session.post(url, json={"name": db_name}, timeout=30)
    data = r.json()
    if not data.get("success"):
        raise Exception(f"خطا در ساخت دیتابیس: {data.get('errors')}")
    return data["result"]["uuid"]


def fetch_worker_code():
    r = requests.get(WORKER_CODE_URL, timeout=25)
    if r.status_code != 200:
        raise Exception(f"دریافت کد از گیت‌هاب ناموفق بود: کد {r.status_code}")
    text = r.text
    if not text.strip():
        raise Exception("فایل ورکر خالی است")
    return text


def upload_worker(session, account_id, worker_name, db_uuid, worker_code):
    metadata = {
        "main_module": "worker.js",
        "bindings": [{"name": "DB", "type": "d1", "id": db_uuid}],
        "compatibility_date": "2025-09-15",
        "compatibility_flags": ["allow_eval_during_startup"],
    }
    url = f"{CF_API_BASE}/accounts/{account_id}/workers/scripts/{worker_name}"
    token = session.headers["Authorization"].split()[-1]
    headers = {"Authorization": f"Bearer {token}"}
    files = {
        "metadata": (None, json.dumps(metadata), "application/json; charset=utf-8"),
        "worker.js": ("worker.js", worker_code.encode("utf-8"), "application/javascript+module"),
    }
    r = requests.put(url, headers=headers, files=files, timeout=90)
    data = r.json()
    if not data.get("success"):
        raise Exception(f"خطا در ساخت ورکر: {data.get('errors')}")

    try:
        sub_url = f"{CF_API_BASE}/accounts/{account_id}/workers/scripts/{worker_name}/subdomain"
        session.post(sub_url, json={"enabled": True}, timeout=15)
    except Exception:
        pass
    return True


def get_worker_subdomain(session, account_id):
    url = f"{CF_API_BASE}/accounts/{account_id}/workers/subdomain"
    try:
        r = session.get(url, timeout=15)
        data = r.json()
        if data.get("success"):
            return data["result"].get("subdomain")
    except Exception:
        pass
    return None


def delete_worker(session, account_id, worker_name):
    url = f"{CF_API_BASE}/accounts/{account_id}/workers/scripts/{worker_name}"
    token = session.headers["Authorization"].split()[-1]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.delete(url, headers=headers, timeout=30)
        return r.json()
    except Exception:
        return {"success": False}


def delete_d1_database(session, account_id, db_uuid):
    url = f"{CF_API_BASE}/accounts/{account_id}/d1/database/{db_uuid}"
    try:
        r = session.delete(url, timeout=30)
        return r.json()
    except Exception:
        return {"success": False}


# ================== ابزارها ==================
def is_cancelled(chat_id):
    return chat_id in CANCEL_FLAGS


def clear_cancel(chat_id):
    CANCEL_FLAGS.discard(chat_id)


def clear_waiting(chat_id):
    WAITING_FOR.pop(chat_id, None)


def cancel_markup():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_op"))
    return kb


def btn(text, callback_data=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=callback_data)


def main_menu(chat_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("🚀 ساخت ورکر جدید", callback_data="create"))
    kb.add(btn("🔑 ساخت توکن کلودفلر", url=TOKEN_URL))
    kb.add(btn("🔄 تغییر توکن", callback_data="change_token"))
    kb.add(btn("💬 پیام به سازنده", callback_data="contact_admin"))
    if chat_id == ADMIN_ID:
        kb.add(btn("🛡️ پنل مدیریت", callback_data="admin_panel"))
    return kb


def admin_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("👥 آمار کاربران", callback_data="admin_users"))
    kb.add(btn("📋 لیست کاربران", callback_data="admin_list_users"))
    kb.add(btn("📢 پیام همگانی", callback_data="admin_broadcast"))
    kb.add(btn("🛑 تعمیرات (خاموش/روشن)", callback_data="admin_maintenance"))
    kb.add(btn("🚫 بن کاربر", callback_data="admin_ban_user"))
    kb.add(btn("✅ رفع بن", callback_data="admin_unban_user"))
    kb.add(btn("🔙 بازگشت", callback_data="back_main"))
    return kb


def restore_session(chat_id):
    if chat_id in USER_SESSIONS:
        return USER_SESSIONS[chat_id]
    saved = db_get_session(chat_id)
    if not saved or not saved.get("token"):
        return None
    try:
        info = verify_token_and_get_account(saved["token"])
        info["workers"] = saved.get("workers", [])
        info["db_uuids"] = saved.get("db_uuids", [])
        USER_SESSIONS[chat_id] = info
        return info
    except Exception:
        return None


# ================== ربات ==================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


# ================== دستورات ==================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    clear_cancel(chat_id)
    clear_waiting(chat_id)

    user = message.from_user
    db_save_user(chat_id, user.username or "", user.first_name or "")

    u = db_get_user(chat_id)
    if u and u.get("is_banned"):
        bot.send_message(chat_id, "🚫 شما بن هستید.")
        return

    if not BOT_ENABLED and chat_id != ADMIN_ID:
        bot.send_message(chat_id, "🔧 ربات در حال تعمیرات است. لطفاً بعداً تلاش کنید.")
        return

    # اگر سشن قبلی وجود دارد، بازیابی کن
    restore_session(chat_id)

    bot.send_message(
        chat_id,
        "╭──────────────────────────╮\n"
        "     ⚡️ **PX Deploy** ⚡️\n"
        "╰──────────────────────────╯\n\n"
        "سلام گل! 👋\n\n"
        "من ربات خودکارسازی **Cloudflare** هستم.\n"
        "با چند تا کلیک ساده برات:\n\n"
        "  🗄️  دیتابیس **D1** می‌سازم\n"
        "  ⚙️  **Worker** دیپلوی می‌کنم\n"
        "  🔗  **Binding** رو خودکار ست می‌کنم\n"
        "  🌐  لینک نهایی رو تحویلت می‌دم\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "  **🚀 شروع سریع:**\n"
        "  1️⃣  اول توکن API بساز\n"
        "  2️⃣  توکن رو بفرست\n"
        "  3️⃣  دکمه ساخت رو بزن\n\n"
        "💡 هر جا خواستی لغو کنی، دستور /leave رو بزن.\n"
        "━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=main_menu(chat_id),
    )


@bot.message_handler(commands=["leave"])
def cmd_leave(message):
    clear_cancel(message.chat.id)
    clear_waiting(message.chat.id)
    CANCEL_FLAGS.add(message.chat.id)
    bot.send_message(
        message.chat.id,
        "🛑 **عملیات لغو شد.**\n\nهر وقت خواستی دوباره شروع کنی، دکمه 🚀 رو بزن.",
        parse_mode="Markdown",
        reply_markup=main_menu(message.chat.id),
    )


@bot.message_handler(commands=["data"])
def cmd_data(message):
    if message.chat.id != ADMIN_ID:
        return
    mount_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "نامشخص")
    volume_name = os.environ.get("RAILWAY_VOLUME_NAME", "نامشخص")
    db_exists = os.path.exists(DB_PATH)
    db_size = os.path.getsize(DB_PATH) if db_exists else 0
    users_count = len(db_get_all_users())
    text = (
        "╭──────────────────────╮\n"
        "   💾 **وضعیت ذخیره‌سازی**\n"
        "╰──────────────────────╯\n\n"
        f"📂 مسیر Volume: `{mount_path}`\n"
        f"🏷️ نام Volume: `{volume_name}`\n\n"
        f"🗄️ مسیر دیتابیس: `{DB_PATH}`\n"
        f"✅ وجود دیتابیس: {'بله' if db_exists else 'خیر'}\n"
        f"📏 حجم دیتابیس: `{db_size}` بایت\n\n"
        f"👥 کاربران ذخیره‌شده: `{users_count}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 **راهنمای Railway:**\n"
        "اگه می‌خوای دیتابیس دائمی بمونه:\n"
        "1️⃣  برو تو پروژه Railway\n"
        "2️⃣  روی سرویس کلیک کن → Settings → Volumes\n"
        "3️⃣  Add Volume بزن\n"
        "4️⃣  Mount Path رو بذار: `/data`\n"
        "5️⃣  Redeploy کن\n\n"
        "بدون Volume، دیتابیس هر بار ری‌استارت پاک می‌شه."
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.id != ADMIN_ID:
        return
    status = "🟢 فعال" if BOT_ENABLED else "🔴 تعمیرات"
    total = len(db_get_all_users())
    bot.send_message(
        message.chat.id,
        f"╭──────────────────────╮\n"
        f"   🛡️ **پنل مدیریت**\n"
        f"╰──────────────────────╯\n\n"
        f"⚙️ وضعیت: {status}\n"
        f"👥 کاربران کل: `{total}`\n"
        f"✅ کاربران فعال: `{len(USER_SESSIONS)}`\n"
        f"🚫 بن‌شده: `{len(BANNED_USERS)}`",
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


# ================== Callback ها ==================
@bot.callback_query_handler(func=lambda call: call.data == "cancel_op")
def cb_cancel_op(call):
    chat_id = call.message.chat.id
    clear_cancel(chat_id)
    clear_waiting(chat_id)
    CANCEL_FLAGS.add(chat_id)
    bot.answer_callback_query(call.id, "🛑 عملیات لغو شد")
    try:
        bot.edit_message_text(
            "🛑 **عملیات لغو شد.**",
            chat_id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=main_menu(chat_id),
        )
    except Exception:
        pass
    bot.send_message(chat_id, "برای شروع دوباره، دکمه 🚀 رو بزن.", reply_markup=main_menu(chat_id))


@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def cb_back_main(call):
    bot.answer_callback_query(call.id)
    clear_waiting(call.message.chat.id)
    bot.edit_message_text(
        "🏠 **منوی اصلی**\n\nیکی از گزینه‌های زیر رو انتخاب کن:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
        reply_markup=main_menu(call.message.chat.id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "create")
def cb_create(call):
    if not BOT_ENABLED and call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🔧 ربات در تعمیرات است", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    clear_cancel(call.message.chat.id)
    clear_waiting(call.message.chat.id)
    handle_create(call.message)


@bot.callback_query_handler(func=lambda call: call.data == "change_token")
def cb_change_token(call):
    if not BOT_ENABLED and call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🔧 ربات در تعمیرات است", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    USER_SESSIONS.pop(call.message.chat.id, None)
    clear_cancel(call.message.chat.id)
    WAITING_FOR[call.message.chat.id] = "token"
    bot.send_message(
        call.message.chat.id,
        "🔑 توکن جدید رو بفرست:\n\n💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "contact_admin")
def cb_contact_admin(call):
    bot.answer_callback_query(call.id)
    clear_cancel(call.message.chat.id)
    WAITING_FOR[call.message.chat.id] = "contact"
    bot.send_message(
        call.message.chat.id,
        "✍️ پیام خودت رو بنویس، مستقیم به دست سازنده می‌رسه:\n\n💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup(),
    )


# ================== پنل ادمین ==================
@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def cb_admin_panel(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ دسترسی نداری", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    status = "🟢 فعال" if BOT_ENABLED else "🔴 تعمیرات"
    total = len(db_get_all_users())
    bot.edit_message_text(
        f"╭──────────────────────╮\n"
        f"   🛡️ **پنل مدیریت**\n"
        f"╰──────────────────────╯\n\n"
        f"⚙️ وضعیت: {status}\n"
        f"👥 کاربران کل: `{total}`\n"
        f"✅ کاربران فعال: `{len(USER_SESSIONS)}`\n"
        f"🚫 بن‌شده: `{len(BANNED_USERS)}`",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def cb_admin_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    total = len(db_get_all_users())
    bot.edit_message_text(
        f"📊 **آمار کامل ربات**\n\n"
        f"👥 کاربران کل: `{total}`\n"
        f"✅ کاربران فعال: `{len(USER_SESSIONS)}`\n"
        f"🚫 بن‌شده: `{len(BANNED_USERS)}`\n"
        f"⚙️ وضعیت: {'🟢 فعال' if BOT_ENABLED else '🔴 تعمیرات'}",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_list_users")
def cb_admin_list_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    users = db_get_all_users()
    if not users:
        text = "📭 هیچ کاربری وجود ندارد."
    else:
        lines = ["📋 **کاربران:**\n"]
        for cid in users[:50]:
            u = db_get_user(cid)
            uname = f"@{u['username']}" if u and u.get("username") else "ندارد"
            lines.append(f"• `{cid}` | {uname}")
        if len(users) > 50:
            lines.append(f"\n... و {len(users) - 50} کاربر دیگر")
        text = "\n".join(lines)
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def cb_admin_broadcast(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    clear_cancel(call.message.chat.id)
    WAITING_FOR[call.message.chat.id] = "broadcast"
    bot.send_message(
        call.message.chat.id,
        "📢 **پیام همگانی**\n\n"
        "متن پیام رو بفرست. این پیام به **همه‌ی کاربرانی که ربات رو استارت کردن** ارسال می‌شه.\n\n"
        "💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_maintenance")
def cb_admin_maintenance(call):
    global BOT_ENABLED
    if call.message.chat.id != ADMIN_ID:
        return
    BOT_ENABLED = not BOT_ENABLED
    db_set_state("bot_enabled", BOT_ENABLED)
    status = "🟢 روشن" if BOT_ENABLED else "🔴 خاموش (تعمیرات)"
    bot.answer_callback_query(call.id, f"وضعیت: {status}")
    bot.edit_message_text(
        f"╭──────────────────────╮\n"
        f"   🛡️ **پنل مدیریت**\n"
        f"╰──────────────────────╯\n\n"
        f"⚙️ وضعیت جدید: {status}",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_ban_user")
def cb_admin_ban_user(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    WAITING_FOR[call.message.chat.id] = "ban"
    bot.send_message(
        call.message.chat.id,
        "🆔 آیدی عددی کاربر را بفرست:\n\n💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_unban_user")
def cb_admin_unban_user(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    WAITING_FOR[call.message.chat.id] = "unban"
    bot.send_message(
        call.message.chat.id,
        "🆔 آیدی عددی کاربر را بفرست:\n\n💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup(),
    )


# ================== هندلرهای ادمین ==================
def handle_ban_user(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        clear_waiting(message.chat.id)
        return
    try:
        target_id = int(message.text.strip())
    except (ValueError, AttributeError):
        bot.send_message(message.chat.id, "❌ آیدی نامعتبر.", reply_markup=admin_menu())
        clear_waiting(message.chat.id)
        return

    if target_id in BANNED_USERS:
        bot.send_message(message.chat.id, "⚠️ این کاربر قبلاً بن شده.", reply_markup=admin_menu())
        clear_waiting(message.chat.id)
        return

    sess = USER_SESSIONS.get(target_id) or restore_session(target_id)
    if not sess:
        bot.send_message(message.chat.id, "❌ این کاربر توکنی ثبت نکرده.", reply_markup=admin_menu())
        clear_waiting(message.chat.id)
        return

    session = sess["session"]
    account_id = sess["account_id"]
    workers = sess.get("workers", [])
    db_uuids = sess.get("db_uuids", [])

    msg = bot.send_message(
        message.chat.id,
        f"⏳ در حال پاک کردن منابع کاربر `{target_id}`...",
        parse_mode="Markdown",
    )

    deleted_w = 0
    for w in workers:
        res = delete_worker(session, account_id, w)
        if res.get("success"):
            deleted_w += 1

    deleted_db = 0
    for db in db_uuids:
        res = delete_d1_database(session, account_id, db)
        if res.get("success"):
            deleted_db += 1

    BANNED_USERS[target_id] = sess
    USER_SESSIONS.pop(target_id, None)
    db_set_ban(target_id, True)

    try:
        bot.send_message(target_id, "🚫 شما توسط مدیر بن شدید و تمام منابع‌تان حذف گردید.")
    except Exception:
        pass

    bot.edit_message_text(
        f"✅ کاربر `{target_id}` بن شد.\n\n"
        f"🗑️ ورکرهای حذف‌شده: `{deleted_w}`\n"
        f"🗄️ دیتابیس‌های حذف‌شده: `{deleted_db}`",
        message.chat.id,
        msg.message_id,
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )
    clear_waiting(message.chat.id)


def handle_unban_user(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        clear_waiting(message.chat.id)
        return
    try:
        target_id = int(message.text.strip())
    except (ValueError, AttributeError):
        bot.send_message(message.chat.id, "❌ آیدی نامعتبر.", reply_markup=admin_menu())
        clear_waiting(message.chat.id)
        return

    if target_id not in BANNED_USERS:
        db_set_ban(target_id, False)
        bot.send_message(
            message.chat.id,
            f"✅ کاربر `{target_id}` رفع بن شد (از دیتابیس).",
            parse_mode="Markdown",
            reply_markup=admin_menu(),
        )
        clear_waiting(message.chat.id)
        return

    BANNED_USERS.pop(target_id, None)
    db_set_ban(target_id, False)
    bot.send_message(
        message.chat.id,
        f"✅ کاربر `{target_id}` رفع بن شد.",
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )
    clear_waiting(message.chat.id)


def handle_broadcast(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        clear_waiting(message.chat.id)
        return
    if not message.text:
        bot.send_message(message.chat.id, "❌ فقط متن پشتیبانی می‌شه.", reply_markup=admin_menu())
        clear_waiting(message.chat.id)
        return

    users = db_get_all_users()
    if not users:
        bot.send_message(message.chat.id, "📭 هیچ کاربری برای ارسال وجود ندارد.", reply_markup=admin_menu())
        clear_waiting(message.chat.id)
        return

    status_msg = bot.send_message(
        message.chat.id,
        f"📤 در حال ارسال به `{len(users)}` کاربر...\n\n⏳ لطفاً صبر کن...",
        parse_mode="Markdown",
    )

    success = failed = blocked = 0
    for i, uid in enumerate(users):
        try:
            bot.send_message(uid, message.text)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 403:
                blocked += 1
            else:
                failed += 1
        except Exception:
            failed += 1

        if (i + 1) % 25 == 0:
            time.sleep(1.5)

        if (i + 1) % 50 == 0:
            try:
                bot.edit_message_text(
                    f"📤 در حال ارسال...\n\n"
                    f"✅ موفق: `{success}`\n"
                    f"🚫 بلاک: `{blocked}`\n"
                    f"❌ خطا: `{failed}`\n"
                    f"📊 پیشرفت: `{i + 1}/{len(users)}`",
                    message.chat.id,
                    status_msg.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    bot.edit_message_text(
        f"╭──────────────────────╮\n"
        f"   📢 **ارسال کامل شد**\n"
        f"╰──────────────────────╯\n\n"
        f"👥 کل کاربران: `{len(users)}`\n"
        f"✅ موفق: `{success}`\n"
        f"🚫 بلاک‌شده: `{blocked}`\n"
        f"❌ خطا: `{failed}`",
        message.chat.id,
        status_msg.message_id,
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )
    clear_waiting(message.chat.id)


def handle_contact_message(message):
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        clear_waiting(message.chat.id)
        return
    if not message.text:
        bot.send_message(message.chat.id, "❌ فقط متن پشتیبانی می‌شه.")
        clear_waiting(message.chat.id)
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "ندارد"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "ناشناس"

    try:
        sent = bot.send_message(
            ADMIN_ID,
            f"📩 **پیام جدید از کاربر**\n\n"
            f"👤 نام: {full_name}\n"
            f"🔗 یوزرنیم: {username}\n"
            f"🆔 آیدی: `{message.chat.id}`\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"{message.text}\n\n"
            f"💡 **برای پاسخ، روی همین پیام ریپلای کن.**",
            parse_mode="Markdown",
        )
        ADMIN_REPLY_MAP[sent.message_id] = message.chat.id
        bot.send_message(
            message.chat.id,
            "✅ پیامت به دست سازنده رسید. ممنون! 🌹",
            reply_markup=main_menu(message.chat.id),
        )
    except Exception:
        bot.send_message(
            message.chat.id,
            "⚠️ ارسال پیام با خطا مواجه شد. بعداً تلاش کن.",
            reply_markup=main_menu(message.chat.id),
        )
    clear_waiting(message.chat.id)


# ================== پاسخ ادمین با ریپلای ==================
@bot.message_handler(
    func=lambda m: m.chat.id == ADMIN_ID and m.reply_to_message is not None,
    content_types=["text"],
)
def handle_admin_reply(message):
    replied_id = message.reply_to_message.message_id
    if replied_id not in ADMIN_REPLY_MAP:
        bot.send_message(message.chat.id, "⚠️ این پیام قابل پاسخ نیست.")
        return
    target_chat_id = ADMIN_REPLY_MAP[replied_id]
    try:
        bot.send_message(
            target_chat_id,
            "📬 **پاسخ از سازنده:**\n\n"
            "━━━━━━━━━━━━━━\n"
            f"{message.text}\n"
            "━━━━━━━━━━━━━━",
            parse_mode="Markdown",
        )
        bot.send_message(message.chat.id, "✅ پاسخ برات ارسال شد.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ارسال پاسخ ناموفق بود: {e}")


# ================== منطق ساخت ==================
def handle_create(message):
    chat_id = message.chat.id
    clear_cancel(chat_id)

    u = db_get_user(chat_id)
    if u and u.get("is_banned"):
        bot.send_message(chat_id, "🚫 شما بن هستید.")
        return

    sess = USER_SESSIONS.get(chat_id) or restore_session(chat_id)
    if not sess:
        bot.send_message(
            chat_id,
            "❌ هنوز توکنی ثبت نکردی.\n"
            "اول توکن رو بفرست یا از دکمه «🔑 ساخت توکن کلودفلر» استفاده کن.",
            reply_markup=main_menu(chat_id),
        )
        return

    status = bot.send_message(
        chat_id,
        "⏳ در حال ساخت منابع...\n🔧 این کار چند ثانیه طول می‌کشه.",
        reply_markup=cancel_markup(),
    )

    session = sess["session"]
    account_id = sess["account_id"]
    db_uuid = None

    try:
        if is_cancelled(chat_id):
            raise Exception("cancelled")

        bot.edit_message_text(
            "📥 دریافت کد ورکر از گیت‌هاب...",
            chat_id,
            status.message_id,
            parse_mode="Markdown",
            reply_markup=cancel_markup(),
        )
        worker_code = fetch_worker_code()

        if is_cancelled(chat_id):
            raise Exception("cancelled")

        db_name = generate_random_name("db")
        bot.edit_message_text(
            f"📦 ساخت دیتابیس D1...\n`{db_name}`",
            chat_id,
            status.message_id,
            parse_mode="Markdown",
            reply_markup=cancel_markup(),
        )
        db_uuid = create_d1_database(session, account_id, db_name)
        sess.setdefault("db_uuids", []).append(db_uuid)

        if is_cancelled(chat_id):
            delete_d1_database(session, account_id, db_uuid)
            if db_uuid in sess.get("db_uuids", []):
                sess["db_uuids"].remove(db_uuid)
            raise Exception("cancelled")

        worker_name = generate_random_name("worker")
        bot.edit_message_text(
            f"📦 دیتابیس ساخته شد ✅\n\n"
            f"⚙️ دیپلوی ورکر...\n`{worker_name}`",
            chat_id,
            status.message_id,
            parse_mode="Markdown",
            reply_markup=cancel_markup(),
        )
        upload_worker(session, account_id, worker_name, db_uuid, worker_code)
        sess.setdefault("workers", []).append(worker_name)

        db_save_session(
            chat_id,
            account_id,
            sess["account_name"],
            sess.get("token", ""),
            sess.get("workers", []),
            sess.get("db_uuids", []),
        )

        subdomain = get_worker_subdomain(session, account_id)
        if subdomain:
            worker_url = f"https://{worker_name}.{subdomain}.workers.dev"
        else:
            worker_url = f"https://{worker_name}.workers.dev"

        final_text = (
            "╭──────────────────────╮\n"
            "    ✅ **عملیات موفق**\n"
            "╰──────────────────────╯\n\n"
            f"🏢 **اکانت:** `{sess['account_name']}`\n"
            f"🆔 **Account ID:** `{account_id}`\n\n"
            "━━━ 🗄️ **دیتابیس D1** ━━━\n"
            f"📛 نام: `{db_name}`\n"
            f"🔑 UUID: `{db_uuid}`\n\n"
            "━━━ ⚙️ **Worker** ━━━\n"
            f"📛 نام: `{worker_name}`\n"
            f"🔗 بایندینگ: `DB` → `{db_name}`\n"
            f"🌐 لینک: {worker_url}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🎉 همه چیز آماده‌ست!"
        )
        bot.edit_message_text(
            final_text,
            chat_id,
            status.message_id,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=main_menu(chat_id),
        )
        clear_cancel(chat_id)

    except Exception as e:
        err_text = str(e)
        if err_text == "cancelled":
            bot.edit_message_text(
                "🛑 **عملیات توسط شما لغو شد.**\n\nمنابع نیمه‌کاره پاک شدن.",
                chat_id,
                status.message_id,
                parse_mode="Markdown",
                reply_markup=main_menu(chat_id),
            )
        elif "Code generation from strings disallowed" in err_text:
            bot.edit_message_text(
                "❌ **خطای امنیتی Cloudflare**\n\n"
                "کد ورکر از `eval()` / `new Function()` استفاده می‌کنه.\n"
                "با وجود فلگ `allow_eval_during_startup` هنوز مشکل وجود داره.\n\n"
                "❌ منابع نیمه‌کاره پاک شدن.",
                chat_id,
                status.message_id,
                parse_mode="Markdown",
                reply_markup=main_menu(chat_id),
            )
            try:
                if db_uuid:
                    delete_d1_database(session, account_id, db_uuid)
                    if db_uuid in sess.get("db_uuids", []):
                        sess["db_uuids"].remove(db_uuid)
            except Exception:
                pass
        else:
            bot.edit_message_text(
                f"❌ **خطا در ساخت:**\n\n`{err_text}`",
                chat_id,
                status.message_id,
                parse_mode="Markdown",
                reply_markup=main_menu(chat_id),
            )
            try:
                if db_uuid:
                    delete_d1_database(session, account_id, db_uuid)
                    if db_uuid in sess.get("db_uuids", []):
                        sess["db_uuids"].remove(db_uuid)
            except Exception:
                pass
        clear_cancel(chat_id)


# ================== هندلر توکن ==================
def handle_token_input(message):
    chat_id = message.chat.id
    if is_cancelled(chat_id):
        clear_cancel(chat_id)
        clear_waiting(chat_id)
        return

    u = db_get_user(chat_id)
    if u and u.get("is_banned"):
        bot.send_message(chat_id, "🚫 شما بن هستید.")
        clear_waiting(chat_id)
        return

    if not BOT_ENABLED and chat_id != ADMIN_ID:
        bot.send_message(chat_id, "🔧 ربات در تعمیرات است.")
        clear_waiting(chat_id)
        return

    token = (message.text or "").strip()
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    if len(token) < 20:
        bot.send_message(chat_id, "❌ توکن نامعتبره. دوباره بفرست:")
        return

    status = bot.send_message(
        chat_id,
        "🔐 در حال احراز هویت با Cloudflare...",
        reply_markup=cancel_markup(),
    )

    try:
        user_info = verify_token_and_get_account(token)
        user_info["username"] = message.from_user.username or str(chat_id)
        USER_SESSIONS[chat_id] = user_info

        db_save_session(
            chat_id,
            user_info["account_id"],
            user_info["account_name"],
            token,
            [],
            [],
        )

        bot.edit_message_text(
            "╭──────────────────────╮\n"
            "   ✅ **احراز هویت موفق**\n"
            "╰──────────────────────╯\n\n"
            f"🏢 **اکانت:** `{user_info['account_name']}`\n"
            f"🆔 **Account ID:** `{user_info['account_id']}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 حالا دکمه ساخت رو بزن تا شروع کنیم!",
            chat_id,
            status.message_id,
            parse_mode="Markdown",
            reply_markup=main_menu(chat_id),
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ **احراز هویت ناموفق:**\n\n`{e}`\n\n"
            "دوباره تلاش کن یا /start بزن.",
            chat_id,
            status.message_id,
            parse_mode="Markdown",
            reply_markup=main_menu(chat_id),
        )
    clear_waiting(chat_id)


# ================== هندلر پیام‌های متنی ==================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_all_text(message):
    chat_id = message.chat.id
    waiting = WAITING_FOR.get(chat_id)

    if waiting == "token":
        handle_token_input(message)
    elif waiting == "contact":
        handle_contact_message(message)
    elif waiting == "broadcast":
        handle_broadcast(message)
    elif waiting == "ban":
        handle_ban_user(message)
    elif waiting == "unban":
        handle_unban_user(message)
    else:
        text = (message.text or "").strip()
        if len(text) > 30 and ("cf" in text.lower() or text.startswith(("v1.", "CF", "eyJ"))):
            WAITING_FOR[chat_id] = "token"
            handle_token_input(message)
        else:
            bot.send_message(
                chat_id,
                "از منوی زیر استفاده کن یا توکن کلودفلر رو بفرست:",
                reply_markup=main_menu(chat_id),
            )


# ================== اجرا ==================
if __name__ == "__main__":
    init_db()
    BOT_ENABLED = db_get_state("bot_enabled", "True") == "True"

    print("╭──────────────────────────╮")
    print("      🤖 PX Deploy Bot")
    print("╰──────────────────────────╯")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print(f"⚙️  Status: {'Enabled' if BOT_ENABLED else 'Maintenance'}")
    print(f"📥 Worker source: {WORKER_CODE_URL}")
    print(f"💾 Data dir: {DATA_DIR}")
    print(f"🗄️  DB path: {DB_PATH}")
    print(f"📦 DB exists: {os.path.exists(DB_PATH)}")
    print("✅ Fixed: compatibility_date + allow_eval_during_startup + DB migration")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
