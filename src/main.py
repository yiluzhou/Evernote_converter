"""
CLI Orchestrator — Phase 3 of Evernote-to-OneNote Converter

End-to-end pipeline: scan .enex files -> parse -> convert ENML to HTML ->
upload to OneNote via Microsoft Graph API.

Usage:
  # GUI wizard mode (recommended for most users):
  python src/main.py

  # Advanced mode (for power users):
  python src/main.py --advanced --enex-dir enex/ --notebook-name "Evernote Import" --client-id <YOUR_CLIENT_ID>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from tqdm import tqdm

# Ensure src/ is on the import path when run directly
sys.path.insert(0, os.path.dirname(__file__))

from enex_parser import EvernoteNote, enml_to_html, parse_enex  # noqa: E402
from onenote_uploader import (  # noqa: E402
    AzureSetupGuidanceError,
    OneNoteRequestSizeLimitError,
    OneNoteUploader,
    analyze_note_multipart_limits,
    get_graph_token,
    is_probable_request_size_error,
)

logger = logging.getLogger(__name__)

DEFAULT_NOTEBOOK_NAME = "Evernote Import"
DEFAULT_ENEX_DIR = "enex"
WIZARD_TOTAL_STEPS = 6
_MULTIPART_CHECK_DISPLAY_LIMIT = 8


def main() -> None:
    argv = sys.argv[1:]
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    mode = _determine_mode(args, argv)
    if mode == "gui":
        _run_gui_mode(args)
    elif mode == "terminal_wizard":
        _run_interactive_mode(args)
    else:
        _run_advanced_mode(args)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Evernote .enex files into Microsoft OneNote",
    )
    parser.add_argument(
        "--advanced",
        action="store_true",
        help="Use advanced CLI mode (recommended for power users only)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run terminal wizard (for environments where GUI is unavailable)",
    )
    parser.add_argument(
        "--terminal-wizard",
        action="store_true",
        help="Run terminal wizard (for environments where GUI is unavailable)",
    )
    parser.add_argument(
        "--enex-dir",
        default=None,
        help="Directory containing .enex files (advanced mode)",
    )
    parser.add_argument(
        "--enex-file",
        action="append",
        dest="enex_files",
        help="Specific .enex file in --enex-dir (repeatable; advanced mode)",
    )
    parser.add_argument(
        "--notebook-name",
        default=DEFAULT_NOTEBOOK_NAME,
        help=f"Notebook name (default: '{DEFAULT_NOTEBOOK_NAME}')",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Azure app client ID (or set ENEX_CLIENT_ID env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and convert only — do not upload to OneNote (advanced/terminal modes)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _determine_mode(args: argparse.Namespace, argv: list[str]) -> str:
    if args.interactive or args.terminal_wizard:
        return "terminal_wizard"

    if args.advanced:
        return "advanced"

    advanced_flags = {
        "--enex-dir",
        "--enex-file",
        "--client-id",
        "--notebook-name",
    }
    if any(flag in argv for flag in advanced_flags):
        return "advanced"

    return "gui"


def _run_gui_mode(args: argparse.Namespace) -> None:
    """Run GUI wizard mode for general users."""
    try:
        from gui_wizard import run_gui_wizard
    except Exception as e:
        logger.warning("GUI mode unavailable (%s). Falling back to terminal wizard.", e)
        _run_interactive_mode(args)
        return

    try:
        run_gui_wizard(
            default_enex_dir=Path(args.enex_dir) if args.enex_dir else Path(DEFAULT_ENEX_DIR),
            default_notebook_name=args.notebook_name,
            default_client_id=args.client_id or os.environ.get("ENEX_CLIENT_ID", ""),
            force_dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except Exception as e:
        logger.warning("GUI mode failed at runtime (%s). Falling back to terminal wizard.", e)
        _run_interactive_mode(args)


def _run_interactive_mode(args: argparse.Namespace) -> None:
    _print_banner()

    _print_step(1, "Scan ENEX Folder")
    default_dir = Path(args.enex_dir) if args.enex_dir else Path(DEFAULT_ENEX_DIR)
    enex_dir, enex_files = _prompt_for_enex_folder(default_dir)
    _print_enex_file_catalog(enex_files)

    _print_step(2, "Select ENEX Files")
    selected_files = _prompt_for_enex_selection(enex_files)
    print(f"Selected {len(selected_files)} file(s) from {enex_dir}")

    _print_step(3, "Parse Notes")
    all_sections, total_notes = _parse_selected_enex_files(selected_files)
    _report_multipart_size_risks(all_sections)

    if args.dry_run:
        _run_dry_run_validation(all_sections, total_notes)
        return

    dry_choice = input("Run dry-run only and exit? (y/N): ").strip().lower()
    if dry_choice in {"y", "yes"}:
        _run_dry_run_validation(all_sections, total_notes)
        return

    _print_step(4, "Connect OneNote Account")
    client_id = _resolve_client_id(interactive=True, cli_client_id=args.client_id)
    uploader = _authenticate_uploader(client_id)

    _print_step(5, "Choose Notebook")
    try:
        notebook_id, notebook_name, notebook_url, was_created = _prompt_for_notebook_choice(
            uploader,
            default_new_name=args.notebook_name,
        )
    except AzureSetupGuidanceError as e:
        _print_azure_setup_guidance(
            e,
            "Cannot list/create notebooks due to Azure app configuration.",
        )
        return

    if not was_created and not _confirm_existing_notebook(notebook_name, notebook_url):
        print("Exiting without upload.")
        return

    _print_step(6, "Upload Notes")
    _print_upload_eta(
        uploader,
        total_notes=total_notes,
        section_count=len(all_sections),
    )
    input("Press Enter to start upload...")
    try:
        _upload_sections(uploader, notebook_id, all_sections, total_notes)
    except AzureSetupGuidanceError as e:
        _print_azure_setup_guidance(
            e,
            "Upload aborted due to Azure app configuration.",
        )
        return
    _print_finish(notebook_url)


def _run_advanced_mode(args: argparse.Namespace) -> None:
    if not args.enex_dir:
        print("Error: --enex-dir is required in advanced mode")
        sys.exit(1)

    try:
        enex_files = _discover_enex_files(Path(args.enex_dir))
        selected_files = _resolve_requested_enex_files(enex_files, args.enex_files)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    all_sections, total_notes = _parse_selected_enex_files(selected_files)
    _report_multipart_size_risks(all_sections)

    if args.dry_run:
        _run_dry_run_validation(all_sections, total_notes)
        return

    client_id = _resolve_client_id(interactive=False, cli_client_id=args.client_id)
    uploader = _authenticate_uploader(client_id)

    try:
        notebook_id, was_created = uploader.get_or_create_notebook(args.notebook_name)
    except AzureSetupGuidanceError as e:
        _print_azure_setup_guidance(
            e,
            "Cannot list/create notebooks due to Azure app configuration.",
        )
        return
    notebook_url = _safe_get_notebook_url(uploader, notebook_id)

    if not was_created and not _confirm_existing_notebook(args.notebook_name, notebook_url):
        print("Exiting without upload.")
        return

    _print_upload_eta(
        uploader,
        total_notes=total_notes,
        section_count=len(all_sections),
    )
    try:
        _upload_sections(uploader, notebook_id, all_sections, total_notes)
    except AzureSetupGuidanceError as e:
        _print_azure_setup_guidance(
            e,
            "Upload aborted due to Azure app configuration.",
        )
        return
    _print_finish(notebook_url)


def _resolve_client_id(interactive: bool, cli_client_id: str | None) -> str:
    client_id = cli_client_id or os.environ.get("ENEX_CLIENT_ID")
    if client_id:
        return client_id

    if not interactive:
        print("Error: --client-id or ENEX_CLIENT_ID env var required for upload")
        sys.exit(1)

    while True:
        client_id = input("Enter your Azure Application (client) ID: ").strip()
        if client_id:
            return client_id
        print("Client ID is required.")


def _authenticate_uploader(client_id: str) -> OneNoteUploader:
    print("Authenticating with Microsoft Graph...")
    try:
        token = get_graph_token(client_id)
        return OneNoteUploader(token)
    except AzureSetupGuidanceError as e:
        print("")
        print("Azure setup issue detected:")
        print(e.details)
        print("")
        print("If you still can't resolve it, take a screenshot of Azure and ask ChatGPT with this prompt:")
        print(e.chatgpt_prompt)
        sys.exit(1)
    except Exception as e:
        print(f"Authentication failed: {e}")
        sys.exit(1)


def _print_azure_setup_guidance(error: AzureSetupGuidanceError, context: str) -> None:
    print("")
    print("Azure setup issue detected:")
    print(context)
    print(error.details)
    print("")
    print("If you still can't resolve it, take a screenshot of Azure and ask ChatGPT with this prompt:")
    print(error.chatgpt_prompt)


def _discover_enex_files(enex_dir: Path) -> list[Path]:
    if not enex_dir.is_dir():
        raise ValueError(f"{enex_dir} is not a directory")

    enex_files = sorted(enex_dir.glob("*.enex"))
    if not enex_files:
        raise ValueError(f"No .enex files found in {enex_dir}")
    return enex_files


def _prompt_for_enex_folder(default_dir: Path) -> tuple[Path, list[Path]]:
    while True:
        raw = input(f"Enter ENEX folder path [{default_dir}]: ").strip()
        enex_dir = Path(raw) if raw else default_dir
        try:
            enex_files = _discover_enex_files(enex_dir)
            return enex_dir, enex_files
        except ValueError as e:
            print(f"{e}")


def _print_enex_file_catalog(enex_files: list[Path]) -> None:
    print("\nDiscovered ENEX files:")
    for idx, path in enumerate(enex_files, start=1):
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  {idx:>2}. {path.name} ({size_mb:.2f} MB)")


def _prompt_for_enex_selection(enex_files: list[Path]) -> list[Path]:
    while True:
        raw = input(
            "Select files by number (e.g. 1,3-5) or press Enter for all: "
        ).strip()
        indices = _parse_index_selection(raw, len(enex_files))
        if indices is None:
            print("Invalid selection. Example: 1,3-5")
            continue
        return [enex_files[i - 1] for i in indices]


def _parse_index_selection(raw: str, total_items: int) -> list[int] | None:
    if total_items <= 0:
        return []

    normalized = raw.strip().lower()
    if not normalized or normalized == "all":
        return list(range(1, total_items + 1))

    indices: set[int] = set()
    tokens = [t for t in normalized.replace(" ", "").split(",") if t]
    if not tokens:
        return None

    for token in tokens:
        if "-" in token:
            parts = token.split("-", 1)
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                return None
            start = int(parts[0])
            end = int(parts[1])
            if start > end:
                return None
            if start < 1 or end > total_items:
                return None
            for idx in range(start, end + 1):
                indices.add(idx)
            continue

        if not token.isdigit():
            return None

        idx = int(token)
        if idx < 1 or idx > total_items:
            return None
        indices.add(idx)

    return sorted(indices)


def _resolve_requested_enex_files(
    available_files: list[Path],
    requested_files: list[str] | None,
) -> list[Path]:
    if not requested_files:
        return available_files

    by_name = {p.name: p for p in available_files}
    selected: list[Path] = []
    seen: set[str] = set()
    for requested in requested_files:
        requested = requested.strip()
        if not requested:
            continue

        candidate = requested if requested.lower().endswith(".enex") else f"{requested}.enex"
        match = by_name.get(candidate)
        if match is None:
            raise ValueError(
                f"Requested file '{requested}' not found in ENEX directory"
            )
        if match.name in seen:
            continue
        selected.append(match)
        seen.add(match.name)

    if not selected:
        raise ValueError("No valid .enex files selected")
    return selected


def _parse_selected_enex_files(
    enex_files: list[Path],
) -> tuple[dict[str, list[EvernoteNote]], int]:
    all_sections: dict[str, list[EvernoteNote]] = {}
    for enex_file in enex_files:
        section_name = _allocate_section_name(enex_file.stem, set(all_sections.keys()))
        print(f"\nParsing: {enex_file.name}")
        notes = parse_enex(str(enex_file))
        all_sections[section_name] = notes
        total_resources = sum(len(n.resources) for n in notes)
        total_guided = sum(1 for n in notes if n.guid)
        print(
            f"  Section: {section_name} | "
            f"{len(notes)} notes, {total_resources} resources, {total_guided} with GUID"
        )

    total_notes = sum(len(notes) for notes in all_sections.values())
    print(f"\nTotal: {total_notes} notes across {len(all_sections)} section(s)")
    return all_sections, total_notes


def _allocate_section_name(original_stem: str, used_names: set[str]) -> str:
    base = _sanitize_section_name(original_stem) or "Untitled"
    if base not in used_names:
        return base

    suffix = 2
    while True:
        candidate = f"{base}_{suffix}"
        if candidate not in used_names:
            return candidate
        suffix += 1


def _run_dry_run_validation(
    all_sections: dict[str, list[EvernoteNote]],
    total_notes: int,
) -> None:
    print("\n[DRY RUN] Validating ENML-to-HTML conversion...")
    errors = []
    for notes in all_sections.values():
        for note in notes:
            try:
                resource_map = {r.md5_hash: r for r in note.resources}
                html = enml_to_html(note.content_enml, resource_map)
                if not html.strip():
                    print(f"  WARNING: Empty HTML for note '{note.title}'")
            except Exception as e:
                errors.append((note.title, str(e)))
                print(f"  ERROR converting '{note.title}': {e}")

    if errors:
        print(f"\n{len(errors)} conversion error(s):")
        for title, err in errors:
            print(f"  - {title}: {err}")
    else:
        print(f"\nAll {total_notes} notes converted successfully.")
    print("[DRY RUN] No upload performed.")


def _report_multipart_size_risks(
    all_sections: dict[str, list[EvernoteNote]],
) -> None:
    """
    Pre-check OneNote multipart request-size risks after ENEX load.

    This helps users spot likely upload failures before authentication/upload.
    """
    hard_hits: list[tuple[str, str, str]] = []
    warning_hits: list[tuple[str, str, str]] = []
    checked_notes = 0

    for section_name, notes in all_sections.items():
        for note in notes:
            if not note.resources:
                continue

            try:
                resource_map = {r.md5_hash: r for r in note.resources}
                html_body = enml_to_html(note.content_enml, resource_map)
            except Exception:
                # Conversion issues are reported in dry-run/upload paths.
                continue

            analysis = analyze_note_multipart_limits(note, html_body)
            if not analysis["uses_multipart"]:
                continue
            checked_notes += 1

            title = (note.title or "").strip() or "Untitled"
            if analysis["violations"]:
                hard_hits.append((section_name, title, "; ".join(analysis["violations"])))
            elif analysis["warnings"]:
                warning_hits.append((section_name, title, "; ".join(analysis["warnings"])))

    if checked_notes == 0:
        return

    if hard_hits:
        print(
            "\nWARNING: "
            f"{len(hard_hits)} note(s) exceed OneNote multipart request-size limits "
            "and will be skipped automatically during upload."
        )
        for section_name, title, details in hard_hits[:_MULTIPART_CHECK_DISPLAY_LIMIT]:
            print(f"  - [{section_name}] {title}: {details}")
        if len(hard_hits) > _MULTIPART_CHECK_DISPLAY_LIMIT:
            remaining = len(hard_hits) - _MULTIPART_CHECK_DISPLAY_LIMIT
            print(f"  ... and {remaining} more note(s)")

    if warning_hits:
        print(
            "\nNotice: "
            f"{len(warning_hits)} note(s) are close to OneNote multipart limits "
            "(may fail depending on payload overhead)."
        )
        for section_name, title, details in warning_hits[:_MULTIPART_CHECK_DISPLAY_LIMIT]:
            print(f"  - [{section_name}] {title}: {details}")
        if len(warning_hits) > _MULTIPART_CHECK_DISPLAY_LIMIT:
            remaining = len(warning_hits) - _MULTIPART_CHECK_DISPLAY_LIMIT
            print(f"  ... and {remaining} more note(s)")


def _prompt_for_notebook_choice(
    uploader: OneNoteUploader,
    default_new_name: str,
) -> tuple[str, str, str, bool]:
    notebooks = uploader.list_notebooks()

    if notebooks:
        print("Existing notebooks:")
        for idx, nb in enumerate(notebooks, start=1):
            print(f"  {idx:>2}. {nb.get('displayName', '(untitled)')}")
        print("Type notebook number to use existing, or type a new notebook name.")
    else:
        print("No existing notebooks found. A new notebook will be created.")

    while True:
        raw = input(f"Notebook choice [{default_new_name}]: ").strip()

        if notebooks and raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(notebooks):
                selected = notebooks[idx - 1]
                notebook_id = selected.get("id", "")
                notebook_name = selected.get("displayName", "")
                notebook_url = _extract_web_url(selected)
                return notebook_id, notebook_name, notebook_url, False
            print("Invalid notebook number.")
            continue

        notebook_name = raw or default_new_name
        notebook_id, was_created = uploader.get_or_create_notebook(notebook_name)
        notebook_url = _safe_get_notebook_url(uploader, notebook_id)
        return notebook_id, notebook_name, notebook_url, was_created


def _extract_web_url(item: dict) -> str:
    return (
        item.get("links", {})
        .get("oneNoteWebUrl", {})
        .get("href", "")
    )


def _safe_get_notebook_url(uploader: OneNoteUploader, notebook_id: str) -> str:
    try:
        return uploader.get_notebook_web_url(notebook_id)
    except Exception as e:
        logger.debug("Failed to fetch notebook URL: %s", e)
        return ""


def _confirm_existing_notebook(notebook_name: str, notebook_url: str) -> bool:
    print(f"\nNotebook '{notebook_name}' already exists.")
    if notebook_url:
        print(f"Notebook link: {notebook_url}")
    print("This tool will not delete notebooks or sections.")
    print("If you want a clean rebuild, delete the notebook manually and run again.")
    proceed = input("Continue importing into this notebook? (y/N): ").strip().lower()
    return proceed in {"y", "yes"}


def _format_eta(seconds: float) -> str:
    total = max(int(round(seconds)), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _print_upload_eta(
    uploader: OneNoteUploader,
    total_notes: int,
    section_count: int,
) -> None:
    estimate_seconds = uploader.estimate_upload_duration_seconds(
        expected_page_writes=total_notes,
        expected_section_creates=section_count,
    )
    per_minute, per_hour = uploader.get_write_rate_limits()
    print(
        "Estimated upload time (rate-limited baseline): "
        f"~{_format_eta(estimate_seconds)} "
        f"for {total_notes} notes across {section_count} section(s)."
    )
    print(
        f"Assumes write pacing limits {per_minute}/min and {per_hour}/hour; "
        "duplicates, retries, and network delays can increase actual time."
    )


def _upload_sections(
    uploader: OneNoteUploader,
    notebook_id: str,
    all_sections: dict[str, list[EvernoteNote]],
    total_notes: int,
) -> None:
    errors: list[tuple[str, str]] = []
    uploaded_notes = 0
    skipped_duplicates = 0
    skipped_oversized = 0
    replaced_pages = 0
    duplicate_policy: str | None = None  # replace_all / skip_all

    with tqdm(total=total_notes, desc="Uploading notes") as pbar:
        for section_name, notes in all_sections.items():
            section_id, sec_created = uploader.get_or_create_section(
                notebook_id, section_name
            )
            guid_index: dict[str, list[dict]] = {}
            fallback_index: dict[tuple[str, str], list[dict]] = {}
            if sec_created:
                tqdm.write(f"✓ Created section: {section_name}")
            else:
                tqdm.write(f"✓ Using existing section: {section_name}")
                guid_index, fallback_index, existing_count = uploader.build_section_duplicate_index(
                    section_id, notes
                )
                tqdm.write(
                    f"  Scanned {existing_count} existing page(s) for duplicate detection"
                )

            for note in notes:
                fallback_identity = uploader.page_identity_from_note(note)
                duplicate_pages = _collect_duplicate_pages(
                    note,
                    fallback_identity,
                    guid_index,
                    fallback_index,
                )

                if duplicate_pages:
                    action = duplicate_policy
                    if action is None:
                        action = _prompt_duplicate_action(note, duplicate_pages, uploader)
                        if action == "replace_all":
                            duplicate_policy = "replace_all"
                            action = "replace"
                        elif action == "skip_all":
                            duplicate_policy = "skip_all"
                            action = "skip"
                    elif action == "replace_all":
                        action = "replace"
                    elif action == "skip_all":
                        action = "skip"

                    if action == "skip":
                        skipped_duplicates += 1
                        tqdm.write(f"  SKIP duplicate note: {note.title}")
                        pbar.update(1)
                        continue

                    if action == "replace":
                        delete_failed = False
                        for page in duplicate_pages:
                            page_id = page.get("id", "")
                            if not page_id:
                                continue
                            try:
                                uploader.delete_page(page_id)
                                replaced_pages += 1
                            except AzureSetupGuidanceError:
                                raise
                            except Exception as e:
                                delete_failed = True
                                errors.append(
                                    (
                                        note.title,
                                        f"Failed deleting duplicate page '{page_id}': {e}",
                                    )
                                )
                                tqdm.write(f"  ERROR deleting duplicate page '{page_id}': {e}")

                        if delete_failed:
                            pbar.update(1)
                            continue

                        if note.guid:
                            guid_index[note.guid] = []
                        fallback_index[fallback_identity] = []

                try:
                    resource_map = {r.md5_hash: r for r in note.resources}
                    html_body = enml_to_html(note.content_enml, resource_map)
                    page_id = uploader.create_page(section_id, note, html_body)
                    uploaded_notes += 1

                    entry = {
                        "id": page_id,
                        "title": note.title,
                        "createdDateTime": note.created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "lastModifiedDateTime": "",
                        "webUrl": "",
                        "evernoteGuid": note.guid,
                        "fallbackIdentity": fallback_identity,
                    }
                    if note.guid:
                        guid_index.setdefault(note.guid, []).append(entry)
                    fallback_index.setdefault(fallback_identity, []).append(entry)
                except AzureSetupGuidanceError:
                    raise
                except OneNoteRequestSizeLimitError as e:
                    skipped_oversized += 1
                    tqdm.write(f"  SKIP oversized note: {note.title} ({e})")
                except Exception as e:
                    if is_probable_request_size_error(e):
                        skipped_oversized += 1
                        tqdm.write(f"  SKIP oversized note: {note.title} ({e})")
                    else:
                        errors.append((note.title, str(e)))
                        tqdm.write(f"  ERROR: '{note.title}': {e}")

                pbar.update(1)

    print(f"\nDone! {uploaded_notes}/{total_notes} notes uploaded.")
    if replaced_pages:
        print(f"Replaced {replaced_pages} existing duplicate page(s).")
    if skipped_duplicates:
        print(f"Skipped {skipped_duplicates} duplicate note(s).")
    if skipped_oversized:
        print(f"Skipped {skipped_oversized} oversized note(s).")
    if errors:
        print(f"\n{len(errors)} failed note(s):")
        for title, err in errors:
            print(f"  - {title}: {err}")


def _collect_duplicate_pages(
    note: EvernoteNote,
    fallback_identity: tuple[str, str],
    guid_index: dict[str, list[dict]],
    fallback_index: dict[tuple[str, str], list[dict]],
) -> list[dict]:
    """Collect unique duplicate pages for a note by GUID and fallback identity."""
    candidates: list[dict] = []
    if note.guid:
        candidates.extend(guid_index.get(note.guid, []))
    candidates.extend(fallback_index.get(fallback_identity, []))

    unique_by_id: dict[str, dict] = {}
    for page in candidates:
        page_id = page.get("id", "")
        if page_id:
            unique_by_id[page_id] = page
    return list(unique_by_id.values())


def _prompt_duplicate_action(
    note: EvernoteNote,
    duplicate_pages: list[dict],
    uploader: OneNoteUploader,
) -> str:
    """Prompt user for duplicate handling decision."""
    tqdm.write("")
    tqdm.write("⚠️  Duplicate page detected")
    tqdm.write(f"Incoming note title: {note.title}")
    tqdm.write(f"Incoming note GUID: {note.guid or '(missing)'}")
    tqdm.write(
        f"Incoming created time: {note.created.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    for idx, page in enumerate(duplicate_pages, start=1):
        page_id = page.get("id", "")
        web_url = page.get("webUrl", "")
        if not web_url and page_id:
            try:
                web_url = uploader.get_page_web_url(page_id)
                if web_url:
                    page["webUrl"] = web_url
            except Exception as e:
                logger.debug("Failed to fetch page URL for %s: %s", page_id, e)

        tqdm.write(
            f"  Existing page {idx}: "
            f"id={page_id}, title={page.get('title', '')}, "
            f"created={page.get('createdDateTime', '')}, "
            f"lastModified={page.get('lastModifiedDateTime', '')}"
        )
        if page.get("evernoteGuid"):
            tqdm.write(f"    Embedded GUID: {page.get('evernoteGuid')}")
        if web_url:
            tqdm.write(f"    Link: {web_url}")

    tqdm.write("Choose action:")
    tqdm.write("  1. Replace this note's duplicate page(s)")
    tqdm.write("  2. Replace all duplicates for remaining notes")
    tqdm.write("  3. Skip this note")
    tqdm.write("  4. Skip all duplicates for remaining notes")
    choice = input("Enter choice (1/2/3/4): ").strip()
    return {
        "1": "replace",
        "2": "replace_all",
        "3": "skip",
        "4": "skip_all",
    }.get(choice, "skip")


def _print_banner() -> None:
    print("\n" + "=" * 72)
    print("Evernote to OneNote Import Wizard")
    print("=" * 72)
    print("Follow the steps below. Press Enter after each input.")


def _print_step(step_number: int, title: str) -> None:
    print("\n" + "-" * 72)
    print(f"Step {step_number}/{WIZARD_TOTAL_STEPS}: {title}")
    print("-" * 72)


def _print_finish(notebook_url: str) -> None:
    print("\n" + "=" * 70)
    print("Import Finished")
    print("=" * 70)
    if notebook_url:
        print(f"\nDirect link: {notebook_url}")
        print("\nAlternatively:")
    print("  1. OneDrive: https://onedrive.live.com/ > Documents folder")
    print("  2. OneNote: https://www.onenote.com/notebooks (may take 1-5 min to sync)")
    print("  3. Run: python scripts/check_notebooks.py (lists all notebooks with links)")


def _sanitize_section_name(name: str) -> str:
    """Remove characters forbidden in OneNote section names."""
    forbidden = r'?*\\/:<>|&#"%~'
    for ch in forbidden:
        name = name.replace(ch, "_")
    return name.strip()


if __name__ == "__main__":
    main()
