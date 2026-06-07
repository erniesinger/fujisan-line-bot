import os
import base64
import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    ImageMessage,
)

app = Flask(__name__)

# --- LINE credentials (from Render env vars) ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
PERPLEXITY_API_KEY = os.environ["PERPLEXITY_API_KEY"]

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Bot identity (used to detect @mentions in groups) ---
# Fetched once at startup. Falls back to None if call fails.
try:
    BOT_INFO = line_bot_api.get_bot_info()
    BOT_USER_ID = BOT_INFO.user_id
    BOT_DISPLAY_NAME = BOT_INFO.display_name
except Exception:
    BOT_USER_ID = None
    BOT_DISPLAY_NAME = "Fujisan Assistant"

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

SYSTEM_PROMPT = (
    "You are the Fujisan Winery assistant. "
    "Reply concisely and in the same language as the user's message "
    "(Japanese or English). When the user sends a vineyard photo, "
    "describe what you see and give viticulture observations "
    "(canopy, leaves, disease signs, fruit set, etc.). "
    "When asked factual questions, use up-to-date web information."
)


# ---------------- helpers ----------------

def ask_perplexity_text(user_text: str) -> str:
    """Send a text question to Perplexity Sonar and return the answer."""
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    }
    try:
        r = requests.post(PERPLEXITY_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(error contacting Perplexity: {e})"


def ask_perplexity_image(image_bytes: bytes, user_caption: str = "") -> str:
    """Send an image (base64) plus optional caption to a vision-capable Sonar model."""
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    user_prompt = user_caption or (
        "Please analyze this photo for Fujisan Winery. "
        "If it's a vineyard or vine photo, comment on vine health, "
        "canopy, leaves, fruit, and anything notable. "
        "If it's a wine label or document, summarize what it shows."
    )
    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }
    try:
        r = requests.post(PERPLEXITY_URL, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(error analyzing image: {e})"


def is_addressed_to_bot(event) -> bool:
    """In 1:1 chats always respond. In groups/rooms, only respond when mentioned."""
    source_type = event.source.type  # 'user', 'group', or 'room'
    if source_type == "user":
        return True

    msg = event.message
    mention = getattr(msg, "mention", None)
    if mention and getattr(mention, "mentionees", None):
        for m in mention.mentionees:
            if BOT_USER_ID and getattr(m, "user_id", None) == BOT_USER_ID:
                return True
    return False


def strip_mention(text: str) -> str:
    """Remove leading @MentionName so the bot doesn't quote itself back."""
    parts = text.strip().split(" ", 1)
    if parts and parts[0].startswith("@"):
        return parts[1] if len(parts) > 1 else ""
    return text


# ---------------- routes ----------------

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET"])
def health():
    return f"Fujisan LINE bot running. Bot: {BOT_DISPLAY_NAME}"


# ---------------- LINE event handlers ----------------

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    if not is_addressed_to_bot(event):
        return
    user_text = strip_mention(event.message.text)
    if not user_text:
        reply = "はい、何かお手伝いできますか? / Yes, how can I help?"
    else:
        reply = ask_perplexity_text(user_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply[:4900]))


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    # In groups, only analyze images if the bot is mentioned in the same message.
    # (LINE attaches mentions to text, not images, so for images we require a
    # 1:1 chat OR a follow-up text mention. Simplest rule: always analyze in 1:1,
    # ignore in groups unless someone @mentions the bot in a text reply.)
    if event.source.type != "user":
        return

    message_id = event.message.id
    content = line_bot_api.get_message_content(message_id)
    image_bytes = b"".join(chunk for chunk in content.iter_content())
    analysis = ask_perplexity_image(image_bytes)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=analysis[:4900]))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
