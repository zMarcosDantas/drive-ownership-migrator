"""
transfer_tree.py

Core library for bulk Google Drive ownership transfer between two accounts
(OLD_OWNER and NEW_OWNER). Also runnable as a script to process one folder
tree end-to-end via four phases:

  Phase 0 — Fix parents: any folder in the tree whose parent the current
            user can't access (e.g. it's inside the new owner's My Drive
            root because an earlier transfer left it there) is moved to
            the old owner's My Drive root. This cuts off the inherited
            writer chain at its source.

  Phase 1 — Unblock: delete the new owner's direct writer permission on
            every folder in the tree. Without this, files inside inherit
            writer access from the folder, which makes the API's
            permissions.create call a silent no-op and pendingOwner=True
            fail.

  Phase 2 — Transfer files: for every old-owner-owned file in the tree
            (recursive), create a direct writer for the new owner, set
            pendingOwner=True, then accept ownership from the new owner's
            account.

  Phase 3 — Transfer folders deepest-first: same flow as Phase 2 but for
            folders. Deepest-first so that when a folder is transferred,
            none of its contents (already owned by the new owner) trigger
            an inherited blocker.

Configure OLD_OWNER_EMAIL / NEW_OWNER_EMAIL below before running.
Run from any account — the script uses both tokens.
"""

import os
import json
import time
import random
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


SCOPES = ["https://www.googleapis.com/auth/drive"]

OLD_OWNER_TOKEN = "token_old_owner.json"
NEW_OWNER_TOKEN = "token_new_owner.json"
CREDENTIALS_FILE = "credentials.json"

# Configure these two before running.
OLD_OWNER_EMAIL = "old.owner@example.com"
NEW_OWNER_EMAIL = "new.owner@example.com"

# Optional — only used when running transfer_tree.py directly.
TARGET_FOLDER_ID = ""

# Folder in the new owner's drive that finished trees get moved into.
# Set to None to leave items where they are.
ORGANIZE_FOLDER_ID = None

LOG_FILE = "transfer_tree_log.jsonl"

DRY_RUN = False
SLEEP_BETWEEN_ITEMS = 0.1
SLEEP_AFTER_CREATE = 0.2
MAX_RETRIES = 5


# ── Auth ───────────────────────────────────────────────────────────────────────

def build_service(token_file: str):
    creds: Optional[Credentials] = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


# ── Logging / helpers ──────────────────────────────────────────────────────────

def log_event(data: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def decode_error(e: HttpError) -> str:
    if getattr(e, "content", None):
        return e.content.decode("utf-8", errors="replace")
    return str(e)


def is_retryable(e: HttpError) -> bool:
    return getattr(e.resp, "status", None) in [429, 500, 502, 503, 504]


def is_direct(perm: dict) -> bool:
    details = perm.get("permissionDetails", [])
    if not details:
        return True
    return any(not d.get("inherited", True) for d in details)


def get_my_drive_root(service) -> str:
    return service.files().get(fileId="root", fields="id").execute()["id"]


def is_parent_ok(service, parent_id: str) -> bool:
    """A parent is fine if the authenticated user owns it (or it's a shared drive)."""
    try:
        meta = service.files().get(
            fileId=parent_id,
            fields="owners,driveId,ownedByMe",
            supportsAllDrives=True,
        ).execute()
    except HttpError:
        return False  # 404 = inaccessible = bad parent
    if meta.get("driveId"):
        return True
    return meta.get("ownedByMe", False)


def fix_parents(service, file_id: str, name: str, my_root: str) -> bool:
    """Remove any parent not owned by the authenticated user; ensure my_root is among parents."""
    try:
        meta = service.files().get(
            fileId=file_id,
            fields="parents",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        print(f"  [WARN] Could not get parents for {name}: {decode_error(e)}")
        return False

    current = meta.get("parents", []) or []

    bad = [p for p in current if not is_parent_ok(service, p)]
    if not bad:
        return True

    print(f"  [FIX PARENT] {name}: removing {bad}, adding {my_root}")
    log_event({
        "status": "parent_fixed",
        "file_id": file_id,
        "file_name": name,
        "removed_parents": bad,
        "added_parent": my_root,
    })

    if DRY_RUN:
        return True

    add = my_root if my_root not in current else None
    kwargs = {
        "fileId": file_id,
        "supportsAllDrives": True,
        "fields": "id,parents",
        "removeParents": ",".join(bad),
    }
    if add:
        kwargs["addParents"] = add

    try:
        service.files().update(**kwargs).execute()
        return True
    except HttpError as e:
        print(f"  [ERROR] fix parents: {decode_error(e)}")
        return False


def load_done_ids() -> set:
    done = set()
    if not os.path.exists(LOG_FILE):
        return done
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("status") in ("accepted", "already_owner"):
                fid = e.get("file_id")
                if fid:
                    done.add(fid)
    return done


# ── Tree walking ───────────────────────────────────────────────────────────────

def walk_tree(service, root_id: str):
    """
    Returns (folders, files) where:
      folders = [{id, name, depth, owners}]  (root first)
      files   = [{id, name}]  (only old-owner-owned)
    """
    folders = []
    files = []

    # Fetch only the root folder's metadata; descendants we learn from
    # their parent's listing — saving N extra files.get calls.
    try:
        root_meta = service.files().get(
            fileId=root_id,
            fields="id,name,owners",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        print(f"  [WARN] Cannot fetch root folder {root_id}: {decode_error(e)}")
        return folders, files

    folders.append({
        "id": root_id,
        "name": root_meta.get("name", "Untitled"),
        "depth": 0,
        "owners": [o.get("emailAddress") for o in root_meta.get("owners", [])],
    })

    queue = [(root_id, 0)]
    while queue:
        parent_id, depth = queue.pop(0)
        page_token = None
        while True:
            try:
                resp = service.files().list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, owners)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
            except HttpError as e:
                print(f"  [WARN] walk_tree list failed for {parent_id}: {decode_error(e)}")
                break
            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime in ("application/vnd.google-apps.shortcut",
                            "application/vnd.google-apps.site"):
                    continue
                if mime == "application/vnd.google-apps.folder":
                    folders.append({
                        "id": f["id"],
                        "name": f.get("name", "Untitled"),
                        "depth": depth + 1,
                        "owners": [o.get("emailAddress") for o in f.get("owners", [])],
                    })
                    queue.append((f["id"], depth + 1))
                else:
                    owners = [o.get("emailAddress") for o in f.get("owners", [])]
                    if OLD_OWNER_EMAIL in owners:
                        files.append({
                            "id": f["id"],
                            "name": f.get("name", "Untitled"),
                        })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    return folders, files


# ── Permission helpers ─────────────────────────────────────────────────────────

def find_perm_for(service, file_id: str, email: str) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = service.permissions().list(
                fileId=file_id,
                supportsAllDrives=True,
                fields="permissions(id,type,role,emailAddress,pendingOwner,permissionDetails(inherited))",
            ).execute()
            for p in resp.get("permissions", []):
                if (p.get("type") == "user"
                        and p.get("emailAddress", "").lower() == email.lower()):
                    return p
            return None
        except HttpError as e:
            if is_retryable(e) and attempt < MAX_RETRIES:
                time.sleep(min(60, (2 ** attempt) + random.uniform(0, 2)))
                continue
            print(f"  [WARN] list permissions failed: {decode_error(e)}")
            return None
    return None


def delete_permission(service, file_id: str, perm_id: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            service.permissions().delete(
                fileId=file_id,
                permissionId=perm_id,
                supportsAllDrives=True,
            ).execute()
            return True
        except HttpError as e:
            if is_retryable(e) and attempt < MAX_RETRIES:
                time.sleep(min(60, (2 ** attempt) + random.uniform(0, 2)))
                continue
            print(f"  [ERROR] delete permission: {decode_error(e)}")
            return False
    return False


def create_writer(service, file_id: str, name: str) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return service.permissions().create(
                fileId=file_id,
                body={"type": "user", "role": "writer", "emailAddress": NEW_OWNER_EMAIL},
                sendNotificationEmail=False,
                supportsAllDrives=True,
                fields="id,type,role,emailAddress,pendingOwner,permissionDetails(inherited)",
            ).execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            err = decode_error(e)
            if "sharingRateLimitExceeded" in err:
                wait = 90 + random.uniform(0, 30)
                print(f"  [RATE LIMIT] Sleeping {wait:.0f}s...")
                time.sleep(wait)
                continue
            if is_retryable(e) and attempt < MAX_RETRIES:
                time.sleep(min(60, (2 ** attempt) + random.uniform(0, 2)))
                continue
            print(f"  [ERROR] create writer HTTP {status}: {err}")
            return None
    return None


def set_pending_owner(service, file_id: str, name: str, perm_id: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            updated = service.permissions().update(
                fileId=file_id,
                permissionId=perm_id,
                body={"role": "writer", "pendingOwner": True},
                supportsAllDrives=True,
                fields="id,role,pendingOwner",
            ).execute()
            if updated.get("pendingOwner") is True:
                return True
            print(f"  [WARN] pendingOwner returned False")
            return False
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            err = decode_error(e)
            if "pendingOwnerWriterRequired" in err:
                print(f"  [BLOCKED] Inherited writer present: {name}")
                log_event({"status": "blocked_inherited", "file_id": file_id, "file_name": name})
                return False
            if is_retryable(e) and attempt < MAX_RETRIES:
                time.sleep(min(60, (2 ** attempt) + random.uniform(0, 2)))
                continue
            print(f"  [ERROR] pendingOwner HTTP {status}: {err}")
            return False
    return False


def accept(new_svc, file_id: str, name: str, perm_id: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            new_svc.permissions().update(
                fileId=file_id,
                permissionId=perm_id,
                body={"role": "owner"},
                transferOwnership=True,
                supportsAllDrives=True,
                fields="id,role",
            ).execute()
            log_event({"status": "accepted", "file_id": file_id, "file_name": name})
            return True
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if is_retryable(e) and attempt < MAX_RETRIES:
                time.sleep(min(60, (2 ** attempt) + random.uniform(0, 2)))
                continue
            print(f"  [ERROR] accept HTTP {status}: {decode_error(e)}")
            return False
    return False


def transfer_item(old_svc, new_svc, item_id: str, item_name: str) -> bool:
    perm = find_perm_for(old_svc, item_id, NEW_OWNER_EMAIL)

    if perm and perm.get("role") == "owner":
        log_event({"status": "already_owner", "file_id": item_id, "file_name": item_name})
        return True

    if perm and perm.get("role") == "writer" and perm.get("pendingOwner") is True and is_direct(perm):
        return accept(new_svc, item_id, item_name, perm["id"])

    if perm and perm.get("role") == "writer" and is_direct(perm):
        perm_id = perm["id"]
    else:
        created = create_writer(old_svc, item_id, item_name)
        if not created:
            return False
        if not is_direct(created):
            print(f"  [BLOCKED] Created permission is inherited: {item_name}")
            log_event({"status": "blocked_inherited", "file_id": item_id, "file_name": item_name})
            return False
        perm_id = created["id"]
        time.sleep(SLEEP_AFTER_CREATE)

    if not set_pending_owner(old_svc, item_id, item_name, perm_id):
        return False

    return accept(new_svc, item_id, item_name, perm_id)


def transfer_file_via_move(old_svc, new_svc, file_id: str, file_name: str, old_root: str, force_move: bool = False) -> bool:
    """
    Move file to the old owner's My Drive root to escape inherited writer
    from a new-owner-owned parent, transfer ownership, then put it back
    under the original parent (the new owner can do it since they now own
    the file).

    force_move=True skips the is_parent_ok check and always moves the file
    (use when the file is known to be inside a new-owner-owned folder tree).
    """
    try:
        meta = old_svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    except HttpError as e:
        print(f"  [WARN] get parents: {decode_error(e)}")
        return False

    original_parents = meta.get("parents", []) or []

    if force_move:
        blocking = [p for p in original_parents if p != old_root]
    else:
        blocking = [p for p in original_parents if not is_parent_ok(old_svc, p)]

    moved = False
    if blocking:
        kwargs = {
            "fileId": file_id,
            "removeParents": ",".join(blocking),
            "supportsAllDrives": True,
            "fields": "id,parents",
        }
        if old_root not in original_parents:
            kwargs["addParents"] = old_root
        try:
            old_svc.files().update(**kwargs).execute()
            moved = True
            print(f"  [MOVED] to old owner root")
            log_event({
                "status": "moved_to_root_for_transfer",
                "file_id": file_id,
                "file_name": file_name,
                "blocking_parents": blocking,
            })
            time.sleep(0.2)
        except HttpError as e:
            print(f"  [ERROR] move: {decode_error(e)}")
            return False

    ok = transfer_item(old_svc, new_svc, file_id, file_name)

    if moved and ok:
        # Step A — old owner (still a writer after transfer, and owner of
        # their My Drive root) removes the file from their My Drive root.
        if old_root not in original_parents:
            try:
                old_svc.files().update(
                    fileId=file_id,
                    removeParents=old_root,
                    supportsAllDrives=True,
                    fields="id,parents",
                ).execute()
            except HttpError as e:
                print(f"  [WARN] remove old-owner root failed: {decode_error(e)}")

        # Step B — new owner (now file owner, and owner of original parents)
        # adds the original parents back. Goes from 0 → 1 parent, which Drive
        # allows.
        try:
            new_svc.files().update(
                fileId=file_id,
                addParents=",".join(blocking),
                supportsAllDrives=True,
                fields="id,parents",
            ).execute()
            print(f"  [RESTORED] to original location")
            log_event({
                "status": "restored_after_transfer",
                "file_id": file_id,
                "file_name": file_name,
                "original_parents": original_parents,
            })
        except HttpError as e:
            print(f"  [WARN] restore failed (file still transferred): {decode_error(e)}")
            log_event({
                "status": "restore_failed",
                "file_id": file_id,
                "file_name": file_name,
                "original_parents": original_parents,
                "error": decode_error(e),
            })

    return ok


def transfer_folder_via_move(old_svc, new_svc, folder_id: str, folder_name: str, old_root: str) -> bool:
    """
    For folders inside new-owner-owned parents: temporarily move the folder
    to the old owner's My Drive root (two-step: old_svc adds it as a parent,
    new_svc removes the new-owner-owned parent), transfer ownership, then
    restore (new_svc adds original parents back, old_svc removes the
    temporary root parent).
    """
    # Try to get parents from old_svc first, fall back to new_svc
    try:
        original_parents = (old_svc.files().get(
            fileId=folder_id, fields="parents", supportsAllDrives=True,
        ).execute().get("parents") or [])
    except HttpError:
        try:
            original_parents = (new_svc.files().get(
                fileId=folder_id, fields="parents", supportsAllDrives=True,
            ).execute().get("parents") or [])
        except HttpError as e:
            print(f"  [WARN] get parents failed: {decode_error(e)}")
            return False

    blocking = [p for p in original_parents if p != old_root]

    if not blocking:
        return transfer_item(old_svc, new_svc, folder_id, folder_name)

    # Step A — old owner (folder owner) adds their own My Drive root as a parent
    if old_root not in original_parents:
        try:
            old_svc.files().update(
                fileId=folder_id,
                addParents=old_root,
                supportsAllDrives=True,
                fields="id,parents",
            ).execute()
        except HttpError as e:
            print(f"  [ERROR] add old-owner root: {decode_error(e)}")
            return False

    # Step B — new owner (owner of the blocking parents) removes them
    for bad_parent in blocking:
        try:
            new_svc.files().update(
                fileId=folder_id,
                removeParents=bad_parent,
                supportsAllDrives=True,
                fields="id,parents",
            ).execute()
        except HttpError as e:
            print(f"  [WARN] remove bad parent {bad_parent}: {decode_error(e)}")

    time.sleep(0.2)

    ok = transfer_item(old_svc, new_svc, folder_id, folder_name)

    if ok:
        # Step C — new owner (now owner) adds original parents back
        try:
            new_svc.files().update(
                fileId=folder_id,
                addParents=",".join(blocking),
                supportsAllDrives=True,
                fields="id,parents",
            ).execute()
            print(f"  [RESTORED] to original location")
        except HttpError as e:
            print(f"  [WARN] restore parents: {decode_error(e)}")

        # Step D — old owner removes the temporary root parent
        if old_root not in original_parents:
            try:
                old_svc.files().update(
                    fileId=folder_id,
                    removeParents=old_root,
                    supportsAllDrives=True,
                    fields="id,parents",
                ).execute()
            except HttpError as e:
                print(f"  [WARN] remove temporary root parent: {decode_error(e)}")

    return ok


# ── Phases ─────────────────────────────────────────────────────────────────────

def phase_fix_parents(old_svc, new_svc, folders: list):
    print(f"\n=== PHASE 0: Fix parents — relocate folders out of the new owner's drive ===\n")
    my_root = get_my_drive_root(old_svc)
    tree_ids = {f["id"] for f in folders}
    print(f"  Old owner's My Drive root: {my_root}\n")
    fixed = ok = failed = 0
    for f in folders:
        # Query parents from BOTH accounts — the new owner can see parents
        # in their own drive that the old owner cannot see (and vice versa).
        # 404s are expected when one account doesn't have access; not an error.
        try:
            old_parents = (old_svc.files().get(
                fileId=f["id"], fields="parents", supportsAllDrives=True,
            ).execute().get("parents") or [])
        except HttpError as e:
            old_parents = []
            if getattr(e.resp, "status", None) != 404:
                print(f"  [WARN] old_svc parents for {f['name']}: HTTP {e.resp.status}")

        try:
            new_parents = (new_svc.files().get(
                fileId=f["id"], fields="parents", supportsAllDrives=True,
            ).execute().get("parents") or [])
        except HttpError as e:
            new_parents = []
            if getattr(e.resp, "status", None) != 404:
                print(f"  [WARN] new_svc parents for {f['name']}: HTTP {e.resp.status}")

        all_parents = list(dict.fromkeys(old_parents + new_parents))
        # A parent inside the tree we're transferring is fine — only flag
        # parents that are outside the tree AND inaccessible to the old owner.
        bad = [p for p in all_parents
               if p not in tree_ids and not is_parent_ok(old_svc, p)]

        if not bad:
            ok += 1
            continue

        print(f"  [FIX] {f['name']}: removing {bad}, adding {my_root if my_root not in all_parents else '(already a parent)'}")
        log_event({
            "status": "parent_fixed",
            "file_id": f["id"],
            "file_name": f["name"],
            "removed_parents": bad,
            "added_parent": my_root if my_root not in all_parents else None,
        })

        if DRY_RUN:
            fixed += 1
            continue

        # Step A — old owner (folder owner) adds their My Drive as a parent.
        if my_root not in all_parents:
            try:
                old_svc.files().update(
                    fileId=f["id"],
                    addParents=my_root,
                    supportsAllDrives=True,
                    fields="id,parents",
                ).execute()
            except HttpError as e:
                print(f"  [ERROR] add parent: {decode_error(e)}")
                failed += 1
                continue

        # Step B — new owner (owner of their own My Drive) removes their
        # drive root as a parent. The old owner cannot do this because they
        # have no access to the new owner's My Drive.
        step_b_ok = True
        for bad_parent in bad:
            try:
                new_svc.files().update(
                    fileId=f["id"],
                    removeParents=bad_parent,
                    supportsAllDrives=True,
                    fields="id,parents",
                ).execute()
            except HttpError as e:
                print(f"  [ERROR] new_svc remove parent {bad_parent}: {decode_error(e)}")
                step_b_ok = False

        if step_b_ok:
            fixed += 1
        else:
            failed += 1
    print(f"\n[PHASE 0 DONE] Fixed: {fixed} | Already OK: {ok} | Failed: {failed}\n")


def phase_unblock(old_svc, folders: list):
    print(f"\n=== PHASE 1: Unblock — remove direct new-owner writer from {len(folders)} folder(s) ===\n")
    cleared = skipped = failed = 0
    for f in folders:
        perm = find_perm_for(old_svc, f["id"], NEW_OWNER_EMAIL)
        if not perm:
            skipped += 1
            continue
        if not is_direct(perm):
            skipped += 1
            continue
        if perm.get("role") not in ("writer", "reader"):
            skipped += 1
            continue
        print(f"  [UNBLOCK] {f['name']} (perm_id={perm['id']})")
        if DRY_RUN:
            cleared += 1
            continue
        if delete_permission(old_svc, f["id"], perm["id"]):
            cleared += 1
            log_event({"status": "permission_deleted", "file_id": f["id"], "file_name": f["name"]})
        else:
            failed += 1
    print(f"\n[PHASE 1 DONE] Cleared: {cleared} | Skipped: {skipped} | Failed: {failed}\n")


def phase_files(old_svc, new_svc, files: list, done_ids: set):
    print(f"\n=== PHASE 2: Transfer {len(files)} file(s) ===\n")
    old_root = get_my_drive_root(old_svc)
    done = failed = skipped = 0
    for i, f in enumerate(files, 1):
        if f["id"] in done_ids:
            skipped += 1
            continue
        print(f"[F {i}/{len(files)}] {f['name']}")
        if DRY_RUN:
            done += 1
            continue
        if transfer_file_via_move(old_svc, new_svc, f["id"], f["name"], old_root):
            done += 1
            done_ids.add(f["id"])
            print(f"  [OK]")
        else:
            failed += 1
        time.sleep(SLEEP_BETWEEN_ITEMS)
    print(f"\n[PHASE 2 DONE] Transferred: {done} | Skipped: {skipped} | Failed: {failed}\n")


def phase_folders(old_svc, new_svc, folders: list, done_ids: set):
    deepest_first = sorted(folders, key=lambda f: -f["depth"])
    print(f"\n=== PHASE 3: Transfer {len(deepest_first)} folder(s) deepest-first ===\n")
    done = failed = skipped = 0
    for i, f in enumerate(deepest_first, 1):
        if f["id"] in done_ids:
            skipped += 1
            continue
        if NEW_OWNER_EMAIL in f.get("owners", []) and OLD_OWNER_EMAIL not in f.get("owners", []):
            skipped += 1
            log_event({"status": "already_owner", "file_id": f["id"], "file_name": f["name"]})
            continue
        print(f"[D {i}/{len(deepest_first)}] depth={f['depth']}  {f['name']}")
        if DRY_RUN:
            done += 1
            continue
        if transfer_item(old_svc, new_svc, f["id"], f["name"]):
            done += 1
            done_ids.add(f["id"])
            print(f"  [OK]")
        else:
            failed += 1
        time.sleep(SLEEP_BETWEEN_ITEMS)
    print(f"\n[PHASE 3 DONE] Transferred: {done} | Skipped: {skipped} | Failed: {failed}\n")


def phase_verify(new_svc, root_id: str):
    """Walk the tree from the new owner's account and report any item not
    owned by NEW_OWNER_EMAIL."""
    print(f"\n=== PHASE 4: Verify — every folder and file should be owned by {NEW_OWNER_EMAIL} ===\n")

    total = ok_count = 0
    not_owned = []

    queue = [root_id]
    visited = set()

    while queue:
        fid = queue.pop(0)
        if fid in visited:
            continue
        visited.add(fid)

        try:
            meta = new_svc.files().get(
                fileId=fid,
                fields="id,name,mimeType,owners",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            print(f"  [WARN] Could not fetch {fid}: {decode_error(e)}")
            continue

        total += 1
        owners = [o.get("emailAddress") for o in meta.get("owners", [])]
        if NEW_OWNER_EMAIL in owners and len(owners) == 1:
            ok_count += 1
        else:
            not_owned.append({
                "id": meta["id"],
                "name": meta.get("name", "Untitled"),
                "mime": meta.get("mimeType", ""),
                "owners": owners,
            })

        if meta.get("mimeType") == "application/vnd.google-apps.folder":
            page_token = None
            while True:
                resp = new_svc.files().list(
                    q=f"'{fid}' in parents and trashed = false",
                    fields="nextPageToken, files(id)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                for child in resp.get("files", []):
                    queue.append(child["id"])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

    print(f"[VERIFY] Total items: {total}")
    print(f"[VERIFY] Owned by {NEW_OWNER_EMAIL}: {ok_count}")
    print(f"[VERIFY] Not yet owned: {len(not_owned)}")

    if not_owned:
        print(f"\n[VERIFY] Items still NOT owned by {NEW_OWNER_EMAIL}:")
        for item in not_owned:
            kind = "FOLDER" if item["mime"] == "application/vnd.google-apps.folder" else "FILE"
            print(f"  [{kind}] {item['name']} ({item['id']}) owners={item['owners']}")
        log_event({
            "status": "verify_incomplete",
            "not_owned_count": len(not_owned),
            "not_owned": not_owned,
        })
    else:
        print(f"\n[VERIFY] All items in the tree are owned by {NEW_OWNER_EMAIL}.")
        log_event({"status": "verify_complete", "total": total})


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== transfer_tree.py ===")
    print(f"  Target root folder: {TARGET_FOLDER_ID}")
    print(f"  Old owner: {OLD_OWNER_EMAIL}")
    print(f"  New owner: {NEW_OWNER_EMAIL}")
    print(f"  DRY_RUN: {DRY_RUN}\n")

    print("[AUTH] Authenticating both accounts...")
    old_svc = build_service(OLD_OWNER_TOKEN)
    new_svc = build_service(NEW_OWNER_TOKEN)
    print("[AUTH] Done.\n")

    print("[SCAN] Walking folder tree...")
    folders, files = walk_tree(old_svc, TARGET_FOLDER_ID)
    print(f"[SCAN] Found {len(folders)} folder(s) and {len(files)} old-owner-owned file(s).\n")

    done_ids = load_done_ids()
    if done_ids:
        print(f"[INFO] {len(done_ids)} items already done (from log).\n")

    phase_fix_parents(old_svc, new_svc, folders)
    phase_unblock(old_svc, folders)
    phase_files(old_svc, new_svc, files, done_ids)
    phase_folders(old_svc, new_svc, folders, done_ids)
    phase_verify(new_svc, TARGET_FOLDER_ID)

    if ORGANIZE_FOLDER_ID and not DRY_RUN:
        old_root = get_my_drive_root(old_svc)
        print(f"\n[ORGANIZE] Moving '{folders[0]['name'] if folders else TARGET_FOLDER_ID}' into organize folder...")
        try:
            meta = new_svc.files().get(
                fileId=TARGET_FOLDER_ID, fields="parents,owners", supportsAllDrives=True,
            ).execute()
            owners = [o.get("emailAddress") for o in meta.get("owners", [])]
            if NEW_OWNER_EMAIL not in owners:
                print(f"  [SKIP organize] root folder not yet owned by {NEW_OWNER_EMAIL}")
            else:
                current_parents = meta.get("parents", []) or []
                if ORGANIZE_FOLDER_ID in current_parents:
                    print(f"  [SKIP organize] already inside organize folder")
                else:
                    if old_root in current_parents:
                        old_svc.files().update(
                            fileId=TARGET_FOLDER_ID,
                            removeParents=old_root,
                            supportsAllDrives=True,
                            fields="id,parents",
                        ).execute()
                    new_svc.files().update(
                        fileId=TARGET_FOLDER_ID,
                        addParents=ORGANIZE_FOLDER_ID,
                        supportsAllDrives=True,
                        fields="id,parents",
                    ).execute()
                    print(f"  [ORGANIZED] moved into organize folder")
                    log_event({"status": "organized", "file_id": TARGET_FOLDER_ID, "organize_folder": ORGANIZE_FOLDER_ID})
        except HttpError as e:
            print(f"  [WARN] organize failed: {decode_error(e)}")

    print("=== ALL PHASES DONE ===")


if __name__ == "__main__":
    main()
