# Flasky Obsidian Sync

Bidirectional sync between an [Obsidian](https://obsidian.md) vault and a [flasky-notes](https://github.com/meho/flasky-notes) server.

**Important:** Sync only works with Flasky Notes v1, which is the legacy implementation before E2EE. If you latest version of Flasky Notes, you should not use this tool until further notice.

## How it works

- Folder names become note categories (`Work/plan.md` -> category "Work")
- Root-level `.md` files default to category "Main"
- Attachments (images, PDFs, audio, video, etc.) are synced alongside notes
- `[[note-title]]` wiki-links render as clickable links in the flasky-notes web UI
- `![[image.png]]` embeds render as inline images/media in the web UI
- Changes are detected via SHA-256 content hashes
- Conflicts (both sides changed) create a `.conflict-YYYY-MM-DD.md` file and are reported to the server for resolution
- Deleted local files are re-downloaded — notes are never auto-deleted from the server

## Install

```bash
pip install -r requirements.txt
```

Python 3.10+ required. The only external dependency is `requests`.

## Setup

1. Generate an API token in your flasky-notes **Settings** page.

2. Run init from inside your Obsidian vault (or anywhere):

```bash
python flasky_sync.py init
```

This prompts for your server URL, API token, and vault path, then writes `.flasky-sync.json` in the vault root.

You can also write the config manually:

```json
{
  "server_url": "https://notes.example.com",
  "api_token": "your-token-here",
  "vault_path": "/home/user/MyVault"
}
```

3. Enable **Obsidian Sync** in your flasky-notes Settings page to use the conflict resolution UI.

## Usage

```bash
# Run sync (default command)
python flasky_sync.py

# Dry run — see what would happen without making changes
python flasky_sync.py --dry-run

# With explicit options
python flasky_sync.py sync --vault /path/to/vault --server https://notes.example.com --token your-token

# Use a specific config file
python flasky_sync.py sync --config /path/to/.flasky-sync.json
```

## Vault structure

```
MyVault/
  .flasky-sync.json       # config (created by init)
  .flasky-state.json      # sync state (managed automatically)
  .obsidian/              # ignored
  Main/
    grocery-list.md       # category "Main"
    screenshot.png        # attachment — synced to server
  Work/
    project-plan.md       # category "Work"
  random-note.md          # category "Main" (root-level default)
```

## Sync state

Sync metadata is stored in `.flasky-state.json` in the vault root — **not** in note frontmatter. Your `.md` files stay clean; only your own frontmatter (tags, status, etc.) is preserved.

The state file tracks the mapping between local file paths and server note IDs, along with content hashes for change detection. It is managed automatically and should not be edited by hand.

**Rename detection:** If you rename a file between syncs, the script detects the rename via content-hash matching and updates the server note accordingly. If you rename **and** edit a file in the same sync cycle, the old path looks deleted and the new path looks like a new note — you'll end up with a duplicate on the server (recoverable by deleting the orphan).

**Migration from frontmatter-based sync:** If your notes contain `flasky_id`/`flasky_hash` keys from an older version of this script, they are automatically migrated to the state file and stripped from frontmatter on the first sync.

Category is derived from the folder name, not stored in frontmatter or state.

## Wiki-links

Obsidian-style wiki-links work in the flasky-notes web UI (themes with markdown rendering: Sage, Cozy, Segment, Tahta):

- `[[My Note]]` — renders as a clickable link to the note
- `[[My Note|custom text]]` — renders with custom display text
- `![[photo.png]]` — renders as an inline image
- `![[recording.mp3]]` — renders as an audio player
- `![[video.mp4]]` — renders as a video player
- `![[document.pdf]]` — renders as a download link

Wiki-links are resolved by note title (case-insensitive). Unresolved links are styled differently so you can spot broken references.

## Attachments

Non-markdown files (images, PDFs, audio, video, etc.) are automatically synced:

- Local attachments not on the server are uploaded
- Server attachments not local are downloaded to the vault root
- Duplicate files (same hash + filename) are deduplicated

Supported extensions include: `.png`, `.jpg`, `.gif`, `.svg`, `.webp`, `.pdf`, `.mp3`, `.mp4`, `.wav`, and more.

## Conflict resolution

When both the local file and server note have changed since the last sync:

1. The server version is saved as `note-title.conflict-2026-03-14.md`
2. The conflict is reported to the server
3. Your local file is left unchanged

Resolve conflicts either by:
- Editing the files manually and deleting the `.conflict-` file
- Using the **Sync Conflicts** table in the flasky-notes Settings page (Keep Local / Keep Server)

Resolved conflicts are cleaned up automatically on the next sync.

## Limitations

- **Nested subfolders are flattened.** Only the top-level folder is used as the category. A file at `Daily/2025-01/2025-01-02.md` syncs with category "Daily", not "Daily/2025-01". This works fine as long as the local file stays in place — the state file tracks the full path. However, if the file is deleted locally and re-downloaded from the server, it will be placed at `Daily/2025-01-02.md` (losing the subfolder structure).

## Dry run

Use `--dry-run` to preview what sync would do without making any changes:

```bash
python flasky_sync.py --dry-run
```

This connects to the server to compare state but does not modify any files, upload/download anything, or update the state file.

## Config file locations

The script searches for `.flasky-sync.json` in this order:

1. Path passed via `--config`
2. Current working directory
3. Home directory (`~/.flasky-sync.json`)

## License

Same license as [flasky-notes](https://github.com/meho/flasky-notes).
