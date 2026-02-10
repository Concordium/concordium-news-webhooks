"""
telegram_bridge.py

Telegram Channel -> Discord Webhook bridge (Python)
- Listens to Telegram channel posts (channel_post)
- Forwards text + media (photo/video/gif/document/etc.) to Discord via a dedicated Discord webhook
- Preserves Telegram "text links" (hyperlinks) by converting them to Discord-friendly Markdown links
- Sends Discord webhook requests asynchronously via httpx.AsyncClient

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
import asyncio
from typing import Optional, Tuple, List, Any

import httpx
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

http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=30)
    return http_client


async def post_to_discord_webhook(client: httpx.AsyncClient, **kwargs) -> httpx.Response:
    for attempt in range(3):
        try:
            r = await client.post(TELEGRAM_DISCORD_WEBHOOK_URL, **kwargs)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Retry on rate limit or server errors only
            if status != 429 and status < 500:
                raise
            if attempt == 2:
                raise
        except httpx.RequestError:
            if attempt == 2:
                raise

        await asyncio.sleep(2 ** attempt)


async def close_http_client(_: Application) -> None:
    global http_client
    if http_client is not None:
        await http_client.aclose()
        http_client = None


def _utf16_offset_to_py_index(s: str, utf16_offset: int) -> int:
    """
    Telegram entity offsets are in UTF-16 code units.
    Convert a UTF-16 offset to a Python string index.
    """
    if utf16_offset <= 0:
        return 0

    count = 0
    for i, ch in enumerate(s):
        count += 2 if ord(ch) > 0xFFFF else 1
        if count > utf16_offset:
            return i
        if count == utf16_offset:
            return i + 1
    return len(s)


def _apply_telegram_entities_to_discord_markdown(text: str, entities: List[Any]) -> str:
    """
    Convert Telegram message entities to Discord-friendly Markdown.
    Supports: text_link, bold, italic, underline, strikethrough, code, pre.
    Telegram offsets are in UTF-16 code units -> converted via _utf16_offset_to_py_index.
    """
    if not text or not entities:
        return text

    # Telegram entity type -> (open, close) markers
    fmt_markers = {
        "bold": ("**", "**"),
        "italic": ("*", "*"),
        "underline": ("__", "__"),
        "strikethrough": ("~~", "~~"),
        "code": ("`", "`"),
    }

    supported = set(fmt_markers.keys()) | {"text_link", "pre"}
    ents = [e for e in entities if getattr(e, "type", None) in supported]
    if not ents:
        return text

    opens: dict[int, List[str]] = {}
    closes: dict[int, List[str]] = {}

    def add_open(i: int, s: str) -> None:
        opens.setdefault(i, []).append(s)

    def add_close(i: int, s: str) -> None:
        closes.setdefault(i, []).append(s)

    # Priorities for deterministic nesting when multiple markers start/end at same position.
    # Open: outer first. Close: inner first.
    # (This keeps patterns like **[text](url)** correct.)
    open_priority = {
        "pre": 0,
        "bold": 1,
        "italic": 2,
        "underline": 3,
        "strikethrough": 4,
        "code": 5,
        "text_link": 10,
    }
    close_priority = {
        "text_link": 1,
        "code": 5,
        "strikethrough": 6,
        "underline": 7,
        "italic": 8,
        "bold": 9,
        "pre": 20,
    }

    # Build marker maps based on the ORIGINAL text (important for UTF-16 offsets).
    for e in ents:
        etype = getattr(e, "type", None)
        off = getattr(e, "offset", None)
        ln = getattr(e, "length", None)
        if off is None or ln is None:
            continue

        start = _utf16_offset_to_py_index(text, off)
        end = _utf16_offset_to_py_index(text, off + ln)

        if start < 0 or end < 0 or start >= end or end > len(text):
            continue

        if etype == "text_link":
            url = getattr(e, "url", None)
            if not url:
                continue
            add_open(start, "[")
            add_close(end, f"]({url})")
            continue

        if etype == "pre":
            # Telegram "pre" can have optional language
            lang = getattr(e, "language", None)
            if lang:
                add_open(start, f"```{lang}\n")
            else:
                add_open(start, "```\n")
            add_close(end, "\n```")
            continue

        if etype in fmt_markers:
            op, cl = fmt_markers[etype]
            add_open(start, op)
            add_close(end, cl)

    def marker_type_open(s: str) -> str:
        if s == "[":
            return "text_link"
        if s.startswith("```"):
            return "pre"
        for k, (op, _) in fmt_markers.items():
            if s == op:
                return k
        return "bold"

    def marker_type_close(s: str) -> str:
        if s.startswith("]("):
            return "text_link"
        if s == "\n```":
            return "pre"
        for k, (_, cl) in fmt_markers.items():
            if s == cl:
                return k
        return "bold"

    for i in opens:
        opens[i].sort(key=lambda s: open_priority.get(marker_type_open(s), 100))
    for i in closes:
        closes[i].sort(key=lambda s: close_priority.get(marker_type_close(s), 100))

    # Build output in one pass:
    # close markers first, then open markers, then the character.
    out: List[str] = []
    for i in range(len(text) + 1):
        if i in closes:
            out.extend(closes[i])
        if i in opens:
            out.extend(opens[i])
        if i < len(text):
            out.append(text[i])

    return "".join(out)


def build_discord_content(update: Update) -> str:
    msg = update.effective_message

    raw_text = msg.text or msg.caption or ""
    if not raw_text:
        raw_text = "(media-only post)"

    entities = msg.entities if msg.text else msg.caption_entities
    text = _apply_telegram_entities_to_discord_markdown(raw_text, entities or [])

    lines = [text]

    # Branded Telegram subscribe link (Markdown, wrapped in <...> to prevent Discord embed)
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

    client = await get_http_client()

    # No media: send plain message
    if not media:
        try:
            await post_to_discord_webhook(client, json={"content": content})
        except httpx.RequestError as e:
            raise RuntimeError(f"Discord webhook request error: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Discord webhook error: {e.response.status_code} {e.response.text}"
            ) from e
        return

    file_id, filename, file_size = media

    # If Telegram gave size and it's above Discord limit: send text only with a note
    if file_size is not None and file_size > DISCORD_MAX_FILE_BYTES:
        note = f"\n\n*(Media skipped: {file_size} bytes > Discord limit {DISCORD_MAX_FILE_BYTES} bytes)*"
        try:
            await post_to_discord_webhook(client, json={"content": content + note})
        except httpx.RequestError as e:
            raise RuntimeError(f"Discord webhook request error: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Discord webhook error: {e.response.status_code} {e.response.text}"
            ) from e
        return

    # Download file from Telegram into memory
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    buf.seek(0)

    # Discord webhook multipart upload
    payload_json = {"content": content}

    try:
        await post_to_discord_webhook(
            client,
            data={"payload_json": json.dumps(payload_json)},
            files={"files[0]": (filename, buf.getvalue())},
        )
    except httpx.RequestError as e:
        raise RuntimeError(f"Discord webhook request error: {e}") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Discord webhook error: {e.response.status_code} {e.response.text}"
        ) from e


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

    app = (
        Application.builder()
        .token(TG_BOT_TOKEN)
        .post_shutdown(close_http_client)
        .build()
    )

    # Listen only to channel posts
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    log.info("Telegram bridge started. Forwarding channel posts to Discord...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()