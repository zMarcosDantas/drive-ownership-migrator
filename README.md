# Drive Bulk Transfer Ownership

Transfers ownership of every file and folder owned by one Google account
to another, including items buried inside other people's folders, shared
drives, or the new owner's own drive. Preserves the original folder
structure — files end up in the same place they started, just with a
different owner.

This isn't possible through the Drive UI in bulk: each item has to be
transferred individually, and items with **inherited writer permissions**
from a parent folder can't be transferred at all without first removing
the blocker. This tool handles all of that automatically.

Battle-tested on a 1 TB drive containing shortcuts, mixed-owner files
inside the same folders, and deeply nested folder trees.

> [!WARNING]
> Always test on a small folder (or a throwaway sub-drive) before running
> against your whole account. Ownership transfers are reversible only by
> running the migration again in the opposite direction — there is no
> undo button. Set `DRY_RUN = True` in `transfer_tree.py` for a no-op
> simulation, and start with a folder of ~10 items before going full
> scale.

## What problem does this solve?

Google Drive's ownership-transfer API has two gotchas:

1. **The `pendingOwner=True` flow** requires a **direct** writer permission
   on the item. If the would-be owner only has inherited writer access
   (from a parent folder), `permissions.create` is a silent no-op and
   `pendingOwner=True` fails with `pendingOwnerWriterRequired`.

2. **Single-call ownership transfer** (`role=owner` + `transferOwnership=True`)
   requires consent, which usually only works within the same Google
   Workspace organisation. For personal accounts, you must use the
   two-step pendingOwner flow.

When the new owner already has access to a parent folder, both accounts
end up with overlapping permissions and items become "stuck" — owned by
the old account, but viewable from the new one. This script breaks the
inheritance chain by temporarily moving items into the old owner's My
Drive root, transferring ownership, then moving them back.

## Setup

1. Install dependencies:
   ```
   pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
   ```

2. Create an OAuth client in [Google Cloud Console](https://console.cloud.google.com/):
   - Enable the **Google Drive API** for your project
   - Create an **OAuth 2.0 Client ID** of type **Desktop app**
   - Download the JSON and save it as `credentials.json` in this folder

3. Edit `transfer_tree.py` and set both email addresses:
   ```python
   OLD_OWNER_EMAIL = "old.owner@example.com"
   NEW_OWNER_EMAIL = "new.owner@example.com"
   ```

4. The first run of either script will open a browser twice — once to
   authenticate the old owner (`token_old_owner.json` is created) and
   once for the new owner (`token_new_owner.json`).

## Usage

### Step 1 — List everything owned by the old account

```
python list_owned_files.py
```

Queries Drive for every item where the old owner appears in `owners`,
regardless of location. Writes the result to `list_owned_files.jsonl`
and prints a summary (total items, folders, files, total size).

### Step 2 — Transfer everything

```
python transfer_from_list.py
```

Reads `list_owned_files.jsonl` and transfers ownership of every item.
The process:

1. Transfers files first, using `force_move` so each one escapes any
   new-owner-owned parent before the transfer.
2. Removes any direct new-owner writer permission on the old owner's
   root (otherwise folders moved there temporarily would still inherit
   the blocker).
3. Transfers folders **deepest-first** — a folder is only transferred
   after every child folder and file inside it.

For each item the flow is:

1. Move the item to the old owner's My Drive root (a two-step
   parent edit — old owner adds the root as a parent, new owner removes
   the original parents).
2. Transfer ownership via the `pendingOwner=True` flow.
3. Restore the original location (new owner adds the original parents
   back, old owner removes the temporary root parent).

The script is **resumable** — every successful transfer is logged in
`transfer_tree_log.jsonl`, and a re-run skips items already done.

## Files

- **`transfer_tree.py`** — Core library: authentication, permission
  handling, the `transfer_file_via_move` / `transfer_folder_via_move`
  primitives, and the four-phase pipeline. Also runnable directly to
  process a single folder tree (set `TARGET_FOLDER_ID`).
- **`list_owned_files.py`** — Step 1. Lists everything the old account
  still owns.
- **`transfer_from_list.py`** — Step 2. Reads the list and transfers
  it all.

## Notes

- Shortcuts (`application/vnd.google-apps.shortcut`) cannot be transferred
  via the API; they are skipped automatically. Delete and recreate them
  manually from the new account if you need to.
- Sites (`application/vnd.google-apps.site`) are also skipped.
- Drive's daily sharing quota will throttle large transfers. The script
  detects `sharingRateLimitExceeded` and sleeps 90 seconds before
  retrying — long runs may end up sleeping a lot. Run overnight for
  large drives.
