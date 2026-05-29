"""
list_owned_files.py

Lists every file/folder still owned by the old owner, regardless of where
it lives (My Drive, inside shared folders, inside the new owner's drive,
etc.). Writes results to list_owned_files.jsonl and prints a summary.
"""

import json
import os
from googleapiclient.errors import HttpError
from transfer_tree import build_service, OLD_OWNER_TOKEN, OLD_OWNER_EMAIL, decode_error

OUTPUT_FILE = "list_owned_files.jsonl"


def main():
    print(f"[AUTH] Authenticating {OLD_OWNER_EMAIL}...")
    svc = build_service(OLD_OWNER_TOKEN)
    print("[AUTH] Done.\n")

    print(f"[SCAN] Searching for all files owned by {OLD_OWNER_EMAIL}...\n")

    items = []
    page_token = None
    page = 0

    while True:
        try:
            resp = svc.files().list(
                q="'me' in owners and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size, parents, owners)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                orderBy="folder,name",
            ).execute()
        except HttpError as e:
            print(f"[ERROR] {decode_error(e)}")
            break

        batch = resp.get("files", [])
        items.extend(batch)
        page += 1
        print(f"  Page {page}: {len(batch)} items (total so far: {len(items)})")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    folders = [x for x in items if x.get("mimeType") == "application/vnd.google-apps.folder"]
    files   = [x for x in items if x.get("mimeType") != "application/vnd.google-apps.folder"]

    total_bytes = sum(int(x.get("size", 0)) for x in items if x.get("size"))

    print(f"\n[RESULT] Total owned items : {len(items)}")
    print(f"         Folders           : {len(folders)}")
    print(f"         Files             : {len(files)}")
    print(f"         Total size        : {total_bytes / (1024**3):.2f} GB\n")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"[SAVED] {OUTPUT_FILE} ({len(items)} entries)")

    if items:
        print("\n[SAMPLE] First 20 items:")
        for item in items[:20]:
            kind = "DIR " if item.get("mimeType") == "application/vnd.google-apps.folder" else "FILE"
            size = item.get("size", "")
            size_str = f"  {int(size):>12,} B" if size else ""
            print(f"  [{kind}] {item['name'][:60]:<60} {item['id']}{size_str}")


if __name__ == "__main__":
    main()
