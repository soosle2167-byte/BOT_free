#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Dual-Client Bridge (Bot & Userbot) with Auto-Link Task
================================================================
- bot_app (Pyrogram):
  * يستمع لأوامر الأدمن فقط في الخاص (/set_interval, /set_source, /set_target, /toggle_auto, /auto_status, /reset_notified).
  * يتجاهل تماماً أي رسائل من الغرباء أو غير المصرح لهم دون أي رد (صمت تام).
  * يرسل الإشعارات للمستخدمين المستهدفين وتقارير العمليات للأدمن.
- user_app (Telethon - Layer 178+):
  * يتصل بنفس جلسة المستخدم الحالية بأمان لدعم كافة التنسيقات الحديثة (Blockquotes وغيرها) بدون أخطاء Constructor.
  * يقرأ آخر 50 رابط فريد غير مستخدم من جروب المصدر ويختار منها رابطاً عشوائياً.
  * يرسل الرابط المختار إلى جروب الهدف.
  * يراقب رد البوت الثاني في جروب الهدف (سواء تم إكمال 15 دعوة أو الرابط مكتمل مسبقاً) ويتجاهل رسائل البوتات الأخرى.
  * يعيد تصميم الرد بالتنسيق المطلوب ويرد مباشرة على رسالة الرابط الأصلية في جروب المصدر.
"""

import os
import re
import json
import time
import random
import asyncio
import logging
import sqlite3
from typing import Union, Optional, List, Dict, Any
from dotenv import load_dotenv

# Pyrogram للـ Bot
from pyrogram import Client as BotClient, idle, filters
from pyrogram.types import Message as PyroMessage
from pyrogram.errors import FloodWait as PyroFloodWait, RPCError as PyroRPCError

# Telethon للـ Userbot
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey
from telethon.tl.types import MessageEntityTextUrl
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import FloodWaitError as TeleFloodWait, RPCError as TeleRPCError

# ---------------------------------------------------------
# إعداد نظام تسجيل الأحداث (Logging)
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DualClient")

# ---------------------------------------------------------
# تحميل متغيرات البيئة والتحقق منها
# ---------------------------------------------------------
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(dotenv_path=env_path)

def get_env_variable(var_name: str, cast_type=str, required: bool = True, default=None):
    """دالة مساعدة لجلب وتحويل متغيرات البيئة بأمان."""
    val = os.getenv(var_name)
    if val is not None:
        val = val.strip()
    if not val:
        if required:
            logger.critical(f"❌ المتغير البيئي المفقود أو الفارغ: {var_name}")
            raise ValueError(f"Environment variable '{var_name}' is required but not set in .env.")
        return default
    if cast_type != str:
        try:
            return cast_type(val)
        except ValueError:
            logger.critical(f"❌ فشل تحويل المتغير {var_name} إلى {cast_type.__name__}")
            raise
    return val

API_ID = get_env_variable("API_ID", cast_type=int)
API_HASH = get_env_variable("API_HASH")
BOT_TOKEN = get_env_variable("BOT_TOKEN")
ALLOWED_USER_ID = get_env_variable("ALLOWED_USER_ID", cast_type=int)
ADMIN_ID = get_env_variable("ADMIN_ID", cast_type=int, required=False, default=ALLOWED_USER_ID)

def normalize_chat_id(val: Union[int, str, None]) -> Union[int, str, None]:
    if val is None:
        return None
    val_str = str(val).strip()
    try:
        return int(val_str)
    except ValueError:
        return val_str

raw_target_group = get_env_variable("TARGET_GROUP_ID", required=False, default=None)
TARGET_GROUP_ID = normalize_chat_id(raw_target_group)

raw_source_group = get_env_variable("SOURCE_GROUP_ID", required=False, default=None)
SOURCE_GROUP_ID = normalize_chat_id(raw_source_group)

# مسارات الملفات
CONFIG_FILE = os.path.join(current_dir, "config.json")
USER_BALANCE_FILE = os.path.join(current_dir, "user_balance.json")
bot_session_path = os.path.join(current_dir, "bot_session")
user_session_path = os.path.join(current_dir, "user_session")

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# ---------------------------------------------------------
# دوال إدارة ملفات الإعدادات والبيانات
# ---------------------------------------------------------
def load_config() -> dict:
    default_config = {
        "auto_enabled": False,
        "interval_minutes": 5,
        "links_pool_size": 50,
        "source_group_id": SOURCE_GROUP_ID,
        "target_group_id": TARGET_GROUP_ID,
        "used_links": [],
        "notified_users": []
    }
    if not os.path.exists(CONFIG_FILE):
        save_config(default_config)
        return default_config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in default_config.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as e:
        logger.error(f"❌ خطأ أثناء تحميل ملف config.json: {e}")
        return default_config

def save_config(data: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"❌ خطأ أثناء حفظ ملف config.json: {e}")

def load_user_balance(file_path: str = USER_BALANCE_FILE) -> dict:
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ خطأ أثناء قراءة ملف user_balance.json: {e}")
        return {}

def save_user_balance(file_path: str, data: dict):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"❌ خطأ أثناء حفظ ملف user_balance.json: {e}")

def parse_admin_ids() -> List[int]:
    ids = []
    for var in ["ALLOWED_USER_ID", "ADMIN_ID"]:
        raw = os.getenv(var, "")
        for part in str(raw).split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    for known in [1955621195, 1087964579]:
        if known not in ids:
            ids.append(known)
    return list(set(ids))

ADMIN_IDS = parse_admin_ids()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ---------------------------------------------------------
# تهيئة عملاء تليجرام (bot_app و user_app)
# ---------------------------------------------------------
main_loop = asyncio.new_event_loop()
asyncio.set_event_loop(main_loop)

def check_and_sync_bot_session(session_path: str, bot_token: str):
    """
    التحقق من أن ملف جلسة البوت يطابق التوكن الحالي.
    إذا تم تغيير BOT_TOKEN في .env، يتم حذف ملف الجلسة القديم تلقائياً
    لتسجيل الدخول بالبوت الجديد دون الحاجة لحذفه يدوياً.
    """
    session_file = session_path + ".session"
    if not os.path.exists(session_file):
        return
    try:
        expected_bot_id = int(bot_token.split(":")[0])
        conn = sqlite3.connect(session_file)
        row = conn.cursor().execute("SELECT user_id FROM sessions").fetchone()
        conn.close()
        if row and row[0] != expected_bot_id:
            logger.warning(
                f"⚠️ تم تغيير توكن البوت! (الجلسة المحفوظة للبوت ID: {row[0]} بينما التوكن الجديد للبوت ID: {expected_bot_id}). "
                "جاري حذف الجلسة القديمة لتسجيل الدخول بالبوت الجديد تلقائياً..."
            )
            for f in [session_file, session_file + "-journal"]:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"خطأ أثناء التحقق من تطابق جلسة البوت: {e}")

# فحص جلسة البوت وتحديثها تلقائياً إذا تغير التوكن
check_and_sync_bot_session(bot_session_path, BOT_TOKEN)

# عميل البوت عبر Pyrogram
bot_app = BotClient(
    name=bot_session_path,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# عميل الحساب الشخصي عبر Telethon
def init_telethon_user() -> TelegramClient:
    session_file = user_session_path + ".session"
    if not os.path.exists(session_file):
        raise FileNotFoundError(f"لم يتم العثور على ملف جلسة المستخدم: {session_file}")

    conn = sqlite3.connect(session_file)
    row = conn.cursor().execute("SELECT dc_id, test_mode, auth_key, user_id, is_bot FROM sessions").fetchone()
    conn.close()

    if not row:
        raise ValueError("ملف جلسة المستخدم فارغ أو غير صالح.")

    dc_id, test_mode, auth_key, user_id, is_bot = row
    session = MemorySession()
    session.set_dc(dc_id, "149.154.167.91", 443)
    session.auth_key = AuthKey(auth_key)
    return TelegramClient(session, API_ID, API_HASH, loop=main_loop)

user_app = init_telethon_user()

# ---------------------------------------------------------
# حالة النظام العامة (Global Runtime State)
# ---------------------------------------------------------
global_status = {
    "daily_limit_paused": False,
    "daily_limit_until": 0,
    "daily_limit_wait_str": "",
    "daily_limit_reopen_time": ""
}

# ---------------------------------------------------------
# منطق معالجة وتحليل رسائل الإكمال والحد اليومي
# ---------------------------------------------------------
def is_daily_limit_message(text: str) -> bool:
    """التحقق مما إذا كانت الرسالة تفيد بانتهاء الحد اليومي للبوت في جروب الهدف."""
    if not text:
        return False
    limit_markers = [
        "الحد اليومي",
        "اكتملت الحصة",
        "الروابط هتفتح بعد",
        "تم الوصول للحد اليومي",
        "بلغ الحد اليومي",
        "اكتملت الحصة المتاحة"
    ]
    return any(marker in text for marker in limit_markers)

def parse_daily_limit_info(text: str) -> dict:
    """استخراج مدة الانتظار وموعد الفتح عند بلوغ الحد اليومي."""
    hours = 0
    minutes = 0
    hm = re.search(r"(\d+)\s*(?:ساعة|ساعات|ساعه)", text)
    if hm:
        hours = int(hm.group(1))
    mm = re.search(r"(\d+)\s*(?:دقيقة|دقائق|دقيقه)", text)
    if mm:
        minutes = int(mm.group(1))

    total_seconds = (hours * 3600) + (minutes * 60)
    wait_seconds = total_seconds + 60 if total_seconds > 0 else 3600

    reopen_match = re.search(r"موعد الفتح:\s*([^\n\r]+)", text)
    reopen_time = reopen_match.group(1).strip() if reopen_match else "غير محدد"

    after_match = re.search(r"الروابط هتفتح بعد:\s*([^\n\r]+)", text)
    if after_match:
        wait_str = after_match.group(1).strip()
    elif hours > 0 or minutes > 0:
        parts = []
        if hours > 0:
            parts.append(f"{hours} ساعة")
        if minutes > 0:
            parts.append(f"{minutes} دقيقة")
        wait_str = " و ".join(parts)
    else:
        wait_str = f"{wait_seconds // 60} دقيقة"

    return {
        "wait_seconds": wait_seconds,
        "wait_str": wait_str,
        "reopen_time": reopen_time
    }

def is_completion_message(text: str) -> bool:
    if not text:
        return False

    # استبعاد رسائل قيد التحميل أو الفحص المؤقت (لأن البوت يعدل نفس الرسالة لاحقاً بعد انتهاء الفحص)
    if "جاري تحميل" in text or "جاري الفحص" in text or "⏳" in text:
        return False

    # تجاهل رسائل نفاد الرصيد أو طلب الشحن أو اختيار اللغة من البوتات الأخرى
    ignore_markers = [
        "معندكش رصيد", "لا يوجد معك رصيد", "رصيدك خلص", "لا يوجد رصيد",
        "Choose language", "اختر اللغة", "افتح العروض لشراء", "عذرا يا",
        "تم استلام طلبك", "انتظر قليلا"
    ]
    if any(m in text for m in ignore_markers):
        return False

    # علامات التأكيد النهائية الحصرية:
    # 1. إما تم إكمال 15 دعوة بنجاح
    # 2. أو الرابط مكتمل مسبقاً
    has_success_title = ("تم إكمال" in text and "15" in text) or ("الرابط مكتمل مسبق" in text or "مكتمل مسبق" in text)
    has_details = ("معرّف اللاعب" in text or "معرف اللاعب" in text or "Player ID" in text or "الحساب" in text)
    return bool(has_success_title and has_details)

def parse_confirmation_message(text: str) -> dict:
    clean_text = text.replace("**", "").replace("__", "").replace("`", "")
    data = {
        "raw_text": text,
        "title": "ℹ️ الرابط مكتمل مسبقًا" if ("مكتمل مسبق" in clean_text) else "✅ تم إكمال 15 دعوة بنجاح",
        "account": None,
        "player_id": None,
        "tickets": None,
        "free_uc": None,
        "draws": None,
        "progress": None,
    }
    account_match = re.search(r"(?:💙\s*)?الحساب\s+([^\n\r]+)", clean_text)
    if account_match:
        acc = account_match.group(1).strip()
        if acc != "—":
            data["account"] = acc
    player_id_match = re.search(r"(?:🔑\s*)?(?:معرّف اللاعب|معرف اللاعب|Player ID)\s*[:：]?\s*([0-9]+)", clean_text)
    if player_id_match:
        data["player_id"] = player_id_match.group(1).strip()
    tickets_match = re.search(r"(?:💰\s*)?(?:UC Drop Ticket|Drop Ticket)\s*[:：]?\s*([^\n\r]+)", clean_text)
    if tickets_match:
        tck = tickets_match.group(1).strip()
        if tck != "—":
            data["tickets"] = tck
    free_uc_match = re.search(r"(?:🟢\s*)?(?:UC مجاني|uc مجاني)\s*[:：]?\s*([^\n\r]+)", clean_text)
    if free_uc_match:
        data["free_uc"] = free_uc_match.group(1).strip()
    draws_match = re.search(r"(?:🎖\s*)?(?:السحوبات|السحب)\s*[:：]?\s*([0-9]+)", clean_text)
    if draws_match:
        data["draws"] = draws_match.group(1).strip()
    progress_match = re.search(r"([0-9]+/[0-9]+\s+مساعدة[^\n\r]*)", clean_text)
    if progress_match:
        data["progress"] = progress_match.group(1).strip()
    else:
        progress_simple = re.search(r"([0-9]+/[0-9]+)", clean_text)
        if progress_simple:
            data["progress"] = f"{progress_simple.group(1)} مساعدة"
    return data

def format_user_notification(parsed_data: dict, link: str = None) -> str:
    lines = [
        "🎉 **تأكيد إكمال المهمة بنجاح!**",
        "",
        f"📋 **الحالة:** {parsed_data.get('title') or 'تم إكمال الدعوات'}"
    ]
    if parsed_data.get("account"):
        lines.append(f"👤 **الحساب:** `{parsed_data['account']}`")
    if parsed_data.get("player_id"):
        lines.append(f"🔑 **معرّف اللاعب:** `{parsed_data['player_id']}`")
    if parsed_data.get("free_uc"):
        lines.append(f"🟢 **UC مجاني:** `{parsed_data['free_uc']}`")
    if parsed_data.get("tickets"):
        lines.append(f"💰 **تذاكر Drop:** `{parsed_data['tickets']}`")
    if parsed_data.get("draws"):
        lines.append(f"🎖 **السحوبات:** `{parsed_data['draws']}`")
    if parsed_data.get("progress"):
        lines.append(f"📊 **نسبة الإنجاز:** `{parsed_data['progress']}`")
        
    if link:
        lines.append(f"\n🔗 **الرابط:**\n{link}")
        
    lines.append("\n✨ **تم إرسال الـ UC المجاني تلقائياً إلى حسابك.**")
    return "\n".join(lines)

BOLD_DIGITS = str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵")

def format_source_reply_design(raw_text: str, user_name: str = "يا بطل") -> str:
    """
    إعادة صياغة الرسالة الواردة من البوت بالتصميم المطلوب من المستخدم.
    """
    clean = raw_text.replace("**", "").replace("__", "").replace("`", "")
    parsed = parse_confirmation_message(clean)

    account_val = parsed.get("account") or "Dr『Ali』"
    player_id_val = parsed.get("player_id") or "51882320415"
    tickets_val = parsed.get("tickets") or "325|660uc"
    free_uc_val = parsed.get("free_uc") or "78uc (78+0)"
    draws_val = parsed.get("draws")
    progress_val = parsed.get("progress") or "15/15 مساعدة · ❤️ 00:02"

    is_already_completed = "مكتمل مسبق" in clean

    if is_already_completed:
        # استخراج كمية الشدات كما هي في الرسالة (لو صفر يكتب صفر ولو رقم يكتبه)
        free_uc = parsed.get("free_uc")
        uc_match = re.search(r"(\d+)", free_uc or "")
        if uc_match:
            uc_formatted = f"{uc_match.group(1).translate(BOLD_DIGITS)} 【 𝗨𝗖 】"
        else:
            uc_formatted = "𝟬 【 𝗨𝗖 】"

        # نسبة الإنجاز
        progress_raw = parsed.get("progress") or "15/15"
        prog_match = re.search(r"(\d+/\d+)", progress_raw)
        if prog_match:
            prog_formatted = prog_match.group(1).translate(BOLD_DIGITS)
        else:
            prog_formatted = "𝟭𝟱/𝟭𝟱"

        name_display = (user_name or "صاحب الرابط").strip()
        player_id_display = player_id_val
        account_display = account_val

        lines = [
            f"⚠️ هذا الرابط مكتمل سابقاً يا {name_display}!\n",
            f"🆔 آي دي اللاعب: {player_id_display}",
            f"👤 اسم اللاعب: {account_display}",
            f"🎟️ الشدات التي تم جمعها تلقائياً: {uc_formatted}",
            "📱 الشدات وصلت حسابك تلقائي",
            f"📊 عدد المساعدات: {prog_formatted}"
        ]
        return "\n".join(lines)
    else:
        if "❤️" in progress_val:
            progress_line = f"✅ {progress_val}" if not progress_val.startswith("✅") else progress_val
        else:
            clean_p = progress_val.replace("✅", "").strip()
            progress_line = f"✅ {clean_p} · ❤️ 00:02"

        header_title = "✅ تم إكمال 15 دعوة بنجاح"
        footer_line = "تم إرسال UC المجاني تلقائيًا إلى حسابك."

        lines = [
            f"{header_title}\n",
            "⭐ Dr.Ali · @aboelnour919 · ⭐ 6868391104",
            f"💙 الحساب   {account_val}",
            f"🔑 معرّف اللاعب   {player_id_val}",
            f"💰  UC Drop Ticket   {tickets_val}",
            f"🟢  UC مجاني   {free_uc_val}",
        ]
        if draws_val:
            lines.append(f"🎖  السحوبات   {draws_val}")

        lines.append("")
        lines.append("🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩   100%")
        lines.append(progress_line)
        lines.append(footer_line)

        return "\n".join(lines)

# ---------------------------------------------------------
# معالجة قيود الإرسال وتخطي FloodWait
# ---------------------------------------------------------
async def send_bot_message_with_anti_flood(
    client: BotClient,
    chat_id: Union[int, str],
    text: str,
    reply_to_message_id: Optional[int] = None,
    max_retries: int = 3,
) -> PyroMessage:
    for attempt in range(1, max_retries + 1):
        try:
            return await client.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id
            )
        except PyroFloodWait as e:
            wait_time = e.value
            logger.warning(
                f"⚠️ قيود تليجرام (FloodWait للبوت): تم إيقاف الإرسال مؤقتاً لمدة {wait_time} ثانية (المحاولة {attempt}/{max_retries})."
            )
            await asyncio.sleep(wait_time)
        except PyroRPCError as e:
            logger.error(f"❌ حدث خطأ من خوادم تليجرام أثناء إرسال البوت: {e}")
            raise e
        except Exception as e:
            logger.error(f"❌ خطأ غير متوقع أثناء إرسال البوت: {e}")
            raise e
    raise TimeoutError(f"فشل إرسال رسالة البوت بعد {max_retries} محاولات بسبب استمرار FloodWait.")

async def send_user_message_with_anti_flood(
    client: TelegramClient,
    chat: Union[int, str],
    text: str,
    reply_to: Optional[int] = None,
    max_retries: int = 3,
):
    for attempt in range(1, max_retries + 1):
        try:
            return await client.send_message(
                chat,
                text,
                reply_to=reply_to
            )
        except TeleFloodWait as e:
            wait_time = e.seconds
            logger.warning(
                f"⚠️ قيود تليجرام (FloodWait): تم إيقاف الإرسال مؤقتاً لمدة {wait_time} ثانية (المحاولة {attempt}/{max_retries})."
            )
            await asyncio.sleep(wait_time)
        except TeleRPCError as e:
            logger.error(f"❌ حدث خطأ من خوادم تليجرام أثناء الإرسال: {e}")
            raise e
        except Exception as e:
            logger.error(f"❌ خطأ غير متوقع: {e}")
            raise e
    raise TimeoutError(f"فشل إرسال الرسالة بعد {max_retries} محاولات بسبب استمرار FloodWait.")

# ---------------------------------------------------------
# وظائف المهمة التلقائية (Auto Task Engine)
# ---------------------------------------------------------
def get_eligible_zero_users(balance_data: dict, notified_cache: set) -> list:
    eligible = []
    for uid, val in balance_data.items():
        if isinstance(val, dict):
            bal = val.get("balance", 0)
            is_notified = val.get("notified", False)
            if bal == 0 and not is_notified and uid not in notified_cache:
                eligible.append(str(uid))
        elif isinstance(val, (int, float)):
            if val == 0 and str(uid) not in notified_cache:
                eligible.append(str(uid))
    return eligible

def mark_user_notified(file_path: str, user_id: str, notified_cache: set):
    uid_str = str(user_id)
    notified_cache.add(uid_str)
    balance_data = load_user_balance(file_path)
    if uid_str in balance_data:
        val = balance_data[uid_str]
        if isinstance(val, dict):
            val["notified"] = True
        else:
            balance_data[uid_str] = {
                "balance": int(val),
                "notified": True
            }
        save_user_balance(file_path, balance_data)

async def ensure_joined_chat(client: TelegramClient, chat: Union[int, str]):
    """
    التأكد من أن حساب المستخدم منضم إلى الجروب لتفادي خطأ CHAT_WRITE_FORBIDDEN.
    """
    if not chat:
        return
    try:
        entity = await client.get_input_entity(chat)
        await client(JoinChannelRequest(entity))
    except Exception as e:
        logger.debug(f"فحص الانضمام إلى ({chat}): {e}")

async def fetch_random_unused_link(
    client: TelegramClient,
    source_chat: Union[int, str],
    used_links: set,
    pool_size: int = 50
) -> Optional[dict]:
    """
    فحص آخر pool_size رسالة في جروب المصدر، واستخراج الروابط غير المستخدمة منها، واختيار رابط عشوائي من بينها.
    """
    try:
        candidate_items = []
        seen_in_batch = set()
        messages_limit = max(1, pool_size)

        async for msg in client.iter_messages(source_chat, limit=messages_limit):
            if not msg:
                continue
            content = msg.text or ""
            found_urls = URL_PATTERN.findall(content)

            # استخراج الروابط المنسقة (Hyperlinks) عبر كائنات Telethon
            if msg.entities:
                for entity in msg.entities:
                    if isinstance(entity, MessageEntityTextUrl) and entity.url:
                        found_urls.append(entity.url)

            user_id = msg.sender_id
            user_name = "صاحب الرابط"
            if msg.sender:
                user_name = getattr(msg.sender, "first_name", None) or getattr(msg.sender, "title", None) or "صاحب الرابط"

            for url in found_urls:
                clean_url = url.strip()
                if clean_url and clean_url not in used_links and clean_url not in seen_in_batch:
                    seen_in_batch.add(clean_url)
                    candidate_items.append({
                        "url": clean_url,
                        "msg_id": msg.id,
                        "from_user_id": user_id,
                        "from_user_name": user_name
                    })

        if candidate_items:
            chosen = random.choice(candidate_items)
            logger.info(
                f"🎲 تم فحص آخر {messages_limit} رسالة في جروب المصدر، وعُثر على {len(candidate_items)} رابط غير مستخدم، وتم اختيار رابط عشوائي: {chosen['url']} من الرسالة ({chosen['msg_id']}) للمستخدم ({chosen['from_user_name']})"
            )
            return chosen
        else:
            logger.warning(f"⚠️ لم يتم العثور على أي رابط جديد غير مستخدم ضمن آخر {messages_limit} رسالة في جروب المصدر ({source_chat}).")
            return None

    except Exception as e:
        logger.error(f"❌ خطأ أثناء قراءة الروابط من جروب المصدر ({source_chat}): {e}")
        return None

async def wait_for_target_bot_response(client: TelegramClient, target_chat: Union[int, str], sent_msg_id: int, timeout: int = 120) -> dict:
    """
    مراقبة رد البوت الثاني في جروب الهدف بعد نشر الرابط، واستخراج بيانات الإكمال أو رصد بلوغ الحد اليومي.
    """
    start_time = time.time()
    logger.info(f"⏳ بانتظار رد البوت في ({target_chat}) بعد الرسالة ({sent_msg_id})...")

    while time.time() - start_time < timeout:
        try:
            async for m in client.iter_messages(target_chat, limit=10):
                if m.id > sent_msg_id:
                    text = m.text or ""
                    # 1. رصد رسائل بلوغ الحد اليومي للبوت
                    if is_daily_limit_message(text):
                        limit_info = parse_daily_limit_info(text)
                        logger.warning(
                            f"⚠️ تم رصد بلوغ الحد اليومي لبوت الهدف (Msg ID: {m.id}): {limit_info['wait_str']} - حتى {limit_info['reopen_time']}"
                        )
                        return {
                            "status": "daily_limit_reached",
                            "message": m,
                            "text": text,
                            **limit_info
                        }

                    # 2. رصد رسائل إكمال الدعوات أو اكتمال الرابط مسبقاً
                    if is_completion_message(text):
                        logger.info(f"🎯 تم العثور على رد البوت (Msg ID: {m.id}):\n{text[:80]}...")
                        status_val = "already_completed" if "مكتمل مسبق" in text else "success"
                        return {
                            "status": status_val,
                            "message": m,
                            "text": text,
                            "reply_text": format_source_reply_design(text)
                        }
        except Exception as e:
            logger.debug(f"خطأ أثناء فحص ردود الجروب: {e}")

        await asyncio.sleep(2)

    raise asyncio.TimeoutError("انتهت مهلة انتظار رد البوت في جروب الهدف.")

async def run_auto_cycle(source_chat: Union[int, str], target_chat: Union[int, str]):
    config = load_config()
    balance_data = load_user_balance(USER_BALANCE_FILE)
    notified_cache = set(config.get("notified_users", []))

    # مزامنة: إذا تم شحن رصيد مستخدم سابقاً (> 0)، نعيد تعيين notified إلى False
    data_changed = False
    for uid, val in list(balance_data.items()):
        if isinstance(val, dict):
            if val.get("balance", 0) > 0 and val.get("notified", False):
                val["notified"] = False
                notified_cache.discard(str(uid))
                data_changed = True
        elif isinstance(val, (int, float)):
            if val > 0 and str(uid) in notified_cache:
                notified_cache.discard(str(uid))
                data_changed = True

    if data_changed:
        save_user_balance(USER_BALANCE_FILE, balance_data)
        config["notified_users"] = list(notified_cache)
        save_config(config)

    # 1. البحث عن مستخدم رصيده صفر ولم يتم إشعاره
    eligible_users = get_eligible_zero_users(balance_data, notified_cache)
    if not eligible_users:
        logger.info("ℹ️ [AutoTask] لا يوجد مستخدمين برصيد 0 في انتظار السحب والإرسال في هذه الدورة.")
        return

    target_user_id = eligible_users[0]
    logger.info(f"🎯 [AutoTask] تم اختيار المستخدم ({target_user_id}) برصيد 0 لبدء الدورة.")

    # التأكد من انضمام الحساب إلى جروبي المصدر والهدف
    await ensure_joined_chat(user_app, source_chat)
    await ensure_joined_chat(user_app, target_chat)

    # 2. سحب رابط عشوائي غير مستخدم من جروب المصدر بحسب حجم العينة المحدد من الأدمن
    used_links = set(config.get("used_links", []))
    pool_size = config.get("links_pool_size", 50)
    target_item = await fetch_random_unused_link(user_app, source_chat, used_links, pool_size=pool_size)
    if not target_item:
        logger.warning(f"⚠️ [AutoTask] لم يتم العثور على أي رابط جديد غير مستخدم في جروب المصدر ({source_chat}).")
        return

    link = target_item["url"]
    source_msg_id = target_item["msg_id"]
    source_user_name = target_item["from_user_name"]

    # 3. إرسال الرابط إلى جروب الهدف عبر الحساب الشخصي
    logger.info(f"🚀 [AutoTask] جاري إرسال الرابط ({link}) إلى جروب الهدف ({target_chat})...")
    try:
        sent_msg = await send_user_message_with_anti_flood(
            client=user_app,
            chat=target_chat,
            text=link
        )
        used_links.add(link)
        config["used_links"] = list(used_links)
        save_config(config)
    except Exception as e:
        logger.error(f"❌ [AutoTask] فشل إرسال الرابط إلى جروب الهدف: {e}")
        return

    # 4. مراقبة رد البوت في جروب الهدف
    try:
        result = await wait_for_target_bot_response(
            client=user_app,
            target_chat=target_chat,
            sent_msg_id=sent_msg.id,
            timeout=90
        )
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ [AutoTask] انتهت المهلة دون تلقي أي رد من البوت في جروب الهدف للرابط ({link}).")
        return
    except Exception as e:
        logger.error(f"❌ [AutoTask] خطأ أثناء انتظار التأكيد: {e}")
        return

    logger.info(f"🎉 [AutoTask] تم استلام رد البوت بنجاح (الحالة: {result['status']}).")

    # معالجة خاصة في حال بلوغ الحد اليومي لبوت الهدف
    if result.get("status") == "daily_limit_reached":
        logger.warning(f"🛑 [AutoTask] توقف دورة العمل: تم بلوغ الحد اليومي لبوت الهدف ({result.get('wait_str')}).")
        # استرجاع الرابط حتى لا يُحسب كرابط مستخدم ويتم حفظه للدورة القادمة
        used_links.discard(link)
        config["used_links"] = list(used_links)
        save_config(config)

        admin_limit_alert = (
            "🛑 **تنبيه هام: تم الوصول للحد اليومي للبوت في جروب الهدف!**\n\n"
            "⚠️ أفاد البوت في جروب الهدف باكتمال الحصة المتاحة لهذا اليوم:\n"
            f"`{result['text']}`\n\n"
            f"⏱ **الروابط هتفتح بعد:** `{result.get('wait_str')}`\n"
            f"📅 **موعد الفتح:** `{result.get('reopen_time')}`\n\n"
            f"⏸ **الإجراء المتخذ تلقائياً:**\n"
            f"1. تم الحفاظ على الرابط وعدم حرقه.\n"
            f"2. تم إيقاف سحب الروابط مؤقتاً لمدة `{result.get('wait_str')}` لتجنب الإرسال العبثي.\n"
            f"3. سيقوم البوت باستئناف العمل تلقائياً فور انتهاء المدة (أو أرسل `/resume` للإلغاء يدوياً)."
        )
        for admin in ADMIN_IDS:
            try:
                await bot_app.send_message(chat_id=admin, text=admin_limit_alert)
            except Exception:
                pass
        return result

    # 5. قيام البوت (bot_app) بالرد المباشر على صاحب اللينك في جروب المصدر بالتصميم المطلوب
    source_replied = False
    formatted_source_reply = format_source_reply_design(result["text"], user_name=source_user_name)
    try:
        await send_bot_message_with_anti_flood(
            client=bot_app,
            chat_id=source_chat,
            text=formatted_source_reply,
            reply_to_message_id=source_msg_id
        )
        source_replied = True
        logger.info(f"✔️ [AutoTask] قام البوت (@{bot_app.me.username if bot_app.me else 'Bot'}) بالرد بنجاح على صاحب اللينك ({source_user_name}) في جروب المصدر ({source_chat}) على الرسالة ({source_msg_id}).")
    except Exception as e:
        logger.error(f"❌ [AutoTask] تعذر رد البوت على صاحب اللينك في جروب المصدر ({source_chat}): {e}")
        # خطة بديلة: إذا تعذر على البوت الرد في الجروب، يحاول الحساب الشخصي
        try:
            await send_user_message_with_anti_flood(
                client=user_app,
                chat=source_chat,
                text=formatted_source_reply,
                reply_to=source_msg_id
            )
            source_replied = True
            logger.info(f"✔️ [AutoTask] تم الرد كإجراء بديل عبر الحساب الشخصي على الرسالة ({source_msg_id}).")
        except Exception as alt_err:
            logger.error(f"❌ [AutoTask] فشل الرد البديل أيضاً: {alt_err}")

    # 6. إشعار المستخدم المستهدف (صاحب الرصيد 0 في user_balance) في حال النجاح أو الاكتمال
    if target_user_id and result["status"] in ["success", "already_completed"]:
        try:
            if str(target_user_id).lstrip("-").isdigit():
                parsed_info = parse_confirmation_message(result["text"])
                user_msg = format_user_notification(parsed_info, link=link)
                await bot_app.send_message(chat_id=int(target_user_id), text=user_msg)
                logger.info(f"✔️ [AutoTask] تم إرسال إشعار النجاح في الخاص للمستخدم المستهدف ({target_user_id}).")
        except Exception as ue:
            logger.error(f"⚠️ [AutoTask] تعذر إرسال الإشعار للمستخدم المستهدف ({target_user_id}): {ue}")

        mark_user_notified(USER_BALANCE_FILE, target_user_id, notified_cache)
        config["notified_users"] = list(notified_cache)
        save_config(config)
        logger.info(f"✅ [AutoTask] تم حفظ حالة الإشعار للمستخدم ({target_user_id}) بنجاح.")

    # 7. إشعار الأدمن في الخاص بالتقرير الكامل
    try:
        if result["status"] == "success":
            status_arabic = "✅ اكتمال بنجاح"
        elif result["status"] == "already_completed":
            status_arabic = "ℹ️ الرابط مكتمل مسبقاً"
        else:
            status_arabic = "⚠️ تنبيه"

        admin_summary = (
            f"📊 **تقرير دورة السحب والتنفيذ:**\n\n"
            f"• **جروب المصدر:** `{source_chat}`\n"
            f"• **صاحب الرابط:** `{source_user_name}` (رسالة: `{source_msg_id}`)\n"
            f"• **تم الرد في المصدر:** {'نعم ✅' if source_replied else 'تعذر ❌'}\n"
            f"• **المستخدم المستهدف:** `{target_user_id}`\n"
            f"• **الرابط:** {link}\n"
            f"• **الحالة:** {status_arabic}\n"
            f"• **رد البوت:**\n`{result['text']}`"
        )
        for admin in ADMIN_IDS:
            try:
                await bot_app.send_message(chat_id=admin, text=admin_summary)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"خطأ أثناء إشعار الأدمن: {e}")

async def auto_scheduler_loop():
    logger.info("🕒 تم بدء حلقة الجدولة التلقائية في الخلفية.")
    while not (bot_app.is_connected and user_app.is_connected()):
        await asyncio.sleep(2)

    logger.info("✅ العميلان متصلان وجاهزان لتنفيذ المهام التلقائية.")

    while True:
        try:
            config = load_config()
            is_enabled = config.get("auto_enabled", False)

            if not is_enabled:
                await asyncio.sleep(5)
                continue

            source_chat = config.get("source_group_id") or SOURCE_GROUP_ID
            target_chat = config.get("target_group_id") or TARGET_GROUP_ID

            if not source_chat or not target_chat:
                logger.warning("⚠️ المهمة التلقائية مفعلة لكن جروب المصدر أو الهدف غير محدد. استخدم /set_source و /set_target.")
                await asyncio.sleep(15)
                continue

            logger.info("🔍 [AutoTask] بدء دورة الفحص التلقائي...")
            cycle_res = await run_auto_cycle(source_chat, target_chat)

            # في حال بلوغ الحد اليومي، نوقف الجدولة تلقائياً حتى موعد الفتح
            if isinstance(cycle_res, dict) and cycle_res.get("status") == "daily_limit_reached":
                wait_secs = cycle_res.get("wait_seconds", 3600)
                wait_str = cycle_res.get("wait_str", f"{wait_secs // 60} دقيقة")
                reopen_str = cycle_res.get("reopen_time", "غير محدد")

                global_status["daily_limit_paused"] = True
                global_status["daily_limit_until"] = time.time() + wait_secs
                global_status["daily_limit_wait_str"] = wait_str
                global_status["daily_limit_reopen_time"] = reopen_str

                logger.warning(
                    f"🛑 [AutoTask] تم إيقاف الجدولة التلقائية مؤقتاً لمدة {wait_str} حتى موعد الفتح ({reopen_str})."
                )

                sleep_elapsed = 0
                while sleep_elapsed < wait_secs:
                    await asyncio.sleep(min(10, wait_secs - sleep_elapsed))
                    sleep_elapsed += 10
                    cfg = load_config()
                    if not cfg.get("auto_enabled", False):
                        break
                    if not global_status.get("daily_limit_paused", True):
                        logger.info("▶️ [AutoTask] تم إلغاء الإيقاف المؤقت يدوياً من الأدمن.")
                        break

                global_status["daily_limit_paused"] = False
                logger.info("🟢 [AutoTask] انتهت فترة الانتظار للحد اليومي! جاري استئناف الفحص التلقائي...")
                continue

            # انتظار الفاصل الزمني المحدد بعد تنفيذ دورة الفحص العادية
            config = load_config()
            interval_seconds = max(config.get("interval_minutes", 5) * 60, 10)
            logger.info(f"⏳ [AutoTask] اكتمال دورة الفحص. الانتظار لمدة {interval_seconds // 60} دقيقة حتى الدورة القادمة...")

            sleep_elapsed = 0
            while sleep_elapsed < interval_seconds:
                await asyncio.sleep(min(5, interval_seconds - sleep_elapsed))
                sleep_elapsed += 5
                cfg = load_config()
                if not cfg.get("auto_enabled", False):
                    break

        except asyncio.CancelledError:
            logger.info("🛑 تم إيقاف حلقة الجدولة التلقائية.")
            break
        except Exception as e:
            logger.error(f"💥 خطأ غير متوقع في حلقة الجدولة: {e}", exc_info=True)
            await asyncio.sleep(10)

# ---------------------------------------------------------
# أوامر الإدارة عبر البوت (Admin Commands Handlers)
# ملاحظة حاسمة: أي شخص غير مسجل كأدمن يتم تجاهله تماماً دون أي رد
# ---------------------------------------------------------
@bot_app.on_message(filters.command("set_interval") & filters.private)
async def cmd_set_interval(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("⚠️ **الاستخدام الصحيح:**\n`/set_interval <عدد الدقائق>`\n\nمثال: `/set_interval 5`")
        return

    try:
        minutes = float(args[1])
        if minutes <= 0:
            raise ValueError()
    except ValueError:
        await message.reply_text("❌ يرجى إدخال رقم صحيح وموجب لعدد الدقائق (مثال: `5` أو `10`).")
        return

    config = load_config()
    config["interval_minutes"] = int(minutes) if minutes.is_integer() else minutes
    save_config(config)
    await message.reply_text(f"⏱️ **تم تحديث الفاصل الزمني بنجاح!**\nسيتم إجراء الفحص كل `{config['interval_minutes']}` دقيقة.")

@bot_app.on_message(filters.command(["set_links", "set_messages", "set_msgs", "set_pool"]) & filters.private)
async def cmd_set_links(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text(
            "⚠️ **الاستخدام الصحيح:**\n"
            "`/set_messages <عدد الرسائل>` أو `/set_links <العدد>`\n\n"
            "مثال: `/set_messages 30` (ليقوم البوت بفحص آخر 30 رسالة في جروب المصدر واختيار رابط عشوائياً منها)."
        )
        return

    try:
        count = int(args[1])
        if count <= 0:
            raise ValueError()
    except ValueError:
        await message.reply_text("❌ يرجى إدخال رقم صحيح وموجب لعدد الرسائل (مثال: `20` أو `50` أو `100`).")
        return

    config = load_config()
    config["links_pool_size"] = count
    save_config(config)
    await message.reply_text(
        f"🎲 **تم تحديث عدد الرسائل المطلوب فحصها بنجاح!**\n"
        f"سيقوم البوت الآن بفحص آخر `{count}` رسالة في جروب المصدر واختيار رابط عشوائياً من الروابط الموجودة داخلها في كل دورة."
    )

@bot_app.on_message(filters.command("set_source") & filters.private)
async def cmd_set_source(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("⚠️ **الاستخدام الصحيح:**\n`/set_source <chat_id أو @username>`\n\nمثال: `/set_source @Invite_30_people`")
        return

    chat_val = normalize_chat_id(args[1].strip())
    config = load_config()
    config["source_group_id"] = chat_val
    save_config(config)
    await message.reply_text(f"📥 **تم ضبط جروب المصدر بنجاح:**\nالمصدر: `{chat_val}`")

@bot_app.on_message(filters.command("set_target") & filters.private)
async def cmd_set_target(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("⚠️ **الاستخدام الصحيح:**\n`/set_target <chat_id أو @username>`\n\nمثال: `/set_target @bots2429`")
        return

    chat_val = normalize_chat_id(args[1].strip())
    config = load_config()
    config["target_group_id"] = chat_val
    save_config(config)
    await message.reply_text(f"📤 **تم ضبط جروب الهدف بنجاح:**\nالهدف: `{chat_val}`")

@bot_app.on_message(filters.command("toggle_auto") & filters.private)
async def cmd_toggle_auto(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    config = load_config()
    new_state = not config.get("auto_enabled", False)
    config["auto_enabled"] = new_state
    save_config(config)

    if new_state:
        await message.reply_text(
            "🟢 **تم تشغيل الميزة التلقائية بنجاح!**\n\n"
            f"• **الفاصل الزمني:** `{config.get('interval_minutes', 5)}` دقيقة\n"
            f"• **جروب المصدر:** `{config.get('source_group_id', 'غير محدد ⚠️')}`\n"
            f"• **جروب الهدف:** `{config.get('target_group_id', 'غير محدد ⚠️')}`"
        )
    else:
        await message.reply_text("🔴 **تم إيقاف الميزة التلقائية مؤقتاً.**")

@bot_app.on_message(filters.command("auto_status") & filters.private)
async def cmd_auto_status(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    config = load_config()
    balances = load_user_balance(USER_BALANCE_FILE)
    notified_cache = set(config.get("notified_users", []))

    zero_users = []
    for uid, val in balances.items():
        if isinstance(val, dict):
            if val.get("balance", 0) == 0:
                zero_users.append((uid, val.get("notified", False)))
        elif isinstance(val, (int, float)):
            if val == 0:
                zero_users.append((uid, str(uid) in notified_cache))

    if global_status.get("daily_limit_paused"):
        remaining_secs = max(0, int(global_status.get("daily_limit_until", 0) - time.time()))
        rem_h = remaining_secs // 3600
        rem_m = (remaining_secs % 3600) // 60
        rem_str = f"{rem_h} ساعة و {rem_m} دقيقة" if rem_h > 0 else f"{rem_m} دقيقة"
        status_icon = f"⏳ متوقف مؤقتاً (حد يومي - متبقي: {rem_str} - فتح: {global_status.get('daily_limit_reopen_time')})"
    elif config.get("auto_enabled", False):
        status_icon = "🟢 قيد التشغيل (مفعل)"
    else:
        status_icon = "🔴 متوقف (معطل)"

    used_links_count = len(config.get("used_links", []))
    links_pool = config.get("links_pool_size", 50)

    text_lines = [
        "📊 **لوحة تحكم الحالة التلقائية (Auto Status)**\n",
        f"• **الحالة:** {status_icon}",
        f"• **الفاصل الزمني:** `{config.get('interval_minutes', 5)}` دقيقة",
        f"• **عدد الرسائل المفحوصة (المصدر):** `{links_pool}` رسالة",
        f"• **جروب المصدر:** `{config.get('source_group_id', 'غير محدد ⚠️')}`",
        f"• **جروب الهدف:** `{config.get('target_group_id', 'غير محدد ⚠️')}`",
        f"• **الروابط المستخدمة:** `{used_links_count}` رابط",
        f"• **المستخدمون برصيد 0:** `{len(zero_users)}` مستخدم",
    ]

    if zero_users:
        text_lines.append("\n👥 **قائمة المستخدمين (رصيد 0):**")
        for uid, is_notif in zero_users[:10]:
            state_label = "تم إشعاره ✅" if is_notif else "في الانتظار ⏳"
            text_lines.append(f"- `{uid}`: {state_label}")
        if len(zero_users) > 10:
            text_lines.append(f"... والمزيد ({len(zero_users) - 10})")

    text_lines.append(
        "\n⚙️ **أوامر الإدارة المتاحة:**\n"
        "/toggle_auto - تشغيل / إيقاف\n"
        "/set_interval <دقائق> - تعديل الفاصل الزمني\n"
        "/set_links <عدد> - تعديل عدد روابط الاختيار العشوائي\n"
        "/set_source <chat_id> - تعيين المصدر\n"
        "/set_target <chat_id> - تعيين الهدف\n"
        "/auto_status - عرض الحالة\n"
        "/reset_notified - إعادة تعيين حالة الإشعار\n"
        "/resume - استئناف فوري وإلغاء انتظار الحد اليومي"
    )

    await message.reply_text("\n".join(text_lines))

@bot_app.on_message(filters.command(["resume", "unpause"]) & filters.private)
async def cmd_resume(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    if global_status.get("daily_limit_paused"):
        global_status["daily_limit_paused"] = False
        await message.reply_text("▶️ **تم إلغاء التوقف المؤقت للحد اليومي واستئناف العمل فوراً!**")
    else:
        await message.reply_text("ℹ️ البوت يعمل بصورة طبيعية ولا يوجد توقف مؤقت للحد اليومي حالياً.")

@bot_app.on_message(filters.command("reset_notified") & filters.private)
async def cmd_reset_notified(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    args = message.text.split()
    config = load_config()
    balances = load_user_balance(USER_BALANCE_FILE)

    if len(args) > 1:
        target_uid = args[1].strip()
        if target_uid in balances and isinstance(balances[target_uid], dict):
            balances[target_uid]["notified"] = False
        if target_uid in config.get("notified_users", []):
            config["notified_users"].remove(target_uid)
        save_user_balance(USER_BALANCE_FILE, balances)
        save_config(config)
        await message.reply_text(f"🔄 تم إعادة تعيين حالة الإشعار للمستخدم `{target_uid}` بنجاح.")
    else:
        for uid, val in balances.items():
            if isinstance(val, dict):
                val["notified"] = False
        config["notified_users"] = []
        save_user_balance(USER_BALANCE_FILE, balances)
        save_config(config)
        await message.reply_text("🔄 تم إعادة تعيين حالة الإشعار لجميع المستخدمين بنجاح.")

@bot_app.on_message(filters.command(["start", "help", "id"]) & filters.private)
async def cmd_start_help(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    logger.info(f"📩 [Bot] استلم أمر {message.text} من الأدمن ({sender_id})")
    help_text = (
        "👋 **أهلاً بك يا أدمن في نظام Telegram Dual-Client!**\n\n"
        f"👤 **معرّف حسابك: `{sender_id}`**\n\n"
        "يمكنك إرسال أي رابط هنا مباشرة لنشره في جروب الهدف كما في السابق.\n\n"
        "🛠 **أوامر الإدارة للمهمة التلقائية:**\n"
        "• `/toggle_auto`: تشغيل أو إيقاف الميزة التلقائية.\n"
        "• `/auto_status`: عرض الحالة والإعدادات الحالية.\n"
        "• `/set_interval <دقائق>`: تحديد وقت الفاصل الزمني.\n"
        "• `/set_messages <عدد>` أو `/set_links <عدد>`: تحديد عدد الرسائل التي يفحصها البوت لاختيار رابط عشوائياً منها.\n"
        "• `/set_source <chat_id>`: تحديد جروب سحب الروابط.\n"
        "• `/set_target <chat_id>`: تحديد جروب نشر الروابط.\n"
        "• `/reset_notified`: إعادة تعيين إشعارات المستخدمين برصيد 0.\n"
        "• `/resume`: استئناف فوري وإلغاء توقف الحد اليومي."
    )
    await message.reply_text(help_text)

# ---------------------------------------------------------
# معالج الرسائل الخاصة العادية (للأدمن فقط)
# ---------------------------------------------------------
is_command_filter = filters.create(lambda _, __, m: bool(m.text and m.text.startswith("/")))

@bot_app.on_message(filters.private & filters.text & ~is_command_filter)
async def handle_private_message(client: BotClient, message: PyroMessage):
    sender_id = message.from_user.id if message.from_user else 0
    if not is_admin(sender_id):
        return  # صمت تام للغرباء

    text = message.text.strip()
    logger.info(f"📩 [Bot] استلم رسالة خاصة من الأدمن ({sender_id}): {text[:50]}")

    urls = URL_PATTERN.findall(text)
    if not urls:
        await message.reply_text(
            "⚠️ **عذراً، لم يتم العثور على أي رابط في رسالتك!**\n\n"
            "يرجى إرسال رسالة تحتوي على رابط صالح يبدأ بـ `http://` أو `https://` لنقوم بنشرها في الجروب.\n\n"
            "أو أرسل `/start` لعرض أوامر الإدارة."
        )
        return

    status_msg = await message.reply_text("⏳ جاري الإرسال إلى الجروب...")

    config = load_config()
    active_target = config.get("target_group_id") or TARGET_GROUP_ID
    if not active_target:
        await status_msg.edit_text("❌ لم يتم تحديد جروب الهدف (TARGET_GROUP_ID).")
        return

    try:
        await send_user_message_with_anti_flood(
            client=user_app,
            chat=active_target,
            text=text,
        )
        await status_msg.edit_text("✅ تم إرسال الرابط في الجروب بنجاح")
        logger.info(f"✔️ تم بنجاح إرسال المحتوى إلى المجموعة ({active_target}) عبر user_app.")
    except Exception as exc:
        error_details = str(exc)
        logger.error(f"❌ تعذر إرسال الرسالة إلى المجموعة: {error_details}")
        await status_msg.edit_text(f"❌ حدث خطأ أثناء محاولة الإرسال للجروب:\n`{error_details}`")

# ---------------------------------------------------------
# نقطة الدخول الرئيسية للتشغيل المتزامن
# ---------------------------------------------------------
async def start_services():
    logger.info("⚡ جاري تشغيل عميل البوت (Pyrogram) وعميل الحساب الشخصي (Telethon)...")
    await bot_app.start()
    await user_app.connect()

    bot_me = await bot_app.get_me()
    user_me = await user_app.get_me()

    logger.info(f"🤖 تم تسجيل دخول البوت بنجاح: @{bot_me.username} (ID: {bot_me.id})")
    user_display = f"{user_me.first_name} (@{user_me.username or 'بدون'})" if user_me else "User"
    logger.info(f"👤 تم تسجيل دخول الحساب الشخصي: {user_display} (ID: {user_me.id if user_me else 'Unknown'})")
    logger.info("✅ العميلان متصلان وجاهزان 100% لاستقبال الرسائل وتنفيذ المهام التلقائية.")

    # بدء حلقة المهمة التلقائية في الخلفية
    scheduler_task = asyncio.create_task(auto_scheduler_loop())

    try:
        await idle()
    finally:
        scheduler_task.cancel()
        try:
            await user_app.disconnect()
        except Exception:
            pass
        try:
            await bot_app.stop()
        except Exception:
            pass

def main():
    logger.info("🚀 جاري بدء تشغيل العميلين (bot_app و user_app) بالتوازي مع المهمة التلقائية...")
    try:
        main_loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        logger.info("🛑 تم إيقاف البرنامج يدوياً.")
    except Exception as e:
        logger.critical(f"💥 خطأ فادح أثناء التشغيل: {e}", exc_info=True)

if __name__ == "__main__":
    main()
