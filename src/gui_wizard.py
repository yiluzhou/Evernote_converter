"""GUI wizard flow for general users (Tkinter)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from bs4 import BeautifulSoup
from enex_parser import EvernoteNote, enml_to_html, parse_enex
from onenote_uploader import (
    AzureSetupGuidanceError,
    OneNoteRequestSizeLimitError,
    OneNoteUploader,
    analyze_note_multipart_limits,
    get_graph_token,
    is_probable_request_size_error,
)
from update_checker import APP_VERSION, get_latest_release_update

logger = logging.getLogger(__name__)

INTRO_GUIDE_TEXT = """\
Before starting:
1. Export notes from Evernote to .enex files:
   - Evernote desktop -> right-click notebook -> Export notebook...
   - Save exported .enex files anywhere on your computer
"""

AZURE_SETUP_GUIDE_TEXT = """\
Azure app setup (one time):

1. Sign in at https://portal.azure.com
2. Open Microsoft Entra ID -> App registrations -> New registration
3. Name: Evernote Converter (or any name)
4. Supported account types: Personal Microsoft accounts only
5. Register, then copy "Application (client) ID"
6. API permissions -> Microsoft Graph -> Delegated permissions:
   - Notes.Create
   - Notes.ReadWrite
7. Authentication -> Allow public client flows = Yes -> Save

Paste the Application (client) ID below.
"""

AZURE_SETUP_GUIDE_TEXT_NO_ENTRY = """\
Azure app setup (one time):

1. Sign in at https://portal.azure.com
2. Open Microsoft Entra ID -> App registrations -> New registration
3. Name: Evernote Converter (or any name)
4. Supported account types: Personal Microsoft accounts only
5. Register, then copy "Application (client) ID"
6. API permissions -> Microsoft Graph -> Delegated permissions:
   - Notes.Create
   - Notes.ReadWrite
7. Authentication -> Allow public client flows = Yes -> Save
"""

_UPDATE_CHECK_DISABLE_VALUES = {"1", "true", "yes", "on"}
_PREVIEW_MAX_NOTES = 24
_PREVIEW_NOTES_PER_SECTION = 4
_PREVIEW_SNIPPET_CHARS = 240
_MULTIPART_CHECK_DISPLAY_LIMIT = 8
NOTE_UPLOAD_WAIT_SECONDS = 2.0
_ACTION_PREVIOUS = "__previous__"
_ACTION_NEXT = "__next__"

# ---------------------------------------------------------------------------
# Visual Theme
# ---------------------------------------------------------------------------

_COLOR_PRIMARY = "#0F6CBD"
_COLOR_PRIMARY_HOVER = "#115EA3"
_COLOR_PRIMARY_TEXT = "#FFFFFF"
_COLOR_BG = "#F5F5F5"
_COLOR_HEADER_BG = "#0F6CBD"
_COLOR_HEADER_FG = "#FFFFFF"
_COLOR_CONTENT_BG = "#FFFFFF"
_COLOR_TEXT_PRIMARY = "#1B1A19"
_COLOR_TEXT_SECONDARY = "#605E5C"
_COLOR_SEPARATOR = "#E1DFDD"
_COLOR_WARNING_BG = "#FFF4CE"
_COLOR_WARNING_FG = "#605E5C"
_COLOR_TOAST_BG = "#323130"
_COLOR_TOAST_FG = "#FFFFFF"
_COLOR_TEXT_WIDGET_BG = "#FAF9F8"
_COLOR_TEXT_WIDGET_SELECT = "#C7E0F4"
_COLOR_HEADER_ACCENT_1 = "#8CCBFF"
_COLOR_HEADER_ACCENT_2 = "#D0E7FF"

_FONT_HEADING = ("Segoe UI", 15, "bold")
_FONT_SUBHEADING = ("Segoe UI", 11)
_FONT_BODY = ("Segoe UI", 10)
_FONT_BODY_BOLD = ("Segoe UI", 10, "bold")
_FONT_SMALL = ("Segoe UI", 9)
_FONT_MONOSPACE = ("Consolas", 10)
_FONT_STEP = ("Segoe UI", 9)

_PAD = 16
_BUTTON_PAD = 4
_BUTTON_WIDTH = 13
_APP_ICON_IMAGE: tk.PhotoImage | None = None


def _setup_styles(root: tk.Tk) -> None:
    """Configure ttk theme and custom styles."""
    try:
        style = ttk.Style(root)
    except Exception:
        return
    style.theme_use("clam")

    style.configure(".", background=_COLOR_BG, font=_FONT_BODY)
    style.configure("TFrame", background=_COLOR_BG)
    style.configure("Content.TFrame", background=_COLOR_CONTENT_BG)
    style.configure("Header.TFrame", background=_COLOR_HEADER_BG)
    style.configure("ButtonBar.TFrame", background=_COLOR_BG)

    style.configure("TLabel", background=_COLOR_BG, foreground=_COLOR_TEXT_PRIMARY,
                     font=_FONT_BODY)
    style.configure("Header.TLabel", background=_COLOR_HEADER_BG, foreground=_COLOR_HEADER_FG,
                     font=_FONT_HEADING)
    style.configure("HeaderSub.TLabel", background=_COLOR_HEADER_BG, foreground=_COLOR_HEADER_FG,
                     font=_FONT_SUBHEADING)
    style.configure("Step.TLabel", background=_COLOR_HEADER_BG, foreground="#D6ECFF",
                     font=_FONT_STEP)
    style.configure("Content.TLabel", background=_COLOR_CONTENT_BG, foreground=_COLOR_TEXT_PRIMARY,
                     font=_FONT_BODY)
    style.configure("Secondary.TLabel", background=_COLOR_BG, foreground=_COLOR_TEXT_SECONDARY,
                     font=_FONT_SMALL)

    style.configure("TButton", font=_FONT_BODY, padding=(12, 6))
    style.configure("Accent.TButton", font=_FONT_BODY_BOLD, padding=(16, 7),
                     background=_COLOR_PRIMARY, foreground=_COLOR_PRIMARY_TEXT)
    style.map("Accent.TButton",
              background=[("active", _COLOR_PRIMARY_HOVER), ("pressed", _COLOR_PRIMARY_HOVER)],
              foreground=[("active", _COLOR_PRIMARY_TEXT)])

    style.configure("TEntry", font=_FONT_BODY, padding=(6, 4))
    style.configure("TSeparator", background=_COLOR_SEPARATOR)
    style.configure("Horizontal.TProgressbar", troughcolor="#E0E0E0",
                     background=_COLOR_PRIMARY, thickness=22)


# ---------------------------------------------------------------------------
# Reusable dialog building blocks
# ---------------------------------------------------------------------------

def _ensure_app_icon(root: tk.Tk) -> None:
    """Apply a small playful custom icon to top-left window chrome."""
    global _APP_ICON_IMAGE
    try:
        if _APP_ICON_IMAGE is None:
            img = tk.PhotoImage(master=root, width=16, height=16)
            img.put("#0F6CBD", to=(0, 0, 16, 16))
            img.put("#FFFFFF", to=(3, 3, 13, 13))
            img.put("#0F6CBD", to=(5, 5, 11, 6))
            img.put("#0F6CBD", to=(5, 8, 11, 9))
            img.put("#0F6CBD", to=(5, 11, 11, 12))
            _APP_ICON_IMAGE = img
        root.iconphoto(True, _APP_ICON_IMAGE)
    except Exception:
        pass

def _build_header(parent: tk.Toplevel, title: str, subtitle: str = "",
                  step: str = "") -> ttk.Frame:
    """Build a colored header banner at the top of a dialog."""
    header = ttk.Frame(parent, style="Header.TFrame")
    header.pack(fill="x", side="top")

    inner = ttk.Frame(header, style="Header.TFrame")
    inner.pack(fill="x", padx=_PAD + 4, pady=(14, 12))

    title_row = ttk.Frame(inner, style="Header.TFrame")
    title_row.pack(fill="x")
    ttk.Label(title_row, text=title, style="Header.TLabel").pack(side="left")
    if step:
        ttk.Label(title_row, text=step, style="Step.TLabel").pack(side="right")

    if subtitle:
        ttk.Label(inner, text=subtitle, style="HeaderSub.TLabel",
                  wraplength=800).pack(fill="x", pady=(4, 0), anchor="w")

    accents = tk.Canvas(header, height=8, bg=_COLOR_HEADER_BG, highlightthickness=0, bd=0)
    accents.pack(fill="x", side="bottom")
    accents.create_rectangle(0, 0, 1000, 1, fill=_COLOR_HEADER_ACCENT_1, outline="")
    accents.create_rectangle(0, 2, 1000, 3, fill=_COLOR_HEADER_ACCENT_2, outline="")

    return header


def _build_button_bar(parent: tk.Toplevel,
                      buttons: list[tuple[str, object, str]]) -> ttk.Frame:
    """Build a separator + button bar at the bottom of a dialog.

    Each button: (text, command, "accent" | "normal").
    "Previous" is packed left; everything else packed right.
    """
    sep = ttk.Separator(parent, orient="horizontal")
    sep.pack(fill="x", side="bottom")
    bar = ttk.Frame(parent, style="ButtonBar.TFrame")
    bar.pack(fill="x", padx=_PAD, pady=10, side="bottom")

    for text, command, btn_style in buttons:
        s = "Accent.TButton" if btn_style == "accent" else "TButton"
        side = "left" if text == "Previous" else "right"
        w = max(_BUTTON_WIDTH, len(text) + 2)
        ttk.Button(bar, text=text, command=command, style=s,
                   width=w).pack(side=side, padx=_BUTTON_PAD)

    return bar


def _build_styled_text(parent: tk.Misc, height: int = 20,
                       **kwargs) -> tuple[tk.Text, ttk.Scrollbar]:
    """Create a styled Text widget with ttk Scrollbar inside a content frame."""
    frame = ttk.Frame(parent, style="Content.TFrame")
    frame.pack(fill="both", expand=True, padx=_PAD, pady=(8, 4))

    text = tk.Text(frame, wrap="word", font=_FONT_MONOSPACE, height=height,
                   bg=_COLOR_TEXT_WIDGET_BG, fg=_COLOR_TEXT_PRIMARY,
                   selectbackground=_COLOR_TEXT_WIDGET_SELECT,
                   selectforeground=_COLOR_TEXT_PRIMARY,
                   borderwidth=1, relief="solid",
                   padx=8, pady=6, **kwargs)
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    text.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    return text, scrollbar


def _configure_dialog(dialog: tk.Toplevel) -> None:
    """Apply background color to a dialog."""
    dialog.configure(bg=_COLOR_BG)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _format_azure_setup_inline_message(context: str, error: AzureSetupGuidanceError) -> str:
    return (
        f"{context}\n\n"
        f"{error.title}\n"
        f"{error.details}\n\n"
        "Update Azure app setup or Client ID, then click Next."
    )


def _activate_modal_dialog(dialog: tk.Toplevel, root: tk.Tk) -> None:
    """Show dialog reliably even when the root window is withdrawn."""
    _ensure_app_icon(dialog)
    if root.winfo_viewable():
        dialog.transient(root)
    dialog.grab_set()
    dialog.lift()
    try:
        dialog.attributes("-topmost", True)
        dialog.after(120, lambda: dialog.attributes("-topmost", False))
    except Exception:
        pass
    dialog.after(0, lambda: _center_dialog(dialog, root))


def _center_dialog(dialog: tk.Toplevel, root: tk.Misc | None = None) -> None:
    """Center dialogs to avoid OS-driven window cascade/shifting."""
    if not dialog.winfo_exists():
        return

    dialog.update_idletasks()
    width = max(dialog.winfo_width(), dialog.winfo_reqwidth())
    height = max(dialog.winfo_height(), dialog.winfo_reqheight())

    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()

    if root is not None and root.winfo_exists() and root.winfo_viewable():
        root.update_idletasks()
        root_x = root.winfo_rootx()
        root_y = root.winfo_rooty()
        root_w = root.winfo_width()
        root_h = root.winfo_height()
        x = root_x + max((root_w - width) // 2, 0)
        y = root_y + max((root_h - height) // 2, 0)
    else:
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2, 0)

    x = min(max(x, 0), max(screen_w - width, 0))
    y = min(max(y, 0), max(screen_h - height, 0))
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    try:
        dialog.focus_force()
    except Exception:
        pass


def _show_copied_toast(parent: tk.Misc) -> None:
    toast = tk.Toplevel(parent)
    toast.overrideredirect(True)
    try:
        toast.attributes("-topmost", True)
    except Exception:
        pass
    tk.Label(
        toast,
        text="  Copied  ",
        bg=_COLOR_TOAST_BG,
        fg=_COLOR_TOAST_FG,
        font=_FONT_SMALL,
        padx=14,
        pady=6,
    ).pack()
    x = max(parent.winfo_pointerx() - 24, 0)
    y = max(parent.winfo_pointery() + 16, 0)
    toast.geometry(f"+{x}+{y}")
    toast.after(700, toast.destroy)


def _format_eta(seconds: float) -> str:
    total = max(int(round(seconds)), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _estimate_remaining_seconds(
    *,
    total_notes: int,
    done: int,
    elapsed_seconds: float,
    estimated_seconds: float | None,
    progress_samples: list[tuple[float, int]],
) -> float:
    """Estimate remaining time using observed throughput with baseline backoff."""
    if done <= 0 or total_notes <= 0:
        if estimated_seconds is None:
            return 0.0
        return max(estimated_seconds - elapsed_seconds, 0.0)

    remaining_items = max(total_notes - done, 0)
    if remaining_items == 0:
        return 0.0

    elapsed_seconds = max(elapsed_seconds, 1e-6)
    overall_rate = done / elapsed_seconds
    if overall_rate <= 0:
        return 0.0

    recent_rate = overall_rate
    if len(progress_samples) >= 2:
        latest_t, latest_done = progress_samples[-1]
        earliest_t, earliest_done = progress_samples[0]
        delta_t = max(latest_t - earliest_t, 0.0)
        delta_done = max(latest_done - earliest_done, 0)
        if delta_t > 0 and delta_done > 0:
            recent_rate = delta_done / delta_t

    effective_rate = (0.65 * recent_rate) + (0.35 * overall_rate)
    dynamic_remaining = remaining_items / max(effective_rate, 1e-6)

    if estimated_seconds is None:
        return dynamic_remaining

    baseline_remaining = max(estimated_seconds - elapsed_seconds, 0.0)
    progress_ratio = done / max(total_notes, 1)
    baseline_weight = max(0.0, 1.0 - (2.0 * progress_ratio))
    if done >= max(3, int(total_notes * 0.05)):
        baseline_weight = min(baseline_weight, 0.25)

    return (baseline_weight * baseline_remaining) + (
        (1.0 - baseline_weight) * dynamic_remaining
    )


def _enable_right_click_copy(text_widget: tk.Text, parent: tk.Misc) -> None:
    def _copy_selection(_event: tk.Event | None = None) -> str | None:
        try:
            selected = text_widget.selection_get()
        except tk.TclError:
            return None

        if not selected.strip():
            return None

        parent.clipboard_clear()
        parent.clipboard_append(selected)
        parent.update_idletasks()
        _show_copied_toast(parent)
        return "break"

    text_widget.bind("<Button-3>", _copy_selection)


# ---------------------------------------------------------------------------
# Main wizard flow
# ---------------------------------------------------------------------------

def run_gui_wizard(
    default_enex_dir: Path,
    default_notebook_name: str,
    default_client_id: str,
    force_dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Run the full GUI wizard flow."""
    root = tk.Tk()
    _ensure_app_icon(root)
    root.withdraw()
    _setup_styles(root)
    client_id = default_client_id

    try:
        _maybe_alert_update_available(root)

        while True:
            # Step 1: Welcome (intro + azure guide + file selection)
            welcome_result = _show_welcome_dialog(root, default_enex_dir)
            if welcome_result is None:
                return

            selected_files = [welcome_result]
            try:
                all_sections, total_notes = _parse_selected_enex_files(selected_files)
            except Exception as e:
                logger.exception("Failed to parse ENEX file: %s", welcome_result)
                messagebox.showerror(
                    "Failed to Read ENEX",
                    (
                        "Could not parse the selected .enex file.\n\n"
                        f"File: {welcome_result}\n\n"
                        f"Details: {e}\n\n"
                        "Please select another file or re-export from Evernote."
                    ),
                    parent=root,
                )
                continue
            _maybe_warn_multipart_size_risks(root, all_sections)

            # Step 2: Note Preview
            preview_action = _show_note_preview_dialog(root, all_sections, total_notes)
            if preview_action == _ACTION_PREVIOUS:
                continue
            if preview_action != _ACTION_NEXT:
                return

            # Step 3: Azure Setup + Authentication
            azure_inline_message = ""
            restart_requested = False
            while True:
                client_result = _show_azure_setup_dialog(root, client_id, azure_inline_message)
                if client_result is None:
                    return
                if client_result == _ACTION_PREVIOUS:
                    break  # Back to outer loop -> welcome

                client_id = client_result
                auth_result = _authenticate_with_busy_dialog(root, client_id)
                if isinstance(auth_result, AzureSetupGuidanceError):
                    azure_inline_message = _format_azure_setup_inline_message(
                        "We couldn't use this Client ID.",
                        auth_result,
                    )
                    continue
                if auth_result is None:
                    azure_inline_message = (
                        "Sign-in was canceled or did not complete.\n\n"
                        "Please try again."
                    )
                    continue

                uploader = auth_result
                azure_inline_message = ""

                # Step 4 + 5: Notebook selection + section review
                while True:
                    notebook_selection = _choose_notebook(root, uploader, default_notebook_name)
                    if isinstance(notebook_selection, AzureSetupGuidanceError):
                        azure_inline_message = _format_azure_setup_inline_message(
                            "Signed in, but we couldn't access OneNote notebooks.",
                            notebook_selection,
                        )
                        break
                    if notebook_selection is None:
                        return
                    if notebook_selection == _ACTION_PREVIOUS:
                        break

                    notebook_id, notebook_name, notebook_url, was_created = notebook_selection

                    if not was_created:
                        # Step 5: Section review for existing notebooks
                        section_names = list(all_sections.keys())
                        review_action = _show_section_review_dialog(
                            root, uploader, notebook_id, notebook_name,
                            notebook_url, section_names, total_notes,
                        )
                        if isinstance(review_action, AzureSetupGuidanceError):
                            azure_inline_message = _format_azure_setup_inline_message(
                                "Could not list notebook sections.",
                                review_action,
                            )
                            break
                        if review_action == _ACTION_PREVIOUS:
                            continue
                        if review_action is None:
                            return

                    # Upload (section review returned NEXT, or notebook was newly created)
                    estimated_upload_seconds = uploader.estimate_upload_duration_seconds(
                        expected_page_writes=total_notes,
                        expected_section_creates=len(all_sections),
                    )
                    upload_summary = _upload_with_progress_window(
                        root,
                        uploader,
                        notebook_id,
                        all_sections,
                        total_notes,
                        notebook_url=notebook_url,
                        estimated_seconds=estimated_upload_seconds,
                    )
                    if upload_summary.get("aborted"):
                        return
                    if _prompt_upload_another_file(
                        root,
                        notebook_name=notebook_name,
                        notebook_url=notebook_url,
                        upload_summary=upload_summary,
                        total_notes=total_notes,
                    ):
                        restart_requested = True
                    else:
                        return
                    break

                if restart_requested:
                    break

            if restart_requested:
                continue

    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

def _prompt_upload_another_file(
    root: tk.Tk,
    notebook_name: str,
    notebook_url: str,
    upload_summary: dict[str, int | bool],
    total_notes: int,
) -> bool:
    uploaded_notes = int(upload_summary.get("uploaded_notes", 0))
    replaced_pages = int(upload_summary.get("replaced_pages", 0))
    skipped_duplicates = int(upload_summary.get("skipped_duplicates", 0))
    skipped_oversized = int(upload_summary.get("skipped_oversized", 0))
    errors_count = int(upload_summary.get("errors_count", 0))

    lines = [
        "Import finished.",
        "",
        f"Notebook: {notebook_name}",
        f"Uploaded: {uploaded_notes}/{total_notes}",
        f"Replaced duplicate pages: {replaced_pages}",
        f"Skipped duplicates: {skipped_duplicates}",
        f"Skipped oversized notes: {skipped_oversized}",
        f"Errors: {errors_count}",
    ]
    if notebook_url:
        lines.extend(["", f"Notebook link: {notebook_url}"])
    lines.extend(["", "Do you want to import another .enex file now?"])

    return messagebox.askyesno(
        "Import Finished",
        "\n".join(lines),
        parent=root,
    )


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


def _show_welcome_dialog(root: tk.Tk, default_enex_dir: Path) -> Path | None:
    """Step 1: Combined welcome screen with intro guide, Azure guide, and file selection."""
    dialog = tk.Toplevel(root)
    dialog.title("Evernote to OneNote Wizard")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("960x600")

    initial_dir = str(default_enex_dir if default_enex_dir.exists() else Path.cwd())

    result: dict[str, Path | None] = {"value": None}

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    def _next() -> None:
        raw_path = path_var.get().strip()
        if not raw_path:
            messagebox.showwarning("File Required",
                                   "Please select a .enex file.", parent=dialog)
            return
        enex_path = Path(raw_path)
        if not enex_path.is_file():
            messagebox.showwarning("Invalid File",
                                   f"File not found:\n{enex_path}", parent=dialog)
            return
        if enex_path.suffix.lower() != ".enex":
            messagebox.showwarning("Invalid File Type",
                                   "Please select a file with .enex extension.",
                                   parent=dialog)
            return
        result["value"] = enex_path
        dialog.destroy()

    _build_header(dialog, "Evernote to OneNote Wizard",
                  "Import your Evernote notebooks to Microsoft OneNote",
                  step="Step 1 of 5")
    _build_button_bar(dialog, [
        ("Cancel", _cancel, "normal"),
        ("Next", _next, "accent"),
    ])

    # File selection row - packed at bottom above button bar
    file_row = ttk.Frame(dialog)
    file_row.pack(fill="x", padx=_PAD, pady=(4, 0), side="bottom")
    ttk.Label(file_row, text="ENEX file:", font=_FONT_BODY_BOLD).pack(side="left")
    path_var = tk.StringVar()
    ttk.Entry(file_row, textvariable=path_var, font=_FONT_BODY).pack(
        side="left", fill="x", expand=True, padx=(8, 0))

    def _browse() -> None:
        current = path_var.get().strip()
        browse_dir = initial_dir
        if current:
            current_path = Path(current)
            if current_path.exists():
                browse_dir = str(current_path.parent)
        selected_path = filedialog.askopenfilename(
            title="Select .enex file",
            initialdir=browse_dir,
            filetypes=[("Evernote ENEX files", "*.enex"), ("All files", "*.*")],
            parent=dialog,
        )
        if selected_path:
            path_var.set(selected_path)

    ttk.Button(file_row, text="Browse...", command=_browse,
               style="Accent.TButton").pack(side="left", padx=(8, 0))

    # Combined guide text
    body, _ = _build_styled_text(dialog)
    body.insert(tk.END, INTRO_GUIDE_TEXT)
    body.insert(tk.END, "\n")
    body.insert(tk.END, AZURE_SETUP_GUIDE_TEXT_NO_ENTRY)
    body.configure(state="disabled")
    _enable_right_click_copy(body, dialog)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _show_note_preview_dialog(
    root: tk.Tk,
    all_sections: dict[str, list[EvernoteNote]],
    total_notes: int,
) -> str | None:
    preview_items = _collect_note_preview_items(all_sections)

    dialog = tk.Toplevel(root)
    dialog.title("Preview Notes")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("960x660")

    result: dict[str, str | None] = {"value": None}

    def _next() -> None:
        result["value"] = _ACTION_NEXT
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    def _previous() -> None:
        result["value"] = _ACTION_PREVIOUS
        dialog.destroy()

    header_sub = (
        f"Parsed {total_notes} note(s) across {len(all_sections)} section(s). "
        f"Showing {len(preview_items)} sample note(s)."
    )
    _build_header(dialog, "Preview Notes", header_sub, step="Step 2 of 5")
    _build_button_bar(dialog, [
        ("Previous", _previous, "normal"),
        ("Cancel", _cancel, "normal"),
        ("Next", _next, "accent"),
    ])

    body, _ = _build_styled_text(dialog)
    if not preview_items:
        body.insert(tk.END, "No notes found in selected files.")
    else:
        for idx, (section_name, note) in enumerate(preview_items, start=1):
            created = note.created.strftime("%Y-%m-%d %H:%M:%S")
            title = note.title.strip() or "Untitled"
            snippet = _build_note_preview_snippet(note, _PREVIEW_SNIPPET_CHARS)
            body.insert(
                tk.END,
                (
                    f"{idx}. [{section_name}] {title}\n"
                    f"   Date: {created}\n"
                    f"   Preview: {snippet}\n\n"
                ),
            )
    body.configure(state="disabled")

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _collect_note_preview_items(
    all_sections: dict[str, list[EvernoteNote]],
) -> list[tuple[str, EvernoteNote]]:
    preview_items: list[tuple[str, EvernoteNote]] = []
    for section_name, notes in all_sections.items():
        for note in notes[:_PREVIEW_NOTES_PER_SECTION]:
            preview_items.append((section_name, note))
            if len(preview_items) >= _PREVIEW_MAX_NOTES:
                return preview_items
    return preview_items


def _build_note_preview_snippet(note: EvernoteNote, max_chars: int) -> str:
    enml = (note.content_enml or "").strip()
    if not enml:
        return "(No content)"

    text = ""
    try:
        soup = BeautifulSoup(enml, "lxml")
        en_note = soup.find("en-note")
        source = en_note if en_note is not None else soup
        text = source.get_text(" ", strip=True)
    except Exception:
        text = enml

    text = " ".join(text.split())
    if not text:
        return "(No content)"

    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _show_azure_setup_dialog(
    root: tk.Tk,
    default_client_id: str,
    inline_message: str = "",
) -> str | None:
    dialog = tk.Toplevel(root)
    dialog.title("Azure App Setup")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("960x640")

    result: dict[str, str | None] = {"value": None}
    client_id_var = tk.StringVar(value=default_client_id)

    def _next() -> None:
        client_id = client_id_var.get().strip()
        if not client_id:
            messagebox.showwarning("Client ID Required",
                                   "Please paste Application (client) ID before proceeding.",
                                   parent=dialog)
            return
        result["value"] = client_id
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    def _previous() -> None:
        result["value"] = _ACTION_PREVIOUS
        dialog.destroy()

    _build_header(dialog, "Azure App Setup",
                  "Register Azure app and paste Client ID", step="Step 3 of 5")
    _build_button_bar(dialog, [
        ("Previous", _previous, "normal"),
        ("Cancel", _cancel, "normal"),
        ("Next", _next, "accent"),
    ])

    # Client ID entry row - packed at bottom above button bar
    id_row = ttk.Frame(dialog)
    id_row.pack(fill="x", padx=_PAD, pady=(4, 0), side="bottom")
    ttk.Label(id_row, text="Application (client) ID:",
              font=_FONT_BODY_BOLD).pack(side="left")
    ttk.Entry(id_row, textvariable=client_id_var, width=52,
              font=_FONT_BODY).pack(side="left", fill="x", expand=True, padx=(8, 0))

    # Inline warning message (if present)
    if inline_message.strip():
        warn_frame = tk.Frame(dialog, bg=_COLOR_WARNING_BG, bd=1, relief="solid")
        warn_frame.pack(fill="x", padx=_PAD, pady=(8, 0))
        tk.Label(warn_frame, text=inline_message.strip(),
                 bg=_COLOR_WARNING_BG, fg=_COLOR_WARNING_FG,
                 font=_FONT_BODY, justify="left", anchor="w",
                 wraplength=900, padx=12, pady=8).pack(fill="x")

    guide, _ = _build_styled_text(dialog)
    guide.insert(tk.END, AZURE_SETUP_GUIDE_TEXT)
    guide.configure(state="disabled")
    _enable_right_click_copy(guide, dialog)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _authenticate_with_busy_dialog(
    root: tk.Tk,
    client_id: str,
) -> OneNoteUploader | AzureSetupGuidanceError | None:
    dialog = tk.Toplevel(root)
    dialog.title("Connecting")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("860x460")

    _build_header(dialog, "Sign In to Microsoft",
                  "Complete authentication to continue", step="Step 3 of 5")

    status_var = tk.StringVar(value="Preparing sign-in...")
    ttk.Label(dialog, textvariable=status_var, font=_FONT_BODY).pack(
        fill="x", padx=_PAD, pady=(8, 4))

    flow_url = {"value": ""}
    canceled = {"value": False}

    def _open_sign_in_page() -> None:
        if flow_url["value"]:
            try:
                webbrowser.open(flow_url["value"])
            except Exception:
                pass

    def _cancel() -> None:
        canceled["value"] = True
        dialog.destroy()

    # Button bar at bottom
    sep = ttk.Separator(dialog, orient="horizontal")
    sep.pack(fill="x", side="bottom")
    bar = ttk.Frame(dialog, style="ButtonBar.TFrame")
    bar.pack(fill="x", padx=_PAD, pady=10, side="bottom")
    open_button = ttk.Button(bar, text="Open Sign-in Page",
                              command=_open_sign_in_page, style="Accent.TButton")
    open_button.pack(side="left", padx=_BUTTON_PAD)
    open_button.state(["disabled"])
    ttk.Button(bar, text="Cancel", command=_cancel,
               style="TButton", width=_BUTTON_WIDTH).pack(side="right", padx=_BUTTON_PAD)

    instructions, _ = _build_styled_text(dialog, height=10)
    instructions.insert(
        tk.END,
        "Waiting for sign-in instructions...\n\n"
        "If a browser does not open automatically, the sign-in URL and code will appear here.",
    )
    instructions.configure(state="disabled")
    _enable_right_click_copy(instructions, dialog)

    events: queue.Queue[tuple[str, object]] = queue.Queue()

    def _worker() -> None:
        try:
            token = get_graph_token(
                client_id,
                device_flow_callback=lambda flow: events.put(("device_flow", flow)),
            )
            events.put((
                "ok",
                OneNoteUploader(
                    token,
                    token_refresh_callback=lambda: get_graph_token(client_id, silent_only=True),
                ),
            ))
        except AzureSetupGuidanceError as e:
            events.put(("azure", e))
        except Exception as e:
            events.put(("error", str(e)))

    threading.Thread(target=_worker, daemon=True).start()

    result: OneNoteUploader | AzureSetupGuidanceError | None = None

    while dialog.winfo_exists():
        while True:
            try:
                event_type, payload = events.get_nowait()
            except queue.Empty:
                break

            if event_type == "device_flow":
                flow = payload if isinstance(payload, dict) else {}
                message = str(flow.get("message", "")).strip()
                verification_uri = str(flow.get("verification_uri", "")).strip()
                user_code = str(flow.get("user_code", "")).strip()
                flow_url["value"] = verification_uri

                lines: list[str] = []
                if message:
                    lines.append(message)
                    lines.append("")
                if verification_uri:
                    lines.append(f"Sign-in page: {verification_uri}")
                if user_code:
                    lines.append(f"Code: {user_code}")
                if verification_uri or user_code:
                    lines.append("")
                    lines.append("If needed: open the sign-in page and enter the code.")
                text = "\n".join(lines).strip() or "Waiting for sign-in instructions..."

                instructions.configure(state="normal")
                instructions.delete("1.0", tk.END)
                instructions.insert(tk.END, text)
                instructions.configure(state="disabled")
                status_var.set("Waiting for Microsoft sign-in to complete...")
                open_button.state(["!disabled"] if verification_uri else ["disabled"])

            elif event_type == "ok":
                if not canceled["value"]:
                    result = payload if isinstance(payload, OneNoteUploader) else None
                dialog.destroy()
                break

            elif event_type == "azure":
                if not canceled["value"]:
                    result = payload if isinstance(payload, AzureSetupGuidanceError) else None
                dialog.destroy()
                break

            elif event_type == "error":
                if not canceled["value"]:
                    messagebox.showerror("Authentication Failed", str(payload), parent=root)
                dialog.destroy()
                break

        if not dialog.winfo_exists():
            break

        dialog.update_idletasks()
        dialog.update()
        time.sleep(0.05)

    if canceled["value"]:
        return None
    return result


def _choose_notebook(
    root: tk.Tk,
    uploader: OneNoteUploader,
    default_notebook_name: str,
) -> tuple[str, str, str, bool] | str | AzureSetupGuidanceError | None:
    try:
        notebooks = uploader.list_notebooks()
    except AzureSetupGuidanceError as e:
        return e
    except Exception as e:
        messagebox.showerror("Notebook Listing Failed", str(e), parent=root)
        return None

    dialog = tk.Toplevel(root)
    dialog.title("Choose Notebook")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("860x520")

    result: dict[str, tuple[str, str, str, bool] | str | AzureSetupGuidanceError | None] = {"value": None}

    def _use_selected() -> None:
        selection = listbox.curselection()
        if not selection:
            messagebox.showinfo("No Notebook Selected",
                                "Select a notebook from the list, or use 'Create New'.",
                                parent=dialog)
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
            result["value"] = e
            dialog.destroy()
            return
        except Exception as e:
            messagebox.showerror("Notebook Error", str(e), parent=dialog)
            return
        result["value"] = (notebook_id, notebook_name, notebook_url, was_created)
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    def _previous() -> None:
        result["value"] = _ACTION_PREVIOUS
        dialog.destroy()

    _build_header(dialog, "Choose Notebook",
                  "Select an existing notebook or create a new one",
                  step="Step 4 of 5")
    _build_button_bar(dialog, [
        ("Previous", _previous, "normal"),
        ("Cancel", _cancel, "normal"),
        ("Create New", _create_new, "normal"),
        ("Use Selected", _use_selected, "accent"),
    ])

    content = ttk.Frame(dialog, style="Content.TFrame")
    content.pack(fill="both", expand=True, padx=_PAD, pady=8)

    list_frame = ttk.Frame(content, style="Content.TFrame")
    list_frame.pack(fill="both", expand=True)

    listbox = tk.Listbox(list_frame, font=_FONT_BODY,
                         bg=_COLOR_TEXT_WIDGET_BG, fg=_COLOR_TEXT_PRIMARY,
                         selectbackground=_COLOR_PRIMARY,
                         selectforeground=_COLOR_PRIMARY_TEXT,
                         borderwidth=1, relief="solid", activestyle="none")
    yscroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=yscroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    for idx, nb in enumerate(notebooks, start=1):
        name = nb.get("displayName", "(untitled)")
        listbox.insert(tk.END, f"  {idx}. {name}")

    if notebooks:
        listbox.selection_set(0)

    entry_frame = ttk.Frame(content, style="Content.TFrame")
    entry_frame.pack(fill="x", pady=(8, 0))
    ttk.Label(entry_frame, text="New notebook name:",
              style="Content.TLabel", font=_FONT_BODY_BOLD).pack(side="left")
    name_var = tk.StringVar(value=default_notebook_name)
    ttk.Entry(entry_frame, textvariable=name_var,
              font=_FONT_BODY).pack(side="left", fill="x", expand=True, padx=(8, 0))

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _show_section_review_dialog(
    root: tk.Tk,
    uploader: OneNoteUploader,
    notebook_id: str,
    notebook_name: str,
    notebook_url: str,
    incoming_section_names: list[str],
    total_notes: int,
) -> str | AzureSetupGuidanceError | None:
    """Step 5: Review existing sections, highlight conflicts, and confirm upload."""
    # Fetch existing sections before building dialog
    try:
        existing_sections = uploader.list_sections(notebook_id)
    except AzureSetupGuidanceError as e:
        return e
    except Exception as e:
        messagebox.showerror("Section Listing Failed", str(e), parent=root)
        return None

    existing_names = {sec.get("displayName", "") for sec in existing_sections}
    overlapping = [n for n in incoming_section_names if n in existing_names]
    new_only = [n for n in incoming_section_names if n not in existing_names]
    estimated_seconds = uploader.estimate_upload_duration_seconds(
        expected_page_writes=total_notes,
        expected_section_creates=len(new_only),
    )
    rate_minute, rate_hour = uploader.get_write_rate_limits()

    dialog = tk.Toplevel(root)
    dialog.title("Section Review")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("860x480")

    result: dict[str, str | None] = {"value": None}

    def _upload() -> None:
        result["value"] = _ACTION_NEXT
        dialog.destroy()

    def _previous() -> None:
        result["value"] = _ACTION_PREVIOUS
        dialog.destroy()

    def _cancel() -> None:
        result["value"] = None
        dialog.destroy()

    if overlapping:
        _build_header(dialog, "Section Review",
                      f"'{notebook_name}' has conflicting sections",
                      step="Step 5 of 5")
    else:
        _build_header(dialog, "Confirm Upload",
                      f"Add sections to '{notebook_name}'",
                      step="Step 5 of 5")

    btn_label = "Proceed with Upload" if overlapping else "Upload"
    _build_button_bar(dialog, [
        ("Previous", _previous, "normal"),
        ("Cancel", _cancel, "normal"),
        (btn_label, _upload, "accent"),
    ])

    # Warning banner for conflicts
    if overlapping:
        warn_frame = tk.Frame(dialog, bg=_COLOR_WARNING_BG, bd=1, relief="solid")
        warn_frame.pack(fill="x", padx=_PAD, pady=(8, 0))
        tk.Label(warn_frame,
                 text=f"{len(overlapping)} section(s) already exist in this notebook and will be updated.",
                 bg=_COLOR_WARNING_BG, fg=_COLOR_WARNING_FG,
                 font=_FONT_BODY, justify="left", anchor="w",
                 wraplength=800, padx=12, pady=8).pack(fill="x")

    # Notebook link
    if notebook_url:
        link_frame = ttk.Frame(dialog)
        link_frame.pack(fill="x", padx=_PAD, pady=(8, 0))
        ttk.Label(link_frame, text="Notebook link:",
                  font=_FONT_BODY_BOLD).pack(side="left")
        link_entry = ttk.Entry(link_frame, font=_FONT_BODY)
        link_entry.insert(0, notebook_url)
        link_entry.configure(state="readonly")
        link_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))

        def _copy_link() -> None:
            dialog.clipboard_clear()
            dialog.clipboard_append(notebook_url)
            dialog.update_idletasks()
            _show_copied_toast(dialog)

        ttk.Button(link_frame, text="Copy Link", command=_copy_link,
                   style="TButton").pack(side="left")

    eta_frame = ttk.Frame(dialog)
    eta_frame.pack(fill="x", padx=_PAD, pady=(8, 0))
    ttk.Label(eta_frame, text="Estimated upload time:",
              font=_FONT_BODY_BOLD).pack(side="left")
    ttk.Label(
        eta_frame,
        text=(
            f"~{_format_eta(estimated_seconds)} "
            f"(rate limits {rate_minute}/min, {rate_hour}/hour)"
        ),
        style="Content.TLabel",
    ).pack(side="left", padx=(6, 0))

    # Section list
    body, _ = _build_styled_text(dialog)
    if overlapping:
        for name in overlapping:
            body.insert(tk.END, f"  [UPDATE]  Section \"{name}\" already exists and will be updated\n")
        if new_only:
            body.insert(tk.END, "\n")
    for name in new_only:
        body.insert(tk.END, f"  [NEW]     Section \"{name}\" will be added\n")
    if not overlapping and not new_only:
        body.insert(tk.END, "No sections to upload.\n")
    body.configure(state="disabled")

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.wait_window()
    return result["value"]


def _upload_with_progress_window(
    root: tk.Tk,
    uploader: OneNoteUploader,
    notebook_id: str,
    all_sections: dict[str, list[EvernoteNote]],
    total_notes: int,
    notebook_url: str = "",
    estimated_seconds: float | None = None,
) -> dict[str, int | bool]:
    progress = tk.Toplevel(root)
    progress.title("Uploading")
    _configure_dialog(progress)
    _activate_modal_dialog(progress, root)
    progress.geometry("960x560")

    _build_header(progress, "Uploading Notes")

    status_var = tk.StringVar(value="Starting upload...")
    ttk.Label(progress, textvariable=status_var, font=_FONT_BODY).pack(
        fill="x", padx=_PAD, pady=(8, 4))

    if estimated_seconds is not None:
        ttk.Label(
            progress,
            text=(
                "Estimated upload time: "
                f"~{_format_eta(estimated_seconds)} "
                "(actual may be longer due to duplicates/retries/network)"
            ),
            style="Secondary.TLabel",
        ).pack(fill="x", padx=_PAD, pady=(0, 4))

    prog_frame = ttk.Frame(progress)
    prog_frame.pack(fill="x", padx=_PAD, pady=4)
    progress_var = tk.DoubleVar(value=0)
    bar = ttk.Progressbar(prog_frame, maximum=max(total_notes, 1),
                          variable=progress_var)
    bar.pack(side="left", fill="x", expand=True)
    pct_label = ttk.Label(prog_frame, text="0%", font=_FONT_SMALL, width=6)
    pct_label.pack(side="right", padx=(8, 0))
    progress_detail_var = tk.StringVar(value="")
    ttk.Label(progress, textvariable=progress_detail_var, style="Secondary.TLabel").pack(
        fill="x", padx=_PAD, pady=(0, 4))

    text, _ = _build_styled_text(progress, height=18)

    # Bottom button bar
    sep = ttk.Separator(progress, orient="horizontal")
    sep.pack(fill="x", side="bottom")
    button_bar = ttk.Frame(progress, style="ButtonBar.TFrame")
    button_bar.pack(fill="x", padx=_PAD, pady=10, side="bottom")
    pause_button = ttk.Button(button_bar, text="Pause", style="TButton")
    pause_button.pack(side="left", padx=_BUTTON_PAD)
    resume_button = ttk.Button(button_bar, text="Resume", style="TButton")
    resume_button.pack(side="left", padx=_BUTTON_PAD)
    stop_button = ttk.Button(button_bar, text="Stop", style="TButton")
    stop_button.pack(side="right", padx=_BUTTON_PAD)
    exit_button = ttk.Button(button_bar, text="Exit", style="Accent.TButton")
    exit_button.pack(side="right", padx=_BUTTON_PAD)
    resume_button.state(["disabled"])

    # Queue for worker -> UI events
    ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    # Queue for UI -> worker responses (duplicate prompt answers)
    response_queue: queue.Queue[str] = queue.Queue()
    progress_started_at = time.monotonic()
    progress_snapshot: dict[str, int] = {
        "done": 0,
        "uploaded_notes": 0,
        "skipped_duplicates": 0,
        "skipped_oversized": 0,
        "errors_count": 0,
    }
    progress_samples: list[tuple[float, int]] = []
    pause_event = threading.Event()
    pause_event.set()
    stop_event = threading.Event()
    paused_since: dict[str, float | None] = {"value": None}
    paused_total_seconds = {"value": 0.0}
    worker_done = {"value": False}
    exit_after_stop = {"value": False}
    can_close = {"value": False}

    def _refresh_progress_details(force_complete: bool = False) -> None:
        done = int(progress_snapshot.get("done", 0))
        uploaded = int(progress_snapshot.get("uploaded_notes", 0))
        now = time.monotonic()
        paused_seconds = paused_total_seconds["value"]
        if paused_since["value"] is not None:
            paused_seconds += max(now - paused_since["value"], 0.0)
        elapsed_seconds = max((now - progress_started_at) - paused_seconds, 0.0)
        progress_samples.append((elapsed_seconds, done))
        progress_samples[:] = progress_samples[-60:]

        processed_pct = int((done / max(total_notes, 1)) * 100)
        uploaded_pct = int((uploaded / max(total_notes, 1)) * 100)

        if force_complete:
            remaining_seconds = 0.0
        else:
            remaining_seconds = _estimate_remaining_seconds(
                total_notes=total_notes,
                done=done,
                elapsed_seconds=elapsed_seconds,
                estimated_seconds=estimated_seconds,
                progress_samples=progress_samples,
            )

        progress_detail_var.set(
            "Uploaded: "
            f"{uploaded}/{total_notes} ({uploaded_pct}%) | "
            "Processed: "
            f"{done}/{total_notes} ({processed_pct}%) | "
            f"Elapsed: {_format_eta(elapsed_seconds)} | "
            f"Remaining: {_format_eta(remaining_seconds)}"
        )

    def _mark_pause_started() -> None:
        if paused_since["value"] is None:
            paused_since["value"] = time.monotonic()

    def _mark_pause_ended() -> None:
        if paused_since["value"] is not None:
            paused_total_seconds["value"] += max(
                time.monotonic() - paused_since["value"],
                0.0,
            )
            paused_since["value"] = None

    def _pause_upload() -> None:
        if worker_done["value"] or stop_event.is_set():
            return
        if not pause_event.is_set():
            return
        pause_event.clear()
        _mark_pause_started()
        pause_button.state(["disabled"])
        resume_button.state(["!disabled"])
        status_var.set("Paused. Click Resume to continue.")
        text.insert(tk.END, "Paused by user.\n")
        text.see(tk.END)
        _refresh_progress_details()

    def _resume_upload() -> None:
        if worker_done["value"] or stop_event.is_set():
            return
        if pause_event.is_set():
            return
        pause_event.set()
        _mark_pause_ended()
        pause_button.state(["!disabled"])
        resume_button.state(["disabled"])
        status_var.set("Resuming upload...")
        text.insert(tk.END, "Resumed by user.\n")
        text.see(tk.END)
        _refresh_progress_details()

    def _stop_upload() -> None:
        if worker_done["value"] or stop_event.is_set():
            return
        stop_event.set()
        pause_event.set()
        _mark_pause_ended()
        pause_button.state(["disabled"])
        resume_button.state(["disabled"])
        stop_button.state(["disabled"])
        status_var.set("Stopping after current request...")
        text.insert(tk.END, "Stop requested by user. Waiting for current request to finish...\n")
        text.see(tk.END)

    def _close_window_now() -> None:
        try:
            progress.destroy()
        except Exception:
            pass

    def _exit_upload() -> None:
        if can_close["value"] or worker_done["value"]:
            _close_window_now()
            return

        should_exit = messagebox.askyesno(
            "Exit Upload",
            "Stop current upload and close this window?",
            parent=progress,
        )
        if not should_exit:
            return

        exit_after_stop["value"] = True
        _stop_upload()
        status_var.set("Stopping and exiting after current request...")

    pause_button.configure(command=_pause_upload)
    resume_button.configure(command=_resume_upload)
    stop_button.configure(command=_stop_upload)
    exit_button.configure(command=_exit_upload)

    def _worker() -> None:
        errors: list[tuple[str, str]] = []
        uploaded_notes = 0
        skipped_duplicates = 0
        skipped_oversized = 0
        replaced_pages = 0
        duplicate_policy: str | None = None
        current_done = 0
        processed_any_note = False

        def _emit_progress() -> None:
            ui_queue.put(("progress", {
                "done": current_done,
                "uploaded_notes": uploaded_notes,
                "skipped_duplicates": skipped_duplicates,
                "skipped_oversized": skipped_oversized,
                "errors_count": len(errors),
            }))

        def _make_summary(extra_errors: int = 0, aborted: bool = False) -> dict:
            return {
                "uploaded_notes": uploaded_notes,
                "skipped_duplicates": skipped_duplicates,
                "skipped_oversized": skipped_oversized,
                "replaced_pages": replaced_pages,
                "errors_count": len(errors) + extra_errors,
                "aborted": aborted,
                "stopped_by_user": bool(aborted and stop_event.is_set()),
            }

        def _wait_for_resume_or_stop() -> bool:
            while True:
                if stop_event.is_set():
                    return True
                if pause_event.is_set():
                    return False
                time.sleep(0.1)

        def _abort_due_to_user_stop() -> None:
            ui_queue.put(("done", _make_summary(aborted=True)))

        for section_name, notes in all_sections.items():
            if _wait_for_resume_or_stop():
                _abort_due_to_user_stop()
                return
            ui_queue.put(("status", f"Section: {section_name}"))

            try:
                section_id, sec_created = uploader.get_or_create_section(notebook_id, section_name)
            except AzureSetupGuidanceError as e:
                ui_queue.put(("azure_error", (
                    e,
                    "Cannot list/create sections. Azure app permissions may be incomplete.",
                    _make_summary(extra_errors=1, aborted=True),
                )))
                return

            guid_index: dict[str, list[dict]] = {}
            fallback_index: dict[tuple[str, str], list[dict]] = {}

            if sec_created:
                ui_queue.put(("log", f"Created section: {section_name}"))
            else:
                ui_queue.put(("log", f"Using existing section: {section_name}"))
                try:
                    guid_index, fallback_index, existing_count = uploader.build_section_duplicate_index(
                        section_id, notes
                    )
                except AzureSetupGuidanceError as e:
                    ui_queue.put(("azure_error", (
                        e,
                        "Cannot scan existing pages for duplicates. Azure app permissions may be incomplete.",
                        _make_summary(extra_errors=1, aborted=True),
                    )))
                    return
                ui_queue.put(("log", f"  Scanned {existing_count} existing page(s) for duplicate detection"))

            for note in notes:
                if processed_any_note and NOTE_UPLOAD_WAIT_SECONDS > 0:
                    ui_queue.put(("status", f"Waiting {int(NOTE_UPLOAD_WAIT_SECONDS)}s before next note..."))
                    remaining_wait = NOTE_UPLOAD_WAIT_SECONDS
                    while remaining_wait > 0:
                        if _wait_for_resume_or_stop():
                            _abort_due_to_user_stop()
                            return
                        step_wait = min(0.1, remaining_wait)
                        time.sleep(step_wait)
                        remaining_wait -= step_wait

                if _wait_for_resume_or_stop():
                    _abort_due_to_user_stop()
                    return
                ui_queue.put(("status", f"Uploading note: {note.title}"))

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
                        ui_queue.put(("ask_duplicate", (note, duplicate_pages)))
                        action = response_queue.get()
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
                        ui_queue.put(("log", f"SKIP duplicate note: {note.title}"))
                        current_done += 1
                        _emit_progress()
                        continue

                    if action == "replace":
                        delete_failed = False
                        for page in duplicate_pages:
                            if _wait_for_resume_or_stop():
                                _abort_due_to_user_stop()
                                return
                            page_id = page.get("id", "")
                            if not page_id:
                                continue
                            try:
                                uploader.delete_page(page_id)
                                replaced_pages += 1
                            except AzureSetupGuidanceError as e:
                                ui_queue.put(("azure_error", (
                                    e,
                                    "Cannot replace duplicate pages. Azure app permissions may be incomplete.",
                                    _make_summary(extra_errors=1, aborted=True),
                                )))
                                return
                            except Exception as e:
                                delete_failed = True
                                errors.append((note.title, f"Failed deleting page '{page_id}': {e}"))
                                ui_queue.put(("log", f"ERROR deleting duplicate page '{page_id}': {e}"))

                        if delete_failed:
                            current_done += 1
                            _emit_progress()
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
                    ui_queue.put(("azure_error", (
                        e,
                        "Cannot create page in OneNote. Azure app permissions may be incomplete.",
                        _make_summary(extra_errors=1, aborted=True),
                    )))
                    return
                except OneNoteRequestSizeLimitError as e:
                    skipped_oversized += 1
                    ui_queue.put(("log", f"SKIP oversized note: {note.title} ({e})"))
                except Exception as e:
                    if is_probable_request_size_error(e):
                        skipped_oversized += 1
                        ui_queue.put(("log", f"SKIP oversized note: {note.title} ({e})"))
                    else:
                        errors.append((note.title, str(e)))
                        ui_queue.put(("log", f"ERROR: '{note.title}': {e}"))

                current_done += 1
                _emit_progress()
                processed_any_note = True

        ui_queue.put(("done", _make_summary()))

    threading.Thread(target=_worker, daemon=True).start()

    final_result: dict[str, int | bool] = {
        "uploaded_notes": 0,
        "skipped_duplicates": 0,
        "skipped_oversized": 0,
        "replaced_pages": 0,
        "errors_count": 0,
        "aborted": True,
        "stopped_by_user": False,
    }
    progress.protocol("WM_DELETE_WINDOW", _exit_upload)

    def _poll_queue() -> None:
        nonlocal final_result
        if not progress.winfo_exists():
            return

        while True:
            try:
                event_type, payload = ui_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "status":
                status_var.set(str(payload))
            elif event_type == "log":
                text.insert(tk.END, str(payload) + "\n")
                text.see(tk.END)
            elif event_type == "progress":
                done = 0
                if isinstance(payload, dict):
                    progress_snapshot.update({
                        "done": int(payload.get("done", 0)),
                        "uploaded_notes": int(payload.get("uploaded_notes", 0)),
                        "skipped_duplicates": int(payload.get("skipped_duplicates", 0)),
                        "skipped_oversized": int(payload.get("skipped_oversized", 0)),
                        "errors_count": int(payload.get("errors_count", 0)),
                    })
                    done = progress_snapshot["done"]
                else:
                    done = int(payload)
                    progress_snapshot["done"] = done
                progress_var.set(done)
                pct = int((done / max(total_notes, 1)) * 100)
                pct_label.configure(text=f"{pct}%")
                _refresh_progress_details()
            elif event_type == "ask_duplicate":
                note, dup_pages = payload
                action = _prompt_duplicate_action_gui(root, note, dup_pages, uploader)
                response_queue.put(action)
            elif event_type == "azure_error":
                error, context, summary = payload
                final_result = summary
                worker_done["value"] = True
                can_close["value"] = True
                pause_button.state(["disabled"])
                resume_button.state(["disabled"])
                stop_button.state(["disabled"])
                try:
                    progress.destroy()
                except Exception:
                    pass
                _show_azure_guidance_dialog(root, error, context)
                return
            elif event_type == "done":
                final_result = payload
                worker_done["value"] = True
                can_close["value"] = True
                _mark_pause_ended()
                pause_button.state(["disabled"])
                resume_button.state(["disabled"])
                stop_button.state(["disabled"])
                exit_button.configure(text="Close")

                stopped_by_user = bool(final_result.get("stopped_by_user", False))
                if stopped_by_user:
                    status_var.set("Upload stopped by user. Review summary, then click Close.")
                else:
                    status_var.set("Upload complete. Review summary, then click Close.")

                progress_snapshot.update({
                    "uploaded_notes": int(final_result["uploaded_notes"]),
                    "skipped_duplicates": int(final_result["skipped_duplicates"]),
                    "skipped_oversized": int(final_result["skipped_oversized"]),
                    "errors_count": int(final_result["errors_count"]),
                })
                if not stopped_by_user:
                    progress_snapshot["done"] = total_notes

                text.insert(
                    tk.END,
                    "\nUpload stopped by user.\n" if stopped_by_user else "\nUpload complete.\n",
                )
                text.insert(tk.END, f"Uploaded: {final_result['uploaded_notes']}/{total_notes}\n")
                text.insert(tk.END, f"Replaced duplicate pages: {final_result['replaced_pages']}\n")
                text.insert(tk.END, f"Skipped duplicates: {final_result['skipped_duplicates']}\n")
                text.insert(tk.END, f"Skipped oversized notes: {final_result['skipped_oversized']}\n")
                text.insert(tk.END, f"Errors: {final_result['errors_count']}\n")
                if notebook_url:
                    text.insert(tk.END, f"Notebook link: {notebook_url}\n")
                text.see(tk.END)
                if not stopped_by_user:
                    progress_var.set(max(total_notes, 1))
                    pct_label.configure(text="100%")
                    _refresh_progress_details(force_complete=True)
                else:
                    current_done = int(progress_snapshot.get("done", 0))
                    progress_var.set(current_done)
                    pct = int((current_done / max(total_notes, 1)) * 100)
                    pct_label.configure(text=f"{pct}%")
                    _refresh_progress_details()
                exit_button.focus_set()

                if exit_after_stop["value"]:
                    _close_window_now()
                    return
                return

        _refresh_progress_details()
        if progress.winfo_exists():
            progress.after(50, _poll_queue)

    progress.after(50, _poll_queue)
    progress.wait_window()

    return final_result


def _show_azure_guidance_dialog(
    parent: tk.Misc,
    error: AzureSetupGuidanceError,
    context: str,
) -> None:
    dialog = tk.Toplevel(parent)
    dialog.title("Azure Setup Problem")
    _configure_dialog(dialog)
    if parent.winfo_viewable():
        dialog.transient(parent)
    dialog.grab_set()
    dialog.lift()
    try:
        dialog.attributes("-topmost", True)
        dialog.after(120, lambda: dialog.attributes("-topmost", False))
    except Exception:
        pass
    dialog.geometry("960x640")

    _build_header(dialog, "Azure Setup Problem", error.title)

    body_text = (
        f"{context}\n\n"
        f"{error.details}\n\n"
        "Quick checklist:\n"
        "1. Confirm the Azure app settings match the setup steps.\n"
        "2. Enter the Client ID again.\n"
        "3. If needed, ask ChatGPT with the prompt below.\n\n"
        "ChatGPT prompt:\n"
        f"{error.chatgpt_prompt}"
    )

    ttk.Label(dialog, text="Tip: select text and right-click to copy.",
              style="Secondary.TLabel").pack(fill="x", padx=_PAD, pady=(0, 4), side="bottom")

    text, _ = _build_styled_text(dialog)
    text.insert(tk.END, body_text)
    text.configure(state="disabled")
    _enable_right_click_copy(text, dialog)

    dialog.wait_window()


def _prompt_duplicate_action_gui(
    root: tk.Tk,
    note: EvernoteNote,
    duplicate_pages: list[dict],
    uploader: OneNoteUploader,
) -> str:
    dialog = tk.Toplevel(root)
    dialog.title("Duplicate Detected")
    _configure_dialog(dialog)
    _activate_modal_dialog(dialog, root)
    dialog.geometry("960x540")

    result = {"value": "skip"}

    def choose(action: str) -> None:
        result["value"] = action
        dialog.destroy()

    _build_header(dialog, "Duplicate Detected", f"Note: {note.title}")

    # Button bar
    sep = ttk.Separator(dialog, orient="horizontal")
    sep.pack(fill="x", side="bottom")
    bar = ttk.Frame(dialog, style="ButtonBar.TFrame")
    bar.pack(fill="x", padx=_PAD, pady=10, side="bottom")
    ttk.Button(bar, text="Replace", command=lambda: choose("replace"),
               style="TButton", width=_BUTTON_WIDTH).pack(side="left", padx=_BUTTON_PAD)
    ttk.Button(bar, text="Replace All", command=lambda: choose("replace_all"),
               style="TButton", width=_BUTTON_WIDTH).pack(side="left", padx=_BUTTON_PAD)
    ttk.Button(bar, text="Skip", command=lambda: choose("skip"),
               style="Accent.TButton", width=_BUTTON_WIDTH).pack(side="right", padx=_BUTTON_PAD)
    ttk.Button(bar, text="Skip All", command=lambda: choose("skip_all"),
               style="TButton", width=_BUTTON_WIDTH).pack(side="right", padx=_BUTTON_PAD)

    header_lines = [
        "Incoming note:",
        f"  Title: {note.title}",
        f"  GUID: {note.guid or '(missing)'}",
        f"  Created: {note.created.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "Matching existing page(s):",
    ]

    body, _ = _build_styled_text(dialog)

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


def _maybe_warn_multipart_size_risks(
    root: tk.Tk,
    all_sections: dict[str, list[EvernoteNote]],
) -> None:
    """Show an early warning if notes may hit OneNote multipart size limits."""
    hard_hits: list[tuple[str, str, str]] = []
    warning_hits: list[tuple[str, str, str]] = []

    for section_name, notes in all_sections.items():
        for note in notes:
            if not note.resources:
                continue

            try:
                resource_map = {r.md5_hash: r for r in note.resources}
                html_body = enml_to_html(note.content_enml, resource_map)
            except Exception:
                continue

            analysis = analyze_note_multipart_limits(note, html_body)
            if not analysis["uses_multipart"]:
                continue

            title = (note.title or "").strip() or "Untitled"
            if analysis["violations"]:
                hard_hits.append((section_name, title, "; ".join(analysis["violations"])))
            elif analysis["warnings"]:
                warning_hits.append((section_name, title, "; ".join(analysis["warnings"])))

    if not hard_hits and not warning_hits:
        return

    lines: list[str] = []
    if hard_hits:
        lines.append(
            f"{len(hard_hits)} note(s) exceed OneNote multipart size limits "
            "and will be skipped automatically during upload:"
        )
        for section_name, title, details in hard_hits[:_MULTIPART_CHECK_DISPLAY_LIMIT]:
            lines.append(f"- [{section_name}] {title}: {details}")
        if len(hard_hits) > _MULTIPART_CHECK_DISPLAY_LIMIT:
            remaining = len(hard_hits) - _MULTIPART_CHECK_DISPLAY_LIMIT
            lines.append(f"... and {remaining} more")

    if warning_hits:
        if lines:
            lines.append("")
        lines.append(
            f"{len(warning_hits)} note(s) are close to multipart limits "
            "(may fail depending on payload overhead):"
        )
        for section_name, title, details in warning_hits[:_MULTIPART_CHECK_DISPLAY_LIMIT]:
            lines.append(f"- [{section_name}] {title}: {details}")
        if len(warning_hits) > _MULTIPART_CHECK_DISPLAY_LIMIT:
            remaining = len(warning_hits) - _MULTIPART_CHECK_DISPLAY_LIMIT
            lines.append(f"... and {remaining} more")

    messagebox.showwarning(
        "OneNote Upload Limit Check",
        "\n".join(lines),
        parent=root,
    )


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
