from flask import Flask, request
import requests
import json
import re
import hmac
import hashlib
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCOURSE_SECRET = os.getenv("DISCOURSE_SECRET")

if not DISCORD_WEBHOOK_URL:
    raise RuntimeError("Environment variable DISCORD_WEBHOOK_URL is missing")
if not DISCOURSE_SECRET:
    raise RuntimeError("Environment variable DISCOURSE_SECRET is missing")

BLOCKED_USERS = {"system", "discobot", "anonymous"}
BLOCKED_EVENTS = {"topic_closed_status_updated"}


def strip_html_tags(text):
    return re.sub(r"<.*?>", "", text)


def verify_signature(data, signature):
    if not DISCOURSE_SECRET or not signature:
        return False
    computed = hmac.new(
        DISCOURSE_SECRET.encode(), data, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={computed}", signature)


def is_blocked_user(data):
    if "user" in data and data["user"]["username"].lower() in BLOCKED_USERS:
        return True
    if "post" in data and data["post"]["username"].lower() in BLOCKED_USERS:
        return True
    if "topic" in data:
        if (
            "created_by" in data["topic"]
            and data["topic"]["created_by"]["username"].lower() in BLOCKED_USERS
        ):
            return True
        if (
            "last_poster" in data["topic"]
            and data["topic"]["last_poster"]["username"].lower() in BLOCKED_USERS
        ):
            return True
    return False


def is_private_message(data):
    if "topic" in data and data["topic"].get("topic_archetype") == "private_message":
        return True
    if "post" in data and data["post"].get("topic_archetype") == "private_message":
        return True
    return False


@app.route("/webhook", methods=["POST"])
def webhook():
    event_type = request.headers.get("X-Discourse-Event")
    signature = request.headers.get("X-Discourse-Event-Signature")
    raw_data = request.data
    data = request.json

    print(f"Received event: {event_type}")

    if not verify_signature(raw_data, signature):
        print("❌ Invalid or missing signature! Request blocked.")
        return "Forbidden", 403

    if event_type in BLOCKED_EVENTS:
        print(f"🔹 Skipping event `{event_type}` (not relevant for Discord).")
        return "OK", 200

    if is_blocked_user(data):
        print("🔹 Skipping message from blocked user (privacy protection).")
        return "OK", 200

    if is_private_message(data):
        print("🔹 Skipping private message (admin notification).")
        return "OK", 200

    message_content = f"📢 **New event from Discourse:** `{event_type}`\n"

    if "topic" in data:
        title = data["topic"]["title"]
        url = f"https://forum.concordium.com/t/{data['topic']['id']}"
        message_content += f"**Topic:** [{title}]({url})\n"

    if "post" in data:
        raw_content = data["post"]["cooked"]
        cleaned_content = strip_html_tags(raw_content)
        url = f"https://forum.concordium.com/t/{data['post']['topic_id']}?p={data['post']['id']}"
        message_content += f"**Post:** {cleaned_content[:100]}... ([Read more]({url}))\n"

    if "user" in data:
        username = data["user"]["username"]
        message_content += f"👤 **User:** {username}\n"

    payload = {"content": message_content}

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Failed to send message to Discord: {e}")
        return "Bad Gateway", 502

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
