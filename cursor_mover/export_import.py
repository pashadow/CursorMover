"""Export and import workspace chat data for cross-machine synchronization.

This module provides:
  - export: Reads workspace DBs (READ-ONLY) and produces a portable JSON file.
  - import: Merges exported data into local workspace DBs (with timestamped backup).

Design principles:
  - Export is strictly READ-ONLY - it never writes to any database.
  - Import always creates a timestamped backup before modifying any file.
  - Intelligent merge: skip existing items, prefer newest by timestamp when conflicts occur.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cursor_mover.cursor_paths import default_cursor_user_dir, workspace_storage_root
from cursor_mover.locks import WorkspaceStorageLockedError, assert_paths_unlocked
from cursor_mover.workspace_storage import (
    iter_workspace_storage_entries,
    workspace_db_paths,
    WorkspaceStorageEntry,
)
from cursor_mover.workspace_uri import folder_uri_basename, folder_uri_display_path


EXPORT_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Summary of an export operation."""

    output_file: Path
    machine_name: str
    workspaces_exported: int
    total_itemtable_keys: int
    total_cursordiskkv_keys: int
    total_composer_ids: int
    export_timestamp: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary of an import operation."""

    input_file: Path
    source_machine: str
    workspaces_processed: int
    workspaces_created: int
    workspaces_updated: int
    workspaces_skipped: int
    itemtable_keys_inserted: int
    cursordiskkv_keys_inserted: int
    composer_ids_before: int
    composer_ids_after: int
    backup_files: tuple[Path, ...]
    matched_by_name: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    ambiguous_name_matches: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)


@dataclass
class WorkspaceExportData:
    """Exported data for a single workspace."""

    folder_uri: str
    workspace_config_uri: str | None
    itemtable_entries: dict[str, str]  # key -> base64(value)
    cursordiskkv_entries: dict[str, str]  # key -> base64(value)
    composer_data_raw: str | None  # base64 of composer.composerData if present
    export_timestamp: str


def _get_machine_name() -> str:
    """Get current machine hostname for identification."""
    return os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown"))


def _timestamp_now() -> str:
    """Returns ISO timestamp string for current time in UTC."""
    return datetime.now(timezone.utc).isoformat()


def _read_workspace_database_readonly(db_path: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Reads ItemTable and cursorDiskKV from a workspace database.
    
    This is a READ-ONLY operation - the database is opened in read-only mode.
    
    Returns:
        Tuple of (itemtable_dict, cursordiskkv_dict) where values are raw bytes.
    """
    if not db_path.exists():
        return {}, {}
    
    # Open database in read-only mode explicitly
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        
        itemtable: dict[str, bytes] = {}
        cursordiskkv: dict[str, bytes] = {}
        
        # Read ItemTable
        try:
            for key, value in cur.execute("SELECT key, value FROM ItemTable"):
                if isinstance(value, memoryview):
                    value = value.tobytes()
                elif not isinstance(value, bytes):
                    value = str(value).encode("utf-8")
                itemtable[key] = value
        except sqlite3.OperationalError:
            # Table doesn't exist
            pass
        
        # Read cursorDiskKV
        try:
            for key, value in cur.execute("SELECT key, value FROM cursorDiskKV"):
                if isinstance(value, memoryview):
                    value = value.tobytes()
                elif not isinstance(value, bytes):
                    value = str(value).encode("utf-8")
                cursordiskkv[key] = value
        except sqlite3.OperationalError:
            # Table doesn't exist
            pass
        
        return itemtable, cursordiskkv
    finally:
        con.close()


def _extract_composer_ids(composer_data_bytes: bytes | None) -> set[str]:
    """Extract composer IDs from composer.composerData."""
    if not composer_data_bytes:
        return set()
    try:
        payload = json.loads(composer_data_bytes.decode("utf-8"))
        composers = payload.get("allComposers", [])
        ids: set[str] = set()
        for item in composers:
            if isinstance(item, dict):
                cid = item.get("composerId")
                if isinstance(cid, str):
                    ids.add(cid)
        return ids
    except Exception:
        return set()


def export_workspace_chats(
    *,
    output_path: Path,
    cursor_user_dir: Path | None = None,
    folder_filter: Path | None = None,
    unsafe_db: bool = False,
) -> ExportResult:
    """Exports workspace chat data to a portable JSON file.
    
    This operation is READ-ONLY - it never writes to any database.
    
    Args:
        output_path: Path to write the export JSON file.
        cursor_user_dir: Override Cursor User directory (default: auto-detect).
        folder_filter: If provided, only export data for this specific workspace folder.
        unsafe_db: If True, skip lock checks (may read inconsistent data).
    
    Returns:
        ExportResult with summary of what was exported.
    
    Raises:
        WorkspaceStorageLockedError: If databases are locked and unsafe_db=False.
    """
    if cursor_user_dir is None:
        cursor_user_dir = default_cursor_user_dir()
    
    machine_name = _get_machine_name()
    export_timestamp = _timestamp_now()
    
    workspaces_data: list[dict[str, Any]] = []
    total_itemtable_keys = 0
    total_cursordiskkv_keys = 0
    total_composer_ids = 0
    
    for entry in iter_workspace_storage_entries(cursor_user_dir):
        # Skip entries without folder URI (not folder workspaces)
        if not entry.folder_uri:
            continue
        
        # If filter is specified, only export matching workspace
        if folder_filter is not None:
            from cursor_mover.workspace_uri import path_to_folder_uri
            filter_uri = path_to_folder_uri(folder_filter.resolve())
            if entry.folder_uri != filter_uri:
                continue
        
        db_path = entry.storage_dir / "state.vscdb"
        
        # Check locks unless unsafe mode
        if not unsafe_db:
            lock_paths = list(workspace_db_paths(entry.storage_dir))
            existing_lock_paths = [p for p in lock_paths if p.exists()]
            if existing_lock_paths:
                try:
                    assert_paths_unlocked(existing_lock_paths)
                except WorkspaceStorageLockedError:
                    # Skip locked workspaces but don't fail entirely
                    continue
        
        # READ-ONLY: Read the database
        itemtable, cursordiskkv = _read_workspace_database_readonly(db_path)
        
        # Get composer data if present
        composer_data_raw = itemtable.get("composer.composerData")
        composer_ids = _extract_composer_ids(composer_data_raw)
        
        # Encode values as base64 for JSON serialization
        itemtable_b64 = {k: base64.b64encode(v).decode("ascii") for k, v in itemtable.items()}
        cursordiskkv_b64 = {k: base64.b64encode(v).decode("ascii") for k, v in cursordiskkv.items()}
        composer_b64 = base64.b64encode(composer_data_raw).decode("ascii") if composer_data_raw else None
        
        workspace_export = {
            "folder_uri": entry.folder_uri,
            "workspace_config_uri": entry.workspace_config_uri,
            "itemtable_entries": itemtable_b64,
            "cursordiskkv_entries": cursordiskkv_b64,
            "composer_data_raw": composer_b64,
            "export_timestamp": export_timestamp,
        }
        workspaces_data.append(workspace_export)
        
        total_itemtable_keys += len(itemtable)
        total_cursordiskkv_keys += len(cursordiskkv)
        total_composer_ids += len(composer_ids)
    
    # Build the export document
    export_document = {
        "format_version": EXPORT_FORMAT_VERSION,
        "source_machine": machine_name,
        "export_timestamp": export_timestamp,
        "workspaces": workspaces_data,
    }
    
    # Write the export file
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export_document, indent=2, ensure_ascii=False), encoding="utf-8")
    
    return ExportResult(
        output_file=output_path,
        machine_name=machine_name,
        workspaces_exported=len(workspaces_data),
        total_itemtable_keys=total_itemtable_keys,
        total_cursordiskkv_keys=total_cursordiskkv_keys,
        total_composer_ids=total_composer_ids,
        export_timestamp=export_timestamp,
    )


@dataclass(frozen=True, slots=True)
class ExportWorkspaceInfo:
    """Summary of one workspace entry inside an export JSON file."""

    folder_uri: str
    folder_path: str
    itemtable_key_count: int
    cursordiskkv_key_count: int
    composer_session_count: int
    prompt_count: int


def list_export_workspaces(input_path: Path) -> tuple[str, list[ExportWorkspaceInfo]]:
    """Reads an export JSON file and summarizes the workspaces it contains.

    This is read-only and does not touch any Cursor database - it only
    parses the export file itself, to answer "which folders are in this
    export?" without running an actual import.

    Returns:
        Tuple of (source_machine, list of per-workspace summaries).

    Raises:
        FileNotFoundError: If the export file doesn't exist.
        ValueError: If the export file format is invalid/unsupported.
    """
    input_path = input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")

    try:
        export_document = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid export file format: {e}") from e

    format_version = export_document.get("format_version")
    if format_version != EXPORT_FORMAT_VERSION:
        raise ValueError(
            f"Unsupported export format version: {format_version} "
            f"(expected {EXPORT_FORMAT_VERSION})"
        )

    source_machine = export_document.get("source_machine", "unknown")
    infos: list[ExportWorkspaceInfo] = []

    for ws_data in export_document.get("workspaces", []):
        folder_uri = ws_data.get("folder_uri") or ""
        folder_path = folder_uri_display_path(folder_uri) if folder_uri else "(no folder - multi-root workspace)"

        itemtable = ws_data.get("itemtable_entries", {})

        composer_session_count = 0
        if "composer.composerData" in itemtable:
            try:
                payload = json.loads(base64.b64decode(itemtable["composer.composerData"]).decode("utf-8"))
                composer_session_count = len(payload.get("allComposers", []))
            except Exception:
                composer_session_count = 0

        prompt_count = 0
        if "aiService.prompts" in itemtable:
            try:
                prompts = json.loads(base64.b64decode(itemtable["aiService.prompts"]).decode("utf-8"))
                prompt_count = len(prompts) if isinstance(prompts, list) else 0
            except Exception:
                prompt_count = 0

        infos.append(
            ExportWorkspaceInfo(
                folder_uri=folder_uri,
                folder_path=folder_path,
                itemtable_key_count=len(itemtable),
                cursordiskkv_key_count=len(ws_data.get("cursordiskkv_entries", {})),
                composer_session_count=composer_session_count,
                prompt_count=prompt_count,
            )
        )

    return source_machine, infos


def _create_timestamped_backup(file_path: Path) -> Path | None:
    """Creates a timestamped backup of a file before modification.
    
    Returns:
        Path to the backup file, or None if file doesn't exist.
    """
    if not file_path.exists():
        return None
    
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_name = f"{file_path.name}.preimport-{ts}"
    backup_path = file_path.with_name(backup_name)
    
    # Use SQLite backup API for database files to ensure consistency
    if file_path.suffix == ".vscdb":
        src_con = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
        try:
            backup_con = sqlite3.connect(backup_path.as_posix())
            try:
                src_con.backup(backup_con)
            finally:
                backup_con.close()
        finally:
            src_con.close()
    else:
        shutil.copy2(file_path, backup_path)
    
    return backup_path


def _ensure_tables_exist(cur: sqlite3.Cursor) -> None:
    """Ensures ItemTable and cursorDiskKV tables exist."""
    cur.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    cur.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")


def _merge_composer_data_with_imported(
    existing_raw: bytes | None,
    imported_raw: bytes | None,
) -> bytes | None:
    """Merges composer data, preferring the newest version of each composer by timestamp."""
    if not imported_raw:
        return existing_raw
    if not existing_raw:
        return imported_raw
    
    try:
        existing_payload = json.loads(existing_raw.decode("utf-8"))
        imported_payload = json.loads(imported_raw.decode("utf-8"))
    except Exception:
        # If either fails to parse, prefer existing
        return existing_raw
    
    existing_composers = existing_payload.get("allComposers", [])
    imported_composers = imported_payload.get("allComposers", [])
    
    if not isinstance(existing_composers, list):
        existing_composers = []
    if not isinstance(imported_composers, list):
        imported_composers = []
    
    # Build a map by composerId, keeping the newest version
    merged: dict[str, dict] = {}
    
    for item in existing_composers:
        if not isinstance(item, dict):
            continue
        cid = item.get("composerId")
        if isinstance(cid, str):
            merged[cid] = item
    
    for item in imported_composers:
        if not isinstance(item, dict):
            continue
        cid = item.get("composerId")
        if not isinstance(cid, str):
            continue
        
        prev = merged.get(cid)
        if prev is None:
            merged[cid] = item
            continue
        
        # Compare timestamps - prefer the newest
        prev_ts = prev.get("lastUpdatedAt")
        new_ts = item.get("lastUpdatedAt")
        if isinstance(prev_ts, (int, float)) and isinstance(new_ts, (int, float)):
            if new_ts > prev_ts:
                merged[cid] = item
        elif prev_ts is None and new_ts is not None:
            merged[cid] = item
        # Also check size as a fallback heuristic (larger = more content = more important)
        elif prev_ts == new_ts:
            prev_size = len(json.dumps(prev))
            new_size = len(json.dumps(item))
            if new_size > prev_size:
                merged[cid] = item
    
    # Sort by lastUpdatedAt (desc), fallback to createdAt
    def sort_key(item: dict) -> int:
        ts = item.get("lastUpdatedAt")
        if isinstance(ts, (int, float)):
            return int(ts)
        ts = item.get("createdAt")
        if isinstance(ts, (int, float)):
            return int(ts)
        return 0
    
    merged_list = sorted(merged.values(), key=sort_key, reverse=True)
    
    # Use existing payload as base structure, replace allComposers
    result_payload = existing_payload.copy()
    result_payload["allComposers"] = merged_list
    
    return json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def import_workspace_chats(
    *,
    input_path: Path,
    cursor_user_dir: Path | None = None,
    folder_filter: Path | None = None,
    create_missing_workspaces: bool = False,
    dry_run: bool = False,
) -> ImportResult:
    """Imports workspace chat data from an export file with intelligent merge.
    
    This operation:
      - Creates a timestamped backup before modifying any database.
      - Skips existing keys (does not overwrite).
      - For composer.composerData, merges by composerId, keeping newest by timestamp.
    
    Args:
        input_path: Path to the export JSON file.
        cursor_user_dir: Override Cursor User directory (default: auto-detect).
        folder_filter: If provided, only import data for this specific workspace folder.
        create_missing_workspaces: If True, create workspaceStorage for folders
            that don't have local entries (requires the folder to exist locally).
        dry_run: If True, don't actually modify anything, just report what would happen.
    
    Returns:
        ImportResult with summary of what was imported.
    
    Raises:
        FileNotFoundError: If input file doesn't exist.
        ValueError: If input file format is invalid.
        WorkspaceStorageLockedError: If databases are locked.
    """
    if cursor_user_dir is None:
        cursor_user_dir = default_cursor_user_dir()
    
    input_path = input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")
    
    # Load export document
    try:
        export_document = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid export file format: {e}") from e
    
    format_version = export_document.get("format_version")
    if format_version != EXPORT_FORMAT_VERSION:
        raise ValueError(
            f"Unsupported export format version: {format_version} "
            f"(expected {EXPORT_FORMAT_VERSION})"
        )
    
    source_machine = export_document.get("source_machine", "unknown")
    workspaces_data = export_document.get("workspaces", [])
    
    if folder_filter is not None:
        from cursor_mover.workspace_uri import path_to_folder_uri
        filter_uri = path_to_folder_uri(folder_filter.resolve())
        # The source machine's folder_uri is virtually never identical to the
        # local one (different drive/home dir, or a different OS entirely),
        # so also allow filtering by folder basename.
        filter_basename = folder_filter.resolve().name
    else:
        filter_uri = None
        filter_basename = None

    # Build a map of folder_uri -> local workspaceStorage entry, plus a
    # basename -> [entries] index used as a fallback match for cross-machine
    # imports where the exported folder_uri's absolute path never matches
    # the local one (e.g. Windows -> macOS, different drive/home directory).
    local_entries: dict[str, WorkspaceStorageEntry] = {}
    local_by_basename: dict[str, list[WorkspaceStorageEntry]] = {}
    for entry in iter_workspace_storage_entries(cursor_user_dir):
        if entry.folder_uri:
            local_entries[entry.folder_uri] = entry
            local_by_basename.setdefault(folder_uri_basename(entry.folder_uri), []).append(entry)

    workspaces_processed = 0
    workspaces_created = 0
    workspaces_updated = 0
    workspaces_skipped = 0
    itemtable_keys_inserted = 0
    cursordiskkv_keys_inserted = 0
    composer_ids_before = 0
    composer_ids_after = 0
    backup_files: list[Path] = []
    matched_by_name: list[tuple[str, str]] = []
    ambiguous_name_matches: list[tuple[str, tuple[str, ...]]] = []
    
    for ws_data in workspaces_data:
        folder_uri = ws_data.get("folder_uri")
        if not folder_uri:
            continue
        
        # Apply filter (exact URI match, or fall back to folder basename
        # match for cross-machine imports where paths never line up exactly)
        if filter_uri is not None and folder_uri != filter_uri:
            if filter_basename is None or folder_uri_basename(folder_uri) != filter_basename:
                continue
        
        workspaces_processed += 1
        
        # Check if we have a local entry for this exact folder_uri.
        local_entry = local_entries.get(folder_uri)

        # Fall back to matching by folder basename - the exact absolute path
        # (drive letter, home directory, OS) almost never matches across
        # machines, but the project folder name usually does.
        if local_entry is None:
            basename = folder_uri_basename(folder_uri)
            candidates = local_by_basename.get(basename, [])
            if len(candidates) == 1:
                local_entry = candidates[0]
                matched_by_name.append((folder_uri, local_entry.folder_uri or ""))
            elif len(candidates) > 1:
                ambiguous_name_matches.append(
                    (basename, tuple(c.folder_uri or "" for c in candidates))
                )

        if local_entry is None:
            if not create_missing_workspaces:
                workspaces_skipped += 1
                continue
            else:
                # TODO: Could compute workspace ID and create entry, but this requires
                # the folder to exist locally with matching stat metadata.
                # For now, skip - user needs to open the folder in Cursor first.
                workspaces_skipped += 1
                continue
        
        db_path = local_entry.storage_dir / "state.vscdb"
        
        # Check locks
        lock_paths = list(workspace_db_paths(local_entry.storage_dir))
        existing_lock_paths = [p for p in lock_paths if p.exists()]
        if existing_lock_paths:
            assert_paths_unlocked(existing_lock_paths)
        
        # Decode imported data
        imported_itemtable: dict[str, bytes] = {}
        imported_cursordiskkv: dict[str, bytes] = {}
        
        for key, b64_value in ws_data.get("itemtable_entries", {}).items():
            try:
                imported_itemtable[key] = base64.b64decode(b64_value)
            except Exception:
                pass
        
        for key, b64_value in ws_data.get("cursordiskkv_entries", {}).items():
            try:
                imported_cursordiskkv[key] = base64.b64decode(b64_value)
            except Exception:
                pass
        
        imported_composer_raw = None
        if ws_data.get("composer_data_raw"):
            try:
                imported_composer_raw = base64.b64decode(ws_data["composer_data_raw"])
            except Exception:
                pass
        
        if dry_run:
            # Just count what would be inserted
            existing_itemtable, existing_cursordiskkv = _read_workspace_database_readonly(db_path)
            new_itemtable_keys = set(imported_itemtable.keys()) - set(existing_itemtable.keys())
            new_cursordiskkv_keys = set(imported_cursordiskkv.keys()) - set(existing_cursordiskkv.keys())
            
            itemtable_keys_inserted += len(new_itemtable_keys)
            cursordiskkv_keys_inserted += len(new_cursordiskkv_keys)
            
            existing_composer = existing_itemtable.get("composer.composerData")
            composer_ids_before += len(_extract_composer_ids(existing_composer))
            merged_composer = _merge_composer_data_with_imported(existing_composer, imported_composer_raw)
            composer_ids_after += len(_extract_composer_ids(merged_composer))
            
            workspaces_updated += 1
            continue
        
        # Create backup before modifying
        if db_path.exists():
            backup = _create_timestamped_backup(db_path)
            if backup:
                backup_files.append(backup)
        
        # Read existing data
        existing_itemtable, existing_cursordiskkv = _read_workspace_database_readonly(db_path)
        
        # Track composer IDs before merge
        existing_composer = existing_itemtable.get("composer.composerData")
        composer_ids_before += len(_extract_composer_ids(existing_composer))
        
        # Open database for writing
        con = sqlite3.connect(db_path.as_posix())
        try:
            cur = con.cursor()
            _ensure_tables_exist(cur)
            
            # Insert missing ItemTable keys (skip existing - intelligent merge)
            for key, value in imported_itemtable.items():
                if key == "composer.composerData":
                    continue  # Handle separately
                if key not in existing_itemtable:
                    cur.execute("INSERT INTO ItemTable(key, value) VALUES (?, ?)", (key, value))
                    itemtable_keys_inserted += 1
            
            # Insert missing cursorDiskKV keys
            for key, value in imported_cursordiskkv.items():
                if key not in existing_cursordiskkv:
                    cur.execute("INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)", (key, value))
                    cursordiskkv_keys_inserted += 1
            
            # Merge composer data intelligently
            merged_composer = _merge_composer_data_with_imported(existing_composer, imported_composer_raw)
            if merged_composer is not None:
                cur.execute(
                    "INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)",
                    ("composer.composerData", merged_composer),
                )
            
            composer_ids_after += len(_extract_composer_ids(merged_composer))
            
            # Integrity check
            check = cur.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError(f"SQLite integrity_check failed: {check[0] if check else 'unknown'}")
            
            con.commit()
        finally:
            con.close()
        
        workspaces_updated += 1
    
    return ImportResult(
        input_file=input_path,
        source_machine=source_machine,
        workspaces_processed=workspaces_processed,
        workspaces_created=workspaces_created,
        workspaces_updated=workspaces_updated,
        workspaces_skipped=workspaces_skipped,
        itemtable_keys_inserted=itemtable_keys_inserted,
        cursordiskkv_keys_inserted=cursordiskkv_keys_inserted,
        composer_ids_before=composer_ids_before,
        composer_ids_after=composer_ids_after,
        backup_files=tuple(backup_files),
        matched_by_name=tuple(matched_by_name),
        ambiguous_name_matches=tuple(ambiguous_name_matches),
    )
