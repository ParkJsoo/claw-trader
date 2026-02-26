import os
import hashlib
import logging
import redis
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO)

# =========================
# Environment Variables
# =========================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_ALLOWED_USER_ID = os.getenv("TG_ALLOWED_USER_ID")
TG_ALLOWED_CHAT_ID = os.getenv("TG_ALLOWED_CHAT_ID")
TG_PIN_HASH = os.getenv("TG_PIN_HASH")
TG_BOOTSTRAP = os.getenv("TG_BOOTSTRAP", "0") == "1"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PAUSE_KEY_PRIMARY = "claw:pause:global"   # 표준
PAUSE_KEY_COMPAT  = "trading:paused"      # 호환(기존)

# =========================
# Safety Checks
# =========================
if not TG_BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN is not set")

if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

# =========================
# Redis Connection
# =========================
r = redis.from_url(REDIS_URL)

try:
    r.ping()
    logging.info("Redis connected successfully.")
except Exception as e:
    raise RuntimeError(f"Redis connection failed: {e}")

# =========================
# Security Functions
# =========================
def verify_pin(pin: str) -> bool:
    if not TG_PIN_HASH:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), b"claw-trader", 200_000)
    return dk.hex() == TG_PIN_HASH


def check_allowlist(update: Update) -> bool:
    return (
        str(update.effective_user.id) == TG_ALLOWED_USER_ID and
        str(update.effective_chat.id) == TG_ALLOWED_CHAT_ID
    )

# =========================
# Handlers
# =========================
async def bootstrap_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TG_BOOTSTRAP:
        return

    logging.warning("BOOTSTRAP MODE ACTIVE")

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    logging.warning(f"USER_ID: {user_id}")
    logging.warning(f"CHAT_ID: {chat_id}")

    await update.message.reply_text(
        "Bootstrap complete.\nCheck server logs for USER_ID and CHAT_ID."
    )


def is_paused_redis() -> bool:
    """
    표준 키 우선, 없으면 호환 키도 확인.
    값은 "true"/"1" 모두 paused로 인정.
    """
    v1 = r.get(PAUSE_KEY_PRIMARY)
    if v1 is not None:
        return v1 in (b"true", b"1")

    v2 = r.get(PAUSE_KEY_COMPAT)
    return v2 in (b"true", b"1")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_allowlist(update):
        return

    if is_paused_redis():
        await update.message.reply_text("System OK | Trading PAUSED")
    else:
        await update.message.reply_text("System OK | Trading ACTIVE")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_allowlist(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /pause <PIN>")
        return

    pin = context.args[0]
    if not verify_pin(pin):
        await update.message.reply_text("Invalid PIN")
        return

    # 표준 + 호환 둘 다 세팅
    r.set(PAUSE_KEY_PRIMARY, "true")
    r.set(PAUSE_KEY_COMPAT, "true")

    await update.message.reply_text("Trading paused.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_allowlist(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /resume <PIN>")
        return

    pin = context.args[0]
    if not verify_pin(pin):
        await update.message.reply_text("Invalid PIN")
        return

    # 표준 + 호환 둘 다 해제
    r.delete(PAUSE_KEY_PRIMARY)
    r.delete(PAUSE_KEY_COMPAT)

    await update.message.reply_text("Trading resumed.")

# =========================
# Main
# =========================
def main():
    if TG_BOOTSTRAP:
        logging.warning("⚠ BOOTSTRAP MODE ENABLED - DISABLE AFTER USE")

    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))

    if TG_BOOTSTRAP:
        app.add_handler(CommandHandler("start", bootstrap_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
