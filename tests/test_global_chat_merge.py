"""Unit tests for cross-machine global chat-history merging."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cursor_mover.global_chat_merge import (
    find_orphaned_source_workspaces,
    list_source_chat_counts,
    match_source_to_local_workspaces,
    merge_global_composer_history,
    rekey_local_workspace_id,
)


def _make_global_storage(root: Path, *, composer_headers: list[tuple], disk_kv: dict[str, str]) -> Path:
    global_dir = root / "globalStorage"
    global_dir.mkdir(parents=True)
    db_path = global_dir / "state.vscdb"
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute(
            "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT, "
            "createdAt INTEGER, lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, "
            "recency INTEGER, checkpointAt INTEGER, value TEXT)"
        )
        con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        con.executemany(
            "INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", composer_headers
        )
        con.executemany(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)", list(disk_kv.items())
        )
        con.commit()
    finally:
        con.close()
    return db_path


def _make_workspace_storage_entry(root: Path, workspace_id: str, meta: dict | None) -> None:
    storage_dir = root / "workspaceStorage" / workspace_id
    storage_dir.mkdir(parents=True)
    if meta is not None:
        (storage_dir / "workspace.json").write_text(json.dumps(meta), encoding="utf-8")


def _header_value(composer_id: str, workspace_id: str) -> str:
    return json.dumps(
        {
            "type": "head",
            "composerId": composer_id,
            "name": "Test session",
            "workspaceIdentifier": {
                "id": workspace_id,
                "uri": {
                    "fsPath": "d:\\projects\\Foo",
                    "external": "file:///d%3A/projects/Foo",
                    "path": "/d:/projects/Foo",
                    "scheme": "file",
                },
            },
        }
    )


class MatchSourceToLocalWorkspacesTest(unittest.TestCase):
    def test_exact_uri_match(self) -> None:
        result = match_source_to_local_workspaces(
            source_by_uri={"file:///d%3A/projects/Foo": ["src-1"]},
            local_by_uri={"file:///d%3A/projects/Foo": "local-1"},
        )
        self.assertEqual(result.matched, {"local-1": ["src-1"]})
        self.assertEqual(result.unmatched_source_uris, ())
        self.assertEqual(result.ambiguous, ())

    def test_basename_fallback_match(self) -> None:
        result = match_source_to_local_workspaces(
            source_by_uri={"file:///d%3A/projects/Foo": ["src-1"]},
            local_by_uri={"file:///Users/me/Documents/projects/Foo": "local-1"},
        )
        self.assertEqual(result.matched, {"local-1": ["src-1"]})

    def test_ambiguous_basename_match(self) -> None:
        result = match_source_to_local_workspaces(
            source_by_uri={"file:///d%3A/projects/Foo": ["src-1"]},
            local_by_uri={
                "file:///Users/me/one/Foo": "local-1",
                "file:///Users/me/two/Foo": "local-2",
            },
        )
        self.assertEqual(result.matched, {})
        self.assertEqual(len(result.ambiguous), 1)
        group = result.ambiguous[0]
        self.assertEqual(group.basename, "Foo")
        self.assertEqual(set(group.local_candidates), {"file:///Users/me/one/Foo", "file:///Users/me/two/Foo"})
        self.assertEqual(group.source_uris, ("file:///d%3A/projects/Foo",))
        self.assertEqual(group.source_ids, ("src-1",))

    def test_ambiguous_group_combines_multiple_source_workspaces_sharing_a_name(self) -> None:
        # Two distinct source workspaces both named "devops" should be grouped
        # together so a single resolution (pick one local folder) merges both.
        result = match_source_to_local_workspaces(
            source_by_uri={
                "file:///d%3A/projects/devops": ["src-1", "src-2"],
                "file:///d%3A/projects-old/devops": ["src-3"],
            },
            local_by_uri={
                "file:///Users/me/one/devops": "local-1",
                "file:///Users/me/two/devops": "local-2",
            },
        )
        self.assertEqual(result.matched, {})
        self.assertEqual(len(result.ambiguous), 1)
        group = result.ambiguous[0]
        self.assertEqual(group.basename, "devops")
        self.assertEqual(set(group.source_uris), {"file:///d%3A/projects/devops", "file:///d%3A/projects-old/devops"})
        self.assertEqual(set(group.source_ids), {"src-1", "src-2", "src-3"})

    def test_unmatched_when_no_local_folder(self) -> None:
        result = match_source_to_local_workspaces(
            source_by_uri={"file:///d%3A/projects/Bar": ["src-1"]},
            local_by_uri={},
        )
        self.assertEqual(result.unmatched_source_uris, ("file:///d%3A/projects/Bar",))


class MergeGlobalComposerHistoryTest(unittest.TestCase):
    def test_merges_headers_composer_data_and_bubbles_with_rewritten_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            source_root = tmp_path / "source"
            dest_root = tmp_path / "dest"

            _make_global_storage(
                source_root,
                composer_headers=[
                    ("composer-1", "src-ws-id", 100, 200, 0, 0, 0, 0, _header_value("composer-1", "src-ws-id")),
                ],
                disk_kv={
                    "composerData:composer-1": json.dumps({"composerId": "composer-1", "text": "hi"}),
                    "bubbleId:composer-1:bubble-a": json.dumps({"bubbleId": "bubble-a", "text": "hello"}),
                    "bubbleId:composer-1:bubble-b": json.dumps({"bubbleId": "bubble-b", "text": "world"}),
                    "bubbleId:other-composer:bubble-x": json.dumps({"bubbleId": "bubble-x"}),
                },
            )
            _make_global_storage(dest_root, composer_headers=[], disk_kv={})

            result = merge_global_composer_history(
                source_cursor_user_dir=source_root,
                dest_cursor_user_dir=dest_root,
                workspace_id_map={"src-ws-id": "local-ws-id"},
                dest_folder_uris={"local-ws-id": "file:///Users/me/Documents/projects/Foo"},
            )

            self.assertEqual(result.headers_inserted, 1)
            self.assertEqual(result.composer_data_inserted, 1)
            self.assertEqual(result.bubbles_inserted, 2)
            self.assertIsNotNone(result.backup_file)

            con = sqlite3.connect((dest_root / "globalStorage" / "state.vscdb").as_posix())
            try:
                cur = con.cursor()
                cur.execute("SELECT workspaceId, value FROM composerHeaders WHERE composerId='composer-1'")
                workspace_id, value = cur.fetchone()
                self.assertEqual(workspace_id, "local-ws-id")
                rewritten = json.loads(value)
                self.assertEqual(rewritten["workspaceIdentifier"]["id"], "local-ws-id")
                self.assertEqual(
                    rewritten["workspaceIdentifier"]["uri"]["external"],
                    "file:///Users/me/Documents/projects/Foo",
                )

                cur.execute("SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'bubbleId:composer-1:%'")
                self.assertEqual(cur.fetchone()[0], 2)
                cur.execute("SELECT COUNT(*) FROM cursorDiskKV WHERE key='bubbleId:other-composer:bubble-x'")
                self.assertEqual(cur.fetchone()[0], 0)
            finally:
                con.close()

    def test_skips_already_present_composer_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            source_root = tmp_path / "source"
            dest_root = tmp_path / "dest"

            _make_global_storage(
                source_root,
                composer_headers=[
                    ("composer-1", "src-ws-id", 100, 200, 0, 0, 0, 0, _header_value("composer-1", "src-ws-id")),
                ],
                disk_kv={"composerData:composer-1": json.dumps({"composerId": "composer-1"})},
            )
            _make_global_storage(
                dest_root,
                composer_headers=[
                    ("composer-1", "local-ws-id", 100, 200, 0, 0, 0, 0, _header_value("composer-1", "local-ws-id")),
                ],
                disk_kv={},
            )

            result = merge_global_composer_history(
                source_cursor_user_dir=source_root,
                dest_cursor_user_dir=dest_root,
                workspace_id_map={"src-ws-id": "local-ws-id"},
                dest_folder_uris={"local-ws-id": "file:///Users/me/Documents/projects/Foo"},
            )

            self.assertEqual(result.headers_inserted, 0)
            self.assertEqual(result.headers_skipped_existing, 1)
            self.assertEqual(result.composer_data_inserted, 0)

    def test_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            source_root = tmp_path / "source"
            dest_root = tmp_path / "dest"

            _make_global_storage(
                source_root,
                composer_headers=[
                    ("composer-1", "src-ws-id", 100, 200, 0, 0, 0, 0, _header_value("composer-1", "src-ws-id")),
                ],
                disk_kv={
                    "composerData:composer-1": json.dumps({"composerId": "composer-1"}),
                    "bubbleId:composer-1:bubble-a": json.dumps({"bubbleId": "bubble-a"}),
                },
            )
            _make_global_storage(dest_root, composer_headers=[], disk_kv={})

            result = merge_global_composer_history(
                source_cursor_user_dir=source_root,
                dest_cursor_user_dir=dest_root,
                workspace_id_map={"src-ws-id": "local-ws-id"},
                dest_folder_uris={"local-ws-id": "file:///Users/me/Documents/projects/Foo"},
                dry_run=True,
            )

            self.assertEqual(result.headers_inserted, 1)
            self.assertEqual(result.bubbles_inserted, 1)
            self.assertIsNone(result.backup_file)

            con = sqlite3.connect((dest_root / "globalStorage" / "state.vscdb").as_posix())
            try:
                cur = con.cursor()
                cur.execute("SELECT COUNT(*) FROM composerHeaders")
                self.assertEqual(cur.fetchone()[0], 0)
            finally:
                con.close()


class RekeyLocalWorkspaceIdTest(unittest.TestCase):
    def test_repoints_sessions_to_new_id_and_rewrites_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cursor_user_dir = Path(tmp).resolve() / "User"
            _make_global_storage(
                cursor_user_dir,
                composer_headers=[
                    ("c1", "old-id", 100, 200, 0, 0, 0, 0, _header_value("c1", "old-id")),
                    ("c2", "old-id", 100, 200, 0, 0, 0, 0, _header_value("c2", "old-id")),
                    ("c3", "unrelated-id", 100, 200, 0, 0, 0, 0, _header_value("c3", "unrelated-id")),
                ],
                disk_kv={"composerData:c1": json.dumps({"composerId": "c1"})},
            )

            result = rekey_local_workspace_id(
                cursor_user_dir=cursor_user_dir,
                old_workspace_id="old-id",
                new_workspace_id="new-id",
                folder_uri="file:///Users/someuser/Documents/projects/Foo",
            )

            self.assertEqual(result.sessions_rekeyed, 2)
            self.assertIsNotNone(result.backup_file)

            con = sqlite3.connect((cursor_user_dir / "globalStorage" / "state.vscdb").as_posix())
            try:
                cur = con.cursor()
                cur.execute("SELECT COUNT(*) FROM composerHeaders WHERE workspaceId='old-id'")
                self.assertEqual(cur.fetchone()[0], 0)

                cur.execute("SELECT composerId, value FROM composerHeaders WHERE workspaceId='new-id'")
                rows = dict(cur.fetchall())
                self.assertEqual(set(rows.keys()), {"c1", "c2"})
                for value in rows.values():
                    payload = json.loads(value)
                    self.assertEqual(payload["workspaceIdentifier"]["id"], "new-id")
                    self.assertEqual(
                        payload["workspaceIdentifier"]["uri"]["external"],
                        "file:///Users/someuser/Documents/projects/Foo",
                    )

                # unrelated session and cursorDiskKV content untouched
                cur.execute("SELECT workspaceId FROM composerHeaders WHERE composerId='c3'")
                self.assertEqual(cur.fetchone()[0], "unrelated-id")
                cur.execute("SELECT COUNT(*) FROM cursorDiskKV")
                self.assertEqual(cur.fetchone()[0], 1)
            finally:
                con.close()

    def test_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cursor_user_dir = Path(tmp).resolve() / "User"
            _make_global_storage(
                cursor_user_dir,
                composer_headers=[
                    ("c1", "old-id", 100, 200, 0, 0, 0, 0, _header_value("c1", "old-id")),
                ],
                disk_kv={},
            )

            result = rekey_local_workspace_id(
                cursor_user_dir=cursor_user_dir,
                old_workspace_id="old-id",
                new_workspace_id="new-id",
                folder_uri="file:///Users/someuser/Documents/projects/Foo",
                dry_run=True,
            )

            self.assertEqual(result.sessions_rekeyed, 1)
            self.assertIsNone(result.backup_file)

            con = sqlite3.connect((cursor_user_dir / "globalStorage" / "state.vscdb").as_posix())
            try:
                cur = con.cursor()
                cur.execute("SELECT COUNT(*) FROM composerHeaders WHERE workspaceId='old-id'")
                self.assertEqual(cur.fetchone()[0], 1)
            finally:
                con.close()


class ListSourceChatCountsTest(unittest.TestCase):
    def test_includes_zero_count_folders_and_sorts_descending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp).resolve() / "source"
            _make_global_storage(
                source_root,
                composer_headers=[
                    ("c1", "busy-id", 100, 100, 0, 0, 0, 0, _header_value("c1", "busy-id")),
                    ("c2", "busy-id", 100, 100, 0, 0, 0, 0, _header_value("c2", "busy-id")),
                ],
                disk_kv={},
            )
            _make_workspace_storage_entry(source_root, "busy-id", {"folder": "file:///d%3A/projects/Busy"})
            _make_workspace_storage_entry(source_root, "quiet-id", {"folder": "file:///d%3A/projects/Quiet"})
            _make_workspace_storage_entry(
                source_root,
                "multiroot-id",
                {"workspace": "file:///c%3A/Users/someuser/AppData/Roaming/Cursor/Workspaces/1/workspace.json"},
            )
            _make_workspace_storage_entry(source_root, "empty-window", {})

            results = list_source_chat_counts(source_root)

            self.assertEqual(len(results), 3)  # empty-window excluded
            self.assertEqual([r.session_count for r in results], [2, 0, 0])
            self.assertEqual([r.real_session_count for r in results], [0, 0, 0])  # no bubbles anywhere
            self.assertEqual(results[0].workspace_id, "busy-id")
            self.assertEqual(results[0].display_path, "d:/projects/Busy")

            multiroot = next(r for r in results if r.workspace_id == "multiroot-id")
            self.assertIsNone(multiroot.folder_uri)
            self.assertIn("multi-root workspace", multiroot.display_path)

    def test_real_session_count_excludes_sessions_with_no_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp).resolve() / "source"
            _make_global_storage(
                source_root,
                composer_headers=[
                    ("c1", "ws-id", 100, 100, 0, 0, 0, 0, _header_value("c1", "ws-id")),
                    ("c2", "ws-id", 100, 100, 0, 0, 0, 0, _header_value("c2", "ws-id")),
                    ("c3", "ws-id", 100, 100, 0, 0, 0, 0, _header_value("c3", "ws-id")),
                ],
                disk_kv={
                    "bubbleId:c1:bubble-a": json.dumps({"bubbleId": "bubble-a"}),
                    "bubbleId:c1:bubble-b": json.dumps({"bubbleId": "bubble-b"}),
                    # c2 has no bubbles at all (empty placeholder); c3 likewise.
                },
            )
            _make_workspace_storage_entry(source_root, "ws-id", {"folder": "file:///d%3A/projects/Foo"})

            results = list_source_chat_counts(source_root)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].session_count, 3)
            self.assertEqual(results[0].real_session_count, 1)

    def test_raises_when_no_global_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                list_source_chat_counts(Path(tmp) / "nonexistent-source")


class FindOrphanedSourceWorkspacesTest(unittest.TestCase):
    def test_finds_multi_root_workspace_with_no_folder_uri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp).resolve() / "source"
            _make_global_storage(
                source_root,
                composer_headers=[
                    ("c1", "multiroot-id", 100, 300, 0, 0, 0, 0, _header_value("c1", "multiroot-id")),
                    ("c2", "multiroot-id", 100, 200, 0, 0, 0, 0, _header_value("c2", "multiroot-id")),
                    ("c3", "plain-folder-id", 100, 100, 0, 0, 0, 0, _header_value("c3", "plain-folder-id")),
                ],
                disk_kv={},
            )
            _make_workspace_storage_entry(
                source_root,
                "multiroot-id",
                {"workspace": "file:///c%3A/Users/me/AppData/Roaming/Cursor/Workspaces/123/workspace.json"},
            )
            _make_workspace_storage_entry(
                source_root, "plain-folder-id", {"folder": "file:///d%3A/projects/Foo"}
            )
            _make_workspace_storage_entry(source_root, "empty-window", {})

            orphaned = find_orphaned_source_workspaces(source_root)

            self.assertEqual(len(orphaned), 1)
            ws = orphaned[0]
            self.assertEqual(ws.workspace_id, "multiroot-id")
            self.assertEqual(ws.session_count, 2)
            self.assertEqual(
                ws.workspace_config_uri,
                "file:///c%3A/Users/me/AppData/Roaming/Cursor/Workspaces/123/workspace.json",
            )
            self.assertEqual(len(ws.sample_session_names), 2)

    def test_no_orphans_when_all_workspaces_have_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp).resolve() / "source"
            _make_global_storage(
                source_root,
                composer_headers=[
                    ("c1", "plain-folder-id", 100, 100, 0, 0, 0, 0, _header_value("c1", "plain-folder-id")),
                ],
                disk_kv={},
            )
            _make_workspace_storage_entry(
                source_root, "plain-folder-id", {"folder": "file:///d%3A/projects/Foo"}
            )

            self.assertEqual(find_orphaned_source_workspaces(source_root), [])


if __name__ == "__main__":
    unittest.main()
