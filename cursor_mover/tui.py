"""Text UI menu for running CursorMover without CLI arguments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cursor_mover.console import info
from cursor_mover.prompts import prompt_choice, prompt_text, prompt_yes_no


@dataclass(frozen=True, slots=True)
class TuiRunConfig:
    cmd: str
    cursor_user_dir: Path | None
    src: Path | None
    dst: Path | None
    overwrite_dst: bool
    overwrite_workspace_storage: bool
    merge_workspace_storage: bool
    unsafe_db: bool
    assume_yes: bool
    delete_sources: bool
    # Export/Import specific fields
    export_output: Path | None = None
    import_input: Path | None = None
    dry_run: bool = False


def run_tui() -> TuiRunConfig | None:
    """Runs an interactive menu and returns the selected configuration."""
    info("CursorMover")
    print()

    cmd = prompt_choice(
        "What do you want to do?",
        {
            "1": "Copy workspace folder + clone Cursor chats (copy)",
            "2": "Move workspace folder + migrate Cursor chats (move)",
            "3": "Inspect workspace mapping (doctor)",
            "4": "Merge chat state from other workspaceStorage entries (merge)",
            "5": "Export chats to file (for cross-machine sync) [READ-ONLY]",
            "6": "Import chats from file (merge into local workspaces)",
            "0": "Exit",
        },
        default="1",
    )
    if cmd == "0":
        return None

    cursor_user_dir_raw = prompt_text("Cursor User dir (leave empty for auto)", default="")
    cursor_user_dir = Path(cursor_user_dir_raw).resolve() if cursor_user_dir_raw else None

    if cmd == "3":
        path_raw = prompt_text("Workspace folder path", default=str(Path.cwd()))
        return TuiRunConfig(
            cmd="doctor",
            cursor_user_dir=cursor_user_dir,
            src=Path(path_raw).resolve(),
            dst=None,
            overwrite_dst=False,
            overwrite_workspace_storage=False,
            merge_workspace_storage=False,
            unsafe_db=False,
            assume_yes=False,
            delete_sources=False,
        )

    if cmd == "4":
        path_raw = prompt_text("Workspace folder path to merge into", default=str(Path.cwd()))
        assume_yes = prompt_yes_no("Auto-confirm prompts? (--yes)", default=False)
        delete_sources = prompt_yes_no(
            "Delete merged source workspaceStorage folders? (--delete-sources)", default=False
        )
        return TuiRunConfig(
            cmd="merge",
            cursor_user_dir=cursor_user_dir,
            src=Path(path_raw).resolve(),
            dst=None,
            overwrite_dst=False,
            overwrite_workspace_storage=False,
            merge_workspace_storage=False,
            unsafe_db=False,
            assume_yes=assume_yes,
            delete_sources=delete_sources,
        )

    if cmd == "5":
        # Export - READ-ONLY operation
        info("")
        info("EXPORT: This is a READ-ONLY operation - no databases will be modified.")
        info("")
        output_raw = prompt_text(
            "Output file path",
            default=str(Path.cwd() / "cursor_chats_export.json"),
        )
        filter_path_raw = prompt_text(
            "Filter to specific workspace folder (leave empty for all)",
            default="",
        )
        unsafe_db = prompt_yes_no(
            "Skip lock checks? (may export inconsistent data)", default=False
        )
        return TuiRunConfig(
            cmd="export",
            cursor_user_dir=cursor_user_dir,
            src=Path(filter_path_raw).resolve() if filter_path_raw else None,
            dst=None,
            overwrite_dst=False,
            overwrite_workspace_storage=False,
            merge_workspace_storage=False,
            unsafe_db=unsafe_db,
            assume_yes=False,
            delete_sources=False,
            export_output=Path(output_raw).resolve(),
        )

    if cmd == "6":
        # Import - writes to local DBs with backup
        info("")
        info("IMPORT: A timestamped backup will be created before any database is modified.")
        info("")
        input_raw = prompt_text("Input file path (export JSON)")
        filter_path_raw = prompt_text(
            "Filter to specific workspace folder (leave empty for all)",
            default="",
        )
        dry_run = prompt_yes_no("Dry run only? (show what would be imported)", default=False)
        assume_yes = prompt_yes_no("Auto-confirm prompts? (--yes)", default=False)
        return TuiRunConfig(
            cmd="import",
            cursor_user_dir=cursor_user_dir,
            src=Path(filter_path_raw).resolve() if filter_path_raw else None,
            dst=None,
            overwrite_dst=False,
            overwrite_workspace_storage=False,
            merge_workspace_storage=False,
            unsafe_db=False,
            assume_yes=assume_yes,
            delete_sources=False,
            import_input=Path(input_raw).resolve(),
            dry_run=dry_run,
        )

    src_raw = prompt_text("Source folder", default=str(Path.cwd()))
    dst_raw = prompt_text("Destination folder")

    overwrite_dst = prompt_yes_no("Overwrite destination folder if it exists?", default=False)
    ws_action = prompt_choice(
        "If destination workspaceStorage/<id> already exists, what should happen?",
        {
            "a": "Abort (do not touch existing destination workspaceStorage)",
            "m": "Merge source chats into existing destination (preserve destination chats)",
            "o": "Overwrite destination workspaceStorage (delete and replace)",
        },
        default="a",
    )
    unsafe_db = prompt_yes_no(
        "Proceed even if Cursor DB appears locked? (UnsafeDB)", default=False
    )

    return TuiRunConfig(
        cmd="copy" if cmd == "1" else "move",
        cursor_user_dir=cursor_user_dir,
        src=Path(src_raw).resolve(),
        dst=Path(dst_raw).resolve(),
        overwrite_dst=overwrite_dst,
        overwrite_workspace_storage=ws_action == "o",
        merge_workspace_storage=ws_action == "m",
        unsafe_db=unsafe_db,
        assume_yes=False,
        delete_sources=False,
    )

