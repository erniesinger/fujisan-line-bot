import os
import io
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
    FileMessage,
)

app = Flask(__name__)

# --- LINE credentials (from Render env vars) ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
PERPLEXITY_API_KEY = os.environ["PERPLEXITY_API_KEY"]

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Bot identity (used to detect @mentions in groups) ---
try:
    BOT_INFO = line_bot_api.get_bot_info()
    BOT_USER_ID = BOT_INFO.user_id
    BOT_DISPLAY_NAME = BOT_INFO.display_name
except Exception:
    BOT_USER_ID = None
    BOT_DISPLAY_NAME = "Fujisan Assistant"

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

SYSTEM_PROMPT = (
    "You are the Fujisan Winery assistant for a Japanese winery team. "
    "Always reply in the same language as the user (Japanese or English). "
    "Be concise (3-6 bullet points unless asked for more detail). "
    "For vineyard photos, comment on vine health, canopy, leaves, disease signs, fruit set. "
    "For wine labels or business documents, summarize the key contents. "
    "For PDFs and spreadsheets, give a structured summary of what they contain "
    "and flag anything notable (deadlines, large numbers, anomalies). "
    "Use up-to-date web information when answering factual questions."
)


# ---------------- Perplexity helpers ----------------

def ask_perplexity_text(user_text: str, model: str = "sonar") -> str:
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
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
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    user_prompt = user_caption or (
        "Please analyze this photo for Fujisan Winery. "
        "If it's a vineyard or vine photo, comment on vine health, canopy, "
        "leaves, fruit, and anything notable. "
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
        r = requests.post(PERPLEXITY_URL, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(error analyzing image: {e})"


# ---------------- File parsers ----------------

def extract_pdf_text(data: bytes, max_chars: int = 12000) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        text = "\n".join(chunks).strip()
        return text[:max_chars] if text else "(no extractable text in PDF)"
    except Exception as e:
        return f"(PDF parse error: {e})"


def extract_xlsx_summary(data: bytes, max_chars: int = 8000) -> str:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        out = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            out.append(f"## Sheet: {sheet_name}")
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    out.append(" | ".join(cells))
                    row_count += 1
                if row_count >= 60:
                    out.append("... (truncated)")
                    break
        joined = "\n".join(out)
        return joined[:max_chars] if joined else "(empty workbook)"
    except Exception as e:
        return f"(Excel parse error: {e})"


def extract_csv_summary(data: bytes, max_chars: int = 8000) -> str:
    try:
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()[:60]
        if len(text.splitlines()) > 60:
            lines.append("... (truncated)")
        joined = "\n".join(lines)
        return joined[:max_chars]
    except Exception as e:
        return f"(CSV parse error: {e})"


# ---------------- LINE helpers ----------------

def is_mentioned(event) -> bool:
    msg = event.message
    mention = getattr(msg, "mention", None)
    if mention and getattr(mention, "mentionees", None):
        for m in mention.mentionees:
            if BOT_USER_ID and getattr(m, "user_id", None) == BOT_USER_ID:
                return True
    return False


def strip_mention(text: str) -> str:
    parts = text.strip().split(" ", 1)
    if parts and parts[0].startswith("@"):
        return parts[1] if len(parts) > 1 else ""
    return text


def download_content(message_id: str) -> bytes:
    content = line_bot_api.get_message_content(message_id)
    return b"".join(chunk for chunk in content.iter_content())


def safe_reply(event, text: str):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text[:4900]))


# ---------------- Event handlers ----------------

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


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    source_type = event.source.type  # 'user', 'group', or 'room'

    # In 1:1 chats, always respond.
    # In groups, only respond when @mentioned.
    if source_type != "user" and not is_mentioned(event):
        return

    user_text = strip_mention(event.message.text)
    if not user_text:
        reply = "はい、何かお手伝いできますか? / Yes, how can I help?"
    else:
        reply = ask_perplexity_text(user_text)
    safe_reply(event, reply)


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    """Analyze every image, in 1:1 chats AND in groups."""
    try:
        image_bytes = download_content(event.message.id)
        analysis = ask_perplexity_image(image_bytes)
    except Exception as e:
        analysis = f"(error: {e})"
    safe_reply(event, analysis)


@handler.add(MessageEvent, message=FileMessage)
def handle_file(event):
    """Analyze PDFs, Excel, and CSV files in any chat."""
    file_name = (event.message.file_name or "").lower()
    try:
        data = download_content(event.message.id)
    except Exception as e:
        safe_reply(event, f"(could not download file: {e})")
        return

    if file_name.endswith(".pdf"):
        body = extract_pdf_text(data)
        prompt = (
            f"The team shared a PDF named '{event.message.file_name}'. "
            "Summarize the key contents in 4-8 bullets, in the document's "
            "primary language. Flag any deadlines, large numbers, or "
            "anomalies. Here is the extracted text:\n\n" + body
        )
        reply = ask_perplexity_text(prompt, model="sonar-pro")
    elif file_name.endswith((".xlsx", ".xls")):
        body = extract_xlsx_summary(data)
        prompt = (
            f"The team shared a spreadsheet named '{event.message.file_name}'. "
            "Summarize what it contains, list the sheets, and highlight any "
            "notable totals, anomalies, or trends. Here is a sample of rows:\n\n" + body
        )
        reply = ask_perplexity_text(prompt, model="sonar-pro")
    elif file_name.endswith(".csv"):
        body = extract_csv_summary(data)
        prompt = (
            f"The team shared a CSV named '{event.message.file_name}'. "
            "Describe the columns, summarize the data, and highlight anything "
            "notable. Here is a sample:\n\n" + body
        )
        reply = ask_perplexity_text(prompt, model="sonar-pro")
    else:
        # Unsupported file type — stay silent to avoid noise
        return

    safe_reply(event, reply)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
