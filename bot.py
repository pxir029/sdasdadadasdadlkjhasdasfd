# -*- coding: utf-8 -*-
import os
import sys
import json
import uuid
import requests
import telebot
from telebot import types

# ================== تنظیمات ==================
BOT_TOKEN = "8944694178:AAE3NZPRLpBjxRmfHLAxg0_gl9IxT-7nmkc"
CF_API_BASE = "https://api.cloudflare.com/client/v4"
ADMIN_ID = 7326030446

# لینک ساخت توکن کلودفلر با دسترسی‌های از پیش تنظیم‌شده
TOKEN_URL = (
    "https://dash.cloudflare.com/profile/api-tokens"
    "?permissionGroupKeys=%5B%7B%22key%22%3A%22account_settings%22%2C%22type%22%3A%22read%22%7D"
    "%2C%7B%22key%22%3A%22d1%22%2C%22type%22%3A%22edit%22%7D"
    "%2C%7B%22key%22%3A%22workers_scripts%22%2C%22type%22%3A%22edit%22%7D%5D"
    "&accountId=*&zoneId=all&name=PX%20Deploy"
)

# آدرس دریافت کد ورکر از گیت‌هاب (فایل هش‌شده)
WORKER_CODE_URL = "https://raw.githubusercontent.com/iran-px-panel/px_wokers/refs/heads/main/worker.js"

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ================== وضعیت سراسری ==================
USER_SESSIONS = {}
ALL_USERS = set()
BANNED_USERS = {}
BOT_ENABLED = True
ADMIN_REPLY_MAP = {}
CANCEL_FLAGS = set()

# ================== توابع Cloudflare ==================
def cf_session(token):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
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
        "workers": [],
        "db_uuids": []
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
    """دریافت کد ورکر از گیت‌هاب به صورت خام و بدون تغییر"""
    r = requests.get(WORKER_CODE_URL, timeout=25)
    if r.status_code != 200:
        raise Exception(f"دریافت کد از گیت‌هاب ناموفق بود: کد {r.status_code}")
    text = r.text
    if not text.strip():
        raise Exception("فایل ورکر خالی است")
    return text

def upload_worker(session, account_id, worker_name, db_uuid, worker_code):
    """آپلود ورکر با کد خام (بدون هیچ تغییر) و بایندینگ D1"""
    metadata = {
        "main_module": "worker.js",
        "bindings": [{"name": "DB", "type": "d1", "id": db_uuid}],
        "compatibility_date": "2024-01-01"
    }
    url = f"{CF_API_BASE}/accounts/{account_id}/workers/scripts/{worker_name}"
    token = session.headers["Authorization"].split()[-1]
    headers = {"Authorization": f"Bearer {token}"}
    files = {
        "metadata": (None, json.dumps(metadata), "application/json; charset=utf-8"),
        "worker.js": ("worker.js", worker_code.encode('utf-8'), "application/javascript+module")
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

def cancel_markup():
    kb = types.InlineKeyboardMarkup()
    kb.add(btn("❌ لغو عملیات", callback_data="cancel_op", style="danger"))
    return kb

# ================== ربات ==================
bot = telebot.TeleBot(BOT_TOKEN)

def btn(text, callback_data=None, url=None, style=None):
    """
    ساخت دکمه Inline با پشتیبانی اختیاری از style.
    اگر نسخه‌ی pyTelegramBotAPI از style پشتیبانی نکند،
    بدون خطا و به صورت دکمه‌ی معمولی ساخته می‌شود.
    style می‌تواند یکی از این مقادیر باشد:
      "primary"  → آبی
      "success"  → سبز
      "danger"   → قرمز
    """
    kwargs = {}
    if url:
        kwargs['url'] = url
    else:
        kwargs['callback_data'] = callback_data
    if style:
        kwargs['style'] = style
    try:
        return types.InlineKeyboardButton(text, **kwargs)
    except TypeError:
        kwargs.pop('style', None)
        return types.InlineKeyboardButton(text, **kwargs)

def main_menu(chat_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("🚀 ساخت ورکر جدید", callback_data="create", style="primary"))
    kb.add(btn("🔑 ساخت توکن کلودفلر", url=TOKEN_URL, style="success"))
    kb.add(btn("🔄 تغییر توکن", callback_data="change_token", style="primary"))
    kb.add(btn("💬 پیام به سازنده", callback_data="contact_admin", style="primary"))
    if chat_id == ADMIN_ID:
        kb.add(btn("🛡️ پنل مدیریت", callback_data="admin_panel", style="danger"))
    return kb

def admin_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("👥 آمار کاربران", callback_data="admin_users", style="primary"))
    kb.add(btn("📋 لیست کاربران", callback_data="admin_list_users", style="primary"))
    kb.add(btn("🛑 تعمیرات (خاموش/روشن)", callback_data="admin_maintenance", style="danger"))
    kb.add(btn("🚫 بن کاربر", callback_data="admin_ban_user", style="danger"))
    kb.add(btn("✅ رفع بن", callback_data="admin_unban_user", style="success"))
    kb.add(btn("🔙 بازگشت", callback_data="back_main", style="primary"))
    return kb

# ================== دستورات ==================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    USER_SESSIONS.pop(message.chat.id, None)
    clear_cancel(message.chat.id)
    ALL_USERS.add(message.chat.id)

    if message.chat.id in BANNED_USERS:
        bot.send_message(message.chat.id, "🚫 ســـــیک کــن ــــ مادر جندگی؟")
        return

    if not BOT_ENABLED and message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "🔧 ربات در حال تعمیرات است. لطفاً بعداً تلاش کنید.")
        return

    bot.send_message(
        message.chat.id,
        "╭──────────────────────────╮\n"
        "     ⚡️ **PX Deploy** ⚡️\n"
        "╰──────────────────────────╯\n\n"
        "سلام گل! 👋\n"
        " (😁ربات توی همین چند دقیقه به یه مشکلی خورده بود) \n\n"
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
        parse_mode='Markdown',
        disable_web_page_preview=True,
        reply_markup=main_menu(message.chat.id)
    )

@bot.message_handler(commands=['leave'])
def cmd_leave(message):
    clear_cancel(message.chat.id)
    CANCEL_FLAGS.add(message.chat.id)
    bot.send_message(
        message.chat.id,
        "🛑 **عملیات لغو شد.**\n\nهر وقت خواستی دوباره شروع کنی، دکمه 🚀 رو بزن.",
        parse_mode='Markdown',
        reply_markup=main_menu(message.chat.id)
    )

@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if message.chat.id != ADMIN_ID:
        return
    status = "🟢 فعال" if BOT_ENABLED else "🔴 تعمیرات"
    bot.send_message(
        message.chat.id,
        f"╭──────────────────────╮\n"
        f"   🛡️ **پنل مدیریت**\n"
        f"╰──────────────────────╯\n\n"
        f"⚙️ وضعیت: {status}\n"
        f"👥 کاربران کل: `{len(ALL_USERS)}`\n"
        f"✅ کاربران فعال: `{len(USER_SESSIONS)}`\n"
        f"🚫 بن‌شده: `{len(BANNED_USERS)}`",
        parse_mode='Markdown',
        reply_markup=admin_menu()
    )

# ================== Callback ها ==================
@bot.callback_query_handler(func=lambda call: call.data == "cancel_op")
def cb_cancel_op(call):
    chat_id = call.message.chat.id
    clear_cancel(chat_id)
    CANCEL_FLAGS.add(chat_id)
    bot.answer_callback_query(call.id, "🛑 عملیات لغو شد", show_alert=False)
    try:
        bot.edit_message_text(
            "🛑 **عملیات لغو شد.**",
            chat_id, call.message.message_id,
            parse_mode='Markdown',
            reply_markup=main_menu(chat_id)
        )
    except Exception:
        pass
    bot.send_message(chat_id, "برای شروع دوباره، دکمه 🚀 رو بزن.", reply_markup=main_menu(chat_id))

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def cb_back_main(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "🏠 **منوی اصلی**\n\nیکی از گزینه‌های زیر رو انتخاب کن:",
        call.message.chat.id, call.message.message_id,
        parse_mode='Markdown',
        reply_markup=main_menu(call.message.chat.id)
    )

@bot.callback_query_handler(func=lambda call: call.data == "create")
def cb_create(call):
    if not BOT_ENABLED and call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🔧 ربات در تعمیرات است", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    clear_cancel(call.message.chat.id)
    handle_create(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "change_token")
def cb_change_token(call):
    if not BOT_ENABLED and call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🔧 ربات در تعمیرات است", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    USER_SESSIONS.pop(call.message.chat.id, None)
    clear_cancel(call.message.chat.id)
    bot.send_message(
        call.message.chat.id,
        "🔑 توکن جدید رو بفرست:\n\n💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup()
    )
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_token_input)

@bot.callback_query_handler(func=lambda call: call.data == "contact_admin")
def cb_contact_admin(call):
    bot.answer_callback_query(call.id)
    clear_cancel(call.message.chat.id)
    bot.send_message(
        call.message.chat.id,
        "✍️ پیام خودت رو بنویس، مستقیم به دست سازنده می‌رسه:\n\n💡 برای لغو، /leave رو بزن.",
        reply_markup=cancel_markup()
    )
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_contact_message)

# ================== پنل ادمین ==================
@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def cb_admin_panel(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ دسترسی نداری", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    status = "🟢 فعال" if BOT_ENABLED else "🔴 تعمیرات"
    bot.edit_message_text(
        f"╭──────────────────────╮\n"
        f"   🛡️ **پنل مدیریت**\n"
        f"╰──────────────────────╯\n\n"
        f"⚙️ وضعیت: {status}\n"
        f"👥 کاربران کل: `{len(ALL_USERS)}`\n"
        f"✅ کاربران فعال: `{len(USER_SESSIONS)}`\n"
        f"🚫 بن‌شده: `{len(BANNED_USERS)}`",
        call.message.chat.id, call.message.message_id,
        parse_mode='Markdown',
        reply_markup=admin_menu()
    )

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def cb_admin_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"📊 **آمار کامل ربات**\n\n"
        f"👥 کاربران کل: `{len(ALL_USERS)}`\n"
        f"✅ کاربران فعال: `{len(USER_SESSIONS)}`\n"
        f"🚫 بن‌شده: `{len(BANNED_USERS)}`\n"
        f"⚙️ وضعیت: {'🟢 فعال' if BOT_ENABLED else '🔴 تعمیرات'}",
        call.message.chat.id, call.message.message_id,
        parse_mode='Markdown', reply_markup=admin_menu()
    )

@bot.callback_query_handler(func=lambda call: call.data == "admin_list_users")
def cb_admin_list_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    if not USER_SESSIONS:
        text = "📭 هیچ کاربر فعالی وجود ندارد."
    else:
        lines = ["📋 **کاربران فعال:**\n"]
        for cid, sess in USER_SESSIONS.items():
            uname = sess.get("username", "?")
            lines.append(f"• `{cid}` | @{uname}\n  └ اکانت: {sess['account_name']}")
        text = "\n".join(lines)
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                          parse_mode='Markdown', reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda call: call.data == "admin_maintenance")
def cb_admin_maintenance(call):
    global BOT_ENABLED
    if call.message.chat.id != ADMIN_ID:
        return
    BOT_ENABLED = not BOT_ENABLED
    status = "🟢 روشن" if BOT_ENABLED else "🔴 خاموش (تعمیرات)"
    bot.answer_callback_query(call.id, f"وضعیت: {status}")
    bot.edit_message_text(
        f"╭──────────────────────╮\n"
        f"   🛡️ **پنل مدیریت**\n"
        f"╰──────────────────────╯\n\n"
        f"⚙️ وضعیت جدید: {status}",
        call.message.chat.id, call.message.message_id,
        parse_mode='Markdown', reply_markup=admin_menu()
    )

@bot.callback_query_handler(func=lambda call: call.data == "admin_ban_user")
def cb_admin_ban_user(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🆔 آیدی عددی کاربر را بفرست:\n\n💡 برای لغو، /leave رو بزن.")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_ban_user)

@bot.callback_query_handler(func=lambda call: call.data == "admin_unban_user")
def cb_admin_unban_user(call):
    if call.message.chat.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🆔 آیدی عددی کاربر را بفرست:\n\n💡 برای لغو، /leave رو بزن.")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_unban_user)

# ================== هندلرهای ادمین ==================
def handle_ban_user(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ آیدی نامعتبر.", reply_markup=admin_menu())
        return

    if target_id in BANNED_USERS:
        bot.send_message(message.chat.id, "⚠️ این کاربر قبلاً بن شده.", reply_markup=admin_menu())
        return
    if target_id not in USER_SESSIONS:
        bot.send_message(message.chat.id, "❌ این کاربر توکنی ثبت نکرده.", reply_markup=admin_menu())
        return

    sess = USER_SESSIONS[target_id]
    session = sess["session"]
    account_id = sess["account_id"]
    workers = sess.get("workers", [])
    db_uuids = sess.get("db_uuids", [])

    msg = bot.send_message(message.chat.id, f"⏳ در حال پاک کردن منابع کاربر `{target_id}`...", parse_mode='Markdown')

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

    try:
        bot.send_message(target_id, "🚫 شما توسط مدیر بن شدید و تمام منابع‌تان حذف گردید.")
    except Exception:
        pass

    bot.edit_message_text(
        f"✅ کاربر `{target_id}` بن شد.\n\n"
        f"🗑️ ورکرهای حذف‌شده: `{deleted_w}`\n"
        f"🗄️ دیتابیس‌های حذف‌شده: `{deleted_db}`",
        message.chat.id, msg.message_id,
        parse_mode='Markdown', reply_markup=admin_menu()
    )

def handle_unban_user(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ آیدی نامعتبر.", reply_markup=admin_menu())
        return

    if target_id not in BANNED_USERS:
        bot.send_message(message.chat.id, "❌ این کاربر بن نبوده.", reply_markup=admin_menu())
        return

    BANNED_USERS.pop(target_id)
    bot.send_message(message.chat.id, f"✅ کاربر `{target_id}` رفع بن شد.", parse_mode='Markdown', reply_markup=admin_menu())

# ================== پیام به سازنده ==================
def handle_contact_message(message):
    if is_cancelled(message.chat.id):
        clear_cancel(message.chat.id)
        return
    if not message.text:
        bot.send_message(message.chat.id, "❌ فقط متن پشتیبانی می‌شه.")
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
            parse_mode='Markdown'
        )
        ADMIN_REPLY_MAP[sent.message_id] = message.chat.id
        bot.send_message(message.chat.id, "✅ پیامت به دست سازنده رسید. ممنون! 🌹", reply_markup=main_menu(message.chat.id))
    except Exception:
        bot.send_message(message.chat.id, "⚠️ ارسال پیام با خطا مواجه شد. بعداً تلاش کن.", reply_markup=main_menu(message.chat.id))

# ================== پاسخ ادمین با ریپلای ==================
@bot.message_handler(
    func=lambda m: m.chat.id == ADMIN_ID and m.reply_to_message is not None,
    content_types=['text']
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
            parse_mode='Markdown'
        )
        bot.send_message(message.chat.id, "✅ پاسخ برات ارسال شد.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ارسال پاسخ ناموفق بود: {e}")

# ================== منطق ساخت ==================
def handle_create(message):
    chat_id = message.chat.id
    clear_cancel(chat_id)

    if chat_id in BANNED_USERS:
        bot.send_message(chat_id, "🚫 شما بن هستید.")
        return

    if chat_id not in USER_SESSIONS:
        bot.send_message(
            chat_id,
            "❌ هنوز توکنی ثبت نکردی.\n"
            "اول توکن رو بفرست یا از دکمه «🔑 ساخت توکن کلودفلر» استفاده کن.",
            reply_markup=main_menu(chat_id)
        )
        return

    status = bot.send_message(
        chat_id,
        "⏳ در حال ساخت منابع...\n🔧 این کار چند ثانیه طول می‌کشه.",
        reply_markup=cancel_markup()
    )
    sess = USER_SESSIONS[chat_id]
    session = sess["session"]
    account_id = sess["account_id"]

    db_uuid = None
    try:
        # مرحله ۱: دریافت کد ورکر از گیت‌هاب (بدون تغییر)
        if is_cancelled(chat_id):
            raise Exception("cancelled")
        bot.edit_message_text(
            "📥 دریافت کد ورکر از گیت‌هاب...",
            chat_id, status.message_id, parse_mode='Markdown',
            reply_markup=cancel_markup()
        )
        worker_code = fetch_worker_code()

        # مرحله ۲: ساخت D1
        if is_cancelled(chat_id):
            raise Exception("cancelled")
        db_name = generate_random_name("db")
        bot.edit_message_text(
            f"📦 ساخت دیتابیس D1...\n`{db_name}`",
            chat_id, status.message_id, parse_mode='Markdown',
            reply_markup=cancel_markup()
        )
        db_uuid = create_d1_database(session, account_id, db_name)
        sess.setdefault("db_uuids", []).append(db_uuid)

        # مرحله ۳: آپلود ورکر با کد خام
        if is_cancelled(chat_id):
            delete_d1_database(session, account_id, db_uuid)
            sess["db_uuids"].remove(db_uuid)
            raise Exception("cancelled")

        worker_name = generate_random_name("worker")
        bot.edit_message_text(
            f"📦 دیتابیس ساخته شد ✅\n\n"
            f"⚙️ دیپلوی ورکر...\n`{worker_name}`",
            chat_id, status.message_id, parse_mode='Markdown',
            reply_markup=cancel_markup()
        )
        upload_worker(session, account_id, worker_name, db_uuid, worker_code)
        sess.setdefault("workers", []).append(worker_name)

        # مرحله ۴: لینک نهایی
        subdomain = get_worker_subdomain(session, account_id)
        if subdomain:
            worker_url = f"https://{worker_name}.{subdomain}.workers.dev"
        else:
            worker_url = f"https://{worker_name}.<subdomain>.workers.dev"

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
            final_text, chat_id, status.message_id,
            parse_mode='Markdown', disable_web_page_preview=True,
            reply_markup=main_menu(chat_id)
        )
        clear_cancel(chat_id)

    except Exception as e:
        err_text = str(e)
        if err_text == "cancelled":
            bot.edit_message_text(
                "🛑 **عملیات توسط شما لغو شد.**\n\nمنابع نیمه‌کاره پاک شدن.",
                chat_id, status.message_id,
                parse_mode='Markdown', reply_markup=main_menu(chat_id)
            )
        elif "Code generation from strings disallowed" in err_text:
            bot.edit_message_text(
                "❌ **خطای امنیتی Cloudflare**\n\n"
                "کد ورکری که از گیت‌هاب گرفته شد، از `eval()` یا `new Function()` استفاده می‌کنه.\n"
                "Cloudflare Workers به‌دلایل امنیتی این کار رو **ممنوع** کرده.\n\n"
                "🔧 **راه‌حل:** باید فایل `worker.js` رو طوری تغییر بدی که از dynamic code generation استفاده نکنه.\n\n"
                "❌ منابع نیمه‌کاره پاک شدن.",
                chat_id, status.message_id,
                parse_mode='Markdown', reply_markup=main_menu(chat_id)
            )
            # پاک‌سازی دیتابیس نیمه‌کاره
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
                chat_id, status.message_id,
                parse_mode='Markdown', reply_markup=main_menu(chat_id)
            )
        clear_cancel(chat_id)

# ================== هندلر توکن ==================
@bot.message_handler(func=lambda m: True)
def handle_token_input(message):
    chat_id = message.chat.id

    if is_cancelled(chat_id):
        clear_cancel(chat_id)
        return

    if chat_id in BANNED_USERS:
        bot.send_message(chat_id, "🚫 شما بن هستید.")
        return

    if not BOT_ENABLED and chat_id != ADMIN_ID:
        bot.send_message(chat_id, "🔧 ربات در تعمیرات است.")
        return

    token = (message.text or "").strip()

    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    if len(token) < 20:
        bot.send_message(chat_id, "❌ توکن نامعتبره. دوباره بفرست:")
        return

    status = bot.send_message(chat_id, "🔐 در حال احراز هویت با Cloudflare...", reply_markup=cancel_markup())

    try:
        user_info = verify_token_and_get_account(token)
        user_info["username"] = message.from_user.username or str(chat_id)
        USER_SESSIONS[chat_id] = user_info
        ALL_USERS.add(chat_id)

        bot.edit_message_text(
            "╭──────────────────────╮\n"
            "   ✅ **احراز هویت موفق**\n"
            "╰──────────────────────╯\n\n"
            f"🏢 **اکانت:** `{user_info['account_name']}`\n"
            f"🆔 **Account ID:** `{user_info['account_id']}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 حالا دکمه ساخت رو بزن تا شروع کنیم!",
            chat_id, status.message_id,
            parse_mode='Markdown',
            reply_markup=main_menu(chat_id)
        )

    except Exception as e:
        bot.edit_message_text(
            f"❌ **احراز هویت ناموفق:**\n\n`{e}`\n\n"
            "دوباره تلاش کن یا /start بزن.",
            chat_id, status.message_id,
            parse_mode='Markdown',
            reply_markup=main_menu(chat_id)
        )

# ================== اجرا ==================
if __name__ == "__main__":
    print("╭──────────────────────────╮")
    print("      🤖 PX Deploy Bot")
    print("╰──────────────────────────╯")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print(f"⚙️  Status: {'Enabled' if BOT_ENABLED else 'Maintenance'}")
    print(f"📥 Worker source: {WORKER_CODE_URL}")
    bot.infinity_polling()
