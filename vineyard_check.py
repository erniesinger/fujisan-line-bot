"""
vineyard_check.py
Runs on a Render Cron schedule. For each new file in the Vineyard AI Drive
folder: download -> analyze with Perplexity Sonar -> push summary to LINE.
"""

import os
import sys
import traceback

from linebot import LineBotApi
from linebot.models import TextSendMessage

from app import (
    ask_perplexity_text,
    ask_perplexity_image,
    extract_pdf_text,
    extract_xlsx_summary,
    extract_csv_summary,
)
from drive_watcher import get_new_files

LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GROUP_ID   = os.environ["VINEYARD_GROUP_ID"]
FOLDER_ID  = os.environ["VINEYARD_FOLDER_ID"]

line_bot_api = LineBotApi(LINE_TOKEN)

VINEYARD_SYSTEM_PROMPT = """あなたは富士山ワイナリーの圃場アドバイザーです。
Aimee からアップロードされた畑の写真・評価レポート・栽培スケジュールを分析し、
日本語で簡潔かつ実務的なコメントを返します。

着目点:
- 生育ステージ（開花・結実・果粒肥大期 等）
- 病害の予兆: べと病・黒とう病・灰色かび病・晩腐病
- 害虫: コガネムシ、ハスモンヨトウ、その他
- 推奨アクション（薬剤散布タイミング・草刈り・誘引・摘房 等）の優先順位
- リスクが高いものは「⚠️」、要観察は「👀」、良好は「✅」を文頭に付ける

出力フォーマット:
📄 ファイル: <name>
要点: 2-4行
推奨アクション: 箇条書きで最大3件（優先順位順）
"""


def chunk(text, size=4900):
    return [text[i:i+size] for i in range(0, len(text), size)]


def push(text):
    for part in chunk(text):
        line_bot_api.push_message(GROUP_ID, TextSendMessage(text=part))


def analyze_file(f):
    name = f["name"]
    mime = f["mimeType"]
    data = f["data"]

    header = f"📄 ファイル: {name}"

    if mime == "application/pdf":
        text = extract_pdf_text(data)
        prompt = f"{VINEYARD_SYSTEM_PROMPT}\n\n以下はPDFから抽出した本文です:\n{text}"
        reply = ask_perplexity_text(prompt, model="sonar")
        return f"{header}\n{reply}"

    if mime.startswith("image/"):
        reply = ask_perplexity_image(data, VINEYARD_SYSTEM_PROMPT)
        return f"{header}\n{reply}"

    if mime in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        summary = extract_xlsx_summary(data)
        prompt = f"{VINEYARD_SYSTEM_PROMPT}\n\n以下はExcelの要約です:\n{summary}"
        reply = ask_perplexity_text(prompt, model="sonar")
        return f"{header}\n{reply}"

    if mime == "text/csv":
        summary = extract_csv_summary(data)
        prompt = f"{VINEYARD_SYSTEM_PROMPT}\n\n以下はCSVの要約です:\n{summary}"
        reply = ask_perplexity_text(prompt, model="sonar")
        return f"{header}\n{reply}"

    if mime.startswith("application/vnd.google-apps"):
        return f"{header}\n(Google ネイティブ形式は次回対応します)"

    return f"{header}\n(対応していない形式: {mime})"


def main():
    print(f"[vineyard_check] checking folder {FOLDER_ID}", flush=True)
    try:
        new_files = get_new_files(FOLDER_ID, mark_seen=True)
    except Exception as e:
        print(f"[vineyard_check] drive error: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)

    if not new_files:
        print("[vineyard_check] no new files", flush=True)
        return

    print(f"[vineyard_check] {len(new_files)} new file(s)", flush=True)
    push(f"🍇 Vineyard AI フォルダに {len(new_files)} 件の新しいファイルが追加されました。分析を開始します。")

    for f in new_files:
        try:
            msg = analyze_file(f)
        except Exception as e:
            msg = f"📄 ファイル: {f.get('name')}\n分析エラー: {e}"
            traceback.print_exc()
        push(msg)


if __name__ == "__main__":
    main()
