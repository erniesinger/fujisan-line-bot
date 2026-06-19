"""
drive_watcher.py
Lists files in the Vineyard AI Google Drive folder, dedupes against a seen-list,
returns metadata + downloaded bytes for any new files.

Env vars required:
- GOOGLE_SERVICE_ACCOUNT_JSON: full JSON contents of the service account key
- VINEYARD_FOLDER_ID:         Drive folder id (the Vineyard AI folder)
"""

import os
import io
import json
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TMP_SEEN_PATH = "/tmp/seen_files.json"
BASELINE_SEEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "seen_files.json",
)


def _drive_service():
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _load_baseline():
    try:
        with open(BASELINE_SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _load_tmp_seen():
    try:
        with open(TMP_SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _load_seen():
    return _load_baseline() | _load_tmp_seen()


def _save_tmp_seen(seen):
    try:
        with open(TMP_SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
    except Exception:
        pass


def list_folder(folder_id):
    svc = _drive_service()
    q = f"'{folder_id}' in parents and trashed = false"
    fields = "files(id, name, mimeType, modifiedTime), nextPageToken"
    results = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=q, fields=fields, pageToken=page_token,
            pageSize=100, orderBy="modifiedTime",
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def download_file(file_id):
    svc = _drive_service()
    request = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()


def get_new_files(folder_id, mark_seen=True):
    seen = _load_seen()
    all_files = list_folder(folder_id)
    new = []
    for f in all_files:
        if f["id"] in seen:
            continue
        try:
            data = download_file(f["id"])
        except Exception as e:
            print(f"[drive_watcher] download failed for {f.get('name')}: {e}", flush=True)
            continue
        f["data"] = data
        new.append(f)
    if mark_seen and new:
        tmp_seen = _load_tmp_seen()
        for f in new:
            tmp_seen.add(f["id"])
        _save_tmp_seen(tmp_seen)
    return new


def seed_seen(folder_id):
    files = list_folder(folder_id)
    seen = {f["id"] for f in files}
    _save_tmp_seen(seen)
    print(f"[drive_watcher] seeded {len(seen)} existing file(s) as seen", flush=True)
    return len(seen)


if __name__ == "__main__":
    import sys
    folder = os.environ["VINEYARD_FOLDER_ID"]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "seed":
        seed_seen(folder)
    else:
        files = list_folder(folder)
        for f in files:
            print(f"{f['modifiedTime']}  {f['mimeType']:40}  {f['name']}", flush=True)
