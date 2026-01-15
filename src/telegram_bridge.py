"""
telegram_bridge.py

Telegram Channel -> Discord Webhook bridge (Python)
- Listens to Telegram channel posts (channel_post)
- Forwards text + media (photo/video/gif/document/etc.) to Discord via a dedicated Discord webhook
- Preserves Telegram "text links" (hyperlinks) by converting them to Discord-friendly Markdown links

Env vars:
  TG_BOT_TOKEN
  TELEGRAM_DISCORD_WEBHOOK_URL
  TELEGRAM_CHANNEL_URL
  DISCORD_MAX_FILE_BYTES (optional, default 8MB)
"""

import io
import os
import json
import logging
from typing import Optional, Tuple, List, Dict, Any

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

def _utf16_offset_to_py_index(s: str, utf16_offset: int) -> int:
    """
    Telegram entity offsets are in UTF-16 code units.
    Convert a UTF-16 offset to a Python string index.
    """
    if utf16_offset <= 0:
        return 0

    count = 0
    for i, ch in enumerate(s):
        # UTF-16 code units: 1 for BMP, 2 for astral symbols (emoji etc.)
        count += 2 if ord(ch) > 0xFFFF else 1
        if count > utf16_offset:
            # Offset points into this character (shouldn't happen for valid entities),
            # but return current index as best effort.
            return i
        if count == utf16_offset:
            return i + 1
    return len(s)


def _apply_telegram_entities_to_discord_markdown(text: str, entities: list) -> str:
    """
    Convert Telegram message entities to Discord-friendly Markdown.
    Preserves hyperlinks and basic formatting (bold/italic/underline/strike/code/pre).

    Minimal "80% improvement":
    - If a TEXT_LINK shares the exact same range with formatting (e.g., bold),
      produce combined markdown like **[text](url)** to avoid broken rendering.

    Correctly handles Telegram offsets (UTF-16).
    """
    if not text or not entities:
        return text

    wrappers = {
        "bold": ("**", "**"),
        "italic": ("*", "*"),
        "underline": ("__", "__"),
        "strikethrough": ("~~", "~~"),
        "code": ("`", "`"),
    }

    supported = {
        "text_link", "url", "bold", "italic", "underline", "strikethrough", "code", "pre"
    }
    ents = [e for e in entities if getattr(e, "type", None) in supported]
    if not ents:
        return text

    # Group entities by (offset, length) to combine TEXT_LINK + formatting on same range.
    by_range = {}
    for e in ents:
        key = (getattr(e, "offset", None), getattr(e, "length", None))
        if key[0] is None or key[1] is None:
            continue
        by_range.setdefault(key, []).append(e)

    # We'll process each range once, from end to start.
    ranges = sorted(by_range.keys(), key=lambda k: k[0], reverse=True)

    rendered = text
    for offset, length in ranges:
        # Convert UTF-16 offsets to Python indices in the *current* rendered string.
        start = _utf16_offset_to_py_index(rendered, offset)
        end = _utf16_offset_to_py_index(rendered, offset + length)

        if start < 0 or end < 0 or start >= end or end > len(rendered):
            continue

        segment = rendered[start:end]
        group = by_range[(offset, length)]

        # If it's a URL entity, keep as-is (Discord will link it).
        # But URL can coexist with formatting; Telegram usually uses TEXT_LINK for styled links.
        if any(getattr(e, "type", None) == "url" for e in group) and not any(
            getattr(e, "type", None) == "text_link" for e in group
        ):
            # Apply formatting wrappers if any (rare for 'url' entity)
            fmt_types = [getattr(e, "type", None) for e in group if getattr(e, "type", None) in wrappers]
            # Apply at most one wrapper to avoid weird combos; choose bold > italic > underline > strike > code
            priority = ["bold", "italic", "underline", "strikethrough", "code"]
            chosen = next((t for t in priority if t in fmt_types), None)
            if chosen:
                pre, suf = wrappers[chosen]
                replacement = f"{pre}{segment}{suf}"
                rendered = rendered[:start] + replacement + rendered[end:]
            continue

        # Handle PRE (code block) first — it should not be wrapped by bold etc.
        pre_entity = next((e for e in group if getattr(e, "type", None) == "pre"), None)
        if pre_entity:
            lang = getattr(pre_entity, "language", None)
            if lang:
                replacement = f"```{lang}\n{segment}\n```"
            else:
                replacement = f"```\n{segment}\n```"
            rendered = rendered[:start] + replacement + rendered[end:]
            continue

        # Build base replacement: either plain segment or markdown link
        text_link_entity = next((e for e in group if getattr(e, "type", None) == "text_link"), None)
        if text_link_entity and getattr(text_link_entity, "url", None):
            url = getattr(text_link_entity, "url")
            base = f"[{segment}]({url})"
        else:
            base = segment

        # Apply ONE wrapper if present (covers 80% case: bold link, italic link, etc.)
        fmt_types = [getattr(e, "type", None) for e in group if getattr(e, "type", None) in wrappers]
        priority = ["bold", "italic", "underline", "strikethrough", "code"]
        chosen = next((t for t in priority if t in fmt_types), None)

        if chosen:
            pre, suf = wrappers[chosen]
            replacement = f"{pre}{base}{suf}"
        else:
            replacement = base

        rendered = rendered[:start] + replacement + rendered[end:]

    return rendered

def build_discord_content(update: Update) -> str:
    msg = update.effective_message

    raw_text = msg.text or msg.caption or ""
    if not raw_text:
        raw_text = "(media-only post)"

    entities = msg.entities if msg.text else msg.caption_entities
    text = _apply_telegram_entities_to_discord_markdown(raw_text, entities or [])

    lines = [text]

    # Branded Telegram subscribe link (Markdown, does not steal Discord embed)
    if TELEGRAM_CHANNEL_URL:
        lines.append(f"\n[Concordium News — subscribe](<{TELEGRAM_CHANNEL_URL}>)")

    return "\n".join(lines)


def pick_telegram_media(update: Update) -> Optional[Tuple[str, str, Optional[int]]]:
    """
    Returns (file_id, filename, file_size) or None if no media found.
    Supports: photo, video, animation (GIF), document, audio, voice, video_note, sticker.
    """
    msg = update.effective_message
    if not msg:
        return None

    if msg.photo:
        photo = msg.photo[-1]
        return (photo.file_id, f"photo_{msg.message_id}.jpg", getattr(photo, "file_size", None))

    if msg.video:
        v = msg.video
        return (
            v.file_id,
            getattr(v, "file_name", None) or f"video_{msg.message_id}.mp4",
            getattr(v, "file_size", None),
        )

    if msg.animation:
        a = msg.animation
        name = getattr(a, "file_name", None) or f"animation_{msg.message_id}.mp4"
        return (a.file_id, name, getattr(a, "file_size", None))

    if msg.document:
        d = msg.document
        name = getattr(d, "file_name", None) or f"document_{msg.message_id}"
        return (d.file_id, name, getattr(d, "file_size", None))

    if msg.audio:
        a = msg.audio
        name = getattr(a, "file_name", None) or f"audio_{msg.message_id}.mp3"
        return (a.file_id, name, getattr(a, "file_size", None))

    if msg.voice:
        v = msg.voice
        return (v.file_id, f"voice_{msg.message_id}.ogg", getattr(v, "file_size", None))

    if msg.video_note:
        vn = msg.video_note
        return (vn.file_id, f"video_note_{msg.message_id}.mp4", getattr(vn, "file_size", None))

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

    if not media:
        r = requests.post(TELEGRAM_DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Discord webhook error: {r.status_code} {r.text}")
        return

    file_id, filename, file_size = media

    if file_size is not None and file_size > DISCORD_MAX_FILE_BYTES:
        note = f"\n\n*(Media skipped: {file_size} bytes > Discord limit {DISCORD_MAX_FILE_BYTES} bytes)*"
        r = requests.post(TELEGRAM_DISCORD_WEBHOOK_URL, json={"content": content + note}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Discord webhook error: {r.status_code} {r.text}")
        return

    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    buf.seek(0)

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

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    log.info("Telegram bridge started. Forwarding channel posts to Discord...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()