import asyncio
import binascii
import datetime
import json
import sqlite3
import threading
import logging

import aiohttp
import requests
from flask import Flask, request, jsonify
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import DecodeError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import like_pb2
import like_count_pb2
import uid_generator_pb2

# ══════════════════════════════════════════════
#  ⚙️  CONFIG  ← এখানে তোমার values দাও
# ══════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8423292244:AAFwHVOWtswHwQt_hPcb5c39ygwhVs8ESHw"   # @BotFather থেকে নাও
ADMIN_CHAT_ID       = 7544347591               # তোমার Telegram numeric ID

# ══════════════════════════════════════════════
#  GLOBAL STATE
# ══════════════════════════════════════════════
api_enabled = True   # /apion /apioff দিয়ে toggle হয়

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════
DB_PATH = "like_history.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS like_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            uid          TEXT    NOT NULL,
            server       TEXT    NOT NULL,
            player_name  TEXT,
            likes_before INTEGER DEFAULT 0,
            likes_after  INTEGER DEFAULT 0,
            likes_given  INTEGER DEFAULT 0,
            status       INTEGER DEFAULT 0,
            created_at   TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def save_log(uid, server, player_name, likes_before, likes_after, likes_given, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        INSERT INTO like_logs
            (uid, server, player_name, likes_before, likes_after, likes_given, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (uid, server, player_name, likes_before, likes_after, likes_given, status, now))
    conn.commit()
    conn.close()

def purge_old_records():
    """24 ঘণ্টার পুরনো records delete।"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("DELETE FROM like_logs WHERE created_at < ?", (cutoff,))
    conn.commit()
    conn.close()

def get_history_by_uid(uid, limit=10):
    purge_old_records()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT player_name, server, likes_before, likes_after, likes_given, status, created_at
        FROM like_logs WHERE uid = ?
        ORDER BY created_at DESC LIMIT ?
    """, (uid, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_today_stats():
    """Last 24h এর সব rows + summary।"""
    purge_old_records()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        SELECT uid, player_name, server, likes_given, status, created_at
        FROM like_logs WHERE created_at >= ?
        ORDER BY created_at DESC
    """, (cutoff,))
    rows = c.fetchall()
    c.execute("""
        SELECT COUNT(*), COALESCE(SUM(likes_given), 0)
        FROM like_logs WHERE created_at >= ?
    """, (cutoff,))
    summary = c.fetchone()
    conn.close()
    return rows, summary  # rows, (total_requests, total_likes)

# ══════════════════════════════════════════════
#  TELEGRAM HELPER (sync — Flask এ use করা যায়)
# ══════════════════════════════════════════════
def send_telegram_message(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    ADMIN_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

# ══════════════════════════════════════════════
#  ORIGINAL API HELPERS
# ══════════════════════════════════════════════
def load_tokens(server_name):
    try:
        if server_name == "IND":
            fname = "token_ind.json"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            fname = "token_br.json"
        else:
            fname = "token_bd.json"
        with open(fname, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Token load error: {e}")
        return None

def encrypt_message(plaintext):
    try:
        key    = b'Yg&tc%DEuh6%Zc^8'
        iv     = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return binascii.hexlify(cipher.encrypt(pad(plaintext, AES.block_size))).decode('utf-8')
    except Exception as e:
        logger.error(f"Encrypt error: {e}")
        return None

def create_protobuf_message(user_id, region):
    try:
        msg        = like_pb2.like()
        msg.uid    = int(user_id)
        msg.region = region
        return msg.SerializeToString()
    except Exception as e:
        logger.error(f"Protobuf error: {e}")
        return None

async def send_request(encrypted_uid, token, url):
    try:
        headers = {
            'User-Agent':      "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Connection':      "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization':   f"Bearer {token}",
            'Content-Type':    "application/x-www-form-urlencoded",
            'Expect':          "100-continue",
            'X-Unity-Version': "2018.4.11f1",
            'X-GA':            "v1 1",
            'ReleaseVersion':  "OB53"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=bytes.fromhex(encrypted_uid), headers=headers) as resp:
                return resp.status if resp.status != 200 else await resp.text()
    except Exception as e:
        logger.error(f"send_request error: {e}")
        return None

async def send_multiple_requests(uid, server_name, url):
    try:
        proto = create_protobuf_message(uid, server_name)
        if not proto:
            return None
        enc_uid = encrypt_message(proto)
        if not enc_uid:
            return None
        tokens = load_tokens(server_name)
        if not tokens:
            return None
        tasks = [
            send_request(enc_uid, tokens[i % len(tokens)]["token"], url)
            for i in range(250)
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.error(f"send_multiple error: {e}")
        return None

def create_protobuf(uid):
    try:
        msg          = uid_generator_pb2.uid_generator()
        msg.saturn_  = int(uid)
        msg.garena   = 1
        return msg.SerializeToString()
    except Exception as e:
        logger.error(f"uid proto error: {e}")
        return None

def enc(uid):
    proto = create_protobuf(uid)
    return encrypt_message(proto) if proto else None

def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except Exception as e:
        logger.error(f"Decode error: {e}")
        return None

def make_request(encrypt, server_name, token):
    try:
        if server_name == "IND":
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
        else:
            url = "https://clientbp.ggblueshark.com/GetPlayerPersonalShow"

        headers = {
            'User-Agent':      "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Connection':      "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization':   f"Bearer {token}",
            'Content-Type':    "application/x-www-form-urlencoded",
            'Expect':          "100-continue",
            'X-Unity-Version': "2018.4.11f1",
            'X-GA':            "v1 1",
            'ReleaseVersion':  "OB53"
        }
        resp   = requests.post(url, data=bytes.fromhex(encrypt), headers=headers, verify=False)
        binary = bytes.fromhex(resp.content.hex())
        return decode_protobuf(binary)
    except Exception as e:
        logger.error(f"make_request error: {e}")
        return None

# ══════════════════════════════════════════════
#  FLASK ROUTE
# ══════════════════════════════════════════════
@app.route('/like', methods=['GET'])
def handle_requests():
    global api_enabled

    # API off আছে?
    if not api_enabled:
        return jsonify({"error": "API is currently disabled by admin."}), 503

    uid         = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()

    if not uid or not server_name:
        return jsonify({"error": "UID and server_name are required"}), 400

    try:
        tokens = load_tokens(server_name)
        if not tokens:
            raise Exception("Failed to load tokens.")
        token = tokens[0]['token']

        encrypted_uid = enc(uid)
        if not encrypted_uid:
            raise Exception("Encryption failed.")

        # Before
        before = make_request(encrypted_uid, server_name, token)
        if before is None:
            raise Exception("Failed to get player info (before).")
        data_before  = json.loads(MessageToJson(before))
        likes_before = int(data_before.get('AccountInfo', {}).get('Likes', 0))

        # Like URL
        if server_name == "IND":
            like_url = "https://client.ind.freefiremobile.com/LikeProfile"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            like_url = "https://client.us.freefiremobile.com/LikeProfile"
        else:
            like_url = "https://clientbp.ggblueshark.com/LikeProfile"

        asyncio.run(send_multiple_requests(uid, server_name, like_url))

        # After
        after = make_request(encrypted_uid, server_name, token)
        if after is None:
            raise Exception("Failed to get player info (after).")
        data_after   = json.loads(MessageToJson(after))
        likes_after  = int(data_after.get('AccountInfo', {}).get('Likes', 0))
        player_uid   = int(data_after.get('AccountInfo', {}).get('UID', 0))
        player_name  = str(data_after.get('AccountInfo', {}).get('PlayerNickname', ''))
        likes_given  = likes_after - likes_before
        status       = 1 if likes_given != 0 else 2

        # DB save
        save_log(uid, server_name, player_name, likes_before, likes_after, likes_given, status)

        result = {
            "LikesGivenByAPI":    likes_given,
            "LikesafterCommand":  likes_after,
            "LikesbeforeCommand": likes_before,
            "PlayerNickname":     player_name,
            "UID":                player_uid,
            "status":             status
        }

        # Admin notify
        icon = "✅" if status == 1 else "⚠️"
        msg = (
            f"🔔 <b>New Like Request</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Player  : <b>{player_name}</b>\n"
            f"🆔 UID     : <code>{uid}</code>\n"
            f"🌍 Server  : <b>{server_name}</b>\n"
            f"💜 Before  : <b>{likes_before}</b>\n"
            f"💜 After   : <b>{likes_after}</b>\n"
            f"➕ Given   : <b>{likes_given}</b>\n"
            f"📊 Status  : {icon} {'Success' if status==1 else 'No likes added'}\n"
            f"🕐 Time    : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        threading.Thread(target=send_telegram_message, args=(msg,), daemon=True).start()

        return jsonify(result)

    except Exception as e:
        logger.error(f"Route error: {e}")
        err_msg = (
            f"❌ <b>API Error</b>\n"
            f"UID: <code>{uid}</code> | Server: {server_name}\n"
            f"Error: {e}"
        )
        threading.Thread(target=send_telegram_message, args=(err_msg,), daemon=True).start()
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════════
#  TELEGRAM BOT COMMANDS
# ══════════════════════════════════════════════

def admin_only(func):
    """শুধু admin ব্যবহার করতে পারবে।"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Unauthorized!")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "✅ ON" if api_enabled else "🔴 OFF"
    await update.message.reply_text(
        f"👋 <b>Like API Bot</b>\n"
        f"Current API Status: <b>{state}</b>\n\n"
        f"📌 <b>Commands:</b>\n"
        f"/history &lt;uid&gt; — UID এর last 10 requests\n"
        f"/todaylikes — আজকের সব requests\n"
        f"/apion — API চালু করো\n"
        f"/apioff — API বন্ধ করো\n"
        f"/status — API এর current status",
        parse_mode="HTML"
    )


@admin_only
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Usage: /history <uid>")
        return

    uid  = context.args[0]
    rows = get_history_by_uid(uid)

    if not rows:
        await update.message.reply_text(
            f"📭 UID <code>{uid}</code> এর কোনো record নেই (last 24h).",
            parse_mode="HTML"
        )
        return

    lines = [f"📋 <b>History — UID: {uid}</b> (last 24h)\n━━━━━━━━━━━━━━━━━━━\n"]
    for i, (pname, server, lb, la, lg, st, cat) in enumerate(rows, 1):
        icon = "✅" if st == 1 else "⚠️"
        lines.append(
            f"{i}. {icon} <b>{pname}</b> | {server}\n"
            f"   {lb} → {la} likes  (+{lg})\n"
            f"   🕐 {cat}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@admin_only
async def cmd_todaylikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows, (total_req, total_likes) = get_today_stats()

    if not rows:
        await update.message.reply_text("📭 আজকে কোনো request নেই।")
        return

    header = (
        f"📊 <b>Today's Requests (last 24h)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Total Requests : <b>{total_req}</b>\n"
        f"Total Likes    : <b>{total_likes}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]
    for i, (uid, pname, server, lg, st, cat) in enumerate(rows, 1):
        icon = "✅" if st == 1 else "⚠️"
        lines.append(
            f"{i}. {icon} <b>{pname}</b> (<code>{uid}</code>)\n"
            f"   {server} | +{lg} likes | 🕐 {cat}\n"
        )

    full = "\n".join(lines)
    if len(full) > 4000:
        full = full[:4000] + "\n\n...আরো records আছে।"

    await update.message.reply_text(full, parse_mode="HTML")


@admin_only
async def cmd_apion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global api_enabled
    api_enabled = True
    logger.info("API enabled by admin.")
    await update.message.reply_text("✅ <b>API চালু করা হয়েছে।</b>", parse_mode="HTML")


@admin_only
async def cmd_apioff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global api_enabled
    api_enabled = False
    logger.info("API disabled by admin.")
    await update.message.reply_text("🔴 <b>API বন্ধ করা হয়েছে।</b>", parse_mode="HTML")


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "✅ চালু (ON)" if api_enabled else "🔴 বন্ধ (OFF)"
    _, (total_req, total_likes) = get_today_stats()
    await update.message.reply_text(
        f"🖥️ <b>API Status</b>: {state}\n\n"
        f"📊 Last 24h:\n"
        f"  Requests : <b>{total_req}</b>\n"
        f"  Likes    : <b>{total_likes}</b>",
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════
#  DAILY AUTO SUMMARY (প্রতিদিন রাত 12টায়)
# ══════════════════════════════════════════════
def daily_summary_job():
    import time
    while True:
        now      = datetime.datetime.now()
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        time.sleep((midnight - now).total_seconds())

        rows, (total_req, total_likes) = get_today_stats()
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        msg = (
            f"📅 <b>Daily Report — {date_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Total Requests : <b>{total_req}</b>\n"
            f"Total Likes    : <b>{total_likes}</b>\n\n"
        )
        if rows:
            msg += "📋 <b>All Requests:</b>\n"
            for uid, pname, server, lg, st, cat in rows[:25]:
                icon = "✅" if st == 1 else "⚠️"
                msg += f"{icon} {pname} ({uid}) | {server} | +{lg}\n"
            if len(rows) > 25:
                msg += f"...এবং আরো {len(rows)-25}টি request।"
        else:
            msg += "📭 কোনো request হয়নি।"

        send_telegram_message(msg)

# ══════════════════════════════════════════════
#  BOT RUNNER (আলাদা thread)
# ══════════════════════════════════════════════
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start",      cmd_start))
    application.add_handler(CommandHandler("history",    cmd_history))
    application.add_handler(CommandHandler("todaylikes", cmd_todaylikes))
    application.add_handler(CommandHandler("apion",      cmd_apion))
    application.add_handler(CommandHandler("apioff",     cmd_apioff))
    application.add_handler(CommandHandler("status",     cmd_status))

    logger.info("✅ Telegram bot polling started.")
    application.run_polling()

# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════
if __name__ == '__main__':
    init_db()

    # Daily summary scheduler
    threading.Thread(target=daily_summary_job, daemon=True).start()

    # Telegram bot (separate thread)
    threading.Thread(target=run_bot, daemon=True).start()

    # Flask
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)
