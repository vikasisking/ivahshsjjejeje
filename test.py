import websocket
import threading
import time
import json
import requests
from datetime import datetime
import html
import os
from flask import Flask, Response
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("otp-bot")

# -------------------- CONFIG (from env) --------------------
WS_URL = os.environ.get("WS_URL")
AUTH_MESSAGE = os.environ.get("AUTH_MESSAGE", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7761576669"))  # set your numeric Telegram ID
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/yourchannel")
DEV_URL = os.environ.get("DEV_URL", "https://t.me/yourdev")
CHAT_URL = os.environ.get("CHAT_URL", "https://t.me/owner")
PING_INTERVAL = int(os.environ.get("PING_INTERVAL", 25))
PORT = int(os.environ.get("PORT", "8080"))

# -------------------- STATE & FILES --------------------
CHAT_IDS_FILE = "chat_ids.json"
MAPPING_FILE = "number_mapping.json"
DOWNLOAD_DIR = "downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CHAT_IDS_LOCK = threading.Lock()
MAPPING_LOCK = threading.Lock()

# load/save helpers
def load_chat_ids():
    try:
        with open(CHAT_IDS_FILE, "r") as f:
            data = json.load(f)
            return set(data)
    except Exception:
        return set()

def save_chat_ids(chat_ids):
    with CHAT_IDS_LOCK:
        with open(CHAT_IDS_FILE, "w") as f:
            json.dump(list(chat_ids), f)

def load_mapping():
    try:
        with open(MAPPING_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}  # number -> user_id (string or int)

def save_mapping(mapping):
    with MAPPING_LOCK:
        with open(MAPPING_FILE, "w") as f:
            json.dump(mapping, f)

# runtime data
CHAT_IDS = load_chat_ids()
NUMBER_TO_USER = load_mapping()  # keys: normalized numbers -> user_id
otp_count = 0
last_otp_time = "N/A"
connected = False
start_pinging = False

# pending uploaded files per admin (admin_id -> filepath)
PENDING_FILES = {}

# -------------------- UTILITIES --------------------
def normalize_number(num: str) -> str:
    """Return only digits, prefixed by + if present, but normalize to digits only for matching.
       We'll store pure digits (no +)."""
    if not num:
        return ""
    digits = re.sub(r"\D", "", num)
    return digits  # e.g. '919812345678'

def last_n_digits(num: str, n: int = 10) -> str:
    d = normalize_number(num)
    return d[-n:] if len(d) >= n else d

def country_to_flag(code: str) -> str:
    if not code or len(code) != 2:
        return ""
    return "".join(chr(127397 + ord(c)) for c in code.upper())

def mask_number(number: str) -> str:
    d = normalize_number(number)
    if len(d) <= 7:
        return number  # small, just return original
    # show first 4 and last 3
    return f"{d[:4]}{'*'*(len(d)-7)}{d[-3:]}"

def build_telegram_buttons():
    buttons = {
        "inline_keyboard": [
            [
                {"text": "☎️ Numbers", "url": CHANNEL_URL},
                {"text": "👑 Owner", "url": CHAT_URL}
            ],
            [
                {"text": "🖥️ Developer", "url": DEV_URL}
            ]
        ]
    }
    return json.dumps(buttons)

# -------------------- TELEGRAM SENDING --------------------
def send_message_to_chat(chat_id, text):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": build_telegram_buttons()
    }
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram send failed to %s: %s", chat_id, r.text)
            return False
        return True
    except Exception as e:
        logger.exception("Error sending Telegram message to %s: %s", chat_id, e)
        return False

def send_to_groups(text):
    if not CHAT_IDS:
        logger.info("No groups configured, skipping group forwarding.")
        return
    for gid in list(CHAT_IDS):
        send_message_to_chat(gid, text)

def send_private_otp(user_id, text):
    try:
        payload = {
            "chat_id": int(user_id),
            "text": text,
            "parse_mode": "HTML"
        }
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Failed to DM %s: %s", user_id, r.text)
            return False
        return True
    except Exception as e:
        logger.exception("Error DMing user %s: %s", user_id, e)
        return False

# -------------------- WEBSOCKET --------------------
def send_ping(ws):
    global start_pinging
    while getattr(ws, "keep_running", False):
        if start_pinging:
            try:
                ws.send("3")
            except Exception as e:
                logger.exception("Ping failed: %s", e)
                break
        time.sleep(PING_INTERVAL)

def on_open(ws):
    global start_pinging, connected
    connected = True
    start_pinging = False
    logger.info("WebSocket connected")
    time.sleep(0.5)
    try:
        ws.send("40/livesms")
        time.sleep(0.3)
        if AUTH_MESSAGE:
            ws.send(AUTH_MESSAGE)
        threading.Thread(target=send_ping, args=(ws,), daemon=True).start()
    except Exception as e:
        logger.exception("on_open exception: %s", e)

def handle_incoming_otp(sms: dict):
    """Central place to format message and decide where to send (DM or groups)."""
    global otp_count, last_otp_time

    raw_msg = sms.get("message", "")
    originator = sms.get("originator", "Unknown")
    recipient = sms.get("recipient", "Unknown")
    country = sms.get("country_iso", "??") or "??"
    try:
        # regex catch: 3-3 or 6 digits, and broaden to 4..8 common patterns
        otp_match = re.search(r'\b\d{3}[- ]?\d{3}\b|\b\d{6}\b|\b\d{4}\b|\b\d{5}\b|\b\d{7,8}\b', raw_msg)
    except Exception:
        otp_match = None
    otp = otp_match.group(0) if otp_match else "N/A"
    masked = mask_number(recipient)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    flag = country_to_flag(country[:2]) if country else ""
    telegram_msg = (
        f"<blockquote>🌍 Country: {flag} <code>{country}</code></blockquote>\n"
        f"<blockquote>🔑 OTP: <code>{html.escape(str(otp))}</code></blockquote>\n"
        f"<blockquote>🕒 Time: <code>{now}</code></blockquote>\n"
        f"<blockquote>📢 Service: <code>{html.escape(originator)}</code></blockquote>\n"
        f"<blockquote>📱 Number: <code>{masked}</code></blockquote>\n"
        f"<blockquote>💬 Message:\n<code>{html.escape(raw_msg)}</code></blockquote>\n"
        f"\n⚡ Delivered instantly"
    )

    # mapping check
    recipient_norm = normalize_number(recipient)
    mapped_user = None
    if recipient_norm:
        with MAPPING_LOCK:
            mapped_user = NUMBER_TO_USER.get(recipient_norm)
            if not mapped_user:
                # fallback: last 10 digits match
                last10 = last_n_digits(recipient_norm, 10)
                for k, v in NUMBER_TO_USER.items():
                    if k.endswith(last10):
                        mapped_user = v
                        break

    if mapped_user:
        logger.info("Recipient %s mapped to user %s — sending DM", recipient, mapped_user)
        sent = send_private_otp(mapped_user, telegram_msg)
        if not sent:
            # if DM fails, forward to groups as fallback
            logger.warning("DM failed to %s, forwarding to groups", mapped_user)
            send_to_groups(telegram_msg)
    else:
        send_to_groups(telegram_msg)

    otp_count += 1
    last_otp_time = now
    # also append to a local log file
    try:
        with open("otp_ws_logs.txt", "a", encoding="utf-8") as lf:
            lf.write(f"[{now}] {recipient} | {originator} | {otp} | {raw_msg}\n")
    except Exception:
        pass

def on_message(ws, message):
    global start_pinging
    if message == "3":
        # pong-like tick
        return
    if message.startswith("40/livesms"):
        start_pinging = True
        return
    if message.startswith("42/livesms,"):
        try:
            payload = message[len("42/livesms,"):]
            data = json.loads(payload)
            if isinstance(data, list) and len(data) > 1 and isinstance(data[1], dict):
                sms = data[1]
                handle_incoming_otp(sms)
            else:
                logger.debug("Unexpected data format: %s", data)
        except Exception as e:
            logger.exception("Error in on_message parsing: %s", e)

def on_error(ws, error):
    logger.exception("WebSocket error: %s", error)

def on_close(ws, code, msg):
    global start_pinging, connected
    connected = False
    start_pinging = False
    logger.warning("WebSocket closed. Attempting reconnect in 1s...")
    time.sleep(1)
    start_ws_thread()

def connect():
    logger.info("Connecting to WebSocket...")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://ivasms.com",
        "Referer": "https://ivasms.com/",
        "Host": "ivasms.com"
    }
    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header=[f"{k}: {v}" for k, v in headers.items()]
    )
    ws.run_forever()

def start_ws_thread():
    t = threading.Thread(target=connect, daemon=True)
    t.start()

# -------------------- TELEGRAM HANDLERS --------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")
    msg = (
        f"📊 <b>Bot Status</b>\n\n"
        f"🔌 Connected: <code>{connected}</code>\n"
        f"✅ Total OTPs: <code>{otp_count}</code>\n"
        f"⏱️ Last OTP Time: <code>{last_otp_time}</code>\n"
        f"📌 Forwarding Groups: {', '.join(CHAT_IDS) if CHAT_IDS else 'None'}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")
    if not context.args:
        return await update.message.reply_text("Usage: /addgroup <chat_id>")
    chat_id = context.args[0]
    CHAT_IDS.add(chat_id)
    save_chat_ids(CHAT_IDS)
    await update.message.reply_text(f"✅ Group {chat_id} added")

async def removegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")
    if not context.args:
        return await update.message.reply_text("Usage: /removegroup <chat_id>")
    chat_id = context.args[0]
    if chat_id in CHAT_IDS:
        CHAT_IDS.remove(chat_id)
        save_chat_ids(CHAT_IDS)
        await update.message.reply_text(f"✅ Group {chat_id} removed")
    else:
        await update.message.reply_text("⚠️ Group not found")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")
    if not CHAT_IDS:
        return await update.message.reply_text("⚠️ No groups added yet. Use /addgroup <id> to add one.")
    test_msg = (
        "<blockquote>🌍 Country: 🇮🇳 <code>IN</code></blockquote>\n"
        "<blockquote>🔑 OTP: <code>123456</code></blockquote>\n"
        "<blockquote>📢 Service: <code>TestService</code></blockquote>\n"
        "<blockquote>💬 Message:\n<code>This is a test message</code></blockquote>\n\n"
        "⚡ Powered by TestBot"
    )
    for gid in CHAT_IDS:
        send_message_to_chat(gid, test_msg)
    await update.message.reply_text("✅ Test message sent to all groups.")

# document (file) handler (admin uploads numbers.txt)
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized. Only admin can upload number files.")
    doc = update.message.document
    if not doc:
        return await update.message.reply_text("⚠️ No document found.")
    if not doc.file_name.lower().endswith(".txt"):
        return await update.message.reply_text("⚠️ Please upload a .txt file with one number per line.")
    file = await doc.get_file()
    save_path = os.path.join(DOWNLOAD_DIR, f"{int(time.time())}_{doc.file_name}")
    await file.download_to_drive(save_path)
    PENDING_FILES[update.effective_user.id] = save_path
    await update.message.reply_text("✅ File saved. Now reply with /setnumber <telegram_user_id> to assign these numbers to that user.")

# /setnumber <userid>
async def setnumber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")
    if update.effective_user.id not in PENDING_FILES:
        return await update.message.reply_text("⚠️ No pending file. First upload numbers.txt and then call /setnumber <userid>.")
    if not context.args:
        return await update.message.reply_text("Usage: /setnumber <telegram_user_id>")
    target_user = context.args[0]
    file_path = PENDING_FILES.pop(update.effective_user.id, None)
    if not file_path or not os.path.exists(file_path):
        return await update.message.reply_text("⚠️ File not found. Please upload again.")
    added = 0
    with open(file_path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    with MAPPING_LOCK:
        for ln in lines:
            normalized = normalize_number(ln)
            if not normalized:
                continue
            NUMBER_TO_USER[normalized] = int(target_user)
            added += 1
        save_mapping(NUMBER_TO_USER)
    await update.message.reply_text(f"✅ Assigned {added} numbers to user {target_user}.")

# -------------------- TELEGRAM SETUP/START --------------------
def start_telegram_listener():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("addgroup", addgroup))
    app.add_handler(CommandHandler("removegroup", removegroup))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("setnumber", setnumber))
    # document handler: admin uploads numbers.txt
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    # run_polling blocks — we start WS + Flask in separate threads before calling this
    app.run_polling()

# -------------------- FLASK --------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def root():
    return Response("Service is running", status=200)

@flask_app.route("/health")
def health():
    return Response("OK", status=200)

# -------------------- START --------------------
if __name__ == "__main__":
    # start websocket thread
    start_ws_thread()
    # start flask in background thread
    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT), daemon=True)
    flask_thread.start()
    # start telegram listener (blocking)
    start_telegram_listener()
