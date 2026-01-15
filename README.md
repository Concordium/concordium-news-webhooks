# Discourse & Telegram → Discord Webhooks

This repository contains two independent services that forward events to Discord using webhooks.

Both services are written in Python and deployed using Docker / Docker Compose.

---

## Services Overview

### 1. Discourse → Discord Webhook

**Purpose:**  
Receives webhook events from Discourse and forwards them to a Discord channel.

**Entrypoint:**  
`src/discourse_webhook.py`

**How it works:**
- Discourse sends HTTP webhook events
- The service validates the secret
- The payload is reformatted and sent to Discord via a webhook

**Required environment variables:**
- `DISCORD_WEBHOOK_URL`
- `DISCOURSE_SECRET`

---

### 2. Telegram Channel → Discord Webhook

**Purpose:**  
Mirrors posts from a Telegram **channel** into a Discord channel.

**Entrypoint:**  
`src/telegram_bridge.py`

**How it works:**
- A Telegram bot listens to channel posts (`channel_post`)
- Text and media (images, videos, GIFs, documents, etc.) are forwarded to Discord
- The Telegram channel title in Discord is clickable (uses channel invite link)

**Required environment variables:**
- `TG_BOT_TOKEN`
- `TELEGRAM_DISCORD_WEBHOOK_URL`
- `TELEGRAM_CHANNEL_URL`

---

## Configuration

Copy the example env file and fill in real values:

```bash
cp .env.example .env