"""GUI wizard flow for general users (Tkinter)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from enex_parser import EvernoteNote, enml_to_html, parse_enex
from onenote_uploader import AzureSetupGuidanceError, OneNoteUploader, get_graph_token
from update_checker import APP_VERSION, get_latest_release_update

logger = logging.getLogger(__name__)

INTRO_GUIDE_TEXT = """\
This wizard is designed for non-technical users.

Before continuing:
1. Export notes from Evernote to .enex files:
   - Evernote desktop -> right-click notebook -> Export notebook...
   - Save exported .enex files anywhere on your computer
2. In this wizard you will:
   - Pick .enex files from a GUI window
   - Connect your OneNote account
   - Choose an existing notebook or create a new one
   - Upload notes with guided duplicate handling

You do not need command-line knowledge.
"""

AZURE_SETUP_GUIDE_TEXT = """\
Azure App Setup (one-time):

1. Open https://portal.azure.com and sign in with your Microsoft account
2. Search for "Microsoft Entra ID"
3. App registrations -> New registration
4. Name: Evernote Converter (or any name)
5. Supported account types: Personal Microsoft accounts only
6. Register
7. Copy "Application (client) ID"
8. API permissions -> Add a permission:
   - Microsoft Graph -> Delegated permissions
   - Add: Notes.Create and Notes.ReadWrite
9. Authentication:
   - Allow public client flows = Yes
   - Save

Paste the Application (client) ID below to continue.
"""

_UPDATE_CHECK_DISABLE_VALUES = {"1", "true", "yes", "on"}


def run_gui_wizard(
    default_enex_dir: Path,
    default_notebook_name: str,
    default_client_id: str,
    force_dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Run the full GUI wizard flow."""
    root = tk.Tk()
    root.withdraw()

    try:
        _maybe_alert_update_available(root)

        if not _show_intro_dialog(root):
            return

        selected_files = _select_enex_files(root, default_enex_dir)
        if not selected_files:
            return

        all_sections, total_notes = _parse_selected_enex_files(selected_files)
        summary_lines = [
            f"Selected files: {len(selected_files)}",
            f"Sections: {len(all_sections)}",
            f"Total notes: {total_notes}",
        ]
        if not messagebox.askokcancel(
            "Parsed Summary",
            "\n".join(summary_lines) + "\n\nProceed?",
            parent=root,
        ):
            return

        dry_run = force_dry_run or messagebox.askyesno(
            "Dry Run",
            "Run dry-run validation only (no upload)?",
            parent=root,
        )
        if dry_run:
            dry_result = _run_dry_run_validation(all_sections, total_notes)
            if dry_result:
                messagebox.showwarning(
                    "Dry Run Complete",
                    f"Found {len(dry_result)} conversion error(s). See terminal output for details.",
                    parent=root,
                )
            else:
                messagebox.showinfo(
                    "Dry Run Complete",
                    f"All {total_notes} notes converted successfully. No upload performed.",
                    parent=root,
                )
            return

        client_id = _show_azure_setup_dialog(root, default_client_id)
        if not client_id:
            return

        uploader = _authenticate_with_busy_dialog(root, client_id)
        if uploader is None:
            return

        notebook_selection = _choose_notebook(root, uploader, default_notebook_name)
        if notebook_selection is None:
            return
        notebook_id, notebook_name, notebook_url, was_created = notebook_selection

        if not was_created:
            prompt = [
                f"Notebook '{notebook_name}' already exists.",
                "This tool will not delete notebooks/sections.",
                "If you want a clean rebuild, delete it manually first.",
            ]
            if notebook_url:
                prompt.append("")
                prompt.append(f"Link: {notebook_url}")
            if not messagebox.askokcancel(
                "Use Existing Notebook",
                "\n".join(prompt),
                parent=root,
            ):
                return

        if not messagebox.askokcancel(
            "Ready to Upload",
            "Start upload now?",
            parent=root,
        ):
            return

        upload_summary = _upload_with_progress_window(
            root,
            uploader,
            notebook_id,
            all_sections,
            total_notes,
        )
        if upload_summary.get("aborted"):
            return

        finish_lines = [
            f"Uploaded: {upload_summary['uploaded_notes']}/{total_notes}",
            f"Replaced duplicate pages: {upload_summary['replaced_pages']}",
            f"Skipped duplicates: {upload_summary['skipped_duplicates']}",
            f"Errors: {upload_summary['errors_count']}",
        ]
        if notebook_url:
            finish_lines.append("")
            finish_lines.append(f"Notebook link: {notebook_url}")

        if upload_summary["errors_count"] > 0:
            messagebox.showwarning(
                "Upload Finished with Errors",
                "\n".join(finish_lines),
                parent=root,
            )
        else:
            messagebox.showinfo(
                "Upload Finished",
                "\n".join(finish_lines),
                parent=root,
            )

    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _maybe_alert_update_available(root: tk.Tk) -> None:
    """Show a startup prompt if a newer release is available."""
    disable_value = os.environ.get("ENEX_DISABLE_UPDATE_CHECK", "").strip().lower()
    if disable_value in _UPDATE_CHECK_DISABLE_VALUES:
        return

    update = get_latest_release_update(current_version=APP_VERSION)
    if not update:
        return

    latest_version = update["latest_version"]
    repo_url = update.get("repo_url") or update.get("download_url", "")
    prompt = (
        "A newer version is available.\n\n"
        f"Current version: {APP_VERSION}\n"
        f"Latest version: {latest_version}\n\n"
        "Open repository page now?"
    )
    if messagebox.askyesno("Update Available", prompt, parent=root):
        try:
            webbrowser.open(repo_url)
        except Exception as e:
            logger.debug("Failed to open update link: %s", e)
            messagebox.showinfo("Repository Link", repo_url, parent=root)


def _select_enex_files(root: tk.Tk, default_enex_dir: Path) -> list[Path] | None:
    initial_dir = str(default_enex_dir if default_enex_dir.exists() else Path.cwd())

    while True:
        folder = filedialog.askdirectory(
            title="Select folder containing .enex files",
            initialdir=initial_dir,
            parent=root,
        )
        if not folder:
            return None

        enex_dir = Path(folder)
        enex_files = sorted(enex_dir.glob("*.enex"))
        if not enex_files:
            retry = messagebox.askretrycancel(
                "No ENEX Files",
                f"No .enex files found in:\n{enex_dir}",
                parent=root,
            )
            if retry:
                initial_dir = str(enex_dir)
                continue
            return None

        selected = _show_enex_selection_dialog(root, enex_files)
        if selected is not None and selected:
            return selected
        if selected == []:
            messagebox.showinfo(
                "Selection Required",
                "Please select at least one .enex file.",
                parent=root,
            )
            continue
        return None


def _show_enex_selection_dialog(root: tk.Tk, enex_files: list[Path]) -> list[Path] | None:
    dialog = tk.Toplevel(root)
    dialog.title("Select ENEX Files")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("780x420")

    tk.Label(
        dialog,
        text="Select one or more .enex files to upload",
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    frame = tk.Frame(dialog)
    frame.pack(fill="both", expand=True, padx=12, pady=6)

    listbox = tk.Listbox(frame, selectmode=tk.EXTENDED)
    yscroll = tk.Scrollbar(frame, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=yscroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    for idx, path in enumerate(enex_files, start=1):
        size_mb = path.stat().st_size / (1024 * 1024)
        listbox.insert(tk.END, f"{idx:>2}. {path.name} ({size_mb:.2f} MB)")

    listbox.selection_set(0, tk.END)

    result: dict[str, list[Path] | None] = {"value": None}

    def _select_all() -> None:
        listbox.selection_set(0, tk.END)

    def _clear_all() -> None:
        listbox.selection_clear(0, tk.END)

    def _ok() -> None:
        selected_indices = listbox.curselection()
        result["value"] = [enex_files[i] for i in selected_indices]
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    button_bar = tk.Frame(dialog)
    button_bar.pack(fill="x", padx=12, pady=(6, 12))

    tk.Button(button_bar, text="Select All", command=_select_all).pack(side="left")
    tk.Button(button_bar, text="Clear", command=_clear_all).pack(side="left", padx=6)
    tk.Button(button_bar, text="Cancel", command=_cancel).pack(side="right")
    tk.Button(button_bar, text="Next", command=_ok).pack(side="right", padx=6)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _show_intro_dialog(root: tk.Tk) -> bool:
    dialog = tk.Toplevel(root)
    dialog.title("Welcome")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("780x420")

    tk.Label(
        dialog,
        text="Evernote to OneNote Wizard",
        font=("Segoe UI", 14, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    body = tk.Text(dialog, wrap="word")
    yscroll = tk.Scrollbar(dialog, orient="vertical", command=body.yview)
    body.configure(yscrollcommand=yscroll.set)
    body.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
    yscroll.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))
    body.insert(tk.END, INTRO_GUIDE_TEXT)
    body.configure(state="disabled")

    result = {"value": False}

    def _continue() -> None:
        result["value"] = True
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = False
        dialog.destroy()

    button_bar = tk.Frame(dialog)
    button_bar.place(relx=0.0, rely=1.0, relwidth=1.0, anchor="sw")
    tk.Button(button_bar, text="Cancel", command=_cancel).pack(side="right", padx=12, pady=10)
    tk.Button(button_bar, text="Continue", command=_continue).pack(side="right", padx=6, pady=10)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return bool(result["value"])


def _show_azure_setup_dialog(root: tk.Tk, default_client_id: str) -> str | None:
    dialog = tk.Toplevel(root)
    dialog.title("Azure App Setup")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("900x620")

    tk.Label(
        dialog,
        text="One-time setup: Register Azure app and paste Client ID",
        font=("Segoe UI", 12, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    guide = tk.Text(dialog, wrap="word")
    yscroll = tk.Scrollbar(dialog, orient="vertical", command=guide.yview)
    guide.configure(yscrollcommand=yscroll.set)
    guide.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
    yscroll.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))
    guide.insert(tk.END, AZURE_SETUP_GUIDE_TEXT)
    guide.configure(state="disabled")

    entry_frame = tk.Frame(dialog)
    entry_frame.place(relx=0.0, rely=1.0, relwidth=1.0, anchor="sw")

    tk.Label(entry_frame, text="Application (client) ID:").pack(
        side="left", padx=(12, 6), pady=10
    )
    client_id_var = tk.StringVar(value=default_client_id)
    tk.Entry(entry_frame, textvariable=client_id_var, width=56).pack(
        side="left", fill="x", expand=True, pady=10
    )

    result = {"value": None}

    def _open_portal() -> None:
        webbrowser.open("https://portal.azure.com")

    def _copy_steps() -> None:
        root.clipboard_clear()
        root.clipboard_append(AZURE_SETUP_GUIDE_TEXT)
        root.update_idletasks()
        messagebox.showinfo("Copied", "Azure setup steps copied to clipboard.", parent=dialog)

    def _continue() -> None:
        client_id = client_id_var.get().strip()
        if not client_id:
            messagebox.showwarning(
                "Client ID Required",
                "Please paste Application (client) ID before continuing.",
                parent=dialog,
            )
            return
        result["value"] = client_id
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    tk.Button(entry_frame, text="Open Azure Portal", command=_open_portal).pack(
        side="left", padx=(8, 0), pady=10
    )
    tk.Button(entry_frame, text="Copy Steps", command=_copy_steps).pack(
        side="left", padx=(6, 0), pady=10
    )
    tk.Button(entry_frame, text="Cancel", command=_cancel).pack(
        side="right", padx=(6, 12), pady=10
    )
    tk.Button(entry_frame, text="Continue", command=_continue).pack(
        side="right", pady=10
    )

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    value = result["value"]
    if value is None:
        return None
    return str(value)


def _authenticate_with_busy_dialog(root: tk.Tk, client_id: str) -> OneNoteUploader | None:
    dialog = tk.Toplevel(root)
    dialog.title("Connecting")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("420x120")

    tk.Label(
        dialog,
        text="Authenticating with Microsoft Graph...\nA browser/device code prompt may appear.",
        justify="left",
    ).pack(fill="both", expand=True, padx=12, pady=12)

    root.update_idletasks()
    try:
        token = get_graph_token(client_id)
        uploader = OneNoteUploader(token)
    except AzureSetupGuidanceError as e:
        dialog.destroy()
        _show_azure_guidance_dialog(
            root,
            e,
            "Authentication failed while connecting to Microsoft Graph.",
        )
        return None
    except Exception as e:
        dialog.destroy()
        messagebox.showerror("Authentication Failed", str(e), parent=root)
        return None

    dialog.destroy()
    return uploader


def _choose_notebook(
    root: tk.Tk,
    uploader: OneNoteUploader,
    default_notebook_name: str,
) -> tuple[str, str, str, bool] | None:
    try:
        notebooks = uploader.list_notebooks()
    except AzureSetupGuidanceError as e:
        _show_azure_guidance_dialog(root, e, "Cannot list notebooks from your account.")
        return None
    except Exception as e:
        messagebox.showerror("Notebook Listing Failed", str(e), parent=root)
        return None

    dialog = tk.Toplevel(root)
    dialog.title("Choose Notebook")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("780x480")

    tk.Label(
        dialog,
        text="Select an existing notebook or enter a new notebook name",
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    frame = tk.Frame(dialog)
    frame.pack(fill="both", expand=True, padx=12, pady=6)

    listbox = tk.Listbox(frame)
    yscroll = tk.Scrollbar(frame, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=yscroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    for idx, nb in enumerate(notebooks, start=1):
        name = nb.get("displayName", "(untitled)")
        listbox.insert(tk.END, f"{idx:>2}. {name}")

    if notebooks:
        listbox.selection_set(0)

    entry_frame = tk.Frame(dialog)
    entry_frame.pack(fill="x", padx=12, pady=6)
    tk.Label(entry_frame, text="New notebook name:").pack(side="left")
    name_var = tk.StringVar(value=default_notebook_name)
    tk.Entry(entry_frame, textvariable=name_var).pack(side="left", fill="x", expand=True, padx=(8, 0))

    result: dict[str, tuple[str, str, str, bool] | None] = {"value": None}

    def _use_selected() -> None:
        selection = listbox.curselection()
        if not selection:
            messagebox.showinfo(
                "No Notebook Selected",
                "Select a notebook from the list, or use 'Create New'.",
                parent=dialog,
            )
            return
        nb = notebooks[selection[0]]
        notebook_id = nb.get("id", "")
        notebook_name = nb.get("displayName", "")
        notebook_url = _extract_web_url(nb)
        result["value"] = (notebook_id, notebook_name, notebook_url, False)
        dialog.destroy()

    def _create_new() -> None:
        notebook_name = name_var.get().strip() or default_notebook_name
        try:
            notebook_id, was_created = uploader.get_or_create_notebook(notebook_name)
            notebook_url = _safe_get_notebook_url(uploader, notebook_id)
        except AzureSetupGuidanceError as e:
            _show_azure_guidance_dialog(
                dialog,
                e,
                "Cannot create notebook due to Azure app configuration.",
            )
            return
        except Exception as e:
            messagebox.showerror("Notebook Error", str(e), parent=dialog)
            return
        result["value"] = (notebook_id, notebook_name, notebook_url, was_created)
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    button_bar = tk.Frame(dialog)
    button_bar.pack(fill="x", padx=12, pady=(6, 12))

    tk.Button(button_bar, text="Cancel", command=_cancel).pack(side="right")
    tk.Button(button_bar, text="Create New", command=_create_new).pack(side="right", padx=6)
    tk.Button(button_bar, text="Use Selected", command=_use_selected).pack(side="right", padx=6)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _upload_with_progress_window(
    root: tk.Tk,
    uploader: OneNoteUploader,
    notebook_id: str,
    all_sections: dict[str, list[EvernoteNote]],
    total_notes: int,
) -> dict[str, int | bool]:
    progress = tk.Toplevel(root)
    progress.title("Uploading")
    progress.transient(root)
    progress.grab_set()
    progress.geometry("880x520")

    status_var = tk.StringVar(value="Starting upload...")
    tk.Label(progress, textvariable=status_var, anchor="w").pack(fill="x", padx=12, pady=(12, 6))

    progress_var = tk.DoubleVar(value=0)
    bar = ttk.Progressbar(progress, maximum=max(total_notes, 1), variable=progress_var)
    bar.pack(fill="x", padx=12, pady=6)

    text = tk.Text(progress, height=22, wrap="word")
    yscroll = tk.Scrollbar(progress, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=yscroll.set)
    text.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(6, 12))
    yscroll.pack(side="right", fill="y", padx=(0, 12), pady=(6, 12))

    def log_line(line: str) -> None:
        text.insert(tk.END, line + "\n")
        text.see(tk.END)
        progress.update_idletasks()

    errors: list[tuple[str, str]] = []
    uploaded_notes = 0
    skipped_duplicates = 0
    replaced_pages = 0
    duplicate_policy: str | None = None
    current_done = 0

    def abort_with_guidance(error: AzureSetupGuidanceError, context: str) -> dict[str, int | bool]:
        try:
            progress.destroy()
        except Exception:
            pass
        _show_azure_guidance_dialog(root, error, context)
        return {
            "uploaded_notes": uploaded_notes,
            "skipped_duplicates": skipped_duplicates,
            "replaced_pages": replaced_pages,
            "errors_count": len(errors) + 1,
            "aborted": True,
        }

    for section_name, notes in all_sections.items():
        status_var.set(f"Section: {section_name}")
        progress.update_idletasks()

        try:
            section_id, sec_created = uploader.get_or_create_section(notebook_id, section_name)
        except AzureSetupGuidanceError as e:
            return abort_with_guidance(
                e,
                "Cannot list/create sections. Azure app permissions may be incomplete.",
            )
        guid_index: dict[str, list[dict]] = {}
        fallback_index: dict[tuple[str, str], list[dict]] = {}

        if sec_created:
            log_line(f"Created section: {section_name}")
        else:
            log_line(f"Using existing section: {section_name}")
            try:
                guid_index, fallback_index, existing_count = uploader.build_section_duplicate_index(
                    section_id, notes
                )
            except AzureSetupGuidanceError as e:
                return abort_with_guidance(
                    e,
                    "Cannot scan existing pages for duplicates. Azure app permissions may be incomplete.",
                )
            log_line(f"  Scanned {existing_count} existing page(s) for duplicate detection")

        for note in notes:
            status_var.set(f"Uploading note: {note.title}")
            progress.update_idletasks()

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
                    action = _prompt_duplicate_action_gui(root, note, duplicate_pages, uploader)
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
                    log_line(f"SKIP duplicate note: {note.title}")
                    current_done += 1
                    progress_var.set(current_done)
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
                        except AzureSetupGuidanceError as e:
                            return abort_with_guidance(
                                e,
                                "Cannot replace duplicate pages. Azure app permissions may be incomplete.",
                            )
                        except Exception as e:
                            delete_failed = True
                            errors.append((note.title, f"Failed deleting page '{page_id}': {e}"))
                            log_line(f"ERROR deleting duplicate page '{page_id}': {e}")

                    if delete_failed:
                        current_done += 1
                        progress_var.set(current_done)
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
            except AzureSetupGuidanceError as e:
                return abort_with_guidance(
                    e,
                    "Cannot create page in OneNote. Azure app permissions may be incomplete.",
                )
            except Exception as e:
                errors.append((note.title, str(e)))
                log_line(f"ERROR: '{note.title}': {e}")

            current_done += 1
            progress_var.set(current_done)
            progress.update_idletasks()

    status_var.set("Upload complete")
    log_line("Upload complete.")
    progress.update_idletasks()
    progress.destroy()

    return {
        "uploaded_notes": uploaded_notes,
        "skipped_duplicates": skipped_duplicates,
        "replaced_pages": replaced_pages,
        "errors_count": len(errors),
        "aborted": False,
    }


def _show_azure_guidance_dialog(
    parent: tk.Misc,
    error: AzureSetupGuidanceError,
    context: str,
) -> None:
    dialog = tk.Toplevel(parent)
    dialog.title("Azure Setup Issue")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.geometry("900x620")

    tk.Label(
        dialog,
        text=error.title,
        font=("Segoe UI", 12, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    body_text = (
        f"{context}\n\n"
        f"{error.details}\n\n"
        "If still stuck:\n"
        "1. Open Azure Portal and verify each setup step.\n"
        "2. Take a screenshot of the Azure page.\n"
        "3. Ask ChatGPT using the prompt below.\n\n"
        "ChatGPT prompt:\n"
        f"{error.chatgpt_prompt}"
    )

    text = tk.Text(dialog, wrap="word")
    yscroll = tk.Scrollbar(dialog, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=yscroll.set)
    text.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
    yscroll.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))
    text.insert(tk.END, body_text)
    text.configure(state="disabled")

    def _open_portal() -> None:
        webbrowser.open("https://portal.azure.com")

    def _copy_prompt() -> None:
        prompt = error.chatgpt_prompt.strip()
        dialog.clipboard_clear()
        dialog.clipboard_append(prompt)
        dialog.update_idletasks()
        messagebox.showinfo(
            "Copied",
            "ChatGPT prompt copied to clipboard.",
            parent=dialog,
        )

    button_bar = tk.Frame(dialog)
    button_bar.pack(fill="x", padx=12, pady=(0, 12))

    tk.Button(button_bar, text="Open Azure Portal", command=_open_portal).pack(side="left")
    tk.Button(button_bar, text="Copy ChatGPT Prompt", command=_copy_prompt).pack(
        side="left",
        padx=6,
    )
    tk.Button(button_bar, text="Close", command=dialog.destroy).pack(side="right")

    dialog.wait_window()


def _prompt_duplicate_action_gui(
    root: tk.Tk,
    note: EvernoteNote,
    duplicate_pages: list[dict],
    uploader: OneNoteUploader,
) -> str:
    dialog = tk.Toplevel(root)
    dialog.title("Duplicate Detected")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("900x520")

    header_lines = [
        "Incoming note:",
        f"  Title: {note.title}",
        f"  GUID: {note.guid or '(missing)'}",
        f"  Created: {note.created.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "Matching existing page(s):",
    ]

    body = tk.Text(dialog, wrap="word")
    body.pack(fill="both", expand=True, padx=12, pady=(12, 6))

    for line in header_lines:
        body.insert(tk.END, line + "\n")

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

        body.insert(
            tk.END,
            (
                f"\n{idx}. id={page_id}\n"
                f"   title={page.get('title', '')}\n"
                f"   created={page.get('createdDateTime', '')}\n"
                f"   lastModified={page.get('lastModifiedDateTime', '')}\n"
            ),
        )
        if page.get("evernoteGuid"):
            body.insert(tk.END, f"   embeddedGuid={page.get('evernoteGuid')}\n")
        if web_url:
            body.insert(tk.END, f"   link={web_url}\n")

    body.configure(state="disabled")

    result = {"value": "skip"}

    def choose(action: str) -> None:
        result["value"] = action
        dialog.destroy()

    button_bar = tk.Frame(dialog)
    button_bar.pack(fill="x", padx=12, pady=(0, 12))

    tk.Button(button_bar, text="Replace", command=lambda: choose("replace")).pack(side="left")
    tk.Button(button_bar, text="Replace All", command=lambda: choose("replace_all")).pack(
        side="left", padx=6
    )
    tk.Button(button_bar, text="Skip", command=lambda: choose("skip")).pack(side="right")
    tk.Button(button_bar, text="Skip All", command=lambda: choose("skip_all")).pack(
        side="right", padx=6
    )

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("skip"))
    dialog.wait_window()
    return result["value"]


def _collect_duplicate_pages(
    note: EvernoteNote,
    fallback_identity: tuple[str, str],
    guid_index: dict[str, list[dict]],
    fallback_index: dict[tuple[str, str], list[dict]],
) -> list[dict]:
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


def _parse_selected_enex_files(
    enex_files: list[Path],
) -> tuple[dict[str, list[EvernoteNote]], int]:
    all_sections: dict[str, list[EvernoteNote]] = {}
    for enex_file in enex_files:
        section_name = _allocate_section_name(enex_file.stem, set(all_sections.keys()))
        notes = parse_enex(str(enex_file))
        all_sections[section_name] = notes

    total_notes = sum(len(notes) for notes in all_sections.values())
    return all_sections, total_notes


def _run_dry_run_validation(
    all_sections: dict[str, list[EvernoteNote]],
    total_notes: int,
) -> list[tuple[str, str]]:
    print("\n[DRY RUN] Validating ENML-to-HTML conversion...")
    errors: list[tuple[str, str]] = []
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
    return errors


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


def _sanitize_section_name(name: str) -> str:
    forbidden = r'?*\\/:<>|&#"%~'
    for ch in forbidden:
        name = name.replace(ch, "_")
    return name.strip()


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
