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
