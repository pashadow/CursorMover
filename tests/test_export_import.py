"""Unit tests for cross-machine export/import matching."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cursor_mover.export_import import import_workspace_chats
from cursor_mover.workspace_uri import folder_uri_basename, path_to_folder_uri


def _make_workspace_storage(root: Path, workspace_id: str, folder_uri: str) -> Path:
    storage_dir = root / "workspaceStorage" / workspace_id
    storage_dir.mkdir(parents=True)
    (storage_dir / "workspace.json").write_text(
        json.dumps({"folder": folder_uri}), encoding="utf-8"
    )
    con = sqlite3.connect((storage_dir / "state.vscdb").as_posix())
    try:
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        con.commit()
    finally:
        con.close()
    return storage_dir


def _export_document(*, workspaces: list[dict]) -> dict:
    return {
        "format_version": 1,
        "source_machine": "WINDOWS-PC",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "workspaces": workspaces,
    }


class WorkspaceUriBasenameTest(unittest.TestCase):
    def test_basename_matches_across_platforms(self) -> None:
        windows_uri = "file:///c%3A/Users/pasha/Projects/Foo"
        posix_uri = "file:///Users/pavlo/Documents/projects/Foo"
        self.assertEqual(folder_uri_basename(windows_uri), "Foo")
        self.assertEqual(folder_uri_basename(posix_uri), "Foo")


class ImportCrossMachineMatchTest(unittest.TestCase):
    def test_import_matches_by_folder_name_when_path_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            cursor_user_dir = tmp_path / "Cursor" / "User"

            local_folder = tmp_path / "mac" / "Foo"
            local_folder.mkdir(parents=True)
            local_uri = path_to_folder_uri(local_folder)
            _make_workspace_storage(cursor_user_dir, "local-id", local_uri)

            windows_uri = "file:///c%3A/Users/pasha/Projects/Foo"
            export_path = tmp_path / "export.json"
            export_path.write_text(
                json.dumps(
                    _export_document(
                        workspaces=[
                            {
                                "folder_uri": windows_uri,
                                "workspace_config_uri": None,
                                "itemtable_entries": {
                                    "some.key": __import__("base64")
                                    .b64encode(b"value")
                                    .decode("ascii")
                                },
                                "cursordiskkv_entries": {},
                                "composer_data_raw": None,
                                "export_timestamp": "2026-01-01T00:00:00+00:00",
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            result = import_workspace_chats(
                input_path=export_path,
                cursor_user_dir=cursor_user_dir,
            )

            self.assertEqual(result.workspaces_updated, 1)
            self.assertEqual(result.workspaces_skipped, 0)
            self.assertEqual(result.itemtable_keys_inserted, 1)
            self.assertEqual(len(result.matched_by_name), 1)
            self.assertEqual(result.matched_by_name[0], (windows_uri, local_uri))

    def test_import_skips_ambiguous_folder_name_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            cursor_user_dir = tmp_path / "Cursor" / "User"

            folder_a = tmp_path / "one" / "Foo"
            folder_a.mkdir(parents=True)
            uri_a = path_to_folder_uri(folder_a)
            _make_workspace_storage(cursor_user_dir, "id-a", uri_a)

            folder_b = tmp_path / "two" / "Foo"
            folder_b.mkdir(parents=True)
            uri_b = path_to_folder_uri(folder_b)
            _make_workspace_storage(cursor_user_dir, "id-b", uri_b)

            windows_uri = "file:///c%3A/Users/pasha/Projects/Foo"
            export_path = tmp_path / "export.json"
            export_path.write_text(
                json.dumps(
                    _export_document(
                        workspaces=[
                            {
                                "folder_uri": windows_uri,
                                "workspace_config_uri": None,
                                "itemtable_entries": {},
                                "cursordiskkv_entries": {},
                                "composer_data_raw": None,
                                "export_timestamp": "2026-01-01T00:00:00+00:00",
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            result = import_workspace_chats(
                input_path=export_path,
                cursor_user_dir=cursor_user_dir,
            )

            self.assertEqual(result.workspaces_updated, 0)
            self.assertEqual(result.workspaces_skipped, 1)
            self.assertEqual(len(result.ambiguous_name_matches), 1)
            basename, candidates = result.ambiguous_name_matches[0]
            self.assertEqual(basename, "Foo")
            self.assertEqual(set(candidates), {uri_a, uri_b})

    def test_import_prefers_exact_uri_match_over_name_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            cursor_user_dir = tmp_path / "Cursor" / "User"

            local_folder = tmp_path / "Foo"
            local_folder.mkdir(parents=True)
            local_uri = path_to_folder_uri(local_folder)
            _make_workspace_storage(cursor_user_dir, "local-id", local_uri)

            export_path = tmp_path / "export.json"
            export_path.write_text(
                json.dumps(
                    _export_document(
                        workspaces=[
                            {
                                "folder_uri": local_uri,
                                "workspace_config_uri": None,
                                "itemtable_entries": {},
                                "cursordiskkv_entries": {},
                                "composer_data_raw": None,
                                "export_timestamp": "2026-01-01T00:00:00+00:00",
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            result = import_workspace_chats(
                input_path=export_path,
                cursor_user_dir=cursor_user_dir,
            )

            self.assertEqual(result.workspaces_updated, 1)
            self.assertEqual(len(result.matched_by_name), 0)


if __name__ == "__main__":
    unittest.main()
