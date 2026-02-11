"""Tests for GUI wizard control-flow behavior."""

import os
import sys
import types
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Provide a lightweight msal stub so gui_wizard -> onenote_uploader imports in
# environments where auth dependencies are not installed.
if "msal" not in sys.modules:
    sys.modules["msal"] = types.SimpleNamespace(
        SerializableTokenCache=object,
        PublicClientApplication=object,
    )

import gui_wizard  # noqa: E402
from onenote_uploader import AzureSetupGuidanceError  # noqa: E402


class _FakeRoot:
    def withdraw(self) -> None:
        return None

    def destroy(self) -> None:
        return None


def test_invalid_client_id_shows_guidance_and_retries(monkeypatch):
    # Avoid constructing real Tk windows in test environments.
    monkeypatch.setattr(gui_wizard.tk, "Tk", lambda: _FakeRoot())
    monkeypatch.setattr(gui_wizard, "_setup_styles", lambda root: None)

    monkeypatch.setattr(gui_wizard, "_maybe_alert_update_available", lambda root: None)
    monkeypatch.setattr(gui_wizard, "_show_welcome_dialog", lambda root, d: Path("dummy.enex"))
    monkeypatch.setattr(gui_wizard, "_parse_selected_enex_files", lambda files: ({"sec": []}, 0))
    monkeypatch.setattr(
        gui_wizard, "_show_note_preview_dialog", lambda root, all_sections, total_notes: gui_wizard._ACTION_NEXT
    )

    setup_calls: list[str] = []
    setup_results = iter(["bad-client-id", "good-client-id"])

    inline_messages: list[str] = []

    def _fake_setup_dialog(root, default_client_id, inline_message=""):
        setup_calls.append(default_client_id)
        inline_messages.append(inline_message)
        return next(setup_results)

    monkeypatch.setattr(gui_wizard, "_show_azure_setup_dialog", _fake_setup_dialog)

    auth_calls: list[str] = []
    fake_guidance = AzureSetupGuidanceError(
        "Azure app client ID is invalid",
        "Bad client ID",
        "prompt",
    )
    fake_uploader = object()

    def _fake_auth(root, client_id):
        auth_calls.append(client_id)
        if client_id == "bad-client-id":
            return fake_guidance
        return fake_uploader

    monkeypatch.setattr(gui_wizard, "_authenticate_with_busy_dialog", _fake_auth)

    monkeypatch.setattr(gui_wizard, "_show_azure_guidance_dialog", lambda *args, **kwargs: None)

    # Exit after successful auth to keep this test focused on retry control-flow.
    monkeypatch.setattr(gui_wizard, "_choose_notebook", lambda root, uploader, name: None)

    gui_wizard.run_gui_wizard(
        default_enex_dir=Path("."),
        default_notebook_name="Notebook",
        default_client_id="",
    )

    assert auth_calls == ["bad-client-id", "good-client-id"]
    assert len(setup_calls) == 2
    assert inline_messages[0] == ""
    assert "couldn't use this Client ID" in inline_messages[1]


def test_parse_error_shows_dialog_and_returns_to_file_selection(monkeypatch):
    monkeypatch.setattr(gui_wizard.tk, "Tk", lambda: _FakeRoot())
    monkeypatch.setattr(gui_wizard, "_setup_styles", lambda root: None)
    monkeypatch.setattr(gui_wizard, "_maybe_alert_update_available", lambda root: None)
    monkeypatch.setattr(gui_wizard, "_maybe_warn_multipart_size_risks", lambda root, all_sections: None)

    welcome_results = iter([Path("bad.enex"), None])
    monkeypatch.setattr(gui_wizard, "_show_welcome_dialog", lambda root, d: next(welcome_results))
    monkeypatch.setattr(
        gui_wizard,
        "_parse_selected_enex_files",
        lambda files: (_ for _ in ()).throw(RuntimeError("xml parse failed")),
    )

    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui_wizard.messagebox,
        "showerror",
        lambda title, message, parent=None: errors.append((title, message)),
    )

    gui_wizard.run_gui_wizard(
        default_enex_dir=Path("."),
        default_notebook_name="Notebook",
        default_client_id="",
    )

    assert len(errors) == 1
    assert errors[0][0] == "Failed to Read ENEX"
    assert "bad.enex" in errors[0][1]


def test_successful_upload_can_restart_and_import_another_file(monkeypatch):
    monkeypatch.setattr(gui_wizard.tk, "Tk", lambda: _FakeRoot())
    monkeypatch.setattr(gui_wizard, "_setup_styles", lambda root: None)
    monkeypatch.setattr(gui_wizard, "_maybe_alert_update_available", lambda root: None)
    monkeypatch.setattr(gui_wizard, "_maybe_warn_multipart_size_risks", lambda root, all_sections: None)

    welcome_results = iter([Path("first.enex"), Path("second.enex"), None])
    monkeypatch.setattr(gui_wizard, "_show_welcome_dialog", lambda root, d: next(welcome_results))

    parse_calls: list[str] = []

    def _fake_parse_selected(files):
        parse_calls.append(files[0].name)
        return {"sec": []}, 0

    monkeypatch.setattr(gui_wizard, "_parse_selected_enex_files", _fake_parse_selected)
    monkeypatch.setattr(
        gui_wizard, "_show_note_preview_dialog", lambda root, all_sections, total_notes: gui_wizard._ACTION_NEXT
    )
    monkeypatch.setattr(gui_wizard, "_show_azure_setup_dialog", lambda root, cid, msg: "client-id")

    class _Uploader:
        @staticmethod
        def estimate_upload_duration_seconds(expected_page_writes, expected_section_creates):
            return 0

    monkeypatch.setattr(gui_wizard, "_authenticate_with_busy_dialog", lambda root, cid: _Uploader())
    monkeypatch.setattr(
        gui_wizard,
        "_choose_notebook",
        lambda root, uploader, default_name: ("nb-id", "Notebook", "https://example.test/notebook", True),
    )

    uploads = {"count": 0}

    def _fake_upload(*args, **kwargs):
        uploads["count"] += 1
        return {
            "uploaded_notes": 0,
            "skipped_duplicates": 0,
            "skipped_oversized": 0,
            "replaced_pages": 0,
            "errors_count": 0,
            "aborted": False,
        }

    monkeypatch.setattr(gui_wizard, "_upload_with_progress_window", _fake_upload)

    prompt_answers = iter([True, False])
    monkeypatch.setattr(
        gui_wizard,
        "_prompt_upload_another_file",
        lambda root, notebook_name, notebook_url, upload_summary, total_notes: next(prompt_answers),
    )

    gui_wizard.run_gui_wizard(
        default_enex_dir=Path("."),
        default_notebook_name="Notebook",
        default_client_id="",
    )

    assert parse_calls == ["first.enex", "second.enex"]
    assert uploads["count"] == 2


def test_remaining_estimate_adapts_to_observed_speed_at_high_progress():
    remaining = gui_wizard._estimate_remaining_seconds(
        total_notes=277,
        done=220,
        elapsed_seconds=11 * 60 + 2,
        estimated_seconds=(47 * 60 + 45),
        progress_samples=[(600.0, 200), (662.0, 220)],
    )
    assert remaining < 10 * 60


def test_remaining_estimate_keeps_baseline_weight_early():
    remaining = gui_wizard._estimate_remaining_seconds(
        total_notes=277,
        done=2,
        elapsed_seconds=60.0,
        estimated_seconds=(47 * 60 + 45),
        progress_samples=[(0.0, 0), (60.0, 2)],
    )
    assert remaining > 30 * 60
