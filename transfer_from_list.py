"""
transfer_from_list.py

Reads list_owned_files.jsonl (produced by list_owned_files.py) and transfers
ownership of every item from the old owner to the new owner.

  - Files first, using force_move so they escape any new-owner-owned parent.
  - Folders deepest-first (depth computed from parent relationships), also
    via move so inherited permissions can't block them.
"""

import json
import time

from googleapiclient.errors import HttpError

from transfer_tree import (
    build_service,
    OLD_OWNER_TOKEN,
    NEW_OWNER_TOKEN,
    OLD_OWNER_EMAIL,
    NEW_OWNER_EMAIL,
    DRY_RUN,
    SLEEP_BETWEEN_ITEMS,
    get_my_drive_root,
    load_done_ids,
    log_event,
    decode_error,
    transfer_file_via_move,
    transfer_folder_via_move,
    find_perm_for,
    delete_permission,
    is_direct,
)

INPUT_FILE = "list_owned_files.jsonl"


def load_items():
    items = []
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def compute_depths(folders: list) -> dict:
    """
    Returns {folder_id: depth} by following parent chains within the owned set.
    Folders whose parent isn't in the owned set get depth 0.
    """
    owned_ids = {f["id"] for f in folders}
    parent_map = {f["id"]: (f.get("parents") or []) for f in folders}
    depth_cache = {}

    def depth_of(fid, visited=None):
        if fid in depth_cache:
            return depth_cache[fid]
        if visited is None:
            visited = set()
        if fid in visited:
            depth_cache[fid] = 0
            return 0
        visited.add(fid)
        parents = parent_map.get(fid, [])
        owned_parents = [p for p in parents if p in owned_ids]
        if not owned_parents:
            depth_cache[fid] = 0
        else:
            depth_cache[fid] = 1 + max(depth_of(p, visited) for p in owned_parents)
        return depth_cache[fid]

    for f in folders:
        depth_of(f["id"])
    return depth_cache


def main():
    print("=== transfer_from_list.py ===")
    print(f"  Input : {INPUT_FILE}")
    print(f"  DRY_RUN: {DRY_RUN}\n")

    print("[AUTH] Authenticating both accounts...")
    old_svc = build_service(OLD_OWNER_TOKEN)
    new_svc = build_service(NEW_OWNER_TOKEN)
    print("[AUTH] Done.\n")

    old_root = get_my_drive_root(old_svc)
    done_ids = load_done_ids()
    if done_ids:
        print(f"[INFO] {len(done_ids)} items already done (from log).\n")

    items = load_items()
    print(f"[LOAD] {len(items)} items from {INPUT_FILE}")

    folders = [x for x in items if x.get("mimeType") == "application/vnd.google-apps.folder"]
    files   = [x for x in items if x.get("mimeType") != "application/vnd.google-apps.folder"]

    print(f"       {len(files)} file(s), {len(folders)} folder(s)\n")

    # ── Files ─────────────────────────────────────────────────────────────────
    print(f"=== Transferring {len(files)} file(s) ===\n")
    f_done = f_failed = f_skipped = 0
    for i, f in enumerate(files, 1):
        if f["id"] in done_ids:
            f_skipped += 1
            continue
        print(f"[F {i}/{len(files)}] {f['name']}")
        if DRY_RUN:
            f_done += 1
            continue
        if transfer_file_via_move(old_svc, new_svc, f["id"], f["name"], old_root, force_move=True):
            f_done += 1
            done_ids.add(f["id"])
            print("  [OK]")
        else:
            f_failed += 1
        time.sleep(SLEEP_BETWEEN_ITEMS)

    print(f"\n[FILES] Transferred: {f_done} | Skipped: {f_skipped} | Failed: {f_failed}\n")

    # ── Unblock old_root before folders ────────────────────────────────────
    # If the new owner has a direct writer on the old owner's root, every
    # folder moved there temporarily will still inherit the new owner as
    # writer and block the transfer.
    print("[UNBLOCK] Checking if new owner has direct writer on old owner's root...")
    root_perm = find_perm_for(old_svc, old_root, NEW_OWNER_EMAIL)
    if root_perm and is_direct(root_perm) and root_perm.get("role") in ("writer", "reader"):
        print(f"  [UNBLOCK] Found direct {root_perm['role']} on old owner's root — removing...")
        if not DRY_RUN:
            if delete_permission(old_svc, old_root, root_perm["id"]):
                print("  [UNBLOCK] Done.")
            else:
                print("  [UNBLOCK] Failed — folder transfers may still be blocked.")
    else:
        print("  [UNBLOCK] No direct new-owner writer on old owner's root — OK.\n")

    # ── Folders deepest-first ─────────────────────────────────────────────────
    depths = compute_depths(folders)
    folders_sorted = sorted(folders, key=lambda x: -depths.get(x["id"], 0))

    print(f"=== Transferring {len(folders_sorted)} folder(s) deepest-first ===\n")
    d_done = d_failed = d_skipped = 0
    for i, f in enumerate(folders_sorted, 1):
        if f["id"] in done_ids:
            d_skipped += 1
            continue
        owners = [o.get("emailAddress") for o in f.get("owners", [])]
        if NEW_OWNER_EMAIL in owners and OLD_OWNER_EMAIL not in owners:
            d_skipped += 1
            log_event({"status": "already_owner", "file_id": f["id"], "file_name": f["name"]})
            continue
        depth = depths.get(f["id"], 0)
        print(f"[D {i}/{len(folders_sorted)}] depth={depth}  {f['name']}")
        if DRY_RUN:
            d_done += 1
            continue
        if transfer_folder_via_move(old_svc, new_svc, f["id"], f["name"], old_root):
            d_done += 1
            done_ids.add(f["id"])
            print("  [OK]")
        else:
            d_failed += 1
        time.sleep(SLEEP_BETWEEN_ITEMS)

    print(f"\n[FOLDERS] Transferred: {d_done} | Skipped: {d_skipped} | Failed: {d_failed}")
    print("\n=== ALL DONE ===")


if __name__ == "__main__":
    main()
