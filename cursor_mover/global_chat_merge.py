"""Merge real chat/composer content from a source machine's globalStorage.

Cursor stores the actual conversation content (composer headers + every
message "bubble") in `globalStorage/state.vscdb`, keyed by workspace id -
NOT in the per-workspace `state.vscdb` that `export`/`import` operate on.
This module copies that content across machines for a given set of
(source workspace id -> destination workspace id) pairs, rewriting the
embedded `workspaceIdentifier` so Cursor's UI associates the sessions with
the local workspace.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cursor_mover.locks import assert_paths_unlocked
from cursor_mover.workspace_storage import iter_workspace_storage_entries
from cursor_mover.workspace_uri import folder_uri_basename, folder_uri_display_path


@dataclass(frozen=True, slots=True)
class SourceWorkspaceChatCount:
    """Real chat session count for one source workspace, read straight from globalStorage.

    An export JSON file cannot answer "how many real chats does this folder
    have" - it never captures `globalStorage` (see module docstring). This
    reads `composerHeaders` on the source machine directly instead, so the
    counts here are authoritative.
    """

    workspace_id: str
    folder_uri: str | None  # None for multi-root .code-workspace entries
    workspace_config_uri: str | None
    session_count: int

    @property
    def display_path(self) -> str:
        if self.folder_uri:
            return folder_uri_display_path(self.folder_uri)
        return f"(multi-root workspace, config={self.workspace_config_uri})"


def list_source_chat_counts(source_cursor_user_dir: Path) -> list[SourceWorkspaceChatCount]:
    """Lists every source workspace with its real chat session count.

    Includes workspaces with 0 sessions, so it answers "how many real chats
    does each folder have" directly - unlike an export JSON's per-workspace
    `composer.composerData` index, which is frequently 0 even for folders
    with substantial real history (see `sync-chats` docs).
    """
    source_db = source_cursor_user_dir / "globalStorage" / "state.vscdb"
    if not source_db.exists():
        raise FileNotFoundError(f"Source globalStorage database not found: {source_db}")

    con = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT workspaceId, COUNT(*) FROM composerHeaders GROUP BY workspaceId")
        counts_by_id = dict(cur.fetchall())
    finally:
        con.close()

    results: list[SourceWorkspaceChatCount] = []
    for entry in iter_workspace_storage_entries(source_cursor_user_dir):
        if not entry.folder_uri and not entry.workspace_config_uri:
            continue  # e.g. an "empty-window" placeholder with no real identity

        results.append(
            SourceWorkspaceChatCount(
                workspace_id=entry.workspace_id,
                folder_uri=entry.folder_uri,
                workspace_config_uri=entry.workspace_config_uri,
                session_count=counts_by_id.get(entry.workspace_id, 0),
            )
        )

    return sorted(results, key=lambda r: r.session_count, reverse=True)


@dataclass(frozen=True, slots=True)
class OrphanedSourceWorkspace:
    """A source workspace with real chat history that folder-based matching can't see.

    Typically a multi-root `.code-workspace` - Cursor stores those with a
    `workspace` (config file) key in `workspace.json` instead of a plain
    `folder` URI, so they never get a folder_uri and never appear in
    `match_source_to_local_workspaces`'s output. The only way to recover
    this history is to merge it manually by workspace id into an explicit
    local destination.
    """

    workspace_id: str
    workspace_config_uri: str | None
    session_count: int
    sample_session_names: tuple[str, ...]


def find_orphaned_source_workspaces(
    source_cursor_user_dir: Path,
    *,
    sample_size: int = 5,
) -> list[OrphanedSourceWorkspace]:
    """Finds source workspaces with chat history that have no folder to match against."""
    source_db = source_cursor_user_dir / "globalStorage" / "state.vscdb"
    if not source_db.exists():
        return []

    entries_by_id = {e.workspace_id: e for e in iter_workspace_storage_entries(source_cursor_user_dir)}

    con = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT workspaceId, COUNT(*) FROM composerHeaders GROUP BY workspaceId")
        results: list[OrphanedSourceWorkspace] = []
        for workspace_id, count in cur.fetchall():
            entry = entries_by_id.get(workspace_id)
            if entry is None or entry.folder_uri or not entry.workspace_config_uri:
                continue

            cur.execute(
                "SELECT value FROM composerHeaders WHERE workspaceId=? ORDER BY lastUpdatedAt DESC LIMIT ?",
                (workspace_id, sample_size),
            )
            names: list[str] = []
            for (value,) in cur.fetchall():
                try:
                    name = json.loads(value).get("name")
                except (json.JSONDecodeError, AttributeError):
                    name = None
                if name:
                    names.append(name)

            results.append(
                OrphanedSourceWorkspace(
                    workspace_id=workspace_id,
                    workspace_config_uri=entry.workspace_config_uri,
                    session_count=count,
                    sample_session_names=tuple(names),
                )
            )
        return results
    finally:
        con.close()


@dataclass(frozen=True, slots=True)
class AmbiguousGroup:
    """One or more source workspaces sharing a folder name that matches multiple local folders.

    Left unresolved, none of `source_ids` are merged anywhere. Resolving the
    group means picking one of `local_candidates` (by folder URI) to merge
    all of `source_ids` into - this is also how multiple same-named source
    workspaces (e.g. two "devops" repos on the source machine) get combined
    into a single local one, since all their ids simply map to the one chosen
    destination.
    """

    basename: str
    local_candidates: tuple[str, ...]  # local folder URIs to choose from
    source_uris: tuple[str, ...]  # source folder URIs that share this basename
    source_ids: tuple[str, ...]  # source workspace ids across those uris


@dataclass(frozen=True, slots=True)
class WorkspaceMatch:
    """Result of matching source workspaces to local ones by folder."""

    matched: dict[str, list[str]]  # local workspace id -> [source workspace ids]
    unmatched_source_uris: tuple[str, ...]  # source folder_uri with no local counterpart
    ambiguous: tuple[AmbiguousGroup, ...]


def match_source_to_local_workspaces(
    source_by_uri: dict[str, list[str]],
    local_by_uri: dict[str, str],
) -> WorkspaceMatch:
    """Matches source workspace folder URIs to local ones.

    Tries an exact folder_uri match first, falling back to matching by folder
    basename - the exact absolute path (drive letter, home directory, or even
    OS) almost never matches across machines, but the project folder name
    usually does. Source URIs whose basename matches more than one local
    folder are grouped together per basename (see `AmbiguousGroup`) instead
    of being matched automatically, since picking the right destination (or
    combining several source workspaces into one) requires a decision only
    the caller/user can make.
    """
    local_by_basename: dict[str, list[str]] = {}
    for uri in local_by_uri:
        local_by_basename.setdefault(folder_uri_basename(uri), []).append(uri)

    matched: dict[str, list[str]] = {}
    unmatched: list[str] = []
    ambiguous_by_basename: dict[str, dict[str, object]] = {}

    for source_uri, source_ids in source_by_uri.items():
        local_id = local_by_uri.get(source_uri)
        if local_id is not None:
            matched.setdefault(local_id, []).extend(source_ids)
            continue

        basename = folder_uri_basename(source_uri)
        candidates = local_by_basename.get(basename, [])
        if len(candidates) == 1:
            matched.setdefault(local_by_uri[candidates[0]], []).extend(source_ids)
        elif len(candidates) > 1:
            group = ambiguous_by_basename.setdefault(
                basename, {"candidates": tuple(candidates), "uris": [], "ids": []}
            )
            group["uris"].append(source_uri)
            group["ids"].extend(source_ids)
        else:
            unmatched.append(source_uri)

    ambiguous = tuple(
        AmbiguousGroup(
            basename=basename,
            local_candidates=group["candidates"],
            source_uris=tuple(group["uris"]),
            source_ids=tuple(group["ids"]),
        )
        for basename, group in ambiguous_by_basename.items()
    )

    return WorkspaceMatch(matched=matched, unmatched_source_uris=tuple(unmatched), ambiguous=ambiguous)


@dataclass(frozen=True, slots=True)
class GlobalMergeResult:
    """Summary of a global chat-history merge operation."""

    headers_inserted: int
    headers_skipped_existing: int
    composer_data_inserted: int
    composer_data_skipped_existing: int
    bubbles_inserted: int
    bubbles_skipped_existing: int
    backup_file: Path | None
    dry_run: bool = False


def _create_timestamped_backup(file_path: Path) -> Path | None:
    """Creates a consistent timestamped backup of a SQLite database file."""
    if not file_path.exists():
        return None

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = file_path.with_name(f"{file_path.name}.pre-global-merge-{ts}")

    src_con = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
    try:
        backup_con = sqlite3.connect(backup_path.as_posix())
        try:
            src_con.backup(backup_con)
        finally:
            backup_con.close()
    finally:
        src_con.close()

    return backup_path


def _rewrite_workspace_identifier(value_text: str, *, new_workspace_id: str, new_folder_uri: str) -> str:
    """Rewrites the embedded `workspaceIdentifier` of a composerHeaders `value` JSON blob."""
    payload = json.loads(value_text)
    identifier = payload.get("workspaceIdentifier")
    if isinstance(identifier, dict):
        identifier["id"] = new_workspace_id
        uri = identifier.get("uri")
        if isinstance(uri, dict):
            local_path = _folder_uri_to_fspath(new_folder_uri)
            uri["fsPath"] = local_path
            uri["external"] = new_folder_uri
            uri["path"] = local_path
            uri["scheme"] = "file"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _folder_uri_to_fspath(folder_uri: str) -> str:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(folder_uri)
    return unquote(parsed.path)


def merge_global_composer_history(
    *,
    source_cursor_user_dir: Path,
    dest_cursor_user_dir: Path,
    workspace_id_map: dict[str, str],
    dest_folder_uris: dict[str, str],
    dry_run: bool = False,
) -> GlobalMergeResult:
    """Merges composer headers, composer data, and message bubbles for the given workspaces.

    Args:
        source_cursor_user_dir: The other machine's `Cursor/User` directory (read-only).
        dest_cursor_user_dir: The local `Cursor/User` directory to merge into.
        workspace_id_map: Maps source workspaceStorage id -> destination workspaceStorage id.
        dest_folder_uris: Maps destination workspaceStorage id -> its local `file://` folder URI
            (used to rewrite the embedded `workspaceIdentifier` in each composerHeaders row).
        dry_run: If True, only count what would change; nothing is written.
    """
    source_db = source_cursor_user_dir / "globalStorage" / "state.vscdb"
    dest_db = dest_cursor_user_dir / "globalStorage" / "state.vscdb"

    if not source_db.exists():
        raise FileNotFoundError(f"Source globalStorage database not found: {source_db}")
    if not dest_db.exists():
        raise FileNotFoundError(f"Destination globalStorage database not found: {dest_db}")

    lock_paths = [dest_db, dest_db.with_name(dest_db.name + "-wal"), dest_db.with_name(dest_db.name + "-shm")]
    assert_paths_unlocked([p for p in lock_paths if p.exists()])

    src_con = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)

    headers_inserted = 0
    headers_skipped = 0
    composer_data_inserted = 0
    composer_data_skipped = 0
    bubbles_inserted = 0
    bubbles_skipped = 0
    backup_file: Path | None = None

    try:
        if dry_run:
            dest_con = sqlite3.connect(f"file:{dest_db.as_posix()}?mode=ro", uri=True)
        else:
            backup_file = _create_timestamped_backup(dest_db)
            dest_con = sqlite3.connect(dest_db.as_posix())

        try:
            src_cur = src_con.cursor()
            dest_cur = dest_con.cursor()

            for source_ws_id, dest_ws_id in workspace_id_map.items():
                dest_folder_uri = dest_folder_uris[dest_ws_id]

                src_cur.execute(
                    "SELECT composerId, createdAt, lastUpdatedAt, isArchived, isSubagent, "
                    "recency, checkpointAt, value FROM composerHeaders WHERE workspaceId=?",
                    (source_ws_id,),
                )
                header_rows = src_cur.fetchall()

                composer_ids_to_copy: list[str] = []

                for row in header_rows:
                    composer_id = row[0]
                    dest_cur.execute(
                        "SELECT 1 FROM composerHeaders WHERE composerId=?", (composer_id,)
                    )
                    if dest_cur.fetchone() is not None:
                        headers_skipped += 1
                        continue

                    composer_ids_to_copy.append(composer_id)
                    new_value = _rewrite_workspace_identifier(
                        row[7], new_workspace_id=dest_ws_id, new_folder_uri=dest_folder_uri
                    )

                    if not dry_run:
                        dest_cur.execute(
                            "INSERT INTO composerHeaders "
                            "(composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, "
                            "isSubagent, recency, checkpointAt, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (composer_id, dest_ws_id, row[1], row[2], row[3], row[4], row[5], row[6], new_value),
                        )
                    headers_inserted += 1

                for composer_id in composer_ids_to_copy:
                    key = f"composerData:{composer_id}"
                    src_cur.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,))
                    row = src_cur.fetchone()
                    if row is not None:
                        dest_cur.execute("SELECT 1 FROM cursorDiskKV WHERE key=?", (key,))
                        if dest_cur.fetchone() is not None:
                            composer_data_skipped += 1
                        else:
                            if not dry_run:
                                dest_cur.execute(
                                    "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)", (key, row[0])
                                )
                            composer_data_inserted += 1

                    src_cur.execute(
                        "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                        (f"bubbleId:{composer_id}:%",),
                    )
                    for bubble_key, bubble_value in src_cur.fetchall():
                        dest_cur.execute("SELECT 1 FROM cursorDiskKV WHERE key=?", (bubble_key,))
                        if dest_cur.fetchone() is not None:
                            bubbles_skipped += 1
                            continue
                        if not dry_run:
                            dest_cur.execute(
                                "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
                                (bubble_key, bubble_value),
                            )
                        bubbles_inserted += 1

            if not dry_run:
                check = dest_cur.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    dest_con.rollback()
                    raise RuntimeError(f"SQLite integrity_check failed: {check[0] if check else 'unknown'}")
                dest_con.commit()
        finally:
            dest_con.close()
    finally:
        src_con.close()

    return GlobalMergeResult(
        headers_inserted=headers_inserted,
        headers_skipped_existing=headers_skipped,
        composer_data_inserted=composer_data_inserted,
        composer_data_skipped_existing=composer_data_skipped,
        bubbles_inserted=bubbles_inserted,
        bubbles_skipped_existing=bubbles_skipped,
        backup_file=backup_file,
        dry_run=dry_run,
    )
