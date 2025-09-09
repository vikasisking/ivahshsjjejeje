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
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio

# -------------------- CONFIG --------------------

WS_URL = os.environ.get("WS_URL")
AUTH_MESSAGE = os.environ.get("AUTH_MESSAGE")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7761576669"))  # apna telegram ID
CHANNEL_URL = os.environ.get("CHANNEL_URL")
DEV_URL = os.environ.get("DEV_URL")
CHAT_URL = os.environ.get("CHAT_URL")

PING_INTERVAL = int(os.environ.get("PING_INTERVAL", 25))

# -------------------- STATE --------------------
CHAT_IDS = set()  # multiple groups
otp_count = 0
last_otp_time = "N/A"
connected = False
start_pinging = False

# -------------------- UTILITIES --------------------

def country_to_flag(code: str) -> str:
    if not code or len(code) != 2:
        return ""
    return "".join(chr(127397 + ord(c)) for c in code.upper())

def mask_number(number: str) -> str:
    if len(number) < 9:
        return number
    return number[:5] + '⁕' * (len(number) - 9) + number[-4:]

# -------------------- TELEGRAM --------------------

def send_to_telegram(text):
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

    payload = {
        "parse_mode": "HTML",
        "text": text,
        "reply_markup": json.dumps(buttons)
    }

    for chat_id in CHAT_IDS:
        payload["chat_id"] = chat_id
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=payload,
                timeout=10
            )
            if response.status_code == 200:
                print(f"✅ Message sent to {chat_id}")
            else:
                print(f"⚠️ Telegram Error [{response.status_code}]: {response.text}")
        except Exception as e:
            print("❌ Telegram Send Failed:", e)

# -------------------- WEBSOCKET --------------------

def send_ping(ws):
    global start_pinging
    while ws.keep_running:
        if start_pinging:
            try:
                ws.send("3")
                print("📡 Ping sent (3)")
            except Exception as e:
                print("❌ Failed to send ping:", e)
                break
        time.sleep(PING_INTERVAL)

def on_open(ws):
    global start_pinging, connected
    connected = True
    start_pinging = False
    print("✅ WebSocket connected")

    time.sleep(0.5)
    ws.send("40/livesms")
    print("➡️ Sent: 40/livesms")

    time.sleep(0.5)
    ws.send(AUTH_MESSAGE)
    print("🔐 Sent auth token")

    threading.Thread(target=send_ping, args=(ws,), daemon=True).start()
# Dummy Test OTP Message
def build_test_message():
    return (
        "<blockquote>🌍 Country: 🇮🇳 IN</blockquote>\n"
        "<blockquote>🔑 OTP: 123456</blockquote>\n"
        "<blockquote>📢 Service: TestService</blockquote>\n"
        "<blockquote>💬 Message:\nThis is a test message</blockquote>\n\n"
        "⚡ Powered by @hiden_25"
    )

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_IDS:
        await update.message.reply_text("⚠️ No groups added yet. Use /addgroup <id> to add one.")
        return

    test_msg = build_test_message()
    for gid in CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=gid, text=test_msg, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send test message to {gid}: {e}")
            continue

    await update.message.reply_text("✅ Test message sent to all groups.")

def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc and doc.file_name.endswith(".txt"):
        file = doc.get_file()
        file_path = f"downloads/{doc.file_name}"
        file.download(file_path)
        context.chat_data["last_file"] = file_path
        update.message.reply_text("✅ File saved. Now reply `/setnumber <userid>` to assign.")

def setnumber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "last_file" not in context.chat_data:
        update.message.reply_text("⚠️ First upload a numbers.txt file and then use this command.")
        return
    
    if len(context.args) != 1:
        update.message.reply_text("⚠️ Usage: /setnumber <userid>")
        return

    user_id = context.args[0]
    file_path = context.chat_data["last_file"]

    with open(file_path, "r") as f:
        numbers = [line.strip() for line in f if line.strip()]

    mapping = load_mapping()
    for number in numbers:
        mapping[number] = user_id
    save_mapping(mapping)

    update.message.reply_text(f"✅ Numbers assigned to user {user_id}")

mapping = load_mapping()
if recipient in mapping:
    target_user = mapping[recipient]
    send_private_otp(target_user, telegram_msg)
else:
    send_to_telegram(telegram_msg)

def send_private_otp(user_id, text):
    payload = {
        "chat_id": user_id,
        "text": text,
        "parse_mode": "HTML"
    }
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)


def on_message(ws, message):
    global start_pinging, otp_count, last_otp_time
    if message == "3":
        print("✅ Pong received")
    elif message.startswith("40/livesms"):
        print("✅ Namespace joined — starting ping")
        start_pinging = True
    elif message.startswith("42/livesms,"):
        try:
            payload = message[len("42/livesms,"):]
            data = json.loads(payload)

            if isinstance(data, list) and len(data) > 1 and isinstance(data[1], dict):
                sms = data[1]
                raw_msg = sms.get("message", "")
                originator = sms.get("originator", "Unknown")
                recipient = sms.get("recipient", "Unknown")
                country = sms.get("country_iso", "??").upper()

                import re
                otp_match = re.search(r'\b\d{3}[- ]?\d{3}\b|\b\d{6}\b', raw_msg)
                otp = otp_match.group(0) if otp_match else "N/A"

                masked = mask_number(recipient)
                now = datetime.now().strftime("%H:%M:%S")
                flag = country_to_flag(country)

                telegram_msg = (
                    f"<blockquote>🌍 Country: {flag} <code>{country}</code></blockquote>\n"
                    f"<blockquote>🔑 OTP: <code>{otp}</code></blockquote>\n"
                    f"<blockquote>🕒 Time: <code>{now}</code></blockquote>\n"
                    f"<blockquote>📢 Service: <code>{originator}</code></blockquote>\n"
                    f"<blockquote>📱 Number: <code>{masked}</code></blockquote>\n"
                    f"<blockquote>💬 Message:\n<code>{html.escape(raw_msg)}</code></blockquote>\n"
                    "\n⚡ Delivered instantly via @hiden_25"
                )

                send_to_telegram(telegram_msg)
                otp_count += 1
                last_otp_time = now
            else:
                print("⚠️ Unexpected data format:", data)

        except Exception as e:
            print("❌ Error parsing message:", e)
            print("Raw message:", message)

def on_error(ws, error):
    print("❌ WebSocket error:", error)

def on_close(ws, code, msg):
    global start_pinging, connected
    connected = False
    start_pinging = False
    print("🔌 WebSocket closed. Reconnecting in 1s...")
    time.sleep(1)
    start_ws_thread()

def connect():
    print("🔄 Connecting to WebSocket...")
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

# -------------------- TELEGRAM COMMANDS --------------------

async def status(update, context: ContextTypes.DEFAULT_TYPE):
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

async def addgroup(update, context):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")

    if not context.args:
        return await update.message.reply_text("Usage: /addgroup <chat_id>")

    chat_id = context.args[0]
    CHAT_IDS.add(chat_id)
    await update.message.reply_text(f"✅ Group {chat_id} added")

async def removegroup(update, context):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")

    if not context.args:
        return await update.message.reply_text("Usage: /removegroup <chat_id>")

    chat_id = context.args[0]
    if chat_id in CHAT_IDS:
        CHAT_IDS.remove(chat_id)
        await update.message.reply_text(f"✅ Group {chat_id} removed")
    else:
        await update.message.reply_text("⚠️ Group not found")

def start_telegram_listener():
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("status", status))
    tg_app.add_handler(CommandHandler("addgroup", addgroup))
    tg_app.add_handler(CommandHandler("removegroup", removegroup))
    tg_app.add_handler(CommandHandler("test", test_command))  # ✅ yahan change
    tg_app.run_polling()

# -------------------- FLASK --------------------

app = Flask(__name__)

@app.route("/")
def root():
    return Response("Service is running", status=200)

@app.route("/health")
def health():
    return Response("OK", status=200)

# -------------------- START --------------------

if __name__ == "__main__":
    start_ws_thread()
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True)
    flask_thread.start()
    start_telegram_listener()
