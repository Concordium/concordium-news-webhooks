"""
telegram_bridge.py

Telegram Channel -> Discord Webhook bridge (Python)
- Listens to Telegram channel posts (channel_post)
- Forwards text + media (photo/video/gif/document/etc.) to Discord via a *dedicated* Discord webhook
- Makes the channel title in Discord clickable using TELEGRAM_CHANNEL_URL (invite link for private channels)

Env vars:
  TG_BOT_TOKEN
  TELEGRAM_DISCORD_WEBHOOK_URL
  TELEGRAM_CHANNEL_URL
  DISCORD_MAX_FILE_BYTES (optional, default 8MB)

Requirements:
  pip install python-telegram-bot==21.6 requests
"""

import io
import os
import json
import logging
from typing import Optional, Tuple

import requests
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tg-to-discord")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TELEGRAM_DISCORD_WEBHOOK_URL = os.getenv("TELEGRAM_DISCORD_WEBHOOK_URL")
TELEGRAM_CHANNEL_URL = os.getenv("TELEGRAM_CHANNEL_URL")

# Conservative default (8MB). Can be higher on boosted servers.
DISCORD_MAX_FILE_BYTES = int(os.getenv("DISCORD_MAX_FILE_BYTES", str(8 * 1024 * 1024)))


def build_discord_content(update: Update) -> str:
    chat = update.effective_chat
    msg = update.effective_message

    chat_title = getattr(chat, "title", None) or "Telegram Channel"
    text = msg.text or msg.caption or ""
    if not text:
        text = "(media-only post)"

    # Clickable channel title (useful for private channels via invite link)
    if TELEGRAM_CHANNEL_URL:
        header = f"[{chat_title}]({TELEGRAM_CHANNEL_URL})"
    else:
        header = f"**{chat_title}**"

    return "\n".join([header, text])


def pick_telegram_media(update: Update) -> Optional[Tuple[str, str, Optional[int]]]:
    """
    Returns (file_id, filename, file_size) or None if no media found.
    Supports: photo, video, animation (GIF), document, audio, voice, video_note, sticker.
    """
    msg = update.effective_message
    if not msg:
        return None

    # Photo: take the largest available size
    if msg.photo:
        photo = msg.photo[-1]
        return (photo.file_id, f"photo_{msg.message_id}.jpg", getattr(photo, "file_size", None))

    # Video
    if msg.video:
        v = msg.video
        return (
            v.file_id,
            getattr(v, "file_name", None) or f"video_{msg.message_id}.mp4",
            getattr(v, "file_size", None),
        )

    # Animation (GIF usually arrives as animation, often mp4)
    if msg.animation:
        a = msg.animation
        name = getattr(a, "file_name", None) or f"animation_{msg.message_id}.mp4"
        return (a.file_id, name, getattr(a, "file_size", None))

    # Document (any file, including GIF-as-document)
    if msg.document:
        d = msg.document
        name = getattr(d, "file_name", None) or f"document_{msg.message_id}"
        return (d.file_id, name, getattr(d, "file_size", None))

    # Audio
    if msg.audio:
        a = msg.audio
        name = getattr(a, "file_name", None) or f"audio_{msg.message_id}.mp3"
        return (a.file_id, name, getattr(a, "file_size", None))

    # Voice
    if msg.voice:
        v = msg.voice
        return (v.file_id, f"voice_{msg.message_id}.ogg", getattr(v, "file_size", None))

    # Video note
    if msg.video_note:
        vn = msg.video_note
        return (vn.file_id, f"video_note_{msg.message_id}.mp4", getattr(vn, "file_size", None))

    # Sticker (static .webp or video .webm)
    if msg.sticker:
        s = msg.sticker
        ext = "webm" if getattr(s, "is_video", False) else "webp"
        return (s.file_id, f"sticker_{msg.message_id}.{ext}", getattr(s, "file_size", None))

    return None


async def send_to_discord(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not TELEGRAM_DISCORD_WEBHOOK_URL:
        raise RuntimeError("Environment variable TELEGRAM_DISCORD_WEBHOOK_URL is missing")

    content = build_discord_content(update)
    media = pick_telegram_media(update)

    # No media: send plain message
    if not media:
        r = requests.post(TELEGRAM_DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Discord webhook error: {r.status_code} {r.text}")
        return

    file_id, filename, file_size = media

    # If Telegram gave size and it's above Discord limit: send text only with a note
    if file_size is not None and file_size > DISCORD_MAX_FILE_BYTES:
        note = f"\n\n*(Media skipped: {file_size} bytes > Discord limit {DISCORD_MAX_FILE_BYTES} bytes)*"
        r = requests.post(TELEGRAM_DISCORD_WEBHOOK_URL, json={"content": content + note}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Discord webhook error: {r.status_code} {r.text}")
        return

    # Download file from Telegram into memory
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    buf.seek(0)

    # Discord webhook multipart upload
    payload_json = {"content": content}
    files = {"files[0]": (filename, buf.read())}
    data = {"payload_json": json.dumps(payload_json)}

    r = requests.post(TELEGRAM_DISCORD_WEBHOOK_URL, data=data, files=files, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord webhook error: {r.status_code} {r.text}")


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await send_to_discord(update, context)
        log.info("Forwarded to Discord")
    except Exception as e:
        log.exception("Failed to forward: %s", e)


def main() -> None:
    if not TG_BOT_TOKEN:
        raise RuntimeError("Environment variable TG_BOT_TOKEN is missing")
    if not TELEGRAM_DISCORD_WEBHOOK_URL:
        raise RuntimeError("Environment variable TELEGRAM_DISCORD_WEBHOOK_URL is missing")

    app = Application.builder().token(TG_BOT_TOKEN).build()

    # Listen only to channel posts
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    log.info("Telegram bridge started. Forwarding channel posts to Discord...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()