# CursorMover

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://github.com/Artemonim/CursorMover/actions/workflows/tests.yml/badge.svg)](https://github.com/Artemonim/CursorMover/actions/workflows/tests.yml)

A utility to move or copy a project folder (Cursor workspace) **together with the Agent history**, so that after changing the path the history doesn't "disappear" from the UI.

Based on an observation from a Cursor Community thread: [Lost access to 5-7 Agent conversations after workspace folder restructure](https://forum.cursor.com/t/lost-access-to-5-7-agent-conversations-after-workspace-folder-restructure/147837).

## Features

- ✅ **Copy/Move workspaces** with full chat history preservation
- ✅ **Merge chat histories** from duplicate workspace storage entries
- ✅ **Export/Import** for cross-machine synchronization of workspace state (NEW!)
- ✅ **Sync-chats** for full chat history (sessions + messages) across machines
- ✅ **Database lock checking** for safe operations
- ✅ **Cross-platform support** (Windows, macOS, Linux)
- ✅ **Interactive TUI** for easy usage
- ✅ **CLI mode** for scripting and automation
- ✅ **Workspace diagnostics** (doctor command)
- ✅ **Automatic backup** before risky operations
- ✅ **SQLite integrity checks** after modifications

## Table of Contents

- [Important (Data Safety)](#important-data-safety)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [What exactly happens (Mode C)](#what-exactly-happens-mode-c)
- [Sync-chats (full chat history across machines)](#sync-chats-command---full-chat-history-across-machines)
- [Commands](#commands)
  - [Doctor](#doctor-command)
  - [Copy/Move](#copymove-workspace--transfer-chats)
  - [Merge](#merge-command)
- [CLI overview](#cli-overview)
- [Limitations](#limitations)
- [Tests](#tests)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Installation

See [INSTALL.md](INSTALL.md) for detailed installation instructions.

Quick install from source:

```bash
git clone https://github.com/Artemonim/CursorMover.git
cd CursorMover
```

## Important (Data Safety)

- **Close Cursor** (or at least close the workspace being operated on) before touching `workspaceStorage`. Otherwise, `state.vscdb` can be locked or inconsistent.
- By default the tool **checks for a lock** on `state.vscdb` (and WAL/SHM files, if present) and **aborts** if the files are busy. You can bypass the lock check with `--unsafe-db` / `-UnsafeDB` (not recommended).
- `copy` / `move` **do not modify** `state.vscdb` contents as SQLite by default: they copy/move folders and clone the entire `workspaceStorage/<id>` directory, updating only `workspace.json` (metadata).
- If the destination `workspaceStorage/<id>` already exists (for example, you opened the new path in Cursor and created new chats there), `copy` / `move` can **merge** source chat state into the existing destination (interactive prompt or `--merge-workspace-storage`). This **does modify** the destination `state.vscdb` (SQLite), runs `PRAGMA integrity_check`, and writes a backup `state.vscdb.premerge-<timestamp>`.
- `merge` **does modify** `state.vscdb` as SQLite (insert/replace only), runs `PRAGMA integrity_check`, and writes a backup `state.vscdb.premerge-<timestamp>` next to the destination DB.
- Still recommended: make a manual backup of the entire `workspaceStorage/<id>` folder before experimenting.

## What exactly happens (Mode C)

Cursor stores workspace data in:

- `Cursor/User/workspaceStorage/<WORKSPACE_ID>/state.vscdb`
- `Cursor/User/workspaceStorage/<WORKSPACE_ID>/workspace.json`

Where `<WORKSPACE_ID>` is calculated from the path + file system metadata. On Windows, this is effectively `md5(fsPath + birthtimeMs)` (see `cursor_mover/workspace_id.py`).

Mode **C**:

- copies/moves the project folder;
- calculates the new `<WORKSPACE_ID>` for the new path;
- copies `workspaceStorage/<old_id>` → `workspaceStorage/<new_id>`;
- updates `workspace.json` inside `workspaceStorage/<new_id>` to the new folder URI.

This is the **migration** path: use `copy` / `move` when you change the workspace folder location on disk.

## Merge (command)

In rare cases you may end up with **multiple** `workspaceStorage/<id>` entries that point to the **same folder URI** (for example after restores, metadata changes, or manual copying of Cursor user data). The `merge` command merges chat-related keys from those entries into the current one. Use `--delete-sources` to delete merged source entries after a successful merge.

Note: `merge` is **not** a migration. It does not move/copy your workspace folder and does not change its path. It only consolidates duplicate `workspaceStorage` state for the same folder URI.

## Sync-chats (command) - full chat history across machines

`export`/`import` (below) only capture a workspace's lightweight `ItemTable`/`cursorDiskKV` data. In current
Cursor versions, the **actual conversation content** (every message, both yours and the AI's) lives in a
different file entirely: `Cursor/User/globalStorage/state.vscdb` (tables `composerHeaders` and `cursorDiskKV`,
keyed by workspace id). `export`/`import` never touch this file, so they cannot carry over full conversations -
at most they carry your own past prompt text (`aiService.prompts`), never the replies.

If you have direct access to the other machine's `Cursor/User` directory (e.g. you copied it onto an external
drive, or it's on a network share), use `sync-chats` instead - it reads that directory's `globalStorage`
directly and merges the real sessions and messages in:

```bash
python -m cursor_mover sync-chats --source-user-dir "/Volumes/DRIVE/Cursor/User"

# Restrict to one local workspace, or preview first
python -m cursor_mover sync-chats --source-user-dir "/Volumes/DRIVE/Cursor/User" --path "/Users/me/Projects/Foo"
python -m cursor_mover sync-chats --source-user-dir "/Volumes/DRIVE/Cursor/User" --dry-run
```

Notes:
- The destination workspace folder must already have been opened in Cursor at least once locally (same
  requirement as `import`).
- Matches workspaces by exact folder path first, falling back to matching by folder name when the absolute
  path differs (e.g. moving from a Windows drive letter to a macOS home directory).
- If a folder name matches more than one local workspace (e.g. two source repos share a name, or one source
  name matches two local folders), that group is reported and, in an interactive terminal, you're asked
  whether to merge all of it into one local folder you pick - this is also how multiple same-named source
  workspaces get **combined** into a single local one. Declining, running non-interactively, or passing
  `--yes` leaves the group skipped (reported, not merged) - use `--path` to target one folder directly instead.
- Some source workspaces have chat history but no folder to match against at all - typically multi-root
  `.code-workspace` entries, which Cursor stores without a plain folder path. These are listed separately
  (with a preview of session names) with a ready-to-use command; merge one manually with
  `--source-workspace-id <id> --path <local folder>`.
- Always creates a timestamped backup (`state.vscdb.pre-global-merge-<timestamp>`) of the destination
  `globalStorage/state.vscdb` before writing, skips composer sessions that already exist locally, and runs
  `PRAGMA integrity_check` before committing.
- Close Cursor before running this (it modifies a database Cursor keeps open while running).

## Export/Import (commands)

For **cross-machine synchronization of lightweight workspace state** (editor layout, recent-prompt text, and
composer metadata - not full conversations, see `sync-chats` above for that), use `export` and `import`:

### Export (READ-ONLY)

Exports all workspace chat data to a portable JSON file. This operation is **strictly read-only** - it never writes to any database.

```powershell
# Export all workspaces
python -m cursor_mover export --output "./cursor_chats_export.json"

# Export only a specific workspace
python -m cursor_mover export --output "./my_project_chats.json" --path "C:\Projects\MyProject"
```

The export file includes:
- Machine name (for identification)
- All `ItemTable` and `cursorDiskKV` entries
- Composer/chat metadata with timestamps
- Export timestamp

### Import (with backup)

Imports chat data from an export file with **intelligent merge**:
- **Always creates a timestamped backup** before modifying any database
- **Skips existing keys** (does not overwrite local data)
- **Merges composer data by ID**, keeping the newest version by timestamp

```powershell
# Import all workspaces (with confirmation prompt)
python -m cursor_mover import --input "./cursor_chats_export.json"

# Import only a specific workspace
python -m cursor_mover import --input "./cursor_chats_export.json" --path "C:\Projects\MyProject"

# Dry run - see what would be imported without making changes
python -m cursor_mover import --input "./cursor_chats_export.json" --dry-run
```

### Cross-Machine Sync Workflow

1. **On source machine (e.g., "rog"):**
   ```powershell
   python -m cursor_mover export --output "./shared/cursor_chats.json"
   git add ./shared/cursor_chats.json && git commit -m "Export chats" && git push
   ```

2. **On destination machine (e.g., "gram"):**
   ```powershell
   git pull
   python -m cursor_mover import --input "./shared/cursor_chats.json"
   ```

**Important notes:**
- Close Cursor before running import (to avoid locked databases)
- Workspaces must exist locally (opened in Cursor at least once) for import to work
- Matches by exact folder path first, then falls back to matching by folder name if the absolute path
  differs across machines (e.g. Windows drive letter vs. macOS home directory). Ambiguous folder-name
  matches (multiple local workspaces with the same folder name) are skipped and reported - use `--path`
  to disambiguate.
- Backup files are created as `state.vscdb.preimport-<timestamp>` next to the original

## Doctor (command)

`doctor` prints how Cursor maps a workspace folder to `workspaceStorage/<id>`:

- folder URI (`file:///...`) used inside `workspaceStorage/*/workspace.json`;
- workspaceStorage id found by scanning existing `workspace.json`;
- workspaceStorage id computed from the folder path + filesystem metadata (what Cursor uses);
- the exact inputs used for hashing (fsPath + stat salt);
- lock check for `state.vscdb` (+ WAL/SHM if present) when a workspaceStorage entry is found.
- warns if multiple `workspaceStorage/<id>` entries exist for the same folder URI (and suggests `merge`).

This command is read-only (no modifications).

## Quick Start

Simplest run (Windows PowerShell):

```powershell
.\run.ps1
```

Simplest run (macOS/Linux):

```bash
./run.sh
```

Manual venv activation (Windows PowerShell):

```powershell
.venv\Scripts\Activate.ps1
```

Important: when launching via `python -m cursor_mover` / `python main.py` **from a repo checkout**, the utility attempts to automatically use `.venv` and install dependencies from `requirements.txt` (can be disabled via environment variables `CURSOR_MOVER_SKIP_BOOTSTRAP=1` and `CURSOR_MOVER_SKIP_INSTALL=1`).

## CLI overview

Show help:

```powershell
python -m cursor_mover --help
```

Global option:

- `--cursor-user-dir`: override auto-detected Cursor `.../Cursor/User` directory.

Interactive TUI (only when stdin/stdout are TTY):

```powershell
python -m cursor_mover
```

TUI includes `copy`, `move`, `doctor`, `merge`, `export`, and `import`.

Note: `copy` / `move` / `merge` require that the workspace folder was opened in Cursor at least once (so it has an existing `workspaceStorage/<id>` entry). If not, open the folder in Cursor and retry.

## Limitations

- Folder workspaces only (not multi-root `.code-workspace` files).
- Cursor storage format and workspace ID logic can change between versions; treat this as a best-effort utility.

About `--dst` semantics:

- If `--dst` **does not match** the source folder name (`--src`), then `--dst` is considered a *container* and the actual destination will be `--dst/<src name>`.
- If `--dst` **matches** the source folder name, then the copy/move is performed directly into `--dst`.

Check (doctor):

```powershell
python -m cursor_mover doctor --path "G:\GitHub\RUSTDemo"
```

Copy workspace + transfer chats (Mode C):

```powershell
python -m cursor_mover copy --src "G:\GitHub\RUSTDemo" --dst "T:\Temp\RUSTDemo"
```

Move workspace + transfer chats (Mode C):

```powershell
python -m cursor_mover move --src "G:\GitHub\RUSTDemo" --dst "T:\Temp\RUSTDemo"
```

Copy/move when destination workspaceStorage already exists (preserve destination chats):

```powershell
python -m cursor_mover copy --src "G:\GitHub\RUSTDemo" --dst "T:\Temp\RUSTDemo" --merge-workspace-storage
```

Unsafe copy with locked DB (experimental):

```powershell
python -m cursor_mover copy -UnsafeDB --src "G:\GitHub\RUSTDemo" --dst "T:\Temp\RUSTDemo"
```

Merge chat state from other workspaceStorage entries for the same folder URI (advanced):

```powershell
python -m cursor_mover merge --path "G:\GitHub\RUSTDemo"
```

## Tests

```powershell
python -m unittest -v
```

## Documentation

Comprehensive documentation is available:

- **[Installation Guide](INSTALL.md)** - Detailed installation instructions
- **[Usage Examples](EXAMPLES.md)** - Practical examples and scenarios
- **[FAQ](FAQ.md)** - Frequently asked questions
- **[Contributing Guide](CONTRIBUTING.md)** - How to contribute to the project
- **[Publishing Guide](PUBLISHING.md)** - Instructions for maintainers
- **[Security Policy](SECURITY.md)** - Security information and reporting
- **[Changelog](CHANGELOG.md)** - Version history and changes
- **[Roadmap](ROADMAP.md)** - Future plans and ideas
- **[Pre-Release Checklist](PRE_RELEASE_CHECKLIST.md)** - Release preparation checklist

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for details on how to contribute to this project.

### Code of Conduct

Please note that this project is released with a [Contributor Code of Conduct](CODE_OF_CONDUCT.md). By participating in this project you agree to abide by its terms.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a list of changes in each version.

## Support

If you encounter any issues or have questions:

1. Check the [existing issues](https://github.com/Artemonim/CursorMover/issues)
2. Read the [documentation](README.md)
3. Open a [new issue](https://github.com/Artemonim/CursorMover/issues/new/choose) if needed

## Acknowledgments

- Inspired by the Cursor Community discussion on workspace migration
- Built with Python and love for the developer community

## Disclaimer

This is a best-effort utility. Cursor's internal storage format and workspace ID logic may change between versions. Always make backups before performing operations on important workspaces.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.