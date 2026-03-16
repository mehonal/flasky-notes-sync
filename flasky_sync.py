#!/usr/bin/env python3
"""Flasky Obsidian Sync — bidirectional sync between an Obsidian vault and flasky-notes."""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_FILENAME = ".flasky-sync.json"
STATE_FILENAME = ".flasky-state.json"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
FILENAME_BAD_CHARS = re.compile(r'[/\\:*?"<>|]')

# Legacy keys — stripped from frontmatter during migration, never written
SYNC_META_KEYS = {"flasky_id", "flasky_hash", "conflict_source"}

# Extensions treated as attachments (not synced as notes)
ATTACHMENT_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".mp4", ".webm", ".ogg", ".mov", ".avi",
    ".mp3", ".wav", ".flac", ".m4a", ".aac",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".pptx",
    ".zip", ".tar", ".gz",
    ".csv", ".json", ".xml",
    ".ttf", ".otf", ".woff", ".woff2",
}


def find_config(cli_path=None):
    """Locate the config file. CLI path > vault root > home dir."""
    if cli_path:
        return Path(cli_path)
    cwd = Path.cwd()
    local = cwd / CONFIG_FILENAME
    if local.exists():
        return local
    home = Path.home() / CONFIG_FILENAME
    if home.exists():
        return home
    return local  # default write location


def load_config(cli_path=None, cli_overrides=None):
    path = find_config(cli_path)
    cfg = {}
    if path.exists():
        cfg = json.loads(path.read_text())
    if cli_overrides:
        for k, v in cli_overrides.items():
            if v is not None:
                cfg[k] = v
    for key in ("server_url", "api_token", "vault_path"):
        if key not in cfg or not cfg[key]:
            print(f"Error: missing config key '{key}'. Run 'python flasky_sync.py init' to set up.")
            sys.exit(1)
    cfg["server_url"] = cfg["server_url"].rstrip("/")
    cfg["vault_path"] = Path(cfg["vault_path"])
    return cfg


def load_state(vault_path):
    state_path = vault_path / STATE_FILENAME
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"last_sync_utc": None, "notes": {}, "attachments": {}}


def save_state(vault_path, state):
    state_path = vault_path / STATE_FILENAME
    state["last_sync_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(text):
    """Return (dict, content_body). If no frontmatter, dict is empty."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        raw = yaml.safe_load(m.group(1))
        if not isinstance(raw, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    body = text[m.end():]
    return raw, body


def build_frontmatter(meta):
    if not meta:
        return ""
    dumped = yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{dumped}---\n"


def write_note_file(filepath, meta, content):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(build_frontmatter(meta) + content)


# ---------------------------------------------------------------------------
# Hash + filename helpers
# ---------------------------------------------------------------------------

def compute_hash(content):
    """SHA-256 hex of content — must match server's content_hash()."""
    if content is None:
        content = ""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_hash(filepath):
    """SHA-256 hex of a binary file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_filename(title):
    name = FILENAME_BAD_CHARS.sub("-", title)
    return name[:200]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def api_get(cfg, endpoint):
    r = requests.get(cfg["server_url"] + endpoint, headers=api_headers(cfg["api_token"]))
    r.raise_for_status()
    return r.json()


def api_post(cfg, endpoint, data=None):
    r = requests.post(cfg["server_url"] + endpoint, headers=api_headers(cfg["api_token"]), json=data)
    r.raise_for_status()
    return r.json()


def api_put(cfg, endpoint, data=None):
    r = requests.put(cfg["server_url"] + endpoint, headers=api_headers(cfg["api_token"]), json=data)
    r.raise_for_status()
    return r.json()


def api_upload(cfg, endpoint, filepath):
    """Upload a file via multipart POST."""
    content_type = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
    with open(filepath, "rb") as f:
        r = requests.post(
            cfg["server_url"] + endpoint,
            headers={"Authorization": f"Bearer {cfg['api_token']}"},
            files={"file": (filepath.name, f, content_type)},
        )
    r.raise_for_status()
    return r.json()


def api_download(cfg, endpoint, dest_path):
    """Download a file to disk."""
    r = requests.get(
        cfg["server_url"] + endpoint,
        headers={"Authorization": f"Bearer {cfg['api_token']}"},
        stream=True,
    )
    r.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


# ---------------------------------------------------------------------------
# Vault scanning
# ---------------------------------------------------------------------------

def is_dot_path(rel_parts):
    """Check if any directory component starts with a dot."""
    return any(p.startswith(".") for p in rel_parts[:-1])


def scan_vault(vault_path):
    """Return dict {rel_path: entry} of all syncable .md files in vault."""
    local_files = {}

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        parts = rel.parts

        if is_dot_path(parts):
            continue
        if parts[-1].startswith("."):
            continue
        if ".conflict-" in parts[-1]:
            continue

        # Determine category from folder path (supports nested subfolders)
        if len(parts) == 1:
            category = "Main"
        else:
            category = "/".join(parts[:-1])

        text = md_file.read_text()
        meta, body = parse_frontmatter(text)
        title = md_file.stem

        # Strip any leftover sync meta keys (legacy frontmatter)
        user_props = {k: v for k, v in meta.items() if k not in SYNC_META_KEYS}

        # Build the content as the server sees it: user frontmatter + body
        server_content = build_frontmatter(user_props) + body if user_props else body
        h = compute_hash(server_content)

        rel_str = str(rel)
        local_files[rel_str] = {
            "path": md_file,
            "rel_path": rel_str,
            "title": title,
            "category": category,
            "content": server_content,
            "content_hash": h,
            "user_props": user_props,
        }

    return local_files


def scan_attachments(vault_path):
    """Return dict {rel_path_str: {path, rel_path, file_hash, filename}}."""
    attachments = {}
    for filepath in vault_path.rglob("*"):
        if not filepath.is_file():
            continue
        rel = filepath.relative_to(vault_path)
        parts = rel.parts

        if is_dot_path(parts):
            continue
        if parts[-1].startswith("."):
            continue

        ext = filepath.suffix.lower()
        if ext not in ATTACHMENT_EXTENSIONS:
            continue

        h = file_hash(filepath)
        attachments[str(rel)] = {
            "path": filepath,
            "rel_path": str(rel),
            "filename": filepath.name,
            "file_hash": h,
        }
    return attachments


# ---------------------------------------------------------------------------
# Migration from frontmatter-based sync
# ---------------------------------------------------------------------------

def migrate_from_frontmatter(vault_path, state, dry_run=False):
    """Migrate from frontmatter-based sync to state-file-based sync.

    Reads flasky_id/flasky_hash from existing frontmatter, builds state entries,
    then rewrites files without sync meta keys.
    """
    migrated = 0
    notes_state = state.setdefault("notes", {})

    # Convert old state format (keyed by flasky_id) to new (keyed by rel_path)
    old_format = any(k.isdigit() for k in notes_state)
    if old_format:
        old_notes = dict(notes_state)
        notes_state.clear()
        for fid, entry in old_notes.items():
            local_path = entry.get("local_path", "")
            if local_path:
                notes_state[local_path] = {
                    "flasky_id": fid,
                    "content_hash": entry.get("content_hash", ""),
                    "server_hash": entry.get("server_hash", ""),
                }
        state["notes"] = notes_state

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        parts = rel.parts
        if is_dot_path(parts):
            continue
        if parts[-1].startswith("."):
            continue

        text = md_file.read_text()
        meta, body = parse_frontmatter(text)

        sync_keys = {k for k in meta if k in SYNC_META_KEYS}
        if not sync_keys:
            continue

        flasky_id = str(meta["flasky_id"]) if meta.get("flasky_id") else ""
        flasky_hash = str(meta["flasky_hash"]) if meta.get("flasky_hash") else ""
        is_conflict = ".conflict-" in parts[-1]

        user_props = {k: v for k, v in meta.items() if k not in SYNC_META_KEYS}

        # Rewrite file without sync meta
        if not dry_run:
            write_note_file(md_file, user_props, body)

        rel_str = str(rel)

        if is_conflict and flasky_id:
            state.setdefault("conflict_files", {})[rel_str] = flasky_id
        elif flasky_id and rel_str not in notes_state:
            server_content = build_frontmatter(user_props) + body if user_props else body
            h = compute_hash(server_content)
            notes_state[rel_str] = {
                "flasky_id": flasky_id,
                "content_hash": flasky_hash or h,
                "server_hash": flasky_hash or h,
            }

        migrated += 1

    return migrated


# ---------------------------------------------------------------------------
# Sync actions — notes
# ---------------------------------------------------------------------------

def action_pull(cfg, state, note, local_path, dry_run=False):
    """Download note from server and write to local file."""
    if dry_run:
        return
    full = api_get(cfg, f"/api/sync/note/{note['id']}")
    content = full["content"] or ""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content)
    # Compute local hash the same way scan_vault does (parse + rebuild frontmatter)
    meta, body = parse_frontmatter(content)
    user_props = {k: v for k, v in meta.items() if k not in SYNC_META_KEYS}
    local_content = build_frontmatter(user_props) + body if user_props else body
    local_hash = compute_hash(local_content)
    rel_path = str(local_path.relative_to(cfg["vault_path"]))
    state["notes"][rel_path] = {
        "flasky_id": str(full["id"]),
        "content_hash": local_hash,
        "server_hash": full["content_hash"],
    }


def action_push_update(cfg, state, entry, note_id, dry_run=False):
    """Push local changes to server."""
    if dry_run:
        return
    resp = api_put(cfg, f"/api/sync/note/{note_id}", {
        "title": entry["title"],
        "content": entry["content"],
        "category": entry["category"],
    })
    state["notes"][entry["rel_path"]] = {
        "flasky_id": str(note_id),
        "content_hash": entry["content_hash"],
        "server_hash": resp["content_hash"],
    }


def action_push_create(cfg, state, entry, dry_run=False):
    """Create a new note on the server."""
    if dry_run:
        return
    resp = api_post(cfg, "/api/sync/note", {
        "title": entry["title"],
        "content": entry["content"],
        "category": entry["category"],
    })
    state["notes"][entry["rel_path"]] = {
        "flasky_id": str(resp["id"]),
        "content_hash": entry["content_hash"],
        "server_hash": resp["content_hash"],
    }


def action_conflict(cfg, state, entry, server_note, vault_path, dry_run=False):
    """Write conflict file and report to server."""
    if dry_run:
        return
    full = api_get(cfg, f"/api/sync/note/{server_note['id']}")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conflict_name = f"{entry['path'].stem}.conflict-{date_str}.md"
    conflict_path = entry["path"].parent / conflict_name
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    conflict_path.write_text(full["content"] or "")

    # Track conflict file in state for cleanup
    rel_conflict = str(conflict_path.relative_to(vault_path))
    state.setdefault("conflict_files", {})[rel_conflict] = str(full["id"])

    # Update state to acknowledge both versions — prevents re-conflict on next sync.
    # User resolves via .conflict- file or web UI; editing the local file will
    # trigger a normal push on the next sync.
    state["notes"][entry["rel_path"]] = {
        "flasky_id": str(full["id"]),
        "content_hash": entry["content_hash"],
        "server_hash": server_note["content_hash"],
    }

    api_post(cfg, "/api/sync/conflict", {
        "note_id": full["id"],
        "local_title": entry["title"],
        "local_content": entry["content"],
        "server_title": full["title"],
        "server_content": full["content"],
        "category": entry["category"],
    })


def cleanup_resolved_conflicts(cfg, vault_path, state, dry_run=False):
    """Check for resolved conflicts and remove .conflict- files."""
    try:
        unresolved = api_get(cfg, "/api/sync/conflicts")
    except Exception:
        return 0
    unresolved_note_ids = {str(c["note_id"]) for c in unresolved if c.get("note_id")}

    conflict_files = state.get("conflict_files", {})
    removed = 0
    to_remove = []

    for rel_path, note_id in conflict_files.items():
        if str(note_id) not in unresolved_note_ids:
            conflict_path = vault_path / rel_path
            if conflict_path.exists():
                if not dry_run:
                    conflict_path.unlink()
                removed += 1
            to_remove.append(rel_path)

    if not dry_run:
        for rel_path in to_remove:
            del conflict_files[rel_path]

    return removed


# ---------------------------------------------------------------------------
# Sync — attachments
# ---------------------------------------------------------------------------

def sync_attachments(cfg, state, vault_path, dry_run=False):
    """Sync attachment files between vault and server."""
    if "attachments" not in state:
        state["attachments"] = {}

    # Get server manifest
    try:
        server_atts = api_get(cfg, "/api/sync/attachments")
    except requests.exceptions.HTTPError:
        print("  (attachment endpoints not available, skipping)")
        return {"uploaded": 0, "downloaded": 0}

    server_map = {a["filename"]: a for a in server_atts}

    # Scan local attachments
    local_atts = scan_attachments(vault_path)
    local_by_name = {}
    for rel_path, entry in local_atts.items():
        local_by_name[entry["filename"]] = entry

    stats = {"uploaded": 0, "downloaded": 0}

    # Upload local attachments not on server (or changed)
    for filename, entry in local_by_name.items():
        server_att = server_map.get(filename)
        if server_att and server_att["file_hash"] == entry["file_hash"]:
            # Already synced, update state
            state["attachments"][filename] = {
                "server_id": server_att["id"],
                "file_hash": entry["file_hash"],
                "local_path": entry["rel_path"],
            }
            continue
        if server_att and server_att["file_hash"] != entry["file_hash"]:
            # Local differs from server — push local (local wins for attachments)
            print(f"  UPLOAD (changed): {entry['rel_path']}")
        else:
            print(f"  UPLOAD (new): {entry['rel_path']}")
        if not dry_run:
            resp = api_upload(cfg, "/api/sync/attachment", entry["path"])
            state["attachments"][filename] = {
                "server_id": resp["id"],
                "file_hash": resp["file_hash"],
                "local_path": entry["rel_path"],
            }
        stats["uploaded"] += 1

    # Download server attachments not local
    for filename, server_att in server_map.items():
        if filename in local_by_name:
            continue
        # Determine local path — put in vault root (Obsidian default attachment location)
        local_path = vault_path / filename
        # If state has a previous path, use that
        if filename in state["attachments"]:
            prev_path = state["attachments"][filename].get("local_path")
            if prev_path:
                local_path = vault_path / prev_path
        print(f"  DOWNLOAD (new): {local_path.relative_to(vault_path)}")
        if not dry_run:
            api_download(cfg, f"/api/sync/attachment/{server_att['id']}", local_path)
            state["attachments"][filename] = {
                "server_id": server_att["id"],
                "file_hash": server_att["file_hash"],
                "local_path": str(local_path.relative_to(vault_path)),
            }
        stats["downloaded"] += 1

    return stats


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(cfg, dry_run=False):
    vault_path = cfg["vault_path"]
    state = load_state(vault_path)

    if dry_run:
        print("  [DRY RUN — no changes will be made]\n")

    # 0. Migrate from frontmatter-based sync if needed
    migrated = migrate_from_frontmatter(vault_path, state, dry_run)
    if migrated:
        print(f"  Migrated {migrated} note(s) from frontmatter to state-file sync")
        if not dry_run:
            save_state(vault_path, state)

    # 1. Get server manifest
    manifest = api_get(cfg, "/api/sync/manifest")
    server_map = {str(n["id"]): n for n in manifest}

    # 2. Scan vault
    local_files = scan_vault(vault_path)

    # 3. Build lookup indexes from state
    notes_state = state.get("notes", {})
    # Reverse index: flasky_id -> rel_path
    id_to_path = {}
    for rel_path, s_entry in notes_state.items():
        fid = s_entry.get("flasky_id")
        if fid:
            id_to_path[str(fid)] = rel_path

    stats = {"pulled": 0, "pushed": 0, "conflicts": 0, "skipped": 0}
    processed_server_ids = set()
    processed_local_paths = set()

    # 4. Process local files that have a known flasky_id in state
    for rel_path, entry in local_files.items():
        s_entry = notes_state.get(rel_path)
        if not s_entry or not s_entry.get("flasky_id"):
            continue  # handled in step 5

        fid = str(s_entry["flasky_id"])
        processed_local_paths.add(rel_path)

        if fid in server_map:
            processed_server_ids.add(fid)
            server_note = server_map[fid]

            local_changed = entry["content_hash"] != s_entry.get("content_hash", "")
            server_changed = server_note["content_hash"] != s_entry.get("server_hash", "")

            if not local_changed and not server_changed:
                stats["skipped"] += 1
            elif local_changed and not server_changed:
                print(f"  PUSH: {rel_path}")
                action_push_update(cfg, state, entry, int(fid), dry_run)
                stats["pushed"] += 1
            elif server_changed and not local_changed:
                print(f"  PULL: {rel_path}")
                action_pull(cfg, state, server_note, entry["path"], dry_run)
                stats["pulled"] += 1
            else:
                print(f"  CONFLICT: {rel_path}")
                action_conflict(cfg, state, entry, server_note, vault_path, dry_run)
                stats["conflicts"] += 1
        else:
            # Local has flasky_id but server doesn't — orphaned, push as new
            print(f"  PUSH (orphaned): {rel_path}")
            action_push_create(cfg, state, entry, dry_run)
            stats["pushed"] += 1

    # 5. Process local files not yet in state
    for rel_path, entry in local_files.items():
        if rel_path in processed_local_paths:
            continue

        # Rename detection: check if any state entry's hash matches this file
        renamed_from = None
        for old_path, s_entry in notes_state.items():
            if old_path in local_files:
                continue  # old path still exists, not a rename
            if s_entry.get("content_hash") == entry["content_hash"]:
                renamed_from = old_path
                break

        if renamed_from:
            old_entry = notes_state.pop(renamed_from)
            fid = str(old_entry["flasky_id"])
            processed_local_paths.add(rel_path)

            if fid in server_map:
                processed_server_ids.add(fid)
                print(f"  PUSH (renamed): {renamed_from} -> {rel_path}")
                action_push_update(cfg, state, entry, int(fid), dry_run)
                stats["pushed"] += 1
            else:
                print(f"  PUSH (new, was {renamed_from}): {rel_path}")
                action_push_create(cfg, state, entry, dry_run)
                stats["pushed"] += 1
            continue

        # Try title+category match against unmatched server notes
        matched = False
        for sid, server_note in server_map.items():
            if sid in processed_server_ids:
                continue
            if server_note["title"] == entry["title"] and server_note["category"] == entry["category"]:
                processed_server_ids.add(sid)
                processed_local_paths.add(rel_path)

                local_changed = entry["content_hash"] != server_note["content_hash"]
                if local_changed:
                    print(f"  PUSH (linked): {rel_path}")
                    action_push_update(cfg, state, entry, int(sid), dry_run)
                    stats["pushed"] += 1
                else:
                    if not dry_run:
                        state["notes"][rel_path] = {
                            "flasky_id": sid,
                            "content_hash": entry["content_hash"],
                            "server_hash": server_note["content_hash"],
                        }
                    stats["skipped"] += 1
                matched = True
                break

        if not matched:
            processed_local_paths.add(rel_path)
            print(f"  PUSH (new): {rel_path}")
            action_push_create(cfg, state, entry, dry_run)
            stats["pushed"] += 1

    # 6. Process server notes not matched to any local file
    for sid, server_note in server_map.items():
        if sid in processed_server_ids:
            continue

        # Check if this was a local deletion (was in state, file now gone)
        old_path = id_to_path.get(sid)
        if old_path and old_path not in local_files:
            local_path = vault_path / old_path
            print(f"  RE-DOWNLOAD (deleted locally): {old_path}")
            action_pull(cfg, state, server_note, local_path, dry_run)
            stats["pulled"] += 1
        else:
            cat = server_note.get("category", "Main")
            fname = sanitize_filename(server_note["title"]) + ".md"
            rel_str = str(Path(cat) / fname)
            # Skip if this path is already tracked (duplicate title on server)
            if rel_str in local_files or rel_str in notes_state:
                print(f"  SKIP (duplicate on server): {cat}/{fname} (id={sid})")
                stats["skipped"] += 1
                continue
            local_path = vault_path / cat / fname
            print(f"  PULL (new): {cat}/{fname}")
            action_pull(cfg, state, server_note, local_path, dry_run)
            stats["pulled"] += 1

    # 7. Sync attachments
    att_stats = sync_attachments(cfg, state, vault_path, dry_run)

    # 8. Cleanup resolved conflicts
    removed = cleanup_resolved_conflicts(cfg, vault_path, state, dry_run)
    if removed:
        print(f"  {'Would clean' if dry_run else 'Cleaned up'} {removed} resolved conflict file(s)")

    # 9. Save state
    if not dry_run:
        save_state(vault_path, state)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Sync complete: "
          f"Pulled: {stats['pulled']}, Pushed: {stats['pushed']}, "
          f"Conflicts: {stats['conflicts']}, Skipped: {stats['skipped']}, "
          f"Attachments up: {att_stats['uploaded']}, down: {att_stats['downloaded']}")


# ---------------------------------------------------------------------------
# Init command
# ---------------------------------------------------------------------------

def init_config():
    print("Flasky Obsidian Sync — setup")
    print("-" * 30)
    server_url = input("Server URL (e.g. https://notes.example.com): ").strip().rstrip("/")
    api_token = input("API token: ").strip()
    vault_path = input(f"Vault path [{os.getcwd()}]: ").strip()
    if not vault_path:
        vault_path = os.getcwd()
    vault_path = os.path.expanduser(vault_path)

    cfg = {
        "server_url": server_url,
        "api_token": api_token,
        "vault_path": vault_path,
    }
    out_path = Path(vault_path) / CONFIG_FILENAME
    out_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"Config written to {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Flasky Obsidian Sync — bidirectional sync between an Obsidian vault and flasky-notes")
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "init"],
                        help="Command to run (default: sync)")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument("--vault", help="Override vault path")
    parser.add_argument("--server", help="Override server URL")
    parser.add_argument("--token", help="Override API token")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    args = parser.parse_args()

    if args.command == "init":
        init_config()
        return

    overrides = {
        "vault_path": args.vault,
        "server_url": args.server,
        "api_token": args.token,
    }
    cfg = load_config(args.config, overrides)
    print(f"Flasky Obsidian Sync")
    print(f"  Vault:  {cfg['vault_path']}")
    print(f"  Server: {cfg['server_url']}")
    sync(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
